"""Escutador do Telegram: controle da coleta e consulta rápida pelo celular.

Aceita apenas o chat de `TELEGRAM_CHAT_ID`; qualquer outro remetente é
ignorado em silêncio. Escreve no banco só a tabela `estado` (freio, janela,
frequência, offset do getUpdates).

Roda como processo contínuo (long polling), registrado no Agendador do
Windows "ao fazer logon" por scripts/agendar_tarefa.ps1.

    uv run goofish-miner-telegram
"""

from __future__ import annotations

import logging
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from . import db, mtop, notificador
from .config import Config, carregar

log = logging.getLogger("goofish_miner.telegram")

TAREFA_WINDOWS = "GoofishMiner"
FREQUENCIA_MIN = 20

AJUDA = ("comandos:\n/status — última varredura e contagens\n"
         "/ultimos [n] — os n últimos avisos (padrão 5)\n"
         "/fontes — lojas e buscas acompanhadas\n"
         "/forcar — dispara uma varredura agora (ignora janela e intervalo)\n"
         "/config — janela e frequência em vigor\n"
         "/janela <inicio> <fim> — ex.: /janela 7 23 (horas de Brasília; fim é exclusivo)\n"
         "/frequencia <minutos> — ex.: /frequencia 30 (mínimo 20)\n"
         "/parar — FREIO: interrompe a varredura em andamento e bloqueia as próximas\n"
         "/continuar — libera a coleta de novo")


# ------------------------------------------------------------------ respostas

def parar_coleta(con: sqlite3.Connection) -> str:
    """Freio de emergência: trava no banco (o coletor checa entre cada passo e na largada)
    e encerra a instância em andamento da tarefa do Windows, se houver."""
    db.gravar_flag(con, "coleta_pausada", "1")
    detalhe = "varredura em andamento encerrada" if _encerrar_tarefa_windows() else "nenhuma varredura em andamento"
    return f"⛔ coleta PAUSADA — {detalhe}. Nada roda até /continuar."


def _encerrar_tarefa_windows() -> bool:
    if sys.platform != "win32":
        return False
    try:
        r = subprocess.run(["schtasks", "/End", "/TN", TAREFA_WINDOWS], capture_output=True, text=True, timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def forcar_varredura(con: sqlite3.Connection, cfg: Config) -> str:
    """Dispara o coletor com --forcar em processo separado e sem console."""
    if db.ler_flag(con, "coleta_pausada") == "1":
        return "⛔ coleta pausada por /parar — mande /continuar antes de /forcar."
    if con.execute("SELECT 1 FROM varredura WHERE status = 'rodando' LIMIT 1").fetchone():
        return "já tem uma varredura rodando; acompanhe com /status."
    # o mesmo interpretador deste processo, na versão sem console se houver (pythonw.exe)
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    cmd = [str(pythonw if pythonw.exists() else exe), "-m", "goofish_miner.coletor", "--forcar"]
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(cmd, cwd=str(cfg.raiz), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, creationflags=flags)
    return "▶ varredura forçada iniciada (ignora janela e intervalo). Acompanhe com /status."


def texto_config(con: sqlite3.Connection, cfg: Config) -> str:
    from .coletor import ajustes
    inicio, fim, intervalo = ajustes(con, cfg)
    origem = "bot" if any(db.ler_flag(con, k) for k in ("janela_inicio", "janela_fim", "intervalo_min_minutos")) else "coleta.toml"
    return (f"janela {inicio:02d}–{fim:02d}h de Brasília · a cada {intervalo} min (origem: {origem})\n"
            f"padrão do arquivo: {cfg.janela_inicio:02d}–{cfg.janela_fim:02d}h, {cfg.intervalo_min_minutos} min\n"
            "/janela <inicio> <fim> · /frequencia <min> · /janela padrao volta ao arquivo")


def ajustar_janela(con: sqlite3.Connection, cfg: Config, args: list[str]) -> str:
    if args[:1] == ["padrao"]:
        for k in ("janela_inicio", "janela_fim", "intervalo_min_minutos"):
            db.apagar_flag(con, k)
        return "ajustes apagados; vale o coleta.toml.\n" + texto_config(con, cfg)
    try:
        a, b = int(args[0]), int(args[1])
    except (IndexError, ValueError):
        return "uso: /janela <inicio> <fim>  (ex.: /janela 7 23) ou /janela padrao"
    if not (0 <= a < b <= 24):
        return "horas inválidas: início entre 0 e 23, fim maior que início e até 24"
    db.gravar_flag(con, "janela_inicio", str(a))
    db.gravar_flag(con, "janela_fim", str(b))
    return "✔ " + texto_config(con, cfg)


def ajustar_frequencia(con: sqlite3.Connection, cfg: Config, args: list[str]) -> str:
    try:
        m = int(args[0])
    except (IndexError, ValueError):
        return "uso: /frequencia <minutos>  (ex.: /frequencia 30)"
    if m < FREQUENCIA_MIN:
        return f"mínimo {FREQUENCIA_MIN} min: abaixo disso é volume demais pro mesmo IP"
    if m > 720:
        return "máximo 720 min (12 h)"
    db.gravar_flag(con, "intervalo_min_minutos", str(m))
    return "✔ " + texto_config(con, cfg)


def texto_status(con: sqlite3.Connection) -> str:
    v = con.execute("SELECT * FROM varredura ORDER BY id DESC LIMIT 1").fetchone()
    linhas = []
    if v:
        linhas.append(f"varredura #{v['id']} {v['status']} · {v['iniciada_em'][:16]}Z · {v['snapshots']} linhas, {v['itens_novos']} novos")
        if v["detalhe"]:
            linhas.append(str(v["detalhe"])[:200])
    else:
        linhas.append("nenhuma varredura ainda")
    anuncios = con.execute("SELECT COUNT(DISTINCT item_id) FROM snapshot").fetchone()[0]
    ontem = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")   # mesmo formato de db.agora()
    por_tipo = {r["tipo"]: r["n"] for r in con.execute(
        "SELECT tipo, COUNT(*) n FROM aviso WHERE criado_em >= ? GROUP BY tipo", (ontem,))}
    linhas.append(f"{anuncios} anúncios no banco · últimas 24 h: {por_tipo.get('novo', 0)} novos, "
                  f"{por_tipo.get('queda', 0)} quedas avisadas")
    if db.ler_flag(con, "coleta_pausada") == "1":
        linhas.append("⛔ coleta PAUSADA por /parar — /continuar libera")
    return "\n".join(linhas)


def texto_ultimos(con: sqlite3.Connection, n: int) -> str:
    rows = con.execute(
        "SELECT a.tipo, a.criado_em, s.item_id, s.titulo, s.preco_cny FROM aviso a JOIN snapshot s ON s.id = a.snapshot_id"
        " WHERE a.tipo IN ('novo', 'queda') ORDER BY a.id DESC LIMIT ?", (n,)).fetchall()
    if not rows:
        return "nenhum aviso ainda"
    icone = {"novo": "🆕", "queda": "📉"}
    return "\n".join(f"{icone[r['tipo']]} ¥{r['preco_cny']:.0f} · {r['titulo'][:40]}\n  {mtop.URL_ITEM.format(item_id=r['item_id'])}"
                     for r in rows)


def texto_fontes(con: sqlite3.Connection, cfg: Config) -> str:
    if not cfg.fontes:
        return "nenhuma fonte em config/fontes.toml"
    out = []
    for f in cfg.fontes:
        n = con.execute("SELECT COUNT(DISTINCT item_id) FROM snapshot WHERE fonte = ?", (f.chave,)).fetchone()[0]
        estreia = "" if db.fonte_conhecida(con, f.chave) else " · ainda não lida"
        out.append(f"• {f.tipo} {f.nome}: {n} anúncios{estreia}")
    return "\n".join(out)


# ------------------------------------------------------------------ processamento

def processar_update(con: sqlite3.Connection, tg, cfg: Config, upd: dict, chat_id: str) -> str | None:
    """Trata um update. Devolve uma descrição curta do que fez (pra log/teste) ou None se ignorou."""
    msg = upd.get("message") or {}
    if str(msg.get("chat", {}).get("id")) != str(chat_id):
        return None
    texto = (msg.get("text") or "").strip()
    if not texto.startswith("/"):
        return None
    cmd, *args = texto.split()
    cmd = cmd.split("@")[0].lower()
    if cmd == "/status":
        tg.enviar(texto_status(con))
    elif cmd == "/ultimos":
        n = int(args[0]) if args and args[0].isdigit() else 5
        tg.enviar(texto_ultimos(con, max(1, min(n, 20))))
    elif cmd == "/fontes":
        tg.enviar(texto_fontes(con, cfg))
    elif cmd == "/forcar":
        tg.enviar(forcar_varredura(con, cfg))
    elif cmd == "/config":
        tg.enviar(texto_config(con, cfg))
    elif cmd == "/janela":
        tg.enviar(ajustar_janela(con, cfg, args))
    elif cmd == "/frequencia":
        tg.enviar(ajustar_frequencia(con, cfg, args))
    elif cmd == "/parar":
        tg.enviar(parar_coleta(con))
    elif cmd == "/continuar":
        db.gravar_flag(con, "coleta_pausada", "0")
        tg.enviar("▶ coleta liberada. A próxima rodada sai no horário normal, dentro da janela.")
    else:
        tg.enviar(AJUDA)
        return "ajuda"
    return cmd[1:]


def escutar(con: sqlite3.Connection, tg: notificador.Telegram, cfg: Config, chat_id: str, *, uma_vez: bool = False) -> int:
    """Long polling. Cada update é processado uma vez (offset persistido no banco)."""
    tratados = 0
    while True:
        offset = db.ler_flag(con, "telegram_offset")
        try:
            updates = tg.atualizacoes(int(offset) if offset else None)
        except (httpx.HTTPError, RuntimeError, ValueError) as e:
            log.warning("getUpdates falhou: %s", e)
            if uma_vez:
                return tratados
            time.sleep(15)
            continue
        for upd in updates:
            try:
                feito = processar_update(con, tg, cfg, upd, chat_id)
                if feito:
                    log.info(feito)
                    tratados += 1
            except Exception:  # noqa: BLE001 — um update ruim não derruba o escutador
                log.exception("update %s falhou", upd.get("update_id"))
            db.gravar_flag(con, "telegram_offset", str(int(upd["update_id"]) + 1))
        if uma_vez:
            return tratados


def main(argv: list[str] | None = None) -> int:
    from .coletor import configurar_log
    cfg = carregar()
    configurar_log(cfg, "telegram.log")
    env = notificador.carregar_env(cfg.raiz)
    if not env["TELEGRAM_BOT_TOKEN"] or not env["TELEGRAM_CHAT_ID"]:
        log.error("TELEGRAM_BOT_TOKEN/CHAT_ID ausentes em .env")
        return 1
    tg = notificador.Telegram(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"])
    con = db.conectar(cfg.db)
    log.info("escutando o bot (chat %s)", env["TELEGRAM_CHAT_ID"])
    escutar(con, tg, cfg, env["TELEGRAM_CHAT_ID"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
