"""FASE 0h — tudo num contexto anônimo limpo, sem nenhuma chamada bloqueada antes.

Hipótese: o perfil persistente acumulou punição do anti-bot após vários
RGV587. Num contexto novo, na ordem que o coletor faria:
1. página do fornecedor (lista via página)
2. detalhe por navegação (funcionou em 0b)
3. detalhe de OUTRO item pelo SDK com ext_querys
4. busca pelo SDK com ext_querys, sem nunca ter aberto a página de busca

Uso:
    uv run python -u scripts/fase0h_contexto_limpo.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fase0_reconhecimento import FORNECEDOR_ID, RAIZ, UA, Registrador, pausa  # noqa: E402
from fase0g_replicar_sdk import JS_CHAMAR, captura  # noqa: E402

BUSCA_DATA = {
    "pageNumber": 1, "keyword": "精工 4R36", "fromFilter": False, "rowsPerPage": 30,
    "sortValue": "", "sortField": "", "customDistance": "", "gps": "", "propValueStr": {},
    "customGps": "", "searchReqFromPage": "pcSearch", "extraFilterValue": "{}", "userPositionJson": "{}",
}
BUSCA_EXT = {"accountSite": "xianyu", "spm_cnt": "a21ybx.search.0.0", "spm_pre": "a21ybx.search.searchInput.0"}


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pasta = RAIZ / "data" / "fase0" / f"{ts}_h"
    pasta.mkdir(parents=True)
    reg = Registrador(pasta)
    achados: dict = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=False, args=["--disable-blink-features=AutomationControlled"], ignore_default_args=["--enable-automation"])
        ctx = browser.new_context(user_agent=UA, locale="zh-CN", viewport={"width": 1366, "height": 850})
        page = ctx.new_page()
        page.on("response", reg.ao_responder)

        reg.etapa = "vendedor"
        print("\n== 1. fornecedor ==")
        page.goto(f"https://www.goofish.com/personal?userId={FORNECEDOR_ID}", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(6_000)
        links = page.evaluate("() => [...document.querySelectorAll('a[href*=\"item?id=\"]')].map(a => a.href)")
        print(f"  links: {len(links)}")
        pausa()

        reg.etapa = "detalhe_nav"
        print("\n== 2. detalhe por navegação ==")
        page.goto(links[0], wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(8_000)
        capd = captura(reg, "mtop.taobao.idle.pc.detail")
        achados["detalhe_nav"] = capd
        print("  ret:", capd["ret"] if capd else None, " query:", {k: v for k, v in (capd or {}).get("query", {}).items() if k in ("spm_cnt", "spm_pre", "accountSite")})
        pausa()

        reg.etapa = "detalhe_sdk"
        print("\n== 3. detalhe de outro item pelo SDK com ext_querys ==")
        outro = links[1].split("id=")[1].split("&")[0]
        extd = {k: capd["query"][k] for k in ("spm_cnt", "spm_pre", "accountSite") if capd and k in capd["query"]}
        r = page.evaluate(JS_CHAMAR, ["mtop.taobao.idle.pc.detail", {"itemId": outro}, extd])
        achados["detalhe_sdk"] = {"item": outro, "ext": extd, "r": r}
        print("  ", json.dumps(r, ensure_ascii=False)[:300])
        pausa()

        reg.etapa = "busca_sdk"
        print("\n== 4. busca pelo SDK com ext_querys, a partir da página do item ==")
        r = page.evaluate(JS_CHAMAR, ["mtop.taobao.idlemtopsearch.pc.search", BUSCA_DATA, BUSCA_EXT])
        achados["busca_sdk"] = r
        print("  ", json.dumps(r, ensure_ascii=False)[:300])

        (pasta / "achados.json").write_text(json.dumps(achados, ensure_ascii=False, indent=2), encoding="utf-8")
        browser.close()

    reg.fechar()
    print(f"\nArquivos em: {pasta}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
