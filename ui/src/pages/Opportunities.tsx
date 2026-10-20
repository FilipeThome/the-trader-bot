import { useEffect, useState } from "react";
import { Link } from "react-router";

import { api } from "../api";
import { formatPct, money } from "../App";
import { Help } from "../Help";
import { MarketCharts } from "../LiveCharts";
import { sizeLimitName, statusLabels, strategyName } from "../labels";
import { useBot } from "../store";
import type { ArmedEntry, Decision, Opportunity } from "../types";

const strategies = [
  ["todas", "Todas"],
  ["trend", "Trend"],
  ["mean_reversion", "Mean Reversion"],
  ["momentum", "Momentum"],
  ["funding_carry", "Funding Carry"],
] as const;

/** Every size cap of the row in USD, smallest first, for the badge's tooltip. */
function capsText(row: Opportunity): string {
  const caps = Object.entries(row.size_caps ?? {}).sort(([, a], [, b]) => a - b);
  if (caps.length === 0) return "Sem os limites calculados nesta linha.";
  return caps.map(([name, value]) => `${sizeLimitName(name)}: ${money(value)}`).join("\n");
}

/** "sinal há 45 s" or "sinal há 3 min": time since the candle that produced the signal closed. */
function signalAge(seconds: number): string {
  return seconds < 120 ? `sinal há ${Math.round(seconds)} s` : `sinal há ${Math.round(seconds / 60)} min`;
}

export function Opportunities() {
  const rows = useBot((state) => state.rows);
  const perform = useBot((state) => state.perform);
  const busy = useBot((state) => state.busy);
  const [strategy, setStrategy] = useState("todas");
  const [selected, setSelected] = useState<Opportunity | null>(null);
  const [tradeId, setTradeId] = useState<number | null>(null);
  const [armed, setArmed] = useState<ArmedEntry[]>([]);
  const [trigger, setTrigger] = useState("");
  const [hours, setHours] = useState("24");

  async function refreshArmed() {
    try {
      setArmed((await api.armed()).rows);
    } catch {
      // The status bar already reports an unreachable API.
    }
  }

  useEffect(() => {
    void refreshArmed();
    const timer = window.setInterval(() => void refreshArmed(), 10000);
    return () => window.clearInterval(timer);
  }, []);

  async function arm(row: Opportunity) {
    const price = Number(trigger.replace(",", "."));
    await perform(`Armar ${row.symbol}`, () => api.arm(row.symbol, row.strategy, price, Number(hours)), (reply) =>
      [
        `Entrada armada: compra ${row.symbol} quando o preço ${reply.condition === "below" ? "cair a" : "subir a"} ${price}`,
        `até ${new Date(reply.expires_at).toLocaleString("pt-BR")}`,
        reply.cycle_started ? "ciclo iniciado" : "",
      ].filter(Boolean).join(" · "),
    );
    void refreshArmed();
  }

  async function execute(row: Opportunity) {
    const body = await perform(`Executar ${row.symbol}`, () => api.execute(row.symbol, row.strategy), (reply) => {
      const results: Decision[] = reply.results ?? [];
      const opened = results.find((item) => item.decision === "operar");
      const notes = results.filter((item) => item.decision !== "operar").map((item) => `${item.decision}: ${item.reason}`);
      if (reply.cycle_started) notes.push("ciclo iniciado para acompanhar o stop e as saídas");
      return [opened ? `Compra de ${row.symbol} executada, com stop na corretora` : `Nenhuma compra de ${row.symbol}`, ...notes].join("\n");
    });
    const started = (body?.results ?? []).find(
      (item) => (item.decision === "operar" || item.decision === "pendente") && item.position_id != null,
    );
    setTradeId(started?.position_id ?? null);
  }
  const visible = rows.filter((row) => strategy === "todas" || row.strategy === strategy);

  return (
    <section>
      <label className="mb-3 flex items-center text-sm">
        Estratégia
        <Help text="Escolhe qual jeito de operar aparece na lista. Todas junta as três. Trend segue o movimento, Mean Reversion aposta na volta à média e Funding Carry só mostra um sinal, sem executar." />
        <select className="ml-2 bg-transparent" value={strategy} onChange={(event) => setStrategy(event.target.value)}>
          {strategies.map(([value, label]) => (
            <option key={value} value={value}>
              {label}
            </option>
          ))}
        </select>
      </label>
      <MarketCharts strategy={strategy} />
      <table className="w-full text-left text-sm">
        <thead className="text-neutral-500">
          <tr>
            <th className="pr-3 font-medium">
              #<Help text="Posição na lista. O número 1 é a oportunidade com a maior nota agora." />
            </th>
            <th className="pr-3 font-medium">
              Par<Help text="As duas moedas da operação. BTC/USDT significa comprar ou vender bitcoin pagando em dólar digital." />
            </th>
            <th className="pr-3 font-medium">
              Estratégia<Help text="O método que gerou a sugestão: Trend, Mean Reversion ou Funding Carry." />
            </th>
            <th className="pr-3 font-medium">
              Ação<Help text="O que o robô sugere fazer nesta linha: comprar, vender ou sair." />
            </th>
            <th className="pr-3 font-medium">
              Score<Help text="Nota de 0 a 1. Quanto maior, mais a oportunidade compensa o risco e o custo. A barra repete o número para ler de relance." />
            </th>
            <th className="pr-3 font-medium">
              Retorno líquido<Help text="Ganho esperado já descontando taxa e derrapagem de preço. Passe o mouse no número para ver o valor antes desses custos." />
            </th>
            <th className="pr-3 font-medium">
              Risco<Help text="Quanto o preço pode andar contra você até o ponto em que a perda é cortada." />
            </th>
            <th className="pr-3 font-medium">
              Tamanho
              <Help text="Quanto o robô compraria, em dólares, e o limite que decidiu esse valor. O tamanho é o menor entre: o risco por trade dividido pela distância do stop (ou menos, pelo Kelly), o teto por ordem, o teto da corretora, a volatilidade diária do par e o espaço que sobra na exposição total. Passe o mouse no selo para ver todos os tetos." />
            </th>
            <th className="pr-3 font-medium">
              Acerto
              <Help text="Taxa de acerto estimada da estratégia: fração de trades que fecharam no lucro no teste histórico e nas posições reais, com a mesma regra de saída usada agora. É histórico, não uma garantia para esta oportunidade. Sem amostra suficiente aparece um traço." />
            </th>
            <th className="pr-3 font-medium">
              Status
              <Help text="Pronta pode ser enviada. Potencial ainda não passou no critério. Bloqueada foi barrada por um limite. Ignorada fica escondida por 24 horas. Em posição já está aberta." />
            </th>
            <th className="font-medium">
              Motivo<Help align="end" text="A explicação curta de por que o robô chegou nesse status. Os botões desta coluna agem só nesta linha." />
            </th>
          </tr>
        </thead>
        <tbody>
          {visible.map((row, index) => (
            <tr key={`${row.symbol}-${row.strategy}`} className="border-t border-neutral-200 dark:border-neutral-800">
              <td className="pr-3">{index + 1}</td>
              <td className="pr-3">
                {row.symbol}
                {row.cluster && (
                  <span
                    className="ml-1 whitespace-nowrap rounded bg-sky-100 px-1 text-xs text-sky-900 dark:bg-sky-900 dark:text-sky-100"
                    title={`Anda junto com ${row.cluster}: o risco somado deste grupo fica em no máximo metade do risco total.`}
                  >
                    cluster {row.cluster}
                  </span>
                )}
              </td>
              <td className="pr-3">{strategyName(row.strategy)}</td>
              <td className="pr-3">{row.action}</td>
              <td className="pr-3">
                <span className="mr-2 inline-block h-2 bg-neutral-800 align-middle dark:bg-neutral-200" style={{ width: `${Math.max(0, Math.min(1, row.score)) * 80}px` }} />
                {row.score.toFixed(2)}
              </td>
              <td
                className="pr-3"
                title={`Bruto ${formatPct(row.gross_return ?? row.net_return)}${row.slippage_est != null ? ` · derrapagem estimada ${formatPct(row.slippage_est)} na ida e volta` : ""}`}
              >
                <span className={row.net_return >= 0 ? "text-green-600" : "text-red-600"}>{formatPct(row.net_return)}</span>
              </td>
              <td className="pr-3 text-red-600">{formatPct(-Math.abs(row.risk_pct))}</td>
              <td className="pr-3 whitespace-nowrap">
                {row.size_usd ? money(row.size_usd) : "—"}
                {row.size_limit && (
                  <span
                    className={`ml-1 cursor-help rounded px-1 text-xs ${row.size_limit === "risk" ? "bg-neutral-200 text-neutral-800 dark:bg-neutral-700 dark:text-neutral-100" : "bg-amber-200 text-amber-900"}`}
                    title={capsText(row)}
                  >
                    {sizeLimitName(row.size_limit)}
                  </span>
                )}
              </td>
              <td className="pr-3" title={`n=${row.win_rate_n ?? 0} trades`}>
                {row.win_rate == null ? "—" : `${Math.round(row.win_rate * 100)}% (n=${row.win_rate_n ?? 0})`}
              </td>
              <td className="pr-3">
                {statusLabels[row.status] ?? row.status}
                {row.test_only && <span className="ml-1 rounded bg-amber-200 px-1 text-xs text-amber-900">teste</span>}
              </td>
              <td>
                {row.reason}
                {row.data_age_seconds != null && (
                  <span className="ml-2 text-neutral-500">idade {Math.round(row.data_age_seconds)}s</span>
                )}
                {row.signal_age_seconds != null && (
                  <span className="ml-2 text-neutral-500" title="Tempo desde o fechamento da vela que gerou o sinal. Acima do limite de idade, o trade automático não entra; a entrada manual mostra a idade e continua permitida.">
                    {signalAge(row.signal_age_seconds)}
                  </span>
                )}
                <button className="ml-2 underline" type="button" onClick={() => setSelected(row)}>
                  detalhe
                </button>
                <Help text="Abre o tamanho sugerido em dólares e o retorno líquido desta linha, sem enviar ordem." />
                {row.status === "pronta" && (
                  <>
                    <button
                      className="ml-2 underline"
                      type="button"
                      disabled={busy}
                      onClick={() => void execute(row)}
                    >
                      executar
                    </button>
                    <Help text="Envia esta ordem agora. Ela ainda passa pelo portão de segurança: se um limite barrar, nada é enviado à corretora." />
                  </>
                )}
                <button
                  className="ml-2 underline"
                  type="button"
                  disabled={busy}
                  onClick={() => void perform(`Ignorar ${row.symbol}`, () => api.ignore(row.symbol, row.strategy), () => `${row.symbol} ignorado por 24 h`)}
                >
                  ignorar 24h
                </button>
                <Help align="end" text="Esconde esta sugestão por um dia. Não cancela uma ordem que já tenha sido enviada." />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {visible.length === 0 && <p className="mt-4 text-sm text-neutral-500">Nenhuma oportunidade neste ambiente.</p>}
      {armed.length > 0 && (
        <div className="mt-6 text-sm">
          <h2 className="inline-flex items-center font-semibold">
            Entradas armadas
            <Help text="Compras que esperam o preço chegar ao gatilho. Disparada virou ordem; expirada passou do prazo." />
          </h2>
          <ul className="mt-1 space-y-1">
            {armed.map((item) => (
              <li key={item.id}>
                {item.symbol} · {strategyName(item.strategy)} · {item.condition === "below" ? "≤" : "≥"} {item.trigger_price} ·{" "}
                {{ armed: "armada", fired: "disparada", expired: "expirada", cancelled: "cancelada" }[item.status]}
                {item.status === "armed" && ` até ${new Date(item.expires_at).toLocaleString("pt-BR")}`}
                {item.reason && <span className="ml-2 text-neutral-500">{item.reason}</span>}
                {item.status === "armed" && (
                  <button
                    className="ml-2 underline"
                    type="button"
                    disabled={busy}
                    onClick={() => void perform(`Cancelar gatilho de ${item.symbol}`, () => api.disarm(item.id)).then(() => refreshArmed())}
                  >
                    cancelar
                  </button>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {tradeId != null && (
        <p className="mt-4 text-sm">
          <Link className="rounded bg-green-700 px-3 py-2 font-medium text-white" to={`/trade/${tradeId}`}>
            Ver o trade ao vivo no gráfico
          </Link>
        </p>
      )}
      {selected && (
        <aside className="mt-6 max-w-xl rounded border border-neutral-300 p-4 text-sm dark:border-neutral-700">
          <h2 className="font-semibold">
            {selected.symbol} · {strategyName(selected.strategy)} · {selected.action}
          </h2>
          <p className="mt-2">{selected.reason}</p>
          <p className="mt-2 inline-flex items-center" title={`Bruto ${formatPct(selected.gross_return ?? 0)} custo ${formatPct(selected.cost ?? 0)}`}>
            Líquido {formatPct(selected.net_return)} · tamanho proposto {selected.size_usd ?? 0} USD
            {selected.size_limit && <> · limitado por {sizeLimitName(selected.size_limit)}</>}
            <Help align="end" text="Líquido é o ganho esperado depois dos custos. O tamanho é quanto dinheiro, em dólares, o robô usaria nesta ordem se ela fosse enviada, e o limite que decidiu esse valor." />
          </p>
          {selected.size_caps && Object.keys(selected.size_caps).length > 0 && (
            <p className="mt-1 whitespace-pre-line text-neutral-500">Tetos do tamanho:{"\n"}{capsText(selected)}</p>
          )}
          <p className="mt-1 text-neutral-500">
            {selected.slippage_est != null && <>Derrapagem estimada {formatPct(selected.slippage_est)} na ida e volta, já no custo. </>}
            {selected.cluster && <>Grupo de correlação: {selected.cluster}. </>}
            {selected.signal_age_seconds != null && <>{signalAge(selected.signal_age_seconds)}. </>}
            {selected.timeframe && <>Tempo gráfico {selected.timeframe}.</>}
          </p>
          {selected.action.startsWith("Compr") && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              Armar por gatilho: comprar a
              <input className="w-28 border border-neutral-300 bg-transparent px-1 dark:border-neutral-700" inputMode="decimal" value={trigger} onChange={(event) => setTrigger(event.target.value)} />
              por
              <input className="w-14 border border-neutral-300 bg-transparent px-1 dark:border-neutral-700" inputMode="numeric" value={hours} onChange={(event) => setHours(event.target.value)} />
              horas
              <button className="underline disabled:opacity-50" type="button" disabled={busy || !(Number(trigger.replace(",", ".")) > 0)} onClick={() => void arm(selected)}>
                armar
              </button>
              <Help align="end" text="Como o Start Condition da Binance. Abaixo do preço atual espera um recuo, acima espera um rompimento. Quando o preço chega lá, o robô compra pelas mesmas regras da entrada manual, e só se esta linha ainda estiver Pronta. Nada fica na corretora antes disso." />
            </div>
          )}
          <button className="mt-3 block underline" type="button" onClick={() => setSelected(null)}>
            fechar
          </button>
        </aside>
      )}
    </section>
  );
}
