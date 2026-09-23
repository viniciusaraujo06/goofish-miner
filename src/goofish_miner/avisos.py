"""O que merece aviso, decidido só pelo histórico do banco.

- **novo**: anúncio que nunca foi tratado (nem avisado, nem linha de base).
- **queda**: o último preço está pelo menos `queda_min_pct` abaixo do preço de
  referência, que é o do último aviso (ou da linha de base) do anúncio; sem
  nenhum, o da primeira vez que ele foi visto. Avisar a queda muda a
  referência, então a mesma queda nunca é avisada duas vezes e uma queda
  seguinte é medida a partir do novo patamar.

Só o último snapshot de cada anúncio conta, e só se tiver sido gravado dentro
de `max_idade_horas`.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from . import mtop
from .config import Config

TIPOS_TRATADOS = ("novo", "queda", "base")


@dataclass(frozen=True)
class Aviso:
    tipo: str                    # 'novo' | 'queda'
    item_id: str
    snapshot_id: int
    titulo: str
    preco: float
    preco_anterior: float | None
    fonte: str
    visto_em: str
    raw: dict

    @property
    def url(self) -> str:
        return mtop.URL_ITEM.format(item_id=self.item_id)

    @property
    def queda_pct(self) -> float | None:
        if not self.preco_anterior:
            return None
        return (self.preco_anterior - self.preco) / self.preco_anterior * 100


def _limite(cfg: Config, agora: datetime | None) -> str:
    base = agora or datetime.now(timezone.utc)
    return (base - timedelta(hours=cfg.notif_max_idade_horas)).isoformat(timespec="seconds")


def pendentes(con: sqlite3.Connection, cfg: Config, agora: datetime | None = None) -> list[Aviso]:
    """Avisos a enviar, do mais recente pro mais antigo."""
    ultimos = con.execute(
        "SELECT s.* FROM snapshot s JOIN (SELECT item_id, MAX(id) mid FROM snapshot GROUP BY item_id) u ON s.id = u.mid"
        " WHERE s.visto_em >= ? ORDER BY s.id DESC", (_limite(cfg, agora),)).fetchall()
    out: list[Aviso] = []
    for s in ultimos:
        tratado = con.execute(
            "SELECT a.snapshot_id, p.preco_cny FROM aviso a LEFT JOIN snapshot p ON p.id = a.snapshot_id"
            f" WHERE a.item_id = ? AND a.tipo IN ({','.join('?' * len(TIPOS_TRATADOS))}) ORDER BY a.id DESC LIMIT 1",
            (s["item_id"], *TIPOS_TRATADOS)).fetchone()
        if tratado is None and cfg.notif_novos:
            out.append(_aviso("novo", s, None))
            continue
        if tratado is not None and tratado["snapshot_id"] == s["id"]:
            continue                                   # este preço já foi tratado
        if not cfg.notif_queda_min_pct:
            continue
        if tratado is not None and tratado["preco_cny"] is not None:
            referencia = float(tratado["preco_cny"])
        else:
            primeiro = con.execute("SELECT preco_cny FROM snapshot WHERE item_id = ? ORDER BY id LIMIT 1",
                                   (s["item_id"],)).fetchone()
            referencia = float(primeiro["preco_cny"])
        if s["preco_cny"] <= referencia * (1 - cfg.notif_queda_min_pct / 100):
            out.append(_aviso("queda", s, referencia))
    return out


def _aviso(tipo: str, s: sqlite3.Row, anterior: float | None) -> Aviso:
    return Aviso(tipo=tipo, item_id=s["item_id"], snapshot_id=s["id"], titulo=s["titulo"], preco=float(s["preco_cny"]),
                 preco_anterior=anterior, fonte=s["fonte"], visto_em=s["visto_em"], raw=json.loads(s["raw"]))
