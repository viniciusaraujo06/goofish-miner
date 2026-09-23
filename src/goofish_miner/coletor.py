"""Varredura: lojas → buscas → avisos.

Roda uma vez e sai (o agendador chama a cada 15 min e ele decide se é a vez).
Comunica-se com o resto do sistema só pelo banco. Lojas vêm antes das buscas:
a linha da loja é a mais rica (vendedor numérico, estado de venda) e é a que
vence quando o mesmo anúncio aparece também numa busca.

    uv run goofish-miner [--forcar] [--paginas N] [--headless/--no-headless]
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time
import traceback
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

from . import db, extracao, mtop
from .config import Config, Fonte, carregar

log = logging.getLogger("goofish_miner")

BRASILIA = ZoneInfo("America/Sao_Paulo")
LIMITE_PRECO_INVALIDO = 0.20   # acima disso a varredura é marcada `parcial`: o formato do preço mudou?


def em_janela(cfg: Config, agora: datetime | None = None, inicio: int | None = None, fim: int | None = None) -> bool:
    h = (agora or datetime.now(BRASILIA)).hour
    a = cfg.janela_inicio if inicio is None else inicio
    b = cfg.janela_fim if fim is None else fim
    return a <= h < b


def ajustes(con, cfg: Config) -> tuple[int, int, int]:
    """(inicio, fim, intervalo_min_minutos) efetivos: o que o bot gravou no banco
    (/janela, /frequencia) sobrepõe o coleta.toml. Sem ajuste, vale o arquivo."""
    def ler(chave: str, padrao: int) -> int:
        v = db.ler_flag(con, chave)
        return int(v) if v is not None and v.lstrip("-").isdigit() else padrao
    return (ler("janela_inicio", cfg.janela_inicio), ler("janela_fim", cfg.janela_fim),
            ler("intervalo_min_minutos", cfg.intervalo_min_minutos))


class Parada(Exception):
    """Freio de emergência acionado (/parar no Telegram) no meio da varredura."""


def coleta_pausada(con) -> bool:
    return db.ler_flag(con, "coleta_pausada") == "1"


class Varredura:
    def __init__(self, cfg: Config, con, paginas: int | None = None):
        self.cfg = cfg
        self.con = con
        self.paginas = paginas            # teto da linha de comando; None = o de cada fonte
        self.visto_em = db.agora()
        self.snapshots = 0
        self.vistos: set[str] = set()     # dedup dentro da rodada
        self.novos: list[str] = []
        self.cont: Counter = Counter()    # novo, mudou, igual, repetido, vendido, filtro, preco_invalido, itens

    def pausa(self, minimo: float | None = None, maximo: float | None = None) -> None:
        """Espera entre requisições — e é onde o freio de emergência é checado."""
        if coleta_pausada(self.con):
            raise Parada("/parar acionado")
        time.sleep(random.uniform(minimo or self.cfg.pausa_min, maximo or self.cfg.pausa_max))

    def _paginas(self, f: Fonte) -> int:
        return min(f.paginas, self.paginas) if self.paginas else f.paginas

    # ------------------------------------------------------------ gravação

    def _guardar(self, snap: db.Snapshot, f: Fonte, estreia: bool) -> str:
        """Filtra, deduplica e grava se for novo ou tiver mudado.
        Devolve 'novo' | 'mudou' | 'igual' | 'repetido' | 'filtro'."""
        if snap.item_id in self.vistos:
            self.cont["repetido"] += 1
            return "repetido"
        if f.filtro.recusa(snap.titulo, snap.preco_cny):
            self.cont["filtro"] += 1
            return "filtro"
        self.vistos.add(snap.item_id)
        r, sid = db.gravar_se_mudou(self.con, snap, self.visto_em)
        self.cont[r] += 1
        if r != "igual":
            self.snapshots += 1
        if r == "novo":
            self.novos.append(snap.item_id)
            if estreia:   # primeira leitura da fonte: linha de base, sem aviso
                db.registrar_aviso(self.con, snap.item_id, sid, "base")
            log.info("novo %s ¥%s %s", snap.item_id, snap.preco_cny, snap.titulo[:50])
        return r

    def _processar_cartao(self, card: dict, f: Fonte, estreia: bool = False) -> str:
        """Um cartão da lista da loja. Devolve o destino dele."""
        self.cont["itens"] += 1
        try:
            snap = extracao.de_cartao_lista(card, f.alvo)
        except extracao.PrecoInvalido as e:
            self.cont["preco_invalido"] += 1
            log.warning("preço inválido, descartado: item %s fonte %s preço=%r", e.item_id, f.chave, e.texto)
            return "preco_invalido"
        if snap.estado != 0:            # vendido não entra
            self.cont["vendido"] += 1
            return "vendido"
        return self._guardar(snap, f, estreia)

    def _processar_resultado(self, main: dict, f: Fonte, estreia: bool = False) -> str:
        self.cont["itens"] += 1
        try:
            snap = extracao.de_resultado_busca(main, f.alvo)
        except extracao.PrecoInvalido as e:
            self.cont["preco_invalido"] += 1
            log.warning("preço inválido, descartado: item %s fonte %s preço=%r", e.item_id, f.chave, e.texto)
            return "preco_invalido"
        return self._guardar(snap, f, estreia)

    # ------------------------------------------------------------ etapas

    def loja(self, page, f: Fonte) -> None:
        estreia = not db.fonte_conhecida(self.con, f.chave)
        log.info("loja %s (%s)%s", f.nome, f.alvo, " — estreia, só linha de base" if estreia else "")
        antes = Counter(self.cont)
        # página 1 da aba "todos": traz itemGroupList (id e tamanho do grupo "em venda")
        r = mtop.lista_vendedor(page, f.alvo, 1)
        dados = r.get("data") or {}
        grupos = extracao.grupos_da_lista(dados)
        a_venda = grupos.get(mtop.GRUPO_A_VENDA)
        for card in dados.get("cardList") or []:
            self._processar_cartao(card, f, estreia)
        self.pausa()

        if a_venda and a_venda["n"] == 0:
            log.info("  nada à venda; pulando paginação")
        else:
            # com o grupo: só anúncio comprável; sem ele (formato mudou): segue na aba "todos"
            grupo_id = a_venda["id"] if a_venda else None
            for pagina in range(1 if grupo_id else 2, self._paginas(f) + 1):
                r = mtop.lista_vendedor(page, f.alvo, pagina, grupo_id=grupo_id)
                dados = r.get("data") or {}
                cards = dados.get("cardList") or []
                for card in cards:
                    self._processar_cartao(card, f, estreia)
                log.info("  %s, página %d: %d itens", "em venda" if grupo_id else "todos", pagina, len(cards))
                if not dados.get("nextPage") or not cards:
                    break
                self.pausa()
            self.pausa()

        perfil = mtop.perfil_vendedor(page, f.alvo)
        db.gravar_vendedor_se_mudou(self.con, extracao.de_perfil(perfil.get("data") or {}, f.alvo,
                                                                a_venda["n"] if a_venda else None), self.visto_em)
        db.registrar_fonte(self.con, f.chave)
        d = self.cont - antes
        log.info("  %s: novos %d, mudaram %d, iguais %d, vendidos %d, fora do filtro %d", f.nome,
                 d["novo"], d["mudou"], d["igual"], d["vendido"], d["filtro"])
        self.pausa()

    def busca(self, page, f: Fonte, primeira: dict | None = None) -> None:
        """`primeira`: página 1 já obtida (a que a página base de busca pediu sozinha)."""
        estreia = not db.fonte_conhecida(self.con, f.chave)
        log.info("busca %r%s", f.alvo, " — estreia, só linha de base" if estreia else "")
        antes = Counter(self.cont)
        for pagina in range(1, self._paginas(f) + 1):
            r = primeira if (pagina == 1 and primeira) else mtop.busca(page, f.alvo, pagina)
            dados = r.get("data") or {}
            itens = extracao.resultados_da_busca(dados)
            for main in itens:
                self._processar_resultado(main, f, estreia)
            log.info("  página %d: %d itens", pagina, len(itens))
            if not (dados.get("resultInfo") or {}).get("hasNextPage") or not itens:
                break
            self.pausa()
        db.registrar_fonte(self.con, f.chave)
        d = self.cont - antes
        log.info("  %r: %d itens, novos %d, mudaram %d, fora do filtro %d", f.alvo, d["itens"], d["novo"], d["mudou"], d["filtro"])
        self.pausa()

    def resumo(self) -> str:
        c = self.cont
        return (f"novos {c['novo']}, mudaram {c['mudou']}, sem mudança {c['igual']}; repetidos na rodada {c['repetido']}; "
                f"vendidos {c['vendido']}, fora do filtro {c['filtro']}, preço inválido {c['preco_invalido']} "
                f"de {c['itens']} itens")


def executar(cfg: Config, *, forcar: bool, paginas: int | None, headless: bool | None) -> int:
    con = db.conectar(cfg.db)
    if not cfg.fontes:
        log.error("nenhuma fonte em config/fontes.toml")
        return 1
    inicio, fim, intervalo = ajustes(con, cfg)
    if not forcar and not em_janela(cfg, inicio=inicio, fim=fim):
        log.info("fora da janela %02d–%02dh de Brasília; nada a fazer", inicio, fim)
        return 0
    if coleta_pausada(con):
        log.info("coleta pausada por /parar; /continuar libera")
        return 0
    # rodada anterior morta no meio (/parar encerra o processo): não fica 'rodando' pra sempre
    con.execute("UPDATE varredura SET status = 'interrompida', terminada_em = ? WHERE status = 'rodando'", (db.agora(),))
    con.commit()
    ultima = con.execute("SELECT iniciada_em, status FROM varredura ORDER BY id DESC LIMIT 1").fetchone()
    if ultima and not forcar:
        delta = (datetime.fromisoformat(db.agora()) - datetime.fromisoformat(ultima["iniciada_em"])).total_seconds()
        if delta < intervalo * 60:
            log.info("última varredura há %.0f min; mínimo %d", delta / 60, intervalo)
            return 0
        if ultima["status"] == "bloqueada" and delta < cfg.apos_bloqueio_horas * 3600:
            log.info("última varredura foi bloqueada há %.1f h; esperando %.0f h de silêncio", delta / 3600, cfg.apos_bloqueio_horas)
            return 0

    vid = db.iniciar_varredura(con)
    v = Varredura(cfg, con, paginas)
    status, msg = "ok", None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                channel=cfg.canal, headless=cfg.headless if headless is None else headless,
                args=["--disable-blink-features=AutomationControlled"], ignore_default_args=["--enable-automation"],
            )
            ctx = browser.new_context(user_agent=mtop.UA, locale="zh-CN", viewport={"width": 1366, "height": 850})
            page = ctx.new_page()
            primeira = None
            if cfg.lojas:
                mtop.abrir_pagina_base(page, cfg.lojas[0].alvo)
            else:   # só buscas: a página base é a primeira delas, e a página 1 dela já vem junto
                primeira = mtop.abrir_pagina_busca(page, cfg.buscas[0].alvo)
            v.pausa(2, 4)
            for f in cfg.lojas:
                v.loja(page, f)
            for i, f in enumerate(cfg.buscas):
                v.busca(page, f, primeira if i == 0 else None)
            browser.close()
    except Parada as e:
        status, msg = "parada", str(e)
        log.warning("varredura interrompida pelo freio de emergência")
    except mtop.Bloqueado as e:
        status, msg = "bloqueada", str(e)
        log.error("varredura abortada — anti-bot: %s", e)
    except Exception:  # noqa: BLE001 — registra qualquer falha no banco antes de sair
        status, msg = "erro", traceback.format_exc()
        log.exception("varredura falhou")

    if v.cont["itens"] and v.cont["preco_invalido"] / v.cont["itens"] > LIMITE_PRECO_INVALIDO and status == "ok":
        status = "parcial"
        msg = f"preço inválido em {v.cont['preco_invalido']} de {v.cont['itens']} itens — formato do campo mudou?"
        log.error(msg)

    db.finalizar_varredura(con, vid, status, v.snapshots, v.cont["novo"], msg)
    log.info(v.resumo())
    log.info("varredura #%d %s: %d linhas gravadas, %d itens novos", vid, status, v.snapshots, v.cont["novo"])

    # Avisos, ainda pelo banco. Falha aqui nunca derruba a coleta.
    if cfg.notif_ativo:
        try:
            from . import notificador
            log.info("avisos: %s", notificador.executar(con, cfg))
        except Exception:  # noqa: BLE001
            log.exception("avisos falharam")
    else:
        log.info("avisos desligados (coleta.toml [notificacao] ativo = false)")
    return 0 if status in ("ok", "parcial") else 1


def configurar_log(cfg: Config, arquivo: str) -> None:
    (cfg.raiz / "data").mkdir(exist_ok=True)
    # sob pythonw (tarefa agendada, sem console) sys.stdout é None: só o arquivo
    handlers: list[logging.Handler] = [logging.FileHandler(cfg.raiz / "data" / arquivo, encoding="utf-8")]
    if sys.stdout is not None:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # títulos em chinês num console cp1252
        handlers.insert(0, logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    logging.getLogger("httpx").setLevel(logging.WARNING)   # o INFO dele imprime a URL com o token do Telegram


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="goofish-miner", description=__doc__.split("\n")[0])
    ap.add_argument("--forcar", action="store_true", help="ignora janela de horário e intervalo mínimo")
    ap.add_argument("--paginas", type=int, help="teto de páginas por fonte nesta rodada")
    ap.add_argument("--headless", action=argparse.BooleanOptionalAction, default=None)
    args = ap.parse_args(argv)
    cfg = carregar()
    configurar_log(cfg, "coletor.log")
    return executar(cfg, forcar=args.forcar, paginas=args.paginas, headless=args.headless)


if __name__ == "__main__":
    sys.exit(main())
