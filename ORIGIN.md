# Origem dos módulos

Base publicada: Vibe-Trading `d7f1b9e22b42c10197497a8e2cac4f0e2cc9ca1e` (HKUDS/Vibe-Trading). Nesse commit o conector Binance é **somente leitura** ("Order placement is not exposed here"). Todo o caminho de escrita na Binance que este repositório usa não está em nenhum commit do Vibe-Trading: ele foi escrito num checkout local do Vibe-Trading, sem commit, e trazido para cá.

Para comparar com a base, use o commit, nunca a árvore de trabalho de um checkout local:

```powershell
git -C <checkout do Vibe-Trading> show d7f1b9e22b42c10197497a8e2cac4f0e2cc9ca1e:agent/src/live/halt.py
```

O `main` do GitHub divergiu de `d7f1b9e` (base comum `d71b28c74`). Em 2026-10-15 nenhum arquivo do conector Binance, do ledger ou do gate mudou lá. Em `enforcement.py`, o `main` só traz a recusa de opções, que já está aqui.

## Iguais ao commit, salvo os caminhos de import

Cabeçalho `# Ported from Vibe-Trading d7f1b9e…`.

| Módulo no bot | Origem em `d7f1b9e` |
| --- | --- |
| `bot/exchange/classification.py`, `usdm.py` | `agent/src/trading/connectors/binance/` |
| `bot/exchange/types.py` | `agent/src/trading/types.py` (somente `TradingProfile` e `READ_CAPABILITIES`) |
| `bot/live/halt.py`, `paths.py`, `audit.py`, `daily_count.py`, `pending_action.py`, `classification.py`, `enforcement.py` | `agent/src/live/` |
| `bot/live/mandate/model.py`, `store.py` | `agent/src/live/mandate/` |
| `bot/tools/redaction.py` | `agent/src/tools/redaction.py` |

## Baseados no commit e alterados aqui

Cabeçalho `# Based on Vibe-Trading d7f1b9e…`.

| Módulo | O que mudou |
| --- | --- |
| `bot/exchange/sdk.py` | Escrita de ordens (`place_order`, `cancel_order`, latch de escrita, `_reject_write_surface`), perfil `live-trade-bot`, ordens `stop_loss_limit`/`take_profit_limit`, `config_path` na raiz base, campos `average`/`cost`/`fees` no retorno, `.env` via `python-dotenv` |
| `bot/exchange/profiles.py` | Perfis de escrita `binance-paper-trade` e `binance-live-trade-bot`, `profile_by_id` |
| `bot/exchange/shaping.py` | `average` e `cost` em `order_to_dict` |
| `bot/exchange/__init__.py` | Docstring |
| `bot/governance/ledger.py` | `strict_fsync`, recusa de registro sem quebra de linha no fim; guardas de `None` para o mypy; `rotate_if_needed` verifica o arquivo ativo a partir do último segmento (antes a segunda rotação falhava) e nunca renomeia por cima de um segmento; o export inclui os segmentos selados |
| `bot/live/sdk_order_gate.py` | Releitura do halt antes do ALLOW e `gate_allowed`, que só vale dentro do ALLOW; guardas de `None` para o mypy (quantidade ausente nega, como as outras falhas de dimensionamento) |

## Código próprio, fora de qualquer commit do Vibe-Trading

Cabeçalho `# First-party code of this repository…`. É revisado e tipado (mypy estrito) como código deste repositório.

- `bot/exchange/_filelock.py`
- `bot/exchange/execution/` (`controls`, `journal`, `lifecycle`, `orchestrate`, `preflight`, `reconcile`, `rules`)

## Adições deste repositório

O perfil `binance-live-trade-bot` escreve só dentro do ALLOW do gate de mandato, com latch e mandato válido. Os perfis `binance-live-trade` e `binance-live-sdk-readonly` continuam sem escrita. O ciclo é `collector` → `evaluator` → `risk` → `executor`. A recuperação do journal fica em `python -m bot.ops`.
