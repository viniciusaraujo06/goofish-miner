from datetime import datetime

import pytest
from conftest import BUSCA, LOJA

from goofish_miner import coletor, db


def _cartao(item_id, status=0, titulo="任天堂 Switch OLED 白色", preco="1500"):
    return {"cardData": {"id": item_id, "title": titulo, "priceInfo": {"price": preco}, "itemStatus": status,
                         "detailParams": {"itemId": item_id, "soldPrice": preco}}}


def _resultado(item_id, titulo="客制化机械键盘", preco="399"):
    return {"exContent": {"itemId": item_id, "title": titulo, "detailParams": {"soldPrice": preco}}, "clickParam": {"args": {}}}


def _avisos(con, tipo=None):
    sql, p = "SELECT item_id FROM aviso", ()
    if tipo:
        sql, p = sql + " WHERE tipo = ?", (tipo,)
    return [r["item_id"] for r in con.execute(sql + " ORDER BY id", p)]


def test_janela_horario(cfg):
    assert coletor.em_janela(cfg, datetime(2026, 9, 7, 10, 0, tzinfo=coletor.BRASILIA))
    assert not coletor.em_janela(cfg, datetime(2026, 9, 7, 23, 0, tzinfo=coletor.BRASILIA))
    assert not coletor.em_janela(cfg, datetime(2026, 9, 7, 7, 59, tzinfo=coletor.BRASILIA))


def test_ajustes_do_bot_sobrepoem_o_arquivo(cfg, con):
    assert coletor.ajustes(con, cfg) == (8, 20, 50)
    db.gravar_flag(con, "janela_inicio", "6")
    db.gravar_flag(con, "intervalo_min_minutos", "30")
    assert coletor.ajustes(con, cfg) == (6, 20, 30)


def test_mesmo_item_na_rodada_gera_uma_linha(cfg, con):
    v = coletor.Varredura(cfg, con)
    assert v._processar_resultado(_resultado("1"), BUSCA) == "novo"
    assert v._processar_resultado(_resultado("1", preco="300"), BUSCA) == "repetido"   # o primeiro a gravar vence
    assert con.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0] == 1
    assert v.novos == ["1"]


def test_filtro_da_fonte_barra_antes_de_gravar(cfg, con):
    v = coletor.Varredura(cfg, con)
    assert v._processar_resultado(_resultado("1", preco="900"), BUSCA) == "filtro"          # acima do preco_max
    assert v._processar_resultado(_resultado("2", titulo="机械键盘 键帽 一套"), BUSCA) == "filtro"   # termo excluído
    assert v._processar_resultado(_resultado("3"), BUSCA) == "novo"
    assert v.cont["filtro"] == 2 and v.novos == ["3"]


def test_vendido_nao_entra(cfg, con):
    v = coletor.Varredura(cfg, con)
    assert v._processar_cartao(_cartao("1", status=1), LOJA) == "vendido"
    assert v._processar_cartao(_cartao("2", status=0), LOJA) == "novo"
    assert v.novos == ["2"] and v.cont["vendido"] == 1


def test_preco_invalido_descartado_com_contagem(cfg, con):
    v = coletor.Varredura(cfg, con)
    assert v._processar_cartao(_cartao("1", preco="面议"), LOJA) == "preco_invalido"
    assert v.cont["preco_invalido"] == 1 and v.novos == []


def test_reobservacao_igual_nao_grava(cfg, con):
    coletor.Varredura(cfg, con)._processar_cartao(_cartao("1"), LOJA)
    v2 = coletor.Varredura(cfg, con)                         # rodada seguinte
    assert v2._processar_cartao(_cartao("1"), LOJA) == "igual"
    v3 = coletor.Varredura(cfg, con)
    assert v3._processar_cartao(_cartao("1", preco="1400"), LOJA) == "mudou"
    assert con.execute("SELECT COUNT(*) FROM snapshot").fetchone()[0] == 2


def test_estreia_da_fonte_vira_linha_de_base_sem_aviso(cfg, con):
    v = coletor.Varredura(cfg, con)
    v._processar_resultado(_resultado("1"), BUSCA, estreia=True)
    v._processar_resultado(_resultado("2"), BUSCA, estreia=False)
    assert _avisos(con, "base") == ["1"]                    # só o da estreia; o outro fica pro notificador


class _PaginaFalsa:
    """Responde às chamadas de mtop com dados prontos, sem navegador."""


def test_busca_registra_a_fonte_ao_terminar(cfg, con, monkeypatch):
    paginas = {1: {"data": {"resultList": [{"data": {"item": {"main": _resultado("1")}}}],
                            "resultInfo": {"hasNextPage": False}}}}
    monkeypatch.setattr(coletor.mtop, "busca", lambda page, termo, pagina: paginas[pagina])
    v = coletor.Varredura(cfg, con)
    assert not db.fonte_conhecida(con, BUSCA.chave)
    v.busca(_PaginaFalsa(), BUSCA)
    assert db.fonte_conhecida(con, BUSCA.chave)
    assert _avisos(con, "base") == ["1"]                    # primeira leitura: base
    monkeypatch.setattr(coletor.mtop, "busca", lambda page, termo, pagina: {"data": {
        "resultList": [{"data": {"item": {"main": _resultado("2")}}}], "resultInfo": {"hasNextPage": False}}})
    coletor.Varredura(cfg, con).busca(_PaginaFalsa(), BUSCA)
    assert _avisos(con, "base") == ["1"]                    # segunda leitura: o "2" é novidade de verdade


def test_loja_le_a_aba_em_venda_e_grava_perfil(cfg, con, monkeypatch):
    chamadas = []

    def lista(page, user_id, pagina, grupo_id=None):
        chamadas.append((pagina, grupo_id))
        if grupo_id is None:
            return {"data": {"cardList": [_cartao("1", status=1)], "nextPage": True,
                             "itemGroupList": [{"groupName": "在售", "groupId": 77, "itemNumber": 2}]}}
        return {"data": {"cardList": [_cartao("2"), _cartao("3")], "nextPage": False}}

    monkeypatch.setattr(coletor.mtop, "lista_vendedor", lista)
    monkeypatch.setattr(coletor.mtop, "perfil_vendedor", lambda page, uid: {"data": {"module": {"base": {"displayName": "A"}}}})
    v = coletor.Varredura(cfg, con)
    v.loja(_PaginaFalsa(), LOJA)
    assert chamadas == [(1, None), (1, 77)]                 # "todos" pra achar o grupo, depois "em venda"
    assert sorted(v.novos) == ["2", "3"] and v.cont["vendido"] == 1
    row = con.execute("SELECT nome, itens_a_venda FROM vendedor_snapshot").fetchone()
    assert (row["nome"], row["itens_a_venda"]) == ("A", 2)


def test_freio_de_emergencia_interrompe_na_pausa(cfg, con):
    v = coletor.Varredura(cfg, con)
    v.pausa()                                               # sem freio, passa
    db.gravar_flag(con, "coleta_pausada", "1")
    with pytest.raises(coletor.Parada):
        v.pausa()


def test_busca_reaproveita_a_pagina_1_da_pagina_base(cfg, con, monkeypatch):
    chamadas = []

    def busca(page, termo, pagina):
        chamadas.append(pagina)
        return {"data": {"resultList": [], "resultInfo": {"hasNextPage": False}}}

    monkeypatch.setattr(coletor.mtop, "busca", busca)
    primeira = {"data": {"resultList": [{"data": {"item": {"main": _resultado("1")}}}], "resultInfo": {"hasNextPage": True}}}
    fonte = coletor.Fonte("busca", "t", "机械键盘", 2)
    v = coletor.Varredura(cfg, con)
    v.busca(_PaginaFalsa(), fonte, primeira)
    assert chamadas == [2]                                  # a página 1 não é pedida de novo
    assert v.cont["novo"] == 1
