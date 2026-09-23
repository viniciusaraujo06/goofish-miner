"""FASE 0c — busca com a extensão CSSBuy Xianyu Helper carregada.

A extensão só esconde o popup de login e libera o scroll (CSS + clique no
"fechar"); não mexe em cookies nem nas chamadas MTOP. Este script existe
pra confirmar isso empiricamente em vez de assumir.

Pré-requisito: cópia da extensão (sem a pasta `_metadata`) em
`data/fase0/ext_cssbuy/`.

Uso:
    uv run python scripts/fase0c_extensao.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fase0_reconhecimento import (  # noqa: E402
    KEYWORD,
    RAIZ,
    UA,
    Registrador,
    chamar_mtop,
    detectar_popup_login,
    pausa,
)


def main() -> int:
    ext = RAIZ / "data" / "fase0" / "ext_cssbuy"
    if not (ext / "manifest.json").exists():
        print(f"extensão não encontrada em {ext}")
        return 1

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pasta = RAIZ / "data" / "fase0" / f"{ts}_c"
    pasta.mkdir(parents=True)
    perfil = RAIZ / "data" / "fase0" / "perfil_chrome"
    reg = Registrador(pasta)
    achados: dict = {"timestamp": ts, "etapas": {}}

    def salvar() -> None:
        (pasta / "achados.json").write_text(json.dumps(achados, ensure_ascii=False, indent=2), encoding="utf-8")

    # Chrome de marca >= 137 ignora --load-extension; o Chromium empacotado não abre
    # nesta máquina (erro de configuração lado a lado), então usamos o Edge.
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(perfil) + "_edge",
            channel="msedge",
            headless=False,
            user_agent=UA,
            locale="zh-CN",
            viewport={"width": 1366, "height": 850},
            args=[
                "--disable-blink-features=AutomationControlled",
                f"--disable-extensions-except={ext}",
                f"--load-extension={ext}",
            ],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", reg.ao_responder)

        reg.etapa = "home"
        print("\n== 1. home ==")
        page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(6_000)
        ext_ativa = page.evaluate(
            "() => [...document.querySelectorAll('style')].some(s => s.textContent.includes('Login modals'))"
        )
        popup = detectar_popup_login(page)
        achados["etapas"]["home"] = {"extensao_css_injetado": ext_ativa, "popup": popup}
        print("  CSS da extensão injetado:", ext_ativa)
        print("  popup:", json.dumps(popup, ensure_ascii=False)[:300])
        page.screenshot(path=str(pasta / "1_home.png"))
        salvar()
        pausa()

        reg.etapa = "busca_url"
        print(f"\n== 2. busca por URL: {KEYWORD} ==")
        page.goto(f"https://www.goofish.com/search?q={KEYWORD}", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(10_000)
        for _ in range(2):
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(2_000)
        links = page.evaluate("() => [...document.querySelectorAll('a[href*=\"item?id=\"]')].map(a => a.href)")
        popup = detectar_popup_login(page)
        texto = page.evaluate("() => document.body.innerText.slice(0, 800)")
        achados["etapas"]["busca_url"] = {"n_links": len(links), "primeiros": links[:5], "popup": popup, "texto": texto}
        print(f"  links de item: {len(links)}")
        print("  popup:", json.dumps(popup, ensure_ascii=False)[:300])
        page.screenshot(path=str(pasta / "2_busca.png"))
        salvar()
        pausa()

        reg.etapa = "busca_sdk"
        print("\n== 3. busca pelo SDK ==")
        r = chamar_mtop(
            page,
            "mtop.taobao.idlemtopsearch.pc.search",
            {"pageNumber": 1, "keyword": KEYWORD, "rowsPerPage": 30, "sortValue": "", "sortField": "", "fromFilter": False, "fromLeafCat": False},
        )
        achados["etapas"]["busca_sdk"] = {"ret": r.get("ret"), "keys": r.get("keys"), "data": r.get("data")}
        print(f"  ret={r.get('ret')} keys={r.get('keys')}")
        salvar()

        ctx.close()

    reg.fechar()
    print("\n==================== RESUMO POR API ====================")
    for api, resultados in sorted(reg.contagem.items()):
        print(api)
        for k, v in Counter(resultados).items():
            print(f"    {k} ×{v}")
    print(f"\nArquivos em: {pasta}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
