import { useEffect, useRef } from "react";
import * as echarts from "echarts";

import { Help } from "../Help";
import { useBot } from "../store";

export function Performance() {
  const days = useBot((state) => state.days);
  const calibration = useBot((state) => state.calibration);
  const dark = useBot((state) => state.dark);
  const node = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!node.current) return;
    const chart = echarts.init(node.current);
    let equity = 0;
    const series = days.map((day) => {
      equity += day.net_pnl;
      return equity;
    });
    const text = dark ? "#a3a3a3" : "#525252";
    const line = dark ? "#404040" : "#d4d4d4";
    chart.setOption({
      backgroundColor: "transparent",
      tooltip: { trigger: "axis" },
      xAxis: { type: "category", data: days.map((day) => day.day), axisLabel: { color: text }, axisLine: { lineStyle: { color: line } } },
      yAxis: { type: "value", name: "USD líquido", axisLabel: { color: text }, splitLine: { lineStyle: { color: line } }, nameTextStyle: { color: text } },
      series: [{ type: "line", data: series, showSymbol: false, lineStyle: { color: "#38bdf8" } }],
    });
    const onResize = () => chart.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      chart.dispose();
    };
  }, [days, dark]);

  const csv = ["day,net_pnl,gross_pnl,fees,slippage,funding,trades", ...days.map((day) =>
    [day.day, day.net_pnl, day.gross_pnl, day.fees, day.slippage, day.funding, day.trades].join(","),
  )].join("\n");

  return (
    <section>
      <p className="mb-2 flex items-center text-sm text-neutral-500">
        Resultado acumulado
        <Help text="Cada ponto soma o resultado líquido dos dias anteriores. A linha sobe quando o robô ganhou e desce quando perdeu, já sem taxa, derrapagem e funding." />
      </p>
      <div ref={node} className="h-80 w-full" />
      <p className="mt-4 flex items-center text-sm">
        <a className="underline" href={`data:text/csv,${encodeURIComponent(csv)}`} download="pnl.csv">
          Exportar CSV
        </a>
        <Help text="Baixa uma planilha com o resultado de cada dia: líquido, bruto, taxas, derrapagem, funding e quantidade de operações. Abre no Excel." />
      </p>
      <p className="mt-8 mb-2 flex items-center text-sm font-medium">
        Calibração: acerto estimado × realizado
        <Help text="Para cada faixa de taxa de acerto estimada na entrada, quantas posições fecharam e quantas deram lucro. Se o realizado fica bem abaixo do estimado, a estimativa está otimista e o modo automático deve ter um mínimo maior. Não entram as linhas de teste." />
      </p>
      {calibration.length === 0 ? (
        <p className="text-sm text-neutral-500">Ainda não há posições fechadas com taxa de acerto estimada.</p>
      ) : (
        <table className="text-left text-sm">
          <thead className="text-neutral-500">
            <tr>
              <th className="pr-6 font-medium">Faixa estimada</th>
              <th className="pr-6 font-medium">Trades</th>
              <th className="pr-6 font-medium">Estimado (média)</th>
              <th className="font-medium">Realizado</th>
            </tr>
          </thead>
          <tbody>
            {calibration.map((row) => (
              <tr key={row.bucket} className="border-t border-neutral-200 dark:border-neutral-800">
                <td className="pr-6">{row.bucket}</td>
                <td className="pr-6">{row.trades}</td>
                <td className="pr-6">{(row.predicted * 100).toFixed(0)}%</td>
                <td className={row.realized + 0.1 < row.predicted ? "text-red-600" : ""}>{(row.realized * 100).toFixed(0)}%</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
