# FASE 0 — reconhecimento do Goofish sem login

> Registro do reconhecimento feito no projeto que deu origem a este, um
> monitor de relógios usados — por isso os termos de exemplo são 精工 (Seiko)
> e as "lojas" aparecem como fornecedores. O que vale pro goofish-miner:
> quais APIs respondem sem login, o papel do `spm`, e que o anti-bot pune
> quem insiste.

Data: 2026-09-07. Scripts: `scripts/fase0*.py` (descartáveis, em ordem
cronológica a–i; os IDs de loja e anúncio neles são fictícios). Capturas brutas (JSONL com corpo completo + screenshots)
em `data/fase0/` (fora do git).

## Resposta curta

| Pergunta | Resposta |
|---|---|
| `window.lib.mtop.request` existe? | **Sim.** `window.lib` expõe `mtop`, `login`, `checkAutoLogin`, `refreshAutoLogin`, `ACCS`. |
| Página de loja responde sem login? | **Sim.** Lista paginada + perfil, por navegação e por SDK direto. |
| Busca por keyword responde sem login? | **Sim, com o rastro `spm`.** Sem ele, cai no login. Pelo SDK, precisa de `ext_querys`. |
| Detalhe de anúncio responde sem login? | **Respondia** (17:27). Passou a bloquear por IP após várias chamadas ruins na mesma hora. Retestar após descanso. |
| A extensão CSSBuy muda algo? | **Não.** Só esconde o popup e libera o scroll. |

**Decisão: a FASE 1 cobre fornecedores E buscas por keyword**, tudo via
SDK a partir de uma única página aberta do goofish. O detalhe do anúncio
entra assim que o reteste confirmar que voltou a responder.

## A regra de ouro: o anti-bot pune quem insiste

O `RGV587_ERROR::SM` ("被挤爆啦", com `data.url` apontando para
`mini_login.htm?appEntrance=baxia`) não é rate limit — é o módulo de
segurança marcando a chamada como suspeita. Observado nesta sessão:

- 17:27 — detalhe do anúncio respondia `SUCCESS` por navegação.
- 17:27–17:55 — ~8 chamadas de detalhe bloqueadas (tentativas minhas via SDK).
- 17:55 e 17:57 — detalhe bloqueado **até na chamada da própria página**,
  em perfil persistente e em contexto anônimo novo. A busca continuava
  passando. Logo: punição por IP, específica daquele endpoint.

Consequência pro coletor: **abortar na primeira `RGV587`** e só voltar na
próxima janela. O backoff exponencial do brief não basta — insistir
piora. Sempre `HTTP 200`; o bloqueio está no `ret`, não no status.

## Endpoints e o que devolvem

### `mtop.idle.web.xyh.item.list` — lista de itens do vendedor

`data: {"userId": "<id>", "pageNumber": 1, "pageSize": 20}`. Devolve
`cardList[]`, `nextPage` (bool) e `totalCount` (veio `0` com 297 itens —
não confiar; usar `nextPage`). Pagina bem pelo SDK direto.

A resposta traz também `itemGroupList[]`: os grupos da loja com
`groupName` (`全部`, `在售`, ...), `groupId` e `itemNumber`. O `itemNumber`
de `在售` é o número real de anúncios à venda (o coletor grava em
`vendedor_snapshot.itens_a_venda`). A aba "em venda" da página chama a
mesma API com `{"groupId": <id do 在售>, "groupName": "在售",
"needGroupInfo": false, "defaultGroup": true}` — capturado em
`scripts/fase0i_aba_em_venda.py`. Ainda não usado pelo coletor (exige mexer
em `mtop.py`).

Por cartão (`cardData`):

- `id` / `detailParams.itemId` — id do anúncio (string)
- `title`, `priceInfo.price` / `detailParams.soldPrice` (string, "338")
- `categoryId` (50025426 = relógios)
- `detailParams.imageInfos` — **JSON serializado em string** com todas as
  fotos (`url`, `major`, `widthSize`, `heightSize`)
- `detailParams.postInfo` — "包邮" (frete grátis)
- `itemLabelDataVO.labelData.r*.tagList[].data.content` — etiquetas como
  `24小时内发布` (publicado nas últimas 24h), `5人想要` (5 interessados)
- `itemStatus` — **0 = à venda, 1 = vendido.** A listagem sem filtro é a
  aba "todos" da loja e traz anúncio vendido junto (41% do que foi
  coletado no primeiro dia; uma das lojas veio 100% vendida na
  página 1). O coletor descarta vendido antes de gravar.

### `mtop.idle.web.user.page.head` — perfil do vendedor

`data: {"userId": "<id>"}`. Em `data.module`:

- `shop.level` ("L4"), `shop.score`, `shop.praiseRatio` (95), `shop.reviewNum` (228)
- `tabs.item.number` (297 itens ativos), `tabs.rate.number` (303 avaliações)
- `social.followers`, `base.displayName`, `base.ipLocation`, `base.introduction`
- `baseInfo.tags.real_name_certification_77`, `idle_zhima_zheng`

**Não traz `已售` (vendas).** Isso só vem no detalhe do item.

### `mtop.taobao.idlemtopsearch.pc.search` — busca por keyword

Pela URL: `https://www.goofish.com/search?q=<kw>&spm=a21ybx.search.searchInput.0`
(sem `spm` → login). Pelo SDK, replicando exatamente a chamada da página:

```js
await window.lib.mtop.request({
  api: 'mtop.taobao.idlemtopsearch.pc.search', v: '1.0',
  appKey: '34839810', dataType: 'originaljson', sessionOption: 'AutoLoginOnly',
  type: 'POST', timeout: 20000,
  ext_querys: { accountSite: 'xianyu', spm_cnt: 'a21ybx.search.0.0',
                spm_pre: 'a21ybx.search.searchInput.0' },
  data: { pageNumber: 1, keyword: '精工5', fromFilter: false, rowsPerPage: 30,
          sortValue: '', sortField: '', customDistance: '', gps: '',
          propValueStr: {}, customGps: '', searchReqFromPage: 'pcSearch',
          extraFilterValue: '{}', userPositionJson: '{}' }
});
```

Testado: página 2 com `ext_querys` → 30 itens; mesma chamada sem
`ext_querys` → `RGV587`; funciona disparada da página de um item, sem
nunca ter aberto a busca. `data.resultInfo.hasNextPage` controla a
paginação.

Por resultado (`resultList[i].data.item.main`):

- `exContent.itemId`, `exContent.title`, `exContent.area` (província),
  `exContent.userNickName`, `exContent.picUrl`
- `exContent.detailParams.soldPrice` (string) — preço limpo; `exContent.price`
  é uma lista de spans de renderização
- `exContent.fishTags.r*.tagList[].data.content` — `轻微使用痕迹`,
  `Seiko/精工`, `累计降价27%`, `20人想要`, `卖家信用优秀`
- `clickParam.args`: `id`, `price`, `publishTime` (epoch ms), `wantNum`,
  `catId`, `seller_id` (**criptografado** — não é o userId numérico; a
  ligação com fornecedor conhecido tem que vir do detalhe)

### `mtop.taobao.idle.pc.detail` — detalhe do anúncio

Disparado pela página `/item?id=<itemId>`. A fonte mais rica:

`itemDO`:
- `desc` — descrição completa (é aqui que aparecem `不退不换`, `表径37mm`,
  `单表无附件` — o título sozinho não basta pro scoring)
- `soldPrice`, `originalPrice`, `quantity`, `transportFee`
- `browseCnt` (281), `wantCnt` (5), `collectCnt` (2)
- `gmtCreate` (epoch ms), `imageInfos[]` (lista), `itemCatDTO`, `itemStatus`

`sellerDO`:
- **`hasSoldNumInteger` (368) — vendas totais do vendedor**
- `sellerId` (numérico), `nick`, `city`, `userRegDay` (811)
- `remarkDO.sellerGoodRemarkCnt` / `sellerBadRemarkCnt` / `sellerDefaultRemarkCnt`
- `newGoodRatioRate` ("95%"), `replyRatio24h`, `replyInterval`, `lastVisitTime`
- `sellerItems[]` — outros itens do vendedor com `attributeMap` (`gmtShelf`, `firstPrice`, `freeShipping`, `bargain`)

Estado atual: bloqueado por IP (ver regra de ouro). A chamada da página
leva `ext_querys` `{accountSite: 'xianyu', spm_cnt: 'a21ybx.item.0.0'}` e
`data: {itemId}` — replicar pelo SDK ficou pendente de reteste porque
todas as tentativas aconteceram já sob punição.

### Ruído (ignorar)

`mtop.taobao.idlemessage.pc.loginuser.get` e `mtop.idle.web.user.page.nav`
sempre devolvem `FAIL_SYS_SESSION_EXPIRED` — chamadas de sessão da própria
página, esperadas sem login. `mtop.gaia.nodejs...index.get` é config/layout.

## Implicações pro desenho da FASE 1

1. **Uma página aberta, tudo via SDK.** Abrir `goofish.com/personal?userId=...`
   uma vez e disparar lista de fornecedores e buscas por `page.evaluate`.
   Nenhuma navegação por item, exceto se o detalhe exigir.
2. **`mtop.py` recebe `ext_querys` por endpoint.** Os valores de
   `spm_cnt`/`spm_pre` são parte da "assinatura de contexto" que o
   anti-bot confere. Tabela por API, versionada no código.
3. **Dedup de busca contra fornecedor conhecido é pelo detalhe**, porque o
   `seller_id` da busca é criptografado. Até lá, dedup por `item_id`.
4. **`vendedor_snapshot` é alimentado pelo detalhe** (`hasSoldNumInteger`).
   Enquanto o detalhe estiver fora, o perfil dá `reviewNum` + `level` como
   proxy do critério "≥ 25 vendas".
5. **Abortar na primeira `RGV587`**, registrar no banco e notificar. Sem retry.
6. Guardar `desc`, `imageInfos` e `fishTags` no `raw`.
7. `userId` como string: `sellerId` vem inteiro no detalhe e string no perfil.

## Ambiente (Windows)

- Chrome de marca ≥ 137 ignora `--load-extension`. Edge aceita.
- O Chromium empacotado do Playwright (`chromium-1234`) não abre na
  máquina de desenvolvimento ("configuração lado a lado incorreta"), mesmo após download
  limpo. O headless shell funciona. Usar `channel="chrome"`.
