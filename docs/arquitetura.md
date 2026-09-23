# Arquitetura

## Três camadas, um banco

```
coletor.py  ──►  [ SQLite ]  ──►  notificador.py / telegram_bot.py  ──►  Telegram
(agendado)          ▲
                    └──────────  mcp_server.py  ──►  Claude Code
```

As camadas conversam **só pelo banco**, nunca por chamada direta. A coleta é
assíncrona (push, quando aparece algo) e a análise é síncrona (pull, quando
alguém pergunta). Se as duas ficassem juntas, o MCP teria que fazer rede no
meio da conversa, o que é lento e gasta o orçamento de requisições do IP. O
servidor MCP nunca faz rede: o que não está no banco ainda não foi coletado.

| Módulo | Responsabilidade |
|---|---|
| `mtop.py` | Chama `window.lib.mtop.request` dentro da página via `page.evaluate`; reconhece bloqueio (`RGV587`, página de punição HTML) |
| `coletor.py` | Varredura: lojas → buscas; filtro por fonte; janela, intervalo, freio de emergência |
| `extracao.py` | JSON do MTOP → `Snapshot` (preço `4.35万` → 43500, etiquetas, foto); corta telemetria do `raw` |
| `db.py` | Schema, regra de repetição, gravação append-only |
| `avisos.py` | Decide, só pelo histórico, o que é anúncio novo e o que é queda de preço |
| `notificador.py` | Envia os avisos com foto; só marca como enviado o que o Telegram confirmou |
| `telegram_bot.py` | Escutador `getUpdates`: `/status`, `/ultimos`, `/fontes`, `/parar`, `/janela`… |
| `mcp_server.py` | Tools de domínio para o Claude Code |

## Por que a assinatura MTOP não é reimplementada

O Goofish não tem API pública. O front fala com o MTOP, o gateway interno
da Alibaba, que usa requisição assinada, cookies gerados em JS e checagem de
fingerprint TLS. Reimplementar isso em Python exigiria engenharia reversa do
JS, quebraria a cada rotação do algoritmo e não traria valor nenhum.

O coletor abre **uma** página do goofish.com com Playwright (o Chrome
instalado, não o Chromium empacotado) e dispara as chamadas pelo SDK que a
própria página já carregou:

```js
await window.lib.mtop.request({ api: 'mtop.idle.web.xyh.item.list', v: '1.0',
                                data: { userId, pageNumber, pageSize: 20 },
                                ext_querys: { accountSite: 'xianyu', spm_cnt: 'a21ybx.personal.0.0' } });
```

A página monta `sign`, token e cookies sozinha. Além da assinatura, o
anti-bot confere o "rastro de navegação" (`spm_cnt`/`spm_pre`), então cada
API leva os seus `ext_querys` fixos em `mtop.py`. Como se chegou a isso está
em [`fase0.md`](fase0.md).

A página base é o perfil da primeira loja configurada. Sem loja, é a busca
do primeiro termo (com `spm` na URL), e a página 1 que ela pede ao carregar é
aproveitada, em vez de pedida de novo pelo SDK.

**Loja:** a listagem padrão é a aba "todos", que traz anúncio vendido junto.
A primeira chamada pede `needGroupInfo` para descobrir o `groupId` do grupo
`在售` ("em venda"), e as páginas seguintes vêm só desse grupo. Loja com zero à
venda não é paginada. O perfil (nível, avaliação, itens à venda) é gravado a
cada rodada, se mudou.

**Busca:** o `seller_id` que a busca devolve é criptografado, então anúncio
de busca não tem vendedor. Se o mesmo anúncio aparecer numa loja
acompanhada, a linha da loja traz o vendedor.

## Schema

O schema completo está em `src/goofish_miner/db.py`. As regras:

- **`snapshot` e `vendedor_snapshot` são append-only.** Nunca há `UPDATE`.
  O histórico de preço é o ativo principal, e o passado não se recoleta.
- **Regra de repetição:** reobservação sem mudança não gera linha;
  reobservação com mudança gera linha nova, sempre. A mudança é detectada
  por `conteudo_hash` = sha1 de (item_id, título, preço, estado). Sem isso,
  quase todo o banco seria repetição. Consequência: `visto_em` é "quando
  entrou ou mudou", não "última vez que estava no ar".
- **`raw` guarda o JSON da resposta**, só sem os campos de telemetria
  (`clickParam`, `trackParams`…). Não dá para saber hoje o que vai importar
  daqui a três meses.
- **O filtro é aplicado na coleta.** Anúncio fora da faixa de preço ou dos
  termos da fonte não entra no banco. Se o preço dele cair para dentro da
  faixa depois, ele entra nesse momento, como novo.
- `fonte` registra a primeira leitura de cada loja ou busca; `aviso` registra
  o que já foi tratado; `estado` guarda o freio, os ajustes do bot e o offset
  do Telegram; `varredura` registra cada rodada: `ok`, `parcial`, `bloqueada`,
  `erro`, `parada` (freio) e `interrompida` (processo morto no meio).

`user_id` e `item_id` são sempre `TEXT`: o comprimento varia de 10 a 13
dígitos, e inteiro nesse tamanho vira bug de precisão em algum JSON.

## Avisos

`avisos.pendentes()` olha só o último snapshot de cada anúncio:

- **novo:** o anúncio nunca foi tratado.
- **queda:** o último preço está pelo menos `queda_min_pct` abaixo do preço
  de referência. A referência é o preço do último aviso (ou da linha de base)
  e, sem nenhum, o da primeira vez que o anúncio foi visto. Avisar a queda
  muda a referência, então a mesma queda nunca sai duas vezes, e a próxima é
  medida a partir do novo patamar.

**Linha de base.** A primeira leitura de uma fonte não gera aviso: o que
aparece nela é marcado como `base`. Sem isso, acrescentar uma busca ao
`fontes.toml` despejaria dezenas de "novos" que não são novos.

**Nada se perde e nada se repete.** Um aviso só é marcado depois que o
Telegram devolve o `message_id`. Sem credencial, com a rede fora ou com o
token errado, nada é marcado, e os avisos ficam para a próxima rodada. Aviso
mais velho que `max_idade_horas` é descartado, para que dias com a máquina
desligada não virem uma enxurrada de mensagens velhas.

## MCP

`mcp_server.py` (stdio, SDK oficial `mcp`) expõe tools de domínio, não
genéricas. Não existe `query_sql` nem `http_get`, e todas são só leitura:

| Tool | O que devolve |
|---|---|
| `buscar(termo, dias)` | anúncios cujo título contém o termo, mais baratos primeiro |
| `historico_preco(termo, dias)` | mediana, quartis, mínimo, máximo e tendência |
| `quedas_de_preco(dias, min_pct)` | quem baixou em relação ao maior preço já visto |
| `novidades(horas, fonte)` | o que apareceu pela primeira vez |
| `anuncio(item_id)` | histórico completo de um anúncio, fontes, avisos, vendedor |
| `vendedor(id)` | perfil da loja ao longo do tempo e os anúncios dela |
| `visao_geral()` | última varredura, fontes, avisos das últimas 24 h |

## Windows

- Agendamento pelo Agendador de Tarefas (`scripts/agendar_tarefa.ps1`). A
  tarefa dispara a cada 15 min e o coletor decide se está na janela e se já
  passou o intervalo mínimo. Fora da vez, sai em menos de um segundo.
- As tarefas rodam `pythonw.exe`, não `uv run`. No Windows 11, um processo de
  console lançado pela tarefa vira uma aba do Windows Terminal, e fechar o
  terminal matava o escutador do bot.
- `-AllowStartIfOnBatteries` e `-DontStopIfGoingOnBatteries` são
  obrigatórios. Sem eles, o notebook fora da tomada para a coleta e o bot.
