"""FASE 0d — detalhe do anúncio via SDK com os params que a própria página usa.

A chamada feita pela página passa com `appKey=34839810`,
`sessionOption=AutoLoginOnly` e `type=originaljson`; a nossa com o appKey
padrão do SDK (12574478) caiu no anti-bot. Testa variantes.

Uso:
    uv run python scripts/fase0d_sdk_params.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fase0_reconhecimento import FORNECEDOR_ID, RAIZ, UA, Registrador, pausa  # noqa: E402

ITEM_ID = "900000000001"


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pasta = RAIZ / "data" / "fase0" / f"{ts}_d"
    pasta.mkdir(parents=True)
    reg = Registrador(pasta)
    resultados: dict = {}

    variantes = {
        "A_pagina_post": {"appKey": "34839810", "dataType": "originaljson", "sessionOption": "AutoLoginOnly", "type": "POST"},
        "B_pagina_get": {"appKey": "34839810", "dataType": "originaljson", "sessionOption": "AutoLoginOnly", "type": "GET"},
        "C_so_appkey": {"appKey": "34839810"},
        "D_padrao": {},
    }

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=False)
        ctx = browser.new_context(user_agent=UA, locale="zh-CN", viewport={"width": 1366, "height": 850})
        page = ctx.new_page()
        page.on("response", reg.ao_responder)

        reg.etapa = "vendedor"
        page.goto(f"https://www.goofish.com/personal?userId={FORNECEDOR_ID}", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(6_000)
        pausa()

        for nome, extra in variantes.items():
            reg.etapa = nome
            print(f"\n== {nome}: {extra} ==")
            r = page.evaluate(
                """async ([itemId, extra]) => {
                    const opts = Object.assign({
                        api: 'mtop.taobao.idle.pc.detail', v: '1.0', data: { itemId }
                    }, extra);
                    const limite = new Promise((_, rej) => setTimeout(() => rej({ ret: ['TIMEOUT_LOCAL::25s'] }), 25000));
                    try {
                        const r = await Promise.race([window.lib.mtop.request(opts), limite]);
                        return { ok: true, ret: r.ret, keys: Object.keys(r.data || {}),
                                 sold: r.data && r.data.sellerDO && r.data.sellerDO.hasSoldNumInteger };
                    } catch (e) {
                        return { ok: false, ret: e && e.ret, erro: String(e && (e.message || e)) };
                    }
                }""",
                [ITEM_ID, extra],
            )
            resultados[nome] = {"opts": extra, "resultado": r}
            print("  ", json.dumps(r, ensure_ascii=False)[:400])
            pausa(4, 8)

        # bônus: lista do vendedor com os params da página, pra confirmar que continua ok
        reg.etapa = "lista_pagina"
        r = page.evaluate(
            """async (userId) => {
                const limite = new Promise((_, rej) => setTimeout(() => rej({ ret: ['TIMEOUT_LOCAL::25s'] }), 25000));
                try {
                    const r = await Promise.race([window.lib.mtop.request({
                        api: 'mtop.idle.web.xyh.item.list', v: '1.0',
                        data: { userId, pageNumber: 2, pageSize: 20 },
                        appKey: '34839810', dataType: 'originaljson', sessionOption: 'AutoLoginOnly', type: 'POST'
                    }), limite]);
                    return { ok: true, ret: r.ret, n: (r.data.cardList || []).length, nextPage: r.data.nextPage };
                } catch (e) { return { ok: false, ret: e && e.ret }; }
            }""",
            FORNECEDOR_ID,
        )
        resultados["lista_pagina2"] = r
        print("\n== lista página 2 com params da página ==\n  ", json.dumps(r, ensure_ascii=False))

        browser.close()

    reg.fechar()
    (pasta / "resultados.json").write_text(json.dumps(resultados, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nArquivos em: {pasta}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
