"""Position state machine. The connector is reached only through the service gate."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from bot.evaluator import Opportunity
from bot.exchange import service
from bot.exchange.weight import weight_pressure
from bot.store import Store

_ENTRY = "pronta"
_REDUCING = frozenset({"em_posicao"})


def idempotency_key(symbol: str, strategy: str, action: str, day: str) -> str:
    raw = f"{symbol}|{strategy}|{action}|{day}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


class Executor:
    def __init__(self, store: Store, profile_id: str) -> None:
        self.store = store
        self.profile_id = profile_id

    def act(
        self,
        opportunities: list[Opportunity],
        *,
        clock_blocked: bool,
        manual: bool = False,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        pressure = weight_pressure() or clock_blocked
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for opportunity in opportunities:
            if opportunity.status not in {_ENTRY, *_REDUCING}:
                continue
            reducing = opportunity.direction < 0 or opportunity.action.lower().startswith("sair")
            if pressure and opportunity.status == _ENTRY and not reducing:
                self.store.add_decision(
                    {
                        "symbol": opportunity.symbol,
                        "strategy": opportunity.strategy,
                        "decision": "nao_operar",
                        "reason": "peso da API ou relógio fora de 1s — só cancelas e saídas",
                        "manual": manual,
                    }
                )
                continue
            if opportunity.status != _ENTRY and opportunity.status not in _REDUCING:
                continue
            if opportunity.status == "em_posicao" and "subir stop" not in opportunity.action.lower() and not reducing:
                continue
            key = idempotency_key(opportunity.symbol, opportunity.strategy, opportunity.action, day)
            order_type = "market"
            kwargs: dict[str, Any] = {
                "side": "buy" if opportunity.direction > 0 else "sell",
                "notional": f"{opportunity.size_usd:.2f}",
                "order_type": order_type,
                "idempotency_key": key,
                "session_id": "bot",
            }
            if opportunity.stop_price and opportunity.status == "em_posicao":
                held = next(
                    (
                        row
                        for row in self.store.open_positions()
                        if row["symbol"] == opportunity.symbol and row["strategy"] == opportunity.strategy
                    ),
                    None,
                )
                qty = float(held["qty"]) if held else 0.0
                if qty <= 0:
                    continue
                kwargs["order_type"] = "stop_loss_limit"
                kwargs["stop_price"] = f"{opportunity.stop_price:.8f}".rstrip("0").rstrip(".")
                kwargs["limit_price"] = kwargs["stop_price"]
                kwargs.pop("notional", None)
                kwargs["quantity"] = f"{qty:.8f}".rstrip("0").rstrip(".")
            result = service.submit_order(opportunity.symbol, self.profile_id, **kwargs)
            decision = "operar" if result.get("status") == "ok" else "bloqueado"
            self.store.add_decision(
                {
                    "symbol": opportunity.symbol,
                    "strategy": opportunity.strategy,
                    "decision": decision,
                    "reason": result.get("error") or opportunity.reason,
                    "manual": manual,
                    "idempotency_key": key,
                    "exchange": {k: result.get(k) for k in ("status", "order_id", "reasons")},
                }
            )
            if result.get("status") == "ok" and opportunity.status == _ENTRY:
                price = float(result.get("price") or 0) or None
                self.store.upsert_position(
                    {
                        "symbol": opportunity.symbol,
                        "strategy": opportunity.strategy,
                        "side": kwargs["side"],
                        "qty": float(result.get("amount") or 0),
                        "avg_price": price or 0,
                        "stop_price": opportunity.stop_price,
                        "target_price": opportunity.target_price,
                        "status": "open",
                        "client_order_id": result.get("client_order_id"),
                    }
                )
            results.append(result)
        return results

    def cancel_open_orders(self) -> list[dict[str, Any]]:
        cancelled = []
        for position in self.store.open_positions():
            order_id = position.get("client_order_id") or ""
            if not order_id:
                continue
            cancelled.append(
                service.cancel_order(str(order_id), self.profile_id, symbol=position["symbol"])
            )
        return cancelled

    def flatten(self) -> list[dict[str, Any]]:
        closed = []
        for position in self.store.open_positions():
            side = "sell" if position["side"] == "buy" else "buy"
            result = service.submit_order(
                position["symbol"],
                self.profile_id,
                side=side,
                quantity=f"{position['qty']:.8f}".rstrip("0").rstrip("."),
                order_type="market",
                idempotency_key=idempotency_key(position["symbol"], position["strategy"], "flatten", "halt"),
                session_id="halt",
            )
            if result.get("status") == "ok":
                self.store.close_position(position["symbol"], position["strategy"], float(position.get("realized_pnl") or 0))
            closed.append(result)
        return closed
