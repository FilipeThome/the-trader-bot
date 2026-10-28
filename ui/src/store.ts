import { create } from "zustand";

import { api } from "./api";
import type { AdviceCalibrationLine, AdvisorUsage, CalibrationRow, Opportunity, PairSlippage, PnlDay, Position, Status } from "./types";

/** What the last panel action did, shown where it is always visible. */
export type Notice = { kind: "busy" | "ok" | "error"; text: string };

/** How a panel action ended: the reply on success, nothing on an error (already shown as the notice). */
export type Outcome<T> = { ok: true; result: T } | { ok: false };

type BotState = {
  status: Status | null;
  rows: Opportunity[];
  positions: Position[];
  days: PnlDay[];
  calibration: CalibrationRow[];
  /** Per-pair round-trip slippage of the last 30 days and the budget block. */
  pairs: PairSlippage[];
  /** The advisor's verdicts measured by counterfactual R, and its calls and cost. */
  adviceCalibration: AdviceCalibrationLine[];
  advisorUsage: AdvisorUsage | null;
  history: Record<string, unknown>[];
  error: string;
  dark: boolean;
  notice: Notice | null;
  busy: boolean;
  load: () => Promise<void>;
  setDark: (dark: boolean) => void;
  clearNotice: () => void;
  /**
   * Run one panel action: show it as running, then its result or its error, then reload.
   * ``describe`` turns the API reply into the success text; the label is used otherwise.
   * Callers clear their inputs only when ``ok`` is true, so a refused change keeps what was typed.
   */
  perform: <T>(label: string, action: () => Promise<T>, describe?: (result: T) => string) => Promise<Outcome<T>>;
};

let noticeTimer = 0;

export const useBot = create<BotState>((set, get) => ({
  status: null,
  rows: [],
  positions: [],
  days: [],
  calibration: [],
  pairs: [],
  adviceCalibration: [],
  advisorUsage: null,
  history: [],
  error: "",
  dark: window.matchMedia("(prefers-color-scheme: dark)").matches,
  notice: null,
  busy: false,
  setDark: (dark) => set({ dark }),
  clearNotice: () => set({ notice: null }),
  load: async () => {
    try {
      const [status, opportunities, positions, performance, history] = await Promise.all([
        api.status(),
        api.opportunities(),
        api.positions(),
        api.performance(),
        api.history(),
      ]);
      set({
        status,
        rows: opportunities.rows,
        positions: positions.rows,
        days: performance.days,
        calibration: performance.calibration ?? [],
        pairs: performance.pairs ?? [],
        adviceCalibration: performance.advice_calibration ?? [],
        advisorUsage: performance.advisor_usage ?? null,
        history: history.rows,
        error: "",
      });
    } catch (error) {
      set({ error: error instanceof Error ? error.message : "API indisponível" });
    }
  },
  perform: async (label, action, describe) => {
    window.clearTimeout(noticeTimer);
    set({ busy: true, notice: { kind: "busy", text: `${label}…` } });
    let outcome: Outcome<Awaited<ReturnType<typeof action>>> = { ok: false };
    try {
      const result = await action();
      outcome = { ok: true, result };
      set({ notice: { kind: "ok", text: describe ? describe(result) : `${label}: feito` } });
      // A success fades; an error stays until the next action or the close button.
      noticeTimer = window.setTimeout(() => {
        if (get().notice?.kind === "ok") set({ notice: null });
      }, 6000);
    } catch (error) {
      set({ notice: { kind: "error", text: `${label}: ${error instanceof Error ? error.message : "falhou"}` } });
    } finally {
      set({ busy: false });
      await get().load();
    }
    return outcome;
  },
}));
