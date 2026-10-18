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
};

export type ValidationJob = {
  status: "running" | "done" | "error";
  stage?: string;
  done: number;
  total: number;
  error?: string;
  result?: { passed: boolean; sharpe: number; max_drawdown: number; trades: number; wins: number; reason?: string };
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
};

export type DayPreset = {
  changes: { field: string; from: unknown; to: unknown }[];
  order_cap_usd: number;
  risk_notional_usd: number;
  capped_by_order_cap: boolean;
  mandate_orders_per_day: number;
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
    error?: string;
    days?: number;
    rounds?: number;
    wins?: number;
    grid_trades?: number;
    ended?: string | null;
    pnl_usd?: number;
    pnl_pct?: number;
    max_drawdown_pct?: number;
  };
};
