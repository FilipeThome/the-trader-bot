const strategies: Record<string, string> = {
  trend: "Trend",
  mean_reversion: "Mean Reversion",
  momentum: "Momentum",
  funding_carry: "Funding Carry",
  intraday_momentum: "Day · Momento intradiário",
  vwap_pullback: "Day · Pullback na VWAP",
  dca: "Robô DCA",
  grid: "Robô Grid",
};

const sides: Record<string, string> = {
  buy: "Compra",
  sell: "Venda",
  long: "Compra",
  short: "Venda",
};

const decisions: Record<string, string> = {
  nao_operar: "Não operar",
  pronta: "Pronta",
  potencial: "Potencial",
  bloqueada: "Bloqueada",
  ignorada: "Ignorada",
  em_posicao: "Em posição",
  aumento: "Aumento de posição",
  DENY: "Recusada",
  ALLOW: "Aceita",
};

export const statusLabels: Record<string, string> = {
  pronta: "Pronta",
  potencial: "Potencial",
  em_posicao: "Em posição",
  bloqueada: "Bloqueada",
  ignorada: "Ignorada",
};

export function strategyName(id: string): string {
  if (!id) return "—";
  return strategies[id] ?? id;
}

export function sideName(side: string): string {
  if (!side) return "—";
  return sides[side.toLowerCase()] ?? side;
}

export function decisionName(value: string): string {
  if (!value) return "—";
  return decisions[value] ?? statusLabels[value] ?? value;
}

/** The limit that set an order's size: the labels of bot.risk.SIZE_LIMITS. */
export const sizeLimits: Record<string, string> = {
  risk: "risco por trade",
  kelly: "Kelly",
  safety: "teto por ordem",
  venue: "teto da corretora",
  volatility: "volatilidade (ATR diário)",
  exposure: "exposição total",
  vol_target: "alvo de volatilidade",
  cash: "caixa",
  throttle: "volatilidade de 7 dias alta (metade)",
  advice: "análise IA",
};

export function sizeLimitName(key: string): string {
  return sizeLimits[key] ?? key;
}

/** Drawdown levels kept in bot_state.risk_state. */
export const riskLevels: Record<string, string> = {
  normal: "normal",
  cautious: "cautela",
  reducing: "só reduzir",
  halt: "halt",
};

/** Kinds of the unprotected window between taking a stop off and placing the next one. */
export const stopWindowKinds: Record<string, string> = {
  trail: "stop móvel",
  tp1: "TP1 do day trade",
  partial: "venda parcial",
  restop: "novo stop após compra",
  exit_remainder: "stop do que sobrou",
};

/** Opportunity.advice: a done verdict's name, or "pending" while the DeepSeek call runs. */
export const adviceVerdicts: Record<string, string> = {
  confirm: "IA confirma",
  reduce: "IA reduz",
  veto: "IA desaconselha",
  abstain: "IA sem opinião",
  pending: "IA analisando",
};

/** The advisor's settings (bot.config.AdvisorConfig): what each mode does with a verdict. */
export const advisorModes: Record<string, string> = {
  off: "desligado",
  shadow: "sombra (só registra)",
  filter: "filtro (pode reduzir ou barrar)",
};

export const advisorModels: Record<string, string> = {
  "deepseek-flash": "DeepSeek Flash (padrão)",
  "deepseek-v4-pro": "DeepSeek V4 Pro",
};
