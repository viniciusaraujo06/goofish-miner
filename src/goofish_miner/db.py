"""SQLite append-only. Toda observação é INSERT; `snapshot` nunca recebe UPDATE.

Regra de repetição: reobservação sem mudança não gera linha; reobservação com
mudança gera linha nova, sempre. "Mudança" é medida por `conteudo_hash`,
resumo de (item, título, preço, estado).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
  id            INTEGER PRIMARY KEY,
  item_id       TEXT    NOT NULL,
  visto_em      TEXT    NOT NULL,
  preco_cny     REAL    NOT NULL,
  titulo        TEXT    NOT NULL,
  vendedor_id   TEXT,               -- a busca não dá o id numérico; a loja dá
  fonte         TEXT    NOT NULL,   -- 'loja:<user_id>' | 'busca:<termo>'
  conteudo_hash TEXT    NOT NULL,
  raw           TEXT    NOT NULL    -- JSON da resposta, sem telemetria
);
CREATE INDEX IF NOT EXISTS idx_item  ON snapshot(item_id, id);
CREATE INDEX IF NOT EXISTS idx_fonte ON snapshot(fonte, visto_em);

-- Perfil da loja, também append-only: o histórico mostra se ela está ativa.
CREATE TABLE IF NOT EXISTS vendedor_snapshot (
  id            INTEGER PRIMARY KEY,
  vendedor_id   TEXT    NOT NULL,
  visto_em      TEXT    NOT NULL,
  nome          TEXT,
  itens_total   INTEGER,           -- aba "todos" da loja (inclui vendidos)
  itens_a_venda INTEGER,           -- grupo "在售" da listagem: o nº real de compráveis
  avaliacao     REAL,              -- % de avaliações positivas
  avaliacoes    INTEGER,
  nivel         TEXT,              -- L1..L6
  conteudo_hash TEXT    NOT NULL,
  raw           TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vendedor ON vendedor_snapshot(vendedor_id, id);

-- Primeira leitura de cada fonte. O que aparece nela forma a linha de base e
-- não vira aviso: acrescentar uma busca não pode despejar 60 "novos" de uma vez.
CREATE TABLE IF NOT EXISTS fonte (
  chave        TEXT PRIMARY KEY,
  primeira_vez TEXT NOT NULL
);

-- O que já foi tratado. 'base' = visto na estreia da fonte, sem mensagem.
-- Uma linha por (item, snapshot) avisado; queda nova = snapshot novo = linha nova.
CREATE TABLE IF NOT EXISTS aviso (
  id                  INTEGER PRIMARY KEY,
  item_id             TEXT    NOT NULL,   -- ou 'varredura:<id>' nos alertas
  snapshot_id         INTEGER,
  tipo                TEXT    NOT NULL,   -- 'novo' | 'queda' | 'base' | 'alerta'
  criado_em           TEXT    NOT NULL,
  telegram_message_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_aviso_item ON aviso(item_id, id);

-- Chave-valor: offset do bot, freio de emergência, janela e frequência ajustadas pelo bot.
CREATE TABLE IF NOT EXISTS estado (
  chave TEXT PRIMARY KEY,
  valor TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS varredura (
  id           INTEGER PRIMARY KEY,
  iniciada_em  TEXT NOT NULL,
  terminada_em TEXT,
  status       TEXT NOT NULL,   -- rodando | ok | parcial | bloqueada | erro | parada | interrompida
  snapshots    INTEGER NOT NULL DEFAULT 0,
  itens_novos  INTEGER NOT NULL DEFAULT 0,
  detalhe      TEXT
);
"""


def _sha(proj: dict) -> str:
    return hashlib.sha1(json.dumps(proj, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def hash_conteudo(item_id: str, titulo: str, preco_cny: float, estado: int) -> str:
    """Projeção útil do anúncio: exatamente quatro campos, chaves ordenadas."""
    return _sha({"estado": int(estado), "item_id": str(item_id), "preco_cny": round(float(preco_cny), 2), "titulo": titulo})


@dataclass(frozen=True)
class Snapshot:
    item_id: str
    preco_cny: float
    titulo: str
    vendedor_id: str | None
    fonte: str
    raw: dict
    estado: int = 0   # itemStatus: 0 à venda, 1 vendido; a busca não informa → 0

    @property
    def conteudo_hash(self) -> str:
        return hash_conteudo(self.item_id, self.titulo, self.preco_cny, self.estado)


@dataclass(frozen=True)
class VendedorSnapshot:
    vendedor_id: str
    nome: str | None
    itens_total: int | None
    itens_a_venda: int | None
    avaliacao: float | None
    avaliacoes: int | None
    nivel: str | None
    raw: dict

    @property
    def conteudo_hash(self) -> str:
        return _sha({k: getattr(self, k) for k in ("vendedor_id", "nome", "itens_total", "itens_a_venda",
                                                   "avaliacao", "avaliacoes", "nivel")})


def agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conectar(caminho: Path) -> sqlite3.Connection:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(caminho)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


# ------------------------------------------------------------------ snapshot

def item_conhecido(con: sqlite3.Connection, item_id: str) -> bool:
    return con.execute("SELECT 1 FROM snapshot WHERE item_id = ? LIMIT 1", (item_id,)).fetchone() is not None


def ultimo_hash(con: sqlite3.Connection, item_id: str) -> str | None:
    row = con.execute("SELECT conteudo_hash FROM snapshot WHERE item_id = ? ORDER BY id DESC LIMIT 1",
                      (item_id,)).fetchone()
    return row["conteudo_hash"] if row else None


def inserir_snapshot(con: sqlite3.Connection, s: Snapshot, visto_em: str | None = None) -> int:
    """INSERT incondicional. A coleta usa `gravar_se_mudou`; este é pros testes e pra quem sabe o que faz."""
    cur = con.execute(
        "INSERT INTO snapshot (item_id, visto_em, preco_cny, titulo, vendedor_id, fonte, conteudo_hash, raw)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (s.item_id, visto_em or agora(), s.preco_cny, s.titulo, s.vendedor_id, s.fonte,
         s.conteudo_hash, json.dumps(s.raw, ensure_ascii=False)),
    )
    con.commit()
    return cur.lastrowid


def gravar_se_mudou(con: sqlite3.Connection, s: Snapshot, visto_em: str | None = None) -> tuple[str, int | None]:
    """('novo' | 'mudou', id da linha) ou ('igual', None). Só grava nos dois primeiros."""
    anterior = ultimo_hash(con, s.item_id)
    if anterior == s.conteudo_hash:
        return "igual", None
    sid = inserir_snapshot(con, s, visto_em)
    return ("novo" if anterior is None else "mudou"), sid


# ------------------------------------------------------------------ vendedor

def gravar_vendedor_se_mudou(con: sqlite3.Connection, v: VendedorSnapshot, visto_em: str | None = None) -> str:
    row = con.execute("SELECT conteudo_hash FROM vendedor_snapshot WHERE vendedor_id = ? ORDER BY id DESC LIMIT 1",
                      (v.vendedor_id,)).fetchone()
    anterior = row["conteudo_hash"] if row else None
    if anterior == v.conteudo_hash:
        return "igual"
    con.execute(
        "INSERT INTO vendedor_snapshot (vendedor_id, visto_em, nome, itens_total, itens_a_venda, avaliacao,"
        " avaliacoes, nivel, conteudo_hash, raw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (v.vendedor_id, visto_em or agora(), v.nome, v.itens_total, v.itens_a_venda, v.avaliacao, v.avaliacoes,
         v.nivel, v.conteudo_hash, json.dumps(v.raw, ensure_ascii=False)),
    )
    con.commit()
    return "novo" if anterior is None else "mudou"


# ------------------------------------------------------------------ fontes e avisos

def fonte_conhecida(con: sqlite3.Connection, chave: str) -> bool:
    return con.execute("SELECT 1 FROM fonte WHERE chave = ?", (chave,)).fetchone() is not None


def registrar_fonte(con: sqlite3.Connection, chave: str) -> None:
    con.execute("INSERT OR IGNORE INTO fonte (chave, primeira_vez) VALUES (?, ?)", (chave, agora()))
    con.commit()


def registrar_aviso(con: sqlite3.Connection, item_id: str, snapshot_id: int | None, tipo: str,
                    message_id: int | None = None) -> None:
    con.execute("INSERT INTO aviso (item_id, snapshot_id, tipo, criado_em, telegram_message_id) VALUES (?, ?, ?, ?, ?)",
                (item_id, snapshot_id, tipo, agora(), message_id))
    con.commit()


# ------------------------------------------------------------------ estado e varredura

def ler_flag(con: sqlite3.Connection, chave: str) -> str | None:
    r = con.execute("SELECT valor FROM estado WHERE chave = ?", (chave,)).fetchone()
    return r["valor"] if r else None


def gravar_flag(con: sqlite3.Connection, chave: str, valor: str) -> None:
    con.execute("INSERT OR REPLACE INTO estado (chave, valor) VALUES (?, ?)", (chave, valor))
    con.commit()


def apagar_flag(con: sqlite3.Connection, chave: str) -> None:
    con.execute("DELETE FROM estado WHERE chave = ?", (chave,))
    con.commit()


def iniciar_varredura(con: sqlite3.Connection) -> int:
    cur = con.execute("INSERT INTO varredura (iniciada_em, status) VALUES (?, 'rodando')", (agora(),))
    con.commit()
    return cur.lastrowid


def finalizar_varredura(con: sqlite3.Connection, id_: int, status: str, snapshots: int, itens_novos: int,
                        detalhe: str | None = None) -> None:
    con.execute(
        "UPDATE varredura SET terminada_em = ?, status = ?, snapshots = ?, itens_novos = ?, detalhe = ? WHERE id = ?",
        (agora(), status, snapshots, itens_novos, detalhe, id_),
    )
    con.commit()
