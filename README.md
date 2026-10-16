# Bot de trading automático

Serviço local que analisa pares USDT na Binance e só envia ordem pelo order gate portado do Vibe-Trading. A interface lê o estado e manda pausar, parar ou ajustar limites. Ela não fala com a exchange.

Isto não é recomendação de investimento. Nenhuma estratégia garante lucro. O retorno do painel é líquido de taxa, slippage e funding.

## O que a v1 faz

- Spot em BTC, ETH, SOL, BNB e XRP. Futuros USD-M são só leitura: a carrega de funding aparece no painel e não executa.
- Quatro famílias: tendência (1h e 4h), momento (4h), reversão à média (15m e 1h) e carrega de funding (sinal apenas).
- Testnet por padrão (`binance-paper-trade`). Produção (`binance-live-trade-bot`) só depois dos gates, de 30 dias com PnL líquido positivo, de um mandato restrito a cripto e da palavra `PRODUCAO`.
- Nenhuma ordem sem mandato, latch e registro no journal antes do envio. Halt não se desarma sozinho.
- Toda compra ganha um STOP_LOSS_LIMIT na Binance logo depois da execução, então a posição fica protegida mesmo com o robô desligado. Alvo e saída da estratégia são checados a cada ciclo: o robô cancela o stop e vende com ordens LIMIT IOC.
- Spot não vende a descoberto. Um sinal de venda sem posição aparece como potencial e nunca vira ordem.
- Stop móvel (ligado por padrão): o stop na Binance sobe com o maior preço, na mesma distância da entrada, e nunca desce. Cada posição grava a regra de saída com que abriu e segue essa regra até fechar, mesmo que a opção mude depois. Uma estratégia validada com outra regra (ou antes desta versão) volta a somente sinal até ser validada de novo. O alvo fixo deixa de fechar a posição; a saída vem do stop ou do sinal da estratégia. O stop só sai do livro quando o novo pode entrar: sem halt, mandato válido, sem pressão de peso da API, abaixo do bid e com uma ordem do dia reservada para cada posição aberta.
- Modo de entrada:
  - **Manual** (padrão): o ciclo nunca compra sozinho. Você executa uma oportunidade Pronta pelo painel e o robô cuida do stop e das saídas; o ciclo liga sozinho nessa hora. Compras feitas direto na Binance não são acompanhadas.
  - **Automático**: o ciclo também compra as oportunidades Prontas cuja **taxa de acerto estimada** da estratégia passa do mínimo (60% por padrão, entre 50% e 95%). A taxa é `(acertos + 1) / (trades + 2)` sobre os trades do teste histórico com a mesma regra de saída e as posições reais fechadas, e só existe com pelo menos 20 trades. É histórico, não probabilidade garantida. Ao trocar para automático o ciclo para e exige Iniciar.

A origem de cada módulo portado está em [ORIGIN.md](ORIGIN.md).

## Trade ao vivo

Ao executar uma oportunidade, o painel mostra o botão **Ver o trade ao vivo no gráfico**; em Posições, cada linha tem o link **ao vivo**. A página `/trade/<id>` mostra as velas do mercado onde a ordem foi executada (no testnet, o livro do Spot Testnet), desde 30 minutos antes da entrada, com as linhas de entrada, stop atual, alvo ou maior preço do stop móvel, e marcadores de entrada e saída. Atualiza a cada 5 segundos enquanto a posição está viva; o servidor guarda as velas por 5 segundos e recua quando o peso da API está alto, para não tirar requisições de stops e saídas.

## Como o robô decide

As regras seguem o que tem evidência publicada para quem só compra no spot. Nada aqui garante lucro: os números de backtest dos artigos são otimistas, o efeito de momento em cripto enfraqueceu depois do lançamento do futuro de BTC, e cada estratégia continua precisando passar no teste histórico antes de operar.

- **Seguir a tendência, só na compra.** O momento de série temporal é o efeito mais robusto em cripto, e funciona no long-only; vender a descoberto não acrescentou retorno significativo. A estratégia **Momentum** compra quando a alta dos últimos 20 dias (120 velas de 4h) é positiva, a EMA 20 está acima da EMA 50, o preço está acima da EMA 50 e o ADX é pelo menos 20. A confiança é essa alta dividida pela oscilação no mesmo período. O stop fica a 2,5 ATR e a saída é o stop móvel ou a EMA 20 cruzando abaixo da 50: deixar o vencedor correr e cortar o perdedor cedo.
- **Escolher onde olhar pelo momento relativo.** O fator de momento entre moedas existe, mas como estratégia própria é fraco depois dos custos. Por isso ele só escolhe os pares: a seleção automática pega os 40 pares USDT mais líquidos (sem stablecoins e tokens alavancados) e fica com os de melhor momento ajustado ao risco, a alta de 28 dias dividida pelo desvio dos retornos diários, só os positivos. Uma compra ainda exige um setup Pronto de uma estratégia validada.
- **Risco fixo por trade.** O tamanho sai do stop: a perda no stop é `risk_per_trade_pct` do capital (1% por padrão, no máximo 2%), limitada ao teto por ordem. Como o stop vem do ATR, um ativo mais volátil compra menos.
- **Risco total limitado.** A soma da perda até o stop de todas as posições vivas, mais a nova, fica abaixo de `max_open_risk_pct` do capital configurado (6% por padrão, no máximo 20%). Uma posição cujo stop móvel já passou do preço médio não conta; uma entrada ainda pendente conta como um trade inteiro.
- **Várias posições, dentro do saldo.** A cada ciclo o robô lê o USDT livre. No modo automático ele percorre as oportunidades Prontas da maior nota para a menor e aceita uma por par (diversificação: duas estratégias nunca compram o mesmo par), dentro de `max_positions`, do risco total e de 95% do USDT livre já descontada a taxa. Se o caixa não cobre o tamanho inteiro, a ordem diminui até o que cabe, desde que fique acima do mínimo da Binance; se não, a linha fica Bloqueada com o motivo. No modo manual cada linha é conferida sozinha, porque você escolhe uma por vez. Sem leitura do saldo, nenhuma entrada sai.
- **Lucro mínimo esperado.** `min_profit_usd` (0 por padrão) recusa a entrada cujo tamanho vezes o retorno líquido até o alvo fica abaixo desse valor. Com o stop móvel o alvo é só referência. Lucro maior por trade só vem de ordem maior, e a perda no stop cresce na mesma proporção.
- **Proteções depois de perder** (como `CooldownPeriod` e `StoplossGuard` do Freqtrade). Um par que fechou com prejuízo fica sem entrada nova por `pair_cooldown_minutes` (120 por padrão). Com `loss_guard_trades` perdas (3 por padrão) nas últimas 6 horas, nenhuma entrada nova sai até a mais antiga sair da janela. Zero desliga cada uma. Isso corta a sequência de re-entradas no mesmo par que vira prejuízo em mercado ruim.
- **Mesma regra ao vivo e no histórico.** As estratégias e as saídas por sinal decidem só com velas fechadas, como o backtest; a vela ainda aberta só serve de preço. No backtest, stop e alvo disparam pela mínima e pela máxima da vela, como as ordens na corretora, e o stop vence quando a mesma vela toca os dois.
- **Folga para o stop.** A entrada usa no máximo 90% do teto por ordem: o portão avalia o stop pelo bid, e uma alta de até ~11% ainda deixa o stop novo dentro do teto. Acima disso o stop móvel para de subir e o stop atual fica no livro.
- **Estabilidade no histórico.** Além de Sharpe acima de 1, drawdown abaixo de 20% e 10 trades, a validação exige que a primeira e a segunda metade dos trades, pela data de saída, lucrem cada uma. Um resultado bom só numa fase do mercado é o sinal mais comum de sorte; o motivo da reprovação aparece no painel.
- **Kelly fracionário** (como o `KELLY_FRACTION` do jevymarket). Quando a estratégia tem taxa de acerto estimada `p`, o risco no stop passa a ser o menor entre `risk_per_trade_pct` e `kelly_fraction` (0,25 por padrão) de `p − (1 − p) / b`, com `b` o ganho até o alvo por unidade de risco; Kelly ≤ 0 recusa a entrada como "sem vantagem estimada". `b` é o ganho do alvo, e com o stop móvel o alvo não é saída, então é um teto. Com os padrões do modo automático (acerto mínimo de 60% e ganho/risco de 1,5) o Kelly fica acima de 0,33 e o teto não limita; ele pesa nas entradas manuais com acerto baixo e quando o acerto mínimo é reduzido. Linhas de teste não usam Kelly.
- **Limite de entradas por ciclo** (`max_entries_per_cycle`, 2 por padrão, como o `MAX_TRADES_PER_RUN` do jevymarket): o modo automático abre no máximo esse número de posições por ciclo e deixa o resto para o próximo, evitando comprar vários pares de uma vez no mesmo movimento.
- **Calibração** (como o `stats` do jevymarket): cada posição grava a taxa de acerto estimada na entrada; em Desempenho, uma tabela compara, por faixa, o estimado com a parte que realmente lucrou. Linhas de teste ficam de fora.
- **Capital real.** O tamanho usa o menor entre o capital configurado e o USDT livre mais o valor das posições do robô. Patrimônio, perda diária e drawdown continuam medidos sobre o capital configurado, então um saque não vira um falso drawdown.

Ficou de fora, por falta de evidência ou por risco de execução: filtro de mercado pela média de 200 do BTC (sem backtest publicado), entradas com ordem limitada para pagar taxa de maker (risco de não executar) e saída chandelier.

Fontes: [Freqtrade, proteções](https://www.freqtrade.io/en/stable/plugins/#protections); [NautilusTrader, motor de risco](https://nautilustrader.io/docs/python-api-latest/risk.html); [Hummingbot, kill switch](https://hummingbot.org/client/global-configs/kill-switch/); [Coin Bureau, risco por trade e calor da carteira](https://coinbureau.com/guides/risk-management-strategies-crypto-trading); [Rozario et al., arXiv 2009.12155](https://arxiv.org/abs/2009.12155v1); [Liu, Tsyvinski e Wu, NBER w25882](https://www.nber.org/papers/w25882.pdf); [Time series and cross sectional momentum in the cryptocurrency market, AUT](https://acfr.aut.ac.nz/__data/assets/pdf_file/0009/918729/Time_Series_and_Cross_Sectional_Momentum_in_the_Cryptocurrency_Market_with_IA.pdf); [Man Group, Crypto: too hot to handle](https://www.man.com/insights/crypto-too-hot-to-handle); [Volatility-managed crypto momentum, Springer 2025](https://link.springer.com/article/10.1007/s11408-025-00474-9).

## Day trade

Duas estratégias de janela curta, que ligam e desligam como as outras: **Day · Momento intradiário** (`intraday_momentum`) e **Day · Pullback na VWAP** (`vwap_pullback`). O desenho, as fontes e a revisão do architect estão em [docs/plano-day-trade.md](docs/plano-day-trade.md).

- **Entrada:** só compra. Entra numa vela fechada de 15 min, e só com as velas fechadas de 1 h em alta (EMA 20 acima da 50, fechamento acima da 50). Não entra entre 03:00 e 07:00 UTC, as horas de livro mais fino.
- **Stop:** 1,5 ATR do 15 min, com piso de 1%. Abaixo disso, o custo de ida e volta (cerca de 0,25%) comeria mais de um quarto do risco.
- **Saída:**
  - No TP1 (1,5 vez o risco), metade é vendida e o stop do resto sobe para o preço de entrada mais 0,2%.
  - O que sobrar sai em até 6 horas.
  - O "equilíbrio" é um stop-limit com folga de 0,5%: num movimento rápido ele pode executar cerca de 0,3% abaixo da entrada, ou não executar num gap.
- **Filtros:** o score e os filtros de custo e de ganho/risco medem o TP1, o alvo em R do trade. Só o gate do backtest mede o resultado real das duas metades.
- **Ordens:** um day trade gasta até 5 ordens (entrada, stop, TP1, novo stop, saída). Com uma estratégia day ligada, o mandato conta 6 por entrada.
- **Validação:** 90 dias de velas de 15 min e de 1 h, com as mesmas regras de saída. O gate é mais exigente que o do swing, porque com muitos trades o Sharpe por trade fica fácil:
  - Sharpe dos retornos diários acima de 1;
  - pelo menos 30 trades;
  - retorno médio líquido de pelo menos 0,10% por trade;
  - drawdown abaixo de 20%;
  - as duas metades do histórico lucrando.

  A validação vale só para as regras que replicou.
- **Preset:** "Aplicar preset de day trade" (Controles) mostra o que muda e só então grava:
  - liga as estratégias day e desliga as swing;
  - risco de 0,5% por trade;
  - perda diária de 2%;
  - até 8 trades por dia;
  - pausa de 60 min no par depois de uma perda;
  - alvo de pelo menos 3x o custo;
  - reconfirma o mandato.

**O que a evidência diz.** A maioria dos day traders perde depois dos custos: menos de 1% lucra de forma previsível em Taiwan, e 97% de quem insistiu perdeu no Brasil. Nenhum estudo revisado por pares mostra day trade só comprado em cripto spot lucrativo depois de 0,2% por ida e volta.

O backtest de 90 dias até 2026-10-16 confirmou isso nas duas estratégias:
- **Pares do testnet (BTC, ETH, SOL):** todos perderam.
- **Pares voláteis (AVAX, ZEC, INJ, NEAR, STRK, TIA):** só o momento intradiário em INJ passou, 1 de 18 testes, o que se espera do acaso.

O robô não opera de verdade o que não passa no gate; no testnet, o modo teste deixa exercitar o fluxo.

## Operação

1. Python 3.12 e Node 22. Na raiz:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest
cd ui
npm install
npm run dev
```

2. Noutro terminal, com a raiz de runtime explícita (não use a pasta do código para chaves):

```powershell
$env:BOT_RUNTIME_ROOT = "$env:USERPROFILE\.the-trader-bot"
python -m bot
```

3. A interface fica em `http://127.0.0.1:5173` e a API em `http://127.0.0.1:8000`. Na primeira subida a API grava um token em `%USERPROFILE%\.the-trader-bot\api-token` (o `BOT_RUNTIME_ROOT`). O proxy do Vite lê esse arquivo e manda o token em cada chamada; o navegador nunca o vê. A API recusa pedidos sem o token, com outro `Host`, com outra `Origin` ou, nas escritas, sem o cabeçalho `X-Bot-Client: panel`. Rode o Vite com o mesmo `BOT_RUNTIME_ROOT` da API.

4. Primeira configuração, na tela Controles:

- Grave as chaves de **Spot Testnet** em `%USERPROFILE%\.the-trader-bot\binance.json` ou nas variáveis `BINANCE_TESTNET_API_KEY` e `BINANCE_TESTNET_API_SECRET`. A interface não recebe chave. A chave de produção, quando existir, precisa de restrição de IP e sem permissão de saque.
- Defina o capital e confirme o mandato. O bot grava `mandate.json` com instrumentos só `crypto`, alavancagem 1 e quatro ordens por entrada permitida no dia (entrada, stop, saída e um stop recolocado).
- No Spot Testnet, ajuste em `binance.json`: `testnet_audit_only: false`, `testnet_max_order_notional` (por exemplo `{"USDT": "100"}`) e `testnet_max_daily_operations` com folga para três ordens por entrada. O padrão do conector é 2 operações por dia, o que não cobre nem uma entrada.
- Ligue uma estratégia e clique em "validar no histórico" (`POST /bot/strategies/{id}/validate`). O teste roda em segundo plano (alguns minutos) sobre os pares configurados: 180 dias de velas de 1h para Trend, 365 dias de 4h para Momentum e 60 dias de 15m para Mean Reversion, com a mesma janela de 300 velas do robô ao vivo. O painel mostra o progresso. Ela só opera se passar: Sharpe acima de 1, drawdown abaixo de 20% e pelo menos 10 trades.
- Pares: "usar os 20 mais líquidos" troca os 5 pares padrão pelos 20 pares USDT de maior volume em 24h (sem stablecoins); pares com posição aberta nunca saem. A coleta roda em paralelo para caber no ciclo de 60 s.
- Seleção automática (`auto_select`, desligada por padrão): uma vez por hora o robô troca a lista pelos pares mais promissores (15 por padrão, de 5 a 20). Enquanto ela está ligada, a lista não muda pelo painel. No testnet, a seleção, o botão dos mais líquidos e a lista editada só aceitam pares que o Spot Testnet lista (`testnet.binance.vision`), porque a análise usa o mercado real e as ordens vão para o testnet.
- Modo teste do Spot Testnet (só testnet): setups de compra que falharam num filtro de qualidade ou cuja estratégia não está validada aparecem como Pronta com a marca **teste**, para você testar compra, stop e saída. Saem pelo botão executar e, com o trade automático ligado, também pelo ciclo (da maior nota para a menor, dentro do saldo e dos limites). Não entram na taxa de acerto nem no decaimento. Produção ignora a opção.
- Inicie. O ciclo é de 60 segundos. O selo cinza significa Spot Testnet.

5. Parar tudo (`POST /bot/halt` ou o botão) para o ciclo e grava o HALT, que bloqueia toda ordem nova. Com "zerar no halt" marcado, o robô vende as posições **antes** de gravar o HALT; o que não vender fica com o stop recolocado. Sem a opção, as posições ficam abertas com o stop na Binance. Retomar é `POST /bot/resume`, manual. O ciclo também entra em halt sozinho depois de três falhas seguidas e quando uma posição fica sem stop e sem como sair.

6. Produção é outro diretório: `testnet/` e `production/` têm store, journal, mandato, HALT e ledger próprios. `binance.json`, `bot.json` e `api-token` ficam na raiz. O interruptor volta para testnet se qualquer pré-condição faltar, e o motivo aparece na tela. As liberações G1 a G4 só mudam editando `bot.json`; a API não as grava. Ao aprovar, o robô grava o mandato de produção com o capital de produção (10% do alocado). Sair de produção exige não haver posição de produção aberta: com "zerar no halt" marcado o robô vende antes; se algo continuar aberto, a troca é recusada, porque o processo em testnet não acompanharia aquelas posições.

O mandato limita a exposição das posições abertas pelo robô, avaliadas pela cotação atual. O resto da conta (caixa em USDT, moedas que já estavam lá) não conta como exposição do robô.

`binance.json` continua sendo o arquivo de credencial do conector portado. `bot.json` guarda modo, capital, estratégias e limites, e esses limites nunca podem afrouxar o mandato.

## Desempenho e concorrência

Medido no testnet, a partir do Brasil (cerca de 270 ms por requisição à Binance):

- **Coleta fora da trava do ciclo.** A leitura de mercado roda sem segurar a trava; só o cálculo e as ordens a seguram. Um `PUT /bot/config` durante um ciclo caiu de até 12,3 s para 77 ms.
- **Coleta paralela por requisição.** Até 16 requisições ao mesmo tempo, com um cliente HTTP reaproveitado (keep-alive) e o livro de todos os pares em uma chamada só. A coleta de 15 pares caiu de 5,7 s para cerca de 2,2 s. Um par que falha fica de fora do ciclo e é registrado no ledger (`collect_partial`); se ele tem posição aberta, o ciclo inteiro é recusado, para o stop e a saída nunca usarem preço velho.
- **Tabela de mercados da Binance em cache** por uma hora, por host e tipo de cliente, com o ajuste de relógio junto. Antes, cada cotação e ordem baixava o `exchangeInfo` de novo.
- **Journal verificado por incremento.** A leitura confere só as linhas novas contra o último hash conhecido. A cadeia inteira é refeita a cada 5 minutos e sempre antes de registrar uma ordem nova.
- **Ledger com rotação** a 1 MiB: anexar não cresce com o histórico. O relatório diário e a verificação na partida (em segundo plano) conferem a cadeia com os arquivos rotacionados.
- **SQLite** com `busy_timeout` de 10 s e journal de rollback (o WAL precisa de memória compartilhada que o bind mount do Docker Desktop não garante). As decisões só são gravadas quando mudam: a mesma oportunidade lida a cada minuto do mesmo candle vira uma linha, não 240. Como o motivo da reversão à média traz o RSI, ela grava uma linha nova quando o RSI muda. Decisões, sinais e pontos de patrimônio com mais de 30 dias são apagados uma vez por dia; o pico do patrimônio fica guardado à parte, então o drawdown não muda.
- **Backtest.** As estratégias recusam primeiro pelos testes baratos (rompimento e volume, RSI, tendência das médias) e só então calculam ADX e ATR; o resultado é idêntico bit a bit (`tests/test_bot_strategy_golden.py`), e a avaliação ficou de 3 a 14 vezes mais rápida. Cada par roda num processo separado (até 4, `spawn`), porque o cálculo é Python e pandas e threads dividiriam a mesma trava do interpretador. A janela é de 299 velas fechadas, a mesma que o ciclo vê, e fica gravada nos parâmetros da validação.

## Docker

```bash
docker compose up -d --build
```

Sobe a API (`python -m bot`, porta 8000) e o painel (build do Vite servido por `vite preview`, porta 5173), as duas só em 127.0.0.1. Os dois contêineres montam a pasta do operador (`~/.the-trader-bot`, ou a de `BOT_HOME`) em `/data`: chaves, `bot.json`, `api-token` e os stores de testnet e produção continuam no mesmo lugar que fora do Docker. O painel monta só leitura, para ler o token. O proxy do painel fala com `http://api:8000` e envia o Host `127.0.0.1:8000`, que é o único que a API aceita. Pare a API rodando fora do Docker antes: dois processos na mesma pasta negociariam em dobro. No Linux, crie a pasta antes do primeiro `up` e rode com o dono dela: `mkdir -p ~/.the-trader-bot && BOT_UID=$(id -u) BOT_GID=$(id -g) docker compose up -d --build`; no Docker Desktop (Windows, macOS) não é preciso. `docker compose logs -f api` mostra o ciclo; `docker compose down` para tudo, e as posições continuam protegidas pelo stop na Binance.

## Testes

```powershell
pytest
```

A suíte cobre store e migração, coletor (dado velho, relógio, par sem perpétuo), score, painel e autenticação da API, cada limite de risco com PnL real, o gate de mandato com posições precificadas, entrada, stop, saída parcial, stop recusado, orçamento de ordens, reconciliação, halt com zerar antes do flag, idempotência, backtest, relatório contra o ledger, decaimento de Sharpe e o perfil de produção.

## Recuperação do journal de execução

Uma queda no meio de um envio, um cancelamento de resultado desconhecido ou uma contradição entre o journal e a Binance bloqueiam **toda** ordem nova, inclusive stops e saídas, até serem resolvidos. Os stops que já estão na Binance continuam valendo. O painel mostra o aviso e `GET /bot/status` traz o campo `journal`. A API nunca resolve esses estados; só o operador, num terminal, com o robô parado:

```powershell
python -m bot.ops status
python -m bot.ops reconcile
python -m bot.ops confirm-absent order:<id> --reason "processo que enviou caiu"
python -m bot.ops ack-conflict conflict:<id> --reason "investigado"
python -m bot.ops adopt-checkpoint --reason "journal restaurado do backup"
```

`--mode testnet|production` escolhe o ambiente; o padrão é o do `bot.json`. `confirm-absent` só marca a ordem como ausente se a Binance confirmar que ela não existe. `adopt-checkpoint` recusa quando o checkpoint aponta registros que sumiram do journal: restaure o backup primeiro. O robô reconcilia sozinho a cada ciclo enquanto houver pendência, mas um `SUBMISSION_PENDING` (envio que nunca voltou) sempre exige `confirm-absent`.

Se um ledger (`bot-ledger.jsonl`, `binance-execution-journal.jsonl`) terminar sem quebra de linha depois de uma queda, ele recusa novos registros. Faça backup, apague a última linha incompleta e rode `adopt-checkpoint`.
