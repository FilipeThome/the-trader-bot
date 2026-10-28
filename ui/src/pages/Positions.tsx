import { useState } from "react";
import { Link } from "react-router";

import { api } from "../api";
import { Help } from "../Help";
import { PositionChart } from "../LiveCharts";
import { sideName, strategyName } from "../labels";
import { formatR } from "../numbers";
import { useBot } from "../store";

/** "sai em 2h 15min" until a day trade's time limit, or "no prazo" once it passed. */
function timeLeft(deadline: string): string {
  const minutes = Math.round((new Date(deadline).getTime() - Date.now()) / 60000);
  if (minutes <= 0) return "no prazo: sai no próximo ciclo";
  return `sai em ${Math.floor(minutes / 60)}h ${minutes % 60}min`;
}

export function Positions() {
  const positions = useBot((state) => state.positions);
  const perform = useBot((state) => state.perform);
  const busy = useBot((state) => state.busy);
  const [symbol, setSymbol] = useState<string | null>(null);
  function close(positionId: number, symbol: string) {
    return perform(`Fechar ${symbol}`, async () => {
      const body = await api.close(positionId);
      // The API answers 200 with the exit's own status; anything but ok is a failed close.
      if (body.result.status !== "ok") throw new Error(body.result.error ?? "a saída não foi concluída");
      return body;
    }, () => `${symbol} vendido e stop cancelado`);
  }
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
              Estratégia<Help text="A estratégia que abriu esta posição." />
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
              Stop<Help text="Preço do stop que fica na Binance. Se o mercado chegar aqui, a corretora vende mesmo com o robô desligado." />
            </th>
            <th className="pr-3 font-medium">
              Alvo<Help text="Preço em que o robô pretende realizar o ganho. No day trade é o TP1: metade da posição é vendida ali e o stop do resto sobe para o preço de entrada mais as taxas." />
            </th>
            <th className="pr-3 font-medium">
              R agora
              <Help text="Quanto o preço andou desde a entrada, em unidades do risco inicial (preço médio menos o stop da entrada): +1 R é ganhar o que se arriscava, −1 R é a perda no stop. Usa o fechamento de 15 min mais recente. Embaixo, o pior (MAE) e o melhor (MFE) ponto desde a entrada. Traço em posições antigas e de robôs, que não têm risco planejado." />
            </th>
            <th className="pr-3 font-medium">
              Tempo<Help text="O tempo gráfico do setup que abriu a posição. As saídas da estratégia leem velas fechadas desse tempo." />
            </th>
            <th className="pr-3 font-medium">
              Desde<Help text="Data e hora em que a posição foi aberta." />
            </th>
            <th className="font-medium">
              <Help align="end" text="Fechar cancela o stop na corretora e vende a quantidade da posição com ordens limitadas imediatas. As ordens ainda passam pelo portão de segurança." />
            </th>
          </tr>
        </thead>
        <tbody>
          {positions.map((row) => (
            <tr
              key={row.id}
              className="cursor-pointer border-t border-neutral-200 dark:border-neutral-800"
              onClick={() => setSymbol(row.symbol)}
            >
              <td className="pr-3">{row.symbol}</td>
              <td className="pr-3">{strategyName(row.strategy)}</td>
              <td className={`pr-3 ${row.side === "buy" || row.side === "long" ? "text-green-600" : "text-red-600"}`}>{sideName(row.side)}</td>
              <td className="pr-3">{row.qty}</td>
              <td className="pr-3">{row.avg_price}</td>
              <td className="pr-3">{row.stop_price ?? "—"}</td>
              <td className="pr-3">
                {row.exit_policy === "day" ? (row.tp1_done ? "TP1 feito · stop no equilíbrio" : `TP1 ${row.target_price ?? "—"}`) : row.target_price ?? "—"}
              </td>
              <td className="pr-3 whitespace-nowrap">
                <span className={(row.r_now ?? 0) >= 0 ? "text-green-600" : "text-red-600"}>{formatR(row.r_now)}</span>
                {(row.mae_r != null || row.mfe_r != null) && (
                  <span className="block text-xs text-neutral-500">
                    MAE {formatR(row.mae_r)} · MFE {formatR(row.mfe_r)}
                  </span>
                )}
              </td>
              <td className="pr-3">{row.timeframe ?? "—"}</td>
              <td className="pr-3">
                {row.opened_at}
                {row.exit_policy === "day" && row.exit_deadline && <span className="block text-neutral-500">{timeLeft(row.exit_deadline)}</span>}
              </td>
              <td>
                <Link className="mr-3 underline" to={`/trade/${row.id}`} onClick={(event) => event.stopPropagation()}>
                  ao vivo
                </Link>
                <button className="underline disabled:opacity-50" type="button" disabled={busy} onClick={(event) => {
                    event.stopPropagation();
                    void close(row.id, row.symbol);
                  }}>
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
