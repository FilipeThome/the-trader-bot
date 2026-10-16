const strategies: Record<string, string> = {
  trend: "Trend",
  mean_reversion: "Mean Reversion",
  momentum: "Momentum",
  funding_carry: "Funding Carry",
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
