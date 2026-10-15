import type { Features, Market, Opportunity, PnlDay, Position, Recommendation, Status } from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  const body = await response.json();
  if (!response.ok) {
    const detail = body.detail;
    const message = typeof detail === "string" ? detail : JSON.stringify(detail ?? body);
    throw new Error(message);
  }
  return body as T;
}

export const api = {
  status: () => request<Status>("/bot/status"),
  opportunities: () => request<{ rows: Opportunity[] }>("/bot/opportunities"),
  positions: () => request<{ rows: Position[] }>("/bot/positions"),
  performance: () => request<{ days: PnlDay[] }>("/bot/performance"),
  features: (symbol: string) => request<Features>(`/bot/features?symbol=${encodeURIComponent(symbol)}`),
  markets: () => request<{ symbols: Market[] }>("/bot/markets"),
  recommendations: () => request<{ rows: Recommendation[] }>("/bot/recommendations"),
  history: () => request<{ rows: Record<string, unknown>[] }>("/bot/history"),
  start: () => request("/bot/start", { method: "POST" }),
  stop: () => request("/bot/stop", { method: "POST" }),
  halt: () => request("/bot/halt", { method: "POST" }),
  resume: () => request("/bot/resume", { method: "POST" }),
  tick: () => request("/bot/tick", { method: "POST" }),
  ignore: (symbol: string, strategy: string) =>
    request("/bot/opportunities/ignore", { method: "POST", body: JSON.stringify({ symbol, strategy }) }),
  execute: (symbol: string, strategy: string) =>
    request("/bot/opportunities/execute", { method: "POST", body: JSON.stringify({ symbol, strategy }) }),
  close: (symbol: string, strategy: string) =>
    request("/bot/positions/close", { method: "POST", body: JSON.stringify({ symbol, strategy }) }),
  updateConfig: (payload: Record<string, unknown>) =>
    request("/bot/config", { method: "PUT", body: JSON.stringify(payload) }),
};
