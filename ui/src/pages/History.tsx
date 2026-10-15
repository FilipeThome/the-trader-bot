import { Help } from "../Help";
import { decisionName, strategyName } from "../labels";
import { useBot } from "../store";

export function History() {
  const history = useBot((state) => state.history);
  return (
    <section>
      <table className="w-full text-left text-sm">
        <thead className="text-neutral-500">
          <tr>
            <th className="pr-3 font-medium">
              Quando<Help text="Data e hora em que o robô registrou a decisão." />
            </th>
            <th className="pr-3 font-medium">
              Par<Help text="A moeda da decisão. Fica vazio quando a decisão vale para o robô inteiro, não para um par." />
            </th>
            <th className="pr-3 font-medium">
              Estratégia<Help text="O método ligado a essa decisão: Trend, Mean Reversion ou Funding Carry." />
            </th>
            <th className="pr-3 font-medium">
              Decisão<Help text="O que foi resolvido. Não operar significa que o robô olhou e preferiu ficar de fora." />
            </th>
            <th className="font-medium">
              Motivo<Help align="end" text="Por que o robô tomou essa decisão, em uma frase." />
            </th>
          </tr>
        </thead>
        <tbody>
          {history.map((row, index) => (
            <tr key={index} className="border-t border-neutral-200 dark:border-neutral-800">
              <td className="pr-3">{String(row.ts ?? "")}</td>
              <td className="pr-3">{String(row.symbol ?? "") || "—"}</td>
              <td className="pr-3">{strategyName(String(row.strategy ?? ""))}</td>
              <td className="pr-3">{decisionName(String(row.decision ?? ""))}</td>
              <td>{String(row.reason ?? "")}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {history.length === 0 && <p className="mt-4 text-sm text-neutral-500">Nenhuma decisão registrada neste ambiente.</p>}
    </section>
  );
}
