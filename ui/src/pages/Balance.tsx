import { useCallback, useEffect, useState } from "react";

import { api } from "../api";
import { money } from "../App";
import { Help } from "../Help";
import type { Balance as BalanceBody } from "../types";

const DUST_USDT = 1;

function amount(value: number): string {
  return value.toLocaleString("pt-BR", { maximumFractionDigits: 8 });
}

export function Balance() {
  const [body, setBody] = useState<BalanceBody | null>(null);
  const [error, setError] = useState("");
  const [showDust, setShowDust] = useState(false);

  const pull = useCallback(async () => {
    try {
      setBody(await api.balance());
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "saldo indisponível");
    }
  }, []);

  useEffect(() => {
    void pull();
    const timer = window.setInterval(() => void pull(), 30000);
    return () => window.clearInterval(timer);
  }, [pull]);

  const rows = (body?.balances ?? []).filter(
    (row) => showDust || row.value_usdt === null || row.value_usdt >= DUST_USDT,
  );
  const hidden = (body?.balances.length ?? 0) - rows.length;

  return (
    <section className="text-sm">
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <p className="inline-flex items-center text-base">
          {body ? (
            <>
              Total estimado <strong className="ml-1">{money(body.total_usdt)}</strong>
              <span className="ml-2 text-neutral-500">{body.is_testnet ? "Spot Testnet" : "Produção"}</span>
            </>
          ) : (
            "Saldo da Binance"
          )}
          <Help text="Soma dos saldos desta conta Binance, avaliados pelo último preço em USDT. Moedas estáveis (USDT, USDC, FDUSD) contam 1 para 1. No testnet o saldo é de ensaio, sem valor real." />
        </p>
        <button className="underline" type="button" onClick={() => void pull()}>
          atualizar
        </button>
        <label className="inline-flex items-center gap-1">
          <input type="checkbox" checked={showDust} onChange={(event) => setShowDust(event.target.checked)} />
          mostrar valores abaixo de {money(DUST_USDT)}
        </label>
      </div>
      {error && <p className="mb-3 text-red-600">{error}</p>}
      {body && (
        <>
          <table className="w-full text-left">
            <thead className="text-neutral-500">
              <tr>
                <th className="pr-3 font-medium">Moeda</th>
                <th className="pr-3 font-medium">
                  Livre<Help text="Pode ser usado em uma ordem nova agora." />
                </th>
                <th className="pr-3 font-medium">
                  Em ordens<Help text="Travado em ordens abertas, como o stop que o robô deixa na Binance." />
                </th>
                <th className="pr-3 font-medium">Total</th>
                <th className="pr-3 font-medium">Preço (USDT)</th>
                <th className="font-medium">Valor (USDT)</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.asset} className="border-t border-neutral-200 dark:border-neutral-800">
                  <td className="pr-3 font-medium">{row.asset}</td>
                  <td className="pr-3">{amount(row.free)}</td>
                  <td className="pr-3">{amount(row.used)}</td>
                  <td className="pr-3">{amount(row.total)}</td>
                  <td className="pr-3">{row.price_usdt === null ? "—" : amount(row.price_usdt)}</td>
                  <td>{row.value_usdt === null ? "sem par USDT" : money(row.value_usdt)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="mt-3 text-neutral-500">
            Atualizado em {new Date(body.updated_at).toLocaleTimeString("pt-BR")}
            {hidden > 0 && ` · ${hidden} moeda(s) abaixo de ${money(DUST_USDT)} escondida(s)`}
            {body.unpriced > 0 && ` · ${body.unpriced} sem preço em USDT, fora do total`}
          </p>
        </>
      )}
    </section>
  );
}
