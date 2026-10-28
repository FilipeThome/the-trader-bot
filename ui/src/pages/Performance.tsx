import { type ReactNode, useEffect, useRef } from "react";
import * as echarts from "echarts";

import { Help } from "../Help";
import { stopWindowKinds, strategyName } from "../labels";
import { formatR, formatUsdCost } from "../numbers";
import { useBot } from "../store";
import type { AdviceCalibrationLine, AdvisorUsage, AdvisorUsageTotals, Status, TimeframeResult } from "../types";

/** The deflated Sharpe the gate needs (runner.MIN_DSR). */
const MIN_DSR = 0.95;

function pct(value: number | null | undefined, digits = 1): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(digits).replace(".", ",")}%`;
}

function num(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value.toFixed(digits).replace(".", ",");
}

function ms(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return value >= 1000 ? `${num(value / 1000, 1)} s` : `${Math.round(value)} ms`;
}

function day(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleDateString("pt-BR");
}

export function Performance() {
  const days = useBot((state) => state.days);
  const calibration = useBot((state) => state.calibration);
  const pairs = useBot((state) => state.pairs);
  const adviceCalibration = useBot((state) => state.adviceCalibration);
  const advisorUsage = useBot((state) => state.advisorUsage);
  const status = useBot((state) => state.status);
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
  const budget = status?.limits.slippage_budget_pct;

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

      {(status?.advisor || advisorUsage) && <AdvisorReport lines={adviceCalibration} usage={advisorUsage} />}

      <div className="mt-8 grid gap-6 text-sm lg:grid-cols-2">
        <StopWindows status={status} />
        <div>
          <p className="mb-2 flex items-center font-medium">
            Derrapagem por par (30 dias)
            <Help text={`Diferença entre o preço de referência e o executado, somando entrada e saída, na média dos últimos 20 trades fechados do par. Acima do orçamento de ${pct(budget, 2)}, o par fica bloqueado para entradas novas até a média voltar ou os trades saírem da janela de 30 dias. As posições abertas seguem com stop e saídas.`} />
          </p>
          {pairs.length === 0 ? (
            <p className="text-neutral-500">Nenhum par analisado.</p>
          ) : (
            <table className="text-left">
              <thead className="text-neutral-500">
                <tr>
                  <th className="pr-4 font-medium">Par</th>
                  <th className="pr-4 font-medium">Trades</th>
                  <th className="pr-4 font-medium">Ida e volta</th>
                  <th className="font-medium">Situação</th>
                </tr>
              </thead>
              <tbody>
                {pairs.map((row) => (
                  <tr key={row.symbol} className="border-t border-neutral-200 dark:border-neutral-800">
                    <td className="pr-4">{row.symbol}</td>
                    <td className="pr-4">{row.trades}</td>
                    <td className={`pr-4 ${budget != null && row.slippage_rt != null && budget > 0 && row.slippage_rt > budget ? "text-red-600" : ""}`}>
                      {pct(row.slippage_rt, 2)}
                    </td>
                    <td className={row.blocked === false ? "text-neutral-500" : row.blocked ? "font-medium text-red-600" : "font-medium text-amber-600"}>
                      {row.blocked === false
                        ? "liberado"
                        : `${row.blocked ? "bloqueado" : "desconhecido"}${row.blocked_reason ? `: ${row.blocked_reason}` : ""}`}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>

      <Validation status={status} />
    </section>
  );
}

/** "+0,12 R ± 0,05": a mean counterfactual R and its standard error. */
function meanWithError(mean: number | null, se: number | null): string {
  if (mean == null) return "—";
  return se == null ? formatR(mean) : `${formatR(mean)} ± ${num(se)}`;
}

/** "12 confirmar · 3 reduzir · 2 vetar · 1 sem opinião": the verdicts of a usage range. */
function verdictMix(totals: AdvisorUsageTotals): string {
  const parts: [number, string][] = [
    [totals.confirm, "confirmar"],
    [totals.reduce, "reduzir"],
    [totals.veto, "vetar"],
    [totals.abstain, "sem opinião"],
  ];
  const shown = parts.filter(([count]) => count > 0).map(([count, label]) => `${count} ${label}`);
  return shown.length ? shown.join(" · ") : "—";
}

/** The advisor's verdicts measured by counterfactual R, per model and prompt version, and its calls and cost. */
function AdvisorReport({ lines, usage }: { lines: AdviceCalibrationLine[]; usage: AdvisorUsage | null }) {
  const groups = new Map<string, AdviceCalibrationLine[]>();
  for (const line of lines) {
    const key = `${line.model} · prompt ${line.prompt_version}`;
    groups.set(key, [...(groups.get(key) ?? []), line]);
  }
  return (
    <div className="mt-8 text-sm">
      <p className="mb-2 flex items-center font-medium">
        Consultor DeepSeek: calibração dos vereditos
        <Help text="Cada veredito gravado, tenha entrado ou não, recebe o R que a posição teria feito entrando no fechamento da vela do sinal, com o stop e o alvo da linha, pelas regras do teste histórico e já sem o custo. Um veto útil tem R contrafactual médio negativo: as linhas desaconselhadas teriam perdido. Um confirmar útil fica acima da linha de todas as consultadas. Uma linha só entra na média depois que as 48 velas da janela passaram desde o sinal, qualquer que seja o resultado; até lá conta em Aguardando. Uma linha cuja janela passou sem as velas para medi-la (o par saiu da lista e a leitura do histórico falhou) expira: fica fora das médias e aparece em Aguardando como expirada. EP é o erro padrão: uma diferença menor que duas vezes ele pode ser sorte. Os vetos com confiança acima do mínimo têm linha própria: são o que o modo filtro tiraria." />
      </p>
      {groups.size === 0 ? (
        <p className="text-neutral-500">Nenhum veredito registrado neste ambiente.</p>
      ) : (
        [...groups.entries()].map(([key, members]) => (
          <div key={key} className="mb-4 overflow-x-auto">
            <p className="mb-1 text-neutral-500">{key}</p>
            <table className="text-left">
              <thead className="text-neutral-500">
                <tr>
                  <th className="pr-6 font-medium">Veredito</th>
                  <th className="pr-6 font-medium">n</th>
                  <th className="pr-6 font-medium">R contrafactual (± EP)</th>
                  <th className="pr-6 font-medium">
                    Reduzir: (1 − fator) × R
                    <Help text="Quanto o tamanho menor mudou o resultado, em R da posição cheia. Negativo é perda evitada; positivo é ganho que ficou na mesa." />
                  </th>
                  <th className="pr-6 font-medium">
                    R realizado
                    <Help text="O R das posições que estas linhas abriram e já fecharam, com o tamanho e as saídas reais. Fica à parte: misturar com o contrafactual escolheria as linhas pelo resultado." />
                  </th>
                  <th className="font-medium">Aguardando</th>
                </tr>
              </thead>
              <tbody>
                {members.map((line) => {
                  const vetoLine = line.group === "veto" || line.group === "veto_confident";
                  const tone = !vetoLine || line.mean_r == null ? "" : line.mean_r < 0 ? "text-green-600" : "text-red-600";
                  return (
                    <tr key={line.group} className={`border-t border-neutral-200 dark:border-neutral-800 ${line.group === "all" ? "font-medium" : ""}`}>
                      <td className="pr-6">{line.label}</td>
                      <td className="pr-6">{line.n}</td>
                      <td className={`pr-6 ${tone}`}>{meanWithError(line.mean_r, line.se)}</td>
                      <td className="pr-6">{line.group === "reduce" ? formatR(line.reduced_mean_r) : "—"}</td>
                      <td className="pr-6">{line.realized_n > 0 ? `${formatR(line.realized_mean_r)} (n=${line.realized_n})` : "—"}</td>
                      <td>
                        {line.waiting}
                        {line.expired ? ` (+${line.expired} expiradas)` : ""}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ))
      )}
      <p className="mb-2 mt-4 flex items-center font-medium">
        Consultor DeepSeek: uso
        <Help text="Consultas, custo e vereditos por dia UTC. Uma consulta por sinal Pronto, não por ciclo. O custo é uma estimativa pela tabela de preços do código; o valor faturado é o do painel da DeepSeek. Erros são consultas sem veredito: chave, limite de concorrência, demora, resposta vazia ou inválida." />
      </p>
      {!usage ? (
        <p className="text-neutral-500">Uso indisponível.</p>
      ) : (
        <>
          <table className="text-left">
            <thead className="text-neutral-500">
              <tr>
                <th className="pr-6 font-medium" />
                <th className="pr-6 font-medium">Consultas</th>
                <th className="pr-6 font-medium">Custo</th>
                <th className="pr-6 font-medium">Vereditos</th>
                <th className="font-medium">Erros</th>
              </tr>
            </thead>
            <tbody>
              {([["Hoje", usage.today], ["30 dias", usage.last_30_days]] as [string, AdvisorUsageTotals][]).map(([label, totals]) => (
                <tr key={label} className="border-t border-neutral-200 dark:border-neutral-800">
                  <td className="pr-6 text-neutral-500">{label}</td>
                  <td className="pr-6">{totals.calls}</td>
                  <td className="pr-6">{formatUsdCost(totals.cost_usd)}</td>
                  <td className="pr-6">{verdictMix(totals)}</td>
                  <td className={totals.errors > 0 ? "text-amber-600" : ""}>{totals.errors}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-1 text-xs text-neutral-500">Custo estimado com os preços de {usage.prices_as_of}.</p>
        </>
      )}
    </div>
  );
}

function StopWindows({ status }: { status: Status | null }) {
  const stats = status?.stop_windows;
  const kinds = Object.entries(stats?.by_kind ?? {});
  return (
    <div>
      <p className="mb-2 flex items-center font-medium">
        Janela sem stop (30 dias)
        <Help text="Para trocar um stop (stop móvel, TP1 do day trade, venda parcial, novo stop depois de uma compra), o robô cancela o antigo e põe o novo. Entre os dois, a posição fica sem stop na corretora. Aqui está quanto durou essa janela: média, p95 (95% das trocas foram mais rápidas) e a pior." />
      </p>
      {!stats || stats.count === 0 ? (
        <p className="text-neutral-500">Nenhuma troca de stop nos últimos 30 dias.</p>
      ) : (
        <>
          <p>
            {stats.count} trocas · média {ms(stats.mean_ms)} · p95 {ms(stats.p95_ms)} · pior{" "}
            <span className={(stats.max_ms ?? 0) > 2000 ? "text-red-600" : ""}>{ms(stats.max_ms)}</span>
          </p>
          {kinds.length > 0 && (
            <table className="mt-2 text-left">
              <thead className="text-neutral-500">
                <tr>
                  <th className="pr-4 font-medium">Tipo</th>
                  <th className="pr-4 font-medium">Trocas</th>
                  <th className="pr-4 font-medium">Média</th>
                  <th className="font-medium">Pior</th>
                </tr>
              </thead>
              <tbody>
                {kinds.map(([kind, row]) => (
                  <tr key={kind} className="border-t border-neutral-200 dark:border-neutral-800">
                    <td className="pr-4">{stopWindowKinds[kind] ?? kind}</td>
                    <td className="pr-4">{row.count}</td>
                    <td className="pr-4">{ms(row.mean_ms)}</td>
                    <td>{ms(row.max_ms)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  );
}

function Validation({ status }: { status: Status | null }) {
  const detail = status?.validation_detail ?? {};
  const ids = Object.keys(detail).filter((id) => Object.keys(detail[id] ?? {}).length > 0);
  return (
    <div className="mt-8 text-sm">
      <p className="mb-1 flex items-center font-medium">
        Validação no histórico
        <Help text="O teste que libera cada estratégia, por tempo gráfico. Aprova com: pelo menos 30 trades fora do hold-out, Sharpe acima de 1, queda menor que 20%, 3 de 4 janelas do walk-forward no lucro com eficiência (WFE) de pelo menos 0,5, hold-out final com pelo menos 5 trades, lucro e ganho por dia de pelo menos metade do da amostra, e Sharpe deflacionado (DSR) de pelo menos 0,95. O DSR desconta quantas combinações foram testadas desde a última aprovação: quanto mais tentativas, mais exigente. O teste aplica os limites da carteira (posições, pausa do par, trava de perdas) com que rodou; mudar um limite não pede validar de novo. Só o tempo gráfico aprovado opera." />
      </p>
      <p className="mb-3 text-neutral-500">PBO não calculado: sem otimização de parâmetros.</p>
      {ids.length === 0 && <p className="text-neutral-500">Nenhuma validação registrada neste ambiente.</p>}
      {ids.map((id) => (
        <StrategyValidation
          key={id}
          id={id}
          results={detail[id]}
          validated={Boolean(status?.validated?.[id])}
          trials={status?.trials?.[id]}
          trialsN={status?.trials_n?.[id]}
          riskPerTrade={status?.limits.risk_per_trade_pct}
          maxDrawdown={status?.limits.max_drawdown_pct}
        />
      ))}
    </div>
  );
}

function StrategyValidation({
  id,
  results,
  validated,
  trials,
  trialsN,
  riskPerTrade,
  maxDrawdown,
}: {
  id: string;
  results: Record<string, TimeframeResult>;
  validated: boolean;
  /** Recorded runs since the last pass; ``trialsN`` is the N the deflated Sharpe uses. */
  trials?: number;
  trialsN?: number;
  riskPerTrade?: number;
  maxDrawdown?: number;
}) {
  const timeframes = Object.keys(results);
  const rows: [string, string, (result: TimeframeResult) => ReactNode][] = [
    ["Resultado", "Aprovado opera; reprovado fica só como sinal. O motivo vem do teste.", (r) => (
      <span className={r.passed ? "text-green-600" : "text-red-600"}>
        {r.passed ? "aprovado" : "reprovado"}
        {!r.passed && r.reason ? <span className="block text-xs text-neutral-500">{r.reason}</span> : null}
      </span>
    )],
    ["Trades", "Trades julgados pelo teste (fora do hold-out) de todos os replayados, e os dias de histórico.", (r) =>
      `${r.gate_trades ?? "—"} de ${r.trades ?? "—"} · ${r.days ?? "—"} dias`],
    ["Sharpe (bruto)", "O Sharpe do teste, sem desconto de tentativas. Precisa passar de 1.", (r) => num(r.sharpe)],
    ["Sharpe diário", "Sharpe dos retornos por dia, com os dias sem trade. É o número comparado com o real para rebaixar uma estratégia que piorou.", (r) => num(r.daily_sharpe)],
    ["Sharpe deflacionado", `Probabilidade de o Sharpe por trade ser maior que o de sorte, descontadas as tentativas. Aprova a partir de ${num(MIN_DSR)}.`, (r) => (
      <span className={(r.dsr ?? 0) >= MIN_DSR ? "text-green-600" : "text-red-600"}>
        {num(r.dsr)} <span className="text-neutral-500">(N={r.trials ?? 1})</span>
      </span>
    )],
    ["Walk-forward", "O histórico dividido em 4 janelas pela data de saída, com o resultado líquido e os trades de cada uma. Precisa de 3 no lucro. WFE é a eficiência: o ritmo de cada janela comparado ao das anteriores.", (r) => (
      <span>
        {(r.windows ?? []).map((window, index) => (
          <span key={index} className={`mr-2 ${window.profitable ? "text-green-600" : "text-red-600"}`} title={`${day(window.start)} a ${day(window.end)}`}>
            {pct(window.net)} ({window.trades})
          </span>
        ))}
        <span className="block text-xs text-neutral-500">WFE {num(r.wfe)}</span>
      </span>
    )],
    ["Hold-out", "Os últimos 20% do histórico, guardados fora do teste. Precisa de pelo menos 5 trades, lucro e ganho por dia de pelo menos metade do ganho por dia da amostra.", (r) =>
      r.holdout ? (
        <span className={r.holdout.passed ? "text-green-600" : "text-red-600"}>
          {pct(r.holdout.net)} em {r.holdout.trades} trades <span className="text-neutral-500">desde {day(r.holdout.start)}</span>
        </span>
      ) : "—"],
    ["Monte Carlo", "1000 sorteios da ordem dos trades: a queda p95 em R, vezes o risco por trade de hoje, comparada ao drawdown máximo. Acima do limite, as linhas ficam só como sinal, sem precisar validar de novo. Sem a queda p95 registrada, também ficam só como sinal até uma nova validação.", (r) => {
      const p95 = r.mc_p95_dd_r;
      // The runtime keeps a row without a readable p95 as signal only (risk.mc_drawdown_ok): never green.
      if (typeof p95 !== "number" || !Number.isFinite(p95)) return <span className="text-red-600">ilegível — valide de novo</span>;
      const loss = riskPerTrade != null ? p95 * riskPerTrade : null;
      return (
        <span className={r.mc_ok === false ? "text-red-600" : r.mc_ok ? "text-green-600" : ""}>
          {num(p95, 1)} R × {pct(riskPerTrade)} = {pct(loss)} <span className="text-neutral-500">(limite {pct(maxDrawdown, 0)})</span>
        </span>
      );
    }],
    ["Filtro de entrada", "Os trades que o robô aceitaria ao vivo (nota, custo e ganho/risco com os limiares usados no teste), com a taxa de acerto, comparados a todos os trades do teste que os limites da carteira deixaram entrar. Com amostra suficiente e os mesmos limiares de hoje, a menor das duas taxas vale no piso de acerto e no Kelly.", (r) =>
      r.filtered_trades == null ? "—" : (
        <span>
          {r.filtered_trades} de {r.trades ?? "—"} · acerto {pct(r.filtered_trades ? (r.filtered_wins ?? 0) / r.filtered_trades : null, 0)}
          {" "}(todos {pct(r.trades ? (r.wins ?? 0) / r.trades : null, 0)})
          <span className="block text-xs text-neutral-500">
            Sharpe diário {num(r.filtered_sharpe)} · nota {num(r.score_threshold)} · custo {num(r.cost_multiple, 1)}x · ganho/risco {num(r.min_reward_risk, 1)}
          </span>
        </span>
      )],
    ["Limites da carteira", "Os limites com que o teste rodou: vagas, um trade por par, pausa do par depois de perda e trava de perdas. Os trades recusados por eles saem do teste. Risco total, cluster do BTC, correlação, caixa e vagas de outras estratégias não entram: o resultado é o melhor caso.", (r) =>
      r.limits && r.limits.max_positions != null ? (
        <span>
          {r.skipped_by_limits ?? 0} trades pulados
          <span className="block text-xs text-neutral-500">
            {r.limits.max_positions} posições · pausa do par {r.limits.pair_cooldown_minutes} min · trava {r.limits.loss_guard_trades} perdas ou {num(r.limits.loss_guard_r, 1)} R em 6 h
          </span>
        </span>
      ) : "—"],
    ["Fator de lucro", "Ganhos brutos divididos pelas perdas brutas. Acima de 1 ganha mais do que perde.", (r) => num(r.profit_factor)],
    ["Expectativa", "Resultado médio por trade em unidades de risco: +0,2 R ganha em média um quinto do que arrisca.", (r) => formatR(r.expectancy_r)],
    ["Queda máxima", "A maior queda do teste, por unidade investida. Precisa ficar abaixo de 20%.", (r) => pct(r.max_drawdown)],
  ];
  const liquid = Object.values(results).find((result) => result.universe === "liquid");
  return (
    <div className="mb-6 overflow-x-auto rounded border border-neutral-300 p-3 dark:border-neutral-700">
      {liquid && (
        <p className="mb-2 text-xs text-neutral-500">
          Seleção automática: o teste rodou nos {liquid.universe_pairs ?? 40} pares mais líquidos por volume, não só nos escolhidos.
          A escolha por volume no fim do período ainda carrega algum viés e não vê pares deslistados.
        </p>
      )}
      <p className="mb-2 font-medium">
        {strategyName(id)} · {validated ? <span className="text-green-600">opera</span> : <span className="text-neutral-500">só sinal</span>}
        <span className="ml-2 inline-flex items-center font-normal text-neutral-500">
          {[
            trials != null ? `${trials} execuções registradas desde a última aprovação` : "",
            trialsN != null ? `N do Sharpe deflacionado: ${trialsN}` : "",
          ].filter(Boolean).join(" · ")}
          {(trials != null || trialsN != null) && (
            <Help text="Cada combinação nova (timeframe, regra, pares, filtro, histerese) conta como uma tentativa. Repetir a mesma combinação registra outra execução, mas não aumenta o N." />
          )}
        </span>
      </p>
      <table className="text-left">
        <thead className="text-neutral-500">
          <tr>
            <th className="pr-4 font-medium" />
            {timeframes.map((timeframe) => (
              <th key={timeframe} className="pr-4 font-medium">
                {timeframe}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, help, cell]) => (
            <tr key={label} className="border-t border-neutral-200 align-top dark:border-neutral-800">
              <td className="pr-4 whitespace-nowrap text-neutral-500">
                {label}
                <Help text={help} />
              </td>
              {timeframes.map((timeframe) => (
                <td key={timeframe} className="pr-4">
                  {cell(results[timeframe])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
