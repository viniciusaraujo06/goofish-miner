from datetime import datetime, timedelta, timezone

from conftest import snap

from goofish_miner import db, mcp_server


def _ha(horas):
    return (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat(timespec="seconds")


def _popular(con):
    for i, (preco, titulo) in enumerate([(600, "富士X100V 银色"), (650, "富士x100v 黑色"), (700, "富士X100V 全新"),
                                         (800, "富士X100V 套装"), (300, "佳能 G7X")]):
        db.inserir_snapshot(con, snap(str(i), preco, titulo=titulo), visto_em=_ha(100 - i))


def test_buscar_e_historico_por_termo(con):
    _popular(con)
    b = mcp_server.buscar(con, "x100v")
    assert b["anuncios"] == 4 and [d["preco"] for d in b["mais_baratos_primeiro"]] == [600, 650, 700, 800]
    h = mcp_server.historico_preco(con, "X100V", 30)
    assert h["anuncios"] == 4 and h["mediana"] == 675 and h["minimo"] == 600 and h["maximo"] == 800
    assert h["tendencia"] == "subindo"
    assert mcp_server.buscar(con, " ")["erro"] == "termo vazio"


def test_quedas_de_preco(con):
    db.inserir_snapshot(con, snap("1", 1000, titulo="a"), visto_em=_ha(50))
    db.inserir_snapshot(con, snap("1", 800, titulo="a"), visto_em=_ha(10))
    db.inserir_snapshot(con, snap("2", 500, titulo="b"), visto_em=_ha(10))
    db.inserir_snapshot(con, snap("2", 490, titulo="b"), visto_em=_ha(5))    # -2%: abaixo do mínimo
    (q,) = mcp_server.quedas_de_preco(con, dias=7, min_pct=5)
    assert (q["item_id"], q["preco"], q["preco_maximo"], q["queda_pct"]) == ("1", 800, 1000, 20.0)


def test_novidades_por_janela_e_fonte(con):
    db.inserir_snapshot(con, snap("1", 100, fonte="busca:x"), visto_em=_ha(30))
    db.inserir_snapshot(con, snap("2", 100, fonte="busca:x"), visto_em=_ha(2))
    db.inserir_snapshot(con, snap("3", 100, fonte="loja:9"), visto_em=_ha(1))
    assert [d["item_id"] for d in mcp_server.novidades(con, 24)] == ["3", "2"]
    assert [d["item_id"] for d in mcp_server.novidades(con, 24, fonte="busca:")] == ["2"]


def test_anuncio_e_vendedor(cfg, con):
    db.inserir_snapshot(con, snap("1", 1600, titulo="Switch OLED", fonte="loja:1000000000001", vendedor_id="1000000000001",
                                  raw={"_etiquetas": ["5人想要"]}))
    db.inserir_snapshot(con, snap("1", 1500, titulo="Switch OLED", fonte="busca:switch"))
    db.gravar_vendedor_se_mudou(con, db.VendedorSnapshot("1000000000001", "Loja", 30, 12, 98.0, 100, "L4", {}))
    a = mcp_server.anuncio(con, "1")
    assert [h["preco"] for h in a["historico"]] == [1600, 1500]
    assert a["fontes"] == ["busca:switch", "loja:1000000000001"] and a["vendedor"]["nome"] == "Loja"
    assert mcp_server.anuncio(con, "404")["erro"]
    v = mcp_server.vendedor(con, cfg, "1000000000001")
    assert v["loja_acompanhada"] == "Loja A" and v["itens_a_venda"] == 12 and v["anuncios_observados"] == 1


def test_visao_geral(cfg, con):
    db.inserir_snapshot(con, snap("1", 100, fonte="busca:机械键盘"))
    db.registrar_fonte(con, "busca:机械键盘")
    g = mcp_server.visao_geral(con, cfg)
    assert g["anuncios_no_banco"] == 1 and g["ultima_varredura"] is None and not g["coleta_pausada"]
    fontes = {f["chave"]: f for f in g["fontes"]}
    assert fontes["busca:机械键盘"]["lida"] and fontes["busca:机械键盘"]["anuncios"] == 1
    assert not fontes["loja:1000000000001"]["lida"]


def test_servidor_registra_as_tools():
    import asyncio
    srv = mcp_server.criar_servidor()
    nomes = {t.name for t in asyncio.run(srv.list_tools())}
    assert nomes == {"buscar", "historico_preco", "quedas_de_preco", "novidades", "anuncio", "vendedor", "visao_geral"}
