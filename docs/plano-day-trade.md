# Plano: modo day trade (v3, revisado pelo architect)

v3: emendas aprovadas pelo architect depois da revisão da implementação (retorno esperado no TP1, 6 ordens por entrada, `keep_qty` gravado no TP1).

## Objetivo

Operar em janela curta, entrando e saindo no mesmo dia (de minutos a poucas horas), em busca de lucro rápido. O robô continua só comprando e só no spot. Cada estratégia continua obrigada a passar no backtest antes de operar fora do modo teste.

## O que a evidência diz

- **Day trade costuma perder.**
  - Taiwan (Barber, Lee, Liu, Odean): menos de 1% dos day traders tem lucro previsível depois dos custos.
  - Brasil (Chague, De-Losso, Giovannetti, 2019): 97% de quem insistiu por mais de 300 pregões perdeu dinheiro.
  - O que derruba o resultado é custo somado à frequência, e são essas as duas alavancas que um robô controla.
- **O custo aqui é cerca de 0,2% por ida e volta** (taker de 0,1% por lado), mais 0,02% a 0,05% de slippage nos pares grandes. Velas de 1 a 5 minutos têm ATR de 0,1% a 0,3% no BTC e não pagam esse custo. O nosso backtest confirmou: a reversão à média em 15 minutos perdeu em todos os 9 pares.
- **Nenhum estudo revisado por pares mostra day trade só comprado em cripto spot lucrativo depois dos custos.** O que tem mais apoio:
  - momento intradiário quando volume e volatilidade estão altos (Gao, Han, Li, Zhou 2018; Shen, Urquhart, Wang 2022 para BTC);
  - tendência no 1 h com entrada no 15 min;
  - pullback dentro da tendência (De Nicola 2021);
  - evitar as horas sem liquidez (cerca de 03:00 a 07:00 UTC).
- **Prática profissional:**
  - risco de 0,25% a 1% por trade;
  - perda máxima diária de 2% a 3%;
  - metade realizada em 1 a 1,5R, com o stop levado ao ponto de equilíbrio;
  - stop de tempo;
  - pausa depois de perdas;
  - nada de operar nas horas mortas.
- **O Freqtrade faz o mesmo** com `minimal_roi` (saída por tempo) e com proteções como StoplossGuard e CooldownPeriod.

## Desenho

### 1. Estilo por estratégia, não um interruptor global

- Cada estratégia ganha o atributo de classe `style` (`"swing"` ou `"day"`) e o atributo `history_days`, que é o histórico usado na validação. As atuais mantêm 60, 180 e 365 dias. As novas estratégias de day trade usam 90.
- As estratégias de day trade aparecem em Controles e ligam e desligam como as outras. Sempre usam a política de saída `exit_policy="day"`, gravada na posição. As estratégias swing continuam com `config.exit_policy` (trailing ou target).
- Um único par só pode ter uma posição viva entre todas as estratégias: uma posição de day trade bloqueia entradas swing naquele par, e vice-versa. Essa é a regra atual do `allocate`.
- **Preset day**, uma ação do painel que grava limites e mostra antes o que muda:
  - risco de 0,5% por trade;
  - perda diária máxima de 2%;
  - no máximo 8 trades por dia;
  - pausa de 60 min no par depois de uma perda;
  - trava de perdas: 3 em 6 h;
  - `cost_multiple` 3;
  - `min_reward_risk` 1,2.

  O preset reconfirma o mandato na mesma ação, por causa do item 5. A prévia mostra o teto efetivo por ordem: com `max_order_notional_pct` baixo, o risco de 0,5% num stop de 1% fica limitado ao teto.
- **Parâmetros de day trade como constantes das estratégias**, não da configuração do operador, para haver menos campos na chave de validação:
  - `MIN_STOP_PCT` = 1,0%;
  - `STOP_ATR` = 1,5;
  - `TP1_R` = 1,5;
  - `TP1_FRACTION` = 0,5;
  - `BREAKEVEN_BUFFER` = 0,2%;
  - `MAX_HOLD_MINUTES` = 360;
  - `QUIET_HOURS_UTC` = 03 a 07.

### 2. Duas estratégias de day trade (`bot/strategies/`)

As duas entram no 15 min com filtro no 1 h. Assinatura:

```python
evaluate(symbol, "15m", frame, *, hourly: DataFrame | None)
```

O padrão é o mesmo do kwarg do FundingCarry em `_setup`. Ao vivo, `hourly = _closed(snapshot, symbol, "1h", moment)`. O 1 h vem das velas de 1 h, nunca reamostrado do 15 min, para o aquecimento da EMA 50 ser igual ao do ao vivo.

**`intraday_momentum`**
- Filtro no 1 h: EMA 20 acima da EMA 50, e fechamento acima da EMA 50.
- Gatilho no 15 min: o fechamento rompe a máxima das 8 velas anteriores, o volume fica acima de 1,5× a mediana de 20 velas, e o preço está acima da VWAP do dia.

**`vwap_pullback`**
- Mesmo filtro no 1 h.
- Gatilho no 15 min: a vela anterior fechou abaixo da VWAP do dia ou da EMA 20, a última voltou para cima, e o RSI(14) está entre 35 e 55.
- Stop: abaixo da mínima das 2 últimas velas.

**Regras comuns às duas**
- **Stop:** o maior entre `STOP_ATR` × ATR(14) do 15 min e `MIN_STOP_PCT`. No pullback, o maior entre isso e a distância até a mínima.
- **Alvo:** `target_pct = TP1_R × stop_pct` (é o TP1).
- **`expected_return`:** igual ao TP1 (`target_pct = TP1_R × stop_pct`). O score, os filtros de custo e de ganho/risco e o payoff do Kelly medem a perna do TP1, o alvo em R do trade; só o gate do backtest mede a expectativa combinada das duas pernas. O valor combinado da v2 (`0,5 × TP1 + 0,5 × buffer`) falhava no próprio preset: ganho/risco de 0,60, abaixo de 1,2, bloqueava toda linha. Aprovado pelo architect na v3. O preço do TP1 vem de `setup.target_pct`.
- **`confidence`:** definida a partir do volume relativo e da força do 1 h. Sem isso o score fica em 0 e toda linha é bloqueada.
- **Avaliador:** `regime_factor()` ganha as duas ids (`trend_up` = 1; `range` ganha um fator menor no pullback). Teste: um setup canônico de cada estratégia chega a "pronta".
- **VWAP** (`bot/indicators.py`): preço típico × volume somado só nas velas do dia UTC corrente, com fuso. Exige pelo menos 4 velas desde a meia-noite, senão a condição de VWAP não conta.
- **Horas mortas:** um helper único, `in_quiet_hours(decision_time)`, em que o horário de decisão é a abertura da vela mais 15 min. É usado pelo `_score`, que bloqueia com "hora sem liquidez", e pelo `simulate`.

### 3. Saída do day trade (v1: TP1, stop no break-even e prazo; sem stop móvel depois do TP1)

**Colunas novas em `positions`**, aditivas, via `_ADDED_POSITION_COLUMNS`:
- `tp1_done`;
- `keep_qty`;
- `exit_deadline`.

O TP1 fica na coluna `target_price`, que já existe. Só o `exit_deadline` é gravado no `open_pending`, para que uma entrada resolvida depois pelo `sync_positions` já tenha o prazo. O `keep_qty` é gravado no início do TP1, antes da primeira ordem: na reserva a quantidade executada ainda não é conhecida.

**`exit_position` generalizado** para `exit_position(..., keep_qty=None, leg="exit", next_stop=None)`, em vez de um caminho novo:

1. **Antes de tirar o stop**, as mesmas checagens do `trail()`:
   - `write_blocker`;
   - peso da API;
   - teto de notional;
   - break-even abaixo de `bid × (1 − STOP_LIMIT_GAP)`;
   - `orders_remaining ≥ 2 + reserva`.

   Se alguma falhar, o TP1 fica para o próximo ciclo e o stop segue no book.
2. **Vende só até `keep_qty`.** Uma nova tentativa depois de IOC parcial, de resultado desconhecido ou de queda do processo vende só até `keep_qty`, nunca outra fração do que sobrou.
3. **Chave de idempotência própria**, com a perna `"tp1"`. Uma saída total depois não reaproveita a tentativa e não cai em `idempotency_conflict`.
4. **Mínimo da corretora (`min_notional`), sem nunca chamar `_close` no modo parcial:**
   - se o pedaço a vender fica abaixo do mínimo, o TP1 é pulado e o stop fica;
   - se a sobra fica abaixo do mínimo, vende tudo.
5. **Venda completa:**
   - o `stop_price` passa a ser o preço de entrada × (1 + `BREAKEVEN_BUFFER`);
   - `tp1_done` = verdadeiro;
   - depois `protect()`.

   Se o stop for recusado, segue o caminho existente: saída de emergência ou `UnprotectedPosition` e halt.
6. **IOC parcial:** recoloca o stop no nível antigo e mantém `tp1_done` falso.

**Runtime, para `exit_policy="day"`:**
- `_exit_reason` não usa "alvo atingido", porque o alvo é o TP1. Usa o prazo vencido, com o motivo "tempo máximo do day trade".
- O TP1 dispara com o fechamento da última vela de 15 min ≥ `target_price`, o mesmo critério do backtest.
- `_trail` ignora posições de day trade.

**Ressalva no README:** o "break-even" pode executar cerca de 0,3% abaixo da entrada por causa do gap de 0,5% do stop-limit, ou não executar num gap.

### 4. Orçamento de ordens e mandato

- Uma ida e volta de day trade gasta 5 ordens (entrada, stop, venda do TP1, novo stop, saída), em vez de 3.
  - Na entrada, `ORDERS_PER_ROUND_TRIP` passa a ser 5 para estratégias de day trade.
  - O TP1 exige `orders_remaining ≥ 2 + reserva`.
- O mandato limita ordens por dia a `ORDERS_PER_ENTRY × max_trades_per_day`.
  - Com estratégia de day trade ligada, conta 6 por entrada (`ORDERS_PER_DAY_ENTRY`): as 5 da ida e volta mais um stop recolocado, no mesmo padrão do swing (3 + 1 = 4).
  - Isso muda o mandato, então o preset reconfirma o mandato e o painel avisa.
  - Com 8 trades por dia: 48 ordens.

### 5. Backtest fiel (`bot/backtest/runner.py`)

**Assinatura:** `simulate(..., hourly=None, day=None)`.
- O `hourly` é recortado sem olhar o futuro: só velas de 1 h com `index + 1h ≤ vela de 15 min + 15 min`, nas últimas 299.
- `_run_validation` busca 15 min e 1 h.
- Um teste prova que a hora em formação nunca aparece.

**Regras com `day`:**
- Entrada só fora das horas mortas, pelo helper comum.
- Na mesma vela, o stop ganha do TP1.
- O TP1 dispara quando o fechamento passa do alvo, com execução no fechamento (conservador, igual ao ao vivo).
- O break-even vale a partir da vela seguinte.
- O stop de tempo sai no fechamento da vela `entrada + ceil(360/15)`.
- **Retorno:** `f·r1 + (1−f)·r2`, menos a taxa taker e o slippage de 0,03% em cada perna.

**`day=None` não muda nada:**
- Antes de mexer no `simulate`, um digest-ouro da lista de trades (target e trailing, nos mesmos candles sintéticos) é gravado e verificado.
- Digests-ouro também para as duas estratégias novas.

**Gate do day trade.** O Sharpe por trade fica fácil demais com muitos trades: com 100 trades em 90 dias, basta média/σ de cerca de 0,05. Por isso, para estratégias de day trade:
- Sharpe sobre os retornos **diários** agregados, acima de 1;
- retorno líquido médio por trade de pelo menos 0,10%;
- pelo menos 30 trades;
- drawdown abaixo de 20%;
- as duas metades lucrando.

**Chave de validação.** Os parâmetros de day trade e o `exit_policy="day"` vão para os `params` do `promotion_record` e são comparados como hoje se compara o `exit_policy`.

### 6. Painel

- **Controles:** as duas estratégias novas, com o selo "day trade", e o botão "Aplicar preset day trade", com a prévia das mudanças e do teto efetivo por ordem.
- **Posições:** o selo "day", o TP1 feito ou não, o stop no break-even e a contagem regressiva até o prazo.
- **Gráfico do trade:** a linha do TP1, a linha do break-even depois do TP1 e uma marca no prazo.

### 7. README

Uma seção "Day trade" com a evidência acima, sem prometer lucro: o que cada regra faz, a ressalva do break-even e o novo orçamento de ordens.

## Fora do escopo (v1)

- Stop móvel depois do TP1. Será testado no backtest antes de entrar.
- Ordens maker na entrada.
- Velas de 1 e 5 min.
- Vender a descoberto.

## Testes

- **Estratégias:**
  - entrada e recusa (filtro do 1 h, volume, VWAP, horas mortas, piso do stop);
  - setup canônico chegando a "pronta";
  - digests-ouro.
- **VWAP:** virada da meia-noite UTC e o mínimo de velas.
- **Simulador:**
  - o digest-ouro com `day=None`;
  - o TP1 pelo fechamento;
  - o stop ganhando na mesma vela;
  - o break-even na vela seguinte;
  - o stop de tempo;
  - as horas mortas;
  - o retorno ponderado com slippage;
  - o 1 h sem olhar o futuro;
  - o gate com Sharpe diário e o retorno mínimo.
- **Executor:**
  - TP1 vende até `keep_qty` e recoloca o stop no break-even;
  - nova tentativa depois de IOC parcial;
  - pedaço abaixo do mínimo pula o TP1;
  - sobra abaixo do mínimo vende tudo;
  - a linha nunca fecha com quantidade sobrando;
  - stop recusado vai para o `protect`;
  - orçamento de 2 + reserva;
  - chave da perna `tp1`.
- **Runtime:**
  - o prazo vencido sai;
  - o TP1 roda uma vez só;
  - o day trade não usa "alvo atingido" nem `_trail`;
  - uma posição pendente resolvida depois mantém o prazo.
- **Store:** a migração das colunas.
- **API:** o preset com o mandato reconfirmado, os campos novos nas posições e a validação buscando 15 min e 1 h.

## Fontes (pesquisa)

- Barber, Lee, Liu, Odean: https://faculty.haas.berkeley.edu/odean/papers/day%20traders/Day%20Trading%20Skill%20110523.pdf
- Chague, De-Losso, Giovannetti 2019: https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3423101
- BIS Bulletin 69: https://www.bis.org/publ/bisbull69.htm
- Gao, Han, Li, Zhou: https://profiles.wustl.edu/en/publications/market-intraday-momentum/
- Shen, Urquhart, Wang: https://research.birmingham.ac.uk/en/publications/bitcoin-intraday-time-series-momentum/
- Concretum: https://concretumgroup.com/seasonality-in-bitcoin-intraday-trend-trading/
- De Nicola 2021: https://epub.ub.uni-muenchen.de/97651/
- Hudson e Urquhart 2019: https://link.springer.com/article/10.1007/s10479-019-03357-1
- Quantpedia: https://quantpedia.com/periodicity-in-cryptocurrencies-recurrent-patterns-in-volatility-and-volume/
- Freqtrade: https://www.freqtrade.io/en/stable/configuration/ e https://www.freqtrade.io/en/stable/plugins/
