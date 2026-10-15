"""Environment switch. Production is fail-closed; leaving production is always allowed."""

from __future__ import annotations

from bot.config import BotConfig
from bot.store import Store


def production_blockers(
    config: BotConfig,
    store: Store,
    *,
    confirmation: str,
    bot_stopped: bool,
    open_orders: int,
    mandate_ok: bool,
    keys_ok: bool,
) -> list[str]:
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
    days = store.pnl_days()
    if len(days) < 30:
        reasons.append("são necessários 30 dias de testnet")
    else:
        window = days[-30:]
        net = sum(float(row["net_pnl"]) for row in window)
        violations = sum(int(row["violations_avoided"]) for row in window)
        # Violations avoided are good. A stored breach count would be separate.
        breaches = int(store.get_state("limit_violations", 0) or 0)
        if net <= 0:
            reasons.append("PnL líquido dos últimos 30 dias não é positivo")
        if breaches:
            reasons.append("há violações de limite no testnet")
        if violations < 0:
            reasons.append("histórico de violações ilegível")
    return reasons


def switch_to_production(config: BotConfig, store: Store, **checks: object) -> tuple[BotConfig, list[str]]:
    reasons = production_blockers(config, store, **checks)  # type: ignore[arg-type]
    if reasons:
        config.mode = "testnet"
        return config, reasons
    config.mode = "production"
    return config, []
