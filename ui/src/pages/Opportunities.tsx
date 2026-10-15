import { useState } from "react";

import { api } from "../api";
import { formatPct } from "../App";
import { Help } from "../Help";
import { MarketCharts } from "../LiveCharts";
import { statusLabels, strategyName } from "../labels";
import { useBot } from "../store";
import type { Opportunity } from "../types";

const strategies = [
  ["todas", "Todas"],
  ["trend", "Trend"],
  ["mean_reversion", "Mean Reversion"],
  ["funding_carry", "Funding Carry"],
] as const;

export function Opportunities() {
  const rows = useBot((state) => state.rows);
  const load = useBot((state) => state.load);
  const [strategy, setStrategy] = useState("todas");
  const [selected, setSelected] = useState<Opportunity | null>(null);
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
              <td className="pr-3">{row.symbol}</td>
              <td className="pr-3">{strategyName(row.strategy)}</td>
              <td className="pr-3">{row.action}</td>
              <td className="pr-3">
                <span className="mr-2 inline-block h-2 bg-neutral-800 align-middle dark:bg-neutral-200" style={{ width: `${Math.max(0, Math.min(1, row.score)) * 80}px` }} />
                {row.score.toFixed(2)}
              </td>
              <td className="pr-3" title={`Bruto ${formatPct(row.gross_return ?? row.net_return)}`}>
                <span className={row.net_return >= 0 ? "text-green-600" : "text-red-600"}>{formatPct(row.net_return)}</span>
              </td>
              <td className="pr-3 text-red-600">{formatPct(-Math.abs(row.risk_pct))}</td>
              <td className="pr-3">{statusLabels[row.status] ?? row.status}</td>
              <td>
                {row.reason}
                {row.data_age_seconds != null && (
                  <span className="ml-2 text-neutral-500">idade {Math.round(row.data_age_seconds)}s</span>
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
                      onClick={() => void api.execute(row.symbol, row.strategy).then(() => load())}
                    >
                      executar
                    </button>
                    <Help text="Envia esta ordem agora. Ela ainda passa pelo portão de segurança: se um limite barrar, nada é enviado à corretora." />
                  </>
                )}
                <button
                  className="ml-2 underline"
                  type="button"
                  onClick={() => void api.ignore(row.symbol, row.strategy).then(() => load())}
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
      {selected && (
        <aside className="mt-6 max-w-xl rounded border border-neutral-300 p-4 text-sm dark:border-neutral-700">
          <h2 className="font-semibold">
            {selected.symbol} · {strategyName(selected.strategy)} · {selected.action}
          </h2>
          <p className="mt-2">{selected.reason}</p>
          <p className="mt-2 inline-flex items-center" title={`Bruto ${formatPct(selected.gross_return ?? 0)} custo ${formatPct(selected.cost ?? 0)}`}>
            Líquido {formatPct(selected.net_return)} · tamanho proposto {selected.size_usd ?? 0} USD
            <Help align="end" text="Líquido é o ganho esperado depois dos custos. O tamanho é quanto dinheiro, em dólares, o robô usaria nesta ordem se ela fosse enviada." />
          </p>
          <button className="mt-3 block underline" type="button" onClick={() => setSelected(null)}>
            fechar
          </button>
        </aside>
      )}
    </section>
  );
}
