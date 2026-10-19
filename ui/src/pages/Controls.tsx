import { useState } from "react";

import { api } from "../api";
import { Help } from "../Help";
import { strategyName } from "../labels";
import { parseDecimal, parseWhole } from "../numbers";
import { useBot } from "../store";
import type { ValidationJob } from "../types";

const strategyHelp: Record<string, string> = {
  trend: "Trend segue o movimento quando o preço rompe a máxima ou a mínima recente. A ideia é acompanhar a tendência, não adivinhar a virada. Só opera depois que o teste histórico libera.",
  momentum: "Momentum compra o que vem subindo de forma constante: alta nos últimos 20 dias em velas de 4h, média de 20 acima da de 50 e tendência firme (ADX). Seguir a tendência só na compra é o efeito com mais evidência em cripto. O stop fica a 2,5 vezes a volatilidade e a posição sai pelo stop móvel ou quando a média de 20 cruza abaixo da de 50. Só opera depois que o teste histórico (365 dias) libera.",
  mean_reversion: "Mean Reversion procura preço que se afastou demais da média e pode voltar. Funciona melhor quando o mercado está de lado. Só opera depois que o teste histórico libera.",
  funding_carry: "Funding Carry compara o custo de segurar um contrato futuro com o preço à vista. Nesta fase o robô só mostra o sinal: ele não envia essa operação.",
  intraday_momentum: "Day trade. Compra no 15 min quando o preço rompe a máxima das últimas 8 velas com volume 1,5x acima do normal, acima da VWAP do dia, e só com o 1h em alta. Stop de 1,5 ATR (no mínimo 1%). Vende metade em 1,5x o risco e leva o stop do resto para o preço de entrada mais as taxas; o que sobrar sai em até 6 horas. Não entra entre 03:00 e 07:00 UTC. Só opera depois do teste histórico (90 dias).",
  vwap_pullback: "Day trade. Compra no 15 min quando o preço recua abaixo da VWAP do dia ou da média de 20 e volta para cima, com RSI entre 35 e 55, e só com o 1h em alta. Mesmas regras de saída do momento intradiário: metade em 1,5x o risco, stop no equilíbrio, saída em até 6 horas. Só opera depois do teste histórico (90 dias).",
};

export function Controls() {
  const status = useBot((state) => state.status);
  const perform = useBot((state) => state.perform);
  const busy = useBot((state) => state.busy);
  const [capital, setCapital] = useState("");
  const [confirmation, setConfirmation] = useState("");
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
      <p className="flex items-center">
        <span>
          Ambiente atual: <strong>{status.mode === "production" ? "Produção" : "Spot Testnet"}</strong>. Capital em uso{" "}
          {status.capital_usd} de {status.allocated_capital_usd} USD.
          {status.pilot_cap_usd != null && (
            <strong className="ml-1 text-amber-600"> Produção piloto: capital limitado a US$ {status.pilot_cap_usd}, sem os 30 dias de testnet.</strong>
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
          : "Ligado, o robô compra sozinho as melhores oportunidades Prontas, da maior nota para a menor, dentro do saldo e dos limites de risco, e cuida do stop e da saída. Só entram estratégias validadas no histórico com taxa de acerto acima do mínimo (no day trade, acima do ponto de equilíbrio do TP1 mais uma margem). Desligar volta ao manual: as posições abertas continuam com stop e saídas."} />
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
          <Help text="Desliga o ciclo. Não cancela ordens que já foram à corretora e não fecha posições." />
        </span>
        <span className="inline-flex items-center">
          <button className="rounded bg-red-600 px-3 py-2 text-white" type="button" onClick={() => void run("Parar tudo", api.halt)}>
            Parar tudo
          </button>
          <Help text="Parada de emergência. Para o ciclo e não volta sozinho. Com a opção de zerar marcada, vende as posições antes de travar. Sem ela, as posições ficam abertas com o stop na Binance." />
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
          <Help text="Quanto dinheiro, em dólares, você coloca à disposição do robô. Cada ordem nova usa no máximo 2% desse valor." />
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
              const text = `Aplicar o preset de day trade?\n\n${lines.join("\n") || "nada muda"}${cap}\nMandato: até ${preview.mandate_orders_per_day} ordens por dia.`;
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
          {((status.limits.risk_per_trade_pct ?? 0.01) * 100).toFixed(1)}% por trade · até {((status.limits.max_open_risk_pct ?? 0.06) * 100).toFixed(0)}% somando as posições · no máximo {status.limits.max_positions} posições · ordem de até US$ {(status.capital_usd * (status.limits.max_order_notional_pct ?? 0.02)).toFixed(0)} · lucro esperado mínimo de US$ {status.min_profit_usd.toFixed(2)} · Kelly {((status.limits.kelly_fraction ?? 0.25) * 100).toFixed(0)}% · até {status.limits.max_entries_per_cycle ?? 2} entradas por ciclo
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
            onClick={() =>
              void run(
                "Salvar alvo e stop fixos",
                () => api.updateConfig({ take_profit_pct: (takeProfitPct ?? 0) / 100, stop_loss_pct: (stopLossPct ?? 0) / 100 }),
                () => `Alvo +${takeProfitPct}% e stop −${stopLossPct}%; as estratégias ligadas estão sendo validadas`,
              )
            }
          >
            {fixedOn ? "salvar" : "ligar"}
          </button>
          {fixedOn && (
            <button
              className="underline"
              type="button"
              onClick={() => void run("Desligar alvo e stop fixos", () => api.updateConfig({ take_profit_pct: null, stop_loss_pct: null }))}
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
            onChange={(event) => { const on = event.target.checked; void run(on ? "Ligar o stop móvel" : "Desligar o stop móvel", () => api.updateConfig({ trailing_stop: on })); }}
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
            onChange={(event) => { const on = event.target.checked; void run(on ? "Ligar a ativação do stop móvel" : "Desligar a ativação do stop móvel", () => api.updateConfig({ trailing_activation: on })); }}
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
            onChange={(event) => { const on = event.target.checked; void run(on ? "Zerar no halt ligado" : "Zerar no halt desligado", () => api.updateConfig({ flatten_on_halt: on })); }}
          />{" "}
          Zerar posições no halt
        </label>
        <Help text="Se estiver marcado, a parada de emergência vende o que estiver aberto antes de travar. Desmarcado, as posições ficam com o stop na Binance." />
      </p>
      <div className="rounded border border-neutral-300 p-3 dark:border-neutral-700">
        <p className="mb-2 flex items-center font-medium">
          Switch de ambiente
          <Help text="Troca o ensaio pela conta real, ou volta ao ensaio. Ir para produção exige o robô parado, sem posição aberta, chaves válidas, mandato só de cripto, as quatro liberações gravadas no bot.json e a palavra PRODUCAO escrita igual. Se faltar qualquer coisa, ele permanece no testnet." />
        </p>
        <input
          className="mb-2 block w-full border bg-transparent px-2 py-1"
          placeholder="digite PRODUCAO para promover"
          value={confirmation}
          onChange={(event) => setConfirmation(event.target.value)}
          aria-label="Confirmação para produção"
        />
        <p className="flex flex-wrap items-center gap-3">
          <span className="inline-flex items-center">
            <button className="underline" type="button" onClick={() => void run("Ir para Produção", () => api.updateConfig({ mode: "production", confirmation }))}>
              Ir para Produção
            </button>
            <Help text="Pede a troca para a conta real. Sem a palavra PRODUCAO e sem as liberações, a tela mostra o motivo e continua no Spot Testnet." />
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
