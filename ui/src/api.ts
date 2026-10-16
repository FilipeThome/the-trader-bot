import type { Balance, CalibrationRow, Decision, Features, Market, Opportunity, PnlDay, Position, Recommendation, Status, Trade } from "./types";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    // The API refuses writes without this header; another site cannot add it without a CORS preflight.
    headers: { "Content-Type": "application/json", "X-Bot-Client": "panel" },
    ...init,
  });
  const body = await response.json().catch(() => ({ detail: response.statusText }));
  if (!response.ok) throw new Error(describeError(body.detail ?? body, response.status));
  return body as T;
}

/** The API's refusal in words: a string, a list of reasons, or the fields a 422 names. */
function describeError(detail: unknown, status: number): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail.map((item) => {
      const entry = item as { loc?: unknown[]; msg?: string };
      const field = (entry.loc ?? []).filter((part) => part !== "body").join(".");
      return field ? `${field}: ${entry.msg ?? "inválido"}` : (entry.msg ?? "inválido");
    });
    return parts.join("; ") || `erro ${status}`;
  }
  if (detail && typeof detail === "object") {
    const reasons = (detail as { reasons?: unknown }).reasons;
    if (Array.isArray(reasons) && reasons.length) return reasons.map(String).join("; ");
  }
  return `erro ${status}`;
}

export const api = {
  status: () => request<Status>("/bot/status"),
  opportunities: () => request<{ rows: Opportunity[] }>("/bot/opportunities"),
  positions: () => request<{ rows: Position[] }>("/bot/positions"),
  performance: () => request<{ days: PnlDay[]; calibration: CalibrationRow[] }>("/bot/performance"),
  trade: (positionId: number) => request<Trade>(`/bot/trades/${positionId}`),
  features: (symbol: string) => request<Features>(`/bot/features?symbol=${encodeURIComponent(symbol)}`),
  markets: () => request<{ symbols: Market[] }>("/bot/markets"),
  balance: () => request<Balance>("/bot/balance"),
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
    request<{ results: Decision[]; cycle_started: boolean }>("/bot/opportunities/execute", {
      method: "POST",
      body: JSON.stringify({ symbol, strategy }),
    }),
  close: (positionId: number) =>
    request<{ result: { status: string; error?: string } }>("/bot/positions/close", {
      method: "POST",
      body: JSON.stringify({ position_id: positionId }),
    }),
  validate: (strategy: string) =>
    request(`/bot/strategies/${encodeURIComponent(strategy)}/validate`, { method: "POST" }),
  chooseSymbols: (preset: "top" | "default") =>
    request<{ symbols: string[]; left_out: string[] }>("/bot/symbols", { method: "POST", body: JSON.stringify({ preset }) }),
  updateConfig: (payload: Record<string, unknown>) =>
    request("/bot/config", { method: "PUT", body: JSON.stringify(payload) }),
};
