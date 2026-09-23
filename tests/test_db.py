from conftest import snap

from goofish_miner import db


def _precos(con, item_id="1"):
    return [r["preco_cny"] for r in con.execute("SELECT preco_cny FROM snapshot WHERE item_id = ? ORDER BY id", (item_id,))]


def test_insert_incondicional_e_append_only(con):
    assert not db.item_conhecido(con, "1")
    db.inserir_snapshot(con, snap(preco=100))
    db.inserir_snapshot(con, snap(preco=90))
    assert db.item_conhecido(con, "1")
    assert _precos(con) == [100.0, 90.0]


def test_sem_mudanca_nao_gera_linha(con):
    r, sid = db.gravar_se_mudou(con, snap())
    assert r == "novo" and sid
    assert db.gravar_se_mudou(con, snap()) == ("igual", None)
    assert _precos(con) == [100.0]


def test_preco_diferente_gera_linha_nova(con):
    """Protege o histórico de preço. Não remover."""
    db.gravar_se_mudou(con, snap(preco=100))
    r, _ = db.gravar_se_mudou(con, snap(preco=90))
    assert r == "mudou"
    assert _precos(con) == [100.0, 90.0]


def test_titulo_diferente_gera_linha_nova(con):
    db.gravar_se_mudou(con, snap(titulo="a"))
    assert db.gravar_se_mudou(con, snap(titulo="b"))[0] == "mudou"
    assert len(_precos(con)) == 2


def test_ruido_volatil_nao_conta_como_mudanca(con):
    db.gravar_se_mudou(con, snap(raw={"rn": "abc", "browseCnt": 10, "_etiquetas": ["24小时内发布"]}))
    assert db.gravar_se_mudou(con, snap(raw={"rn": "xyz", "browseCnt": 99, "_etiquetas": []}))[0] == "igual"
    assert len(_precos(con)) == 1


def test_mesmo_anuncio_em_outra_fonte_sem_mudanca_uma_linha(con):
    db.gravar_se_mudou(con, snap(fonte="loja:1"))
    assert db.gravar_se_mudou(con, snap(fonte="busca:x"))[0] == "igual"
    assert len(_precos(con)) == 1


def test_vendedor_so_grava_se_mudou(con):
    v = db.VendedorSnapshot("1", "n", 297, 29, 95.0, 228, "L4", {"k": 1})
    assert db.gravar_vendedor_se_mudou(con, v) == "novo"
    assert db.gravar_vendedor_se_mudou(con, v) == "igual"
    v2 = db.VendedorSnapshot("1", "n", 297, 29, 95.0, 230, "L4", {"k": 2})
    assert db.gravar_vendedor_se_mudou(con, v2) == "mudou"
    assert con.execute("SELECT COUNT(*) FROM vendedor_snapshot").fetchone()[0] == 2


def test_fonte_e_flags(con):
    assert not db.fonte_conhecida(con, "busca:x")
    db.registrar_fonte(con, "busca:x")
    db.registrar_fonte(con, "busca:x")   # idempotente
    assert db.fonte_conhecida(con, "busca:x")
    db.gravar_flag(con, "k", "1")
    assert db.ler_flag(con, "k") == "1"
    db.apagar_flag(con, "k")
    assert db.ler_flag(con, "k") is None


def test_varredura(con):
    vid = db.iniciar_varredura(con)
    db.finalizar_varredura(con, vid, "ok", 5, 2)
    row = con.execute("SELECT * FROM varredura WHERE id = ?", (vid,)).fetchone()
    assert (row["status"], row["snapshots"], row["itens_novos"]) == ("ok", 5, 2)
    assert row["terminada_em"] is not None
