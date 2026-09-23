import json

import pytest

from goofish_miner import extracao


def test_preco():
    assert extracao._preco("338") == 338.0
    assert extracao._preco("¥1,299") == 1299.0
    assert extracao._preco("4.35万") == 43500.0   # nunca vira 4.35 nem zero
    assert extracao._preco("2千") == 2000.0
    assert extracao._preco("面议") is None        # "a combinar"
    assert extracao._preco("0") is None
    assert extracao._preco(None) is None


def _cartao(status=0, imagens=None, pic="http://img/a.jpg"):
    dp = {"itemId": "900000000001", "soldPrice": "4200", "picUrl": pic}
    if imagens is not None:
        dp["imageInfos"] = json.dumps(imagens)   # o Goofish manda serializado em string
    return {
        "cardData": {
            "id": "900000000001",
            "title": "富士X100V 银色 九成新 带原装遮光罩",
            "priceInfo": {"preText": "¥", "price": "4200"},
            "detailParams": dp,
            "itemStatus": status,
            "picInfo": {"picUrl": pic},
            "trackParams": {"labId": "320"},
            "itemLabelDataVO": {
                "labelData": {"r3": {"tagList": [{"data": {"content": "5人想要"}}]}},
                "serviceUtParams": "[...]", "trackParams": {"x": 1}, "unShowLabelUtParams": "{}",
            },
        },
        "cardType": 1003,
    }


def test_cartao_lista_a_venda():
    s = extracao.de_cartao_lista(_cartao(status=0), "1000000000001")
    assert s.item_id == "900000000001"
    assert s.preco_cny == 4200.0
    assert s.vendedor_id == "1000000000001"
    assert s.fonte == "loja:1000000000001"
    assert s.estado == 0
    assert s.raw["_etiquetas"] == ["5人想要"]


def test_cartao_lista_vendido_tem_estado_1():
    assert extracao.de_cartao_lista(_cartao(status=1), "1").estado == 1


def test_cartao_lista_raw_sem_telemetria():
    raw = extracao.de_cartao_lista(_cartao(), "1").raw
    assert "trackParams" not in raw["cardData"]
    for k in ("serviceUtParams", "trackParams", "unShowLabelUtParams"):
        assert k not in raw["cardData"]["itemLabelDataVO"]
    assert raw["cardData"]["itemLabelDataVO"]["labelData"]  # o dado real ficou


def test_cartao_lista_preco_invalido_levanta():
    card = _cartao()
    card["cardData"]["priceInfo"]["price"] = "面议"
    card["cardData"]["detailParams"]["soldPrice"] = ""
    with pytest.raises(extracao.PrecoInvalido):
        extracao.de_cartao_lista(card, "1")


def test_foto_principal_da_lista():
    seis = [{"url": f"http://img/{i}.jpg", "major": i == 3} for i in range(6)]
    assert extracao.foto_principal(_cartao(imagens=seis)) == "http://img/3.jpg"
    sem_major = [{"url": f"http://img/{i}.jpg"} for i in range(3)]
    assert extracao.foto_principal(_cartao(imagens=sem_major, pic="http://img/capa.jpg")) == "http://img/capa.jpg"
    assert extracao.foto_principal(_cartao(imagens=[], pic=None)) is None


def _resultado():
    return {
        "exContent": {
            "itemId": "900000000003",
            "title": "自用闲置 客制化机械键盘 热插拔",
            "picUrl": "http://img/x.jpg",
            "detailParams": {"soldPrice": "499"},
            "fishTags": {"r2": {"tagList": [{"data": {"content": "轻微使用痕迹"}}]}},
            "trackParams": {"a": 1}, "serviceUtParams": "[]", "fishTagCustomParam": "{}", "photoSearchUrl": "http://x",
        },
        "clickParam": {"args": {"id": "900000000003", "price": "499", "seller_id": "cripto==",
                                "publishTime": "1735703351000", "wantNum": "3", "idle_mount_tai_task_abs": "1:T:0;..."}},
        "targetUrl": "https://www.goofish.com/item?id=900000000003",
    }


def test_resultado_busca_sem_estado_e_raw_enxuto():
    data = {"resultList": [{"data": {"item": {"main": _resultado()}}}, {"data": {"outro": {}}}],
            "resultInfo": {"hasNextPage": True}}
    itens = extracao.resultados_da_busca(data)
    assert len(itens) == 1                       # o card que não é item fica de fora
    s = extracao.de_resultado_busca(itens[0], "机械键盘")
    assert s.item_id == "900000000003"
    assert s.preco_cny == 499.0
    assert s.vendedor_id is None                 # seller_id da busca é criptografado
    assert s.estado == 0
    assert s.fonte == "busca:机械键盘"
    assert s.raw["_etiquetas"] == ["轻微使用痕迹"]
    assert "clickParam" not in s.raw and "targetUrl" not in s.raw
    for k in ("trackParams", "serviceUtParams", "fishTagCustomParam", "photoSearchUrl"):
        assert k not in s.raw["exContent"]
    assert s.raw["_busca"] == {"publishTime": "1735703351000", "wantNum": "3", "seller_id": "cripto=="}
    assert extracao.foto_principal(s.raw) == "http://img/x.jpg"


def test_perfil_e_grupos():
    perfil = {"module": {"shop": {"level": "L4", "praiseRatio": 95, "reviewNum": 228},
                         "tabs": {"item": {"number": 297}}, "base": {"displayName": "示例店A"}}}
    lista = {"itemGroupList": [{"groupName": "全部", "groupId": 11998544, "itemNumber": 297},
                               {"groupName": "在售", "groupId": 11998545, "itemNumber": 29}]}
    grupos = extracao.grupos_da_lista(lista)
    assert grupos == {"全部": {"id": 11998544, "n": 297}, "在售": {"id": 11998545, "n": 29}}
    v = extracao.de_perfil(perfil, "1000000000001", grupos["在售"]["n"])
    assert (v.nome, v.nivel, v.avaliacao, v.avaliacoes, v.itens_total, v.itens_a_venda) == ("示例店A", "L4", 95.0, 228, 297, 29)
    assert extracao.grupos_da_lista({}) == {}
