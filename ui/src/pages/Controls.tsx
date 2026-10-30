import { useState } from "react";

import { api } from "../api";
import { Help } from "../Help";
import { advisorModels, advisorModes, riskLevels, strategyName } from "../labels";
import { formatUsdCost, parseDecimal, parseWhole } from "../numbers";
import { useBot } from "../store";
import type { MandateSummary, Status, ValidationJob } from "../types";

const strategyHelp: Record<string, string> = {
  trend: "Trend segue o movimento quando o preço rompe a máxima ou a mínima recente. A ideia é acompanhar a tendência, não adivinhar a virada. Só opera no tempo gráfico (1h ou 4h) que passar no teste histórico.",
  momentum: "Momentum compra o que vem subindo de forma constante: alta nos últimos 20 dias em velas de 4h, média de 20 acima da de 50 e tendência firme (ADX). Seguir a tendência só na compra é o efeito com mais evidência em cripto. O stop fica a 2,5 vezes a volatilidade e a posição sai pelo stop móvel ou quando a média de 20 cruza abaixo da de 50. Só opera depois que o teste histórico (480 dias de velas de 4h) libera.",
  mean_reversion: "Mean Reversion procura preço que se afastou demais da média e pode voltar. Funciona melhor quando o mercado está de lado. Só opera no tempo gráfico (15 min ou 1h) que passar no teste histórico.",
  funding_carry: "Funding Carry compara o custo de segurar um contrato futuro com o preço à vista. Nesta fase o robô só mostra o sinal: ele não envia essa operação.",
  intraday_momentum: "Day trade. Compra no 15 min quando o preço rompe a máxima das últimas 8 velas com volume 1,5x acima do normal, acima da VWAP do dia, e só com o 1h em alta. Stop de 1,5 ATR (no mínimo 1%). Vende metade em 1,5x o risco e leva o stop do resto para o preço de entrada mais as taxas; o que sobrar sai em até 6 horas. Não entra entre 03:00 e 07:00 UTC nem perto dos eventos do calendário macro. Só opera depois do teste histórico (120 dias).",
  vwap_pullback: "Day trade. Compra no 15 min quando o preço recua abaixo da VWAP do dia ou da média de 20 e volta para cima, com RSI entre 35 e 55, e só com o 1h em alta. Mesmas regras de saída do momento intradiário: metade em 1,5x o risco, stop no equilíbrio, saída em até 6 horas. Também pausa perto dos eventos do calendário macro. Só opera depois do teste histórico (120 dias).",
};

export function Controls() {
  const status = useBot((state) => state.status);
  const perform = useBot((state) => state.perform);
  const busy = useBot((state) => state.busy);
  const [capital, setCapital] = useState("");
  const [confirmation, setConfirmation] = useState("");
  // Production pilot asked from the panel: its cap, and which gates of the switch it waives.
  const [pilotCap, setPilotCap] = useState("");
  const [pilotHistory, setPilotHistory] = useState(false);
  const [pilotValidation, setPilotValidation] = useState(false);
  const [pilotGates, setPilotGates] = useState(false);
  const [winRate, setWinRate] = useState("");
  const [tradeRisk, setTradeRisk] = useState("");
  const [openRisk, setOpenRisk] = useState("");
  const [pickCount, setPickCount] = useState("");
  const [minProfit, setMinProfit] = useState("");
  const [takeProfit, setTakeProfit] = useState("");
  const [stopLoss, setStopLoss] = useState("");
  // The result of a pair preset, shown next to its buttons: the top notice is out of sight there.
  const [symbolsNote, setSymbolsNote] = useState("");

  if (!status) return <p>Carregando controles…</p>;
  const autoTrading = status.entry_mode === "auto" && status.running;
  // One parser for every field: "10.000" is refused as ambiguous instead of read as 10.
  const capitalValue = parseDecimal(capital);
  const capitalValid = capitalValue != null && capitalValue > 0;
  const winRatePct = parseDecimal(winRate);
  const winRateValue = (winRatePct ?? 0) / 100;
  const winRateValid = winRatePct != null && winRateValue >= 0.5 && winRateValue <= 0.95;
  const tradeRiskPct = parseDecimal(tradeRisk);
  const openRiskPct = parseDecimal(openRisk);
  const tradeRiskValue = (tradeRiskPct ?? 0) / 100;
  const openRiskValue = (openRiskPct ?? 0) / 100;
  const pickCountValue = parseWhole(pickCount);
  const pickCountValid = pickCountValue != null && pickCountValue >= 5 && pickCountValue <= 20;
  const minProfitValue = parseDecimal(minProfit);
  const minProfitValid = minProfitValue != null && minProfitValue <= 1000;
  const fixedOn = status.take_profit_pct != null && status.stop_loss_pct != null;
  const takeProfitPct = parseDecimal(takeProfit) ?? (takeProfit.trim() === "" && fixedOn ? (status.take_profit_pct ?? 0) * 100 : null);
  const stopLossPct = parseDecimal(stopLoss) ?? (stopLoss.trim() === "" && fixedOn ? (status.stop_loss_pct ?? 0) * 100 : null);
  // Same bounds as bot.strategies.fixed: a target under 1% never clears the cost filter.
  const fixedValid = takeProfitPct != null && takeProfitPct >= 1 && takeProfitPct <= 100 && stopLossPct != null && stopLossPct >= 0.5 && stopLossPct <= 50;
  // Same bounds as the API's pilot_cap_usd; the production capital must fit the cap.
  const pilotCapValue = parseDecimal(pilotCap);
  const pilotCapValid = pilotCapValue != null && pilotCapValue > 0 && pilotCapValue <= 50;
  const productionCapital = status.production_capital_usd ?? status.allocated_capital_usd * 0.1;
  const pilotFits = pilotCapValid && productionCapital <= (pilotCapValue ?? 0) + 1e-9;
  const pilotMaxAllocated =
    pilotCapValid && productionCapital > 0 ? (status.allocated_capital_usd * (pilotCapValue ?? 0)) / productionCapital : null;
  const riskPayload: Record<string, number> = {};
  if (tradeRisk.trim() !== "") riskPayload.risk_per_trade_pct = tradeRiskValue;
  if (openRisk.trim() !== "") riskPayload.max_open_risk_pct = openRiskValue;
  const riskValid =
    Object.keys(riskPayload).length > 0 &&
    (tradeRisk.trim() === "" || (tradeRiskPct != null && tradeRiskValue > 0 && tradeRiskValue <= 0.02)) &&
    (openRisk.trim() === "" || (openRiskPct != null && openRiskValue > 0 && openRiskValue <= 0.2));

  function run<T>(label: string, action: () => Promise<T>, describe?: (result: T) => string) {
    return perform(label, action, describe);
  }
  const symbolsResult = (body: { symbols: string[]; left_out: string[] }) => {
    const unchanged = body.symbols.join(",") === status.symbols.join(",");
    const text =
      (unchanged ? "Nada mudou: a lista já era esta. " : "") +
      `Pares analisados: ${body.symbols.join(", ")}` +
      (body.left_out.length
        ? `\nFora, por não serem negociados no Spot Testnet:${body.left_out.join(", ")}`
        : "");
    setSymbolsNote(text);
    return text;
  };

  return (
    <section className="max-w-xl text-sm">
      <fieldset disabled={busy} className="space-y-4 disabled:opacity-70">
      {status.journal?.blocked && (
        <p className="text-red-600">
          O journal de execução bloqueia toda ordem nova, inclusive stops e saídas ({status.journal.error ?? `${status.journal.blocking.length} pendência(s)`}). Rode <code>python -m bot.ops status</code> e siga a seção Recuperação do README.
        </p>
      )}
      <RiskBanner status={status} />
      <p className="flex items-center">
        <span>
          Ambiente atual: <strong>{status.mode === "production" ? "Produção" : "Spot Testnet"}</strong>. Capital em uso{" "}
          {status.capital_usd} de {status.allocated_capital_usd} USD.
          {status.pilot_cap_usd != null && (
            <strong className="ml-1 text-amber-600">
              {" "}
              Produção piloto: capital limitado a US$ {status.pilot_cap_usd}, sem os 30 dias de testnet.
              {status.pilot_skip_validation && " As estratégias ligadas operam sem validação no histórico."}
              {status.pilot_skip_gates && " Trocado sem as liberações G1 a G4."}
            </strong>
          )}
        </span>
        <Help text="Spot Testnet é o ensaio, sem a conta real. Produção usa dinheiro de verdade e, nessa hora, o robô arrisca só 10% do capital que você alocou. Capital em uso é o que entra na conta do tamanho de cada ordem." />
      </p>
      <p className="flex items-center gap-2">
        <button
          className={`rounded px-3 py-2 font-medium text-white ${autoTrading ? "bg-green-700" : "bg-neutral-500"}`}
          type="button"
          aria-pressed={autoTrading}
          onClick={() =>
            void run(autoTrading ? "Desligando o trade automático" : "Ligando o trade automático", async () => {
              if (autoTrading) {
                await api.updateConfig({ entry_mode: "manual" });
                return;
              }
              if (status.entry_mode !== "auto") await api.updateConfig({ entry_mode: "auto" });
              await api.start();
            })
          }
        >
          Trade automático: {autoTrading ? "ligado" : status.entry_mode === "auto" ? "pausado" : "desligado"}
        </button>
        <Help text={status.mode === "testnet" && status.testnet_test_mode
          ? "Ligado, o robô compra sozinho as melhores oportunidades Prontas, da maior nota para a menor, dentro do saldo e dos limites de risco, e cuida do stop e da saída. No Spot Testnet com o modo teste, ele também opera as linhas de teste para você ver o fluxo inteiro. Desligar volta ao manual: as posições abertas continuam com stop e saídas."
          : "Ligado, o robô compra sozinho as melhores oportunidades Prontas, da maior nota para a menor, dentro do saldo e dos limites de risco, e cuida do stop e da saída. Só entram estratégias validadas no histórico com taxa de acerto acima do mínimo (no day trade, acima do ponto de equilíbrio do TP1 mais uma margem) e sinais mais novos que a idade máxima. Desligar volta ao manual: as posições abertas continuam com stop e saídas."} />
      </p>
      <div className="flex flex-wrap gap-2">
        <span className="inline-flex items-center">
          <button className="rounded bg-neutral-900 px-3 py-2 text-white dark:bg-neutral-100 dark:text-neutral-900" type="button" onClick={() => void run("Iniciar o ciclo", api.start)}>
            Iniciar
          </button>
          <Help text="Liga o ciclo. Cerca de uma vez por minuto o robô lê o mercado, dá nota às oportunidades e só envia ordem se ela estiver Pronta e dentro dos limites." />
        </span>
        <span className="inline-flex items-center">
          <button className="rounded border px-3 py-2" type="button" onClick={() => void run("Pausar o ciclo", api.stop)}>
            Pausar
          </button>
          <Help text="Desliga o ciclo. Não fecha posições e deixa os stops na corretora. Cancela antes as compras limitadas paradas dos robôs; se um cancelamento não se confirma, a pausa é recusada e o ciclo fica como estava; se uma posição ficar sem stop, o robô entra em halt." />
        </span>
        <span className="inline-flex items-center">
          <button className="rounded bg-red-600 px-3 py-2 text-white" type="button" onClick={() => void run("Parar tudo", api.halt)}>
            Parar tudo
          </button>
          <Help text="Parada de emergência. Para o ciclo e não volta sozinho. Com zerar no halt marcado no mandato, vende as posições antes de travar. Sem isso, as posições ficam abertas com o stop na Binance." />
        </span>
        <span className="inline-flex items-center">
          <button className="rounded border px-3 py-2" type="button" onClick={() => void run("Retomar após halt", api.resume, () => "Halt retirado. Clique em Iniciar para voltar a analisar.")}>
            Retomar após halt
          </button>
          <Help align="end" text="Tira o alerta da parada de emergência. O robô continua parado: para voltar a analisar, clique em Iniciar." />
        </span>
      </div>
      <label className="block">
        <span className="inline-flex items-center">
          Capital alocado
          <Help text={`Quanto dinheiro, em dólares, você coloca à disposição do robô. O tamanho de cada ordem sai do risco por trade dividido pela distância do stop, até o teto por ordem de ${pctText(status.limits.max_order_notional_pct)} desse valor (ou o do mandato, se for menor).`} />
        </span>
        <input className="mt-1 block w-full bg-transparent border px-2 py-1" value={capital} onChange={(event) => setCapital(event.target.value)} />
      </label>
      <p className="flex items-center">
        <button
          className="underline disabled:opacity-50"
          type="button"
          disabled={!capitalValid}
          onClick={() => void run("Salvar capital", () => api.updateConfig({ allocated_capital_usd: capitalValue ?? 0, reconfirm_mandate: true }), () => `Capital de US$ ${capitalValue} salvo e mandato confirmado`)}
        >
          Salvar capital{capitalValid ? ` de US$ ${capitalValue}` : ""} e confirmar mandato
        </button>
        <Help text="Grava o valor digitado e registra que você autoriza o robô a operar cripto neste ambiente. Sem esse registro, a conta real não liga." />
      </p>
      <MandateCard status={status} />
      <div className="flex flex-wrap gap-x-4 gap-y-2">
        {Object.entries(status.strategies).map(([name, enabled]) => (
          <span key={name} className="inline-flex items-center">
            <label>
              <input
                type="checkbox"
                checked={enabled}
                onChange={(event) => {
                  const on = event.target.checked;
                  const day = name === "intraday_momentum" || name === "vwap_pullback";
                  void run(
                    `${strategyName(name)} ${on ? "ligada" : "desligada"}`,
                    () => api.updateConfig({ strategies: { [name]: on } }),
                    () => (on && day ? `${strategyName(name)} ligada. Um day trade usa até 5 ordens: aplique o preset de day trade para reconfirmar o mandato.` : `${strategyName(name)} ${on ? "ligada" : "desligada"}`),
                  );
                }}
              />{" "}
              {strategyName(name)}
            </label>
            <Help text={strategyHelp[name] ?? "Liga ou desliga este método. Desligado, ele não gera oportunidade."} />
            {name !== "funding_carry" && (
              <ValidationControl
                job={status.validations?.[name]}
                validated={Boolean(status.validated?.[name])}
                onStart={() => void run(`Validar ${strategyName(name)}`, () => api.validate(name), () => `Validação de ${strategyName(name)} iniciada; o progresso aparece ao lado`)}
              />
            )}
          </span>
        ))}
      </div>
      <div className="rounded border border-neutral-300 p-3 dark:border-neutral-700">
        <p className="mb-2 flex items-center font-medium">
          Day trade
          <Help text="Liga as duas estratégias de day trade, desliga as de swing e grava os limites usados por traders de intraday: risco de 0,5% por trade, perda diária máxima de 2%, até 8 trades por dia, pausa de 60 min no par depois de uma perda, alvo de pelo menos 3x o custo. O mandato é confirmado de novo, porque um day trade usa mais ordens por dia. Você vê o que muda antes de confirmar. Os estudos mostram que a maioria dos day traders perde depois dos custos: o robô só opera de verdade o que passar no teste histórico." />
        </p>
        <button
          className="underline disabled:opacity-50"
          type="button"
          onClick={() =>
            void run("Preset day trade", async () => {
              const preview = await api.dayPreset();
              const lines = preview.changes.map((change) => `${change.field}: ${String(change.from)} → ${String(change.to)}`);
              const cap = preview.capped_by_order_cap
                ? `\nO teto por ordem (US$ ${preview.order_cap_usd}) limita o tamanho: o risco de 0,5% pediria US$ ${preview.risk_notional_usd}.`
                : "";
              const sizing = preview.sizing
                ? `\nCom stop de ${pctText(preview.sizing.stop_pct)}, uma ordem seria de US$ ${preview.sizing.size_usd}, limitada por ${preview.sizing.label}.`
                : "";
              const text = `Aplicar o preset de day trade?\n\n${lines.join("\n") || "nada muda"}${cap}${sizing}\nMandato: até ${preview.mandate_orders_per_day} ordens por dia.`;
              if (!window.confirm(text)) throw new Error("cancelado");
              return api.applyDayPreset();
            }, () => "Preset de day trade aplicado e mandato confirmado")
          }
        >
          Aplicar preset de day trade
        </button>
      </div>
      <div className="rounded border border-neutral-300 p-3 dark:border-neutral-700">
        <p className="mb-2 flex items-center font-medium">
          Pares analisados ({status.symbols.length})
          <Help text="Quanto mais pares, mais setups aparecem. Mais líquidos usa os 20 pares USDT com maior volume nas últimas 24h, sem stablecoins. Pares com posição aberta nunca saem da lista." />
        </p>
        <p className="mb-2 text-neutral-500">{status.symbols.join(", ")}</p>
        <p className="mb-2 flex flex-wrap items-center gap-2">
          <label>
            <input
              type="checkbox"
              checked={status.auto_select}
              onChange={(event) => { const on = event.target.checked; void run(on ? "Ligar a seleção automática" : "Desligar a seleção automática", () => api.updateConfig({ auto_select: on }), () => (on ? "Seleção automática ligada: os pares mudam no próximo ciclo" : "Seleção automática desligada")); }}
            />{" "}
            Escolher automaticamente os {status.auto_select_count} mais promissores
          </label>
          <Help text="Uma vez por hora o robô pega os 40 pares USDT mais líquidos e fica com os de melhor momento ajustado ao risco: a alta dos últimos 28 dias dividida pela oscilação diária. Só entram pares em alta. Isso só escolhe quais pares as estratégias olham; nenhuma compra sai daqui sem um setup Pronto. Pares com posição aberta nunca saem." />
          {status.auto_select_at && (
            <span className="text-neutral-500">última escolha: {new Date(status.auto_select_at).toLocaleTimeString()}</span>
          )}
        </p>
        <p className="mb-2 flex flex-wrap items-center gap-2">
          <input
            className="w-16 border bg-transparent px-2 py-1"
            placeholder={String(status.auto_select_count)}
            value={pickCount}
            onChange={(event) => setPickCount(event.target.value)}
            aria-label="Quantidade de pares escolhidos automaticamente"
          />
          pares
          <button
            className="underline disabled:opacity-50"
            type="button"
            disabled={!pickCountValid}
            onClick={() => void run("Salvar quantidade de pares", () => api.updateConfig({ auto_select_count: pickCountValue ?? 0 }), () => `A seleção automática usa ${pickCountValue} pares`)}
          >
            salvar quantidade
          </button>
        </p>
        {!status.auto_select && (
          <p className="flex flex-wrap gap-3">
            <button className="underline" type="button" onClick={() => void run("Usar os mais líquidos", () => api.chooseSymbols("top"), symbolsResult)}>
              usar os 20 mais líquidos
            </button>
            <button className="underline" type="button" onClick={() => void run("Voltar aos pares padrão", () => api.chooseSymbols("default"), symbolsResult)}>
              voltar aos 5 padrão
            </button>
          </p>
        )}
        {!status.auto_select && status.mode !== "production" && (
          <p className="mt-2 text-neutral-500">
            No Spot Testnet só entram os pares que ele lista, porque as ordens vão para lá.
          </p>
        )}
        {symbolsNote && <p className="mt-2 whitespace-pre-line">{symbolsNote}</p>}
      </div>
      <div className="rounded border border-neutral-300 p-3 dark:border-neutral-700">
        <p className="mb-2 flex items-center font-medium">
          Risco da carteira
          <Help text="Risco por trade: quanto do capital se perde se o stop for atingido; o tamanho da ordem sai daí, então ativos mais voláteis compram menos. Risco total: a soma dessas perdas de todas as posições abertas. O robô reparte o USDT livre entre as oportunidades, da maior nota para a menor, uma posição por par, e nunca dimensiona sobre um dinheiro que não está na conta." />
        </p>
        <p className="mb-2 text-neutral-500">
          {((status.limits.risk_per_trade_pct ?? 0.01) * 100).toFixed(1)}% por trade · até {((status.limits.max_open_risk_pct ?? 0.06) * 100).toFixed(0)}% somando as posições · no máximo {status.limits.max_positions} posições · teto por ordem de US$ {(status.capital_usd * (status.limits.max_order_notional_pct ?? 0.02)).toFixed(0)} · lucro esperado mínimo de US$ {status.min_profit_usd.toFixed(2)} · Kelly {((status.limits.kelly_fraction ?? 0.25) * 100).toFixed(0)}% · até {status.limits.max_entries_per_cycle ?? 2} entradas por ciclo
        </p>
        <p className="mb-2 flex flex-wrap items-center gap-2">
          US$
          <input
            className="w-20 border bg-transparent px-2 py-1"
            placeholder={status.min_profit_usd.toFixed(2)}
            value={minProfit}
            onChange={(event) => setMinProfit(event.target.value)}
            aria-label="Lucro esperado mínimo em dólares"
          />
          de lucro esperado mínimo por trade
          <button
            className="underline disabled:opacity-50"
            type="button"
            disabled={!minProfitValid}
            onClick={() => void run("Salvar lucro mínimo", () => api.updateConfig({ min_profit_usd: minProfitValue ?? 0 }), () => `Lucro esperado mínimo: US$ ${minProfitValue}`)}
          >
            salvar mínimo{minProfitValid ? ` de US$ ${minProfitValue}` : ""}
          </button>
          <Help text="O robô só entra quando o tamanho da ordem vezes o retorno líquido até o alvo dá pelo menos esse valor. Com o stop móvel, o alvo é só a referência: o lucro real pode ser maior ou menor, e a perda no stop continua possível. Um mínimo maior exige ordens maiores, ou seja, mais risco." />
        </p>
        <p className="flex flex-wrap items-center gap-2">
          <input
            className="w-16 border bg-transparent px-2 py-1"
            placeholder="1"
            value={tradeRisk}
            onChange={(event) => setTradeRisk(event.target.value)}
            aria-label="Risco por trade em porcentagem"
          />
          % por trade (até 2)
          <input
            className="w-16 border bg-transparent px-2 py-1"
            placeholder="6"
            value={openRisk}
            onChange={(event) => setOpenRisk(event.target.value)}
            aria-label="Risco total em porcentagem"
          />
          % no total (até 20)
          <button
            className="underline disabled:opacity-50"
            type="button"
            disabled={!riskValid}
            onClick={() => void run("Salvar risco", () => api.updateConfig({ limits: riskPayload }))}
          >
            salvar risco
          </button>
        </p>
      </div>
      <LimitsCard status={status} />
      {status.mode === "testnet" && (
        <p className="flex items-center">
          <label>
            <input
              type="checkbox"
              checked={status.testnet_test_mode}
              onChange={(event) => { const on = event.target.checked; void run(on ? "Ligar o modo teste" : "Desligar o modo teste", () => api.updateConfig({ testnet_test_mode: on })); }}
            />{" "}
            Modo teste do Spot Testnet
          </label>
          <Help text="Só no testnet. Setups de compra que não passaram em um filtro de qualidade (nota, custo, ganho/risco, regime) ou cuja estratégia não está validada aparecem como Pronta com a marca teste, para você testar a compra, o stop e a saída. Saem pelo botão executar e, com o trade automático ligado, também pelo robô. Essas posições não entram na taxa de acerto. Os limites de risco e o mandato continuam valendo." />
        </p>
      )}
      <div className="rounded border border-neutral-300 p-3 dark:border-neutral-700">
        <p className="mb-2 flex items-center font-medium">
          Entradas
          <Help text="Manual: o robô nunca compra sozinho. Você clica em executar numa oportunidade Pronta e ele cuida do stop e das saídas; o ciclo liga sozinho nessa hora. Automático: além disso, o ciclo compra sozinho as oportunidades Prontas cuja taxa de acerto estimada passa do mínimo (no day trade, do ponto de equilíbrio do TP1 mais uma margem). Ao trocar para automático o ciclo para, e é preciso clicar em Iniciar." />
        </p>
        <label className="mr-4">
          <input
            type="radio"
            name="entry_mode"
            checked={status.entry_mode === "manual"}
            onChange={() => void run("Entradas manuais", () => api.updateConfig({ entry_mode: "manual" }))}
          />{" "}
          Manual (só gerencia o que eu abrir)
        </label>
        <label>
          <input
            type="radio"
            name="entry_mode"
            checked={status.entry_mode === "auto"}
            onChange={() => void run("Entradas automáticas", () => api.updateConfig({ entry_mode: "auto" }), () => "Entradas automáticas: o ciclo foi pausado; clique em Iniciar")}
          />{" "}
          Automático a partir de {Math.round(status.min_win_rate * 100)}% de acerto (swing)
        </label>
        <p className="mt-2 flex flex-wrap items-center gap-2">
          <input
            className="w-20 border bg-transparent px-2 py-1"
            placeholder={String(Math.round(status.min_win_rate * 100))}
            value={winRate}
            onChange={(event) => setWinRate(event.target.value)}
            aria-label="Taxa de acerto mínima em porcentagem"
          />
          %
          <button
            className="underline disabled:opacity-50"
            type="button"
            disabled={!winRateValid}
            onClick={() => void run("Salvar acerto mínimo", () => api.updateConfig({ min_win_rate: winRateValue }), () => `Acerto mínimo: ${winRatePct}%`)}
          >
            salvar mínimo{winRateValid ? ` de ${winRatePct}%` : ""}
          </button>
          <Help text={`Entre 50% e 95%, para as estratégias swing. A taxa só existe com pelo menos ${status.min_win_rate_sample} trades da estratégia, no teste histórico e nas posições reais, com a mesma regra de saída. Quem não tem amostra nunca entra sozinho. Day trade ignora este mínimo: ganha menos vezes por desenho (metade no TP1, o resto no equilíbrio), então entra sozinho quando a taxa passa do ponto de equilíbrio do TP1 mais ${Math.round((status.day_win_rate_margin ?? 0.03) * 100)} pontos. Esse ponto depende do stop e do custo de cada oportunidade: com stop de 1%, fica perto de 47%.`} />
        </p>
      </div>
      <div className="rounded border border-neutral-300 p-3 dark:border-neutral-700">
        <p className="mb-2 flex items-center font-medium">
          Alvo e stop fixos
          <Help text="Você define o ganho e a perda, em porcentagem do preço de entrada. O robô compra, põe o stop na Binance a essa distância abaixo do preço médio executado e vende quando o preço chega ao alvo. Vale para todas as estratégias e substitui o stop, o alvo e as saídas de cada uma, o stop móvel e as saídas do day trade. É outra regra de saída: as estratégias ligadas são validadas de novo sozinhas no histórico, e só operam se passarem. No automático, a taxa de acerto precisa passar do ponto de equilíbrio dessa regra (perda ÷ (perda + ganho líquido)) mais 3 pontos. O alvo mínimo é 1%, para cobrir o custo de ida e volta. Posições já abertas seguem a regra com que abriram." />
        </p>
        <p className="mb-2 text-neutral-500">
          {fixedOn
            ? `Ligado: vende com +${((status.take_profit_pct ?? 0) * 100).toFixed(2)}% e corta com −${((status.stop_loss_pct ?? 0) * 100).toFixed(2)}%.`
            : "Desligado: cada estratégia usa o próprio stop e alvo (ou o stop móvel)."}
        </p>
        <p className="flex flex-wrap items-center gap-2">
          ganho
          <input
            className="w-16 border bg-transparent px-2 py-1"
            placeholder={fixedOn ? String(((status.take_profit_pct ?? 0) * 100).toFixed(2)) : "3"}
            value={takeProfit}
            onChange={(event) => setTakeProfit(event.target.value)}
            aria-label="Ganho do alvo em porcentagem"
          />
          % · perda
          <input
            className="w-16 border bg-transparent px-2 py-1"
            placeholder={fixedOn ? String(((status.stop_loss_pct ?? 0) * 100).toFixed(2)) : "1,5"}
            value={stopLoss}
            onChange={(event) => setStopLoss(event.target.value)}
            aria-label="Perda do stop em porcentagem"
          />
          %
          <button
            className="underline disabled:opacity-50"
            type="button"
            disabled={!fixedValid}
            onClick={() => {
              if (!window.confirm(`${fixedOn ? "Trocar para" : "Ligar"} alvo +${takeProfitPct}% e stop −${stopLossPct}%?\n\n${revalidationWarning}`)) return;
              void run(
                "Salvar alvo e stop fixos",
                () => api.updateConfig({ take_profit_pct: (takeProfitPct ?? 0) / 100, stop_loss_pct: (stopLossPct ?? 0) / 100 }),
                () => `Alvo +${takeProfitPct}% e stop −${stopLossPct}%; as estratégias ligadas estão sendo validadas`,
              );
            }}
          >
            {fixedOn ? "salvar" : "ligar"}
          </button>
          {fixedOn && (
            <button
              className="underline"
              type="button"
              onClick={() => {
                if (!window.confirm(`Desligar o alvo e o stop fixos? Cada estratégia volta ao próprio stop e alvo.\n\n${revalidationWarning}`)) return;
                void run("Desligar alvo e stop fixos", () => api.updateConfig({ take_profit_pct: null, stop_loss_pct: null }));
              }}
            >
              desligar
            </button>
          )}
        </p>
      </div>
      <p className={`flex items-center ${fixedOn ? "opacity-50" : ""}`}>
        <label>
          <input
            type="checkbox"
            disabled={fixedOn}
            checked={status.trailing_stop}
            onChange={(event) => {
              const on = event.target.checked;
              if (!window.confirm(`${on ? "Ligar" : "Desligar"} o stop móvel?\n\n${revalidationWarning}`)) return;
              void run(on ? "Ligar o stop móvel" : "Desligar o stop móvel", () => api.updateConfig({ trailing_stop: on }));
            }}
          />{" "}
          Stop móvel
        </label>
        <Help text="O stop na Binance sobe junto com o maior preço, mantendo a distância da entrada, e nunca desce. Assim a posição segue enquanto o preço sobe e sai quando ele volta. Com o stop móvel ligado, o alvo fixo não fecha a posição. Trocar esta opção exige validar a estratégia de novo, porque a taxa de acerto depende da regra de saída. Posições já abertas seguem a regra com que abriram." />
      </p>
      <p className="flex items-center">
        <label className={status.trailing_stop ? "" : "opacity-50"}>
          <input
            type="checkbox"
            disabled={!status.trailing_stop}
            checked={status.trailing_activation}
            onChange={(event) => {
              const on = event.target.checked;
              if (!window.confirm(`${on ? "Ligar" : "Desligar"} a ativação do stop móvel?\n\n${revalidationWarning}`)) return;
              void run(on ? "Ligar a ativação do stop móvel" : "Desligar a ativação do stop móvel", () => api.updateConfig({ trailing_activation: on }));
            }}
          />{" "}
          Stop móvel com ativação (trailing take-profit)
        </label>
        <Help text={`Como o trailing take-profit da Binance. O stop de entrada fica parado até o lucro chegar a ${status.trailing_rules?.activation_r ?? 1}× a distância do stop. Daí em diante ele segue o maior preço a ${Math.round((status.trailing_rules?.callback_share ?? 0.5) * 100)}% dessa distância, o que já trava lucro na ativação. É outra regra de saída: as estratégias precisam ser validadas de novo com ela.`} />
      </p>
      <p className="flex items-center">
        <label>
          <input
            type="checkbox"
            checked={status.flatten_on_halt}
            onChange={(event) => {
              const on = event.target.checked;
              void run(on ? "Zerar no halt ligado" : "Zerar no halt desligado", async () => {
                const target = status.mandate_target;
                const changes = target ? mandateChanges(status.mandate, { ...target, flatten_on_halt: on }) : [];
                const text =
                  `${on ? "Ligar" : "Desligar"} zerar posições no halt?\n\n` +
                  "Esta opção faz parte do mandato da corretora, e a parada de emergência segue o mandato. Para mudar, o mandato é reconfirmado com os limites atuais do bot.json." +
                  (!target
                    ? "\n\nNão foi possível calcular o mandato novo: confira os limites no cartão do mandato depois de confirmar."
                    : changes.length
                      ? `\n\nO mandato passa a ter:\n${changes.join("\n")}`
                      : "");
                if (!window.confirm(text)) throw new Error("cancelado");
                return api.updateConfig({ flatten_on_halt: on, reconfirm_mandate: true });
              }, () => `Zerar no halt ${on ? "ligado" : "desligado"} e mandato reconfirmado`);
            }}
          />{" "}
          Zerar posições no halt
        </label>
        <Help text="Se estiver marcado, a parada de emergência vende o que estiver aberto antes de travar. Desmarcado, as posições ficam com o stop na Binance. Faz parte do mandato: a parada segue o que está no mandato, então mudar aqui pede para reconfirmar o mandato com os limites atuais do bot.json." />
      </p>
      <RulesCard status={status} />
      <MacroCard status={status} />
      <AdvisorCard status={status} />
      <div className="rounded border border-neutral-300 p-3 dark:border-neutral-700">
        <p className="mb-2 flex items-center font-medium">
          Switch de ambiente
          <Help text="Troca o ensaio pela conta real, ou volta ao ensaio. Ir para produção exige o robô parado, sem posição aberta, chaves válidas, mandato só de cripto, as quatro liberações gravadas no bot.json, a palavra PRODUCAO escrita igual e 30 dias de testnet com lucro, 30 trades com R e drawdown dentro do limite. O piloto abaixo dispensa só esses 30 dias, com o capital limitado. Se faltar qualquer coisa, ele permanece no testnet." />
        </p>
        <div className="mb-3 rounded border border-amber-400 p-2">
          <p className="mb-1 flex items-center font-medium">
            Piloto com pouco dinheiro
            <Help text="Para testar a conta real com uma quantia pequena. Dispensa os 30 dias de testnet enquanto o capital de produção (10% do alocado) ficar até o limite informado, de no máximo US$ 50. O limite é gravado na troca e trava o capital: subir o alocado depois é recusado. Chaves, mandato, robô parado, sem posição aberta, a palavra PRODUCAO e sem violação no testnet continuam exigidos. A segunda caixa deixa as estratégias ligadas operarem sem o teste histórico e, até haver amostra, sem estimativa de acerto. A terceira dispensa as quatro liberações G1 a G4 do bot.json só nesta troca. Mesmo assim, deixe na conta só o valor do teste." />
          </p>
          <label className="flex items-center gap-2">
            <input
              type="checkbox"
              checked={pilotHistory}
              onChange={(event) => {
                setPilotHistory(event.target.checked);
                if (!event.target.checked) {
                  setPilotValidation(false);
                  setPilotGates(false);
                }
              }}
            />
            Ignorar o histórico de 30 dias do testnet
          </label>
          <label className="mt-1 flex items-center gap-2">
            <input type="checkbox" disabled={!pilotHistory} checked={pilotValidation} onChange={(event) => setPilotValidation(event.target.checked)} />
            Ignorar a validação das estratégias (operar sem o teste histórico)
          </label>
          <label className="mt-1 flex items-center gap-2">
            <input type="checkbox" disabled={!pilotHistory} checked={pilotGates} onChange={(event) => setPilotGates(event.target.checked)} />
            Ignorar as liberações G1 a G4 do bot.json (robô parado continua exigido)
          </label>
          <p className="mt-2">
            <label>
              Capital do piloto (US$, até 50){" "}
              <input
                className="w-24 border bg-transparent px-2 py-1"
                value={pilotCap}
                onChange={(event) => setPilotCap(event.target.value)}
                disabled={!pilotHistory}
                inputMode="decimal"
                aria-label="Capital do piloto"
              />
            </label>
          </p>
          <p className="mt-1 text-xs text-neutral-500">
            Capital de produção hoje: {usd(productionCapital)}, de {usd(status.allocated_capital_usd)} alocados.
            {pilotHistory && pilotCapValid && !pilotFits && pilotMaxAllocated != null && (
              <span className="ml-1 text-red-600">Acima do limite: reduza o capital alocado para até {usd(pilotMaxAllocated)}.</span>
            )}
            {pilotHistory && pilotCap.trim() !== "" && !pilotCapValid && <span className="ml-1 text-red-600">Informe um valor acima de 0 e até 50.</span>}
          </p>
        </div>
        <input
          className="mb-2 block w-full border bg-transparent px-2 py-1"
          placeholder="digite PRODUCAO para promover"
          value={confirmation}
          onChange={(event) => setConfirmation(event.target.value)}
          aria-label="Confirmação para produção"
        />
        <p className="flex flex-wrap items-center gap-3">
          <span className="inline-flex items-center">
            <button
              className="underline disabled:no-underline disabled:opacity-50"
              type="button"
              disabled={pilotHistory && !pilotFits}
              onClick={() => {
                const payload: Record<string, unknown> = { mode: "production", confirmation };
                if (pilotHistory) {
                  payload.pilot_cap_usd = pilotCapValue;
                  payload.pilot_skip_validation = pilotValidation;
                  payload.pilot_skip_gates = pilotGates;
                  const text =
                    `Ir para produção como piloto, com dinheiro real?\n\nCapital de produção limitado a ${usd(pilotCapValue ?? 0)}, sem os 30 dias de testnet.` +
                    (pilotValidation
                      ? "\nAs estratégias ligadas vão operar SEM validação no histórico e, até haver amostra, sem estimativa de acerto."
                      : "") +
                    (pilotGates ? "\nA troca dispensa as liberações G1 a G4 do bot.json." : "") +
                    "\n\nDeixe na conta só o valor do teste.";
                  if (!window.confirm(text)) return;
                }
                void run("Ir para Produção", () => api.updateConfig(payload));
              }}
            >
              Ir para Produção
            </button>
            <Help text="Pede a troca para a conta real. Sem a palavra PRODUCAO e sem as liberações, a tela mostra o motivo e continua no Spot Testnet. Com o piloto marcado, pede confirmação e envia o limite de capital." />
          </span>
          <span className="inline-flex items-center">
            <button className="underline" type="button" onClick={() => void run("Voltar para Spot Testnet", () => api.updateConfig({ mode: "testnet" }))}>
              Voltar para Spot Testnet
            </button>
            <Help align="end" text="Volta ao ensaio. Com zerar no halt marcado, as posições de produção são vendidas antes. Se alguma posição de produção continuar aberta, a troca é recusada: feche-a antes, para que nada fique sem acompanhamento." />
          </span>
        </p>
      </div>
      </fieldset>
    </section>
  );
}

/** "1,5%": a fraction shown as a percentage, without trailing zeros. */
function pctText(value: number | null | undefined, digits = 2): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${Number((value * 100).toFixed(digits)).toString().replace(".", ",")}%`;
}

function usd(value: number): string {
  return `US$ ${value.toFixed(2)}`;
}

/** The confirm text of every change to a validation key (exit rule: fixed TP/SL and trailing; market filter; hysteresis). */
const revalidationWarning =
  "As estratégias ligadas passam a só sinal até validar de novo, e cada nova validação conta como tentativa no Sharpe deflacionado.";

function yesNo(value: boolean): string {
  return value ? "sim" : "não";
}

/** Each mandate field, compared the way the gate reads it: dollars to the cent, the rest exactly. */
const mandateFields: [string, (mandate: MandateSummary) => string, (a: MandateSummary, b: MandateSummary) => boolean][] = [
  ["Teto por ordem", (m) => usd(m.max_order_notional_usd), (a, b) => Math.abs(a.max_order_notional_usd - b.max_order_notional_usd) > 0.005],
  ["Exposição total", (m) => usd(m.max_total_exposure_usd), (a, b) => Math.abs(a.max_total_exposure_usd - b.max_total_exposure_usd) > 0.005],
  ["Ordens por dia", (m) => String(m.max_trades_per_day), (a, b) => a.max_trades_per_day !== b.max_trades_per_day],
  ["Zerar no halt", (m) => yesNo(m.flatten_on_halt), (a, b) => a.flatten_on_halt !== b.flatten_on_halt],
];

/** Where the committed mandate differs from the target a reconfirmation would write, one line per field. */
function mandateChanges(mandate: MandateSummary | null | undefined, target: MandateSummary): string[] {
  if (!mandate) return [];
  return mandateFields.filter(([, , differs]) => differs(mandate, target)).map(([label, shown]) => `${label}: ${shown(mandate)} → ${shown(target)}`);
}

/** The drawdown level the runtime keeps: cautious and reducing change what the bot may buy. */
function RiskBanner({ status }: { status: Status }) {
  const state = status.risk_state;
  if (!state || (state.level !== "cautious" && state.level !== "reducing")) return null;
  const since = state.since ? ` desde ${new Date(state.since).toLocaleString("pt-BR")}` : "";
  const drawdown = pctText(state.drawdown, 1);
  const reduce = pctText(status.limits.dd_reduce_pct, 1);
  // Where the runtime leaves reducing: the cautious step, or the reducing limit itself when that step is off (0).
  const ddReduce = status.limits.dd_reduce_pct ?? 0;
  const release = pctText(ddReduce > 0 ? ddReduce : status.limits.max_drawdown_pct, 1);
  const halt = pctText(status.limits.halt_drawdown_pct, 1);
  if (state.level === "cautious") {
    return (
      <p role="status" className="rounded border border-amber-400 bg-amber-50 p-3 text-amber-900 dark:bg-amber-950 dark:text-amber-200">
        <strong>Modo {riskLevels.cautious}{since}.</strong> O patrimônio está {drawdown} abaixo do pico (a partir de {reduce}). Cada trade arrisca a metade e
        entra no máximo uma posição por ciclo. Volta ao normal sozinho quando a queda ficar abaixo de {release}.
        {state.reason ? ` Motivo: ${state.reason}.` : ""}
      </p>
    );
  }
  return (
    <p role="alert" className="rounded border border-red-500 bg-red-50 p-3 text-red-900 dark:bg-red-950 dark:text-red-200">
      <strong>Modo {riskLevels.reducing}{since}.</strong> O patrimônio está {drawdown} abaixo do pico (limite {pctText(status.limits.max_drawdown_pct, 1)}). Nenhuma
      compra nova: estratégias, entradas armadas, execução manual e robôs ficam bloqueados. Stops, saídas, stop móvel e TP1 continuam. Sai sozinho quando a
      queda ficar abaixo de {release}; em {halt} o robô para (halt).
      {state.reason ? ` Motivo: ${state.reason}.` : ""}
    </p>
  );
}

/** The committed mandate next to what bot.json asks for; the smaller cap wins until a reconfirmation. */
function MandateCard({ status }: { status: Status }) {
  const perform = useBot((state) => state.perform);
  const mandate = status.mandate;
  const target = status.mandate_target ?? null;
  const changes = target ? mandateChanges(mandate, target) : [];
  // The bot.json percentage behind each dollar cap, so a difference can be traced to its limit.
  const fieldNotes: Record<string, string> = {
    "Teto por ordem": ` (${pctText(status.limits.max_order_notional_pct)})`,
    "Exposição total": ` (${pctText(status.limits.max_exposure_pct)})`,
  };
  return (
    <div className="rounded border border-neutral-300 p-3 dark:border-neutral-700">
      <p className="mb-2 flex items-center font-medium">
        Mandato da corretora
        <Help text="O mandato é o limite que o portão de segurança aplica a cada ordem, gravado quando você confirma. O bot.json guarda os seus limites. Se um teto do bot.json passar do mandato, o menor vence até você reconfirmar: é por isso que uma ordem pode sair menor do que o risco pediria, com o selo teto por ordem ou teto da corretora." />
      </p>
      {!mandate ? (
        <p className="text-neutral-500">Nenhum mandato gravado neste ambiente. Salve o capital para confirmar um.</p>
      ) : (
        <>
          <table className="text-left">
            <thead className="text-neutral-500">
              <tr>
                <th className="pr-4 font-medium" />
                <th className="pr-4 font-medium">Mandato</th>
                <th className="font-medium">bot.json</th>
              </tr>
            </thead>
            <tbody>
              {mandateFields.map(([label, shown, differs]) => (
                <tr key={label} className="border-t border-neutral-200 dark:border-neutral-800">
                  <td className="pr-4">{label}</td>
                  <td className="pr-4">{shown(mandate)}</td>
                  {target ? (
                    <td className={differs(mandate, target) ? "text-amber-600" : ""}>
                      {shown(target)}
                      {fieldNotes[label] ?? ""}
                    </td>
                  ) : (
                    <td className="text-neutral-500">—</td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
          {!target ? (
            <p className="mt-2 text-amber-600">Não foi possível calcular o mandato que o bot.json gravaria agora; a comparação fica indisponível.</p>
          ) : changes.length > 0 ? (
            <p className="mt-2 flex flex-wrap items-center gap-2">
              <span className="text-amber-600">O mandato difere do bot.json; até reconfirmar, valem o menor teto, as ordens por dia e o zerar no halt do mandato.</span>
              <button
                className="underline"
                type="button"
                onClick={() =>
                  void perform("Reconfirmar o mandato", async () => {
                    if (!window.confirm(`Reconfirmar o mandato com os limites do bot.json?\n\n${changes.join("\n")}`)) throw new Error("cancelado");
                    return api.updateConfig({ reconfirm_mandate: true });
                  }, () => "Mandato reconfirmado com os limites do bot.json")
                }
              >
                reconfirmar o mandato
              </button>
            </p>
          ) : (
            <p className="mt-2 text-neutral-500">O mandato está igual ao bot.json.</p>
          )}
        </>
      )}
    </div>
  );
}

type LimitField = {
  key: string;
  label: string;
  /** pct: typed in % and sent as a fraction; int: a whole number; ratio: a plain decimal. */
  unit: "pct" | "int" | "ratio";
  min: number;
  max: number;
  /** The minimum itself is refused (a bound written as "> min"). */
  above?: boolean;
  help: string;
};

const limitGroups: [string, string, LimitField[]][] = [
  [
    "Drawdown em degraus",
    "Queda do patrimônio a partir do pico. Cada degrau precisa ficar abaixo do seguinte. Mudar só o só reduzir mantém os degraus gravados que continuam em ordem e recalcula os que saem dela (cautela na metade dele, halt em 1,5 vez dele entre 15% e 50%). A cautela em 0 continua desligada.",
    [
      { key: "dd_reduce_pct", label: "Cautela a partir de (%)", unit: "pct", min: 0, max: 50, help: "Risco por trade pela metade e no máximo uma entrada por ciclo. 0 desliga este degrau." },
      { key: "max_drawdown_pct", label: "Só reduzir a partir de (%)", unit: "pct", min: 0, max: 50, above: true, help: "Nenhuma compra nova; stops, saídas, stop móvel e TP1 continuam. Sai sozinho quando a queda volta abaixo da cautela; com a cautela em 0, quando volta abaixo deste nível." },
      { key: "halt_drawdown_pct", label: "Halt a partir de (%)", unit: "pct", min: 0, max: 100, above: true, help: "Parada de emergência: o robô para e não volta sozinho." },
    ],
  ],
  [
    "Tamanho das ordens",
    "Tetos que diminuem o tamanho calculado pelo risco por trade. O selo de cada oportunidade mostra qual deles decidiu.",
    [
      { key: "vol_cap_pct", label: "Volatilidade diária (% do capital)", unit: "pct", min: 0, max: 5, help: "Uma oscilação diária média (ATR de 14 dias) da posição custa no máximo esta parte do capital. Freia pares muito voláteis. 0 desliga." },
      { key: "target_vol_annual", label: "Alvo de volatilidade anual (%)", unit: "pct", min: 0, max: 300, help: "Quando a volatilidade da carteira passa disso, as entradas novas encolhem para caber. Nada é vendido para reequilibrar. 0 desliga." },
      { key: "vol_throttle_ratio", label: "Freio de volatilidade (×)", unit: "ratio", min: 0, max: 10, help: "Se a volatilidade dos últimos 7 dias passar deste múltiplo da habitual do par, a ordem sai pela metade. 0 desliga." },
    ],
  ],
  [
    "Concentração",
    "Limites para não empilhar o mesmo risco em pares que andam juntos.",
    [
      { key: "cluster_corr", label: "Correlação com BTC (0 a 1)", unit: "ratio", min: 0, max: 1, above: true, help: "Um par com correlação acima disto entra no grupo do BTC. O risco somado do grupo fica em no máximo metade do risco total." },
      { key: "max_instances", label: "Robôs DCA/Grid ativos", unit: "int", min: 0, max: 20, help: "Quantos robôs podem rodar ao mesmo tempo." },
    ],
  ],
  [
    "Execução",
    "Filtros de qualidade da entrada.",
    [
      { key: "slippage_budget_pct", label: "Orçamento de derrapagem (%)", unit: "pct", min: 0, max: 5, help: "Se a derrapagem média de ida e volta dos últimos 20 trades de um par passar disto, o par fica bloqueado para entradas novas. 0 desliga." },
      { key: "max_signal_age_seconds", label: "Idade máxima do sinal (s)", unit: "int", min: 0, max: 3600, help: "O trade automático não entra num sinal mais velho que isto, contado do fechamento da vela. A entrada manual mostra a idade e continua permitida. 0 desliga." },
    ],
  ],
];

function limitShown(field: LimitField, value: number | undefined): string {
  if (value == null) return "—";
  if (field.unit === "pct") return pctText(value);
  return String(value).replace(".", ",");
}

/** The typed value as the API wants it, or null when it is not valid for the field. */
function limitValue(field: LimitField, text: string): number | null {
  const parsed = field.unit === "int" ? parseWhole(text) : parseDecimal(text);
  if (parsed == null) return null;
  if (parsed > field.max || parsed < field.min || (field.above && parsed === field.min)) return null;
  return field.unit === "pct" ? parsed / 100 : parsed;
}

function LimitsCard({ status }: { status: Status }) {
  const perform = useBot((state) => state.perform);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const fields = limitGroups.flatMap(([, , items]) => items);
  const typed = fields.filter((field) => (drafts[field.key] ?? "").trim() !== "");
  const payload: Record<string, number> = {};
  let valid = typed.length > 0;
  for (const field of typed) {
    const value = limitValue(field, drafts[field.key]);
    if (value == null) valid = false;
    else payload[field.key] = value;
  }
  return (
    <div className="rounded border border-neutral-300 p-3 dark:border-neutral-700">
      <p className="mb-2 flex items-center font-medium">
        Limites de risco
        <Help text="Os degraus de drawdown, os tetos do tamanho, a concentração e os filtros de execução. Preencha só o que quer mudar; o valor atual aparece ao lado. Nenhum destes faz parte do mandato." />
      </p>
      {limitGroups.map(([title, help, items]) => (
        <div key={title} className="mb-3">
          <p className="flex items-center text-neutral-500">
            {title}
            <Help text={help} />
          </p>
          {items.map((field) => {
            const text = drafts[field.key] ?? "";
            const bad = text.trim() !== "" && limitValue(field, text) == null;
            return (
              <label key={field.key} className="mt-1 flex flex-wrap items-center gap-2">
                <span className="inline-flex w-64 items-center">
                  {field.label}
                  <Help text={`${field.help} De ${field.min} a ${field.max}.`} />
                </span>
                <input
                  className={`w-20 border bg-transparent px-2 py-1 ${bad ? "border-red-600" : ""}`}
                  placeholder={limitShown(field, status.limits[field.key]).replace("%", "")}
                  value={text}
                  onChange={(event) => setDrafts({ ...drafts, [field.key]: event.target.value })}
                  aria-label={field.label}
                />
                <span className="text-neutral-500">atual {limitShown(field, status.limits[field.key])}</span>
              </label>
            );
          })}
        </div>
      ))}
      <button
        className="underline disabled:opacity-50"
        type="button"
        disabled={!valid}
        onClick={() =>
          void perform("Salvar limites de risco", () => api.updateConfig({ limits: payload }), () => "Limites de risco salvos").then((outcome) => {
            if (outcome.ok) setDrafts({});
          })
        }
      >
        salvar limites
      </button>
    </div>
  );
}

/** Market filter, exit hysteresis and the day-trade flatten on a daily loss. */
function RulesCard({ status }: { status: Status }) {
  const perform = useBot((state) => state.perform);
  const filterOn = status.market_filter === "btc_ema40d";
  const hysteresis = status.exit_hysteresis ?? true;
  return (
    <div className="space-y-2 rounded border border-neutral-300 p-3 dark:border-neutral-700">
      <p className="flex items-center font-medium">
        Regras de mercado e saída
        <Help text="O filtro de mercado e a histerese mudam as regras que o teste histórico replica: ao mudar, as estratégias ligadas são validadas de novo sozinhas. No Sharpe deflacionado, cada combinação nova (timeframe, regra, pares, filtro, histerese) conta como uma tentativa." />
      </p>
      <p className="flex items-center">
        <label>
          <input
            type="checkbox"
            checked={filterOn}
            onChange={(event) => {
              const on = event.target.checked;
              if (!window.confirm(`${on ? "Ligar" : "Desligar"} o filtro de mercado?\n\n${revalidationWarning}`)) return;
              void perform(on ? "Ligar o filtro de mercado" : "Desligar o filtro de mercado", () => api.updateConfig({ market_filter: on ? "btc_ema40d" : "off" }), () =>
                `Filtro de mercado ${on ? "ligado" : "desligado"}; as estratégias ligadas estão sendo validadas`);
            }}
          />{" "}
          Filtro de mercado: só comprar com o BTC acima da média de 40 dias
        </label>
        <Help text="Compara o fechamento de 4h do BTC com a média exponencial das últimas 240 velas de 4h (cerca de 40 dias). Abaixo dela, nenhuma entrada nova, em nenhum par. Começa desligado: compare validando com e sem." />
      </p>
      <p className="flex items-center">
        <label>
          <input
            type="checkbox"
            checked={hysteresis}
            onChange={(event) => {
              const on = event.target.checked;
              if (!window.confirm(`${on ? "Ligar" : "Desligar"} a histerese de saída?\n\n${revalidationWarning}`)) return;
              void perform(on ? "Ligar a histerese de saída" : "Desligar a histerese de saída", () => api.updateConfig({ exit_hysteresis: on }), () =>
                `Histerese ${on ? "ligada" : "desligada"}; as estratégias ligadas estão sendo validadas`);
            }}
          />{" "}
          Histerese na saída de Trend e Momentum
        </label>
        <Help text="Ligada, a saída por cruzamento de médias exige folga: a média de 20 fica 0,5% abaixo da de 50, ou abaixo dela em duas velas fechadas seguidas. Evita sair e voltar várias vezes num cruzamento indeciso. Posições já abertas seguem a regra com que abriram." />
      </p>
      <p className="flex items-center">
        <label>
          <input
            type="checkbox"
            checked={status.flatten_day_on_daily_loss ?? false}
            onChange={(event) => {
              const on = event.target.checked;
              void perform(on ? "Zerar day trades na perda do dia" : "Manter day trades na perda do dia", () => api.updateConfig({ flatten_day_on_daily_loss: on }));
            }}
          />{" "}
          Na perda máxima do dia, vender os day trades abertos
        </label>
        <Help text={`A perda do dia conta o patrimônio, inclusive o que ainda não foi realizado. Ao passar de ${pctText(status.limits.max_daily_loss_pct)} do capital, o robô pausa as entradas até o dia seguinte (UTC). Marcado, ele também vende os day trades abertos antes de pausar; as posições swing seguem com o stop. Desmarcado por padrão, porque vender na perda costuma vender no fundo.`} />
      </p>
    </div>
  );
}

/** The DeepSeek advisor's settings, saved through PUT /bot/config ``advisor``. The key never passes through the panel. */
function AdvisorCard({ status }: { status: Status }) {
  const perform = useBot((state) => state.perform);
  const [budget, setBudget] = useState("");
  const [vetoPct, setVetoPct] = useState("");
  const [wait, setWait] = useState("");
  const advisor = status.advisor;
  if (!advisor) return null;
  // Same bounds as bot.config.AdvisorConfig; the API refuses anything else with a 422.
  const budgetValue = parseDecimal(budget);
  const budgetValid = budgetValue != null && budgetValue <= 50;
  const vetoValue = parseDecimal(vetoPct);
  const vetoValid = vetoValue != null && vetoValue >= 50 && vetoValue <= 100;
  const waitValue = parseWhole(wait);
  const waitValid = waitValue != null && waitValue >= 5 && waitValue <= 120;
  const typed: [string, boolean, string, number | null][] = [
    ["daily_budget_usd", budgetValid, budget, budgetValue],
    ["min_veto_confidence", vetoValid, vetoPct, vetoValue == null ? null : vetoValue / 100],
    ["wait_seconds", waitValid, wait, waitValue],
  ];
  const filled = typed.filter(([, , text]) => text.trim() !== "");
  const payload: Record<string, number> = {};
  for (const [key, , , value] of filled) if (value != null) payload[key] = value;
  const valid = filled.length > 0 && filled.every(([, ok]) => ok);
  const save = (label: string, change: Record<string, unknown>, describe?: () => string) =>
    perform(label, () => api.updateConfig({ advisor: change }), describe);
  return (
    <div className="space-y-2 rounded border border-neutral-300 p-3 dark:border-neutral-700">
      <p className="flex items-center font-medium">
        Consultor DeepSeek
        <Help text="Uma segunda opinião sobre cada sinal Pronto das estratégias, dada pela IA da DeepSeek com os números do setup, do par, do BTC e da conta. O motor do robô decide: a IA só pode tirar uma entrada do automático ou diminuir o tamanho dela. Nunca cria nem aumenta uma ordem, e nunca passa pelo portão de segurança, pelo mandato, pelo halt ou pelos stops. Sem chave, sem rede ou sem orçamento, o robô opera como se ela estivesse desligada." />
      </p>
      <p>
        Chave:{" "}
        {advisor.configured ? (
          <span className="text-green-600">presente</span>
        ) : (
          <>
            <span className="text-amber-600">ausente</span>
            <span className="block text-neutral-500">
              Rode <code>python -m bot.ops advisor-key</code> na máquina do robô e cole a chave quando ele pedir. Ela fica em deepseek.json, nunca no painel nem no bot.json.
            </span>
          </>
        )}
      </p>
      <p className="flex flex-wrap items-center gap-2">
        <label className="inline-flex items-center">
          Modo
          <select
            className="ml-2 bg-transparent"
            value={advisor.mode}
            onChange={(event) => {
              const mode = event.target.value;
              void save(`Consultor: ${advisorModes[mode] ?? mode}`, { mode });
            }}
          >
            {Object.entries(advisorModes).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <Help text="Desligado: nada é consultado. Sombra: cada sinal Pronto é enviado à IA uma vez; o veredito é gravado e aparece em Oportunidades, e nenhuma ordem muda nem espera. Filtro: um veto com confiança acima do mínimo tira a linha do automático (o manual continua permitido), e um reduzir multiplica o tamanho por um fator entre 0,25 e 1. Confirmar não muda nada. Em nenhum modo a IA cria ou aumenta uma ordem. Robôs DCA e Grid, entradas armadas e linhas de teste não são consultados. Use a sombra até a calibração em Desempenho mostrar que os vetos ajudam." />
        <label className="ml-4 inline-flex items-center">
          Modelo
          <select
            className="ml-2 bg-transparent"
            value={advisor.model}
            onChange={(event) => {
              const model = event.target.value;
              void save(`Consultor: modelo ${advisorModels[model] ?? model}`, { model });
            }}
          >
            {Object.entries(advisorModels).map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <Help align="end" text="Flash é rápido e barato. V4 Pro custa mais por chamada. Cada modelo e cada versão do prompt têm a própria linha na calibração de Desempenho." />
      </p>
      <p className="flex items-center">
        <label>
          <input
            type="checkbox"
            checked={advisor.require_verdict}
            onChange={(event) => {
              const on = event.target.checked;
              if (on && !window.confirm("Exigir o veredito da IA?\n\nNo modo filtro, uma linha sem veredito não entra no automático. Se a DeepSeek falhar, a chave faltar ou o orçamento do dia acabar, as entradas automáticas param até ela voltar.")) return;
              void save(on ? "Exigir o veredito da IA" : "Não exigir o veredito da IA", { require_verdict: on });
            }}
          />{" "}
          Exigir veredito para entrar no automático
        </label>
        <Help text="Só no modo filtro. Marcado, uma linha Pronta sem veredito não entra no automático. Atenção: se a API da DeepSeek falhar, ficar sem chave ou o orçamento do dia acabar, as entradas automáticas param até ela voltar. Stops, saídas e a entrada manual continuam. Desmarcado, depois da espera a linha segue sem veredito." />
      </p>
      <AdvisorField
        label="Orçamento diário (US$)"
        help="Gasto máximo estimado por dia UTC. Ao chegar nele, as linhas seguem sem veredito até o dia seguinte. De 0 a 50."
        current={formatUsdCost(advisor.daily_budget_usd)}
        text={budget}
        bad={budget.trim() !== "" && !budgetValid}
        onChange={setBudget}
      />
      <AdvisorField
        label="Confiança mínima do veto (%)"
        help="No modo filtro, um veto só tira a linha do automático com pelo menos esta confiança. Abaixo dela, o veto só aparece. De 50 a 100."
        current={`${Math.round(advisor.min_veto_confidence * 100)}%`}
        text={vetoPct}
        bad={vetoPct.trim() !== "" && !vetoValid}
        onChange={setVetoPct}
      />
      <AdvisorField
        label="Espera pelo veredito (s)"
        help="No modo filtro, quanto tempo uma linha Pronta fica fora do automático esperando o veredito, contado entre ciclos. Depois disso, ou com erro, segue sem ele. De 5 a 120."
        current={`${advisor.wait_seconds} s`}
        text={wait}
        bad={wait.trim() !== "" && !waitValid}
        onChange={setWait}
      />
      <button
        className="underline disabled:opacity-50"
        type="button"
        disabled={!valid}
        onClick={() =>
          void save("Salvar o consultor", payload, () => "Consultor salvo").then((outcome) => {
            if (outcome.ok) {
              setBudget("");
              setVetoPct("");
              setWait("");
            }
          })
        }
      >
        salvar consultor
      </button>
      <p className="text-neutral-500">
        Hoje, testnet e produção somados: {advisor.today.calls} consultas · {formatUsdCost(advisor.today.cost_usd)} de {formatUsdCost(advisor.daily_budget_usd)} (estimativa) · {advisor.today.errors} erros
        {advisor.last_ok && <> · último veredito {new Date(advisor.last_ok).toLocaleString("pt-BR")}</>} · prompt {advisor.prompt_version}
      </p>
      {advisor.last_error && <p className="text-red-600">Último erro: {advisor.last_error}</p>}
    </div>
  );
}

/** One numeric advisor setting: typed, checked against its bounds, saved with the others. */
function AdvisorField({
  label,
  help,
  current,
  text,
  bad,
  onChange,
}: {
  label: string;
  help: string;
  current: string;
  text: string;
  bad: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex flex-wrap items-center gap-2">
      <span className="inline-flex w-64 items-center">
        {label}
        <Help text={help} />
      </span>
      <input
        className={`w-20 border bg-transparent px-2 py-1 ${bad ? "border-red-600" : ""}`}
        value={text}
        onChange={(event) => onChange(event.target.value)}
        aria-label={label}
      />
      <span className="text-neutral-500">atual {current}</span>
    </label>
  );
}

/** Operator list of macro events: day-strategy entries pause around each one. */
function MacroCard({ status }: { status: Status }) {
  const perform = useBot((state) => state.perform);
  const [events, setEvents] = useState("");
  const [minutes, setMinutes] = useState("");
  const current = status.macro_events_utc ?? [];
  const lines = events.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const badLines = lines.filter((line) => Number.isNaN(Date.parse(/[zZ]|[+-]\d\d:?\d\d$/.test(line) ? line : `${line}Z`)));
  const minutesValue = parseWhole(minutes);
  const minutesValid = minutes.trim() === "" || (minutesValue != null && minutesValue <= 180);
  const payload: Record<string, unknown> = {};
  if (lines.length > 0) payload.macro_events_utc = lines;
  if (minutes.trim() !== "" && minutesValue != null) payload.macro_pause_minutes = minutesValue;
  const valid = Object.keys(payload).length > 0 && badLines.length === 0 && minutesValid && lines.length <= 100;
  return (
    <div className="rounded border border-neutral-300 p-3 dark:border-neutral-700">
      <p className="mb-2 flex items-center font-medium">
        Calendário macro
        <Help text="Eventos que você anota (FOMC, CPI, payroll), em horário UTC. Perto de cada um, as estratégias de day trade não abrem posição; as swing e as saídas seguem normais. É uma convenção do operador: o teste histórico não replica estas pausas." />
      </p>
      <p className="mb-2 text-neutral-500">
        {current.length ? `Pausa de ${status.macro_pause_minutes ?? 15} min antes e depois de: ${current.join(", ")}` : `Nenhum evento. Pausa de ${status.macro_pause_minutes ?? 15} min antes e depois de cada um.`}
      </p>
      <textarea
        className={`block w-full border bg-transparent px-2 py-1 ${badLines.length ? "border-red-600" : ""}`}
        rows={3}
        placeholder="um por linha, em UTC: 2026-11-04T18:00"
        value={events}
        onChange={(event) => setEvents(event.target.value)}
        aria-label="Eventos macro em UTC, um por linha"
      />
      {badLines.length > 0 && <p className="text-red-600">Data inválida: {badLines.join(", ")}</p>}
      <p className="mt-2 flex flex-wrap items-center gap-2">
        pausa de
        <input
          className="w-16 border bg-transparent px-2 py-1"
          placeholder={String(status.macro_pause_minutes ?? 15)}
          value={minutes}
          onChange={(event) => setMinutes(event.target.value)}
          aria-label="Minutos de pausa em volta de cada evento"
        />
        min (até 180)
        <button
          className="underline disabled:opacity-50"
          type="button"
          disabled={!valid}
          onClick={() =>
            void perform("Salvar calendário macro", () => api.updateConfig(payload), () =>
              lines.length ? `${lines.length} evento(s) macro salvos` : "Pausa dos eventos macro salva",
            ).then((outcome) => {
              // A refused list (422) stays in the box, so the operator can fix it instead of retyping it.
              if (!outcome.ok) return;
              setEvents("");
              setMinutes("");
            })
          }
        >
          {lines.length ? "substituir a lista" : "salvar pausa"}
        </button>
        {current.length > 0 && (
          <button
            className="underline"
            type="button"
            onClick={() => {
              if (!window.confirm(`Limpar a lista?\n\nRemove ${current.length === 1 ? "o evento" : `os ${current.length} eventos`} do calendário macro.`)) return;
              void perform("Limpar o calendário macro", () => api.updateConfig({ macro_events_utc: [] }));
            }}
          >
            limpar a lista
          </button>
        )}
      </p>
    </div>
  );
}

function ValidationControl({ job, validated, onStart }: { job?: ValidationJob; validated: boolean; onStart: () => void }) {
  if (job?.status === "running") {
    const progress =
      job.stage === "calculando" ? "calculando" : job.stage === "na fila" ? "na fila" : `baixando ${job.done}/${job.total} pares`;
    return <span className="ml-1 text-neutral-500">validando… {progress}</span>;
  }
  const verdict =
    job?.status === "error"
      ? ` (falhou: ${job.error})`
      : job?.result
        ? ` (${job.result.passed ? "aprovada" : "reprovada"}: Sharpe ${job.result.sharpe.toFixed(2)}, ${job.result.wins}/${job.result.trades} acertos, drawdown ${(job.result.max_drawdown * 100).toFixed(0)}%${job.result.reason ? `; ${job.result.reason}` : ""})`
        : "";
  return (
    <>
      <button className="ml-1 underline" type="button" onClick={onStart}>
        {validated ? "validada · refazer teste" : "validar no histórico"}
      </button>
      {verdict && <span className="ml-1 text-neutral-500">{verdict}</span>}
    </>
  );
}
