"""Descobre o chat_id da sua conversa com o bot, sem expor o token.

Pré-requisitos:
1. criar o bot no @BotFather e guardar o token em `.env`, na linha
   `TELEGRAM_BOT_TOKEN=...`
2. mandar qualquer mensagem para o bot (um `/start` basta) — o Telegram só
   entrega o chat depois que você fala com ele primeiro

Uso:
    uv run python scripts/telegram_chat_id.py

O token nunca é impresso. Copie o `chat_id` que aparecer para o `.env`, na
linha `TELEGRAM_CHAT_ID=...`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

RAIZ = Path(__file__).resolve().parent.parent


def ler_env(nome: str) -> str | None:
    """Lê uma chave do `.env` sem depender de biblioteca externa."""
    caminho = RAIZ / ".env"
    if not caminho.exists():
        return None
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        if chave.strip() == nome:
            return valor.strip().strip("'\"")
    return None


def main() -> int:
    token = ler_env("TELEGRAM_BOT_TOKEN")
    if not token:
        print("TELEGRAM_BOT_TOKEN não encontrado em .env")
        print()
        print("Crie o arquivo .env na raiz do projeto com uma linha assim:")
        print("  TELEGRAM_BOT_TOKEN=123456789:AA...")
        print()
        print("O token vem do @BotFather no Telegram, com o comando /newbot.")
        return 1

    try:
        r = httpx.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
    except httpx.HTTPError as e:
        print(f"falha de rede ao falar com o Telegram: {e}")
        return 1

    if r.status_code == 401:
        print("o Telegram recusou o token. Confira se copiou ele inteiro do @BotFather.")
        return 1
    if r.status_code != 200:
        print(f"o Telegram respondeu {r.status_code}")   # sem corpo: pode conter o token
        return 1

    updates = (r.json() or {}).get("result") or []
    if not updates:
        print("o bot ainda não recebeu nenhuma mensagem.")
        print()
        print("Abra a conversa com o seu bot no Telegram, mande um /start, e rode de novo.")
        print("Bot do Telegram não consegue escrever para você antes de você escrever para ele.")
        return 1

    vistos: dict[int, str] = {}
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            nome = chat.get("title") or " ".join(
                filter(None, [chat.get("first_name"), chat.get("last_name")])
            ) or chat.get("username") or "(sem nome)"
            vistos[chat["id"]] = f"{nome} [{chat.get('type')}]"

    if not vistos:
        print("recebi atualizações, mas nenhuma com conversa identificável. Mande um /start ao bot.")
        return 1

    print("conversas encontradas:")
    for chat_id, desc in vistos.items():
        print(f"  chat_id = {chat_id}   {desc}")
    print()
    print("Copie o número acima para o .env, numa linha nova:")
    print(f"  TELEGRAM_CHAT_ID={next(iter(vistos))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
