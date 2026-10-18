"""Environment switch. Production is fail-closed; leaving production is always allowed.

A full promotion needs 30 days of testnet history with a positive net PnL. A
pilot skips only that history: it needs every other check and a production
capital at or below ``pilot_max_production_capital_usd`` from bot.json. The
cap of a pilot is recorded in the production store at the switch
(``PILOT_STATE``) and enforced where the mandate is written, so editing
bot.json later cannot loosen it.
"""

from __future__ import annotations

from bot.config import BotConfig
from bot.store import Store

#: Production store key holding the capital cap of a pilot; absent or None after a full promotion.
PILOT_STATE = "pilot_cap_usd"
HISTORY_DAYS = 30


def production_blockers(
    config: BotConfig,
    store: Store,
    *,
    confirmation: str,
    bot_stopped: bool,
    open_orders: int,
    mandate_ok: bool,
    keys_ok: bool,
) -> tuple[list[str], float | None]:
    """Reasons the switch is refused, and the pilot cap when only the history is missing.

    The cap may come back with other reasons; only an empty list makes it a pilot switch.
    """
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
    # Applies to a pilot too. Nothing writes this counter yet (mandate breaches go to the
    # audit log), so today it only blocks when set by hand.
    breaches = int(store.get_state("limit_violations", 0) or 0)
    if breaches:
        reasons.append("há violações de limite no testnet")
    history = _history_blockers(store)
    if not history:
        return reasons, None
    cap = config.pilot_max_production_capital_usd
    capital = config.production_capital()
    if cap > 0 and capital <= cap:
        return reasons, cap
    reasons.extend(history)
    if cap > 0:
        reasons.append(f"piloto: capital de produção de US$ {capital:.2f} acima do limite de US$ {cap:.2f}")
    return reasons, None


def _history_blockers(store: Store) -> list[str]:
    days = store.pnl_days()
    if len(days) < HISTORY_DAYS:
        return [f"são necessários {HISTORY_DAYS} dias de testnet"]
    window = days[-HISTORY_DAYS:]
    reasons: list[str] = []
    if sum(float(row["net_pnl"]) for row in window) <= 0:
        reasons.append(f"PnL líquido dos últimos {HISTORY_DAYS} dias não é positivo")
    if sum(int(row["violations_avoided"]) for row in window) < 0:
        reasons.append("histórico de violações ilegível")
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
