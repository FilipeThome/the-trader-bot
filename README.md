# The Trader Bot

A local service that analyses USDT pairs on Binance and only sends an order through the order gate ported from Vibe-Trading. The operator panel reads the state and sends pause, stop and limit changes. It never talks to the exchange.

This is not investment advice. No strategy guarantees a profit. The return shown in the panel is net of fees, slippage and funding.

The panel is in Portuguese. This README uses the English term and gives the panel's label in parentheses on first use.

## Contents

- [Quick start](#quick-start)
- [What v1 does](#what-v1-does)
- [Fixed target and stop](#fixed-target-and-stop)
- [New coins](#new-coins)
- [Live trade](#live-trade)
- [How the bot decides](#how-the-bot-decides)
- [Day trading](#day-trading)
- [DCA and grid robots](#dca-and-grid-robots)
- [DeepSeek advisor](#deepseek-advisor)
- [Repository layout](#repository-layout)
- [Operation](#operation)
- [Environment variables and runtime files](#environment-variables-and-runtime-files)
- [Performance and concurrency](#performance-and-concurrency)
- [Docker](#docker)
- [Tests](#tests)
- [Execution journal recovery](#execution-journal-recovery)

## Quick start

Python 3.12 and Node 22. From the repository root:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest
cd ui
npm install
npm run dev
```

In another terminal, with an explicit runtime root (never keep keys in the source folder):

```powershell
$env:BOT_RUNTIME_ROOT = "$env:USERPROFILE\.the-trader-bot"
python -m bot
```

Panel at `http://127.0.0.1:5173`, API at `http://127.0.0.1:8000`. The first configuration (Spot Testnet keys, capital, mandate, strategies) is in [Operation](#operation). `docker compose up -d --build` runs both services in containers; see [Docker](#docker).

## What v1 does

- Spot on BTC, ETH, SOL, BNB and XRP. USD-M futures are read-only: the funding carry appears in the panel with its estimated daily return, and never executes.
- Four families: trend (1h and 4h), momentum (4h), mean reversion (15m and 1h) and funding carry (signal only).
- Testnet by default (`binance-paper-trade`). Production (`binance-live-trade-bot`) only after the gates, 30 days of positive net PnL and 30 trades with positive expectancy in R, a crypto-only mandate and the word `PRODUCAO`.
- No order without a mandate, a latch and a journal record before the send. A halt never disarms itself.
- Every buy gets a STOP_LOSS_LIMIT on Binance right after the fill, so the position stays protected even with the bot off. Target and strategy exit are checked every cycle: the bot cancels the stop and sells with LIMIT IOC orders.
- **LIMIT IOC entry.** The buy is a LIMIT IOC order 0.2% above the ask, with the quantity in base asset. Whatever fills opens the position; the rest is cancelled at once, and with no fill the row disappears. No ask, no buy. Stop and target are recomputed on the average fill price. Each position records entry and exit slippage, the result in R, the MAE and the MFE; Positions (Posições) shows the current R. DCA and grid robots keep buying at market.
- **Crossed stop.** If the last trade reaches the stop (which is what triggers a STOP_LOSS_LIMIT) and the order sits in the book unfilled, with the bid below the stop-limit's limit, the bot cancels the stop and sells with an IOC 0.5% below the bid. If coin is left, it tries another IOC 2% below. If coin is still left, it halts. If the stop does not leave the book or the sell cannot go out (unconfirmed cancellation, a stopped robot buy without a read, daily orders exhausted), the stop stays in the book and the bot retries on the next cycle; on the third cycle in a row it halts. Only a write block (active halt, invalid mandate) waits without counting. There is no market sell as a last resort: it would be a production-only path that the Spot Testnet never exercises.
- Spot never sells short. A sell signal without a position shows as potential and never becomes an order.
- Trailing stop (on by default): the stop on Binance follows the highest price at the same distance as at entry, and never moves down. Each position records the exit rule it opened with and follows that rule until it closes, even if the option changes later. A strategy validated under another rule (or before this version) goes back to signal-only until validated again. The fixed target no longer closes the position; the exit comes from the stop or the strategy signal. The stop only leaves the book when the new one can enter: no halt, valid mandate, no API weight pressure, room below the lower of bid and last price, and one daily order reserved for each open position. The room is half the trailing distance, between 0.5% and 1%: the new stop is not born glued to the price, and a `trailing_tp` with a short distance keeps rising. Every stop replacement measures the time with no stop in the book; Performance (Desempenho) shows mean, p95 and maximum over the last 30 days, by type.
- Entry mode:
  - **Manual** (default): the cycle never buys on its own. You execute a Ready (Pronta) opportunity from the panel and the bot handles the stop and the exits; the cycle starts by itself at that moment. Buys made directly on Binance are not tracked.
  - **Automatic**: the cycle also buys the Ready opportunities whose strategy **estimated hit rate** passes the minimum (60% by default, between 50% and 95%). The rate is `(wins + 1) / (trades + 2)` over the backtest trades with the same exit rule plus the real closed positions, and only exists with at least 20 trades. It is history, not a guaranteed probability. Switching to automatic stops the cycle and requires Start.

The origin of each ported module is in [ORIGIN.md](ORIGIN.md).

## Fixed target and stop

In Controls (Controles), **Fixed target and stop** (Alvo e stop fixos) takes the gain and the loss as a percentage of the entry price (for example +3% and −1.5%). When on, it applies to every strategy, day trading included, and replaces each strategy's stop, target and exits, the trailing stop, and the day trade's TP1, breakeven and 6 h limit.

- **Entry:** as before, a Ready opportunity from a validated strategy. In automatic mode the estimated hit rate must pass the rule's breakeven, `loss / (loss + net gain)`, plus 3 points. With +3% and −1.5% that is close to 38%, not the swing mode's 60%. The reward/risk filter does not apply here, because you chose the gain and the loss; the cost filter still does.
- **Stop:** a STOP_LOSS_LIMIT on Binance at the chosen loss below the **average fill price**, not the analysis price.
- **Target:** the cycle sells with LIMIT IOC when the last 15 min price reaches the target, also computed on the average fill price. There is no exit on strategy signal and no time limit.
- **Score:** the row's expected return never exceeds what the strategy itself estimates, so a wide target does not make a weak signal look strong.
- **Limits:** target from 1% to 100% (below 1% the round-trip cost would eat the gain and no row would be Ready), loss from 0.5% to 50%.
- **Validation:** the values are part of the rule (`fixed:<target in basis points>:<loss in basis points>`). Changing either requires validating again, and the hit rate only mixes trades of the same rule. In the backtest the stop fires on the candle low and wins when the same candle touches both; the target fires on the candle **close**, like the cycle, which sells at the last price and leaves no order in the book. Every rule pays 0.03% slippage per leg. Day strategies still skip entries between 03:00 and 07:00 UTC and are judged by the day gate.
- **Automatic end to end:** when the target and stop change, or a strategy is switched on, the bot validates on history by itself, one strategy at a time, every enabled strategy that has no result yet for the current rule. With the **Automatic** entry mode and Start, the bot picks, buys, protects and sells without a click, and only trades what passed the test.
- Open positions follow the rule they opened with.

## New coins

A recently listed pair lacks the history the long analyses need, so the bot looks at its day:

- **Automatic selection:** a pair with fewer than 29 closed daily candles (listed less than a month ago) is ranked on its last 24 1 h candles, with the same formula: return divided by the standard deviation of returns. It needs at least 12 h of trading. Before, those pairs never got in.
- **Day trading:** with fewer than 60 1 h candles (listed less than 2.5 days ago), the 1 h trend filter reads the day itself on the 15 min: EMA 20 above EMA 50 and close above the day's VWAP. Pairs with history keep using the 1 h; their setups are bit-for-bit identical to before.
- Mean reversion on the 15 min already traded with 10 h of candles. Trend (1 h, 60 candles) and Momentum (4 h, weeks of history) still require the long history, by design.

## Live trade

When you execute an opportunity, the panel shows the **See the live trade on the chart** (Ver o trade ao vivo no gráfico) button; in Positions, each row has the **live** (ao vivo) link. The `/trade/<id>` page shows the candles of the market where the order was filled (on the testnet, the Spot Testnet book), from 30 minutes before the entry, with the entry line, the current stop, the target or the trailing stop's highest price, and entry and exit markers. It refreshes every 5 seconds while the position is alive; the server caches the candles for 5 seconds and backs off when the API weight is high, so it never takes requests away from stops and exits.

## How the bot decides

The rules follow what has published evidence for a spot-only buyer. Nothing here guarantees a profit: the backtest numbers in the papers are optimistic, the momentum effect in crypto weakened after the BTC futures launch, and each strategy still has to pass the historical test before it trades.

- **Follow the trend, long only.** Time-series momentum is the most robust effect in crypto, and it works long-only; shorting added no significant return. The **Momentum** strategy buys when the last 20 days' rise (120 4h candles) is positive, EMA 20 is above EMA 50, the price is above EMA 50 and ADX is at least 20. Confidence is that rise divided by the swing over the same period. The stop sits at 2.5 ATR and the exit is the trailing stop or EMA 20 crossing below EMA 50: let the winner run and cut the loser early.
- **Pick where to look by relative momentum.** The cross-sectional momentum factor exists, but as a strategy of its own it is weak after costs. So it only picks the pairs: automatic selection takes the 40 most liquid USDT pairs (no stablecoins or leveraged tokens) and keeps those with the best risk-adjusted momentum, the 28-day rise divided by the standard deviation of daily returns, positive ones only. A buy still needs a Ready setup from a validated strategy.
- **Size by risk, under separate caps.** The size comes from the stop: the loss at the stop is `risk_per_trade_pct` of capital (1% by default, at most 2%), or less when Kelly limits it. That amount divided by the stop distance gives the size. Then the lowest of these caps applies:
  - **per-order cap:** `max_order_notional_pct` of capital × 90%. A safety ceiling only;
  - **broker cap:** the mandate's and the Spot Testnet's (`binance.json`), × 90%;
  - **volatility:** an order exposed to one daily ATR14 costs at most `vol_cap_pct` of capital (1% by default, 0 disables). A pair with a 10% daily ATR buys at most 10% of capital; BTC and ETH almost never reach it;
  - **total exposure:** what is left of `max_exposure_pct` (60%), or of the volatility target, minus what is already open at the last price. The order shrinks until it fits; with no room, it is refused.

  When the 7-day standard deviation of 4 h returns exceeds `vol_throttle_ratio` (2 by default, 0 disables) times that of all 4 h candles read (about 50 days), the order is halved. Since the stop comes from the ATR, a more volatile asset buys less.
- **Per-order cap of 25%** on a fresh install. An existing `bot.json` keeps its saved value (2% on old installs) until you change it. The cap enters the mandate: until you reconfirm, the old mandate's cap wins, and Controls shows both side by side. With the defaults, a 2% stop gives 22.5% of capital (the per-order cap wins) and an 8% stop gives 12.5% (risk per trade wins). With the 2% cap, every order sits near 1.8% of capital and risk per trade limits nothing. Each opportunity shows the limit that won ("per-order cap", "risk per trade", "volatility (daily ATR)" and others) and the value of each cap.
- **Portfolio volatility target** (`target_vol_annual`, 60% by default, 0 disables). The portfolio's annual volatility, in dollars, is estimated from 1 h returns (exponential average with a 72 h half-life, with covariance) and may not exceed 60% of capital. It only limits new entries: nothing is sold to rebalance. With fewer than 48 aligned returns, it does not limit.
- **Bounded total risk.** The sum of the loss to the stop of every live position, plus the new one, stays below `max_open_risk_pct` of the configured capital (6% by default, at most 20%). A position whose trailing stop already passed the average price does not count; a still-pending entry counts as a whole trade.
- **Correlation and the BTC cluster.** An entry is refused when there are already `max_correlated` (2) positions with correlation above `correlation_threshold` (0.80) with it, counting live ones and rows already accepted in the same cycle. A pair is in the BTC cluster when it is BTC or when the correlation of its 1 h returns with BTC exceeds `cluster_corr` (0.70). The cluster's risk to the stop, live plus accepted in the cycle, stays within half of `max_open_risk_pct` (3% with the defaults). The row shows the cluster.
- **Several positions, within the balance.** Every cycle the bot reads the free USDT. In automatic mode it walks the Ready opportunities from the highest score to the lowest and accepts one per pair (diversification: two strategies never buy the same pair), within `max_positions`, the total risk and 95% of the free USDT net of fees. If cash does not cover the whole size, the order shrinks to what fits, as long as it stays above Binance's minimum; otherwise the row is Blocked (Bloqueada) with the reason. In manual mode each row is checked on its own, because you pick one at a time. Without a balance read, no entry goes out.
- **Minimum expected profit.** `min_profit_usd` (0 by default) refuses an entry whose size times the net return to the target is below that value. With the trailing stop the target is only a reference. A larger profit per trade only comes from a larger order, and the loss at the stop grows in the same proportion.
- **Protections after a loss** (like Freqtrade's `CooldownPeriod` and `StoplossGuard`). A pair that closed at a loss gets no new entry for `pair_cooldown_minutes` (120 by default). With `loss_guard_trades` losses (3 by default) in the last 6 hours, or with those losses adding up to `loss_guard_r` R or more (2 by default, 0 to 20; R is the loss divided by the risk to the stop at entry), no new entry goes out until the oldest losses leave the window. Zero disables each one. This cuts the string of re-entries in the same pair that turns into a loss in a bad market.
- **Same rule live and in history.** The strategies and the signal exits decide only on closed candles, like the backtest; the still-open candle only serves as a price, and the panel marks it as "open candle preview". The signal exit reads the timeframe the position opened with: a 4 h trend exits on 4 h candles. Old positions, with no recorded timeframe, use the candles from before. In the backtest the stop fires on the candle low, at the stop price or at the open when the candle opens below it; the `target` rule's target sells at the close of the candle that closes at the target, like the cycle, never on a wick; the stop wins when the same candle touches both. Each leg pays 0.1% fee and 0.03% slippage.
- **Exit hysteresis** (`exit_hysteresis`, on by default). Trend and momentum only exit on signal when EMA 20 is 0.5% below EMA 50 or below it on 2 closed candles in a row, not on the first cross. It is part of the validation. Each position records the rule it opened with; old positions exit without hysteresis.
- **Simple-average indicators.** ATR, ADX and RSI use a simple moving average, not Wilder's smoothing. This is a decision: live and history compute the same numbers, and the golden digests and recorded validations stay valid. Changing it would change all of that with no evidence that it trades better.
- **Room for the stop.** The entry uses at most 90% of the per-order cap: the gate evaluates the stop on the bid, and a rise of up to ~11% still leaves the new stop within the cap. Above that the trailing stop stops rising and the current stop stays in the book.
- **Historical validation (gate v3).** Each timeframe the strategy trades is validated separately, over the configured set of pairs, and a setup only executes if its timeframe passed. With automatic selection on, the validated set is the liquid universe it picks from (the 40 highest-volume pairs), not the pairs picked by recent momentum: those rose precisely at the end of the period, and validating on them would be selection bias. Picking by volume at the end of the period still carries a remainder of that bias and does not see delisted pairs; the panel says so. Nothing is re-fitted: the parameters are fixed. The replay uses only closed candles and applies the portfolio limits to the trades of all pairs together: `max_positions`, one trade per pair, the pair cooldown after a loss and the loss guard, counting only accepted trades that already exited, without looking ahead. The limits used are recorded and shown in the panel; changing them does not require validating again. The limits the replay does not reproduce (total open risk, BTC cluster, correlation, entries per cycle, drawdown steps, cash) make the result a best case. The gate:
  - the last 20% of the period, by exit date, is a hold-out with at least 5 trades, which may not lose and must earn per day at least half of what the sample earns;
  - the rest is cut into 4 windows: 3 must profit, and the walk-forward efficiency (median of each window's gain per day over that of the previous ones) is at least 0.5;
  - on the trades before the hold-out: Sharpe above 1, drawdown below 20% and at least 30 trades;
  - deflated Sharpe of at least 0.95, with N equal to the number of distinct combinations (timeframe, exit rule, pairs, market filter, hysteresis) tested since the last approval, plus the current one. Every run is recorded as an attempt, so testing until it passes costs.

  Monte Carlo resamples the trades in R 1000 times and records the p95 drawdown. It does not fail the validation: the cycle checks `p95 × risk_per_trade_pct < max_drawdown_pct` every minute, and the rows turn signal-only when it does not pass. Changing the limits does not require validating again. PBO (probability of backtest overfitting) is not computed: the bot does not optimise parameters, so there is no family of configurations to compare; the multiple-testing correction is the deflated Sharpe with N, and the panel says so. A result from the old gate, another strategy version or another rule goes stale: the bot validates again at start-up and in the loop, at once when the gate, version or rule changes, and at most once a day when only the pair set changes. The `classify` thresholds (`score_threshold`, `cost_multiple`, `min_reward_risk`, which is 0 with a fixed exit) are also part of the rule: changing one makes the validation stale, and the strategy's rows stay signal-only, in the cycle and in `/bot/status`, until the new validation passes. The reason for a failure appears in the panel.
- **Drawdown steps.** Drawdown is measured from the peak of cumulative PnL. With the defaults:
  - **5%** (`dd_reduce_pct`, 0 disables): caution. Risk per trade halved and one entry per cycle.
  - **10%** (`max_drawdown_pct`): reduce-only. No buy goes out: strategy, manual, armed or robot. Stops, exits, trailing stop and TP1 continue. It is not the HALT: it comes back by itself when the drawdown falls below the first step (5%), not before, so it does not oscillate on the line.
  - **15%** (`halt_drawdown_pct`): halt, which never disarms itself. After resuming, the bot stays in reduce-only until the drawdown is below 5%.

  A `bot.json` without the steps derives both from `max_drawdown_pct`: half of it, and 1.5 times it between 15% and 50%. With a limit of 50% or more, the halt sits at 1.5 times the limit, at most 100%. Changing only `max_drawdown_pct` through the panel or the API recomputes both; a caution step at 0 stays disabled. An unreadable equity also puts the bot in reduce-only. The panel shows the step in a band at the top.
- **Daily loss on equity.** The day's loss is the equity now, with open positions at the last price, minus that of the first cycle of the UTC day: unrealised loss counts. Past `max_daily_loss_pct` (3%), no buy goes out until 00:00 UTC, not manual, not armed, not robot. The cycle keeps running: stops, crossed stop, trailing stop, TP1, time limit and robot sells keep working, and Start is accepted during the pause. With `flatten_day_on_daily_loss` (off by default), the bot first sells the day-trade positions; swing ones keep their stop. Flattening by default would sell at the bottom.
- **Market filter** (`market_filter`, off by default). With `btc_ema40d`, no entry goes out while BTC's last 4 h close is below the EMA of 240 4 h candles (about 40 days), nor when BTC was not read in the cycle. With fewer than 240 closed candles, the filter lets it through. It is part of the validation: switching it on requires validating again, and the backtest applies it without looking ahead. To compare, validate with and without; both runs count as attempts in the deflated Sharpe. It stays off because no published backtest supports it: it has to be tested, not assumed.
- **Live demotion.** A strategy goes back to signal-only when, over 30 calendar days with at least 10 closed trades, its live daily Sharpe (a day without a trade counts 0) is below half the backtest's daily Sharpe, measured the same way, or when the last 20 trades have a negative mean R. With a negative mean R over the last 10 trades of one pair, within 90 days, the strategy stops on that pair only. A pair whose average round-trip slippage over the last 20 trades (30 days, at least 10 measured) exceeds `slippage_budget_pct` (0.3%) gets no entry. Validating again restarts the strategy's count; the per-pair blocks expire with time. Performance shows slippage per pair.
- **Signal age.** A signal up to `max_signal_age_seconds` (120 s) old since the close of the decision candle enters in automatic mode. An older signal only enters while the price has not left it behind: at most 0.5 R above the close of the signal candle and less than a whole stop below it (R is the stop distance). A row stays Ready for the whole candle (four hours on the 4h), so a clock-only limit let automatic mode enter only in the first minutes after each close, and never after a restart; the risk of entering late is chasing the price, and that is what the rule measures. Outside it, the row states the delay and the displacement and is left for manual. Manual and armed entries show the age and the displacement and pass.
- **Macro calendar** (convention, no paper). `macro_events_utc` (empty by default) takes event times such as CPI and FOMC; day-trade entries pause `macro_pause_minutes` (15) before and after each one. The backtest does not reproduce the pause.
- **Real account fee.** Once a day the bot reads the account fee on Binance (`/api/v3/account/commission`, standard plus tax, assuming no BNB discount). A failure retries in 1 h and keeps the last good read. The executor uses that fee in the sell margin and the accounting, and so do the robots. The score uses the higher of 0.1% and the account's; the backtest stays at 0.1%, so the validation does not depend on the account.
- **Fractional Kelly** (like jevymarket's `KELLY_FRACTION`). When the strategy has an estimated hit rate `p`, the risk at the stop becomes the lower of `risk_per_trade_pct` and `kelly_fraction` (0.25 by default) of `p − (1 − p) / b`, with `b` the gain to the target per unit of risk; Kelly ≤ 0 refuses the entry as "no estimated edge". `b` is the target's gain, and with the trailing stop the target is not an exit, so it is a ceiling. With the automatic mode defaults (minimum hit rate 60% and reward/risk 1.5) Kelly is above 0.33 and the ceiling does not bind; it weighs on manual entries with a low hit rate and when the minimum hit rate is lowered. Test rows do not use Kelly.
- **Entries per cycle limit** (`max_entries_per_cycle`, 2 by default, like jevymarket's `MAX_TRADES_PER_RUN`): automatic mode opens at most that many positions per cycle (1 in caution mode) and leaves the rest for the next one, avoiding buying several pairs at once on the same move.
- **Hit rate of accepted setups.** The replay marks on each trade whether the live filters (score, cost, reward/risk) would accept it. The estimated rate used by Kelly and the hit-rate floor is the lower of the rate over all trades and the rate over accepted ones, but the accepted rate only counts with a minimum sample and with the same thresholds as the current config; below that the overall rate applies. Performance shows both.
- **Calibration** (like jevymarket's `stats`): each position records the estimated hit rate at entry; in Performance, a table compares, per bucket, the estimate with the share that actually profited. Test rows are left out.
- **Real capital.** Sizing uses the lower of the configured capital and the free USDT plus the value of the bot's positions. Equity, daily loss and drawdown are still measured on the configured capital, so a withdrawal does not become a false drawdown. The stored peak is that of cumulative PnL, not of equity, so changing the capital does not become a drawdown either: the peak becomes the new capital plus that PnL.

Left out: chandelier exit, market sell as the crossed stop's last resort (production-only path, untestable on the Spot Testnet) and OCO on the day trade's TP1 (see Day trading). Resting limit buys exist only for the DCA and grid robots, as an option.

Sources: [Freqtrade, protections](https://www.freqtrade.io/en/stable/plugins/#protections); [NautilusTrader, risk engine](https://nautilustrader.io/docs/python-api-latest/risk.html); [Hummingbot, kill switch](https://hummingbot.org/client/global-configs/kill-switch/); [Coin Bureau, risk per trade and portfolio heat](https://coinbureau.com/guides/risk-management-strategies-crypto-trading); [Rozario et al., arXiv 2009.12155](https://arxiv.org/abs/2009.12155v1); [Liu, Tsyvinski and Wu, NBER w25882](https://www.nber.org/papers/w25882.pdf); [Time series and cross sectional momentum in the cryptocurrency market, AUT](https://acfr.aut.ac.nz/__data/assets/pdf_file/0009/918729/Time_Series_and_Cross_Sectional_Momentum_in_the_Cryptocurrency_Market_with_IA.pdf); [Man Group, Crypto: too hot to handle](https://www.man.com/insights/crypto-too-hot-to-handle); [Volatility-managed crypto momentum, Springer 2025](https://link.springer.com/article/10.1007/s11408-025-00474-9).

## Day trading

Two short-window strategies, switched on and off like the others: **Day · Intraday momentum** (`intraday_momentum`) and **Day · VWAP pullback** (`vwap_pullback`). The design, the sources and the architect's review are in [docs/plano-day-trade.md](docs/plano-day-trade.md).

- **Entry:** buy only. Enters on a closed 15 min candle, and only with the closed 1 h candles rising (EMA 20 above EMA 50, close above EMA 50). No entry between 03:00 and 07:00 UTC, the hours with the thinnest book.
- **Stop:** 1.5 ATR of the 15 min, with a 1% floor. Below that, the round-trip cost (about 0.25%) would eat more than a quarter of the risk.
- **Exit:**
  - At TP1 (1.5 times the risk), half is sold and the stop of the rest rises to the entry price plus 0.2%.
  - What is left exits within 6 hours.
  - "Breakeven" is a stop-limit with 0.5% room: in a fast move it may fill about 0.3% below the entry, or not fill on a gap.
- **Filters:** the score and the cost and reward/risk filters measure TP1, the trade's target in R. Only the backtest gate measures the real result of the two halves.
- **Orders:** a day trade spends up to 5 orders (entry, stop, TP1, new stop, exit). With a day strategy on, the mandate counts 6 per entry.
- **TP1 without OCO:** between cancelling the stop and replacing it at TP1, half of the position spends a few seconds with no stop. The bot measures that window (Performance). OCO is reconsidered if the p95 of the TP1 window exceeds 2 s over 30 days, or if a crossed stop happens during a TP1.
- **Validation:** 120 days of 15 min and 1 h candles, with the same exit rules. The gate is the swing's v3 (4 windows, hold-out and deflated Sharpe), with the Sharpe measured on daily returns, because with many trades the per-trade Sharpe is easy:
  - Sharpe of daily returns above 1;
  - at least 30 trades before the hold-out;
  - average net return of at least 0.10% per trade;
  - drawdown below 20%;
  - 3 of 4 windows profitable, hold-out with at least 5 trades and no loss, and deflated Sharpe of at least 0.95.

  The validation only covers the rules it replicated.
- **Preset:** "Apply day-trade preset" (Aplicar preset de day trade, in Controls) shows what changes and only then saves:
  - switches the day strategies on and the swing ones off;
  - 0.5% risk per trade;
  - 2% daily loss;
  - up to 8 trades a day;
  - 60 min pause on the pair after a loss;
  - target of at least 3x the cost;
  - reconfirms the mandate.

**What the evidence says.** Most day traders lose after costs: fewer than 1% profit predictably in Taiwan, and 97% of those who persisted lost in Brazil. No peer-reviewed study shows long-only day trading in spot crypto to be profitable after 0.2% per round trip.

The 90-day backtest to 2026-10-16, still with the old gate, confirmed this on both strategies:
- **Testnet pairs (BTC, ETH, SOL):** all lost.
- **Volatile pairs (AVAX, ZEC, INJ, NEAR, STRK, TIA):** only intraday momentum on INJ passed, 1 of 18 tests, which is what chance would give.

The bot does not really trade what fails the gate; on the testnet, test mode lets you exercise the flow.

## DCA and grid robots

Robots the operator configures on the Robots (Robôs) page, like Binance's Spot DCA and Spot Grid. They do not go through the validation gate: creation checks the whole plan before the first buy (worst case against risk per trade, total exposure, per-order cap and order budget). Their orders come out of `instance_orders_per_day` (0 by default: no robots), reserved in the mandate.

- **Maximum robots:** `max_instances` (3 by default) alive at the same time. One per pair, and never on a pair with a strategy position.
- **Pause between rounds:** `cooldown_minutes` (1 min by default, like Binance's 60 s). The DCA's re-buy after the target and the grid's restart after selling everything wait that long.
- **Preview:** a replay of the last 30, 90 and 180 days of 1 h candles. Marks in red a grid whose close stayed outside the range more than 30% of the time: a grid only earns in a sideways market. It is history, not a promise.
- **Orders:** market buys and LIMIT IOC sells, every minute, with the stop on Binance. Only reduce-only, halt and the daily pause block robot buys; stops and sells continue. Stopping a robot sells what it holds, with no global halt.
- **Resting buys in the book** (off by default; "resting limit buy" option (compra limitada no livro) on the robots page): with `resting_buys`, the robot leaves at most one LIMIT GTC buy order, at the next grid level or the DCA's next safety order. It locks USDT, not the coin, so it coexists with the stop. It costs 3 orders of the budget (place, cancel, replace the stop). At VIP0 maker and taker both cost 0.1%: the gain is the price (fills at the level, no spread, and catches touches within the minute), not the fee. On fill, the buy joins the position and the stop is replaced; the halt cancels resting buys before writing the flag. They also leave the book before any sell, on Stop, and on any buy block (clock, daily pause, reduce-only); a fill that arrives together with the cancellation joins the position. Pausing the cycle is refused (409, "compra limitada ainda no livro") while the cancellation is unconfirmed or the bought part has no stop yet: the cycle keeps running until it resolves. A buy Binance already confirmed is only forgotten when a cancellation confirms the order does not exist; until then it blocks sells and new resting buys. If the old stop does not leave the exchange within 3 cycles in a row after a buy, the bot halts. The 30/90/180-day preview fills the buy at the level when the candle low touches it.
- **Account fee and the gate.** With an account fee lower than the market default (VIP tier), the stop and sell quantities keep the default fee's margin, which is what the order gate checks: a little more dust is left, and the stop is never refused for balance.

## DeepSeek advisor

An optional advisor asks [DeepSeek](https://api-docs.deepseek.com) for a second opinion on each Ready strategy row. It **advises**; the deterministic engine still decides. The design, the decisions and the architect's review are in [docs/plano-refatoracao-deepseek.md](docs/plano-refatoracao-deepseek.md).

- **What it can do.** Answer `confirm`, `reduce` or `veto`, with confidence, regime and a short rationale. In `filter` mode, a veto with confidence of at least `min_veto_confidence` (60%) takes the row out of automatic mode; it stays Ready for manual, with the reason "IA desaconselha: ...". A `reduce` multiplies the size by a factor between 0.25 and 1, after every cap and the throttle; the cap shows as "análise IA".
- **What it never does.** Create a row, raise a size, turn a refusal into an entry, touch the order gate, the mandate, the halt, the stops or the exits. DCA and grid robots, armed entries and test rows are not consulted. Manual entries show the verdict and are not blocked.
- **Why it only reduces.** An LLM answer cannot be reproduced in the backtest. Removing or shrinking entries is the same class as the filters that already exist only live, so the validation stays a ceiling on what the bot does.
- **Modes** (`advisor.mode` in Controls): `off` (default), `shadow` (asks and records, no order changes) and `filter`. Start in `shadow` and only switch to `filter` when the calibration shows the vetoes avoid losses.
- **Neutral failure.** With no key, no network, an exhausted budget, an invalid answer or a timeout, the bot trades as if there were no advisor. With `require_verdict`, a row without a verdict does not enter in automatic mode; the panel asks for confirmation before switching it on.
- **Outside the cycle lock.** The calls run on their own thread with a bounded queue; the cycle only reads the cache and enqueues without waiting. A row waits for the verdict for up to `wait_seconds` (45 s), counted across cycles, and then goes on. Each signal (pair, strategy, timeframe and candle) is asked once per environment: a testnet verdict never applies to a production row. A restart does not ask again: the verdicts already recorded in `advice_log` return to the cache before the first cycle's score, with no call and no cost, so a `reduce` recorded before the restart also reduces the first entry after it. Each call attempt has a total deadline of 45 s, besides the connect and read deadlines: blank rows from a long queue do not hold the advisor.
- **Model.** `deepseek-flash` (default) or `deepseek-v4-pro`, in JSON mode and without reasoning, for predictable latency and cost. The prompt carries only numbers and categories built by the code (setup, timeframe indicators, BTC regime, account risk level), never text from an external source. The indicators use only closed candles up to the close of the signal candle, on every timeframe, even when the query happens hours later: the model does not see the path the calibration measures it on.
- **Cost.** Daily budget in `daily_budget_usd` (US$ 1 by default), per DeepSeek key: checked before every call against the sum of the UTC day's spend on the testnet and in production, so it survives restarts and mode switches. The header and Controls show that sum. A billed attempt that ends in an error (such as an empty answer, retried once) also counts. The cost is an estimate from the price table in the code (dated); the billed amount is the one in the DeepSeek console. A call costs about US$ 0.001.
- **Calibration.** Each verdict gets a counterfactual R under the fixed-exit replay rules: entry at the close of the signal candle, stop on the low (with gap), target only on the close, at most 48 candles, net of fee and slippage. Performance groups by model and prompt version and shows, per verdict, n, mean R and standard error; high-confidence vetoes get their own row and the realised R of the rows that entered sits in a separate column. A useful veto has a negative counterfactual R. A row only counts once the 48 candles have passed, and each signal counts once. When the pair leaves the analysed list, the window's candles are read from Binance through the same path as the historical test (a few per hour, never under weight pressure); a row whose candles were read and that is still unlabelled a day after the window ends expires: it stays out of the averages, is not retried and appears in Waiting (Aguardando) as expired. A read that fails (network, Binance error) does not prove missing data: the row is retried, and only three failures after the grace day expire it.

**Key.** Lives in `deepseek.json`, in the same folder as `binance.json` (`BOT_RUNTIME_ROOT`), with permission 0600. To save it, run on the bot's machine and paste the key when prompted (it is read from standard input, never from an argument):

```powershell
python -m bot.ops advisor-key
```

`DEEPSEEK_API_KEY` only applies when the file has no key. The key never goes to `bot.json`, the panel, the API, the logs, the ledger or the `advice_log`; a field with `key`, `secret` or `token` in its name is refused in `PUT /bot/config`.

## Repository layout

```
bot/                 Python package: API, trading loop and everything they share
  __main__.py        python -m bot: uvicorn API on 8000 plus the tick loop thread
  runtime.py         Runtime: cycle lock, halt, ledger, start/pause/resume
  cycle/             one cycle's parts: market reads, exits, risk level, scoring, robots
  strategies/        trend, momentum, mean reversion, day (intraday, VWAP), fixed, trailing, funding
  backtest/          walk-forward backtest shared with the live strategies; validation gate
  risk.py            sizing by risk, caps, drawdown steps, correlation and cluster limits
  executor.py        entries, stops, exits, crossed stop, reconciliation
  instances.py       DCA and grid robots
  exchange/          Binance connector: ccxt reads, SDK writes, weight, profiles
  live/              mandate, halt and order gate ported from Vibe-Trading
  governance/        tamper-evident ledger ported from Vibe-Trading
  advisor/           DeepSeek advisor: client, prompt, budget, calibration
  api/               FastAPI app, token/Host/Origin auth, presets, validation jobs
  store.py           SQLite store (positions, decisions, validations, advice log)
  collector.py, markets.py, features.py, indicators.py   market data and indicators
  promotion.py, decay.py, mandate_io.py, config.py, paths.py, ops.py   gates, demotion, config, CLI
ui/                  operator panel: React 19, Vite, Tailwind, ECharts
  src/pages/         Controls, Opportunities, Positions, Bots, Performance, Balance, History, TradeLive
  vite.config.ts     proxy that adds the API token and the Host header
tests/               pytest suite, with a fake venue for the exchange
docs/                design documents (day trading, strategy review, refactor and DeepSeek)
ORIGIN.md            origin of every ported module and how to diff it against Vibe-Trading
Dockerfile, ui/Dockerfile, docker-compose.yml   containers for API and panel
```

## Operation

1. Python 3.12 and Node 22. From the root:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest
cd ui
npm install
npm run dev
```

2. In another terminal, with an explicit runtime root (do not use the source folder for keys):

```powershell
$env:BOT_RUNTIME_ROOT = "$env:USERPROFILE\.the-trader-bot"
python -m bot
```

3. The panel is at `http://127.0.0.1:5173` and the API at `http://127.0.0.1:8000`. On first start the API writes a token to `%USERPROFILE%\.the-trader-bot\api-token` (the `BOT_RUNTIME_ROOT`). The Vite proxy reads that file and sends the token with every call; the browser never sees it. The API refuses requests without the token, with another `Host`, with another `Origin` or, on writes, without the `X-Bot-Client: panel` header. Run Vite with the same `BOT_RUNTIME_ROOT` as the API.

4. First configuration, on the Controls page:

- Save the **Spot Testnet** keys in `%USERPROFILE%\.the-trader-bot\binance.json` or in the `BINANCE_TESTNET_API_KEY` and `BINANCE_TESTNET_API_SECRET` variables. The panel never receives a key. The production key, when there is one, needs an IP restriction and no withdrawal permission.
- Set the capital and confirm the mandate. The bot writes `mandate.json` with `crypto` instruments only, leverage 1 and four orders per entry allowed per day (entry, stop, exit and one replaced stop).
- On the Spot Testnet, set in `binance.json`: `testnet_audit_only: false`, `testnet_max_order_notional` (for example `{"USDT": "100"}`) and `testnet_max_daily_operations` with room for three orders per entry. The connector's default is 2 operations a day, which does not cover even one entry.
- Switch a strategy on and click "validate on history" (validar no histórico, `POST /bot/strategies/{id}/validate`). The test runs in the background over the configured pairs, on each timeframe the strategy trades: 240 days of 1h and 480 of 4h for Trend, 480 days of 4h for Momentum, 120 days of 15m and 240 of 1h for Mean Reversion, and 120 days of 15m (plus the 1h) for the day strategies, with the same 300-candle window as the live bot. Every strategy on every timeframe, with 15 pairs, took about 3 minutes on synthetic data; each run records the time spent. The panel shows the progress and, in Performance, the detail per timeframe: raw and deflated Sharpe, attempts, the 4 windows, the hold-out, the Monte Carlo against the limit, profit factor and expectancy in R. It only trades if it passes gate v3: Sharpe above 1, drawdown below 20% and at least 30 trades before the hold-out, 3 of 4 windows profitable, hold-out with at least 5 trades and no loss, and deflated Sharpe of at least 0.95 (see "How the bot decides").
- Pairs: "use the 20 most liquid" (usar os 20 mais líquidos) swaps the 5 default pairs for the 20 USDT pairs with the highest 24h volume (no stablecoins); pairs with an open position never leave. The collection runs in parallel to fit the 60 s cycle.
- Automatic selection (`auto_select`, off by default): once an hour the bot replaces the list with the most promising pairs (15 by default, 5 to 20). While it is on, the list cannot be changed from the panel. On the testnet, the selection, the most-liquid button and the edited list only accept pairs the Spot Testnet lists (`testnet.binance.vision`), because the analysis uses the real market and the orders go to the testnet. Any listed pair qualifies, even with little trading there. The testnet's 24 h ticker lags 30 s to minutes on thin pairs, and the gate refuses a quote older than 15 s. So, on the testnet only, bid and ask come from the live book and the quote age counts from the read time; production keeps using Binance's time. A pair with no sellers in the testnet book (like PEPE on 2026-10-19) does not fill the buy.
- Spot Testnet test mode (testnet only): buy setups that failed a quality filter or whose strategy is not validated appear as Ready with the **test** (teste) mark, so you can test buy, stop and exit. They go out through the execute button and, with automatic trading on, also through the cycle (highest score first, within the balance and the limits). They do not enter the hit rate or the demotion. Production ignores the option.
- Start. The cycle is 60 seconds. The grey badge means Spot Testnet.

5. Stop everything (`POST /bot/halt` or the button) stops the cycle and writes the HALT, which blocks every new order. With "flatten on halt" (zerar no halt) checked, the bot sells the positions **before** writing the HALT; what it cannot sell keeps a replaced stop. Without the option, the positions stay open with the stop on Binance. The choice lives in the mandate: changing it requires reconfirming the mandate, and with a saved mandate the mandate decides. Resuming is `POST /bot/resume`, manual. Before releasing, the bot resolves the limit buys the halt left in the book; if any is still unconfirmed, the resume is refused (409) and the halt continues. Start runs the same check when the cycle is stopped. The cycle also halts by itself after three failures in a row, on a drawdown of `halt_drawdown_pct` (15%) and when a position is left without a stop and without a way out, including a crossed stop the two IOCs did not sell. After a drawdown halt, resuming returns to reduce-only until the drawdown falls below `dd_reduce_pct`.

6. Production is another directory: `testnet/` and `production/` have their own store, journal, mandate, HALT and ledger. `binance.json`, `bot.json` and `api-token` stay at the root. The switch falls back to testnet if any precondition is missing, and the reason shows on screen. The G1 to G4 releases only change by editing `bot.json`; the API never writes them. **Promotion:** the switch requires, over the last 30 days of testnet, positive net PnL, at least 30 closed strategy trades with a recorded R, positive expectancy in R and the Monte Carlo p95 × risk per trade below `max_drawdown_pct`. It also requires no quantitative mandate violation in the testnet audit ledger over those 30 days. **Pilot:** the exception is a pilot: with a capital limit of up to 50 (`pilot_max_production_capital_usd` in `bot.json`, or `pilot_cap_usd` sent by the panel on the switch, in the "Small-money pilot" (Piloto com pouco dinheiro) card) above zero and the production capital (allocated × `production_capital_fraction`, 10% by default) within that value, the switch waives only the history; every other condition still applies. The panel can also request `pilot_skip_validation`: the pilot writes that flag in the production store and the enabled strategies trade without the historical validation (and, until there is a sample, without a hit-rate estimate), keeping only the per-pair demotion; the flag is cleared on every switch that does not request it and never applies on the testnet. The panel can also request `pilot_skip_gates`, only with `pilot_gates_waivable: true` in `bot.json` (without it the switch is refused): the pilot waives the G1 to G4 releases on that switch only (bot stopped, no open position, keys, mandate, PRODUCAO and the testnet audit are still required), and the flag stays in the production store; a full promotion never waives them. The limit is written to the production store at the switch and checked wherever the mandate is written, so editing `bot.json` later does not loosen it: raising the capital, reconfirming the mandate or applying the preset above the limit is refused (409). The panel shows "Pilot production" (Produção piloto). Even so, the real limit is the account balance: keep only the test amount in it. On approval, the bot writes the production mandate with the production capital (10% of the allocated). Leaving production requires no open production position: with "flatten on halt" checked the bot sells first; if anything stays open, the switch is refused, because the testnet process would not track those positions.

The mandate limits the exposure of the positions the bot opened, valued at the current quote. The rest of the account (USDT cash, coins that were already there) does not count as the bot's exposure.

`binance.json` is still the ported connector's credential file. `bot.json` holds mode, capital, strategies and limits, and those limits can never loosen the mandate.

## Environment variables and runtime files

| Variable | Used by | Meaning |
| --- | --- | --- |
| `BOT_RUNTIME_ROOT` | API, panel | Runtime directory for keys, `bot.json`, `api-token` and the stores. Default `~/.the-trader-bot`. The panel reads the token there. |
| `BOT_API_HOST` | API | Bind address of the API. Default `127.0.0.1`; Docker sets `0.0.0.0`. The Host-header guard still applies. |
| `BOT_API_URL` | panel | Where the Vite proxy sends `/bot` calls. Default `http://127.0.0.1:8000`; Docker sets `http://api:8000`. |
| `BOT_PANEL_HOST` | panel | Bind address of Vite. Default `127.0.0.1`; Docker sets `0.0.0.0`. |
| `BOT_HOME`, `BOT_UID`, `BOT_GID` | docker compose | Host directory mounted at `/data` (default `~/.the-trader-bot`) and the uid/gid the containers run as. |
| `BINANCE_TESTNET_API_KEY`, `BINANCE_TESTNET_API_SECRET` | API | Spot Testnet keys, as an alternative to `binance.json`. |
| `DEEPSEEK_API_KEY` | API | Advisor key, only when `deepseek.json` has none. |

Files under `BOT_RUNTIME_ROOT`:

| Path | Content |
| --- | --- |
| `binance.json` | Connector credentials and testnet limits (`testnet_audit_only`, `testnet_max_order_notional`, `testnet_max_daily_operations`). |
| `bot.json` | Mode, capital, strategies, limits, G1 to G4 releases, pilot settings. Written by the API, except the releases. |
| `api-token` | Token the API writes on first start and the panel's proxy sends on every call. |
| `deepseek.json` | Advisor key, written by `python -m bot.ops advisor-key`. |
| `testnet/`, `production/` | One per environment: SQLite store, `mandate.json`, `HALT`, `bot-ledger.jsonl` and `binance-execution-journal.jsonl`. |

## Performance and concurrency

Measured on the testnet, from Brazil (about 270 ms per request to Binance):

- **Collection outside the cycle lock.** The market read runs without holding the lock; only the computation and the orders hold it. A `PUT /bot/config` during a cycle dropped from up to 12.3 s to 77 ms.
- **Parallel collection per request.** Up to 16 requests at once, with a reused HTTP client (keep-alive) and the book of every pair in a single call. Collecting 15 pairs dropped from 5.7 s to about 2.2 s. A pair that fails is left out of the cycle and recorded in the ledger (`collect_partial`); if it has an open position, the whole cycle is refused, so the stop and the exit never use a stale price.
- **Binance market table cached** for an hour, per host and client type, with the clock adjustment alongside. Before, every quote and order downloaded `exchangeInfo` again.
- **Journal verified incrementally.** The read checks only the new lines against the last known hash. The whole chain is rebuilt every 5 minutes and always before recording a new order.
- **Ledger with rotation** at 1 MiB: appending does not grow with the history. The daily report and the start-up check (in the background) verify the chain across the rotated files.
- **SQLite** with a 10 s `busy_timeout` and a rollback journal (WAL needs shared memory that Docker Desktop's bind mount does not guarantee). Decisions are only written when they change: the same opportunity read every minute of the same candle becomes one row, not 240. Since the mean reversion reason carries the RSI, it writes a new row when the RSI changes. Decisions, signals and equity points older than 30 days are deleted once a day; the cumulative PnL peak is kept separately, so the drawdown does not change.
- **Backtest.** The strategies refuse first on the cheap tests (breakout and volume, RSI, moving-average trend) and only then compute ADX and ATR; the result is bit-for-bit identical (`tests/test_bot_strategy_golden.py`), and the evaluation became 3 to 14 times faster. Each pair runs in a separate process (up to 4, `spawn`), because the computation is Python and pandas and threads would share the interpreter lock. The window is 299 closed candles, the same the cycle sees, and is recorded in the validation parameters.

## Docker

```bash
docker compose up -d --build
```

Starts the API (`python -m bot`, port 8000) and the panel (the Vite build served by `vite preview`, port 5173), both on 127.0.0.1 only. Both containers mount the operator's folder (`~/.the-trader-bot`, or the one in `BOT_HOME`) at `/data`: keys, `bot.json`, `api-token` and the testnet and production stores stay where they are outside Docker. The panel mounts it read-only, to read the token. The panel's proxy talks to `http://api:8000` and sends the Host `127.0.0.1:8000`, the only one the API accepts. Stop any API running outside Docker first: two processes on the same folder would trade twice. On Linux, create the folder before the first `up` and run as its owner: `mkdir -p ~/.the-trader-bot && BOT_UID=$(id -u) BOT_GID=$(id -g) docker compose up -d --build`; Docker Desktop (Windows, macOS) needs neither. `docker compose logs -f api` shows the cycle; `docker compose down` stops everything, and the positions stay protected by the stop on Binance.

## Tests

```powershell
pytest
ruff check .
mypy
```

The suite covers store and migration, collector (stale data, clock, pair without a perpetual), score, panel and API authentication, every risk limit with real PnL, the mandate gate with priced positions, entry, stop, partial exit, refused stop, order budget, reconciliation, halt with flatten before the flag, idempotency, backtest, report against the ledger, Sharpe decay and the production profile.

Since the strategy review, it also covers: sizing by risk with every cap and the limit that won, the mandate cap until reconfirmation, the LIMIT IOC entry (partial, no fill, no ask), the crossed stop (two IOCs and halt), the trailing stop's room and the no-stop window, the drawdown steps and reduce-only on every buy path, the daily loss on equity, the market filter without look-ahead, same-cycle correlation and the BTC cluster, gate v3 (walk-forward, hold-out, deflated Sharpe with N, Monte Carlo in R, per timeframe and per pairs), re-validation of stale results, hysteresis, demotion by trades and by pair, the slippage block, promotion with R and audit, signal age, the macro calendar, the account fee and the robots (pause between rounds, maximum robots and the 30/90/180-day preview).

`ruff` lints `bot` and `tests`; `mypy` type-checks the packages listed in `pyproject.toml` in strict mode, with the vendored modules reviewed as a diff against their origin instead (see [ORIGIN.md](ORIGIN.md)). The UI type-checks with `npm run build` in `ui/`.

## Execution journal recovery

A crash in the middle of a send, a cancellation with an unknown result or a contradiction between the journal and Binance block **every** new order, stops and exits included, until resolved. The stops already on Binance keep working. The panel shows the warning and `GET /bot/status` carries the `journal` field. The API never resolves these states; only the operator, in a terminal, with the bot stopped:

```powershell
python -m bot.ops status
python -m bot.ops reconcile
python -m bot.ops confirm-absent order:<id> --reason "the sending process crashed"
python -m bot.ops ack-conflict conflict:<id> --reason "investigated"
python -m bot.ops adopt-checkpoint --reason "journal restored from backup"
```

`--mode testnet|production` picks the environment; the default is the one in `bot.json`. `confirm-absent` only marks the order as absent if Binance confirms it does not exist. `adopt-checkpoint` refuses when the checkpoint points to records that vanished from the journal: restore the backup first. The bot reconciles by itself every cycle while something is pending, but a `SUBMISSION_PENDING` (a send that never returned) always requires `confirm-absent`.

If a ledger (`bot-ledger.jsonl`, `binance-execution-journal.jsonl`) ends without a line break after a crash, it refuses new records. Back it up, delete the last incomplete line and run `adopt-checkpoint`.
