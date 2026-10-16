import { useEffect, useState } from "react";
import { Link, useParams } from "react-router";

import { formatPct } from "../App";
import { api } from "../api";
import { Help } from "../Help";
import { TradeChart } from "../LiveCharts";
import { strategyName } from "../labels";
import type { Trade } from "../types";

const statusNames: Record<string, string> = {
  pending: "aguardando execução",
  open: "aberta",
  closed: "fechada",
  void: "anulada",
};

/** One trade on its own chart: price, entry, stop, target and the trailing peak, refreshed every 5 s while it runs. */
export function TradeLive() {
  const { id } = useParams();
  const [trade, setTrade] = useState<Trade | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const positionId = Number(id);
    if (!Number.isInteger(positionId)) {
      setError("trade inválido");
      return;
    }
    let cancel = false;
    let timer = 0;
    // One request at a time: a slow reply can never land after a newer one and undo it.
    let inflight = false;
    async function pull() {
      if (inflight) return;
      inflight = true;
      try {
        const body = await api.trade(positionId);
        if (cancel) return;
        setTrade(body);
        setError("");
        // A finished trade no longer changes, so polling stops.
        if (body.position.status === "closed" || body.position.status === "void") window.clearInterval(timer);
      } catch (reason) {
        if (!cancel) setError(reason instanceof Error ? reason.message : "trade indisponível");
      } finally {
        inflight = false;
      }
    }
    void pull();
    timer = window.setInterval(() => void pull(), 5000);
    return () => {
      cancel = true;
      window.clearInterval(timer);
    };
  }, [id]);

  const position = trade?.position;
  const running = position?.status === "open" || position?.status === "pending";
  return (
    <section className="space-y-4 text-sm">
      <p>
        <Link className="underline" to="/positions">
          ← posições
        </Link>
      </p>
      {error && <p className="text-red-600">{error}</p>}
      {position && trade && (
        <>
          <div className="flex flex-wrap items-center gap-x-6 gap-y-1">
            <span className="text-base font-semibold">
              {position.symbol} · {strategyName(position.strategy)}
              {position.test_only ? <span className="ml-2 rounded bg-amber-200 px-1 text-xs text-amber-900">teste</span> : null}
            </span>
            <span>
              {statusNames[position.status] ?? position.status}
              {running && <span className="ml-2 text-neutral-500">ao vivo · {new Date(trade.updated_at).toLocaleTimeString("pt-BR")}</span>}
            </span>
            {trade.pnl_usd != null && (
              <span className={trade.pnl_usd >= 0 ? "text-green-600" : "text-red-600"}>
                {position.status === "open" ? "Resultado agora" : "Resultado"}: US$ {trade.pnl_usd.toFixed(2)}
                {trade.pnl_pct != null && ` (${formatPct(trade.pnl_pct)})`}
              </span>
            )}
            <Help text="Resultado agora: preço atual menos o preço médio, vezes a quantidade, antes da taxa de saída. Fechada: o resultado realizado gravado na saída." />
          </div>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-1 sm:grid-cols-4">
            <Item label="Quantidade" value={position.qty} />
            <Item label="Entrada" value={position.avg_price || null} />
            <Item label="Preço atual" value={position.status === "closed" ? position.exit_price : trade.last_price} />
            <Item label="Stop" value={position.stop_price} />
            <Item label={position.exit_policy === "trailing" ? "Maior preço (stop móvel)" : "Alvo"} value={position.exit_policy === "trailing" ? position.peak_price : position.target_price} />
            <Item label="Aberta em" value={position.opened_at ? new Date(position.opened_at).toLocaleString("pt-BR") : null} />
            <Item label="Fechada em" value={position.closed_at ? new Date(position.closed_at).toLocaleString("pt-BR") : null} />
            <Item label="Motivo" value={position.reason} />
          </dl>
          <TradeChart trade={trade} />
        </>
      )}
    </section>
  );
}

function Item({ label, value }: { label: string; value: string | number | null | undefined }) {
  return (
    <div>
      <dt className="text-neutral-500">{label}</dt>
      <dd>{value == null || value === "" ? "—" : value}</dd>
    </div>
  );
}
