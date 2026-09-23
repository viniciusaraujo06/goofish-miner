"""FASE 0i — que parâmetro a aba "在售" (à venda) da loja manda na listagem?

A chamada `mtop.idle.web.xyh.item.list` sem filtro devolve a aba "todos",
com vendidos. Abre a loja `1000000002` (que vem 100% vendida na página 1),
captura o POST da listagem inicial, clica na aba "在售" e captura o POST
seguinte. A diferença entre os dois `data` é o parâmetro procurado.

Uso:
    uv run python -u scripts/fase0i_aba_em_venda.py
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

LOJA = "1000000002"
API = "mtop.idle.web.xyh.item.list"


def posts(reg):
    out = []
    for linha in Path(reg.arquivo.name).read_text(encoding="utf-8").splitlines():
        d = json.loads(linha)
        if d["api"] == API:
            corpo = u.parse_qs(d["post_data"] or "")
            data = json.loads(corpo["data"][0]) if "data" in corpo else None
            n = len(((d.get("corpo") or {}).get("data") or {}).get("cardList") or [])
            status = [((c.get("cardData") or {}).get("itemStatus")) for c in ((d.get("corpo") or {}).get("data") or {}).get("cardList") or []]
            out.append({"etapa": d["etapa"], "ret": d["ret"], "data": data, "n": n, "itemStatus": status})
    return out


def main() -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pasta = RAIZ / "data" / "fase0" / f"{ts}_i"
    pasta.mkdir(parents=True)
    reg = Registrador(pasta)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=False,
                                     args=["--disable-blink-features=AutomationControlled"],
                                     ignore_default_args=["--enable-automation"])
        ctx = browser.new_context(user_agent=UA, locale="zh-CN", viewport={"width": 1366, "height": 850})
        page = ctx.new_page()
        page.on("response", reg.ao_responder)

        reg.etapa = "todos"
        page.goto(f"https://www.goofish.com/personal?userId={LOJA}", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(6_000)
        page.screenshot(path=str(pasta / "1_todos.png"))
        abas = page.evaluate("""() => [...document.querySelectorAll('div, span, a, li')]
            .filter(e => e.children.length === 0 && /在售|已售出|全部/.test(e.textContent || ''))
            .map(e => ({ tag: e.tagName, texto: (e.textContent || '').trim().slice(0, 30), cls: (e.className || '').toString().slice(0, 60) }))""")
        print("candidatos a aba:", json.dumps(abas, ensure_ascii=False)[:1500])
        pausa()

        reg.etapa = "em_venda"
        # o modal de login intercepta o clique; some com ele (só no DOM, nada vai pro servidor)
        page.evaluate("""() => {
            document.querySelectorAll('.ant-modal-wrap, .ant-modal-mask, .ant-modal-root, #alibaba-login-box').forEach(e => e.remove());
            document.body.style.overflow = 'auto';
        }""")
        page.wait_for_timeout(500)
        alvo = page.get_by_text("在售", exact=False).first
        alvo.click()
        page.wait_for_timeout(6_000)
        page.screenshot(path=str(pasta / "2_em_venda.png"))

        browser.close()
    reg.fechar()

    print("\n== chamadas de listagem capturadas ==")
    for p in posts(reg):
        print(json.dumps({k: v for k, v in p.items() if k != "itemStatus"}, ensure_ascii=False))
        print("   itemStatus:", p["itemStatus"])
    print(f"\nArquivos em: {pasta}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
