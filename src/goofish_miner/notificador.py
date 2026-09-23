"""Envia no Telegram os avisos pendentes (anúncio novo, queda de preço) e o
alerta de varredura bloqueada. Lê só o banco; token e chat em `.env`.

Só marca como avisado o que o Telegram confirmou: sem credencial ou com a rede
fora, nada é marcado e os avisos ficam pra próxima rodada.

    uv run goofish-miner-notificar [--simular] [--marcar-vistos]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
from pathlib import Path

import httpx

from . import avisos, db, extracao, mtop
from .config import RAIZ, Config, carregar

log = logging.getLogger("goofish_miner.notificador")


def carregar_env(raiz: Path = RAIZ) -> dict[str, str]:
    env: dict[str, str] = {}
    caminho = raiz / ".env"
    if caminho.exists():
        for linha in caminho.read_text(encoding="utf-8").splitlines():
            if "=" in linha and not linha.lstrip().startswith("#"):
                k, v = linha.split("=", 1)
                env[k.strip()] = v.strip()
    env.setdefault("TELEGRAM_BOT_TOKEN", os.environ.get("TELEGRAM_BOT_TOKEN", ""))
    env.setdefault("TELEGRAM_CHAT_ID", os.environ.get("TELEGRAM_CHAT_ID", ""))
    return env


_TIPOS_IMAGEM = {"image/jpeg": "foto.jpg", "image/png": "foto.png", "image/webp": "foto.webp"}


def baixar_imagem(url: str, limite_bytes: int = 9_000_000) -> tuple[bytes, str, str] | None:
    """(bytes, nome, content-type). Se o Telegram buscar a URL sozinho, a CDN da
    Alibaba devolve webp e ele recusa; baixando aqui com UA de Chrome vem JPEG."""
    try:
        r = httpx.get(url, timeout=20, follow_redirects=True,
                      headers={"User-Agent": mtop.UA, "Referer": "https://www.goofish.com/"})
        tipo = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
        if r.status_code == 200 and tipo in _TIPOS_IMAGEM and 0 < len(r.content) <= limite_bytes:
            return r.content, _TIPOS_IMAGEM[tipo], tipo
        log.warning("imagem da CDN inesperada (%s, HTTP %s, %s bytes)", tipo, r.status_code, len(r.content))
    except httpx.HTTPError as e:
        log.warning("download da imagem falhou: %s", str(e)[:120])
    return None


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.base = f"https://api.telegram.org/bot{token}"
        self.chat_id = chat_id

    def _post(self, metodo: str, **kw) -> dict:
        with httpx.Client(timeout=kw.pop("_timeout", 30)) as cli:
            r = cli.post(f"{self.base}/{metodo}", **kw)
        j = r.json()
        if not j.get("ok"):
            raise RuntimeError(f"telegram {metodo}: {j}")
        return j.get("result") or {}

    def enviar(self, texto: str, foto_url: str | None = None) -> int | None:
        """Com foto quando der; se a foto falhar, cai pro texto puro (sem o preview genérico do Goofish)."""
        if foto_url:
            imagem = baixar_imagem(foto_url)
            if imagem:
                conteudo, nome, tipo = imagem
                try:
                    res = self._post("sendPhoto", data={"chat_id": self.chat_id, "caption": texto[:1024]},
                                     files={"photo": (nome, conteudo, tipo)})
                    return res.get("message_id")
                except RuntimeError as e:
                    log.warning("sendPhoto falhou (%s); enviando texto", str(e)[:120])
        corpo = {"chat_id": self.chat_id, "text": texto[:4096], "disable_web_page_preview": True}
        return self._post("sendMessage", json=corpo).get("message_id")

    def atualizacoes(self, offset: int | None, timeout_s: int = 50) -> list[dict]:
        corpo = {"timeout": timeout_s, "allowed_updates": ["message"]}
        if offset is not None:
            corpo["offset"] = offset
        return self._post("getUpdates", json=corpo, _timeout=timeout_s + 15) or []


def nome_da_fonte(cfg: Config, chave: str) -> str:
    f = cfg.fonte(chave)
    if f:
        return f"{f.tipo} {f.nome}"
    return chave.replace(":", " ", 1)   # fonte que saiu do fontes.toml


def mensagem(a: avisos.Aviso, cfg: Config) -> str:
    fonte = nome_da_fonte(cfg, a.fonte)
    if a.tipo == "queda":
        linhas = [f"📉 baixou {a.queda_pct:.0f}% · {fonte}", a.titulo[:120], f"¥{a.preco_anterior:.0f} → ¥{a.preco:.0f}"]
    else:
        linhas = [f"🆕 novo · {fonte}", a.titulo[:120], f"¥{a.preco:.0f}"]
    etiquetas = [e for e in a.raw.get("_etiquetas") or [] if e]
    if etiquetas:
        linhas.append(" · ".join(etiquetas[:4]))
    linhas.append(a.url)
    return "\n".join(linhas)


def alerta_varredura(con: sqlite3.Connection, tg: Telegram | None) -> bool:
    """Uma mensagem por varredura bloqueada/erro, nunca repetida."""
    r = con.execute("SELECT id, status, detalhe FROM varredura WHERE status IN ('bloqueada', 'erro') ORDER BY id DESC LIMIT 1").fetchone()
    if not r:
        return False
    chave = f"varredura:{r['id']}"
    if con.execute("SELECT 1 FROM aviso WHERE item_id = ? LIMIT 1", (chave,)).fetchone():
        return False
    texto = f"⚠️ varredura #{r['id']} {r['status']}: {(r['detalhe'] or '')[:300]}\nO coletor para e espera antes de tentar de novo. Nada a fazer agora."
    mid = tg.enviar(texto) if tg else None
    if tg and mid is None:
        return False
    db.registrar_aviso(con, chave, None, "alerta", mid)
    return True


def enviar_pendentes(con: sqlite3.Connection, cfg: Config, tg: Telegram | None, *,
                     simular: bool = False, marcar_vistos: bool = False) -> dict[str, int]:
    """Envia até `max_por_rodada` avisos. `simular` só imprime (roda sobre cópia em memória);
    `marcar_vistos` marca todos como tratados sem enviar (zera o acumulado)."""
    cont = {"enviados": 0, "marcados": 0, "restantes": 0}
    for a in avisos.pendentes(con, cfg):
        if marcar_vistos:
            db.registrar_aviso(con, a.item_id, a.snapshot_id, "base")
            cont["marcados"] += 1
            continue
        if cont["enviados"] >= cfg.notif_max_por_rodada:
            cont["restantes"] += 1
            continue
        texto = mensagem(a, cfg)
        mid = None
        if tg:
            foto = extracao.foto_principal(a.raw)
            try:
                mid = tg.enviar(texto, foto + cfg.foto_sufixo_cdn if foto else None)
            except (httpx.HTTPError, RuntimeError) as exc:
                log.error("falha ao enviar %s: %s", a.item_id, exc)
                break   # rede caiu ou token errado: tenta de novo na próxima rodada, sem marcar
            if mid is None:
                log.error("o Telegram não confirmou %s; não vou marcar como avisado", a.item_id)
                break
        elif simular:
            log.info("[simulação]\n%s\n", texto)
        else:
            break   # sem Telegram e sem simulação: nada a fazer, e nada é marcado
        db.registrar_aviso(con, a.item_id, a.snapshot_id, a.tipo, mid)
        cont["enviados"] += 1
    return cont


def executar(con: sqlite3.Connection, cfg: Config, *, simular: bool = False, marcar_vistos: bool = False) -> dict:
    tg = None
    if not (simular or marcar_vistos):
        env = carregar_env(cfg.raiz)
        if not env["TELEGRAM_BOT_TOKEN"] or not env["TELEGRAM_CHAT_ID"]:
            log.error("TELEGRAM_BOT_TOKEN/CHAT_ID ausentes em %s — nada foi enviado nem marcado", cfg.raiz / ".env")
            return {"erro": "credenciais ausentes", "enviados": 0}
        tg = Telegram(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"])
    cont: dict = enviar_pendentes(con, cfg, tg, simular=simular, marcar_vistos=marcar_vistos)
    if tg:
        cont["alerta"] = int(alerta_varredura(con, tg))
    return cont


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="goofish-miner-notificar", description=__doc__.split("\n")[0])
    ap.add_argument("--simular", action="store_true", help="imprime o que seria enviado; não marca nada")
    ap.add_argument("--marcar-vistos", action="store_true", help="marca todos os pendentes como vistos sem enviar")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)   # o INFO dele imprime a URL com o token
    cfg = carregar()
    con = db.conectar(cfg.db)
    if args.simular:
        mem = sqlite3.connect(":memory:")   # simulação não registra nada: roda numa cópia
        mem.row_factory = sqlite3.Row
        con.backup(mem)
        con = mem
    elif not cfg.notif_ativo and not args.marcar_vistos:
        log.warning("avisos desligados em coleta.toml ([notificacao] ativo = false); use --simular pra ver o que sairia")
        return 0
    log.info("avisos: %s", json.dumps(executar(con, cfg, simular=args.simular, marcar_vistos=args.marcar_vistos)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
