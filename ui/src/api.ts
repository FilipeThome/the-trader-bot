import type { AdviceCalibrationLine, AdvisorUsage, ArmedEntry, Balance, BotInstance, CalibrationRow, DayPreset, Decision, Features, InstanceKind, InstancePreview, Market, Opportunity, PairSlippage, PnlDay, Position, Recommendation, Status, Trade } from "./types";

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
  performance: () =>
    request<{
      days: PnlDay[];
      calibration: CalibrationRow[];
      pairs?: PairSlippage[];
      equity_peak?: number;
      advice_calibration?: AdviceCalibrationLine[];
      advisor_usage?: AdvisorUsage;
    }>("/bot/performance"),
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
  dayPreset: () => request<DayPreset>("/bot/presets/day"),
  applyDayPreset: () => request<DayPreset & { status: string }>("/bot/presets/day", { method: "POST" }),
  armed: () => request<{ rows: ArmedEntry[] }>("/bot/armed"),
  arm: (symbol: string, strategy: string, trigger_price: number, hours: number) =>
    request<{ condition: string; expires_at: string; cycle_started: boolean }>("/bot/armed", {
      method: "POST",
      body: JSON.stringify({ symbol, strategy, trigger_price, hours }),
    }),
  disarm: (id: number) => request(`/bot/armed/${id}`, { method: "DELETE" }),
  instances: () => request<{ rows: BotInstance[]; instance_orders_per_day: number }>("/bot/instances"),
  previewInstance: (kind: InstanceKind, symbol: string, params: Record<string, unknown>) =>
    request<InstancePreview>("/bot/instances/preview", { method: "POST", body: JSON.stringify({ kind, symbol, params }) }),
  createInstance: (kind: InstanceKind, symbol: string, params: Record<string, unknown>) =>
    request<{ id: number; step: string | null; cycle_started: boolean }>("/bot/instances", {
      method: "POST",
      body: JSON.stringify({ kind, symbol, params }),
    }),
  stopInstance: (id: number) => request<{ instance_status: string; outcome: string }>(`/bot/instances/${id}/stop`, { method: "POST" }),
  /**
   * PUT /bot/config. ``reconfirm_mandate: true`` rewrites the mandate from the resulting config;
   * a ``flatten_on_halt`` change without it is refused (409), because the halt follows the mandate.
   */
  updateConfig: (payload: Record<string, unknown>) =>
    request("/bot/config", { method: "PUT", body: JSON.stringify(payload) }),
};
