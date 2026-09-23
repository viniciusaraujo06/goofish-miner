"""FASE 0e — busca com o `spm` da caixa de pesquisa e via digitação real.

O dono relata que `search?q=精工5&spm=a21ybx.search.searchInput.0` mostra
relógios no Chrome dele, enquanto a URL sem `spm` cai no login.

Uso:
    uv run python -u scripts/fase0e_busca_spm.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fase0_reconhecimento import KEYWORD, RAIZ, UA, Registrador, detectar_popup_login, pausa  # noqa: E402

URL_SPM = "https://www.goofish.com/search?q=%E7%B2%BE%E5%B7%A55&spm=a21ybx.search.searchInput.0"


def resumo_busca(page, reg, achados, nome, pasta):
    page.wait_for_timeout(10_000)
    links = page.evaluate("() => [...document.querySelectorAll('a[href*=\"item?id=\"]')].map(a => a.href)")
    titulos = page.evaluate(
        "() => [...document.querySelectorAll('a[href*=\"item?id=\"]')].map(a => a.innerText.replace(/\\s+/g,' ').slice(0,60)).slice(0,8)"
    )
    busca_ret = [r for r in reg.contagem.get("mtop.taobao.idlemtopsearch.pc.search", [])]
    achados[nome] = {"url": page.url, "n_links": len(links), "titulos": titulos, "popup": detectar_popup_login(page), "search_ret": busca_ret[-1] if busca_ret else None}
    print(f"  url: {page.url}")
    print(f"  links: {len(links)}  search_ret: {achados[nome]['search_ret']}")
    for t in titulos:
        print("   -", t)
    page.screenshot(path=str(pasta / f"{nome}.png"))


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pasta = RAIZ / "data" / "fase0" / f"{ts}_e"
    pasta.mkdir(parents=True)
    reg = Registrador(pasta)
    achados: dict = {}

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(RAIZ / "data" / "fase0" / "perfil_chrome"),
            channel="chrome",
            headless=False,
            user_agent=UA,
            locale="zh-CN",
            viewport={"width": 1366, "height": 850},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", reg.ao_responder)

        reg.etapa = "url_spm"
        print("\n== 1. URL com spm ==")
        page.goto(URL_SPM, wait_until="domcontentloaded", timeout=60_000)
        resumo_busca(page, reg, achados, "1_url_spm", pasta)
        pausa()

        reg.etapa = "digitando"
        print("\n== 2. home → digita na caixa → Enter ==")
        page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5_000)
        caixa = page.locator("input[type='search'], input[placeholder], input").first
        caixa.click()
        page.keyboard.type(KEYWORD, delay=120)
        page.wait_for_timeout(800)
        page.keyboard.press("Enter")
        page.wait_for_timeout(3_000)
        # a busca pode abrir em aba nova
        alvo = ctx.pages[-1]
        if alvo is not page:
            alvo.on("response", reg.ao_responder)
            alvo.wait_for_load_state("domcontentloaded")
        resumo_busca(alvo, reg, achados, "2_digitando", pasta)

        (pasta / "achados.json").write_text(json.dumps(achados, ensure_ascii=False, indent=2), encoding="utf-8")
        ctx.close()

    reg.fechar()
    print("\n==================== RESUMO POR API ====================")
    for api, resultados in sorted(reg.contagem.items()):
        if "search" in api or "detail" in api:
            print(api)
            for k, v in Counter(resultados).items():
                print(f"    {k} ×{v}")
    print(f"\nArquivos em: {pasta}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
