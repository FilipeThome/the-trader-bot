# Origem dos módulos portados

Commit de origem no Vibe-Trading: `d7f1b9e22b42c10197497a8e2cac4f0e2cc9ca1e`.

Cada arquivo portado começa com esse identificador. O diff trimestral contra o Vibe-Trading deve usar esse commit como base e só então os commits posteriores daquele repositório.

| Módulo no bot | Origem |
| --- | --- |
| `bot/exchange/` | `agent/src/trading/connectors/binance/` |
| `bot/exchange/execution/` | `agent/src/trading/connectors/binance/execution/` |
| `bot/exchange/types.py` | `agent/src/trading/types.py` (somente `TradingProfile`) |
| `bot/governance/ledger.py` | `agent/src/governance/ledger.py` |
| `bot/live/halt.py`, `paths.py`, `audit.py`, `daily_count.py`, `pending_action.py`, `classification.py`, `enforcement.py`, `sdk_order_gate.py` | `agent/src/live/` |
| `bot/live/mandate/` | `agent/src/live/mandate/model.py`, `store.py` |
| `bot/tools/redaction.py` | `agent/src/tools/redaction.py` |

Adições deste repositório, em cima do porte: perfil `binance-live-trade-bot` (escrita só com latch e mandato), ordens `stop_loss_limit` e `take_profit_limit`, e o ciclo `collector` → `evaluator` → `risk` → `executor`. Os perfis `binance-live-trade` e `binance-live-sdk-readonly` continuam sem escrita.
