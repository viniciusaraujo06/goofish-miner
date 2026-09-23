"""Chamadas MTOP executadas dentro da página do Goofish, pelo SDK que ela já carrega.

Nunca reimplementar a assinatura fora do browser. O anti-bot confere, além
da assinatura, o "rastro de navegação" (`spm_cnt`/`spm_pre`) e o
`accountSite` — por isso cada API tem seus `ext_querys` fixos aqui.

A resposta é lida da rede, não do retorno do SDK: quando a chamada é
bloqueada (`RGV587_ERROR::SM`) o SDK devolve um `TIMEOUT` genérico e
esconde o motivo real.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout

URL_PESSOAL = "https://www.goofish.com/personal?userId={user_id}"
URL_ITEM = "https://www.goofish.com/item?id={item_id}"
# Sem o `spm` a busca por URL cai no login (docs/fase0.md).
URL_BUSCA = "https://www.goofish.com/search?q={termo}&spm=a21ybx.search.searchInput.0"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

API_LISTA = "mtop.idle.web.xyh.item.list"
API_PERFIL = "mtop.idle.web.user.page.head"
API_BUSCA = "mtop.taobao.idlemtopsearch.pc.search"

EXT_QUERYS = {
    API_LISTA: {"accountSite": "xianyu", "spm_cnt": "a21ybx.personal.0.0"},
    API_PERFIL: {"accountSite": "xianyu", "spm_cnt": "a21ybx.personal.0.0"},
    API_BUSCA: {"accountSite": "xianyu", "spm_cnt": "a21ybx.search.0.0", "spm_pre": "a21ybx.search.searchInput.0"},
}

_RE_JSONP = re.compile(r"^\s*[\w$.]+\s*\(\s*(.*)\s*\)\s*;?\s*$", re.S)

_JS_CHAMAR = """async ([api, data, extQ, timeoutMs]) => {
    const limite = new Promise((_, rej) => setTimeout(() => rej({ ret: ['TIMEOUT_LOCAL'] }), timeoutMs + 5000));
    const opts = { api, v: '1.0', appKey: '34839810', dataType: 'originaljson',
                   sessionOption: 'AutoLoginOnly', type: 'POST', timeout: timeoutMs,
                   data, ext_querys: extQ };
    try {
        const r = await Promise.race([window.lib.mtop.request(opts), limite]);
        return { ok: true, ret: r.ret };
    } catch (e) {
        return { ok: false, ret: e && e.ret, erro: String(e && (e.message || e)) };
    }
}"""


class MtopErro(Exception):
    """Falha genérica (rede, timeout, resposta não-JSON)."""


class Bloqueado(MtopErro):
    """Anti-bot recusou a chamada. Não insistir: cada tentativa piora a punição."""

    def __init__(self, api: str, ret: list[str]):
        super().__init__(f"{api}: {ret}")
        self.api = api
        self.ret = ret


def _corpo(api: str, texto: str) -> dict:
    """JSON (ou JSONP) da resposta. HTML no lugar do JSON é a página de punição
    da Alibaba — punição por IP, mais grave que o RGV587: aborta e não insiste."""
    inicio = texto.lstrip()[:15].lower()
    if inicio.startswith("<!doctype") or inicio.startswith("<html"):
        raise Bloqueado(api, ["PUNISH::página de punição (HTML) no lugar do JSON"])
    m = _RE_JSONP.match(texto)
    try:
        return json.loads(m.group(1) if m else texto)
    except (json.JSONDecodeError, ValueError) as e:
        raise MtopErro(f"{api}: corpo não é JSON ({e}): {texto[:120]!r}") from e


def _checar(api: str, corpo: dict) -> dict:
    ret = corpo.get("ret") or []
    if any(r.startswith("SUCCESS") for r in ret):
        return corpo
    if any("RGV587" in r or "FAIL_SYS_USER_VALIDATE" in r for r in ret):
        raise Bloqueado(api, ret)
    raise MtopErro(f"{api}: {ret}")


def chamar(page: Page, api: str, data: dict, timeout_ms: int = 20_000) -> dict:
    """Executa a API pelo SDK da página e devolve o corpo completo (`ret` + `data`)."""
    try:
        with page.expect_response(lambda r: f"/h5/{api}/" in r.url, timeout=timeout_ms + 10_000) as info:
            page.evaluate(_JS_CHAMAR, [api, data, EXT_QUERYS.get(api, {}), timeout_ms])
    except PlaywrightTimeout as e:
        raise MtopErro(f"{api}: sem resposta de rede ({e})") from e
    return _checar(api, _corpo(api, info.value.text()))


def abrir_pagina_base(page: Page, user_id: str) -> None:
    """Uma página do Goofish carregada é o único pré-requisito do SDK. Dali saem
    todas as chamadas da varredura."""
    page.goto(URL_PESSOAL.format(user_id=user_id), wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_function("() => window.lib && window.lib.mtop && window.lib.mtop.request", timeout=30_000)


GRUPO_A_VENDA = "在售"


def lista_vendedor(page: Page, user_id: str, pagina: int, por_pagina: int = 20,
                   grupo_id: int | None = None) -> dict:
    """Lista da loja. Sem `grupo_id` é a aba "todos" (com vendidos) e a resposta
    traz `itemGroupList`, de onde sai o id do grupo "在售". Com `grupo_id`, é a
    aba "em venda": só anúncio comprável (replicado de scripts/fase0i_aba_em_venda.py)."""
    data: dict = {"userId": user_id, "pageNumber": pagina, "pageSize": por_pagina}
    if grupo_id is None:
        data["needGroupInfo"] = True
    else:
        data.update({"needGroupInfo": False, "groupId": grupo_id, "groupName": GRUPO_A_VENDA, "defaultGroup": True})
    return chamar(page, API_LISTA, data)


def abrir_pagina_busca(page: Page, termo: str, timeout_ms: int = 30_000) -> dict:
    """Página base quando não há loja: abre a busca (com `spm`) e devolve a página 1
    que a própria página pede ao carregar. Pedir a mesma página de novo pelo SDK
    seria uma requisição idêntica segundos depois da primeira."""
    try:
        with page.expect_response(lambda r: f"/h5/{API_BUSCA}/" in r.url, timeout=timeout_ms) as info:
            page.goto(URL_BUSCA.format(termo=quote(termo)), wait_until="domcontentloaded", timeout=60_000)
    except PlaywrightTimeout as e:
        raise MtopErro(f"{API_BUSCA}: a página de busca não pediu resultados ({e})") from e
    corpo = _checar(API_BUSCA, _corpo(API_BUSCA, info.value.text()))
    page.wait_for_function("() => window.lib && window.lib.mtop && window.lib.mtop.request", timeout=30_000)
    return corpo


def perfil_vendedor(page: Page, user_id: str) -> dict:
    return chamar(page, API_PERFIL, {"userId": user_id})


def busca(page: Page, keyword: str, pagina: int, por_pagina: int = 30) -> dict:
    data = {
        "pageNumber": pagina, "keyword": keyword, "fromFilter": False, "rowsPerPage": por_pagina,
        "sortValue": "", "sortField": "", "customDistance": "", "gps": "", "propValueStr": {},
        "customGps": "", "searchReqFromPage": "pcSearch", "extraFilterValue": "{}", "userPositionJson": "{}",
    }
    return chamar(page, API_BUSCA, data)

