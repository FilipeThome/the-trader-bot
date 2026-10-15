import { useEffect } from "react";
import { NavLink, Route, Routes } from "react-router";

import { Help } from "./Help";
import { Controls } from "./pages/Controls";
import { History } from "./pages/History";
import { Opportunities } from "./pages/Opportunities";
import { Performance } from "./pages/Performance";
import { Positions } from "./pages/Positions";
import { useBot } from "./store";

const links: [string, string, string][] = [
  ["/", "Oportunidades", "Lista o que o robô está vendo agora, da melhor nota para a pior. Nada aqui vira ordem sozinho se o status não for Pronta."],
  ["/positions", "Posições", "Mostra o que está aberto neste ambiente: quanto entrou, onde corta a perda e onde realiza o ganho."],
  ["/performance", "Desempenho", "Mostra se o dinheiro cresceu ou encolheu ao longo dos dias, já descontando custos."],
  ["/controls", "Controles", "Liga, pausa e configura o robô. Também é aqui que se troca o ensaio (testnet) pela conta real."],
  ["/history", "Histórico", "Registro do que o robô decidiu, inclusive quando escolheu não operar."],
];

export function App() {
  const load = useBot((state) => state.load);
  const status = useBot((state) => state.status);
  const dark = useBot((state) => state.dark);
  const setDark = useBot((state) => state.setDark);
  const error = useBot((state) => state.error);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
  }, [dark]);

  useEffect(() => {
    void load();
    const source = new EventSource("/bot/events");
    source.onmessage = () => void load();
    const timer = window.setInterval(() => void load(), 15000);
    return () => {
      source.close();
      window.clearInterval(timer);
    };
  }, [load]);

  const production = status?.mode === "production";

  return (
    <div className="min-h-screen bg-neutral-100 text-neutral-900 dark:bg-neutral-950 dark:text-neutral-100">
      <header className="flex items-center justify-between border-b border-neutral-200 px-6 py-4 dark:border-neutral-800">
        <div>
          <p className="text-xs uppercase tracking-wide text-neutral-500">Bot de trading</p>
          <h1 className="text-lg font-semibold">Painel</h1>
        </div>
        <div className="flex items-center gap-3">
          <span className="inline-flex items-center">
            <span
              className={`rounded px-2 py-1 text-xs font-medium ${production ? "bg-red-600 text-white" : "bg-neutral-300 text-neutral-800 dark:bg-neutral-700 dark:text-neutral-100"}`}
            >
              {production ? "Produção" : "Spot Testnet"}
            </span>
            <Help
              align="end"
              text={
                production
                  ? "Conta real da Binance. Ordens daqui usam o capital de verdade. O selo fica vermelho de propósito para não passar despercebido."
                  : "Ambiente de ensaio da Binance. O robô treina aqui sem usar a conta real. O selo só fica vermelho, com o nome Produção, depois que você promove de propósito."
              }
            />
          </span>
          <span className="inline-flex items-center">
            <button className="text-sm underline" onClick={() => setDark(!dark)} type="button">
              {dark ? "Tema claro" : "Tema escuro"}
            </button>
            <Help align="end" text="Só muda as cores da tela. Não liga, não pausa e não envia ordem." />
          </span>
        </div>
      </header>
      {status && (
        <p className="flex flex-wrap items-center gap-x-1 px-6 py-3 text-sm">
          <span>PnL líquido do dia {money(status.net_pnl)}</span>
          <Help text="Quanto o robô ganhou ou perdeu hoje, já tirando taxa, diferença entre o preço esperado e o executado, e funding. Não é o valor bruto." />
          <span>· trades {status.trades}</span>
          <Help text="Quantas operações foram concluídas hoje neste ambiente." />
          <span>· violações evitadas {status.violations_avoided}</span>
          <Help text="Vezes em que uma ordem foi barrada porque passaria de um limite de risco, como tamanho, perda do dia ou número de posições." />
          <span>{status.halted ? "· halt ativo" : status.running ? "· em ciclo" : "· parado"}</span>
          <Help
            text={
              status.halted
                ? "Parada de emergência ligada. O robô não volta sozinho. Cancele o alerta em Controles e só então clique em Iniciar."
                : status.running
                  ? "O robô está olhando o mercado cerca de uma vez por minuto e só envia ordem se a oportunidade passar em todos os limites."
                  : "O ciclo automático está desligado. Nada de novo é analisado nem enviado até você clicar em Iniciar."
            }
          />
        </p>
      )}
      {error && <p className="px-6 text-sm text-red-600">{error}</p>}
      <nav className="flex flex-wrap gap-4 px-6 pb-4 text-sm">
        {links.map(([path, label, hint]) => (
          <span key={path} className="inline-flex items-center">
            <NavLink to={path} end={path === "/"} className={({ isActive }) => (isActive ? "font-semibold" : "text-neutral-500")}>
              {label}
            </NavLink>
            <Help text={hint} />
          </span>
        ))}
      </nav>
      <main className="px-6 pb-10">
        <Routes>
          <Route path="/" element={<Opportunities />} />
          <Route path="/positions" element={<Positions />} />
          <Route path="/performance" element={<Performance />} />
          <Route path="/controls" element={<Controls />} />
          <Route path="/history" element={<History />} />
        </Routes>
      </main>
    </div>
  );
}

export function formatPct(value: number): string {
  const sign = value > 0 ? "+" : "";
  return `${sign}${(value * (Math.abs(value) < 2 ? 100 : 1)).toFixed(2)}${Math.abs(value) < 2 ? "%" : ""}`;
}

export function money(value: number): string {
  return value.toLocaleString("pt-BR", { style: "currency", currency: "USD" });
}
