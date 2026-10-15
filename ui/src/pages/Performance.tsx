import { useEffect, useRef } from "react";
import * as echarts from "echarts";

import { Help } from "../Help";
import { useBot } from "../store";

export function Performance() {
  const days = useBot((state) => state.days);
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
    </section>
  );
}
