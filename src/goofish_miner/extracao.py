"""Normaliza as respostas do Goofish (lista da loja, busca, perfil) para
`Snapshot` / `VendedorSnapshot`.

Aqui também se decide o que do JSON cru vai pro banco: fica o que descreve o
anúncio, o preço, o vendedor ou o estado; sai o que descreve como a interface
rastreia o usuário.
"""

from __future__ import annotations

import copy
import json
import re

from .db import Snapshot, VendedorSnapshot

# ---- telemetria removida do `raw` antes de gravar. Um lugar só pra conferir
#      quando a Alibaba mudar o formato.
CHAVES_REMOVER_BUSCA = ("clickParam", "targetUrl")
CHAVES_REMOVER_EXCONTENT = ("trackParams", "serviceUtParams", "fishTagCustomParam", "photoSearchUrl")
CHAVES_REMOVER_CARD = ("trackParams",)
CHAVES_REMOVER_LABEL = ("serviceUtParams", "trackParams", "unShowLabelUtParams")
# Campos com significado que só existem em `clickParam.args`; preservados em `raw["_busca"]`.
CAMPOS_PRESERVAR_CLICK = ("publishTime", "wantNum", "seller_id")

_RE_PRECO = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(万|千)?")
_MULT = {"万": 10_000, "千": 1_000, None: 1}


class PrecoInvalido(ValueError):
    """Preço que não dá pra converter. Quem chamou descarta o anúncio e loga o texto cru."""

    def __init__(self, item_id: str, texto):
        super().__init__(f"item {item_id}: preço inválido {texto!r}")
        self.item_id = item_id
        self.texto = texto


def _preco(valor) -> float | None:
    """'338' → 338.0; '¥1,299' → 1299.0; '4.35万' → 43500.0; sem número ou zero → None."""
    if valor is None:
        return None
    s = str(valor).replace("¥", "").replace("￥", "").strip()
    m = _RE_PRECO.search(s)
    if not m:
        return None
    v = float(m.group(1).replace(",", "")) * _MULT[m.group(2)]
    return v if v > 0 else None


def _preco_ou_erro(valor, item_id: str) -> float:
    p = _preco(valor)
    if p is None:
        raise PrecoInvalido(item_id, valor)
    return p


def _etiquetas(label_data: dict | None) -> list[str]:
    """Achata `labelData.r*.tagList[].data.content` (lista e busca usam a mesma forma)."""
    out: list[str] = []
    for linha in (label_data or {}).values():
        for tag in (linha or {}).get("tagList", []):
            c = (tag.get("data") or {}).get("content")
            if c:
                out.append(str(c))
    return out


def _sem(d: dict, chaves: tuple[str, ...]) -> None:
    for k in chaves:
        d.pop(k, None)


# ---------------------------------------------------------------- foto

def foto_principal(raw: dict) -> str | None:
    """URL da foto principal, de um `raw` de lista (`cardData`) ou de busca (`exContent`)."""
    cd = raw.get("cardData")
    if cd:
        infos = (cd.get("detailParams") or {}).get("imageInfos")
        if isinstance(infos, str):   # vem serializado em string
            try:
                infos = json.loads(infos)
            except json.JSONDecodeError:
                infos = []
        for i in infos or []:
            if isinstance(i, dict) and i.get("major") and i.get("url"):
                return i["url"]
        return (cd.get("picInfo") or {}).get("picUrl") or (cd.get("detailParams") or {}).get("picUrl") or None
    return (raw.get("exContent") or {}).get("picUrl") or None


# ---------------------------------------------------------------- lista da loja

def de_cartao_lista(card: dict, vendedor_id: str) -> Snapshot:
    cd = card.get("cardData", card)
    dp = cd.get("detailParams") or {}
    titulo = cd.get("title") or dp.get("title") or ""
    item_id = str(cd.get("id") or dp.get("itemId"))
    preco = _preco_ou_erro((cd.get("priceInfo") or {}).get("price") or dp.get("soldPrice"), item_id)
    estado = int(cd.get("itemStatus") or 0)

    raw = copy.deepcopy(card)
    rcd = raw.get("cardData", raw)
    raw["_etiquetas"] = _etiquetas((rcd.get("itemLabelDataVO") or {}).get("labelData"))
    _sem(rcd, CHAVES_REMOVER_CARD)
    if isinstance(rcd.get("itemLabelDataVO"), dict):
        _sem(rcd["itemLabelDataVO"], CHAVES_REMOVER_LABEL)

    return Snapshot(item_id=item_id, preco_cny=preco, titulo=titulo, vendedor_id=vendedor_id,
                    fonte=f"loja:{vendedor_id}", raw=raw, estado=estado)


# ---------------------------------------------------------------- busca por termo

def resultados_da_busca(data: dict) -> list[dict]:
    """Só os cartões de item; a busca mistura outros tipos de card."""
    out = []
    for r in data.get("resultList", []) or []:
        item = ((r.get("data") or {}).get("item") or {}).get("main")
        if item and (item.get("exContent") or {}).get("itemId"):
            out.append(item)
    return out


def de_resultado_busca(main: dict, termo: str) -> Snapshot:
    ex = main.get("exContent") or {}
    args = (main.get("clickParam") or {}).get("args") or {}
    titulo = ex.get("title") or (ex.get("detailParams") or {}).get("title") or ""
    item_id = str(ex.get("itemId") or args.get("id"))
    preco = _preco_ou_erro((ex.get("detailParams") or {}).get("soldPrice") or args.get("price"), item_id)

    raw = copy.deepcopy(main)
    raw["_busca"] = {k: args.get(k) for k in CAMPOS_PRESERVAR_CLICK}
    raw["_etiquetas"] = _etiquetas(ex.get("fishTags"))
    _sem(raw, CHAVES_REMOVER_BUSCA)
    if isinstance(raw.get("exContent"), dict):
        _sem(raw["exContent"], CHAVES_REMOVER_EXCONTENT)

    return Snapshot(
        item_id=item_id, preco_cny=preco, titulo=titulo,
        vendedor_id=None,  # `seller_id` da busca é criptografado, não é o userId numérico
        fonte=f"busca:{termo}", raw=raw, estado=0,  # a busca não informa itemStatus
    )


# ---------------------------------------------------------------- perfil da loja

def _int(v) -> int | None:
    try:
        return int(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _pct(v) -> float | None:
    try:
        return float(str(v).replace("%", ""))
    except (TypeError, ValueError):
        return None


def grupos_da_lista(data: dict) -> dict[str, dict]:
    """`itemGroupList` da listagem: {nome: {"id": groupId, "n": itemNumber}}.
    O grupo `在售` é a aba "em venda"; seu `n` é o nº real de anúncios compráveis."""
    out: dict[str, dict] = {}
    for g in data.get("itemGroupList") or []:
        nome, gid, n = g.get("groupName"), _int(g.get("groupId")), _int(g.get("itemNumber"))
        if nome and gid is not None:
            out[str(nome)] = {"id": gid, "n": n}
    return out


def de_perfil(data: dict, vendedor_id: str, itens_a_venda: int | None = None) -> VendedorSnapshot:
    mod = data.get("module") or {}
    shop, tabs, base = mod.get("shop") or {}, mod.get("tabs") or {}, mod.get("base") or {}
    return VendedorSnapshot(
        vendedor_id=vendedor_id,
        nome=base.get("displayName"),
        itens_total=_int((tabs.get("item") or {}).get("number")),  # aba "todos", inclui vendidos
        itens_a_venda=itens_a_venda,
        avaliacao=_pct(shop.get("praiseRatio")),
        avaliacoes=_int(shop.get("reviewNum")),
        nivel=shop.get("level"),
        raw=data,
    )
