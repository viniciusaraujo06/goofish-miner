"""FASE 0 — reconhecimento do Goofish sem login.

Script descartável. Abre goofish.com em contexto anônimo, navega por
busca, anúncio e página de vendedor, e registra toda chamada `mtop.*`
(status HTTP, código `ret` da API e corpo completo). Ao final imprime um
resumo por API. Tudo vai para `data/fase0/<timestamp>/`.

Uso:
    uv run python scripts/fase0_reconhecimento.py [--headless]
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import Page, Response, sync_playwright

RAIZ = Path(__file__).resolve().parent.parent
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
KEYWORD = "精工5"
FORNECEDOR_ID = "1000000000001"  # Loja A

# MTOP devolve JSONP quando chamado com callback: mtopjsonp1({...})
RE_JSONP = re.compile(r"^\s*[\w$.]+\s*\(\s*(.*)\s*\)\s*;?\s*$", re.S)
RE_API = re.compile(r"/h5/(mtop\.[\w.]+)/([\d.]+)/")


def pausa(minimo: float = 3, maximo: float = 8) -> None:
    t = random.uniform(minimo, maximo)
    print(f"    ... pausa {t:.1f}s")
    time.sleep(t)


class Registrador:
    """Captura respostas mtop.* e grava uma linha JSONL por chamada."""

    def __init__(self, pasta: Path) -> None:
        self.pasta = pasta
        self.arquivo = (pasta / "chamadas.jsonl").open("a", encoding="utf-8")
        self.etapa = "inicio"
        self.contagem: dict[str, list[str]] = defaultdict(list)
        self.n = 0

    def ao_responder(self, resp: Response) -> None:
        m = RE_API.search(resp.url)
        if not m:
            return
        api, versao = m.group(1), m.group(2)
        corpo_txt = ""
        corpo_json = None
        try:
            corpo_txt = resp.text()
            bruto = corpo_txt
            jp = RE_JSONP.match(bruto)
            if jp:
                bruto = jp.group(1)
            corpo_json = json.loads(bruto)
        except Exception as e:  # noqa: BLE001 — só reconhecimento
            corpo_json = {"_erro_parse": repr(e)}
        ret = corpo_json.get("ret") if isinstance(corpo_json, dict) else None
        ret_str = ";".join(ret) if isinstance(ret, list) else str(ret)
        self.n += 1
        self.contagem[api].append(f"HTTP {resp.status} | {ret_str}")
        print(f"  [{self.n:03d}] {api} v{versao}  HTTP {resp.status}  ret={ret_str}")
        linha = {
            "n": self.n,
            "etapa": self.etapa,
            "api": api,
            "versao": versao,
            "url": resp.url,
            "status": resp.status,
            "post_data": resp.request.post_data,
            "ret": ret,
            "tamanho": len(corpo_txt),
            "corpo": corpo_json,
        }
        self.arquivo.write(json.dumps(linha, ensure_ascii=False) + "\n")
        self.arquivo.flush()

    def fechar(self) -> None:
        self.arquivo.close()


def inspecionar_sdk(page: Page) -> dict:
    """Verifica se o SDK MTOP está exposto na página e sob qual nome."""
    return page.evaluate(
        """() => {
            const out = {};
            out.lib_keys = window.lib ? Object.keys(window.lib) : null;
            out.lib_mtop = !!(window.lib && window.lib.mtop);
            out.lib_mtop_request = !!(window.lib && window.lib.mtop && window.lib.mtop.request);
            out.mtop_global = typeof window.mtop;
            out.candidatos = Object.keys(window).filter(k => /mtop|lib|__/i.test(k)).slice(0, 60);
            return out;
        }"""
    )


def chamar_mtop(page: Page, api: str, dados: dict, versao: str = "1.0") -> dict:
    """Dispara uma chamada MTOP pelo SDK da própria página."""
    return page.evaluate(
        """async ([api, v, data]) => {
            try {
                const r = await window.lib.mtop.request({ api, v, data, type: 'GET', dataType: 'json' });
                return { ok: true, ret: r.ret, keys: Object.keys(r.data || {}), data: r.data };
            } catch (e) {
                return { ok: false, ret: e && e.ret, erro: String(e && (e.message || e)), bruto: e };
            }
        }""",
        [api, versao, dados],
    )


def detectar_popup_login(page: Page) -> dict:
    return page.evaluate(
        """() => {
            const ifr = [...document.querySelectorAll('iframe')].map(i => i.src).filter(s => /login|passport/i.test(s));
            const overlays = [...document.querySelectorAll('div')].filter(d => {
                const cs = getComputedStyle(d);
                return cs.position === 'fixed' && parseInt(cs.zIndex || '0') > 1000 && d.offsetWidth > 300 && d.offsetHeight > 300;
            }).map(d => (d.className || d.id || d.tagName).toString().slice(0, 80));
            return { iframes_login: ifr, overlays_fixos: overlays.slice(0, 10), body_overflow: getComputedStyle(document.body).overflow };
        }"""
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pasta = RAIZ / "data" / "fase0" / ts
    pasta.mkdir(parents=True)
    reg = Registrador(pasta)
    achados: dict = {"timestamp": ts, "etapas": {}}

    def salvar_achados() -> None:
        (pasta / "achados.json").write_text(
            json.dumps(achados, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def foto(nome: str) -> None:
        page.screenshot(path=str(pasta / f"{nome}.png"), full_page=False)

    with sync_playwright() as pw:
        # Chrome do sistema tem o fingerprint mais natural; cai pro chromium empacotado se não houver.
        try:
            browser = pw.chromium.launch(channel="chrome", headless=args.headless)
            print("Navegador: Chrome do sistema")
        except Exception:
            browser = pw.chromium.launch(headless=args.headless)
            print("Navegador: chromium do Playwright")
        ctx = browser.new_context(user_agent=UA, locale="zh-CN", viewport={"width": 1366, "height": 850})
        page = ctx.new_page()
        page.on("response", reg.ao_responder)

        # ---- etapa 1: home ------------------------------------------------
        reg.etapa = "home"
        print("\n== 1. home ==")
        page.goto("https://www.goofish.com/", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(6_000)
        sdk = inspecionar_sdk(page)
        popup = detectar_popup_login(page)
        achados["etapas"]["home"] = {"sdk": sdk, "popup": popup, "titulo": page.title(), "url": page.url}
        print("  SDK:", json.dumps(sdk, ensure_ascii=False))
        print("  popup:", json.dumps(popup, ensure_ascii=False))
        foto("1_home")
        salvar_achados()
        pausa()

        # ---- etapa 2: busca via navegação -------------------------------
        reg.etapa = "busca_url"
        print(f"\n== 2. busca por URL: {KEYWORD} ==")
        page.goto(f"https://www.goofish.com/search?q={KEYWORD}", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(8_000)
        links = page.evaluate(
            "() => [...document.querySelectorAll('a[href*=\"item?id=\"]')].map(a => a.href).slice(0, 10)"
        )
        popup = detectar_popup_login(page)
        achados["etapas"]["busca_url"] = {"links_item": links, "popup": popup, "url": page.url}
        print(f"  links de item encontrados no DOM: {len(links)}")
        print("  popup:", json.dumps(popup, ensure_ascii=False))
        foto("2_busca")
        salvar_achados()
        pausa()

        # ---- etapa 3: chamadas diretas pelo SDK --------------------------
        reg.etapa = "sdk_direto"
        print("\n== 3. chamadas diretas via window.lib.mtop ==")
        sdk = inspecionar_sdk(page)
        achados["etapas"]["sdk_direto"] = {"sdk": sdk, "chamadas": {}}
        if sdk.get("lib_mtop_request"):
            tentativas = {
                "busca": (
                    "mtop.taobao.idlemtopsearch.pc.search",
                    {"pageNumber": 1, "keyword": KEYWORD, "rowsPerPage": 30, "sortValue": "", "sortField": "", "fromFilter": False},
                ),
                "vendedor_itens": (
                    "mtop.idle.web.xyh.item.list",
                    {"userId": FORNECEDOR_ID, "pageNumber": 1, "pageSize": 20},
                ),
                "vendedor_perfil": (
                    "mtop.idle.web.user.page.head",
                    {"userId": FORNECEDOR_ID},
                ),
            }
            for nome, (api, dados) in tentativas.items():
                r = chamar_mtop(page, api, dados)
                resumo = {k: v for k, v in r.items() if k != "data"}
                achados["etapas"]["sdk_direto"]["chamadas"][nome] = {"api": api, "resultado": r}
                print(f"  {nome:16s} {api}: {json.dumps(resumo, ensure_ascii=False)[:300]}")
                salvar_achados()
                pausa(2, 4)
        else:
            print("  window.lib.mtop.request NÃO existe — chamada direta indisponível")

        # ---- etapa 4: anúncio ---------------------------------------------
        reg.etapa = "item"
        print("\n== 4. página de anúncio ==")
        if links:
            page.goto(links[0], wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(8_000)
            popup = detectar_popup_login(page)
            achados["etapas"]["item"] = {"url": page.url, "titulo": page.title(), "popup": popup}
            print("  url:", page.url)
            print("  popup:", json.dumps(popup, ensure_ascii=False))
            foto("4_item")
        else:
            achados["etapas"]["item"] = {"pulado": "nenhum link de item na busca"}
            print("  pulado: busca não rendeu links")
        salvar_achados()
        pausa()

        # ---- etapa 5: página do fornecedor --------------------------------
        reg.etapa = "vendedor"
        print(f"\n== 5. página do fornecedor {FORNECEDOR_ID} ==")
        page.goto(f"https://www.goofish.com/personal?userId={FORNECEDOR_ID}", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(8_000)
        # rola pra ver se a listagem pagina sem login
        for _ in range(3):
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(2_000)
        links_v = page.evaluate(
            "() => [...document.querySelectorAll('a[href*=\"item?id=\"]')].map(a => a.href).slice(0, 50)"
        )
        popup = detectar_popup_login(page)
        achados["etapas"]["vendedor"] = {"url": page.url, "links_item": links_v, "popup": popup}
        print(f"  links de item no DOM após scroll: {len(links_v)}")
        print("  popup:", json.dumps(popup, ensure_ascii=False))
        foto("5_vendedor")
        salvar_achados()

        browser.close()

    reg.fechar()

    # ---- resumo ---------------------------------------------------------
    print("\n==================== RESUMO POR API ====================")
    linhas = []
    for api, resultados in sorted(reg.contagem.items()):
        agrupado = defaultdict(int)
        for r in resultados:
            agrupado[r] += 1
        detalhe = ", ".join(f"{k} ×{v}" for k, v in agrupado.items())
        linhas.append(f"{api}\n    {detalhe}")
        print(f"{api}\n    {detalhe}")
    (pasta / "resumo.txt").write_text("\n".join(linhas), encoding="utf-8")
    print(f"\nArquivos em: {pasta}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
