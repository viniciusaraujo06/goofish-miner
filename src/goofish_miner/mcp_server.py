"""Servidor MCP (stdio) sobre `data/miner.db`, pra análise no Claude Code.

Tools de domínio, não genéricas (nada de `query_sql`). Só leitura, e nunca faz
rede: coletar é trabalho do coletor; aqui é análise do que já está no banco.

Sobre o tempo: `snapshot.visto_em` é quando o anúncio entrou ou mudou (título,
preço, estado). Reobservação idêntica não grava linha, então "última mudança"
não quer dizer "última vez que estava no ar".

O `.mcp.json` da raiz registra o servidor quando o Claude Code abre a pasta. Em outro lugar:
    claude mcp add --transport stdio goofish-miner -- uv --directory <raiz do repo> run python -m goofish_miner.mcp_server

Teste rápido sem Claude:
    uv run python -m goofish_miner.mcp_server --teste
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta, timezone

from . import db, mtop
from .config import Config, carregar

_RESUMO = """
WITH u AS (SELECT item_id, MIN(id) pid, MAX(id) uid, COUNT(*) linhas, MAX(preco_cny) maximo, MIN(preco_cny) minimo
           FROM snapshot GROUP BY item_id)
SELECT u.item_id, p.visto_em AS primeira_vez, s.visto_em AS ultima_mudanca, s.titulo, s.preco_cny AS preco,
       p.preco_cny AS preco_inicial, u.maximo AS preco_maximo, u.minimo AS preco_minimo, u.linhas,
       s.fonte, s.vendedor_id
FROM u JOIN snapshot s ON s.id = u.uid JOIN snapshot p ON p.id = u.pid
"""


def _desde(horas: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=horas)).isoformat(timespec="seconds")


def _url(item_id: str) -> str:
    return mtop.URL_ITEM.format(item_id=item_id)


def _resumos(con: sqlite3.Connection, where: str = "", params: tuple = (), ordem: str = "s.id DESC",
             limite: int | None = None) -> list[dict]:
    sql = _RESUMO + (f" WHERE {where}" if where else "") + f" ORDER BY {ordem}" + (" LIMIT ?" if limite else "")
    rows = con.execute(sql, (*params, limite) if limite else params).fetchall()
    return [{**dict(r), "titulo": r["titulo"][:80], "url": _url(r["item_id"])} for r in rows]


_TERMO = "instr(lower(s.titulo), lower(?)) > 0"


def _tendencia(anteriores: list[float], recentes: list[float], tolerancia: float = 0.05) -> str:
    if not anteriores or not recentes:
        return "insuficiente"
    a, r = statistics.median(anteriores), statistics.median(recentes)
    if a == 0:
        return "insuficiente"
    var = (r - a) / a
    return "subindo" if var > tolerancia else "caindo" if var < -tolerancia else "estável"


# ------------------------------------------------------------------ tools (funções puras, testáveis)

def buscar(con: sqlite3.Connection, termo: str, dias: float = 30, limite: int = 20) -> dict:
    termo = (termo or "").strip()
    if not termo:
        return {"erro": "termo vazio"}
    itens = _resumos(con, f"{_TERMO} AND s.visto_em >= ?", (termo, _desde(dias * 24)), "s.preco_cny", limite)
    return {"termo": termo, "dias": dias, "anuncios": len(itens), "mais_baratos_primeiro": itens}


def historico_preco(con: sqlite3.Connection, termo: str, dias: float = 90) -> dict:
    """Preço atual de cada anúncio distinto cujo título contém o termo, na ordem em que apareceram."""
    termo = (termo or "").strip()
    if not termo:
        return {"erro": "termo vazio"}
    itens = _resumos(con, f"{_TERMO} AND s.visto_em >= ?", (termo, _desde(dias * 24)), "p.id")
    precos = [d["preco"] for d in itens]
    out: dict = {"termo": termo, "dias": dias, "anuncios": len(itens)}
    if precos:
        meio = len(precos) // 2
        out.update(mediana=statistics.median(precos), minimo=min(precos), maximo=max(precos),
                   quartis=statistics.quantiles(precos, n=4) if len(precos) >= 4 else None,
                   tendencia=_tendencia(precos[:meio], precos[meio:]) if len(precos) >= 4 else "insuficiente",
                   mais_recentes=itens[-10:][::-1])
    return out


def quedas_de_preco(con: sqlite3.Connection, dias: float = 7, min_pct: float = 5, limite: int = 20) -> list[dict]:
    """Anúncios cujo preço atual está pelo menos `min_pct` abaixo do maior preço já visto, com a queda recente."""
    fator = 1 - min_pct / 100
    itens = _resumos(con, "s.visto_em >= ? AND u.linhas > 1 AND s.preco_cny <= u.maximo * ?", (_desde(dias * 24), fator),
                     "(u.maximo - s.preco_cny) / u.maximo DESC", limite)
    for d in itens:
        d["queda_pct"] = round((d["preco_maximo"] - d["preco"]) / d["preco_maximo"] * 100, 1)
    return itens


def novidades(con: sqlite3.Connection, horas: float = 24, fonte: str | None = None, limite: int = 20) -> list[dict]:
    """Anúncios vistos pela primeira vez nas últimas `horas`. `fonte` filtra pela chave ou parte dela."""
    where, params = "p.visto_em >= ?", (_desde(horas),)
    if fonte:
        where += " AND instr(p.fonte, ?) > 0"
        params = (*params, fonte)
    return _resumos(con, where, params, "p.id DESC", limite)


def anuncio(con: sqlite3.Connection, item_id: str) -> dict:
    item_id = str(item_id)
    linhas = con.execute("SELECT id, visto_em, preco_cny, titulo, fonte, vendedor_id, raw FROM snapshot"
                         " WHERE item_id = ? ORDER BY id", (item_id,)).fetchall()
    if not linhas:
        return {"erro": f"anúncio {item_id} não está no banco"}
    ult = linhas[-1]
    raw = json.loads(ult["raw"])
    vendedor_id = next((r["vendedor_id"] for r in reversed(linhas) if r["vendedor_id"]), None)
    out = {
        "item_id": item_id, "url": _url(item_id), "titulo": ult["titulo"], "preco": ult["preco_cny"],
        "etiquetas": raw.get("_etiquetas") or [], "publicado_em_ms": (raw.get("_busca") or {}).get("publishTime"),
        "interessados": (raw.get("_busca") or {}).get("wantNum"),
        "historico": [{"em": r["visto_em"], "preco": r["preco_cny"], "titulo": r["titulo"][:80], "fonte": r["fonte"]}
                      for r in linhas],
        "fontes": sorted({r["fonte"] for r in linhas}),
        "avisos": [dict(a) for a in con.execute("SELECT tipo, criado_em FROM aviso WHERE item_id = ? ORDER BY id", (item_id,))],
        "vendedor_id": vendedor_id,
    }
    if vendedor_id:
        v = con.execute("SELECT nome, nivel, avaliacao, itens_a_venda FROM vendedor_snapshot WHERE vendedor_id = ?"
                        " ORDER BY id DESC LIMIT 1", (vendedor_id,)).fetchone()
        out["vendedor"] = dict(v) if v else None
    return out


def vendedor(con: sqlite3.Connection, cfg: Config, vendedor_id: str) -> dict:
    vendedor_id = str(vendedor_id)
    perfis = con.execute("SELECT * FROM vendedor_snapshot WHERE vendedor_id = ? ORDER BY id", (vendedor_id,)).fetchall()
    # qualquer linha do vendedor conta: se o anúncio reapareceu numa busca, a última linha vem sem vendedor
    itens = _resumos(con, "u.item_id IN (SELECT item_id FROM snapshot WHERE vendedor_id = ?)", (vendedor_id,), "p.id DESC")
    if not perfis and not itens:
        return {"erro": f"vendedor {vendedor_id} não está no banco"}
    precos = [d["preco"] for d in itens]
    f = cfg.fonte(f"loja:{vendedor_id}")
    ult = perfis[-1] if perfis else None
    return {
        "vendedor_id": vendedor_id, "loja_acompanhada": f.nome if f else None,
        "nome": ult["nome"] if ult else None, "nivel": ult["nivel"] if ult else None,
        "avaliacao_pct": ult["avaliacao"] if ult else None, "avaliacoes": ult["avaliacoes"] if ult else None,
        "itens_a_venda": ult["itens_a_venda"] if ult else None, "itens_total": ult["itens_total"] if ult else None,
        "perfil_ao_longo_do_tempo": [{"em": p["visto_em"], "itens_a_venda": p["itens_a_venda"], "avaliacoes": p["avaliacoes"]}
                                     for p in perfis[-10:]],
        "anuncios_observados": len(itens),
        "faixa_preco": {"min": min(precos), "mediana": statistics.median(precos), "max": max(precos)} if precos else None,
        "ultimos_anuncios": itens[:10],
    }


def visao_geral(con: sqlite3.Connection, cfg: Config) -> dict:
    v = con.execute("SELECT * FROM varredura ORDER BY id DESC LIMIT 1").fetchone()
    desde = _desde(24)
    return {
        "ultima_varredura": dict(v) if v else None,
        "coleta_pausada": db.ler_flag(con, "coleta_pausada") == "1",
        "anuncios_no_banco": con.execute("SELECT COUNT(DISTINCT item_id) FROM snapshot").fetchone()[0],
        "fontes": [{"chave": f.chave, "nome": f.nome, "paginas": f.paginas,
                    "lida": db.fonte_conhecida(con, f.chave),
                    "anuncios": con.execute("SELECT COUNT(DISTINCT item_id) FROM snapshot WHERE fonte = ?", (f.chave,)).fetchone()[0]}
                   for f in cfg.fontes],
        "avisos_24h": {r["tipo"]: r["n"] for r in con.execute(
            "SELECT tipo, COUNT(*) n FROM aviso WHERE criado_em >= ? GROUP BY tipo", (desde,))},
    }


# ------------------------------------------------------------------ servidor

def _classe_servidor():
    """mcp 2.x renomeou FastMCP → MCPServer; aceita os dois."""
    try:
        from mcp.server.mcpserver import MCPServer
        return MCPServer
    except ImportError:
        from mcp.server.fastmcp import FastMCP
        return FastMCP


def criar_servidor():
    cfg = carregar()
    srv = _classe_servidor()(
        "goofish-miner",
        instructions=("Histórico de anúncios do Goofish (marketplace de usados da Alibaba) coletado de lojas e buscas "
                      "configuradas. Preços em CNY (¥). Só leitura; não faz rede: o que não está no banco ainda não foi "
                      "coletado. Títulos costumam estar em chinês — busque pelo termo em chinês também."),
    )

    def con() -> sqlite3.Connection:
        return db.conectar(cfg.db)

    @srv.tool(name="buscar")
    def _buscar(termo: str, dias: float = 30, limite: int = 20) -> dict:
        """Anúncios cujo título contém o termo e que entraram ou mudaram nos últimos N dias, mais baratos primeiro."""
        return buscar(con(), termo, dias, limite)

    @srv.tool(name="historico_preco")
    def _historico_preco(termo: str, dias: float = 90) -> dict:
        """Mediana, quartis, mínimo, máximo e tendência do preço dos anúncios cujo título contém o termo."""
        return historico_preco(con(), termo, dias)

    @srv.tool(name="quedas_de_preco")
    def _quedas_de_preco(dias: float = 7, min_pct: float = 5, limite: int = 20) -> list[dict]:
        """Anúncios que baixaram pelo menos min_pct% em relação ao maior preço já visto, com a mudança nos últimos N dias."""
        return quedas_de_preco(con(), dias, min_pct, limite)

    @srv.tool(name="novidades")
    def _novidades(horas: float = 24, fonte: str | None = None, limite: int = 20) -> list[dict]:
        """Anúncios vistos pela primeira vez nas últimas N horas; `fonte` filtra (ex.: 'busca:', 'loja:123')."""
        return novidades(con(), horas, fonte, limite)

    @srv.tool(name="anuncio")
    def _anuncio(item_id: str) -> dict:
        """Tudo sobre um anúncio: histórico de preço e título, fontes em que apareceu, etiquetas, avisos, vendedor."""
        return anuncio(con(), item_id)

    @srv.tool(name="vendedor")
    def _vendedor(vendedor_id: str) -> dict:
        """Perfil de uma loja (nível, avaliação, itens à venda ao longo do tempo) e os anúncios dela no banco."""
        return vendedor(con(), cfg, vendedor_id)

    @srv.tool(name="visao_geral")
    def _visao_geral() -> dict:
        """Estado do sistema: última varredura, fontes e quantos anúncios cada uma trouxe, avisos das últimas 24 h."""
        return visao_geral(con(), cfg)

    return srv


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--teste" in argv:
        sys.stdout.reconfigure(encoding="utf-8")   # títulos em chinês num console cp1252
        cfg = carregar()
        con = db.conectar(cfg.db)
        print(json.dumps(visao_geral(con, cfg), ensure_ascii=False, indent=1, default=str))
        print(json.dumps(novidades(con, 24 * 7, limite=3), ensure_ascii=False, indent=1))
        print(json.dumps(quedas_de_preco(con, 30, limite=3), ensure_ascii=False, indent=1))
        return 0
    criar_servidor().run()   # stdio
    return 0


if __name__ == "__main__":
    sys.exit(main())
