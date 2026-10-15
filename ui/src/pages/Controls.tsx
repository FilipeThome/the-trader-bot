import { useState } from "react";

import { api } from "../api";
import { Help } from "../Help";
import { strategyName } from "../labels";
import { useBot } from "../store";

const strategyHelp: Record<string, string> = {
  trend: "Trend segue o movimento quando o preço rompe a máxima ou a mínima recente. A ideia é acompanhar a tendência, não adivinhar a virada. Só opera depois que o teste histórico libera.",
  mean_reversion: "Mean Reversion procura preço que se afastou demais da média e pode voltar. Funciona melhor quando o mercado está de lado. Só opera depois que o teste histórico libera.",
  funding_carry: "Funding Carry compara o custo de segurar um contrato futuro com o preço à vista. Nesta fase o robô só mostra o sinal: ele não envia essa operação.",
};

export function Controls() {
  const status = useBot((state) => state.status);
  const load = useBot((state) => state.load);
  const [capital, setCapital] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const [message, setMessage] = useState("");

  if (!status) return <p>Carregando controles…</p>;

  async function run(action: () => Promise<unknown>) {
    try {
      await action();
      setMessage("");
      await load();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "falhou");
      await load();
    }
  }

  return (
    <section className="max-w-xl space-y-4 text-sm">
      <p className="flex items-center">
        <span>
          Ambiente atual: <strong>{status.mode === "production" ? "Produção" : "Spot Testnet"}</strong>. Capital em uso{" "}
          {status.capital_usd} de {status.allocated_capital_usd} USD.
        </span>
        <Help text="Spot Testnet é o ensaio, sem a conta real. Produção usa dinheiro de verdade e, nessa hora, o robô arrisca só 10% do capital que você alocou. Capital em uso é o que entra na conta do tamanho de cada ordem." />
      </p>
      <div className="flex flex-wrap gap-2">
        <span className="inline-flex items-center">
          <button className="rounded bg-neutral-900 px-3 py-2 text-white dark:bg-neutral-100 dark:text-neutral-900" type="button" onClick={() => void run(api.start)}>
            Iniciar
          </button>
          <Help text="Liga o ciclo. Cerca de uma vez por minuto o robô lê o mercado, dá nota às oportunidades e só envia ordem se ela estiver Pronta e dentro dos limites." />
        </span>
        <span className="inline-flex items-center">
          <button className="rounded border px-3 py-2" type="button" onClick={() => void run(api.stop)}>
            Pausar
          </button>
          <Help text="Desliga o ciclo. Não cancela ordens que já foram à corretora e não fecha posições." />
        </span>
        <span className="inline-flex items-center">
          <button className="rounded bg-red-600 px-3 py-2 text-white" type="button" onClick={() => void run(api.halt)}>
            Parar tudo
          </button>
          <Help text="Parada de emergência. Cancela as ordens abertas e não volta sozinho. As posições ficam abertas, a menos que a opção de zerar esteja marcada." />
        </span>
        <span className="inline-flex items-center">
          <button className="rounded border px-3 py-2" type="button" onClick={() => void run(api.resume)}>
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
          className="underline"
          type="button"
          onClick={() => void run(() => api.updateConfig({ allocated_capital_usd: Number(capital), reconfirm_mandate: true }))}
        >
          Salvar capital e confirmar mandato
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
                onChange={(event) => void run(() => api.updateConfig({ strategies: { [name]: event.target.checked } }))}
              />{" "}
              {strategyName(name)}
            </label>
            <Help text={strategyHelp[name] ?? "Liga ou desliga este método. Desligado, ele não gera oportunidade."} />
          </span>
        ))}
      </div>
      <p className="flex items-center">
        <label>
          <input
            type="checkbox"
            checked={status.flatten_on_halt}
            onChange={(event) => void run(() => api.updateConfig({ flatten_on_halt: event.target.checked }))}
          />{" "}
          Zerar posições no halt
        </label>
        <Help text="Se estiver marcado, a parada de emergência também fecha o que estiver aberto. Desmarcado, ela só cancela ordens que ainda não viraram posição." />
      </p>
      <div className="rounded border border-neutral-300 p-3 dark:border-neutral-700">
        <p className="mb-2 flex items-center font-medium">
          Switch de ambiente
          <Help text="Troca o ensaio pela conta real, ou volta ao ensaio. Ir para produção exige o robô parado, sem ordens abertas, chaves válidas, mandato só de cripto, quatro liberações e a palavra PRODUCAO escrita igual. Se faltar qualquer coisa, ele permanece no testnet." />
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
            <button className="underline" type="button" onClick={() => void run(() => api.updateConfig({ mode: "production", confirmation }))}>
              Ir para Produção
            </button>
            <Help text="Pede a troca para a conta real. Sem a palavra PRODUCAO e sem as liberações, a tela mostra o motivo e continua no Spot Testnet." />
          </span>
          <span className="inline-flex items-center">
            <button className="underline" type="button" onClick={() => void run(() => api.updateConfig({ mode: "testnet" }))}>
              Voltar para Spot Testnet
            </button>
            <Help align="end" text="Volta ao ensaio a qualquer momento. Ordens abertas na produção são canceladas nessa volta." />
          </span>
        </p>
      </div>
      {message && <p className="text-red-600">{message}</p>}
    </section>
  );
}
