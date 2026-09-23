from dataclasses import replace

import pytest

from goofish_miner import db
from goofish_miner.config import Config, Filtro, Fonte

LOJA = Fonte("loja", "Loja A", "1000000000001", 2)
BUSCA = Fonte("busca", "teclado", "机械键盘", 1, Filtro(preco_max=600, excluir=("键帽",)))


def fazer_cfg(raiz, **kw) -> Config:
    base = Config(raiz=raiz, fontes=[LOJA, BUSCA], janela_inicio=8, janela_fim=20, intervalo_min_minutos=50,
                  apos_bloqueio_horas=3, pausa_min=0, pausa_max=0, canal="chrome", headless=True,
                  notif_ativo=True, notif_novos=True, notif_queda_min_pct=10, notif_max_por_rodada=10,
                  notif_max_idade_horas=48)
    return replace(base, **kw)


@pytest.fixture
def cfg(tmp_path):
    return fazer_cfg(tmp_path)


@pytest.fixture
def con(cfg):
    return db.conectar(cfg.db)


def snap(item_id="1", preco=100.0, titulo="t", fonte="busca:机械键盘", raw=None, estado=0, vendedor_id=None):
    return db.Snapshot(item_id=item_id, preco_cny=preco, titulo=titulo, vendedor_id=vendedor_id,
                       fonte=fonte, raw=raw if raw is not None else {}, estado=estado)
