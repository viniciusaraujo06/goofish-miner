import pytest

from goofish_miner.config import Filtro, carregar


def _escrever(tmp_path, fontes: str, coleta_extra: str = ""):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "fontes.toml").write_text(fontes, encoding="utf-8")
    (tmp_path / "config" / "coleta.toml").write_text(
        "[janela]\ninicio = 8\nfim = 20\n[paginas]\nloja = 3\nbusca = 2\n[pausa]\nmin = 3.0\nmax = 8.0\n"
        "[navegador]\ncanal = \"chrome\"\nheadless = true\n" + coleta_extra, encoding="utf-8")
    return carregar(tmp_path)


def test_config_do_repo_carrega():
    cfg = carregar()
    assert cfg.fontes and all(isinstance(f.alvo, str) for f in cfg.fontes)
    assert cfg.notif_ativo is False   # o exemplo publicado nunca sai mandando mensagem


def test_filtro_por_fonte_sobrepoe_o_padrao_campo_a_campo(tmp_path):
    cfg = _escrever(tmp_path, """
[filtro]
preco_min = 50
excluir = ["壳"]

[[busca]]
termo = "富士 X100V"
[busca.filtro]
preco_min = 3000
incluir = ["X100V"]

[[busca]]
termo = "机械键盘"
paginas = 5

[[loja]]
nome = "Loja"
user_id = "1000000000001"
""")
    x100, teclado = cfg.buscas
    assert x100.filtro == Filtro(preco_min=3000, incluir=("x100v",), excluir=("壳",))   # excluir veio do padrão
    assert teclado.filtro == Filtro(preco_min=50, excluir=("壳",)) and teclado.paginas == 5
    assert cfg.lojas[0].paginas == 3 and cfg.lojas[0].chave == "loja:1000000000001"
    assert cfg.fonte("busca:机械键盘") is teclado


def test_user_id_precisa_ser_numerico(tmp_path):
    with pytest.raises(ValueError, match="user_id"):
        _escrever(tmp_path, '[[loja]]\nnome = "x"\nuser_id = "abc"\n')


def test_chave_desconhecida_no_filtro_nao_passa_calada(tmp_path):
    with pytest.raises(ValueError, match="preco_maximo"):
        _escrever(tmp_path, '[[busca]]\ntermo = "x"\n[busca.filtro]\npreco_maximo = 10\n')


def test_fonte_repetida(tmp_path):
    with pytest.raises(ValueError, match="repetida"):
        _escrever(tmp_path, '[[busca]]\ntermo = "x"\n[[busca]]\ntermo = "x"\n')


def test_filtro_recusa():
    f = Filtro(preco_min=100, preco_max=500, incluir=("x100v", "x100f"), excluir=("壳",))
    assert f.recusa("富士X100V 银色", 300) is None
    assert f.recusa("富士X100V 银色", 50) == "preco_min"
    assert f.recusa("富士X100V 银色", 900) == "preco_max"
    assert f.recusa("富士X-T30", 300) == "incluir"
    assert f.recusa("X100V 专用皮壳", 300) == "excluir"
    assert Filtro().recusa("qualquer coisa", 1) is None   # sem filtro, tudo passa
