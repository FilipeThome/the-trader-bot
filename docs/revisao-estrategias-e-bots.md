# Revisão do sistema frente à prática profissional, à pesquisa acadêmica e aos bots de mercado

Data: 2026-10-20. Branch: `feat/auto-trade-tp-sl`. Documento de análise e proposta, escrito antes da implementação. A seção 11 registra o que foi implementado.

Três frentes de pesquisa alimentaram o documento, cada uma feita por um agente independente:

1. **Auditoria do código** (`bot/`, `ui/`, `tests/`): inventário de arquitetura, estratégias, risco, execução e validação, com referência a arquivo e linha.
2. **Pesquisa acadêmica e profissional**: estudos de economistas sobre day trade e investidor pessoa física, pesquisa sobre análise técnica e momento, pesquisa sobre overfitting de backtest, e as regras de dimensionamento e risco que traders sistemáticos publicam.
3. **Catálogo de bots**: Binance (Spot Grid, Futures Grid, Spot DCA, Rebalancing, TWAP, arbitragem de funding), Bybit, OKX, KuCoin, Bitget, Pionex, 3Commas, Bitsgap, Altrady, Cryptohopper, e os frameworks abertos Freqtrade, Hummingbot, QuantConnect/LEAN, NautilusTrader, vectorbt, Jesse.

As afirmações marcadas **[não verificado]** vêm de fontes secundárias ou de memória e precisam de confirmação antes de virar requisito.

---

## 1. Resumo executivo

O sistema já está acima da média dos bots de varejo em segurança de execução: mandato com gate fail-closed, journal idempotente com reconciliação, stop na corretora logo após cada compra, halt pegajoso, proteções de cooldown e trava de perdas, validação obrigatória no histórico com teste de estabilidade por metades, e regras de day trade alinhadas ao que a literatura recomenda (stop com piso, TP1 parcial, break-even, stop de tempo, horas mortas). As fontes já citadas no README e em `docs/plano-day-trade.md` são as mesmas que a pesquisa desta revisão confirmou.

As lacunas encontradas estão em cinco grupos, por ordem de impacto:

| # | Lacuna | Evidência / prática | Onde está no código | Prioridade |
|---|---|---|---|---|
| 1 | **O tamanho da posição é, na prática, notional fixo.** O teto de 2% por ordem × 0,9 vence a fórmula de risco em todo cenário realista; risco por trade, Kelly, risco total e exposição são inertes. Capital usado fica em ~9%. | Tharp, Turtles, Basso: risco fixo por trade escalado por volatilidade; Moreira-Muir e Harvey et al.: alvo de volatilidade reduz cauda. | `bot/risk.py:69-85`, `max_order_notional_pct=0.02` em `bot/config.py` | P1 |
| 2 | **Stop-limit acionado e não executado não tem tratamento.** O código só reage a `filled` e a `cancelled/expired/rejected`; um stop que disparou e ficou no livro abaixo do preço deixa a posição descoberta. | Freqtrade `stoploss_on_exchange` reverifica a cada 60 s e sai a mercado; Hummingbot fecha TP/SL/tempo a mercado. | `bot/executor.py:1086-1090` | P0 |
| 3 | **Validação sem correção para múltiplos testes, sem walk-forward e sem hold-out.** Seis estratégias × pares × quatro regras de saída, gate de 10 trades no swing. O próprio README admite que 1 aprovação em 18 é o esperado do acaso. | Harvey-Liu, Bailey-López de Prado (PBO, Sharpe deflacionado), Pardo e Davey (walk-forward, Monte Carlo, incubação). | `bot/backtest/runner.py:314,348`, `bot/api/app.py:686-713` | P2 |
| 4 | **Diferenças entre ao vivo e histórico.** Saída por sinal lê outro timeframe do que o validado; só `timeframes[0]` é validado; o swing não paga slippage no histórico; o backtest assume que o stop sempre executa. | Freqtrade documenta as próprias premissas; Davey: "resultado de papel dentro da distribuição do backtest". | `bot/runtime.py:1287,1297`, `bot/api/app.py:689`, `bot/backtest/runner.py:59` | P1 |
| 5 | **Sem filtro de regime do mercado e sem freio por drawdown parcial.** O drawdown de 10% vira halt direto; não há redução de risco em −5%, nem leitura do BTC/volatilidade do mercado. | Turtles (corte de 20% do notional a cada 10% de drawdown), Harvey et al. (alvo de volatilidade), Freqtrade `MaxDrawdown`, LEAN `MaximumDrawdownPercentPortfolio`. | `bot/risk.py:115`, `bot/runtime.py:1231`, `bot/indicators.py:49` | P2 |

O resto do documento detalha a evidência, o estado atual e a proposta de cada item, mais os ajustes menores, e termina com um plano por fases com critérios de aceite.

---

## 2. O que a pesquisa econômica diz

### 2.1 Resultado do day trader e do investidor pessoa física

| Estudo | Amostra | Resultado | Implicação para o robô |
|---|---|---|---|
| Barber e Odean, *Trading Is Hazardous to Your Wealth* (J. Finance 2000) | 66.465 contas numa corretora dos EUA, 1991-96 | Retorno líquido médio 16,4% a.a. contra 17,9% do mercado; os 20% mais ativos fizeram 11,4%. Causa: custo de transação mais excesso de confiança, não seleção ruim. | Cada trade extra tem de pagar o custo de ida e volta. Giro é uma linha de custo, não variável livre. |
| Barber, Lee, Liu, Odean, *Just How Much Do Individual Investors Lose by Trading?* (RFS 2009) | Todo o mercado de Taiwan | Pessoa física perde 3,8 pontos a.a. (2,2% do PIB); "praticamente toda" a perda vem de ordens agressivas (taker). | Preferir ordens maker quando a execução não é urgente. Hoje toda entrada é MARKET. |
| Barber, Lee, Liu, Odean, *The Cross-Section of Speculator Skill* (JFM 2014) | Day traders de Taiwan, 1992-2006 | Menos de 1% tem retorno anormal positivo e previsível depois dos custos. Os 500 melhores do ano anterior fazem 37,9 bp/dia no ano seguinte. | Habilidade existe mas é rara; a prior de qualquer estratégia intradiária nova é "sem vantagem depois dos custos". |
| Barber et al., *Learning, Fast or Slow* (RAPS 2020) | Idem | 97% dos day traders tendem a perder; 74% do volume vem de quem já tem histórico de perda. | O decaimento automático (`decay.py`) é a resposta certa; precisa ser mais rápido que 30 dias quando a amostra permite. |
| Chague, De-Losso, Giovannetti, *Day Trading for a Living?* (FGV/USP 2019) | Day traders de mini-índice no Brasil que insistiram por mais de 300 pregões | 97% perderam; 1,1% ganhou mais que o salário mínimo; o melhor fez ~US$ 310/dia com desvio de US$ 2.560/dia. Nenhuma evidência de aprendizado. | Mesma conclusão. |
| Odean, *Are Investors Reluctant to Realize Their Losses?* (J. Finance 1998) | 10.000 contas | Realizam 14,8% dos ganhos e só 9,8% das perdas (efeito disposição). | Saídas precisam ser regra fixa e não anulável: stop, alvo e stop de tempo definidos na entrada. O sistema já faz isso; manter. |
| Heimer e Imas, *Biased by Choice* (RFS 2022) | Teto de alavancagem de 50:1 no forex dos EUA (2010) | O teto melhorou o desempenho e reduziu o efeito disposição. | Teto duro de alavancagem ajuda até quem tem habilidade. O sistema está em 1×; manter como invariante. |
| Hasso, Pelster, Breitmayer (JBEF 2019); Kogan, Makarov, Niessner, Schoar (JFE 2024) | 465.926 contas; traders do eToro | Em cripto o varejo é seguidor de tendência (em ações é contrário) e realiza retorno menor. | O viés do varejo em cripto é comprar alta tardia; o filtro de regime e o custo mínimo do setup são a defesa. |
| BIS Bulletin 69 (2023) | Usuários de apps de cripto, 2015-22 | Maioria perdeu dinheiro em BTC em quase todas as economias. | Idem. |

### 2.2 Análise técnica, momento e custos

- **Brock, Lakonishok, LeBaron (1992)** encontraram sinal em médias móveis e rompimentos no Dow 1897-1986; **Sullivan, Timmermann, White (1999)** mostraram que, corrigindo o data snooping em ~7.800 regras, nada sobrevive fora da amostra depois de 1986. **Park e Irwin (2007)**: de 95 estudos modernos, 56 positivos, mas com data snooping e custo mal estimado como problemas recorrentes. Implicação: regras técnicas clássicas são fracas; só sobrevivem com custo baixo e validação fora da amostra.
- **Moskowitz, Ooi, Pedersen (2012)**, momento de série temporal em 58 futuros: posição = sinal(retorno 12 m) × 40% / σ, com σ = EWMA de 60 dias. É a referência do **Momentum** do sistema e da proposta de alvo de volatilidade.
- **Liu e Tsyvinski (RFS 2021)** e **Liu, Tsyvinski, Wu**: o retorno de cripto carrega em momento próprio de cripto e em atenção, não em fatores tradicionais; três fatores (mercado, tamanho, momento) explicam a seção cruzada. O sistema já usa momento relativo só para escolher pares; manter.
- **Novy-Marx e Velikov (RFS 2016)**: anomalias com giro mensal abaixo de 50% sobrevivem aos custos; acima disso, poucas. A mitigação mais eficaz é **histerese**: limiar de entrada mais exigente que o de manutenção. O sistema tem `score_threshold` só na entrada e sai por sinal inverso; não há banda.
- **Baur, Cahill, Godfrey, Liu (2019)**: efeitos de hora e dia da semana no BTC existem, mas não são persistentes; volume é menor no fim de semana e maior no horário de bolsa dos EUA. Implicação: horário serve de **filtro de liquidez e spread**, não de fonte de alfa. As horas mortas 03-07 UTC estão corretas como filtro.
- **Schmeling, Schrimpf, Todorov, *Crypto Carry* (BIS WP 1087, 2023)**: o carry futuro-spot passou de 40% a.a. em períodos, movido por varejo seguidor de tendência; o risco são os picos de margem e liquidação no drawdown. O `funding_carry` do sistema é só sinal; se um dia executar, precisa de hedge delta-neutro e folga de margem para 30-50% de movimento adverso.

### 2.3 Overfitting de backtest

- **Harvey e Liu, *Backtesting* (JPM 2015)**: cortar o Sharpe pela metade é regra errada; o desconto é não linear. Abaixo de Sharpe 0,4 o corte passa de 50%; acima de 1,0 fica em ~25%. Um Sharpe de 0,75 escolhido entre 200 tentativas vira ~0,32 **[magnitudes de fonte secundária]**.
- **Bailey, Borwein, López de Prado, Zhu, *The Probability of Backtest Overfitting***: validação cruzada combinatória simétrica (CSCV); PBO é a fração de partições em que a melhor configuração dentro da amostra fica abaixo da mediana fora dela.
- **Bailey e López de Prado, *The Deflated Sharpe Ratio* (JPM 2014)**: corrige o Sharpe pelo número de tentativas e por assimetria e curtose.
- **López de Prado, *Advances in Financial Machine Learning* (2018)**: rotulagem por tripla barreira (stop, alvo, tempo), meta-rotulagem, k-fold purgado com embargo, CPCV.
- **Pardo (2008)** e **Davey (2014)**: walk-forward com eficiência (retorno fora / retorno dentro) ≥ 50-60%; Monte Carlo sobre os trades para distribuição de drawdown; incubação em papel de 6 a 12 meses antes de dinheiro.

Requisitos derivados: registrar o número de configurações testadas; reportar Sharpe deflacionado e PBO; hold-out nunca tocado; gates de promoção com walk-forward e Monte Carlo. Os limiares numéricos (PBO ≤ 0,5, DSR ≥ 0,95) são convenção, não resultado de artigo.

---

## 3. O que os profissionais fazem (regras acionáveis)

| Regra | Fonte | Como o sistema está |
|---|---|---|
| Risco por trade de 0,5% a 1% do patrimônio; `qty = risco / |entrada − stop|` | Tharp, Turtles (1% em 2N), Basso (0,5%), Minervini (≤2%) **[secundário]** | A fórmula existe em `risk.py:69-85`, mas o teto de notional a torna inerte (seção 5.1). |
| Teto por volatilidade além do teto por risco: `ATR × qty / patrimônio ≤ 0,2-0,3%` | Basso **[secundário]** | Não existe. |
| Alvo de volatilidade da carteira: `exposição = vol alvo / σ_EWMA`, com teto e banda de rebalanceamento | Moreira-Muir (2017), Harvey et al. (2018): reduz cauda esquerda e drawdown em ativos de risco | Não existe. |
| Risco total aberto ≤ 6-10%; cluster com ρ > 0,7 conta como uma posição | Turtles **[secundário]** | `max_open_risk_pct=6%` existe, mas inerte pelo mesmo motivo; correlação só contra posições já abertas, não entre as novas do mesmo ciclo (`risk.py:165`). |
| Freio por drawdown: metade do risco em −10% do pico; parar em −20% | Turtles, Seykota **[secundário]** | Halt direto em −10% (`risk.py:115`); sem degrau intermediário. |
| Perda diária máxima de 2-3% ou ~3R, parando o resto do dia UTC | Convenção de mesa | Existe (`max_daily_loss_pct=3%`), mas só sobre PnL realizado; não conta o não realizado nem zera posições. |
| Toda posição com as três barreiras na entrada: stop, alvo, tempo | López de Prado; Hummingbot Position Executor | Swing não tem stop de tempo; day tem (360 min). |
| Expectativa em R medida em ≥ 30-100 trades antes de promover | Tharp | Gate do swing aceita 10 trades; não há R-múltiplo gravado por trade. |
| Ganho/risco planejado ≥ 2-3, ou `p_breakeven = 1/(1+RR)` líquido de custo | Tudor Jones (5:1) **[secundário]** | `min_reward_risk=1.5`; `cost_multiple=2`. Para a regra fixa o sistema já usa o breakeven + 3 pontos. |
| Kelly fracionário 0,25-0,5, com estimativas encolhidas para zero | MacLean, Thorp, Ziemba (2010); Chan **[não verificado]** | `kelly_fraction=0.25` existe; inerte pelo teto. |
| Preferir maker; orçamento `ida e volta = 2×taxa + spread + slippage`; exigir `edge ≥ 2-3 × custo` | Barber et al. 2009; prática | `cost_multiple=2` existe; entrada é sempre MARKET; slippage é modelado só pelo topo do livro com notional fixo de US$ 1.000 (`collector.py:87`). |
| Rastrear slippage realizado por par e desligar par acima do orçamento | Prática | Não existe. |
| Histerese: limiar de entrada > limiar de saída | Novy-Marx e Velikov | Não existe. |
| Não entrar em horas de livro fino; filtro de profundidade mínima; pausa ao redor de CPI/FOMC **[convenção]** | Baur et al.; prática | Horas mortas só no day trade; sem filtro de profundidade; sem calendário macro. |
| Walk-forward (WFE ≥ 50%), Monte Carlo de drawdown (p95 dentro da tolerância), incubação em papel | Pardo, Davey | Nada disso; a promoção exige só 30 dias de PnL líquido positivo no testnet, sem teste de significância nem mínimo de trades (`promotion.py`). |
| Market making: limite de inventário, pausa depois de N execuções de um lado só, kill switch por PnL | Hummingbot | Não se aplica hoje (o bot não faz market making); relevante para o grid. |

---

## 4. O que os bots de mercado fazem

### 4.1 Binance

- **Spot Grid**: aritmético ou geométrico; 2 a 500 grids (170 com Trailing Up); preço de gatilho opcional; stop-loss abaixo da faixa e take-profit acima, editáveis depois; **"Sell All Base Coins on Stop" ligado por padrão**, pulado se a liquidez não permitir (~0,9 × ordem máxima de mercado); Trailing Up sobe a faixa e o stop junto; lucro de grid é realizado após taxas; reserva de taxa no ativo base. Parâmetros automáticos (IA) a partir da volatilidade histórica. Aviso oficial: o bot continua executando em crash ou spike; pode perder tudo.
- **Futures Grid**: long/short/neutro, alavancagem com aviso acima de 20×, stop superior/inferior por direção, trailing que não pode ser desligado, risco documentado de posição reversa com trailing.
- **Spot DCA**: ordem base e ordens DCA; desvio de preço 0,1-15%; multiplicadores de desvio e de tamanho (padrão 1); capital exigido = base + DCA × (1 + m + … + m^(n−1)); take-profit da média, fixo ou trailing; stop-loss que encerra a rodada ou o bot; **cooldown de 60 s entre rodadas**; faixa opcional só para a ordem base.
- **Rebalancing Bot**: por limiar (0,5% a 50%) ou intervalo (30 min a 28 dias).
- **Funding-Rate Arbitrage Bot** (2024): delta-neutro, carteira separada.
- **TWAP/VP** em USD-M: máximo de 30 ordens algo abertas; a API não garante execução.

O DCA e o grid do sistema (`bot/instances.py`) seguem o modelo da Binance com três diferenças: o stop e o pior caso são calculados antes da primeira compra (melhor que a Binance), o **cooldown entre rodadas não existe**, e a execução é por polling de 60 s com compra a mercado e venda IOC, o que perde ciclos dentro do minuto e paga taker nas duas pernas.

### 4.2 Outras corretoras

- **Bybit**: Futures Combo (2-10 contratos, pesos, rebalanceamento por desvio 1-50% ou tempo), Martingale com stop ratio e loop.
- **OKX**: Spot/Infinite/Moon/Futures Grid, DCA, Smart Portfolio, Smart Arbitrage, TWAP/iceberg; **Signal Bot** com `maxLag` (1-3600 s, padrão 60): sinal mais velho que isso é descartado.
- **KuCoin**: grid spot/futuros/infinito, DCA, martingale, Smart Rebalance; taxa flat de 0,06% nos bots de futuros; orientação oficial de que grid serve para moeda líquida e volátil em lateralização.
- **Bitget, MEXC, Gate.io, Crypto.com**: variações dos mesmos; Coinbase Advanced não tem bots nativos.

### 4.3 Plataformas de terceiros

- **3Commas DCA**: ordem base, ordens de média ("averaging"), máximo de ordens, desvio, multiplicador de passo (ex.: 1% × escala 2 dá −1, −3, −7, −15, −31%), multiplicador de volume, condição por indicador ou webhook para cada ordem de média, take-profit fixo ou trailing (com aviso de que fecha a mercado e perde em par ilíquido), **stop-loss com timeout em segundos para não disparar em pavio**, máximo de trades ativos, máximo por par, **cooldown por par**, condições de início.
- **Pionex**: ~16 bots; grid mantém ~100-120 ordens vivas; taxa 0,05%; só Trailing Up; stop/TP vende tudo; alerta de que mudar a faixa trava perda flutuante; arbitragem spot-futuros com retorno anunciado pelo próprio vendedor.
- **Bitsgap COMBO**, **Altrady** (grid 2-100 níveis, reserva de taxa), **Cryptohopper** (timeout no trailing stop desde out/2025), **Coinrule**, **HaasOnline**, **Gunbot**, **TradeSanta**.

### 4.4 Frameworks abertos

- **Freqtrade**: `minimal_roi` (saída por tempo e lucro), `stoploss` obrigatório, trailing com ativação, **`stoploss_on_exchange` com `stoploss_on_exchange_limit_ratio` (ex.: 0,99) reverificado a cada 60 s**, `max_open_trades`, `tradable_balance_ratio`, `custom_price_max_distance_ratio` (2%), `exit_profit_only`; proteções **StoplossGuard, MaxDrawdown (modo equity), CooldownPeriod, LowProfitPairs**; backtest sem slippage e com stop executando no preço exato (documentado como premissa); hyperopt com perda por Sharpe/Sortino/Calmar/drawdown; FreqAI.
- **Hummingbot**: pure market making (spread, refresh, níveis, inventário), Avellaneda-Stoikov, XEMM; **V2 Position Executor com tripla barreira** (stop, alvo, tempo, trailing com ativação e delta), LIMIT para abrir e MARKET para fechar; executores de grid, DCA, arbitragem, TWAP.
- **QuantConnect/LEAN**: `MaximumDrawdownPercentPerSecurity` (5%), `MaximumDrawdownPercentPortfolio`, `MaximumUnrealizedProfitPercentPerSecurity`, `TrailingStopRiskManagementModel`, `MaximumSectorExposureRiskManagementModel` (20%); pipeline alfa → construção de carteira → risco → execução.
- **NautilusTrader**: RiskEngine com estados ACTIVE, **REDUCING** (só ordens que reduzem posição) e HALTED; `max_order_submit_rate`, `max_notional_per_order`.
- **vectorbt**: `fees`, `slippage`, `sl_stop`, `sl_trail`, `tp_stop` vetorizados. **Jesse**: walk-forward e Monte Carlo nativos.

### 4.5 Controles comuns a todos e situação no sistema

| Controle | Quem tem | Sistema |
|---|---|---|
| Máximo de trades simultâneos e por par | 3Commas, Freqtrade, Pionex | Sim (`max_positions`, uma por par). |
| Cooldown por par e depois de perda | 3Commas, Binance DCA, Freqtrade | Sim para estratégias; **não para instâncias DCA/grid**. |
| Pausa por drawdown (modo equity) | Freqtrade, LEAN, Nautilus | Halt em 10%; sem degraus; sem modo "só reduzir". |
| Stop com debounce (timeout) | 3Commas, Cryptohopper | Não; o stop é da corretora e dispara em pavio. |
| Stop na corretora reverificado e reforçado a mercado | Freqtrade | **Não** (lacuna P0). |
| Grid "vender tudo ao parar" | Binance, Pionex, Altrady | Sim, via stop abaixo da faixa; sem opção de parar e vender por decisão do operador sem halt global. |
| Sinal com idade máxima | OKX `maxLag` | Cotação ≤ 15 s existe; a vela de decisão não tem idade máxima explícita. |
| Teto de notional e taxa de envio | Nautilus, Binance (30 algos) | Sim (mandato, peso da API). |
| Limite de slippage na entrada | Freqtrade (2%) | **Não**: entrada é MARKET sem proteção. |
| Paper trading | Todos | Sim (testnet, modo teste). |
| Funding modelado | Só bots de arbitragem | Só leitura. |

### 4.6 Ressalvas documentadas pelos próprios vendedores

- Grid só ganha em lateralização; abaixo da faixa segura moeda no prejuízo, acima fica em caixa e perde a alta; mudar a faixa trava perda; cada degrau precisa pagar duas taxas.
- Martingale/DCA compromete capital em progressão geométrica: com passo e volume em escala 2, a quinta ordem já está em −31% com 16× o tamanho da primeira.
- Trailing que fecha a mercado perde em par ilíquido.
- Retornos de arbitragem de funding são anunciados pelo vendedor e somem quando o funding fica neutro ou negativo.
- Backtest sem slippage superestima (Freqtrade).

---

## 5. Estado atual do sistema (resumo da auditoria)

Ciclo de 60 s (`bot/runtime.py:823`): coleta fora da trava (1m, 15m, 1h, 4h, livro, funding) → `sync_positions` → trailing → saídas → instâncias DCA/grid → decaimento → score (estratégia → `evaluator.classify` → `RiskManager.assess`) → `protect` (cooldown, trava de perdas) → `allocate` → `executor.act` (só automático) → gatilhos armados.

Seis estratégias, todas só compra: `trend` (1h/4h, rompimento de 20 velas com ADX > 25, stop 2 ATR, saída EMA20 < EMA50), `mean_reversion` (15m/1h, RSI < 30 e abaixo da Bollinger, alvo EMA20), `momentum` (4h, retorno de 120 velas, stop 2,5 ATR), `funding_carry` (só sinal), `intraday_momentum` e `vwap_pullback` (15m com filtro de 1h, stop max(1,5 ATR, 1%), TP1 1,5R, break-even, 360 min, horas mortas). Regras de saída: alvo, trailing, trailing após 1R, day, fixa (`fixed:<tp>:<sl>`). DCA e grid como instâncias do operador, com pior caso calculado antes da primeira compra.

Risco: sizing por stop com teto de notional (`risk.py:69-85`), Kelly fracionário, risco total 6%, exposição 60%, 5 posições, uma por par, 2 entradas por ciclo, 20 trades/dia, correlação ≥ 0,8 contra posições abertas, cooldown 120 min, trava 3 perdas em 6 h, perda diária 3% realizada, drawdown 10% → halt, taxa taker 0,1% sem desconto BNB, slippage = spread + US$ 1.000 / profundidade do topo.

Execução: MARKET na compra; STOP_LOSS_LIMIT GTC com limite 0,5% abaixo do gatilho; saídas por LIMIT IOC a bid × 0,995; idempotência por (perfil, posição, perna, tentativa) com journal; sem retry dentro do ciclo; reconciliação a cada ciclo; peso da API com pressão em 80%; cotação ≤ 15 s; USD-M só leitura.

Validação: replay com parâmetros padrão numa janela de 299 velas; stop na mínima da vela, alvo swing na máxima, alvo day/fixo no fechamento; 0,2% de taxa; slippage 0,03% por perna só no day; gate swing: Sharpe por trade > 1, DD < 20%, ≥ 10 trades, duas metades lucrando; gate day: Sharpe diário > 1, ≥ 30 trades, ≥ 0,10%/trade, DD < 20%, metades. Promoção para produção: 30 dias de PnL líquido positivo, mandato, gates manuais, palavra PRODUCAO. Decaimento: Sharpe ao vivo de 30 dias < 0,5 × Sharpe do backtest.

Indicadores (`bot/indicators.py`): RSI, ATR e ADX por média simples, não Wilder; valores diferem dos da corretora e do TradingView.

---

## 6. Lacunas, evidência e proposta

Cada item traz: o que o código faz hoje, o que a evidência ou o mercado faz, o risco e a proposta. Nada abaixo é implementação.

### 6.1 Sizing: o teto por ordem anula o risco por trade (P1)

**Hoje.** `size = min(capital × 2% × 0,9, capital × share / stop_pct)` (`risk.py:69-85`). Com `share = 1%` a segunda parcela só é menor que a primeira quando `stop_pct > 55%`. Toda posição sai com ~1,8% do capital e o risco real no stop é 1,8% × stop (0,02% a 0,09% do capital). Consequências: risco por trade, Kelly, `max_open_risk_pct` e `max_exposure_pct` nunca limitam nada; cinco posições usam ~9% do capital; o escalonamento por ATR, que o README anuncia ("um ativo mais volátil compra menos"), não acontece.

**Evidência.** Tharp, Turtles, Basso: o tamanho vem do stop. Harvey et al. (2018): alvo de volatilidade reduz drawdown em ativos de risco. O teto por ordem do mandato é herdado do conector e faz sentido como trava de segurança, não como regra de sizing.

**Proposta.**
- Separar dois conceitos: **teto de segurança por ordem** (mandato, ex.: 10-25% do capital) e **tamanho por risco** (`risk_per_trade_pct / stop_pct`).
- Acrescentar o **teto por volatilidade** de Basso: `qty × ATR14 / capital ≤ vol_cap_pct` (0,2-0,3%).
- Alvo de volatilidade da carteira: `exposição total = min(max_exposure_pct, target_vol / σ_EWMA_carteira)`, com banda de rebalanceamento para não girar.
- Mostrar no painel, por linha, qual limite venceu (risco, teto por ordem, volatilidade, caixa), para o operador ver o que está travando.
- Critério de aceite: com stop de 2% e risco de 1%, a ordem é 50% do capital limitada pelo teto de segurança, não 1,8%; com stop de 4% a ordem cai à metade.
- Reconfirmação do mandato ao mudar o teto, como hoje.

### 6.2 Stop-limit acionado e não executado (P0)

**Hoje.** `_check_stop` (`executor.py:1070-1090`) consulta o stop e só age em `_FILLED` ou `_DEAD`. Um STOP_LOSS_LIMIT que disparou e virou LIMIT no livro com o preço já abaixo do limite (gap maior que 0,5%) fica em `new` e a função retorna sem fazer nada. A posição fica sem proteção enquanto o preço cair. O README só avisa que o break-even "pode não executar num gap".

**Mercado.** Freqtrade reverifica o stop na corretora a cada `stoploss_on_exchange_interval` (60 s) e, se o preço passou do limite, cancela e sai a mercado. Hummingbot fecha TP/SL/tempo a mercado.

**Proposta.**
- No `_check_stop`, quando o stop está acionado (campo de status da Binance ou `stopPrice` acima do último preço com a ordem ainda aberta) ou quando `last ≤ stop × (1 − STOP_LIMIT_GAP)`, cancelar o stop e sair pela via existente de saída de emergência (IOC a bid − gap; se falhar, tentar MARKET em produção, onde o testnet não é limitação).
- Registrar no ledger como `stop_gapped` com o preço do gatilho e o de execução, para medir slippage de stop real.
- Teste: venue falso com stop em `new` depois do gatilho e preço 2% abaixo; o ciclo seguinte fecha a posição.

### 6.3 Janela sem proteção no trailing e no TP1 (P0)

**Hoje.** Trailing e TP1 cancelam o stop e só depois colocam o novo (`executor.py:542-605`, `616-693`); se a colocação falha, o caminho é saída de emergência ou halt. A Binance Spot não tem "replace" atômico nem OCO para este caso (OCO exige dois lados).

**Proposta.**
- Ordem das operações: colocar o novo stop primeiro só não é possível porque o saldo está travado pelo stop antigo. Alternativa: manter a janela, mas limitar o trailing a quando `orders_remaining ≥ 2 + reserva` (já feito) **e** o peso da API está folgado (já feito) **e** o último preço está ≥ 1% acima do novo stop (novo), para a janela não coincidir com o preço perto do gatilho.
- Medir e expor no painel o tempo médio entre cancelamento e nova colocação, para o operador saber a janela real.
- Para o day trade, avaliar o OCO da Binance (TP1 limit + stop-limit) na metade a vender: um único envio protege e vende, sem janela. Fora do escopo da v1 do plano de day trade, mas é a solução estrutural.

### 6.4 Entrada a mercado sem limite de slippage (P1)

**Hoje.** A compra é MARKET por notional; o stop absoluto é fixado na hora do score (`runtime.py:1203`), então o slippage da execução muda o risco realizado. O modelo de slippage usa só o topo do livro com US$ 1.000 fixos (`collector.py:87`).

**Mercado.** Freqtrade `custom_price_max_distance_ratio` (2%); OKX Signal Bot aceita limit com offset; Hummingbot abre com LIMIT.

**Proposta.**
- Entrada por **LIMIT IOC a ask × (1 + gap)** com gap pequeno (0,1-0,3%), em vez de MARKET: executa o que couber dentro do gap e deixa o resto de fora; uma execução parcial é tratada como já se trata a venda parcial. Alternativa menor: manter MARKET, mas recusar a entrada quando `ask − bid` ou a profundidade dentro de 0,2% não cobre a ordem inteira.
- Modelar slippage com o **notional da própria ordem**, não US$ 1.000 fixos, caminhando pelos níveis do livro já coletados.
- Recalcular o stop e o alvo sobre o **preço médio executado** em todas as regras, como a regra fixa já faz (`executor.py:262-267`), para o risco em R ficar igual ao planejado.
- Rastrear slippage realizado por par (entrada e saída) e **desligar o par** quando a média de 20 trades passa do orçamento.

### 6.5 Validação: múltiplos testes, walk-forward, hold-out (P2)

**Hoje.** Um único replay por estratégia e timeframe sobre os pares atuais; gate de 10 trades no swing; sem correção para o número de combinações testadas; os pares são escolhidos pela seleção automática por momento recente e o resultado da validação não é amarrado aos pares (`validated_for` ignora símbolos). O README reconhece que 1 aprovação em 18 é o esperado do acaso.

**Evidência.** Harvey-Liu, Bailey-López de Prado, Pardo, Davey (seção 2.3).

**Proposta.**
- **Walk-forward com hold-out**: validar em janelas rolantes (ex.: 4 janelas de 60 dias para o day, 4 de 90 dias para o swing) e exigir eficiência fora da amostra ≥ 50% e lucro em ≥ 3 das 4 janelas; o último trimestre fica como hold-out que a validação nunca usa para parametrizar nada. Substitui o teste de "duas metades".
- **Sharpe deflacionado**: gravar quantas combinações (estratégia × timeframe × regra de saída × conjunto de pares) foram testadas desde a última aprovação e descontar o Sharpe pelo número de tentativas; aprovar com DSR ≥ 0,95 **[limiar sugerido]**. Mostrar o Sharpe bruto e o deflacionado no painel.
- **Monte Carlo de drawdown**: reamostrar os trades 1.000 vezes e exigir que o p95 do drawdown fique abaixo de `max_drawdown_pct`; hoje um drawdown de 19% passa e o halt é 10%, uma incoerência entre o gate e o halt.
- **Mínimo de trades** do swing de 10 para 30, alinhado ao day.
- **Amarrar a validação aos pares**: a chave `validated_for` inclui o conjunto de pares, e a seleção automática só introduz um par novo como "sinal apenas" até a estratégia ser validada nele; ou, mais simples, validar por par e manter uma tabela estratégia × par.
- **Validar todos os timeframes operados**: hoje `trend` 4h e `mean_reversion` 1h operam sob a validação do 1h e do 15m (`api/app.py:689`).
- **Métricas adicionais no resultado**: profit factor, expectativa em R, Sortino, Calmar, ganho/perda médios, exposição, MAE/MFE por trade; gravar o R-múltiplo de cada trade ao vivo e no histórico.

### 6.6 Fidelidade entre ao vivo e histórico (P1)

| Diferença | Onde | Proposta |
|---|---|---|
| Saída por sinal lê 1h no trend mesmo para entrada de 4h; 15m no mean reversion mesmo para 1h | `runtime.py:1287,1297` | Ler o timeframe da própria posição (gravar `timeframe` na posição). |
| Swing não paga slippage no backtest | `runner.py:59` | Aplicar o mesmo 0,03% por perna (ou o slippage medido por par). |
| Backtest assume stop sempre executado em min(abertura, stop) | `runner.py` | Modelar o gap: se a abertura da vela fica abaixo do stop × (1 − gap), executar na abertura (pior). |
| Alvo swing executa na máxima da vela; ao vivo é IOC depois do poll | `runner.py` | Executar no fechamento da vela, como o day e a regra fixa já fazem. |
| Decaimento compara Sharpe diário ao vivo (sem dias zero) com Sharpe por trade do backtest | `decay.py`, `store.py:577` | Calcular os dois da mesma forma (diário com dias zero) ou comparar expectativa em R. |
| Indicadores por média simples | `indicators.py` | Documentar como decisão (são consistentes entre ao vivo e histórico) ou migrar para Wilder; não misturar. |
| `recommend.py` avalia a vela em formação | display | Marcar no painel como "prévia da vela aberta". |

### 6.7 Freios por drawdown e regime (P2)

**Hoje.** Drawdown de 10% é halt direto; perda diária de 3% considera só o realizado; sem filtro de mercado; regime é por símbolo (ADX e EMA50).

**Mercado e evidência.** Turtles: −20% de notional a cada −10%; Freqtrade `MaxDrawdown` em modo equity; Nautilus `REDUCING`; Harvey et al.: alvo de volatilidade.

**Proposta.**
- Degraus: em −5% do pico, risco por trade pela metade e `max_entries_per_cycle` 1; em −10%, modo **REDUCING** (só saídas e stops, sem entradas) em vez de halt total, que hoje para até a gestão das saídas por sinal; halt pegajoso só em −15% ou por falha operacional.
- Perda diária sobre **patrimônio** (realizado + não realizado), com opção de zerar posições de day trade ao atingir o limite; swing mantém o stop.
- **Filtro de mercado** opcional e validado no backtest, não assumido: BTC acima da EMA de 50 dias ou volatilidade realizada de 30 dias do BTC abaixo do percentil 80. O README rejeitou o filtro de 200 dias por falta de backtest publicado; a proposta é testá-lo no próprio walk-forward e ligar só se melhorar a eficiência fora da amostra.
- Throttle por volatilidade: quando a σ de 7 dias do par passa de 2× a de 90 dias, metade do tamanho.

### 6.8 Correlação e concentração (P2)

**Hoje.** Pearson de ~300 retornos horários só contra posições abertas; dois novos pares do mesmo ciclo não são checados entre si; sem beta ao BTC.

**Proposta.** Checar as linhas aceitas no mesmo ciclo entre si; somar o risco aberto por cluster (ρ > 0,7 com o BTC conta como um cluster) e limitar o cluster a `max_open_risk_pct / 2`; mostrar o cluster no painel.

### 6.9 Histerese e custo de giro (P3)

**Evidência.** Novy-Marx e Velikov.

**Proposta.** Saída por sinal do trend e do momentum com banda: entrar em EMA20 > EMA50 e sair só em EMA20 < EMA50 × (1 − 0,5%) ou depois de duas velas fechadas abaixo; testar no walk-forward e comparar o giro.

### 6.10 Ordens maker (P3)

**Evidência.** Barber et al. 2009: quase toda a perda do varejo vem de ordens agressivas. O README rejeitou maker por risco de não executar; o plano de day trade adiou.

**Proposta.** Para o grid e o DCA, cuja lógica é de preço e não de urgência, colocar **ordens LIMIT GTC de compra nos níveis** (post-only quando a Binance aceitar `LIMIT_MAKER`) em vez de comprar a mercado no poll: paga maker, executa dentro do minuto e é o que a Binance e a Pionex fazem. O stop continua protegendo o que já foi comprado; o saldo em USDT reservado pelas ordens de compra não conflita com o stop, que trava o ativo base. Exige contabilizar essas ordens abertas no orçamento diário e no journal.

### 6.11 Instâncias DCA e grid (P2)

Comparadas à Binance, à 3Commas e à Pionex:

- Falta **cooldown entre rodadas** (Binance 60 s, 3Commas por par).
- Falta **máximo de instâncias por par** e global, e tratamento de instância em par com posição de estratégia.
- Falta o aviso de que o grid só rende em lateralização; proposta: a prévia (`backtest/instances.py`) mostra o resultado nos últimos 30, 90 e 180 dias e marca em vermelho se o par saiu da faixa em mais de 30% do tempo.
- Falta a opção "parar e vender tudo" por decisão do operador, sem halt global.
- DCA: expor o capital total exigido na criação (fórmula da Binance) e recusar se passar de `max_exposure_pct`, não só do teto por ordem.
- Para martingale: manter a recusa quando o pior caso passa de `risk_per_trade_pct`; é a proteção que a 3Commas não dá.

### 6.12 Promoção e decaimento (P2)

- Promoção exige PnL líquido positivo em 30 dias; propor também ≥ 30 trades fechados, expectativa em R > 0 e p95 do Monte Carlo dentro do limite; o contador `limit_violations` que a promoção lê nunca é escrito (`promotion.py`), então ou passa a ser gravado pelo gate ou sai da checagem.
- Decaimento com 30 dias é lento; propor também um gatilho por trades: 20 trades fechados com expectativa em R < 0 rebaixa a estratégia a sinal apenas (Freqtrade `LowProfitPairs` faz o equivalente por par).
- Rebaixamento por par: um par com 10 trades e expectativa negativa deixa de operar aquela estratégia.

### 6.13 Idade do sinal e calendário (P3)

- Idade máxima da vela de decisão (OKX `maxLag`): recusar entrada se o ciclo começou mais de 120 s depois do fechamento da vela, para não comprar um rompimento de 15 min já com 3 min de atraso.
- Calendário macro **[convenção, sem artigo]**: opção de pausar entradas de day trade 15 min antes e depois de CPI e FOMC; só se o operador ligar.

### 6.14 Pequenos

- `funding_carry`: `period_return = annual/365` é diário, mas é rotulado como 8h (`funding.py`); corrigir o rótulo.
- Taxa taker fixa em 0,1% sem desconto BNB: ler a taxa real da conta pela API uma vez por dia.
- `flatten_on_halt` desligado por padrão: manter, mas tornar a escolha explícita no mandato.
- Painel: mostrar, por posição, o R atual, o MAE e o MFE; na validação, o Sharpe deflacionado, o número de tentativas e o walk-forward por janela.

---

## 7. O que não fazer

- **Não** adicionar velas de 1 e 5 min: o ATR delas (0,1-0,3% no BTC) não paga 0,2-0,25% de ida e volta; a reversão à média em 15 min já perdeu em 9 pares.
- **Não** adicionar martingale sem pior caso limitado pelo risco por trade; o modelo da 3Commas (quinta ordem a −31% com 16×) é o exemplo do que evitar.
- **Não** usar sazonalidade horária como fonte de alfa; só como filtro de liquidez.
- **Não** relaxar `min_reward_risk` e `cost_multiple` globais para fazer uma estratégia passar; o architect já rejeitou isso no plano de day trade.
- **Não** executar `funding_carry` sem hedge delta-neutro, futuros com escrita habilitada e folga de margem para 30-50% adverso.
- **Não** confiar em retorno anunciado de arbitragem por vendedores.
- **Não** operar alavancado: Heimer-Imas e ESMA mostram que o teto de alavancagem melhora o resultado até de quem tem habilidade.

---

## 8. Plano por fases (proposta, sem implementação)

Cada fase passa pelo architect antes de código, tem testes próprios e termina com a suíte verde.

**P0, segurança de execução (menor e mais urgente)**
1. Stop acionado e não executado → saída de emergência (6.2). Teste com venue falso.
2. Trailing só com folga de 1% entre o preço e o novo stop; métrica da janela sem proteção (6.3).
3. Rótulo do `funding_carry`; leitura da taxa real da conta (6.14).

**P1, sizing e fidelidade**
4. Separar teto de segurança por ordem de tamanho por risco; teto por volatilidade; painel mostra o limite vencedor (6.1).
5. Entrada LIMIT IOC com gap, slippage por notional da ordem, stop e alvo sobre o preço médio executado, slippage realizado por par (6.4).
6. Saída por sinal no timeframe da posição; slippage no swing; alvo swing no fechamento; stop com gap no backtest; decaimento com a mesma métrica (6.6). Digests-ouro atualizados com justificativa.

**P2, validação e freios**
7. Walk-forward com hold-out, Sharpe deflacionado, Monte Carlo de drawdown, 30 trades mínimos, validação por timeframe e amarrada aos pares, métricas em R (6.5).
8. Degraus de drawdown com modo REDUCING; perda diária sobre patrimônio; throttle por volatilidade; filtro de mercado testado no walk-forward (6.7).
9. Correlação entre as entradas do mesmo ciclo e por cluster (6.8).
10. Instâncias: cooldown, máximo por par, prévia com tempo fora da faixa, parar e vender (6.11).
11. Promoção com mínimo de trades e expectativa; decaimento por trades e por par; `limit_violations` gravado ou removido (6.12).

**P3, refinamentos com backtest obrigatório**
12. Histerese nas saídas por sinal (6.9).
13. Ordens maker no grid e no DCA (6.10).
14. Idade máxima da vela; calendário macro opcional (6.13).
15. OCO para o TP1 do day trade (6.3).

Critério de conclusão de cada fase: testes passando, resultado do walk-forward não pior que antes da mudança nas estratégias já validadas, README e `docs/` atualizados.

---

## 9. Ressalvas

- Os números de risco profissionais (0,5-1% por trade, 2-3% ao dia, −10%/−20%) são convenções de mesa e citações secundárias, não resultados de artigo revisado.
- Os limiares de PBO, Sharpe deflacionado e eficiência de walk-forward são sugestões desta revisão.
- As páginas de FAQ da Binance usadas são antigas em parte; confirmar limites na interface antes de copiar valores.
- A lacuna 6.2 foi confirmada por leitura do código (`executor.py:1086-1090`), não por reprodução no testnet.
- A conclusão 6.1 vem da fórmula de `risk.py:69-85` com os padrões de `config.py`; um operador com `max_order_notional_pct` alto já teria o sizing por risco funcionando.
- Nenhum estudo revisado mostra day trade só comprado em cripto spot lucrativo depois de 0,2% por ida e volta; a prior continua "sem vantagem", e nada neste documento muda isso.

---

## 10. Fontes

Acadêmicas
- Barber, Odean, *Trading Is Hazardous to Your Wealth*: https://faculty.haas.berkeley.edu/odean/papers/returns/returns.html
- Barber, Odean, *Boys Will Be Boys*: https://faculty.haas.berkeley.edu/odean/papers/gender/gender.html
- Barber, Lee, Liu, Odean, *Just How Much Do Individual Investors Lose by Trading?*: https://faculty.haas.berkeley.edu/odean/papers%20current%20versions/justhowmuchdoindividualinvestorslose_rfs_2009.pdf
- Barber, Lee, Liu, Odean, *The Cross-Section of Speculator Skill*: https://faculty.haas.berkeley.edu/odean/papers/day%20traders/Day%20Trading%20Skill%20110523.pdf
- Barber, Lee, Liu, Odean, *Learning, Fast or Slow*: https://faculty.haas.berkeley.edu/odean/papers/day%20traders/day%20trading%20and%20learning%20110217.pdf
- Chague, De-Losso, Giovannetti, *Day Trading for a Living?*: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3423101
- Odean, *Are Investors Reluctant to Realize Their Losses?*: https://faculty.haas.berkeley.edu/odean/papers/disposition/disposition.html
- Heimer, Imas, *Biased by Choice*: https://ideas.repec.org/a/oup/rfinst/v35y2022i4p1643-1681..html
- Hasso, Pelster, Breitmayer: https://ideas.repec.org/a/eee/beexfi/v23y2019icp64-74.html
- Kogan, Makarov, Niessner, Schoar, *Are Cryptos Different?*: https://mitsloan.mit.edu/cfi/forthcoming-crypto-trading-paper
- BIS Bulletin 69: https://www.bis.org/publ/bisbull69.htm
- Schmeling, Schrimpf, Todorov, *Crypto Carry*: https://www.bis.org/publ/work1087.pdf
- Brock, Lakonishok, LeBaron: https://ideas.repec.org/a/bla/jfinan/v47y1992i5p1731-64.html
- Sullivan, Timmermann, White: https://www.fmg.ac.uk/sites/default/files/2020-11/dp303.pdf
- Park, Irwin: https://ideas.repec.org/a/bla/jecsur/v21y2007i4p786-826.html
- Moskowitz, Ooi, Pedersen, *Time Series Momentum*: https://papers.ssrn.com/abstract=2089463
- Liu, Tsyvinski: https://www.nber.org/system/files/working_papers/w24877/w24877.pdf
- Liu, Tsyvinski, Wu, *Common Risk Factors in Cryptocurrency*: https://cowles.yale.edu/sites/default/files/2022-10/LiuTsyvinskiWu2019%20COMMON%20RISK%20FACTORS.pdf
- Novy-Marx, Velikov, *A Taxonomy of Anomalies and Their Trading Costs*: https://www.nber.org/papers/w20721
- Baur, Cahill, Godfrey, Liu, *Bitcoin time-of-day effects*: https://research-repository.uwa.edu.au/en/publications/bitcoin-time-of-day-day-of-week-and-month-of-year-effects-in-retu/
- Harvey, Liu, *Backtesting*: https://people.duke.edu/~charvey/Research/Published_Papers/P120_Backtesting.PDF
- Bailey, Borwein, López de Prado, Zhu, *The Probability of Backtest Overfitting*: https://papers.ssrn.com/abstract=2326253
- Moreira, Muir, *Volatility-Managed Portfolios*: https://www.nber.org/papers/22208
- Harvey et al., *The Impact of Volatility Targeting*: https://people.duke.edu/~charvey/Research/Published_Papers/P135_The_impact_of.pdf
- MacLean, Thorp, Ziemba, *Good and Bad Properties of the Kelly Criterion*: https://www.stat.berkeley.edu/~aldous/157/Papers/Good_Bad_Kelly.pdf

Profissionais
- Van Tharp, R-múltiplos: https://traders.com/Documentation/FEEDbk_docs/2007/01/Abstracts_new/Tharp/tharp.html
- Regras dos Turtles (secundário): https://www.thechartist.com.au/being-proactive-with-position-sizing/
- Tom Basso (entrevista): https://michaelsincere.com/articles/marketwatch-interview-with-market-wizard-legendary-trader-tom-basso
- Pardo, *The Evaluation and Optimization of Trading Strategies*: https://www.wiley-vch.de/en/areas-interest/finance-economics-law/the-evaluation-and-optimization-of-trading-strategies-978-0-470-12801-5
- Kevin Davey: https://algoadvantage.substack.com/p/036-kevin-davey-part-i-its-all-about

Bots e frameworks
- Binance Spot Grid: https://www.binance.com/en/support/faq/what-is-spot-grid-trading-and-how-does-it-work-d5f441e8ab544a5b98241e00efb3a4ab
- Binance Spot Grid parâmetros automáticos: https://www.binance.com/en/support/faq/how-to-use-spot-grid-trading-auto-parameters-76bd4effa3c4456c971a1c6835762742
- Binance Futures Grid: https://www.binance.com/en/support/faq/what-is-futures-grid-trading-f4c453bab89648beb722aa26634120c3
- Binance Spot DCA: https://www.binance.com/en/support/faq/binance-spot-dca-parameters-797f6e465186474fac2add2f7ce0474a
- Binance Rebalancing Bot: https://www.binance.com/en/support/faq/what-is-rebalancing-bot-and-frequently-asked-questions-29bbbd2e7fc24085be7a8a7d02779457
- Binance Futures TWAP (API): https://developers.binance.com/docs/algo/future-algo/Time-Weighted-Average-Price-New-Order
- Bybit Futures Combo: https://www.bybit.com/en/help-center/article/Futures-Combo-Bot-Parameters-Explained
- OKX Signal Bot: https://www.okx.com/help/signal-bot-alert-message-specifications
- KuCoin bots: https://www.kucoin.com/support/31880987809689
- 3Commas DCA: https://help.3commas.io/en/articles/3108940-main-settings-dca-bot
- Pionex Grid FAQ: https://intercom.help/pionex/en/articles/15500905-grid-trading-bot-faq
- Bitsgap COMBO: https://bitsgap.com/helpdesk/article/10043546796572-Advanced-COMBO-Bot-Settings
- Altrady Grid: https://help.altrady.com/en/article/grid-bot-settings-1kiinnw
- Freqtrade configuração: https://www.freqtrade.io/en/stable/configuration/
- Freqtrade stoploss: https://www.freqtrade.io/en/stable/stoploss/
- Freqtrade proteções: https://www.freqtrade.io/en/stable/plugins/
- Freqtrade backtesting (premissas): https://www.freqtrade.io/en/stable/backtesting/
- Hummingbot Position Executor: https://hummingbot.org/strategies/v2-strategies/executors/positionexecutor/
- Hummingbot pure market making: https://hummingbot.org/strategies/v1-strategies/pure-market-making/
- QuantConnect risk models: https://www.quantconnect.com/docs/v2/writing-algorithms/algorithm-framework/risk-management/supported-models
- NautilusTrader risk: https://nautilustrader.io/docs/latest/api_reference/risk
- vectorbt Portfolio: https://vectorbt.dev/api/portfolio/base/

---

## 11. Status da implementação

Data: 2026-10-20. O plano da seção 8 foi implementado em quatro ondas, depois do desenho aprovado pelo architect, que fixou as decisões D1 a D27. Onda 1: `0078587`. Onda 2: `a794831`. Onda 3: `14e9047`. Onda 4: compras paradas no livro dos robôs (6.10), as correções da revisão das ondas 1 a 3 e esta documentação. O README descreve o comportamento atual.

| Item | Passo | Onda | Status | Decisões |
|---|---|---|---|---|
| 6.1 Sizing | 4 | 1, 2, 3 | implementado com mudança | D1, D2, D3 |
| 6.2 Stop acionado e não executado | 1 | 1 | versão mínima | D15 |
| 6.3 Folga do trailing e métrica da janela | 2 | 2, 3 | implementado com mudança | D16 |
| 6.3 OCO no TP1 do day trade | 15 | 4 (decisão documentada) | versão mínima | D17 |
| 6.4 Entrada LIMIT IOC e slippage | 5 | 1, 2, 3 | implementado com mudança | D4 |
| 6.5 Validação (walk-forward, hold-out, DSR, Monte Carlo, 30 trades, timeframes, pares) | 7 | 1, 2, 3 | implementado com mudança | D6, D7, D8, D9, D10, D11, D12 |
| 6.5 PBO | 7 | 1 | versão mínima | D9 |
| 6.6 Fidelidade entre ao vivo e histórico | 6 | 1, 2, 3 | implementado com mudança | D24, D25, D26 |
| 6.7 Degraus de drawdown, perda diária, throttle, filtro de mercado | 8 | 1, 2, 3 | implementado com mudança | D5, D13, D20 |
| 6.8 Correlação no mesmo ciclo e cluster do BTC | 9 | 2, 3 | implementado | — |
| 6.9 Histerese | 12 | 1, 3 | implementado | D14 |
| 6.10 Ordens maker no grid e no DCA | 13 | 4 | implementado com mudança | D18 |
| 6.11 Instâncias DCA e grid | 10 | 1, 2, 3 | implementado | D27 |
| 6.12 Promoção e decaimento | 11 | 2, 3 | implementado com mudança | D22, D26 |
| 6.13 Idade do sinal e calendário macro | 14 | 1, 3 | implementado | D21 |
| 6.14 Rótulo do funding e taxa real da conta | 3 | 1, 3 | implementado | D19 |
| 6.14 `flatten_on_halt` no mandato e painel (R, MAE, MFE, validação) | 3 | 2, 3 | implementado | D23 |

O que mudou em relação à proposta, item a item:

- **6.1.** O teto por volatilidade usa o ATR14 diário com `vol_cap_pct` de 1%, não os 0,2-0,3% citados: com os stops deste robô (1,5 a 2,5 ATR), 0,2% traria de volta o notional fixo pequeno que o item critica. O alvo de volatilidade só limita entradas novas; nada é vendido para rebalancear. O teto por ordem de 25% vale só para instalações novas: o teto entra no mandato, e um `bot.json` gravado mantém o seu até o operador mudar e reconfirmar.
- **6.3.** A folga do trailing é metade da distância do stop, entre 0,5% e 1%, não 1% fixo: com 1% fixo, um `trailing_tp` de distância curta não andaria mais, e o ao vivo divergiria do replay.
- **6.4.** O slippage estimado continua pelo topo do livro, agora com o notional da própria ordem; caminhar pelos níveis exigiria ler `/depth`. O gap de 0,2% do IOC limita o custo real. Os robôs DCA e grid continuam comprando a mercado.
- **6.5.** A validação é amarrada ao conjunto de pares, não a uma tabela estratégia × par: cada par teria poucos trades para o mínimo de 30. O walk-forward corta o período em 4 janelas iguais mais 20% de hold-out, sem reajuste, porque nada é parametrizado. O Monte Carlo em R é gravado na validação e conferido no ciclo contra os limites atuais (`p95 × risk_per_trade_pct < max_drawdown_pct`), então mudar limites não exige validar de novo. Os dias por timeframe são 120 (15m e day), 240 (1h) e 480 (4h); todas as estratégias em todos os timeframes, com 15 pares, levaram cerca de 3 minutos em dados sintéticos.
- **6.6.** O slippage do swing é o mesmo 0,03% por perna do day, não o medido por par: a validação não deve depender do histórico ao vivo. O modelo de gap do stop já existia (executa na abertura) e ganhou teste. Os indicadores ficam em média simples, por decisão.
- **6.7.** O filtro de mercado é o BTC acima da EMA de 240 velas de 4 h (cerca de 40 dias): a coleta de 300 velas não aquece uma EMA 50 diária, e o percentil de volatilidade precisaria de um ano de BTC diário ao vivo. Fica desligado por padrão e entra na chave da validação. O throttle compara 7 dias com as velas de 4 h lidas (cerca de 50 dias), não com 90 dias.
- **6.10.** Sem `LIMIT_MAKER`, que seria um tipo de ordem novo no pacote de execução portado. No máximo uma compra parada por robô, por opção. Na faixa VIP0 maker e taker custam 0,1%, então o ganho é o preço, não a taxa.
- **6.12.** O contador `limit_violations`, que nada gravava, foi trocado pela contagem de violações quantitativas no ledger de auditoria do testnet em 30 dias. O decaimento compara o Sharpe diário ao vivo com dias zero contra o Sharpe diário do backtest, também com dias zero.

### Versões mínimas

**PBO não calculado (6.5).** O CSCV de Bailey e López de Prado mede a chance de a melhor configuração de uma família otimizada ser só sorte. O robô não otimiza parâmetros: as estratégias rodam com constantes fixas, então não há família de configurações. Calcular PBO sobre as tentativas gravadas misturaria períodos e conjuntos de pares diferentes, e o número não significaria nada. A correção para múltiplos testes é o Sharpe deflacionado com N igual às combinações testadas desde a última aprovação. O painel diz "PBO não calculado: sem otimização de parâmetros". Se um dia o robô passar a otimizar parâmetros, o PBO volta a fazer sentido.

**OCO no TP1 não construída (passo 15).** Uma OCO é uma lista de ordens que o journal, a chave de idempotência, a avaliação do gate e a reconciliação não modelam. A perna limitada dela executaria num pavio, enquanto o TP1 validado vende no fechamento de 15 min: o ao vivo divergiria do histórico. O stop teria de virar duas ordens. O ganho é uma janela de poucos segundos sem stop em metade da posição; o limite de 5 ordens algorítmicas da Binance não é o problema (seriam 2). A versão mínima mede a janela sem stop de cada troca (trailing, TP1, venda parcial, recolocação, resto de saída), com média, p95 e máximo em Desempenho. Gatilho para rever: p95 da janela do TP1 acima de 2 s em 30 dias, ou qualquer `stop_gapped` durante um TP1.

**Sem MARKET no stop atravessado (6.2).** O stop preso é detectado pelo preço: ordem ainda aberta e bid abaixo de `stop × (1 − 0,5%)`. O robô cancela o stop e vende com um IOC 0,5% abaixo do bid, depois com um IOC 2% abaixo, e entra em halt se ainda sobrar posição. Se o cancelamento do stop falha, o stop fica no livro e o robô tenta de novo no ciclo seguinte; o halt vem no terceiro ciclo seguido. A proposta previa MARKET em produção como último recurso; ficou de fora porque seria um caminho só de produção, que o Spot Testnet não exercita e os testes não cobrem. Um IOC limitado a 2% é o que o `stoploss_on_exchange_limit_ratio` do Freqtrade faz. Um IOC de resultado desconhecido também leva ao halt, para não vender duas vezes.

### Desvios combinados na integração

- **`bot.json` antigo com drawdown de 50% ou mais.** A derivação do desenho (`halt = min(50%, max(15%, 1,5 × limite))`) poria o halt abaixo do limite, e o arquivo deixaria de carregar. Nesse caso o halt fica em 1,5 vez o limite, no máximo 100%; o degrau de cautela continua na metade do limite.
- **Mudar só `max_drawdown_pct` pela API recalcula os degraus.** `PUT /bot/config` mescla os limites gravados com os enviados. Mantidos, os degraus antigos podiam ficar fora de ordem e recusar a mudança (422). Quando o limite muda sem os degraus no corpo, um degrau gravado que continua em ordem fica; só o que o novo limite deixa fora de ordem sai e é derivado de novo. Um degrau de cautela em 0 continua desligado.
- **Um halt retomado fica em só reduzir.** O degrau de halt é gravado em `bot_state.risk_state` como `reducing`. Depois de `POST /bot/resume`, nenhuma compra sai até o drawdown cair abaixo de `dd_reduce_pct` (ou de `max_drawdown_pct`, com a cautela desligada). Stops e saídas continuam.
- **O filtro de mercado falha fechado sem dado do BTC.** Ligado, um ciclo em que o BTC/USDT não foi lido bloqueia toda entrada ("BTC sem leitura neste ciclo"). Com menos de 240 velas de 4 h fechadas, ele deixa passar, como no desenho.
- **O nível de risco é calculado antes dos robôs.** O desenho punha o cálculo depois das saídas e dos robôs. Na integração ele foi para depois das saídas e antes dos robôs, uma vez por ciclo: robôs e estratégias obedecem ao mesmo nível no mesmo ciclo. Só reduzir, halt e pausa diária bloqueiam as compras dos robôs; a cautela não as reduz.

### Correções da revisão das ondas 1 a 3

O reviewer aceitou as ondas 1 a 3 com cinco correções, todas aplicadas na onda 4 com testes (`tests/test_bot_review_fixes.py`, `tests/test_bot_resting_buys.py`):

- **Margem de taxa contra o gate (alta).** `market_rules` troca a taxa taker pela da conta, mas o order gate confere o saldo com a taxa padrão do mercado. Com taxa de conta menor, o stop pedia mais moeda do que o gate aceita, era recusado e o robô parava com a posição sem stop. As quantidades de stop e de venda agora usam a maior das duas taxas, também quando o stop é ajustado ao saldo livre. A taxa da conta continua sendo a contabilizada e a do score.
- **Pausa diária só bloqueia compras (alta).** Antes ela desligava o ciclo inteiro, inclusive o stop atravessado, o stop móvel e as saídas, até o operador reiniciar depois de 00:00 UTC. Com a perda diária sobre o patrimônio e ordens maiores, ela ficaria frequente. Agora toda compra é recusada até o dia seguinte, e o resto do ciclo continua.
- **Cancelamento do stop atravessado (média).** Uma falha passageira ao cancelar levava ao halt com o stop ainda no livro. Agora o robô tenta de novo nos ciclos seguintes e só entra em halt no terceiro seguido.
- **Monte Carlo ausente (baixa).** Uma validação sem drawdown p95 legível deixava a linha operar; agora ela fica só como sinal até validar de novo.
- **Leitura da taxa da conta (baixa).** A leitura roda numa thread própria e o ciclo espera no máximo 2 s, usando o que já estiver em cache.

Uma segunda revisão, sobre a onda 4, aceitou com quatro correções, também aplicadas com testes: Pausar o ciclo é recusado enquanto uma compra parada não se confirma cancelada ou a parte comprada não tem stop; só um bloqueio de escrita deixa de contar para o halt do stop atravessado; uma compra parada já confirmada pela Binance não é esquecida numa única leitura "não encontrada"; e uma recolocação de stop pendente não esconde mais a checagem do stop atravessado.

### Revisão rigorosa da branch

Uma revisão rigorosa da branch inteira, em seis fatias, reprovou o código com 12 blockers e 14 majors; todos foram corrigidos com testes, e uma verificação independente confirmou cada item. Os mais importantes:

- **Sharpe com ruído de ponto flutuante.** 30 stops idênticos davam Sharpe por trade de −2×10¹⁵, que envenenava a variância do Sharpe deflacionado e travava a estratégia para sempre. A dispersão agora usa tolerância relativa, e tentativas com Sharpe não finito ou acima de 5 em valor absoluto ficam fora da variância.
- **Decaimento depois da revalidação.** O Sharpe ao vivo usa só os dias desde a validação.
- **Estado de risco fora do ciclo.** Criar um robô lê o nível gravado e não grava nada; só o ciclo, com preços, mede o dia.
- **Pausar, retomar e iniciar.** Pausar nunca liga um ciclo parado; retomar e iniciar resolvem antes as compras paradas que o halt deixou.
- **Stop atravessado.** Exige também que o último negócio tenha chegado ao stop, que é o que dispara um STOP_LOSS_LIMIT.
- **Exposição do mandato.** As compras paradas dos robôs contam na exposição das compras, mas não na das vendas: um stop de outro par nunca é recusado por causa delas.
- **Uma regra para o Monte Carlo** (`risk.mc_drawdown_ok`) e **uma derivação dos tetos do mandato** (`mandate_caps_for`), usadas pelo ciclo, pela API e pelo painel.
