import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Same resolution as bot.paths.base_runtime_root: the API writes its token there on start.
function runtimeRoot(): string {
  for (const name of ["BOT_RUNTIME_ROOT", "VIBE_TRADING_HOME"]) {
    const value = process.env[name]?.trim();
    if (value) return value;
  }
  return join(homedir(), ".the-trader-bot");
}

let tokenWarning = "";

// Read on every request, so the panel works even when Vite starts before the API.
function apiToken(): string {
  try {
    return readFileSync(join(runtimeRoot(), "api-token"), "utf-8").trim();
  } catch (error) {
    // A missing file only means the API has not started yet. Anything else (EACCES under
    // a different uid in Docker) would turn every request into a silent 401: say so once.
    const code = (error as NodeJS.ErrnoException).code ?? "unknown";
    if (code !== "ENOENT" && tokenWarning !== code) {
      tokenWarning = code;
      console.error(`api-token unreadable in ${runtimeRoot()} (${code}): every API call will get 401`);
    }
    return "";
  }
}

// Docker sets these: the API is reached as http://api:8000 and the panel listens on all interfaces.
const apiUrl = process.env.BOT_API_URL?.trim() || "http://127.0.0.1:8000";
const panelHost = process.env.BOT_PANEL_HOST?.trim() || "127.0.0.1";

const proxy = {
  "/bot": {
    target: apiUrl,
    changeOrigin: true,
    configure: (server: { on: (event: "proxyReq", listener: (request: { setHeader: (name: string, value: string) => void }) => void) => void }) => {
      server.on("proxyReq", (request) => {
        request.setHeader("X-Bot-Token", apiToken());
        // The API accepts only its own Host (DNS-rebinding guard), whatever name reaches it.
        request.setHeader("Host", "127.0.0.1:8000");
      });
    },
  },
};

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { host: panelHost, port: 5173, proxy },
  preview: { host: panelHost, port: 5173, strictPort: true, proxy },
});
