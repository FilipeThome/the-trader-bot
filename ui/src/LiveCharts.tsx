import { useEffect, useRef, useState } from "react";
import * as echarts from "echarts";
import type { ECharts, EChartsOption } from "echarts";

import { formatPct } from "./App";
import { api } from "./api";
import { Help } from "./Help";
import { statusLabels, strategyName } from "./labels";
import { useBot } from "./store";
import type { Features, Market, PriceBlock, Recommendation, Trade } from "./types";

const PAIRS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"];

export function useFeatures(symbol: string) {
  const [data, setData] = useState<Features | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancel = false;
    async function pull() {
      try {
        const body = await api.features(symbol);
        if (!cancel) {
          setData(body);
          setError("");
        }
      } catch (reason) {
        if (!cancel) setError(reason instanceof Error ? reason.message : "mercado indisponível");
      }
    }
    void pull();
    const timer = window.setInterval(() => void pull(), 10000);
    return () => {
      cancel = true;
      window.clearInterval(timer);
    };
  }, [symbol]);

  return { data, error };
}

export function MarketCharts({ strategy }: { strategy: string }) {
  const [symbol, setSymbol] = useState("BTC/USDT");
  const { data, error } = useFeatures(symbol);
  const showTrend = strategy === "todas" || strategy === "trend";
  const showMean = strategy === "todas" || strategy === "mean_reversion";
  const showFunding = strategy === "todas" || strategy === "funding_carry";

  return (
    <div className="mb-6 space-y-4">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <PairMenu value={symbol} onChange={setSymbol} />
        <Help text="Lista todos os pares spot que a Binance deixa negociar agora. Abra e digite o nome, por exemplo SOL ou ETH/BTC. O gráfico acompanha a escolha e atualiza a cada dez segundos." />
        {data && (
          <span className="text-neutral-500">
            ao vivo · {new Date(data.updated_at).toLocaleTimeString("pt-BR")}
          </span>
        )}
      </div>
      <TopPicks selected={symbol} onSelect={setSymbol} />
      {error && <p className="text-sm text-red-600">{error}</p>}
      {data && showTrend && <TrendChart data={data} />}
      {data && showMean && <MeanChart data={data} />}
      {data && showFunding && <FundingChart data={data} />}
    </div>
  );
}

export function PositionChart({
  symbol,
  entry,
  stop,
  target,
}: {
  symbol: string;
  entry: number;
  stop: number | null;
  target: number | null;
}) {
  const { data, error } = useFeatures(symbol);
  const option = data ? positionOption(data.mean_reversion, entry, stop, target, useBot.getState().dark) : null;
  const node = useChart(option, useBot((state) => state.dark));
  return (
    <section className="mb-6">
      <p className="mb-2 flex items-center text-sm text-neutral-500">
        {symbol} · 15 minutos
        <Help text="Preço recente desta posição. A linha cinza é a entrada, a vermelha é o stop e a verde é o alvo. Atualiza a cada dez segundos." />
      </p>
      {error && <p className="text-sm text-red-600">{error}</p>}
      <div ref={node} className="h-72 w-full" />
    </section>
  );
}

/** A trade on the candles of its venue, with entry, stop, target or trailing peak, and entry and exit marks. */
export function TradeChart({ trade }: { trade: Trade }) {
  const dark = useBot((state) => state.dark);
  const { position } = trade;
  const block: PriceBlock = {
    interval: trade.interval,
    t: trade.candles.map((candle) => candle.t),
    candles: trade.candles.map((candle) => [candle.o, candle.c, candle.l, candle.h]),
  };
  const trailing = position.exit_policy === "trailing" || position.exit_policy === "trailing_tp";
  const day = position.exit_policy === "day";
  // After the first target the rest has no target: its stop sits at break-even.
  const target = trailing || (day && position.tp1_done) ? null : position.target_price;
  const option = trade.candles.length
    ? positionOption(block, position.avg_price || null, position.stop_price, target, dark, {
        targetLabel: day ? "TP1" : "Alvo",
        deadline: day ? position.exit_deadline : null,
        peak: trailing ? position.peak_price : null,
        entryAt: position.avg_price ? position.opened_at : null,
        exitAt: position.closed_at,
        exitPrice: position.exit_price,
      })
    : null;
  const node = useChart(option, dark);
  return (
    <section>
      <p className="mb-2 flex items-center text-neutral-500">
        {position.symbol} · velas de {trade.interval} na corretora deste ambiente
        <Help text="Velas do mercado onde a ordem foi executada (no testnet, o livro do Spot Testnet). Cinza: entrada. Vermelha: stop atual na Binance; com o stop móvel ela sobe junto com o maior preço (azul). Verde: alvo; no day trade é o TP1, onde metade é vendida e o stop do resto vai para o ponto de equilíbrio. Laranja: o prazo do day trade. Os marcadores mostram onde a posição entrou e saiu." />
      </p>
      {!trade.candles.length && <p className="text-neutral-500">Sem velas para este período.</p>}
      <div ref={node} className="h-96 w-full" />
    </section>
  );
}

function TrendChart({ data }: { data: Features }) {
  const dark = useBot((state) => state.dark);
  const node = useChart(trendOption(data, dark), dark);
  return (
    <section>
      <p className="mb-1 flex items-center text-sm text-neutral-500">
        Trend · 1 hora
        <Help text="Velas do preço, a média de 50 períodos e a máxima e a mínima das últimas 20 velas. Embaixo, o volume contra a média e a força do movimento. Acima de 25 a força conta como tendência." />
      </p>
      <div ref={node} className="h-96 w-full" />
    </section>
  );
}

function MeanChart({ data }: { data: Features }) {
  const dark = useBot((state) => state.dark);
  const node = useChart(meanOption(data, dark), dark);
  return (
    <section>
      <p className="mb-1 flex items-center text-sm text-neutral-500">
        Mean Reversion · 15 minutos
        <Help text="Velas com a média e as bandas. Embaixo, o RSI: abaixo de 30 o preço está esticado para baixo, acima de 70 esticado para cima." />
      </p>
      <div ref={node} className="h-96 w-full" />
    </section>
  );
}

function FundingChart({ data }: { data: Features }) {
  const dark = useBot((state) => state.dark);
  const node = useChart(data.funding_carry.points.length ? fundingOption(data, dark) : null, dark);
  return (
    <section>
      <p className="mb-1 flex items-center text-sm text-neutral-500">
        Funding Carry · 8 horas
        <Help text="Custo anual de segurar o contrato futuro. A linha tracejada é o limiar. Nesta fase o robô só mostra o sinal, não executa." />
      </p>
      {data.funding_carry.points.length === 0 ? (
        <p className="text-sm text-neutral-500">Este par não tem funding na Binance.</p>
      ) : (
        <div ref={node} className="h-56 w-full" />
      )}
    </section>
  );
}

function TopPicks({ selected, onSelect }: { selected: string; onSelect: (symbol: string) => void }) {
  const [rows, setRows] = useState<Recommendation[]>([]);
  const [phase, setPhase] = useState<"loading" | "ready" | "error">("loading");

  useEffect(() => {
    let cancel = false;
    async function pull() {
      try {
        const body = await api.recommendations();
        if (!cancel) {
          setRows(body.rows);
          setPhase("ready");
        }
      } catch {
        if (!cancel) setPhase("error");
      }
    }
    void pull();
    const timer = window.setInterval(() => void pull(), 60000);
    return () => {
      cancel = true;
      window.clearInterval(timer);
    };
  }, []);

  return (
    <section>
      <p className="mb-2 flex items-center text-sm text-neutral-500">
        Top 5 mais promissores
        <Help text="Até cinco pares líquidos. Primeiro entram os setups do robô, com score e retorno líquido. Se faltarem cinco, entram altas do dia com volume: essas linhas não têm score nem estratégia e não são sinal de compra. Par em queda não entra. Clicar abre o gráfico." />
      </p>
      {phase === "loading" && <p className="text-sm text-neutral-500">Calculando o top 5…</p>}
      {phase === "error" && <p className="text-sm text-red-600">Não foi possível calcular os pares mais promissores.</p>}
      {phase === "ready" && rows.length === 0 && (
        <p className="text-sm text-neutral-500">Nenhum par líquido com retorno positivo neste momento. Tente de novo em um minuto.</p>
      )}
      {rows.length > 0 && (
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
          {rows.map((row, index) => (
            <button
              key={row.symbol}
              type="button"
              className={`rounded border px-3 py-2 text-left text-sm ${row.symbol === selected ? "border-neutral-900 dark:border-neutral-100" : "border-neutral-300 dark:border-neutral-700"}`}
              onClick={() => onSelect(row.symbol)}
            >
              <span className="block font-semibold">
                {index + 1}. {row.symbol}
              </span>
              {row.kind === "setup" ? (
                <>
                  <span className="block text-neutral-500">
                    {strategyName(row.strategy)} · score {row.score.toFixed(2)}
                  </span>
                  <span className={`block ${row.net_return >= 0 ? "text-green-600" : "text-red-600"}`}>{formatPct(row.net_return)} líquido</span>
                </>
              ) : (
                <>
                  <span className="block text-neutral-500">movimento do dia · sem setup</span>
                  <span className="block text-green-600">alta de {formatPct(row.change)}</span>
                </>
              )}
              <span className="block text-neutral-500">{statusLabels[row.status] ?? row.status}</span>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}

function PairMenu({ value, onChange }: { value: string; onChange: (symbol: string) => void }) {
  const status = useBot((state) => state.status);
  const root = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [rows, setRows] = useState<Market[]>([]);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancel = false;
    api
      .markets()
      .then((body) => {
        if (!cancel) setRows(body.symbols);
      })
      .catch(() => {
        if (!cancel) setFailed(true);
      });
    return () => {
      cancel = true;
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    function onPointer(event: MouseEvent) {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onPointer);
    return () => document.removeEventListener("mousedown", onPointer);
  }, [open]);

  const fallback = (status?.symbols?.length ? status.symbols : PAIRS).map((symbol) => {
    const [base, quote] = symbol.split("/");
    return { symbol, base, quote };
  });
  const source = rows.length ? rows : fallback;
  const needle = query.trim().toUpperCase();
  const visible = needle ? source.filter((row) => row.symbol.includes(needle) || row.base.includes(needle)) : source;

  return (
    <div ref={root} className="relative">
      <button
        type="button"
        className="rounded border border-neutral-300 px-2 py-1 dark:border-neutral-700"
        aria-expanded={open}
        aria-haspopup="listbox"
        onClick={() => setOpen((current) => !current)}
      >
        {value} ▾
      </button>
      {open && (
        <div className="absolute z-40 mt-1 w-64 rounded border border-neutral-300 bg-white p-2 shadow-lg dark:border-neutral-700 dark:bg-neutral-900">
          <input
            className="mb-2 w-full border bg-transparent px-2 py-1"
            placeholder="Buscar par"
            aria-label="Buscar par"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <ul className="max-h-64 overflow-auto" role="listbox" aria-label="Pares da Binance">
            {visible.map((row) => (
              <li key={row.symbol}>
                <button
                  type="button"
                  role="option"
                  aria-selected={row.symbol === value}
                  className={`block w-full px-2 py-1 text-left ${row.symbol === value ? "font-semibold" : ""} hover:bg-neutral-100 dark:hover:bg-neutral-800`}
                  onClick={() => {
                    onChange(row.symbol);
                    setOpen(false);
                    setQuery("");
                  }}
                >
                  {row.symbol}
                </button>
              </li>
            ))}
          </ul>
          {failed && <p className="mt-2 text-xs text-red-600">Não foi possível ler a lista da Binance.</p>}
          {!failed && <p className="mt-2 text-xs text-neutral-500">{source.length} pares negociáveis. Digite para filtrar.</p>}
        </div>
      )}
    </div>
  );
}

function useChart(option: EChartsOption | null, dark: boolean) {
  const node = useRef<HTMLDivElement>(null);
  const chart = useRef<ECharts | null>(null);

  useEffect(() => {
    if (!node.current) return;
    const instance = echarts.init(node.current);
    chart.current = instance;
    const onResize = () => instance.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      instance.dispose();
      chart.current = null;
    };
  }, [dark]);

  useEffect(() => {
    const instance = chart.current;
    if (!option || !instance) return;
    // A refresh replaces the data but keeps the zoom the user chose.
    const zoom = (instance.getOption()?.dataZoom as { start?: number; end?: number }[] | undefined)?.[0];
    instance.setOption(option, true);
    if (zoom?.start != null && zoom.end != null && (zoom.start > 0 || zoom.end < 100)) {
      instance.dispatchAction({ type: "dataZoom", start: zoom.start, end: zoom.end });
    }
  }, [option, dark]);

  return node;
}

function colors(dark: boolean) {
  return {
    text: dark ? "#a3a3a3" : "#525252",
    line: dark ? "#404040" : "#d4d4d4",
    tip: dark ? "#171717" : "#ffffff",
  };
}

function axis(dark: boolean) {
  const tone = colors(dark);
  return {
    axisLabel: { color: tone.text, fontSize: 11 },
    axisLine: { lineStyle: { color: tone.line } },
    splitLine: { lineStyle: { color: tone.line } },
  };
}

function base(dark: boolean): EChartsOption {
  const tone = colors(dark);
  return {
    backgroundColor: "transparent",
    textStyle: { color: tone.text },
    tooltip: { trigger: "axis", backgroundColor: tone.tip, borderWidth: 0, textStyle: { color: tone.text } },
    legend: { textStyle: { color: tone.text }, top: 0 },
  };
}

function stamps(times: string[]): string[] {
  return times.map((value) =>
    new Date(value).toLocaleString("pt-BR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }),
  );
}

function candleStyle() {
  return {
    type: "candlestick" as const,
    itemStyle: { color: "#16a34a", color0: "#dc2626", borderColor: "#16a34a", borderColor0: "#dc2626" },
  };
}

function trendOption(data: Features, dark: boolean): EChartsOption {
  const block = data.trend;
  const labels = stamps(block.t);
  const tone = axis(dark);
  return {
    ...base(dark),
    dataZoom: [{ type: "inside", xAxisIndex: [0, 1, 2] }],
    grid: [
      { left: 72, right: 16, top: 32, height: "48%" },
      { left: 72, right: 16, top: "68%", height: "10%" },
      { left: 72, right: 16, top: "84%", height: "10%" },
    ],
    xAxis: [0, 1, 2].map((gridIndex) => ({
      type: "category" as const,
      data: labels,
      gridIndex,
      axisLabel: { ...tone.axisLabel, show: gridIndex === 2 },
      axisLine: tone.axisLine,
    })),
    yAxis: [
      { scale: true, gridIndex: 0, ...tone },
      {
        scale: true,
        gridIndex: 1,
        splitNumber: 2,
        ...tone,
        axisLabel: {
          ...tone.axisLabel,
          formatter: (value: number) => (value >= 1_000_000 ? `${(value / 1_000_000).toFixed(1)}M` : value >= 1_000 ? `${Math.round(value / 1_000)}k` : String(value)),
        },
      },
      { scale: true, gridIndex: 2, splitNumber: 2, ...tone },
    ],
    series: [
      { ...candleStyle(), name: "Preço", data: block.candles, xAxisIndex: 0, yAxisIndex: 0 },
      line("Média 50", block.ema50, "#38bdf8"),
      line("Máxima 20", block.breakout_high, "#f59e0b"),
      line("Mínima 20", block.breakout_low, "#a78bfa"),
      { type: "bar", name: "Volume", data: block.volume, xAxisIndex: 1, yAxisIndex: 1, itemStyle: { color: "#737373" } },
      line("Volume médio", block.volume_avg, "#e5e5e5", 1),
      {
        ...line("Força", block.adx, "#fb7185", 2),
        markLine: { symbol: "none", data: [{ yAxis: 25, label: { formatter: "25" } }], lineStyle: { color: "#fb7185" } },
      },
    ],
  };
}

function meanOption(data: Features, dark: boolean): EChartsOption {
  const block = data.mean_reversion;
  const labels = stamps(block.t);
  const tone = axis(dark);
  return {
    ...base(dark),
    dataZoom: [{ type: "inside", xAxisIndex: [0, 1] }],
    grid: [
      { left: 72, right: 16, top: 32, height: "55%" },
      { left: 72, right: 16, top: "76%", height: "16%" },
    ],
    xAxis: [0, 1].map((gridIndex) => ({
      type: "category" as const,
      data: labels,
      gridIndex,
      axisLabel: { ...tone.axisLabel, show: gridIndex === 1 },
      axisLine: tone.axisLine,
    })),
    yAxis: [
      { scale: true, gridIndex: 0, ...tone },
      { min: 0, max: 100, gridIndex: 1, ...tone },
    ],
    series: [
      { ...candleStyle(), name: "Preço", data: block.candles, xAxisIndex: 0, yAxisIndex: 0 },
      line("Média", block.mid, "#38bdf8"),
      line("Banda superior", block.upper, "#f59e0b"),
      line("Banda inferior", block.lower, "#a78bfa"),
      {
        ...line("RSI", block.rsi, "#34d399", 1),
        markLine: {
          symbol: "none",
          data: [
            { yAxis: 30, label: { formatter: "30" } },
            { yAxis: 70, label: { formatter: "70" } },
          ],
          lineStyle: { color: "#34d399" },
        },
      },
    ],
  };
}

function fundingOption(data: Features, dark: boolean): EChartsOption {
  const tone = axis(dark);
  const points = data.funding_carry.points;
  const labels = stamps(points.map((point) => point.t));
  const values = points.map((point) => Number((point.v * 100).toFixed(2)));
  const limit = Number((data.funding_carry.threshold * 100).toFixed(2));
  return {
    ...base(dark),
    grid: { left: 72, right: 16, top: 32, bottom: 28 },
    xAxis: { type: "category", data: labels, ...tone },
    yAxis: { type: "value", name: "% ao ano", scale: true, ...tone, nameTextStyle: { color: colors(dark).text } },
    series: [
      {
        type: "line",
        name: "Funding",
        data: values,
        showSymbol: true,
        itemStyle: { color: "#38bdf8" },
        markLine: {
          symbol: "none",
          data: [{ yAxis: limit, label: { formatter: `limiar ${limit}%` } }],
          lineStyle: { type: "dashed", color: "#f59e0b" },
        },
      },
    ],
  };
}

type TradeMarks = {
  peak?: number | null;
  entryAt?: string | null;
  exitAt?: string | null;
  exitPrice?: number | null;
  /** Label of the target line: "TP1" for a day trade's first target. */
  targetLabel?: string;
  /** A day trade's time limit, drawn as a vertical line. */
  deadline?: string | null;
};

function positionOption(
  block: PriceBlock,
  entry: number | null,
  stop: number | null,
  target: number | null,
  dark: boolean,
  extra: TradeMarks = {},
): EChartsOption {
  const labels = stamps(block.t);
  const tone = axis(dark);
  const marks: { yAxis: number; name: string; lineStyle: { color: string }; label: { formatter: string } }[] = [];
  if (entry != null) marks.push({ yAxis: entry, name: "Entrada", lineStyle: { color: "#a3a3a3" }, label: { formatter: "Entrada" } });
  if (stop != null) marks.push({ yAxis: stop, name: "Stop", lineStyle: { color: "#dc2626" }, label: { formatter: "Stop" } });
  const targetLabel = extra.targetLabel ?? "Alvo";
  if (target != null) marks.push({ yAxis: target, name: targetLabel, lineStyle: { color: "#16a34a" }, label: { formatter: targetLabel } });
  if (extra.peak != null) marks.push({ yAxis: extra.peak, name: "Topo", lineStyle: { color: "#2563eb" }, label: { formatter: "Topo" } });
  const points: { name: string; coord: [string, number]; value: string; itemStyle: { color: string } }[] = [];
  const entryAt = snap(block.t, extra.entryAt);
  if (entryAt >= 0 && entry != null) points.push({ name: "Entrada", coord: [labels[entryAt], entry], value: "E", itemStyle: { color: "#16a34a" } });
  const deadlineAt = snap(block.t, extra.deadline);
  const verticals = deadlineAt >= 0 && deadlineAt < labels.length - 1
    ? [{ xAxis: labels[deadlineAt], name: "Prazo", lineStyle: { color: "#f59e0b" }, label: { formatter: "Prazo" } }]
    : [];
  const exitAt = snap(block.t, extra.exitAt);
  if (exitAt >= 0 && extra.exitPrice != null) points.push({ name: "Saída", coord: [labels[exitAt], extra.exitPrice], value: "S", itemStyle: { color: "#dc2626" } });
  return {
    ...base(dark),
    dataZoom: [{ type: "inside" }],
    grid: { left: 72, right: 16, top: 16, bottom: 28 },
    xAxis: { type: "category", data: labels, ...tone },
    yAxis: { scale: true, ...tone },
    series: [
      {
        ...candleStyle(),
        name: "Preço",
        data: block.candles,
        markLine: { symbol: "none", data: [...marks, ...verticals] },
        markPoint: { data: points },
      },
    ],
  };
}

/** Index of the candle that contains ``moment``: the last one that opened at or before it, or -1. */
function snap(times: string[], moment: string | null | undefined): number {
  if (!moment) return -1;
  const at = new Date(moment).getTime();
  let found = -1;
  times.forEach((value, index) => {
    if (new Date(value).getTime() <= at) found = index;
  });
  return found;
}

function line(name: string, data: Array<number | null>, color: string, axisIndex = 0) {
  return {
    type: "line" as const,
    name,
    data,
    showSymbol: false,
    xAxisIndex: axisIndex,
    yAxisIndex: axisIndex,
    itemStyle: { color },
    lineStyle: { width: 1.4, color },
  };
}
