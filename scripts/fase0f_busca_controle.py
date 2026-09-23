"""FASE 0f — controle: a busca passa por causa do `spm` ou por acaso?

Mesmo perfil persistente, três passos:
1. `search?q=...` SEM spm  → esperado: bloqueado (RGV587)
2. `search?q=...&spm=a21ybx.search.searchInput.0` → esperado: SUCCESS
3. a partir da página 2, chamada direta pelo SDK pedindo pageNumber=2
   (é o que o coletor vai precisar pra paginar)

Uso:
    uv run python -u scripts/fase0f_busca_controle.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fase0_reconhecimento import RAIZ, UA, Registrador, pausa  # noqa: E402

Q = "%E7%B2%BE%E5%B7%A55"
SEM_SPM = f"https://www.goofish.com/search?q={Q}"
COM_SPM = f"https://www.goofish.com/search?q={Q}&spm=a21ybx.search.searchInput.0"


def ultimo_search(reg):
    r = reg.contagem.get("mtop.taobao.idlemtopsearch.pc.search", [])
    return r[-1] if r else None


def n_links(page):
    return page.evaluate("() => document.querySelectorAll('a[href*=\"item?id=\"]').length")


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pasta = RAIZ / "data" / "fase0" / f"{ts}_f"
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

        reg.etapa = "sem_spm"
        print("\n== 1. SEM spm ==")
        page.goto(SEM_SPM, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(10_000)
        achados["sem_spm"] = {"search_ret": ultimo_search(reg), "links": n_links(page)}
        print("  ", achados["sem_spm"])
        pausa()

        reg.etapa = "com_spm"
        print("\n== 2. COM spm ==")
        page.goto(COM_SPM, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(10_000)
        achados["com_spm"] = {"search_ret": ultimo_search(reg), "links": n_links(page)}
        print("  ", achados["com_spm"])
        pausa()

        reg.etapa = "sdk_pagina2"
        print("\n== 3. SDK pageNumber=2 a partir da página com spm ==")
        r = page.evaluate(
            """async () => {
                const limite = new Promise((_, rej) => setTimeout(() => rej({ ret: ['TIMEOUT_LOCAL::25s'] }), 25000));
                const opts = {
                    api: 'mtop.taobao.idlemtopsearch.pc.search', v: '1.0',
                    appKey: '34839810', dataType: 'originaljson', sessionOption: 'AutoLoginOnly', type: 'POST',
                    data: { pageNumber: 2, keyword: '精工5', rowsPerPage: 30, sortValue: '', sortField: '',
                            fromFilter: false, fromLeafCat: false, fromLeafCat: false }
                };
                try {
                    const r = await Promise.race([window.lib.mtop.request(opts), limite]);
                    const lista = (r.data && r.data.resultList) || [];
                    return { ok: true, ret: r.ret, keys: Object.keys(r.data || {}), n: lista.length };
                } catch (e) { return { ok: false, ret: e && e.ret }; }
            }"""
        )
        achados["sdk_pagina2"] = r
        print("  ", json.dumps(r, ensure_ascii=False)[:400])

        (pasta / "achados.json").write_text(json.dumps(achados, ensure_ascii=False, indent=2), encoding="utf-8")
        ctx.close()

    reg.fechar()
    print(f"\nArquivos em: {pasta}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
