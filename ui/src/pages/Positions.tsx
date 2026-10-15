import { useState } from "react";

import { api } from "../api";
import { Help } from "../Help";
import { PositionChart } from "../LiveCharts";
import { sideName, strategyName } from "../labels";
import { useBot } from "../store";

export function Positions() {
  const positions = useBot((state) => state.positions);
  const load = useBot((state) => state.load);
  const [symbol, setSymbol] = useState<string | null>(null);
  const current = positions.find((row) => row.symbol === symbol) ?? positions[0];
  return (
    <section>
      {current && (
        <PositionChart symbol={current.symbol} entry={current.avg_price} stop={current.stop_price} target={current.target_price} />
      )}
      <table className="w-full text-left text-sm">
        <thead className="text-neutral-500">
          <tr>
            <th className="pr-3 font-medium">
              Par<Help text="A moeda que está aberta contra USDT." />
            </th>
            <th className="pr-3 font-medium">
              Estratégia<Help text="Qual método abriu esta posição: Trend, Mean Reversion ou Funding Carry." />
            </th>
            <th className="pr-3 font-medium">
              Lado<Help text="Compra significa que você ganha se o preço subir. Venda significa que você ganha se o preço cair." />
            </th>
            <th className="pr-3 font-medium">
              Tamanho<Help text="Quantidade da moeda nesta posição, não o valor em dólares." />
            </th>
            <th className="pr-3 font-medium">
              Preço médio<Help text="Preço médio pelo qual a posição foi aberta." />
            </th>
            <th className="pr-3 font-medium">
              Stop<Help text="Preço em que a perda é cortada. Se o mercado chegar aqui, a posição deve ser encerrada." />
            </th>
            <th className="pr-3 font-medium">
              Alvo<Help text="Preço em que o robô pretende realizar o ganho." />
            </th>
            <th className="pr-3 font-medium">
              Desde<Help text="Data e hora em que a posição foi aberta." />
            </th>
            <th className="font-medium">
              <Help align="end" text="Fechar pede para sair desta posição a preço de mercado. A ordem ainda passa pelo portão de segurança." />
            </th>
          </tr>
        </thead>
        <tbody>
          {positions.map((row) => (
            <tr
              key={`${row.symbol}-${row.strategy}`}
              className="cursor-pointer border-t border-neutral-200 dark:border-neutral-800"
              onClick={() => setSymbol(row.symbol)}
            >
              <td className="pr-3">{row.symbol}</td>
              <td className="pr-3">{strategyName(row.strategy)}</td>
              <td className={`pr-3 ${row.side === "buy" || row.side === "long" ? "text-green-600" : "text-red-600"}`}>{sideName(row.side)}</td>
              <td className="pr-3">{row.qty}</td>
              <td className="pr-3">{row.avg_price}</td>
              <td className="pr-3">{row.stop_price ?? "—"}</td>
              <td className="pr-3">{row.target_price ?? "—"}</td>
              <td className="pr-3">{row.opened_at}</td>
              <td>
                <button className="underline" type="button" onClick={() => void api.close(row.symbol, row.strategy).then(() => load())}>
                  fechar
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {positions.length === 0 && <p className="mt-4 text-sm text-neutral-500">Nenhuma posição aberta.</p>}
    </section>
  );
}
