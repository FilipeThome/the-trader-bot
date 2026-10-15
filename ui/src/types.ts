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
};

export type Position = {
  symbol: string;
  strategy: string;
  side: string;
  qty: number;
  avg_price: number;
  stop_price: number | null;
  target_price: number | null;
  opened_at: string;
};

export type Market = { symbol: string; base: string; quote: string };

export type Recommendation = {
  symbol: string;
  strategy: string;
  action: string;
  score: number;
  net_return: number;
  status: string;
  reason: string;
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
  halted: boolean;
  mode: "testnet" | "production";
  symbols: string[];
  capital_usd: number;
  allocated_capital_usd: number;
  strategies: Record<string, boolean>;
  limits: Record<string, number>;
  flatten_on_halt: boolean;
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
