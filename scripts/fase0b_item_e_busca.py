"""FASE 0b — anúncio individual + segunda tentativa de busca.

Complementa o `fase0_reconhecimento.py`. Diferenças:
- contexto **persistente** (cookies sobrevivem entre rodadas, como um usuário real);
- flag `--disable-blink-features=AutomationControlled` pra não expor `navigator.webdriver`;
- abre um anúncio a partir da página do fornecedor (que sabemos que funciona);
- só depois tenta a busca, por navegação e pelo SDK.

Uso:
    uv run python scripts/fase0b_item_e_busca.py [--headless]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fase0_reconhecimento import (  # noqa: E402
    FORNECEDOR_ID,
    KEYWORD,
    RAIZ,
    UA,
    Registrador,
    chamar_mtop,
    detectar_popup_login,
    inspecionar_sdk,
    pausa,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pasta = RAIZ / "data" / "fase0" / f"{ts}_b"
    pasta.mkdir(parents=True)
    perfil = RAIZ / "data" / "fase0" / "perfil_chrome"
    reg = Registrador(pasta)
    achados: dict = {"timestamp": ts, "etapas": {}}

    def salvar() -> None:
        (pasta / "achados.json").write_text(json.dumps(achados, ensure_ascii=False, indent=2), encoding="utf-8")

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(perfil),
            channel="chrome",
            headless=args.headless,
            user_agent=UA,
            locale="zh-CN",
            viewport={"width": 1366, "height": 850},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", reg.ao_responder)

        # ---- 1. home (aquece cookies) --------------------------------------
        reg.etapa = "home"
        print("\n== 1. home ==")
        page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5_000)
        webdriver = page.evaluate("() => ({ webdriver: navigator.webdriver, ua: navigator.userAgent, plugins: navigator.plugins.length })")
        achados["etapas"]["home"] = {"navigator": webdriver}
        print("  navigator:", json.dumps(webdriver, ensure_ascii=False))
        salvar()
        pausa()

        # ---- 2. fornecedor → pega link de item -----------------------------
        reg.etapa = "vendedor"
        print(f"\n== 2. fornecedor {FORNECEDOR_ID} ==")
        page.goto(f"https://www.goofish.com/personal?userId={FORNECEDOR_ID}", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(6_000)
        links = page.evaluate("() => [...document.querySelectorAll('a[href*=\"item?id=\"]')].map(a => a.href)")
        achados["etapas"]["vendedor"] = {"n_links": len(links), "primeiros": links[:5]}
        print(f"  links de item: {len(links)}")
        salvar()
        pausa()

        # ---- 3. anúncio individual -----------------------------------------
        reg.etapa = "item"
        print("\n== 3. anúncio ==")
        if links:
            page.goto(links[0], wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(8_000)
            popup = detectar_popup_login(page)
            texto = page.evaluate("() => document.body.innerText.slice(0, 1500)")
            achados["etapas"]["item"] = {"url": page.url, "titulo": page.title(), "popup": popup, "texto": texto}
            print("  url:", page.url)
            print("  titulo:", page.title())
            print("  popup:", json.dumps(popup, ensure_ascii=False)[:300])
            page.screenshot(path=str(pasta / "3_item.png"))
            # tenta detalhe via SDK com o id do anúncio
            item_id = page.url.split("id=")[-1].split("&")[0]
            r = chamar_mtop(page, "mtop.taobao.idle.pc.detail", {"itemId": item_id})
            achados["etapas"]["item"]["sdk_detail"] = {"item_id": item_id, "ret": r.get("ret"), "keys": r.get("keys"), "erro": r.get("erro")}
            achados["etapas"]["item"]["sdk_detail_data"] = r.get("data")
            print(f"  SDK detail({item_id}): ret={r.get('ret')} keys={r.get('keys')}")
        else:
            achados["etapas"]["item"] = {"pulado": "sem links"}
        salvar()
        pausa()

        # ---- 4. busca por navegação ----------------------------------------
        reg.etapa = "busca_url"
        print(f"\n== 4. busca por URL: {KEYWORD} ==")
        page.goto(f"https://www.goofish.com/search?q={KEYWORD}", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(10_000)
        links_b = page.evaluate("() => [...document.querySelectorAll('a[href*=\"item?id=\"]')].map(a => a.href)")
        popup = detectar_popup_login(page)
        achados["etapas"]["busca_url"] = {"n_links": len(links_b), "primeiros": links_b[:5], "popup": popup}
        print(f"  links de item: {len(links_b)}")
        print("  popup:", json.dumps(popup, ensure_ascii=False)[:300])
        page.screenshot(path=str(pasta / "4_busca.png"))
        salvar()
        pausa()

        # ---- 5. busca pelo SDK ---------------------------------------------
        reg.etapa = "busca_sdk"
        print("\n== 5. busca pelo SDK ==")
        sdk = inspecionar_sdk(page)
        if sdk.get("lib_mtop_request"):
            r = chamar_mtop(
                page,
                "mtop.taobao.idlemtopsearch.pc.search",
                {"pageNumber": 1, "keyword": KEYWORD, "rowsPerPage": 30, "sortValue": "", "sortField": "", "fromFilter": False, "fromLeafCat": False},
            )
            achados["etapas"]["busca_sdk"] = {"ret": r.get("ret"), "keys": r.get("keys"), "erro": r.get("erro"), "data": r.get("data")}
            print(f"  ret={r.get('ret')} keys={r.get('keys')}")
        salvar()

        ctx.close()

    reg.fechar()
    print("\n==================== RESUMO POR API ====================")
    for api, resultados in sorted(reg.contagem.items()):
        from collections import Counter
        print(api)
        for k, v in Counter(resultados).items():
            print(f"    {k} ×{v}")
    print(f"\nArquivos em: {pasta}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
