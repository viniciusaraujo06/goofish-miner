"""Leitura dos TOMLs de `config/`."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent.parent


@dataclass(frozen=True)
class Filtro:
    """Faixa de preço e termos do título. Termos são comparados em minúsculas."""
    preco_min: float = 0.0
    preco_max: float = 0.0          # 0 = sem teto
    incluir: tuple[str, ...] = ()   # pelo menos um no título; vazio = qualquer título
    excluir: tuple[str, ...] = ()   # nenhum no título

    def recusa(self, titulo: str, preco: float) -> str | None:
        """Motivo da recusa, ou None se o anúncio passa."""
        t = (titulo or "").lower()
        if self.preco_min and preco < self.preco_min:
            return "preco_min"
        if self.preco_max and preco > self.preco_max:
            return "preco_max"
        if self.incluir and not any(termo in t for termo in self.incluir):
            return "incluir"
        if any(termo in t for termo in self.excluir):
            return "excluir"
        return None


def _filtro(d: dict | None, base: Filtro) -> Filtro:
    """Filtro da fonte sobre o padrão, campo a campo: o que a fonte não diz vem do padrão."""
    if not d:
        return base
    desconhecidas = set(d) - {f.name for f in fields(Filtro)}
    if desconhecidas:
        raise ValueError(f"chave desconhecida em [filtro]: {sorted(desconhecidas)}")
    mudancas: dict = {}
    for k in ("preco_min", "preco_max"):
        if k in d:
            mudancas[k] = float(d[k])
    for k in ("incluir", "excluir"):
        if k in d:
            mudancas[k] = tuple(str(t).lower() for t in d[k])
    return replace(base, **mudancas)


@dataclass(frozen=True)
class Fonte:
    tipo: str        # "loja" | "busca"
    nome: str        # como aparece nos avisos
    alvo: str        # userId da loja ou termo da busca
    paginas: int
    filtro: Filtro = field(default_factory=Filtro)

    @property
    def chave(self) -> str:
        """Identidade da fonte no banco (`snapshot.fonte`, tabela `fonte`)."""
        return f"{self.tipo}:{self.alvo}"


@dataclass(frozen=True)
class Config:
    raiz: Path
    fontes: list[Fonte]
    janela_inicio: int
    janela_fim: int
    intervalo_min_minutos: int
    apos_bloqueio_horas: float
    pausa_min: float
    pausa_max: float
    canal: str
    headless: bool
    notif_ativo: bool = False
    notif_novos: bool = True
    notif_queda_min_pct: float = 10.0
    notif_max_por_rodada: int = 10
    notif_max_idade_horas: float = 48.0
    foto_sufixo_cdn: str = ""

    @property
    def db(self) -> Path:
        return self.raiz / "data" / "miner.db"

    @property
    def lojas(self) -> list[Fonte]:
        return [f for f in self.fontes if f.tipo == "loja"]

    @property
    def buscas(self) -> list[Fonte]:
        return [f for f in self.fontes if f.tipo == "busca"]

    def fonte(self, chave: str) -> Fonte | None:
        return next((f for f in self.fontes if f.chave == chave), None)


def _ler(caminho: Path) -> dict:
    with caminho.open("rb") as f:
        return tomllib.load(f)


def carregar(raiz: Path = RAIZ) -> Config:
    pasta = raiz / "config"
    fts = _ler(pasta / "fontes.toml")
    coleta = _ler(pasta / "coleta.toml")
    pag = coleta.get("paginas", {})
    padrao = _filtro(fts.get("filtro"), Filtro())

    fontes: list[Fonte] = []
    for l in fts.get("loja", []):
        user_id = str(l["user_id"])
        if not user_id.isdigit():   # string sempre: 10 a 13 dígitos, e int vira bug de precisão
            raise ValueError(f"user_id inválido em fontes.toml: {user_id!r}")
        fontes.append(Fonte("loja", str(l.get("nome") or user_id), user_id,
                            int(l.get("paginas", pag.get("loja", 3))), _filtro(l.get("filtro"), padrao)))
    for b in fts.get("busca", []):
        termo = str(b["termo"]).strip()
        if not termo:
            raise ValueError("busca com termo vazio em fontes.toml")
        fontes.append(Fonte("busca", str(b.get("nome") or termo), termo,
                            int(b.get("paginas", pag.get("busca", 2))), _filtro(b.get("filtro"), padrao)))
    chaves = [f.chave for f in fontes]
    repetidas = {c for c in chaves if chaves.count(c) > 1}
    if repetidas:
        raise ValueError(f"fonte repetida em fontes.toml: {sorted(repetidas)}")

    janela, notif = coleta["janela"], coleta.get("notificacao", {})
    return Config(
        raiz=raiz,
        fontes=fontes,
        janela_inicio=int(janela["inicio"]),
        janela_fim=int(janela["fim"]),
        intervalo_min_minutos=int(janela.get("intervalo_min_minutos", 50)),
        apos_bloqueio_horas=float(janela.get("apos_bloqueio_esperar_horas", 3)),
        pausa_min=float(coleta["pausa"]["min"]),
        pausa_max=float(coleta["pausa"]["max"]),
        canal=str(coleta["navegador"]["canal"]),
        headless=bool(coleta["navegador"]["headless"]),
        notif_ativo=bool(notif.get("ativo", False)),
        notif_novos=bool(notif.get("novos", True)),
        notif_queda_min_pct=float(notif.get("queda_min_pct", 10)),
        notif_max_por_rodada=int(notif.get("max_por_rodada", 10)),
        notif_max_idade_horas=float(notif.get("max_idade_horas", 48)),
        foto_sufixo_cdn=str(notif.get("foto_sufixo_cdn", "")),
    )
