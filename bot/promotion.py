"""Environment switch. Production is fail-closed; leaving production is always allowed.

A full promotion needs the testnet history of the last 30 days: 30 days of PnL with
a positive net, at least ``MIN_LIVE_TRADES`` closed strategy trades with a recorded
R, a positive mean R, and a Monte Carlo p95 drawdown (in R, times the risk per
trade) below the drawdown limit. A pilot skips only that history: it needs every
other check and a production capital at or below the pilot cap, either
``pilot_max_production_capital_usd`` from bot.json or the cap the panel sends with
the switch (``pilot_cap``). The cap of a pilot is recorded in the production store
at the switch (``PILOT_STATE``) and enforced where the mandate is written, so
editing bot.json later cannot loosen it. A pilot may also ask to operate its
strategies without a stored backtest validation (``PILOT_SKIP_VALIDATION_STATE``,
read by the cycle's signal-only rule); only the switch writes that key.

Both need a testnet without quantitative mandate breaches in the last 30 days, read
from the testnet's live-action audit ledger (D22). The ledger is only read here.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bot.backtest.stats import mc_drawdown_r
from bot.config import BotConfig, RiskLimits
from bot.live.audit import audit_ledger_path
from bot.store import Store

#: Production store key holding the capital cap of a pilot; absent or None after a full promotion.
PILOT_STATE = "pilot_cap_usd"
#: Production store key: a pilot's enabled strategies trade without a stored backtest validation.
#: False or absent after a full promotion. Only the switch writes it.
PILOT_SKIP_VALIDATION_STATE = "pilot_skip_validation"
HISTORY_DAYS = 30
#: Closed testnet trades with a recorded R a full promotion needs within ``HISTORY_DAYS``.
MIN_LIVE_TRADES = 30
#: Days of the testnet audit ledger read for breaches.
BREACH_DAYS = 30


def audit_breaches(since: datetime, *, path: Path | None = None) -> int:
    """Quantitative mandate breaches recorded at or after ``since`` in a live-action audit ledger.

    A breach is a ``kind == "breach"`` record (``bot.live.audit.LiveActionEvent``)
    whose ``gate_decision["kind"]`` is ``"quantitative"``; a record without that
    field counts too (fail-closed). Structural breaches (``universe``,
    ``instrument``) are refusals of an order the mandate never allows, not a limit
    the bot ran into. ``path`` defaults to the ledger of the pinned environment; a
    missing file has no breach. Raises ``ValueError`` when a line is not a record or
    a breach has no readable ``ts``, and ``OSError`` when the file cannot be read.
    """
    ledger = path or audit_ledger_path()
    if not ledger.exists():
        return 0
    cutoff = _utc(since)
    count = 0
    with ledger.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except ValueError as exc:
                raise ValueError(f"audit line {number} is not JSON") from exc
            if not isinstance(record, dict):
                raise ValueError(f"audit line {number} is not a record")
            if record.get("kind") != "breach":
                continue
            gate = record.get("gate_decision")
            if isinstance(gate, dict) and gate.get("kind", "quantitative") != "quantitative":
                continue
            try:
                stamp = _utc(datetime.fromisoformat(str(record["ts"])))
            except (KeyError, ValueError) as exc:
                raise ValueError(f"audit line {number} has no readable ts") from exc
            if stamp >= cutoff:
                count += 1
    return count


def audit_ledger_for(store: Store) -> Path:
    """The audit ledger of the environment the testnet ``store`` lives in.

    The store and the ledger share the environment root (``<root>/bot.sqlite`` and
    ``<root>/live/audit.jsonl``), so the switch reads the testnet ledger whatever
    the process pin is.
    """
    return store.path.parent / "live" / audit_ledger_path().name


def production_blockers(
    config: BotConfig,
    store: Store,
    *,
    confirmation: str,
    bot_stopped: bool,
    open_orders: int,
    mandate_ok: bool,
    keys_ok: bool,
    now: datetime | None = None,
    pilot_cap: float | None = None,
) -> tuple[list[str], float | None]:
    """Reasons the switch is refused, and the pilot cap when only the history is missing.

    ``store`` is the testnet store. ``pilot_cap`` is the cap the panel sent with the
    switch; None reads ``pilot_max_production_capital_usd`` from the config. The cap
    may come back with other reasons; only an empty list makes it a pilot switch.
    """
    moment = _utc(now or datetime.now(timezone.utc))
    reasons: list[str] = []
    if not bot_stopped:
        reasons.append("bot precisa estar parado")
    if open_orders:
        reasons.append("há ordens abertas")
    if not keys_ok:
        reasons.append("chaves de produção ausentes ou recusadas por check_status")
    if not mandate_ok:
        reasons.append("mandato inválido ou instrumentos não restritos")
    for gate in ("g1", "g2", "g3", "g4"):
        if not config.gates.get(gate):
            reasons.append(f"gate {gate.upper()} não aprovado")
    if confirmation != "PRODUCAO":
        reasons.append("confirmação deve ser a palavra PRODUCAO")
    # Applies to a pilot too.
    try:
        breaches = audit_breaches(moment - timedelta(days=BREACH_DAYS), path=audit_ledger_for(store))
    except (OSError, ValueError):
        reasons.append("auditoria do testnet ilegível")
    else:
        if breaches:
            reasons.append(f"há violações de limite no testnet: {breaches} nos últimos {BREACH_DAYS} dias")
    history = _history_blockers(store, config.limits, moment)
    if not history:
        return reasons, None
    cap = config.pilot_max_production_capital_usd if pilot_cap is None else pilot_cap
    capital = config.production_capital()
    if cap > 0 and capital <= cap:
        return reasons, cap
    reasons.extend(history)
    if cap > 0:
        reasons.append(f"piloto: capital de produção de US$ {capital:.2f} acima do limite de US$ {cap:.2f}")
    return reasons, None


def _history_blockers(store: Store, limits: RiskLimits, now: datetime) -> list[str]:
    reasons: list[str] = []
    days = store.pnl_days()
    if len(days) < HISTORY_DAYS:
        reasons.append(f"são necessários {HISTORY_DAYS} dias de testnet")
    else:
        window = days[-HISTORY_DAYS:]
        if sum(float(row["net_pnl"]) for row in window) <= 0:
            reasons.append(f"PnL líquido dos últimos {HISTORY_DAYS} dias não é positivo")
    trades = store.closed_trades(since=(now - timedelta(days=HISTORY_DAYS)).isoformat())
    r = [float(trade["r_multiple"]) for trade in trades if trade.get("r_multiple") is not None]
    if len(r) < MIN_LIVE_TRADES:
        reasons.append(
            f"são necessários {MIN_LIVE_TRADES} trades fechados com R registrado nos últimos "
            f"{HISTORY_DAYS} dias (há {len(r)})"
        )
        return reasons
    expectancy = sum(r) / len(r)
    if expectancy <= 0:
        reasons.append(f"expectativa em R dos últimos {HISTORY_DAYS} dias não é positiva ({expectancy:+.2f} R)")
    p95 = mc_drawdown_r(r)
    if p95 * limits.risk_per_trade_pct >= limits.max_drawdown_pct:
        reasons.append(
            f"drawdown p95 do Monte Carlo ({p95:.1f} R × {limits.risk_per_trade_pct:.1%} = "
            f"{p95 * limits.risk_per_trade_pct:.1%}) não fica abaixo do limite de {limits.max_drawdown_pct:.1%}"
        )
    return reasons


def switch_to_production(config: BotConfig, store: Store, **checks: object) -> tuple[BotConfig, list[str], float | None]:
    """The config switched to production, or left on testnet with the reasons. The third value
    is the pilot cap when the switch is a pilot, None for a full promotion."""
    reasons, pilot = production_blockers(config, store, **checks)  # type: ignore[arg-type]
    if reasons:
        config.mode = "testnet"
        return config, reasons, None
    config.mode = "production"
    return config, [], pilot


def _utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)
