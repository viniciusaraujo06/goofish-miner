"""FASE 0g — replicar pelo SDK a chamada exata que a página faz.

A página passa no anti-bot; nossa chamada direta não. Diferenças vistas na
query: `spm_pre`, `spm_cnt`, `accountSite`. Aqui capturamos o POST da
página (query + corpo), e repetimos pelo SDK com `ext_querys` e o mesmo
`data`, pra busca (pageNumber 2) e pra detalhe.

Uso:
    uv run python -u scripts/fase0g_replicar_sdk.py
"""

from __future__ import annotations

import json
import sys
import urllib.parse as u
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fase0_reconhecimento import RAIZ, UA, Registrador, pausa  # noqa: E402

COM_SPM = "https://www.goofish.com/search?q=%E7%B2%BE%E5%B7%A55&spm=a21ybx.search.searchInput.0"
ITEM_ID = "900000000001"

JS_CHAMAR = """async ([api, data, extQ]) => {
    const limite = new Promise((_, rej) => setTimeout(() => rej({ ret: ['TIMEOUT_LOCAL::25s'] }), 25000));
    const opts = { api, v: '1.0', appKey: '34839810', dataType: 'originaljson',
                   sessionOption: 'AutoLoginOnly', type: 'POST', timeout: 20000, data, ext_querys: extQ };
    try {
        const r = await Promise.race([window.lib.mtop.request(opts), limite]);
        const d = r.data || {};
        return { ok: true, ret: r.ret, keys: Object.keys(d).slice(0, 12),
                 n: (d.resultList || []).length, sold: d.sellerDO && d.sellerDO.hasSoldNumInteger };
    } catch (e) { return { ok: false, ret: e && e.ret }; }
}"""


def captura(reg, api):
    """Última chamada registrada daquela API (query decodificada + corpo POST)."""
    for linha in reversed(Path(reg.arquivo.name).read_text(encoding="utf-8").splitlines()):
        d = json.loads(linha)
        if d["api"] == api:
            q = {k: v[0] for k, v in u.parse_qs(u.urlparse(d["url"]).query).items()}
            corpo = u.parse_qs(d["post_data"] or "")
            data = json.loads(corpo["data"][0]) if "data" in corpo else None
            return {"ret": d["ret"], "query": q, "data": data}
    return None


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pasta = RAIZ / "data" / "fase0" / f"{ts}_g"
    pasta.mkdir(parents=True)
    reg = Registrador(pasta)
    achados: dict = {}

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(RAIZ / "data" / "fase0" / "perfil_chrome"),
            channel="chrome", headless=False, user_agent=UA, locale="zh-CN",
            viewport={"width": 1366, "height": 850},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("response", reg.ao_responder)

        # ---- busca --------------------------------------------------------
        reg.etapa = "busca_pagina"
        print("\n== 1. busca com spm (chamada da página) ==")
        page.goto(COM_SPM, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(10_000)
        cap = captura(reg, "mtop.taobao.idlemtopsearch.pc.search")
        achados["busca_pagina"] = cap
        print("  ret:", cap["ret"])
        print("  query extra:", {k: v for k, v in cap["query"].items() if k in ("spm_cnt", "spm_pre", "accountSite")})
        print("  data:", json.dumps(cap["data"], ensure_ascii=False)[:600])
        pausa()

        reg.etapa = "busca_sdk_replica"
        print("\n== 2. mesma busca pelo SDK, pageNumber=2, com ext_querys ==")
        data2 = dict(cap["data"] or {})
        data2["pageNumber"] = 2
        ext = {k: cap["query"][k] for k in ("spm_cnt", "spm_pre", "accountSite") if k in cap["query"]}
        r = page.evaluate(JS_CHAMAR, ["mtop.taobao.idlemtopsearch.pc.search", data2, ext])
        achados["busca_sdk_replica"] = {"data": data2, "ext_querys": ext, "resultado": r}
        print("  ", json.dumps(r, ensure_ascii=False)[:400])
        pausa()

        reg.etapa = "busca_sdk_sem_ext"
        print("\n== 3. mesma busca pelo SDK, pageNumber=2, SEM ext_querys (controle) ==")
        r = page.evaluate(JS_CHAMAR, ["mtop.taobao.idlemtopsearch.pc.search", data2, {}])
        achados["busca_sdk_sem_ext"] = r
        print("  ", json.dumps(r, ensure_ascii=False)[:400])
        pausa()

        # ---- detalhe ------------------------------------------------------
        reg.etapa = "detalhe_pagina"
        print("\n== 4. detalhe (chamada da página) ==")
        page.goto(f"https://www.goofish.com/item?id={ITEM_ID}", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(8_000)
        capd = captura(reg, "mtop.taobao.idle.pc.detail")
        achados["detalhe_pagina"] = capd
        print("  ret:", capd["ret"] if capd else None)
        if capd:
            print("  query extra:", {k: v for k, v in capd["query"].items() if k in ("spm_cnt", "spm_pre", "accountSite")})
            print("  data:", json.dumps(capd["data"], ensure_ascii=False)[:400])
        pausa()

        reg.etapa = "detalhe_sdk_replica"
        print("\n== 5. detalhe de OUTRO item pelo SDK com ext_querys ==")
        outro = "900000000002"
        datad = dict(capd["data"] or {}) if capd else {"itemId": outro}
        datad["itemId"] = outro
        extd = {k: capd["query"][k] for k in ("spm_cnt", "spm_pre", "accountSite") if capd and k in capd["query"]}
        r = page.evaluate(JS_CHAMAR, ["mtop.taobao.idle.pc.detail", datad, extd])
        achados["detalhe_sdk_replica"] = {"data": datad, "ext_querys": extd, "resultado": r}
        print("  ", json.dumps(r, ensure_ascii=False)[:400])

        (pasta / "achados.json").write_text(json.dumps(achados, ensure_ascii=False, indent=2), encoding="utf-8")
        ctx.close()

    reg.fechar()
    print(f"\nArquivos em: {pasta}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
