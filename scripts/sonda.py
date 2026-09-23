"""Sonda: UMA chamada ao Goofish pra saber se este IP/navegador está punido.

Nunca repete, nunca insiste. Uso:

    uv run python scripts/sonda.py busca      # search?q=精工5&spm=...   (contexto zh-CN, como o coletor)
    uv run python scripts/sonda.py busca-br   # mesma busca, mas locale pt-BR e Chrome sem UA customizado
    uv run python scripts/sonda.py detalhe <item_id>   # abre um anúncio à venda e lê o ret do detalhe
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from goofish_miner import mtop  # noqa: E402

BUSCA = "https://www.goofish.com/search?q=%E7%B2%BE%E5%B7%A55&spm=a21ybx.search.searchInput.0"


def main() -> int:
    modo = sys.argv[1] if len(sys.argv) > 1 else "busca"
    if modo == "detalhe" and len(sys.argv) < 3:
        print("uso: sonda.py detalhe <item_id>  (id de um anúncio à venda, da URL item?id=...)")
        return 2
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True,
                                     args=["--disable-blink-features=AutomationControlled"],
                                     ignore_default_args=["--enable-automation"])
        if modo == "busca-br":
            ctx = browser.new_context(locale="pt-BR", viewport={"width": 1366, "height": 850})
        else:
            ctx = browser.new_context(user_agent=mtop.UA, locale="zh-CN", viewport={"width": 1366, "height": 850})
        page = ctx.new_page()
        rets: list[str] = []
        page.on("response", lambda r: rets.append(f"{r.url.split('/h5/')[1].split('/')[0]}: {r.text()[:120]}")
                if "/h5/mtop." in r.url and ("pc.search/" in r.url or "pc.detail/" in r.url) else None)
        if modo == "detalhe":
            page.goto(mtop.URL_ITEM.format(item_id=sys.argv[2]), wait_until="domcontentloaded", timeout=60_000)
        else:
            page.goto(BUSCA, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(8_000)
        n = page.evaluate("() => document.querySelectorAll('a[href*=\"item?id=\"]').length")
        browser.close()
    print(f"modo={modo}  links de item na página: {n}")
    for r in rets:
        print("  ", r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
