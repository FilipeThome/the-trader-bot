"""Profile types kept so the ported Binance connector keeps its contract."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Environment = Literal["paper", "live"]
Transport = Literal["local_tws", "remote_mcp", "broker_sdk", "local_plugin"]

READ_CAPABILITIES = (
    "account.read",
    "positions.read",
    "orders.read",
    "quotes.read",
    "history.read",
)


@dataclass(frozen=True)
class TradingProfile:
    """A user-selectable trading connector profile."""

    id: str
    connector: str
    label: str
    environment: Environment
    transport: Transport
    capabilities: tuple[str, ...]
    readonly: bool
    config: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def to_dict(self, *, selected: bool = False) -> dict[str, Any]:
        payload = asdict(self)
        payload["capabilities"] = list(self.capabilities)
        payload["selected"] = selected
        return payload
