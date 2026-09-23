from conftest import snap

from goofish_miner import db, telegram_bot

CHAT = "123"


class FakeTelegram:
    def __init__(self, updates=None):
        self.enviadas: list[str] = []
        self.updates = updates or []

    def enviar(self, texto, foto_url=None):
        self.enviadas.append(texto)
        return len(self.enviadas)

    def atualizacoes(self, offset, timeout_s=50):
        return [u for u in self.updates if offset is None or u["update_id"] >= offset]


def _msg(texto, chat=CHAT, uid=1):
    return {"update_id": uid, "message": {"text": texto, "chat": {"id": int(chat)}}}


def test_ignora_outro_chat_e_texto_solto(cfg, con):
    tg = FakeTelegram()
    assert telegram_bot.processar_update(con, tg, cfg, _msg("/status", chat="999"), CHAT) is None
    assert telegram_bot.processar_update(con, tg, cfg, _msg("oi"), CHAT) is None
    assert tg.enviadas == []


def test_status_ultimos_e_fontes(cfg, con):
    sid = db.inserir_snapshot(con, snap("1", 399, titulo="客制化机械键盘"))
    db.registrar_aviso(con, "1", sid, "novo")
    db.registrar_fonte(con, "busca:机械键盘")
    tg = FakeTelegram()
    telegram_bot.processar_update(con, tg, cfg, _msg("/status"), CHAT)
    assert "nenhuma varredura" in tg.enviadas[-1] and "1 novos" in tg.enviadas[-1]
    telegram_bot.processar_update(con, tg, cfg, _msg("/ultimos 3"), CHAT)
    assert "¥399" in tg.enviadas[-1] and "item?id=1" in tg.enviadas[-1]
    telegram_bot.processar_update(con, tg, cfg, _msg("/fontes"), CHAT)
    assert "busca teclado: 1 anúncios" in tg.enviadas[-1] and "loja Loja A: 0 anúncios · ainda não lida" in tg.enviadas[-1]


def test_freio_e_liberacao(cfg, con, monkeypatch):
    monkeypatch.setattr(telegram_bot, "_encerrar_tarefa_windows", lambda: False)
    tg = FakeTelegram()
    telegram_bot.processar_update(con, tg, cfg, _msg("/parar"), CHAT)
    assert db.ler_flag(con, "coleta_pausada") == "1" and "PAUSADA" in tg.enviadas[-1]
    telegram_bot.processar_update(con, tg, cfg, _msg("/forcar"), CHAT)
    assert "/continuar" in tg.enviadas[-1]                  # forçar com o freio puxado não roda
    telegram_bot.processar_update(con, tg, cfg, _msg("/continuar"), CHAT)
    assert db.ler_flag(con, "coleta_pausada") == "0"


def test_janela_e_frequencia(cfg, con):
    tg = FakeTelegram()
    telegram_bot.processar_update(con, tg, cfg, _msg("/janela 7 23"), CHAT)
    assert tg.enviadas[-1].startswith("✔ janela 07–23h")
    telegram_bot.processar_update(con, tg, cfg, _msg("/frequencia 5"), CHAT)
    assert "mínimo" in tg.enviadas[-1] and db.ler_flag(con, "intervalo_min_minutos") is None
    telegram_bot.processar_update(con, tg, cfg, _msg("/frequencia 30"), CHAT)
    assert "a cada 30 min (origem: bot)" in tg.enviadas[-1]
    telegram_bot.processar_update(con, tg, cfg, _msg("/janela padrao"), CHAT)
    assert "origem: coleta.toml" in tg.enviadas[-1]


def test_comando_desconhecido_mostra_ajuda(cfg, con):
    tg = FakeTelegram()
    assert telegram_bot.processar_update(con, tg, cfg, _msg("/xyz"), CHAT) == "ajuda"
    assert tg.enviadas[-1].startswith("comandos:")


def test_offset_persistido_processa_cada_update_uma_vez(cfg, con):
    tg = FakeTelegram([_msg("/status", uid=10), _msg("/config", uid=11)])
    assert telegram_bot.escutar(con, tg, cfg, CHAT, uma_vez=True) == 2
    assert db.ler_flag(con, "telegram_offset") == "12"
    assert telegram_bot.escutar(con, tg, cfg, CHAT, uma_vez=True) == 0
