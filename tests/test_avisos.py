from datetime import datetime, timedelta, timezone

from conftest import BUSCA, fazer_cfg, snap

from goofish_miner import avisos, db, notificador


def _tipos(pend):
    return [(a.tipo, a.item_id) for a in pend]


class FakeTelegram:
    def __init__(self, falhar=False):
        self.enviadas: list[tuple[str, str | None]] = []
        self.falhar = falhar

    def enviar(self, texto, foto_url=None):
        if self.falhar:
            raise RuntimeError("telegram fora")
        self.enviadas.append((texto, foto_url))
        return len(self.enviadas)


def test_novo_uma_vez_so(cfg, con):
    db.inserir_snapshot(con, snap("1", 400))
    pend = avisos.pendentes(con, cfg)
    assert _tipos(pend) == [("novo", "1")]
    db.registrar_aviso(con, "1", pend[0].snapshot_id, "novo")
    assert avisos.pendentes(con, cfg) == []


def test_linha_de_base_nao_avisa(cfg, con):
    sid = db.inserir_snapshot(con, snap("1", 400))
    db.registrar_aviso(con, "1", sid, "base")
    assert avisos.pendentes(con, cfg) == []


def test_queda_medida_desde_o_ultimo_aviso(cfg, con):
    sid = db.inserir_snapshot(con, snap("1", 400))
    db.registrar_aviso(con, "1", sid, "novo")
    db.inserir_snapshot(con, snap("1", 380))                # -5%: abaixo do limiar de 10%
    assert avisos.pendentes(con, cfg) == []
    db.inserir_snapshot(con, snap("1", 350))                # -12,5% desde o aviso
    (a,) = avisos.pendentes(con, cfg)
    assert (a.tipo, a.preco_anterior, a.preco, round(a.queda_pct, 1)) == ("queda", 400, 350, 12.5)
    db.registrar_aviso(con, "1", a.snapshot_id, "queda")
    db.inserir_snapshot(con, snap("1", 330))                # -5,7% desde a queda avisada: não repete
    assert avisos.pendentes(con, cfg) == []
    db.inserir_snapshot(con, snap("1", 310))                # -11,4% desde 350
    assert _tipos(avisos.pendentes(con, cfg)) == [("queda", "1")]


def test_subida_nao_avisa(cfg, con):
    sid = db.inserir_snapshot(con, snap("1", 400))
    db.registrar_aviso(con, "1", sid, "base")
    db.inserir_snapshot(con, snap("1", 500))
    assert avisos.pendentes(con, cfg) == []


def test_sem_avisar_novos_a_queda_conta_desde_a_primeira_vez(tmp_path):
    cfg = fazer_cfg(tmp_path, notif_novos=False)
    con = db.conectar(cfg.db)
    db.inserir_snapshot(con, snap("1", 400))
    assert avisos.pendentes(con, cfg) == []
    db.inserir_snapshot(con, snap("1", 300))
    assert _tipos(avisos.pendentes(con, cfg)) == [("queda", "1")]


def test_queda_desligada(tmp_path):
    cfg = fazer_cfg(tmp_path, notif_queda_min_pct=0)
    con = db.conectar(cfg.db)
    sid = db.inserir_snapshot(con, snap("1", 400))
    db.registrar_aviso(con, "1", sid, "base")
    db.inserir_snapshot(con, snap("1", 100))
    assert avisos.pendentes(con, cfg) == []


def test_aviso_velho_nao_sai(cfg, con):
    velho = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat(timespec="seconds")
    db.inserir_snapshot(con, snap("1", 400), visto_em=velho)
    db.inserir_snapshot(con, snap("2", 400))
    assert _tipos(avisos.pendentes(con, cfg)) == [("novo", "2")]


def test_mensagens(cfg, con):
    db.inserir_snapshot(con, snap("1", 399, titulo="客制化机械键盘", raw={"_etiquetas": ["24小时内发布", "3人想要"]}))
    (a,) = avisos.pendentes(con, cfg)
    m = notificador.mensagem(a, cfg)
    assert m.startswith(f"🆕 novo · busca {BUSCA.nome}") and "¥399" in m and "3人想要" in m and "item?id=1" in m
    q = avisos.Aviso("queda", "1", 2, "x", 300.0, 400.0, "busca:que saiu do toml", "t", {})
    assert notificador.mensagem(q, cfg).startswith("📉 baixou 25% · busca que saiu do toml")
    assert "¥400 → ¥300" in notificador.mensagem(q, cfg)


def test_envia_e_marca_so_o_que_foi_confirmado(cfg, con):
    db.inserir_snapshot(con, snap("1", 399, raw={"exContent": {"picUrl": "http://img/1.jpg"}}))
    tg = FakeTelegram()
    assert notificador.enviar_pendentes(con, cfg, tg)["enviados"] == 1
    assert tg.enviadas[0][1] == "http://img/1.jpg"         # a foto vai junto (com o sufixo da CDN, se houver)
    assert notificador.enviar_pendentes(con, cfg, tg)["enviados"] == 0

    db.inserir_snapshot(con, snap("2", 399))
    assert notificador.enviar_pendentes(con, cfg, FakeTelegram(falhar=True))["enviados"] == 0
    assert _tipos(avisos.pendentes(con, cfg)) == [("novo", "2")]   # a falha não queimou o aviso


def test_sem_credenciais_nao_marca_nada(cfg, con):
    db.inserir_snapshot(con, snap("1", 399))
    r = notificador.executar(con, cfg)                     # tmp_path não tem .env
    assert r.get("erro") == "credenciais ausentes"
    assert _tipos(avisos.pendentes(con, cfg)) == [("novo", "1")]


def test_teto_por_rodada_e_marcar_vistos(tmp_path):
    cfg = fazer_cfg(tmp_path, notif_max_por_rodada=2)
    con = db.conectar(cfg.db)
    for i in range(5):
        db.inserir_snapshot(con, snap(str(i), 100))
    r = notificador.enviar_pendentes(con, cfg, FakeTelegram())
    assert (r["enviados"], r["restantes"]) == (2, 3)
    assert notificador.enviar_pendentes(con, cfg, None, marcar_vistos=True)["marcados"] == 3
    assert avisos.pendentes(con, cfg) == []


def test_alerta_de_varredura_bloqueada_uma_vez(cfg, con):
    vid = db.iniciar_varredura(con)
    db.finalizar_varredura(con, vid, "bloqueada", 0, 0, "RGV587")
    tg = FakeTelegram()
    assert notificador.alerta_varredura(con, tg) is True
    assert notificador.alerta_varredura(con, tg) is False
    assert len(tg.enviadas) == 1 and "bloqueada" in tg.enviadas[0][0]
