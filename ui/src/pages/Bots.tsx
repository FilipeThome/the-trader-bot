import { useEffect, useState } from "react";
import { Link } from "react-router";

import { api } from "../api";
import { formatPct } from "../App";
import { Help } from "../Help";
import { strategyName } from "../labels";
import { useBot } from "../store";
import type { BotInstance, InstanceKind, InstancePreview } from "../types";

/** [param, label, default, help, percent]: a percent field is typed in % and sent as a fraction. */
type Field = [string, string, string, string, boolean?];

const fields: Record<InstanceKind, Field[]> = {
  dca: [
    ["base_usd", "Ordem base (USDT)", "10", "A primeira compra, a mercado, quando o robô começa uma rodada."],
    ["so_count", "Ordens de segurança", "3", "Quantas compras extras o robô faz se o preço cair. De 1 a 8."],
    ["step_pct", "Queda até a 1ª ordem (%)", "1.5", "Quanto o preço cai, a partir da base, até a primeira ordem de segurança.", true],
    ["step_mult", "Multiplicador da distância", "1", "Cada nova distância é a anterior vezes este número (Price Deviation Multiplier). 1 = distâncias iguais."],
    ["size_mult", "Multiplicador do tamanho", "1", "Cada ordem de segurança é a base vezes este número elevado à sua posição (DCA Order Size Multiplier)."],
    ["take_profit_pct", "Take-profit (%)", "1.5", "Vende tudo quando o preço sobe isto acima do preço médio.", true],
    ["stop_pct", "Stop abaixo da última ordem (%)", "3", "O stop fica na corretora este tanto abaixo da última ordem de segurança.", true],
    ["max_orders_per_day", "Ordens por dia", "20", "Teto diário de ordens deste robô, contando os stops trocados."],
  ],
  grid: [
    ["lower", "Preço inferior", "", "Limite de baixo da faixa."],
    ["upper", "Preço superior", "", "Limite de cima da faixa."],
    ["grids", "Grades", "10", "Quantos intervalos entre os limites. De 2 a 30."],
    ["invest_usd", "Investimento (USDT)", "50", "Dividido igualmente entre as grades."],
    ["stop_price", "Stop", "", "Abaixo do preço inferior. Fica na corretora para tudo o que o grid tiver e encerra o grid."],
    ["take_profit_price", "Take-profit (opcional)", "", "Acima do preço superior: vende tudo e encerra o grid."],
    ["max_orders_per_day", "Ordens por dia", "20", "Teto diário de ordens deste robô, contando os stops trocados."],
  ],
};

function initial(kind: InstanceKind): Record<string, string> {
  return Object.fromEntries(fields[kind].map(([name, , value]) => [name, value]));
}

function payload(kind: InstanceKind, values: Record<string, string>, extra: Record<string, unknown>): Record<string, unknown> {
  const params: Record<string, unknown> = { ...extra };
  for (const [name, , , , percent] of fields[kind]) {
    const raw = values[name]?.trim() ?? "";
    if (raw === "") {
      if (name === "take_profit_price") params[name] = null;
      continue;
    }
    const number = Number(raw.replace(",", "."));
    params[name] = percent ? number / 100 : number;
  }
  return params;
}

const statusNames: Record<string, string> = { active: "ativo", stopping: "parando", stopped: "parado" };

export function Bots() {
  const status = useBot((state) => state.status);
  const perform = useBot((state) => state.perform);
  const busy = useBot((state) => state.busy);
  const [kind, setKind] = useState<InstanceKind>("dca");
  const [symbol, setSymbol] = useState("");
  const [values, setValues] = useState<Record<InstanceKind, Record<string, string>>>({ dca: initial("dca"), grid: initial("grid") });
  const [repeat, setRepeat] = useState(false);
  const [mode, setMode] = useState<"arithmetic" | "geometric">("arithmetic");
  const [preview, setPreview] = useState<InstancePreview | null>(null);
  const [previewKey, setPreviewKey] = useState("");
  const [rows, setRows] = useState<BotInstance[]>([]);
  const [reserved, setReserved] = useState<number | null>(null);
  const [reserveInput, setReserveInput] = useState("");

  const pair = symbol || status?.symbols[0] || "";
  const extra = kind === "dca" ? { repeat } : { mode };
  const params = payload(kind, values[kind], extra);
  const key = JSON.stringify([kind, pair, params]);

  async function refresh() {
    try {
      const body = await api.instances();
      setRows(body.rows);
      setReserved(body.instance_orders_per_day);
    } catch {
      // The status bar already reports an unreachable API.
    }
  }

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, []);

  async function simulate() {
    const result = await perform(`Simular ${kind.toUpperCase()} em ${pair}`, () => api.previewInstance(kind, pair, params), (body) =>
      body.problems.length ? `Simulação pronta: ${body.problems.length} problema(s) a resolver` : "Simulação pronta: o plano pode rodar",
    );
    if (result) {
      setPreview(result);
      setPreviewKey(key);
    }
  }

  async function create() {
    await perform(`Criar ${kind.toUpperCase()} em ${pair}`, () => api.createInstance(kind, pair, params), (body) =>
      [`Robô ${kind.toUpperCase()} criado em ${pair}`, body.step, body.cycle_started ? "ciclo iniciado" : ""].filter(Boolean).join(" · "),
    );
    setPreview(null);
    void refresh();
  }

  const canCreate = preview != null && previewKey === key && preview.problems.length === 0 && !busy;

  return (
    <section className="space-y-6 text-sm">
      <p className="max-w-3xl text-neutral-600 dark:text-neutral-400">
        Robôs configurados por você, como o Spot DCA e o Spot Grid da Binance. Antes de criar, a simulação mostra o plano inteiro, a perda se o stop
        for atingido e como o mesmo plano teria ido nos últimos 30 dias. O robô compra a mercado e vende com IOC a cada ciclo de 1 minuto, sempre com
        um stop na corretora para tudo o que tiver. Ele ocupa o par: nenhuma estratégia entra nele enquanto o robô roda.
      </p>

      <div className="flex flex-wrap items-center gap-2">
        Ordens por dia reservadas para robôs: <strong>{reserved ?? "—"}</strong>
        <Help text="O mandato da corretora soma esta reserva ao orçamento diário das estratégias. Os tetos diários dos robôs ativos precisam caber nela. Sem reserva, nenhum robô é criado." />
        <input className="w-20 border border-neutral-300 bg-transparent px-1 dark:border-neutral-700" value={reserveInput} placeholder="40" onChange={(event) => setReserveInput(event.target.value)} />
        <button
          className="underline disabled:opacity-50"
          type="button"
          disabled={busy || !/^\d+$/.test(reserveInput)}
          onClick={() =>
            void perform("Salvar reserva de ordens", () => api.updateConfig({ limits: { instance_orders_per_day: Number(reserveInput) }, reconfirm_mandate: true })).then(() => refresh())
          }
        >
          salvar e reconfirmar o mandato
        </button>
      </div>

      <div className="max-w-3xl space-y-3 rounded border border-neutral-300 p-4 dark:border-neutral-700">
        <div className="flex flex-wrap items-center gap-4">
          {(["dca", "grid"] as const).map((item) => (
            <label key={item}>
              <input type="radio" checked={kind === item} onChange={() => { setKind(item); setPreview(null); }} /> {strategyName(item)}
            </label>
          ))}
          <label>
            Par{" "}
            <select className="bg-transparent" value={pair} onChange={(event) => { setSymbol(event.target.value); setPreview(null); }}>
              {(status?.symbols ?? []).map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
          {kind === "dca" ? (
            <label>
              <input type="checkbox" checked={repeat} onChange={(event) => setRepeat(event.target.checked)} /> repetir após o take-profit
            </label>
          ) : (
            <label>
              Espaçamento{" "}
              <select className="bg-transparent" value={mode} onChange={(event) => setMode(event.target.value as "arithmetic" | "geometric")}>
                <option value="arithmetic">aritmético</option>
                <option value="geometric">geométrico</option>
              </select>
            </label>
          )}
        </div>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {fields[kind].map(([name, label, , help]) => (
            <label key={name} className="flex items-center justify-between gap-2">
              <span className="inline-flex items-center">
                {label}
                <Help text={help} />
              </span>
              <input
                className="w-28 border border-neutral-300 bg-transparent px-1 text-right dark:border-neutral-700"
                inputMode="decimal"
                value={values[kind][name] ?? ""}
                onChange={(event) => setValues({ ...values, [kind]: { ...values[kind], [name]: event.target.value } })}
              />
            </label>
          ))}
        </div>
        <div className="flex gap-4">
          <button className="underline disabled:opacity-50" type="button" disabled={busy || !pair} onClick={() => void simulate()}>
            simular
          </button>
          <button className="rounded bg-green-700 px-3 py-1 font-medium text-white disabled:opacity-40" type="button" disabled={!canCreate} onClick={() => void create()}>
            criar robô
          </button>
          <Help text="Criar só fica disponível depois de uma simulação sem problemas, com os mesmos valores. A primeira compra sai na hora." />
        </div>
      </div>

      {preview && <PreviewCard preview={preview} />}

      <table className="w-full text-left">
        <thead className="text-neutral-500">
          <tr>
            <th className="pr-3 font-medium">Robô</th>
            <th className="pr-3 font-medium">Par</th>
            <th className="pr-3 font-medium">Estado</th>
            <th className="pr-3 font-medium">
              Estoque<Help text="O que o robô tem agora: quantidade, preço médio e o stop que está na corretora." />
            </th>
            <th className="pr-3 font-medium">
              Realizado<Help text="Lucro ou perda já fechados pelas vendas deste robô, depois das taxas." />
            </th>
            <th className="font-medium">Último evento</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id} className="border-t border-neutral-200 dark:border-neutral-800">
              <td className="pr-3">
                #{row.id} {strategyName(row.kind)}
              </td>
              <td className="pr-3">{row.symbol}</td>
              <td className="pr-3">{statusNames[row.status] ?? row.status}</td>
              <td className="pr-3">
                {row.position ? (
                  <Link className="underline" to={`/trade/${row.position.id}`}>
                    {row.position.qty} @ {row.position.avg_price?.toFixed(4)} · stop {row.position.stop_price}
                  </Link>
                ) : (
                  "—"
                )}
              </td>
              <td className={`pr-3 ${row.realized_pnl >= 0 ? "text-green-600" : "text-red-600"}`}>US$ {row.realized_pnl.toFixed(2)}</td>
              <td>
                {row.reason ?? "—"}
                {row.status !== "stopped" && (
                  <button
                    className="ml-2 underline"
                    type="button"
                    disabled={busy}
                    onClick={() => void perform(`Parar robô #${row.id}`, () => api.stopInstance(row.id), (body) => `Robô #${row.id}: ${statusNames[body.instance_status] ?? body.instance_status}`).then(() => refresh())}
                  >
                    parar e vender
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length === 0 && <p className="text-neutral-500">Nenhum robô neste ambiente.</p>}
    </section>
  );
}

function PreviewCard({ preview }: { preview: InstancePreview }) {
  const { plan, replay } = preview;
  return (
    <aside className="max-w-3xl space-y-3 rounded border border-neutral-300 p-4 dark:border-neutral-700">
      <h2 className="font-semibold">
        Simulação · {strategyName(preview.kind)} · {preview.symbol} a {preview.price}
      </h2>
      {preview.problems.length > 0 ? (
        <ul className="list-disc pl-5 text-red-600">
          {preview.problems.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      ) : (
        <p className="text-green-600">O plano passa em todos os limites.</p>
      )}
      <p>
        Perda se o stop for atingido com tudo comprado: <strong className="text-red-600">US$ {plan.loss_at_stop_usd.toFixed(2)}</strong>
        {plan.stop_price != null && <> · stop em {plan.stop_price.toFixed(6)}</>}
        {plan.total_usd != null && <> · capital total US$ {plan.total_usd.toFixed(2)}</>}
        {plan.first_buy_usd != null && <> · primeira compra US$ {plan.first_buy_usd.toFixed(2)}</>}
        {plan.profit_per_grid_net != null && <> · lucro por grade {formatPct(plan.profit_per_grid_net)} líquido</>}
        {preview.order_cap != null && <> · teto por ordem US$ {preview.order_cap.toFixed(2)}</>}
      </p>
      {plan.ladder && (
        <table className="text-left">
          <thead className="text-neutral-500">
            <tr>
              <th className="pr-4 font-medium">Ordem</th>
              <th className="pr-4 font-medium">Preço</th>
              <th className="pr-4 font-medium">USDT</th>
              <th className="font-medium">Preço médio depois</th>
            </tr>
          </thead>
          <tbody>
            {plan.ladder.map((row) => (
              <tr key={row.index}>
                <td className="pr-4">{row.index === 0 ? "base" : `segurança ${row.index}`}</td>
                <td className="pr-4">{row.price.toFixed(6)}</td>
                <td className="pr-4">{row.usd.toFixed(2)}</td>
                <td>{row.avg.toFixed(6)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {plan.levels && <p className="text-neutral-500">Níveis: {plan.levels.map((level) => level.toFixed(4)).join(" · ")}</p>}
      <p className="inline-flex items-center">
        {replay.error
          ? `Histórico: ${replay.error}`
          : `Últimos ${replay.days} dias: ${formatPct(replay.pnl_pct ?? 0)} (US$ ${(replay.pnl_usd ?? 0).toFixed(2)}) · queda máxima ${formatPct(-(replay.max_drawdown_pct ?? 0))}` +
            (replay.rounds != null ? ` · ${replay.wins}/${replay.rounds} rodadas no alvo` : ` · ${replay.grid_trades} vendas de grade`) +
            (replay.ended === "stop" ? " · terminou no stop" : replay.ended === "take_profit" ? " · terminou no take-profit" : "")}
        <Help align="end" text="O mesmo plano repetido em velas de 15 minutos, com as taxas e metade do spread em cada perna, e o stop pela mínima da vela. Rodadas de DCA se repetem no replay. Mostra o passado, não promete o futuro." />
      </p>
    </aside>
  );
}
