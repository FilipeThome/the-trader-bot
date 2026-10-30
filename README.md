# Bot de trading automático

Serviço local que analisa pares USDT na Binance e só envia ordem pelo order gate portado do Vibe-Trading. A interface lê o estado e manda pausar, parar ou ajustar limites. Ela não fala com a exchange.

Isto não é recomendação de investimento. Nenhuma estratégia garante lucro. O retorno do painel é líquido de taxa, slippage e funding.

## O que a v1 faz

- Spot em BTC, ETH, SOL, BNB e XRP. Futuros USD-M são só leitura: a carrega de funding aparece no painel, com o retorno estimado ao dia, e não executa.
- Quatro famílias: tendência (1h e 4h), momento (4h), reversão à média (15m e 1h) e carrega de funding (sinal apenas).
- Testnet por padrão (`binance-paper-trade`). Produção (`binance-live-trade-bot`) só depois dos gates, de 30 dias com PnL líquido positivo e 30 trades com expectativa positiva em R, de um mandato restrito a cripto e da palavra `PRODUCAO`.
- Nenhuma ordem sem mandato, latch e registro no journal antes do envio. Halt não se desarma sozinho.
- Toda compra ganha um STOP_LOSS_LIMIT na Binance logo depois da execução, então a posição fica protegida mesmo com o robô desligado. Alvo e saída da estratégia são checados a cada ciclo: o robô cancela o stop e vende com ordens LIMIT IOC.
- **Entrada LIMIT IOC.** A compra é uma ordem LIMIT IOC 0,2% acima do ask, com a quantidade em moeda base. O que executa abre a posição; o resto é cancelado na hora, e sem execução a linha some. Sem ask, não há compra. Stop e alvo são recalculados sobre o preço médio executado. Cada posição grava o slippage de entrada e de saída, o resultado em R, o MAE e o MFE; Posições mostra o R atual. Robôs DCA e grid continuam comprando a mercado.
- **Stop atravessado.** Se o último negócio chega ao stop (é o que dispara um STOP_LOSS_LIMIT) e a ordem fica no livro sem executar, com o bid abaixo do limite do stop-limit, o robô cancela o stop e vende com um IOC 0,5% abaixo do bid. Se sobrar moeda, tenta outro IOC 2% abaixo. Se ainda sobrar, entra em halt. Se o stop não sai do livro ou a venda não pode sair (cancelamento sem confirmação, compra parada de um robô sem leitura, ordens do dia esgotadas), o stop continua no livro e o robô tenta de novo no ciclo seguinte; no terceiro ciclo seguido, entra em halt. Só um bloqueio de escrita (halt ativo, mandato inválido) espera sem contar. Não há venda a mercado como último recurso: seria um caminho só de produção, que o Spot Testnet não exercita.
- Spot não vende a descoberto. Um sinal de venda sem posição aparece como potencial e nunca vira ordem.
- Stop móvel (ligado por padrão): o stop na Binance sobe com o maior preço, na mesma distância da entrada, e nunca desce. Cada posição grava a regra de saída com que abriu e segue essa regra até fechar, mesmo que a opção mude depois. Uma estratégia validada com outra regra (ou antes desta versão) volta a somente sinal até ser validada de novo. O alvo fixo deixa de fechar a posição; a saída vem do stop ou do sinal da estratégia. O stop só sai do livro quando o novo pode entrar: sem halt, mandato válido, sem pressão de peso da API, com folga abaixo do menor entre o bid e o último preço e com uma ordem do dia reservada para cada posição aberta. A folga é metade da distância do stop móvel, entre 0,5% e 1%: o stop novo não nasce colado no preço, e um `trailing_tp` com distância curta continua subindo. Cada troca de stop mede o tempo sem stop no livro; Desempenho mostra média, p95 e máximo dos últimos 30 dias, por tipo.
- Modo de entrada:
  - **Manual** (padrão): o ciclo nunca compra sozinho. Você executa uma oportunidade Pronta pelo painel e o robô cuida do stop e das saídas; o ciclo liga sozinho nessa hora. Compras feitas direto na Binance não são acompanhadas.
  - **Automático**: o ciclo também compra as oportunidades Prontas cuja **taxa de acerto estimada** da estratégia passa do mínimo (60% por padrão, entre 50% e 95%). A taxa é `(acertos + 1) / (trades + 2)` sobre os trades do teste histórico com a mesma regra de saída e as posições reais fechadas, e só existe com pelo menos 20 trades. É histórico, não probabilidade garantida. Ao trocar para automático o ciclo para e exige Iniciar.

A origem de cada módulo portado está em [ORIGIN.md](ORIGIN.md).

## Alvo e stop fixos

Em Controles, **Alvo e stop fixos** recebe o ganho e a perda em porcentagem do preço de entrada (por exemplo, +3% e −1,5%). Ligado, vale para todas as estratégias, day trade incluído, e substitui o stop, o alvo e as saídas de cada uma, o stop móvel e o TP1/equilíbrio/limite de 6 h do day trade.

- **Entrada:** como antes, uma oportunidade Pronta de uma estratégia validada. No automático, a taxa de acerto estimada precisa passar do ponto de equilíbrio da regra, `perda / (perda + ganho líquido)`, mais 3 pontos. Com +3% e −1,5%, isso fica perto de 38%, não nos 60% do swing. O filtro de ganho/risco não vale aqui, porque você escolheu o ganho e a perda; o filtro de custo continua.
- **Stop:** um STOP_LOSS_LIMIT na Binance à perda escolhida abaixo do **preço médio executado**, não do preço da análise.
- **Alvo:** o ciclo vende com LIMIT IOC quando o último preço de 15 min chega ao alvo, também calculado sobre o preço médio executado. Não há saída por sinal da estratégia nem limite de tempo.
- **Score:** o retorno esperado da linha nunca passa do que a própria estratégia estima, então um alvo largo não faz um sinal fraco parecer forte.
- **Limites:** alvo de 1% a 100% (abaixo de 1% o custo de ida e volta comeria o ganho e nenhuma linha ficaria Pronta), perda de 0,5% a 50%.
- **Validação:** os valores fazem parte da regra (`fixed:<alvo em pontos-base>:<perda em pontos-base>`). Mudar qualquer um exige validar de novo, e a taxa de acerto só mistura trades da mesma regra. No histórico, o stop dispara pela mínima da vela e vence quando a mesma vela toca os dois; o alvo dispara no **fechamento** da vela, como o ciclo, que vende pelo último preço e não deixa ordem no livro. Toda regra paga 0,03% de slippage por perna. Estratégias day continuam sem entrar entre 03:00 e 07:00 UTC e são julgadas pelo gate day.
- **Automático de ponta a ponta:** ao mudar o alvo e o stop, ou ao ligar uma estratégia, o robô valida sozinho no histórico, uma estratégia por vez, cada estratégia ligada que ainda não tem resultado para a regra atual. Com o modo de entrada **Automático** e Iniciar, o robô escolhe, compra, protege e vende sem clique, e só opera o que passou no teste.
- Posições abertas seguem a regra com que abriram.

## Moedas novas

Um par listado há pouco tempo não tem o histórico que as análises longas pedem, então o robô olha o dia dele:

- **Seleção automática:** um par com menos de 29 velas diárias fechadas (listado há menos de um mês) é ranqueado pelas últimas 24 velas de 1 h, com o mesmo cálculo: retorno dividido pelo desvio dos retornos. Ele precisa de pelo menos 12 h de negociação. Antes, esses pares nunca entravam.
- **Day trade:** com menos de 60 velas de 1 h (listado há menos de 2,5 dias), o filtro de tendência do 1 h lê o próprio dia no 15 min: EMA 20 acima da EMA 50 e fechamento acima da VWAP do dia. Pares com histórico continuam usando o 1 h; os setups deles são idênticos bit a bit aos de antes.
- A reversão à média no 15 min já operava com 10 h de velas. Tendência (1 h, 60 velas) e Momentum (4 h, semanas de histórico) continuam exigindo o histórico longo, por desenho.

## Trade ao vivo

Ao executar uma oportunidade, o painel mostra o botão **Ver o trade ao vivo no gráfico**; em Posições, cada linha tem o link **ao vivo**. A página `/trade/<id>` mostra as velas do mercado onde a ordem foi executada (no testnet, o livro do Spot Testnet), desde 30 minutos antes da entrada, com as linhas de entrada, stop atual, alvo ou maior preço do stop móvel, e marcadores de entrada e saída. Atualiza a cada 5 segundos enquanto a posição está viva; o servidor guarda as velas por 5 segundos e recua quando o peso da API está alto, para não tirar requisições de stops e saídas.

## Como o robô decide

As regras seguem o que tem evidência publicada para quem só compra no spot. Nada aqui garante lucro: os números de backtest dos artigos são otimistas, o efeito de momento em cripto enfraqueceu depois do lançamento do futuro de BTC, e cada estratégia continua precisando passar no teste histórico antes de operar.

- **Seguir a tendência, só na compra.** O momento de série temporal é o efeito mais robusto em cripto, e funciona no long-only; vender a descoberto não acrescentou retorno significativo. A estratégia **Momentum** compra quando a alta dos últimos 20 dias (120 velas de 4h) é positiva, a EMA 20 está acima da EMA 50, o preço está acima da EMA 50 e o ADX é pelo menos 20. A confiança é essa alta dividida pela oscilação no mesmo período. O stop fica a 2,5 ATR e a saída é o stop móvel ou a EMA 20 cruzando abaixo da 50: deixar o vencedor correr e cortar o perdedor cedo.
- **Escolher onde olhar pelo momento relativo.** O fator de momento entre moedas existe, mas como estratégia própria é fraco depois dos custos. Por isso ele só escolhe os pares: a seleção automática pega os 40 pares USDT mais líquidos (sem stablecoins e tokens alavancados) e fica com os de melhor momento ajustado ao risco, a alta de 28 dias dividida pelo desvio dos retornos diários, só os positivos. Uma compra ainda exige um setup Pronto de uma estratégia validada.
- **Tamanho pelo risco, sob tetos separados.** O tamanho sai do stop: a perda no stop é `risk_per_trade_pct` do capital (1% por padrão, no máximo 2%), ou menos quando o Kelly limita. Esse valor dividido pela distância do stop dá o tamanho. Depois vale o menor destes tetos:
  - **teto por ordem:** `max_order_notional_pct` do capital × 90%. É só um teto de segurança;
  - **teto da corretora:** o do mandato e o do Spot Testnet (`binance.json`), × 90%;
  - **volatilidade:** uma ordem exposta a um ATR14 diário custa no máximo `vol_cap_pct` do capital (1% por padrão, 0 desliga). Um par com ATR diário de 10% compra no máximo 10% do capital; BTC e ETH quase nunca chegam nele;
  - **exposição total:** o que sobra de `max_exposure_pct` (60%), ou do alvo de volatilidade, menos o que já está aberto pelo último preço. A ordem diminui até caber; sem espaço, é recusada.

  Quando a oscilação de 7 dias dos retornos de 4 h passa de `vol_throttle_ratio` (2 por padrão, 0 desliga) vezes a de todas as velas de 4 h lidas (cerca de 50 dias), a ordem cai pela metade. Como o stop vem do ATR, um ativo mais volátil compra menos.
- **Teto por ordem de 25%** numa instalação nova. Um `bot.json` já gravado mantém o valor salvo (2% nas instalações antigas) até você mudar. O teto entra no mandato: até reconfirmar, o teto do mandato antigo vence, e Controles mostra os dois lado a lado. Com os padrões, um stop de 2% dá 22,5% do capital (vence o teto por ordem) e um stop de 8% dá 12,5% (vence o risco por trade). Com o teto de 2%, toda ordem fica perto de 1,8% do capital e o risco por trade não limita nada. Cada oportunidade mostra o limite que venceu ("teto por ordem", "risco por trade", "volatilidade (ATR diário)" e outros) e o valor de cada teto.
- **Alvo de volatilidade da carteira** (`target_vol_annual`, 60% por padrão, 0 desliga). A volatilidade anual da carteira, em dólares, é estimada com retornos de 1 h (média exponencial com meia-vida de 72 h, com covariância) e não passa de 60% do capital. Só limita entradas novas: nada é vendido para rebalancear. Com menos de 48 retornos alinhados, não limita.
- **Risco total limitado.** A soma da perda até o stop de todas as posições vivas, mais a nova, fica abaixo de `max_open_risk_pct` do capital configurado (6% por padrão, no máximo 20%). Uma posição cujo stop móvel já passou do preço médio não conta; uma entrada ainda pendente conta como um trade inteiro.
- **Correlação e cluster do BTC.** Uma entrada é recusada quando já há `max_correlated` (2) posições com correlação acima de `correlation_threshold` (0,80) com ela, contando as vivas e as linhas já aceitas no mesmo ciclo. Um par está no cluster do BTC quando é o BTC ou quando a correlação dos retornos de 1 h com o BTC passa de `cluster_corr` (0,70). O risco até o stop do cluster, vivas e aceitas no ciclo somadas, fica em metade de `max_open_risk_pct` (3% com os padrões). A linha mostra o cluster.
- **Várias posições, dentro do saldo.** A cada ciclo o robô lê o USDT livre. No modo automático ele percorre as oportunidades Prontas da maior nota para a menor e aceita uma por par (diversificação: duas estratégias nunca compram o mesmo par), dentro de `max_positions`, do risco total e de 95% do USDT livre já descontada a taxa. Se o caixa não cobre o tamanho inteiro, a ordem diminui até o que cabe, desde que fique acima do mínimo da Binance; se não, a linha fica Bloqueada com o motivo. No modo manual cada linha é conferida sozinha, porque você escolhe uma por vez. Sem leitura do saldo, nenhuma entrada sai.
- **Lucro mínimo esperado.** `min_profit_usd` (0 por padrão) recusa a entrada cujo tamanho vezes o retorno líquido até o alvo fica abaixo desse valor. Com o stop móvel o alvo é só referência. Lucro maior por trade só vem de ordem maior, e a perda no stop cresce na mesma proporção.
- **Proteções depois de perder** (como `CooldownPeriod` e `StoplossGuard` do Freqtrade). Um par que fechou com prejuízo fica sem entrada nova por `pair_cooldown_minutes` (120 por padrão). Com `loss_guard_trades` perdas (3 por padrão) nas últimas 6 horas, ou com essas perdas somando `loss_guard_r` R ou mais (2 por padrão, de 0 a 20; R é o prejuízo dividido pelo risco até o stop na entrada), nenhuma entrada nova sai até as perdas mais antigas saírem da janela. Zero desliga cada uma. Isso corta a sequência de re-entradas no mesmo par que vira prejuízo em mercado ruim.
- **Mesma regra ao vivo e no histórico.** As estratégias e as saídas por sinal decidem só com velas fechadas, como o backtest; a vela ainda aberta só serve de preço, e o painel a marca como "prévia da vela aberta". A saída por sinal lê o timeframe com que a posição abriu: uma tendência de 4 h sai pelas velas de 4 h. Posições antigas, sem timeframe gravado, usam as velas de antes. No backtest, o stop dispara pela mínima da vela, no preço do stop ou na abertura quando a vela abre abaixo dele; o alvo da regra `target` vende no fechamento da vela que fecha no alvo, como o ciclo, nunca num pavio; o stop vence quando a mesma vela toca os dois. Cada perna paga 0,1% de taxa e 0,03% de slippage.
- **Histerese na saída** (`exit_hysteresis`, ligada por padrão). Tendência e momento só saem pelo sinal quando a EMA 20 fica 0,5% abaixo da EMA 50 ou abaixo dela em 2 velas fechadas seguidas, não no primeiro cruzamento. Faz parte da validação. Cada posição grava a regra com que abriu; posições antigas saem sem histerese.
- **Indicadores com média simples.** ATR, ADX e RSI usam média móvel simples, não a suavização de Wilder. É uma decisão: ao vivo e histórico calculam os mesmos números, e os digests-ouro e as validações gravadas continuam valendo. Trocar mudaria tudo isso sem evidência de que opera melhor.
- **Folga para o stop.** A entrada usa no máximo 90% do teto por ordem: o portão avalia o stop pelo bid, e uma alta de até ~11% ainda deixa o stop novo dentro do teto. Acima disso o stop móvel para de subir e o stop atual fica no livro.
- **Validação no histórico (gate v3).** Cada timeframe que a estratégia opera é validado à parte, sobre o conjunto de pares configurado, e um setup só executa se o timeframe dele passou. Com a seleção automática ligada, o conjunto validado é o universo líquido de onde ela escolhe (os 40 pares de maior volume), não os pares escolhidos pelo momento recente: estes subiram justamente no fim do período, e validar neles seria viés de seleção. A escolha por volume no fim do período ainda carrega um resto desse viés e não vê pares deslistados; o painel diz isso. Um par fora do conjunto validado fica só sinal (o modo teste continua funcionando). Nada é reajustado: os parâmetros são fixos. O replay usa só velas fechadas e aplica os limites da carteira aos trades de todos os pares juntos: `max_positions`, um trade por par, a pausa do par depois de perda e a trava de perdas, contando só trades aceitos que já saíram, sem olhar o futuro. Os limites usados ficam gravados e aparecem no painel; mudá-los não exige validar de novo. Os limites que o replay não reproduz (risco aberto total, cluster do BTC, correlação, entradas por ciclo, degraus de drawdown, caixa) fazem do resultado o melhor caso. O gate:
  - os últimos 20% do período, pela data de saída, são um hold-out com pelo menos 5 trades, que não pode perder e precisa ganhar por dia pelo menos metade do que a amostra ganha;
  - o resto é cortado em 4 janelas: 3 precisam lucrar, e a eficiência walk-forward (mediana do ganho por dia de cada janela sobre o das anteriores) fica em pelo menos 0,5;
  - nos trades antes do hold-out: Sharpe acima de 1, drawdown abaixo de 20% e pelo menos 30 trades;
  - Sharpe deflacionado de pelo menos 0,95, com N igual ao número de combinações diferentes (timeframe, regra de saída, pares, filtro de mercado, histerese) testadas desde a última aprovação, mais a atual. Cada rodada fica gravada como tentativa, então testar até passar custa.

  O Monte Carlo reamostra 1000 vezes os trades em R e grava o p95 do drawdown. Ele não reprova na validação: o ciclo confere `p95 × risk_per_trade_pct < max_drawdown_pct` a cada minuto, e as linhas viram só sinal quando não passa. Mudar os limites não exige validar de novo. O PBO (probabilidade de overfitting do backtest) não é calculado: o robô não otimiza parâmetros, então não há família de configurações para comparar; a correção para múltiplos testes é o Sharpe deflacionado com N, e o painel diz isso. Um resultado do gate antigo, de outra versão da estratégia ou de outra regra fica velho: o robô valida de novo na partida e no laço, na hora quando muda o gate, a versão ou a regra, e no máximo uma vez por dia quando muda só o conjunto de pares. Os limiares do `classify` (`score_threshold`, `cost_multiple`, `min_reward_risk`, que vale 0 com saída fixa) também fazem parte da regra: mudar um deles deixa a validação velha, e as linhas da estratégia ficam só sinal, no ciclo e em `/bot/status`, até a nova validação passar. O motivo da reprovação aparece no painel.
- **Degraus de drawdown.** O drawdown é medido do pico do PnL acumulado. Com os padrões:
  - **5%** (`dd_reduce_pct`, 0 desliga): cautela. Risco por trade pela metade e uma entrada por ciclo.
  - **10%** (`max_drawdown_pct`): só reduzir. Nenhuma compra sai: estratégia, manual, armada ou robô. Stops, saídas, stop móvel e TP1 continuam. Não é o HALT: volta sozinho quando o drawdown cai abaixo do primeiro degrau (5%), não antes, para não oscilar na linha.
  - **15%** (`halt_drawdown_pct`): halt, que não se desarma sozinho. Depois de retomar, o robô fica em só reduzir até o drawdown ficar abaixo de 5%.

  Um `bot.json` sem os degraus deriva os dois de `max_drawdown_pct`: metade dele, e 1,5 vez dele entre 15% e 50%. Com um limite de 50% ou mais, o halt fica em 1,5 vez o limite, no máximo 100%. Mudar só `max_drawdown_pct` pelo painel ou pela API recalcula os dois; um degrau de cautela em 0 continua desligado. Um patrimônio ilegível também põe o robô em só reduzir. O painel mostra o degrau numa faixa no topo.
- **Perda diária sobre o patrimônio.** A perda do dia é o patrimônio agora, com as posições abertas pelo último preço, menos o do primeiro ciclo do dia UTC: o prejuízo não realizado conta. Passou de `max_daily_loss_pct` (3%), nenhuma compra sai até 00:00 UTC, nem manual, nem armada, nem de robô. O ciclo continua rodando: stops, stop atravessado, stop móvel, TP1, limite de tempo e vendas dos robôs seguem funcionando, e Iniciar é aceito durante a pausa. Com `flatten_day_on_daily_loss` (desligado por padrão), o robô vende antes as posições de day trade; as swing ficam com o stop. Zerar por padrão venderia no fundo.
- **Filtro de mercado** (`market_filter`, desligado por padrão). Com `btc_ema40d`, nenhuma entrada sai enquanto o último fechamento de 4 h do BTC fica abaixo da EMA de 240 velas de 4 h (cerca de 40 dias), nem quando o BTC não foi lido no ciclo. Com menos de 240 velas fechadas, o filtro deixa passar. Ele faz parte da validação: ligar exige validar de novo, e o backtest o aplica sem olhar o futuro. Para comparar, valide com e sem; as duas rodadas contam como tentativas no Sharpe deflacionado. Fica desligado porque não há backtest publicado que o sustente: precisa ser testado, não assumido.
- **Rebaixamento ao vivo.** A estratégia volta a só sinal quando, em 30 dias corridos com pelo menos 10 trades fechados, o Sharpe diário ao vivo (dia sem trade conta 0) fica abaixo da metade do Sharpe diário do backtest, medido do mesmo jeito, ou quando os últimos 20 trades têm média de R negativa. Com média de R negativa nos últimos 10 trades de um par, dentro de 90 dias, a estratégia para só naquele par. Um par cujo slippage médio de ida e volta nos últimos 20 trades (30 dias, ao menos 10 medidos) passa de `slippage_budget_pct` (0,3%) fica sem entrada. Validar de novo recomeça a contagem da estratégia; os bloqueios por par saem com o tempo. Desempenho mostra o slippage por par.
- **Idade do sinal.** Um sinal com até `max_signal_age_seconds` (120 s) desde o fechamento da vela de decisão entra no automático. Um sinal mais velho só entra enquanto o preço não o deixou para trás: no máximo 0,5 R acima do fechamento da vela do sinal e menos de um stop inteiro abaixo dele (R é a distância do stop). Uma linha fica Pronta pela vela inteira (quatro horas no 4h), então um limite só de relógio deixava o automático entrar apenas nos primeiros minutos depois de cada fechamento, e nunca depois de um reinício; o risco de entrar tarde é perseguir o preço, e é isso que a regra mede. Fora dela, a linha diz o atraso e o deslocamento e fica para o manual. Entradas manuais e armadas mostram a idade e o deslocamento e passam.
- **Calendário macro** (convenção, sem artigo). `macro_events_utc` (vazio por padrão) recebe horários de eventos como CPI e FOMC; entradas de day trade pausam `macro_pause_minutes` (15) antes e depois de cada um. O backtest não reproduz a pausa.
- **Taxa real da conta.** Uma vez por dia o robô lê a taxa da conta na Binance (`/api/v3/account/commission`, padrão mais imposto, sem supor desconto em BNB). Uma falha tenta de novo em 1 h e mantém a última leitura boa. O executor usa essa taxa na margem de venda e na contabilidade, e os robôs também. O score usa a maior entre 0,1% e a da conta; o backtest fica em 0,1%, para a validação não depender da conta.
- **Kelly fracionário** (como o `KELLY_FRACTION` do jevymarket). Quando a estratégia tem taxa de acerto estimada `p`, o risco no stop passa a ser o menor entre `risk_per_trade_pct` e `kelly_fraction` (0,25 por padrão) de `p − (1 − p) / b`, com `b` o ganho até o alvo por unidade de risco; Kelly ≤ 0 recusa a entrada como "sem vantagem estimada". `b` é o ganho do alvo, e com o stop móvel o alvo não é saída, então é um teto. Com os padrões do modo automático (acerto mínimo de 60% e ganho/risco de 1,5) o Kelly fica acima de 0,33 e o teto não limita; ele pesa nas entradas manuais com acerto baixo e quando o acerto mínimo é reduzido. Linhas de teste não usam Kelly.
- **Limite de entradas por ciclo** (`max_entries_per_cycle`, 2 por padrão, como o `MAX_TRADES_PER_RUN` do jevymarket): o modo automático abre no máximo esse número de posições por ciclo (1 no modo cautela) e deixa o resto para o próximo, evitando comprar vários pares de uma vez no mesmo movimento.
- **Taxa de acerto dos setups aceitos.** O replay marca em cada trade se os filtros ao vivo (nota, custo, ganho/risco) o aceitariam. A taxa estimada usada no Kelly e no piso de acerto é a menor entre a de todos os trades e a dos aceitos, mas a dos aceitos só entra com amostra mínima e com os mesmos limiares da config atual; abaixo disso vale a geral. Desempenho mostra as duas.
- **Calibração** (como o `stats` do jevymarket): cada posição grava a taxa de acerto estimada na entrada; em Desempenho, uma tabela compara, por faixa, o estimado com a parte que realmente lucrou. Linhas de teste ficam de fora.
- **Capital real.** O tamanho usa o menor entre o capital configurado e o USDT livre mais o valor das posições do robô. Patrimônio, perda diária e drawdown continuam medidos sobre o capital configurado, então um saque não vira um falso drawdown. O pico guardado é o do PnL acumulado, não o do patrimônio, então mudar o capital também não vira drawdown: o pico passa a ser o capital novo mais esse PnL.

Ficou de fora: saída chandelier, venda a mercado como último recurso do stop atravessado (caminho só de produção, sem teste no Spot Testnet) e OCO no TP1 do day trade (veja Day trade). Compras com ordem limitada no livro existem só para os robôs DCA e grid, por opção.

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
- **TP1 sem OCO:** entre cancelar o stop e recolocá-lo no TP1, metade da posição fica alguns segundos sem stop. O robô mede essa janela (Desempenho). A OCO volta a ser estudada se o p95 da janela do TP1 passar de 2 s em 30 dias, ou se um stop atravessado acontecer durante um TP1.
- **Validação:** 120 dias de velas de 15 min e de 1 h, com as mesmas regras de saída. O gate é o v3 do swing (4 janelas, hold-out e Sharpe deflacionado), com o Sharpe medido nos retornos diários, porque com muitos trades o Sharpe por trade fica fácil:
  - Sharpe dos retornos diários acima de 1;
  - pelo menos 30 trades antes do hold-out;
  - retorno médio líquido de pelo menos 0,10% por trade;
  - drawdown abaixo de 20%;
  - 3 de 4 janelas lucrando, hold-out com pelo menos 5 trades e sem perda, e Sharpe deflacionado de pelo menos 0,95.

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

O backtest de 90 dias até 2026-10-16, ainda com o gate antigo, confirmou isso nas duas estratégias:
- **Pares do testnet (BTC, ETH, SOL):** todos perderam.
- **Pares voláteis (AVAX, ZEC, INJ, NEAR, STRK, TIA):** só o momento intradiário em INJ passou, 1 de 18 testes, o que se espera do acaso.

O robô não opera de verdade o que não passa no gate; no testnet, o modo teste deixa exercitar o fluxo.

## Robôs DCA e grid

Robôs que o operador configura na tela Robôs, como o Spot DCA e o Spot Grid da Binance. Não passam pelo gate de validação: a criação confere o plano inteiro antes da primeira compra (pior caso contra o risco por trade, exposição total, teto por ordem e orçamento de ordens). As ordens deles saem de `instance_orders_per_day` (0 por padrão: sem robôs), reservado no mandato.

- **Máximo de robôs:** `max_instances` (3 por padrão) vivos ao mesmo tempo. Um por par, e nunca num par com posição de estratégia.
- **Pausa entre rodadas:** `cooldown_minutes` (1 min por padrão, como os 60 s da Binance). A recompra do DCA depois do alvo e o reinício do grid depois de vender tudo esperam esse tempo.
- **Prévia:** replay dos últimos 30, 90 e 180 dias de velas de 1 h. Marca em vermelho o grid cujo fechamento ficou fora da faixa em mais de 30% do tempo: grid só rende em lateralização. É histórico, não promessa.
- **Ordens:** compras a mercado e vendas LIMIT IOC, a cada minuto, com o stop na Binance. Só reduzir, halt e pausa diária bloqueiam as compras dos robôs; stop e vendas continuam. Parar um robô vende o que ele tem, sem halt global.
- **Compras paradas no livro** (desligadas por padrão; opção "compra limitada no livro" na página dos robôs): com `resting_buys`, o robô deixa no máximo uma ordem LIMIT GTC de compra, no próximo nível do grid ou na próxima ordem de segurança do DCA. Ela trava USDT, não a moeda, então convive com o stop. Custa 3 ordens do orçamento (colocar, cancelar, recolocar o stop). Na faixa VIP0 maker e taker custam 0,1%: o ganho é o preço (executa no nível, sem spread, e pega toques dentro do minuto), não a taxa. Ao executar, a compra entra na posição e o stop é recolocado; o halt cancela as compras paradas antes de gravar o flag. Elas também saem do livro antes de qualquer venda, ao Parar, e em qualquer bloqueio de compra (relógio, pausa diária, só reduzir); uma execução que chega junto com o cancelamento entra na posição. Pausar o ciclo é recusado (409, "compra limitada ainda no livro") enquanto o cancelamento não se confirma ou a parte comprada ainda não tem stop: o ciclo continua até resolver. Uma compra que a Binance já confirmou só é esquecida quando um cancelamento confirma que a ordem não existe; até lá ela bloqueia vendas e novas compras paradas. Se o stop antigo não sai da corretora em 3 ciclos seguidos depois de uma compra, o robô entra em halt. A prévia de 30/90/180 dias executa a compra no nível quando a mínima da vela o toca.
- **Taxa da conta e o gate.** Com uma taxa de conta menor que a padrão do mercado (faixa VIP), as quantidades de stop e de venda mantêm a margem da taxa padrão, que é a que o order gate confere: sobra um pouco mais de poeira, e o stop nunca é recusado por saldo.

## Consultor DeepSeek

Um consultor opcional pede à [DeepSeek](https://api-docs.deepseek.com) uma segunda opinião sobre cada linha Pronta de estratégia. Ele **aconselha**; quem decide continua sendo o motor determinístico. O desenho, as decisões e a revisão do architect estão em [docs/plano-refatoracao-deepseek.md](docs/plano-refatoracao-deepseek.md).

- **O que ele pode fazer.** Responder `confirm`, `reduce` ou `veto`, com confiança, regime e uma justificativa curta. No modo `filter`, um veto com confiança de pelo menos `min_veto_confidence` (60%) tira a linha do automático; ela continua Pronta para o manual, com o motivo "IA desaconselha: ...". Um `reduce` multiplica o tamanho por um fator entre 0,25 e 1, depois de todos os tetos e do throttle; o teto aparece como "análise IA".
- **O que ele nunca faz.** Criar uma linha, aumentar um tamanho, transformar uma recusa em entrada, mexer no order gate, no mandato, no halt, nos stops ou nas saídas. Robôs DCA e grid, entradas armadas e linhas de teste não são consultados. Entradas manuais mostram o veredito e não são bloqueadas.
- **Por que só reduz.** Uma resposta de LLM não pode ser reproduzida no backtest. Remover ou reduzir entradas é a mesma classe dos filtros que já existem só ao vivo, então a validação continua sendo um teto do que o robô faz.
- **Modos** (`advisor.mode` em Controles): `off` (padrão), `shadow` (consulta e grava, nenhuma ordem muda) e `filter`. Comece em `shadow` e só ligue `filter` quando a calibração mostrar que os vetos evitam perdas.
- **Falha neutra.** Sem chave, sem rede, com orçamento esgotado, resposta inválida ou demora, o robô opera como sem o consultor. Com `require_verdict`, uma linha sem veredito não entra no automático; o painel pede confirmação antes de ligar.
- **Fora da trava do ciclo.** As chamadas rodam numa thread própria com fila limitada; o ciclo só lê o cache e enfileira sem esperar. Uma linha espera o veredito por até `wait_seconds` (45 s), contados entre ciclos, e depois segue. Cada sinal (par, estratégia, timeframe e vela) é consultado uma vez por ambiente: um veredito do testnet nunca vale para uma linha de produção. Um reinício não pergunta de novo: os vereditos já gravados em `advice_log` voltam para o cache antes da nota do primeiro ciclo, sem chamada e sem custo, então um `reduce` gravado antes do reinício também reduz a primeira entrada depois dele. Cada tentativa de chamada tem um prazo total de 45 s, além dos prazos de conexão e de leitura: linhas em branco de uma fila longa não prendem o consultor.
- **Modelo.** `deepseek-flash` (padrão) ou `deepseek-v4-pro`, em modo JSON e sem raciocínio, para latência e custo previsíveis. O prompt leva só números e categorias montados pelo código (setup, indicadores dos timeframes, regime do BTC, nível de risco da conta), nunca texto de fonte externa. Os indicadores usam só velas fechadas até o fechamento da vela do sinal, em todos os timeframes, mesmo quando a consulta acontece horas depois: o modelo não vê o caminho em que a calibração o mede.
- **Custo.** Orçamento diário em `daily_budget_usd` (US$ 1 por padrão), por chave da DeepSeek: conferido antes de cada chamada contra a soma do gasto do dia UTC no testnet e em produção, então sobrevive a reinícios e a trocas de modo. O cabeçalho e Controles mostram essa soma. Uma tentativa cobrada que termina em erro (como a resposta vazia, repetida uma vez) também conta. O custo é uma estimativa pela tabela de preços do código (datada); o valor cobrado é o do console da DeepSeek. Uma chamada custa perto de US$ 0,001.
- **Calibração.** Cada veredito ganha um R contrafactual com as regras do replay de saída fixa: entrada no fechamento da vela do sinal, stop pela mínima (com gap), alvo só no fechamento, no máximo 48 velas, líquido de taxa e slippage. Desempenho agrupa por modelo e versão do prompt e mostra, por veredito, n, R médio e erro padrão; os vetos de alta confiança têm linha própria e o R realizado das linhas que entraram fica numa coluna à parte. Um veto útil tem R contrafactual negativo. Uma linha só conta depois que as 48 velas passaram, e cada sinal conta uma vez. Quando o par sai da lista analisada, as velas da janela são lidas da Binance pelo mesmo caminho do teste histórico (poucas por hora, nunca sob pressão de peso); uma linha cujas velas foram lidas e que continua sem rótulo um dia depois do fim da janela expira: fica fora das médias, não é tentada de novo e aparece em Aguardando como expirada. Uma leitura que falha (rede, erro da Binance) não prova falta de dado: a linha é tentada de novo, e só três falhas depois do dia de folga a expiram.

**Chave.** Fica em `deepseek.json`, na mesma pasta do `binance.json` (`BOT_RUNTIME_ROOT`), com permissão 0600. Para gravar, rode na máquina do robô e cole a chave quando pedir (ela é lida da entrada padrão, nunca de argumento):

```powershell
python -m bot.ops advisor-key
```

`DEEPSEEK_API_KEY` só vale quando o arquivo não tem chave. A chave nunca vai para o `bot.json`, para o painel, para a API, para os logs, para o ledger ou para o `advice_log`; um campo com `key`, `secret` ou `token` no nome é recusado em `PUT /bot/config`.

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
- Ligue uma estratégia e clique em "validar no histórico" (`POST /bot/strategies/{id}/validate`). O teste roda em segundo plano sobre os pares configurados, em cada timeframe que a estratégia opera: 240 dias de 1h e 480 de 4h para Trend, 480 dias de 4h para Momentum, 120 dias de 15m e 240 de 1h para Mean Reversion e 120 dias de 15m (mais o 1h) para as day, com a mesma janela de 300 velas do robô ao vivo. Todas as estratégias em todos os timeframes, com 15 pares, levaram cerca de 3 minutos em dados sintéticos; cada rodada grava o tempo gasto. O painel mostra o progresso e, em Desempenho, o detalhe por timeframe: Sharpe bruto e deflacionado, tentativas, as 4 janelas, o hold-out, o Monte Carlo contra o limite, fator de lucro e expectativa em R. Ela só opera se passar no gate v3: Sharpe acima de 1, drawdown abaixo de 20% e pelo menos 30 trades antes do hold-out, 3 de 4 janelas lucrando, hold-out com pelo menos 5 trades e sem perda, e Sharpe deflacionado de pelo menos 0,95 (veja "Como o robô decide").
- Pares: "usar os 20 mais líquidos" troca os 5 pares padrão pelos 20 pares USDT de maior volume em 24h (sem stablecoins); pares com posição aberta nunca saem. A coleta roda em paralelo para caber no ciclo de 60 s.
- Seleção automática (`auto_select`, desligada por padrão): uma vez por hora o robô troca a lista pelos pares mais promissores (15 por padrão, de 5 a 20). Enquanto ela está ligada, a lista não muda pelo painel. No testnet, a seleção, o botão dos mais líquidos e a lista editada só aceitam pares que o Spot Testnet lista (`testnet.binance.vision`), porque a análise usa o mercado real e as ordens vão para o testnet. Vale qualquer par listado, mesmo com pouca negociação lá. O ticker de 24 h do testnet atrasa de 30 s a minutos nos pares finos, e o gate recusa cotação com mais de 15 s. Por isso, só no testnet, o bid e o ask vêm do livro ao vivo e a idade da cotação conta da hora da leitura; produção continua usando o horário da Binance. Um par sem vendedores no livro do testnet (como PEPE em 2026-10-19) não executa a compra.
- Modo teste do Spot Testnet (só testnet): setups de compra que falharam num filtro de qualidade ou cuja estratégia não está validada aparecem como Pronta com a marca **teste**, para você testar compra, stop e saída. Saem pelo botão executar e, com o trade automático ligado, também pelo ciclo (da maior nota para a menor, dentro do saldo e dos limites). Não entram na taxa de acerto nem no decaimento. Produção ignora a opção.
- Inicie. O ciclo é de 60 segundos. O selo cinza significa Spot Testnet.

5. Parar tudo (`POST /bot/halt` ou o botão) para o ciclo e grava o HALT, que bloqueia toda ordem nova. Com "zerar no halt" marcado, o robô vende as posições **antes** de gravar o HALT; o que não vender fica com o stop recolocado. Sem a opção, as posições ficam abertas com o stop na Binance. A escolha fica no mandato: mudá-la exige reconfirmar o mandato, e com um mandato gravado é ele que decide. Retomar é `POST /bot/resume`, manual. Antes de liberar, o robô resolve as compras limitadas que o halt deixou no livro; se alguma continua sem confirmação, a retomada é recusada (409) e o halt continua. Iniciar faz a mesma checagem quando o ciclo está parado. O ciclo também entra em halt sozinho depois de três falhas seguidas, com drawdown de `halt_drawdown_pct` (15%) e quando uma posição fica sem stop e sem como sair, inclusive um stop atravessado que os dois IOCs não venderam. Depois de um halt por drawdown, retomar volta em só reduzir até o drawdown cair abaixo de `dd_reduce_pct`.

6. Produção é outro diretório: `testnet/` e `production/` têm store, journal, mandato, HALT e ledger próprios. `binance.json`, `bot.json` e `api-token` ficam na raiz. O interruptor volta para testnet se qualquer pré-condição faltar, e o motivo aparece na tela. As liberações G1 a G4 só mudam editando `bot.json`; a API não as grava. **Promoção:** a troca exige, nos últimos 30 dias de testnet, PnL líquido positivo, pelo menos 30 trades de estratégia fechados com R registrado, expectativa em R positiva e p95 do Monte Carlo × risco por trade abaixo de `max_drawdown_pct`. Exige também nenhuma violação quantitativa do mandato no ledger de auditoria do testnet nesses 30 dias. **Piloto:** a exceção é um piloto: com um limite de capital de até 50 (`pilot_max_production_capital_usd` no `bot.json`, ou `pilot_cap_usd` enviado pelo painel na troca, no cartão "Piloto com pouco dinheiro") maior que zero e o capital de produção (alocado × `production_capital_fraction`, 10% por padrão) até esse valor, a troca dispensa só o histórico; todas as outras condições continuam valendo. O painel também pode pedir `pilot_skip_validation`: o piloto grava essa marca no store de produção e as estratégias ligadas operam sem validação no histórico (e, até haver amostra, sem estimativa de acerto), mantendo só o rebaixamento por par; a marca é apagada em toda troca que não a pede e nunca vale no testnet. O painel também pode pedir `pilot_skip_gates`, só com `pilot_gates_waivable: true` no `bot.json` (sem isso a troca é recusada): o piloto dispensa as liberações G1 a G4 só nessa troca (robô parado, sem posição aberta, chaves, mandato, PRODUCAO e auditoria do testnet continuam exigidos), e a marca fica no store de produção; uma promoção completa nunca as dispensa. O limite é gravado no store de produção na hora da troca e conferido onde o mandato é escrito, então editar o `bot.json` depois não o afrouxa: subir o capital, reconfirmar o mandato ou aplicar o preset acima do limite é recusado (409). O painel mostra "Produção piloto". Mesmo assim, o limite real é o saldo da conta: deixe nela só o valor do teste. Ao aprovar, o robô grava o mandato de produção com o capital de produção (10% do alocado). Sair de produção exige não haver posição de produção aberta: com "zerar no halt" marcado o robô vende antes; se algo continuar aberto, a troca é recusada, porque o processo em testnet não acompanharia aquelas posições.

O mandato limita a exposição das posições abertas pelo robô, avaliadas pela cotação atual. O resto da conta (caixa em USDT, moedas que já estavam lá) não conta como exposição do robô.

`binance.json` continua sendo o arquivo de credencial do conector portado. `bot.json` guarda modo, capital, estratégias e limites, e esses limites nunca podem afrouxar o mandato.

## Desempenho e concorrência

Medido no testnet, a partir do Brasil (cerca de 270 ms por requisição à Binance):

- **Coleta fora da trava do ciclo.** A leitura de mercado roda sem segurar a trava; só o cálculo e as ordens a seguram. Um `PUT /bot/config` durante um ciclo caiu de até 12,3 s para 77 ms.
- **Coleta paralela por requisição.** Até 16 requisições ao mesmo tempo, com um cliente HTTP reaproveitado (keep-alive) e o livro de todos os pares em uma chamada só. A coleta de 15 pares caiu de 5,7 s para cerca de 2,2 s. Um par que falha fica de fora do ciclo e é registrado no ledger (`collect_partial`); se ele tem posição aberta, o ciclo inteiro é recusado, para o stop e a saída nunca usarem preço velho.
- **Tabela de mercados da Binance em cache** por uma hora, por host e tipo de cliente, com o ajuste de relógio junto. Antes, cada cotação e ordem baixava o `exchangeInfo` de novo.
- **Journal verificado por incremento.** A leitura confere só as linhas novas contra o último hash conhecido. A cadeia inteira é refeita a cada 5 minutos e sempre antes de registrar uma ordem nova.
- **Ledger com rotação** a 1 MiB: anexar não cresce com o histórico. O relatório diário e a verificação na partida (em segundo plano) conferem a cadeia com os arquivos rotacionados.
- **SQLite** com `busy_timeout` de 10 s e journal de rollback (o WAL precisa de memória compartilhada que o bind mount do Docker Desktop não garante). As decisões só são gravadas quando mudam: a mesma oportunidade lida a cada minuto do mesmo candle vira uma linha, não 240. Como o motivo da reversão à média traz o RSI, ela grava uma linha nova quando o RSI muda. Decisões, sinais e pontos de patrimônio com mais de 30 dias são apagados uma vez por dia; o pico do PnL acumulado fica guardado à parte, então o drawdown não muda.
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

Desde a revisão de estratégias, cobre também: o tamanho pelo risco com cada teto e o limite que venceu, o teto do mandato até reconfirmar, a entrada LIMIT IOC (parcial, sem execução, sem ask), o stop atravessado (dois IOCs e halt), a folga do stop móvel e a janela sem stop, os degraus de drawdown e o só reduzir em todo caminho de compra, a perda diária sobre o patrimônio, o filtro de mercado sem olhar o futuro, a correlação no mesmo ciclo e o cluster do BTC, o gate v3 (walk-forward, hold-out, Sharpe deflacionado com N, Monte Carlo em R, por timeframe e por pares), a revalidação de resultados velhos, a histerese, o decaimento por trades e por par, o bloqueio por slippage, a promoção com R e auditoria, a idade do sinal, o calendário macro, a taxa da conta e os robôs (pausa entre rodadas, máximo de robôs e prévia de 30/90/180 dias).

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
