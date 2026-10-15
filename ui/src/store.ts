import { create } from "zustand";

import { api } from "./api";
import type { Opportunity, PnlDay, Position, Status } from "./types";

type BotState = {
  status: Status | null;
  rows: Opportunity[];
  positions: Position[];
  days: PnlDay[];
  history: Record<string, unknown>[];
  error: string;
  dark: boolean;
  load: () => Promise<void>;
  setDark: (dark: boolean) => void;
};

export const useBot = create<BotState>((set) => ({
  status: null,
  rows: [],
  positions: [],
  days: [],
  history: [],
  error: "",
  dark: window.matchMedia("(prefers-color-scheme: dark)").matches,
  setDark: (dark) => set({ dark }),
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
        history: history.rows,
        error: "",
      });
    } catch (error) {
      set({ error: error instanceof Error ? error.message : "API indisponível" });
    }
  },
}));
