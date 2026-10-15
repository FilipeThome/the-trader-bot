from __future__ import annotations

from bot.store import Store


def test_store_survives_restart(tmp_path) -> None:
    path = tmp_path / "bot.sqlite"
    store = Store(path)
    store.set_state("running", True)
    store.replace_opportunities(
        [
            {
                "symbol": "BTC/USDT",
                "strategy": "trend",
                "action": "Comprar",
                "score": 0.82,
                "net_return": 0.024,
                "risk_pct": 0.011,
                "status": "pronta",
                "reason": "rompimento",
            }
        ]
    )
    store.upsert_position(
        {
            "symbol": "BTC/USDT",
            "strategy": "trend",
            "side": "buy",
            "qty": 0.01,
            "avg_price": 100.0,
            "status": "open",
        }
    )
    store.close()

    restored = Store(path)
    assert restored.get_state("running") is True
    assert restored.opportunities()[0]["symbol"] == "BTC/USDT"
    assert restored.open_positions()[0]["qty"] == 0.01
    restored.close()
