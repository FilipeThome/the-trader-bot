export type Opportunity = {
  symbol: string;
  strategy: string;
  action: string;
  score: number;
  net_return: number;
  risk_pct: number;
  status: string;
  reason: string;
  data_age_seconds?: number | null;
  gross_return?: number;
  cost?: number;
  size_usd?: number;
  win_rate?: number | null;
  win_rate_n?: number;
  blocker?: string | null;
  test_only?: boolean;
  timeframe?: string | null;
  /** The limit that set ``size_usd`` (a key of SIZE_LIMITS), and every cap in USD. */
  size_limit?: string | null;
  size_caps?: Record<string, number> | null;
  /** Seconds since the candle that produced the signal closed. */
  signal_age_seconds?: number | null;
  /** Live price minus the signal bar's close, in stop distances (R). */
  entry_drift_r?: number | null;
  /** Correlation cluster of the pair ("BTC"), or null when it trades on its own. */
  cluster?: string | null;
  /** Estimated slippage, a share of the price, inside the round-trip cost. */
  slippage_est?: number | null;
  plan_target_pct?: number | null;
  /** The setup's market regime (trend_up, trend_down, range...). */
  regime?: string | null;
  /** The DeepSeek advisor's verdict on the row, when it was consulted. */
  advice?: Advice | null;
};

/** Opportunity.advice: pending while the call runs, done with the verdict, failed with a redacted error. */
export type Advice = {
  status: "pending" | "done" | "failed";
  requested_at?: string;
  verdict?: "confirm" | "reduce" | "veto" | "abstain";
  confidence?: number;
  size_factor?: number;
  regime?: string | null;
  risk_flags?: string[];
  rationale?: string;
  id?: number | null;
  error?: string;
};

export type ValidationJob = {
  status: "running" | "done" | "error";
  stage?: string;
  done: number;
  total: number;
  error?: string;
  result?: {
    passed: boolean;
    sharpe: number;
    max_drawdown: number;
    trades: number;
    wins: number;
    reason?: string;
    timeframes?: Record<string, TimeframeResult>;
  };
};

/** One walk-forward window of a validation, by exit time. */
export type WalkForwardWindow = { start: string; end: string; trades: number; net: number; profitable: boolean };

/** One timeframe's validation result (strategy_stats.params.timeframes[tf]); ``mc_ok`` comes from /bot/status. */
export type TimeframeResult = {
  passed: boolean;
  reason?: string;
  wins?: number;
  trades?: number;
  gate_trades?: number;
  days?: number;
  sharpe?: number;
  sharpe_trade?: number;
  daily_sharpe?: number;
  dsr?: number;
  trials?: number;
  max_drawdown?: number;
  net_return?: number;
  mc_p95_dd_r?: number;
  wfe?: number;
  windows?: WalkForwardWindow[];
  holdout?: { start: string; trades: number; net: number; passed: boolean } | null;
  profit_factor?: number;
  expectancy_r?: number;
  sortino?: number;
  calmar?: number;
  avg_win?: number;
  avg_loss?: number;
  exposure?: number;
  mae_r?: number;
  mfe_r?: number;
  turnover?: number;
  avg_hold_hours?: number;
  elapsed_s?: number;
  /** Trades ``classify`` would have made ready, with the thresholds the replay used. */
  filtered_trades?: number;
  filtered_wins?: number;
  filtered_sharpe?: number;
  score_threshold?: number;
  cost_multiple?: number;
  min_reward_risk?: number;
  /** Trades the replayed portfolio limits refused, and those limits. */
  skipped_by_limits?: number;
  limits?: { max_positions?: number; pair_cooldown_minutes?: number; loss_guard_trades?: number; loss_guard_r?: number };
  /** From /bot/status: "liquid" (automatic selection's 40 most liquid pairs) or "pairs" (the configured ones). */
  universe?: string;
  universe_pairs?: number;
  mc_ok?: boolean;
};

/** bot_state.risk_state, written by the runtime each cycle; null before the first cycle. */
export type RiskState = {
  level: "normal" | "cautious" | "reducing" | "halt" | string;
  since?: string | null;
  drawdown?: number | null;
  reason?: string | null;
};

export type StopWindowStats = {
  count: number;
  mean_ms: number | null;
  p95_ms: number | null;
  max_ms: number | null;
  by_kind: Record<string, { count: number; mean_ms: number; max_ms: number }>;
};

/** A mandate's caps: the committed one (null without a mandate) or the one bot.json would write now. */
export type MandateSummary = {
  max_order_notional_usd: number;
  max_total_exposure_usd: number;
  max_trades_per_day: number;
  flatten_on_halt: boolean;
};

export type PairSlippage = {
  symbol: string;
  trades: number;
  /** Mean round-trip slippage of the last trades that recorded it; null without any. */
  slippage_rt: number | null;
  /** null when the slippage check itself failed: the state is unknown, with the cause in ``blocked_reason``. */
  blocked: boolean | null;
  blocked_reason?: string | null;
};

export type Position = {
  id: number;
  symbol: string;
  strategy: string;
  side: string;
  qty: number;
  avg_price: number;
  stop_price: number | null;
  target_price: number | null;
  opened_at: string;
  exit_policy?: string | null;
  /** Day trades: when the rest leaves, and whether the first target was sold. */
  exit_deadline?: string | null;
  tp1_done?: number;
  timeframe?: string | null;
  initial_stop?: number | null;
  /** Price moves in units of the entry risk; null without an initial stop (legacy, DCA, grid). */
  r_now?: number | null;
  mae_r?: number | null;
  mfe_r?: number | null;
};

export type DayPreset = {
  changes: { field: string; from: unknown; to: unknown }[];
  order_cap_usd: number;
  risk_notional_usd: number;
  capped_by_order_cap: boolean;
  mandate_orders_per_day: number;
  /** The order one entry at ``stop_pct`` would place under the preset, and the limit that set it. */
  sizing?: { stop_pct: number; size_usd: number; binding: string; label: string; caps: Record<string, number> };
};

export type Market = { symbol: string; base: string; quote: string };

export type BalanceRow = {
  asset: string;
  free: number;
  used: number;
  total: number;
  price_usdt: number | null;
  value_usdt: number | null;
};

export type Balance = {
  mode: "testnet" | "production";
  is_testnet: boolean;
  balances: BalanceRow[];
  total_usdt: number;
  unpriced: number;
  updated_at: string;
};

export type Recommendation =
  | {
      kind: "setup";
      symbol: string;
      strategy: string;
      action: string;
      score: number;
      net_return: number;
      status: string;
      reason: string;
      /** Read on the candle that is still open: a preview, not a closed-candle signal. */
      forming_bar?: boolean;
    }
  | {
      kind: "movimento";
      symbol: string;
      change: number;
      quote_volume: number;
      status: string;
      reason: string;
    };

export type CalibrationRow = { bucket: string; trades: number; predicted: number; realized: number };

export type Decision = {
  symbol?: string;
  strategy?: string;
  decision: string;
  reason: string;
  position_id?: number;
};

export type Trade = {
  position: {
    id: number;
    symbol: string;
    strategy: string;
    side: string;
    status: "pending" | "open" | "closed" | "void";
    qty: number;
    avg_price: number;
    stop_price: number | null;
    target_price: number | null;
    peak_price: number | null;
    exit_policy: string | null;
    opened_at: string;
    closed_at: string | null;
    exit_price: number | null;
    realized_pnl: number | null;
    reason: string | null;
    test_only: number;
    exit_deadline?: string | null;
    tp1_done?: number;
    timeframe?: string | null;
    initial_stop?: number | null;
    entry_slippage?: number | null;
    exit_slippage?: number | null;
    r_multiple?: number | null;
  };
  interval: string;
  candles: { t: string; o: number; h: number; l: number; c: number }[];
  last_price: number | null;
  pnl_usd: number | null;
  pnl_pct: number | null;
  updated_at: string;
};

export type Point = { t: string; v: number };

export type PriceBlock = {
  interval: string;
  t: string[];
  candles: number[][];
};

export type Features = {
  symbol: string;
  updated_at: string;
  trend: PriceBlock & {
    ema50: Array<number | null>;
    breakout_high: Array<number | null>;
    breakout_low: Array<number | null>;
    volume: Array<number | null>;
    volume_avg: Array<number | null>;
    adx: Array<number | null>;
  };
  mean_reversion: PriceBlock & {
    mid: Array<number | null>;
    upper: Array<number | null>;
    lower: Array<number | null>;
    rsi: Array<number | null>;
  };
  funding_carry: {
    threshold: number;
    points: Point[];
    latest: number | null;
  };
};

export type Status = {
  running: boolean;
  /** Day rows enter automatically above the break-even of their first target plus this. */
  day_win_rate_margin?: number;
  /** Production pilot: the capital cap recorded at the switch; null outside a pilot. */
  pilot_cap_usd?: number | null;
  halted: boolean;
  halt_reason?: string | null;
  journal?: { blocked: boolean; error: string | null; blocking: { state_key: string; event: string }[] };
  validated: Record<string, boolean>;
  /** Per strategy and timeframe: the last validation's result, with ``mc_ok`` at today's limits. */
  validation_detail?: Record<string, Record<string, TimeframeResult>>;
  /** Recorded validation runs since the strategy's last pass; a rerun of the same combination counts again. */
  trials?: Record<string, number>;
  /**
   * The N of the deflated Sharpe: distinct (timeframe, exit rule, pair set, market filter, hysteresis)
   * combinations since the last pass, plus the current one. Absent on an API without it.
   */
  trials_n?: Record<string, number>;
  risk_state?: RiskState | null;
  /** Unprotected windows of the last 30 days, when a stop is swapped. */
  stop_windows?: StopWindowStats | null;
  mandate?: MandateSummary | null;
  /** What a reconfirmation would write now from bot.json; null when it cannot be computed, absent on an older API. */
  mandate_target?: MandateSummary | null;
  market_filter?: "off" | "btc_ema40d";
  exit_hysteresis?: boolean;
  flatten_day_on_daily_loss?: boolean;
  macro_events_utc?: string[];
  macro_pause_minutes?: number;
  mode: "testnet" | "production";
  symbols: string[];
  capital_usd: number;
  allocated_capital_usd: number;
  strategies: Record<string, boolean>;
  limits: Record<string, number>;
  flatten_on_halt: boolean;
  entry_mode: "manual" | "auto";
  min_win_rate: number;
  min_win_rate_sample: number;
  trailing_stop: boolean;
  trailing_activation: boolean;
  trailing_rules?: { activation_r: number; callback_share: number };
  take_profit_pct?: number | null;
  stop_loss_pct?: number | null;
  exit_policy?: string;
  testnet_test_mode: boolean;
  auto_select: boolean;
  auto_select_count: number;
  auto_select_at?: string | null;
  min_profit_usd: number;
  validations: Record<string, ValidationJob>;
  gates: Record<string, boolean>;
  net_pnl: number;
  trades: number;
  violations_avoided: number;
};

export type PnlDay = {
  day: string;
  net_pnl: number;
  gross_pnl: number;
  fees: number;
  slippage: number;
  funding: number;
  trades: number;
};

export type ArmedEntry = {
  id: number;
  symbol: string;
  strategy: string;
  trigger_price: number;
  condition: "below" | "above";
  created_at: string;
  expires_at: string;
  status: "armed" | "fired" | "expired" | "cancelled";
  reason: string | null;
  position_id: number | null;
};

export type InstanceKind = "dca" | "grid";

/** The non-numeric options of a robot (``bot.instances`` DcaParams / GridParams), sent with its fields. */
export type InstanceOptions = {
  /** One LIMIT GTC buy rests on the book at the next safety order or grid level (off by default). */
  resting_buys: boolean;
  /** DCA only: start a new round after the take-profit. */
  repeat?: boolean;
  /** Grid only: level spacing. */
  mode?: "arithmetic" | "geometric";
};

export type BotInstance = {
  id: number;
  kind: InstanceKind;
  symbol: string;
  params: Record<string, number | string | boolean | null>;
  state: Record<string, unknown>;
  status: "active" | "stopping" | "stopped";
  created_at: string;
  stopped_at: string | null;
  realized_pnl: number;
  reason: string | null;
  position: Position | null;
};

export type InstancePreview = {
  kind: InstanceKind;
  symbol: string;
  price: number;
  spread: number;
  taker: number;
  order_cap: number | null;
  problems: string[];
  plan: {
    ladder?: { index: number; price: number; usd: number; avg: number; qty: number }[];
    levels?: number[];
    qty_per_level?: number[];
    stop_price?: number;
    total_usd?: number;
    first_buy_usd?: number;
    profit_per_grid_net?: number;
    loss_at_stop_usd: number;
    orders_per_round?: number;
    max_held_usd?: number;
  };
  replay: {
    interval?: string;
    error?: string;
    windows?: ReplayWindow[];
  };
};

/** The plan replayed on the last ``days`` days of 1h candles. */
export type ReplayWindow = {
  days: number;
  rounds?: number;
  wins?: number;
  grid_trades?: number;
  ended?: string | null;
  pnl_usd?: number;
  pnl_pct?: number;
  max_drawdown_pct?: number;
  /** Share of the closes outside the grid's range. */
  out_of_range_share: number;
  /** Grid only: out of range more than 30% of the time. */
  warn: boolean;
};
