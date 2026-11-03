# Refatoração por boas práticas e análise com a DeepSeek

Data: 2026-10-28. Branch: `feat/auto-trade-tp-sl`. Desenho aprovado com mudanças por três architects (segurança, arquitetura, validade quantitativa). As mudanças exigidas já estão no texto; a seção 9 as lista com o identificador de cada uma. A seção 10 registra o que foi implementado.

## 1. Ponto de partida

A revisão de 2026-10-20 (`docs/revisao-estrategias-e-bots.md`, seção 11) já levou o robô às práticas que a literatura e os frameworks abertos recomendam: tamanho pelo risco sob tetos, degraus de drawdown, walk-forward com hold-out e Sharpe deflacionado, correlação, histerese, idade do sinal. As decisões D1 a D27 continuam valendo. A única emenda é a do item 3.2 (seção 5): o veredito do gate passa a depender dos limites da carteira, e os limites usados ficam gravados.

Uma auditoria em sete frentes paralelas (arquitetura, sinais e risco, execução, validação, API e painel, segredos e operação, API da DeepSeek) achou o que ainda falta. Este plano junta o que vale o custo em cinco ondas. Cada onda é testada e commitada; a onda 2 é um commit por módulo extraído.

## 2. Princípios

1. **A DeepSeek aconselha; o motor determinístico decide.** O modelo nunca cria uma linha, nunca aumenta um tamanho, nunca transforma uma recusa em entrada e nunca toca o order gate, o mandato, o halt ou os stops. Ele só pode desaconselhar uma entrada automática ou multiplicar o tamanho dela por um fator entre 0,25 e 1.
2. **Fidelidade ao histórico.** Uma resposta de LLM não é reproduzível no backtest. Por isso ela só remove ou reduz entradas, a mesma classe dos filtros que já existem só ao vivo (idade do sinal, piso de acerto). O que a validação mede continua sendo um teto do que o robô faz.
3. **Falha neutra.** Sem chave, sem rede, com orçamento esgotado, resposta inválida, fila cheia ou demora, o robô opera como hoje. A exceção é `require_verdict`, que o operador liga sabendo que uma falha da API para as entradas automáticas.
4. **Medir antes de confiar.** O modo padrão é `off`. O primeiro modo útil é `shadow`: consulta e grava, mas nenhuma ordem muda. A calibração mostra, por veredito, o R de cada linha medido com as regras do backtest. Só com essa evidência o operador liga `filter`.
5. **Nenhuma chamada de rede e nenhuma espera dentro da trava do ciclo.** Sob a trava, o ciclo só consulta o cache de vereditos e enfileira pedidos sem bloquear. A thread do consultor nunca pega a trava do ciclo nem a da API.
6. **Nenhum segredo fora do diretório do operador.** A chave fica em `deepseek.json`, ao lado de `binance.json`, com permissão 0600, nunca em `bot.json`, numa resposta da API, num log, no ledger ou em `advice_log`.

## 3. Onda 1: segurança da execução e dos segredos

| # | Achado | Mudança | Teste |
|---|---|---|---|
| 1.1 (P0) | `sdk._exchange_outcome` classifica a recusa pelo nome da classe da exceção do ccxt. `OrderImmediatelyFillable` (-2010, "Stop price would trigger immediately"), `OrderNotFillable`, `RateLimitExceeded` e `DDoSProtection` caem em "desconhecido": o journal bloqueia toda escrita por cerca de 70 s e um stop que já dispararia fica sem tratamento. | Classificar pela hierarquia: definitivo é `isinstance` de `BadRequest`, `InvalidOrder`, `InsufficientFunds`, `AuthenticationError`, `PermissionDenied`, `AccountSuspended`, `BadSymbol`, `ArgumentsRequired`, `OrderNotFound`, `RateLimitExceeded` ou `DDoSProtection`, com recuo pelos nomes do MRO para as exceções falsas dos testes. `NetworkError` (inclui `InvalidNonce`), `RequestTimeout`, `ExchangeNotAvailable` e o resto continuam desconhecidos. | Subclasse de `InvalidOrder` chamada `OrderImmediatelyFillable` grava `ORDER_REJECTED` e leva `Executor.protect` à saída de emergência no mesmo ciclo; `RequestTimeout` continua `SUBMISSION_UNKNOWN`. |
| 1.2 (P1) | No spot, o ccxt 4.5.76 mapeia -1021 (relógio fora da janela) para `InvalidNonce`, subclasse de `NetworkError`. Hoje isso vira `SUBMISSION_UNKNOWN`: cerca de 70 s sem escrita e um stop não colocado, que leva ao halt na sincronização seguinte. | Em `place_order` e `cancel_order`, só o código -1021 (`InvalidNonce` ou nome da classe, mais `"code":-1021` na mensagem) remede o relógio (`load_time_difference`; os 1000 ms extras só quando a mensagem diz "ahead of the server", como na leitura). Antes de reenviar, `_unsent_before_write` roda de novo (trava, superfície de escrita, mandato, halt); se bloquear, devolve o resultado da primeira tentativa com `request_sent=True`. Reenvia no máximo uma vez, com os mesmos parâmetros e o mesmo `newClientOrderId`, dentro do mesmo registro `SUBMISSION_PENDING`. Um segundo -1021 continua desconhecido. -1022 (assinatura inválida) não é reenviado. | (1) Falso levanta `InvalidNonce` com -1021 uma vez e aceita: um `ORDER_SUBMITTED`, relógio remedido, mesmo `newClientOrderId` nas duas chamadas. (2) -1022 não reenvia e grava `ORDER_REJECTED`. (3) Halt ligado entre as duas tentativas: uma única chamada a `create_order`. |
| 1.3 (P1) | Logs, `halt_reason` e erros da API guardam `str(exc)` cru. O ccxt põe a URL assinada (`signature=`) na mensagem e no traceback; um erro do httpx da DeepSeek pode ecoar a URL. A redação existe, mas só o ledger de auditoria a usa. | `bot/logging_setup.py`: um `logging.Formatter` que passa o registro inteiro já formatado (incluindo `exc_text` e `stack_info`) por `redact_text` e por padrões próprios (`signature=`, `sk-...`, `Bearer ...`, `api_key=`). Instalado em todo handler do logger raiz e nos do uvicorn, em `bot/__main__.py` e na entrada do servidor da API. `bot/tools/redaction.py` (vendorizado, idêntico ao commit de origem) não muda. `Runtime.halt`, os `error` devolvidos à API e o `tick_error` passam pela mesma função. | `tests/test_bot_redaction.py`: `logging.getLogger('bot.x').exception(...)` com `signature=abc` e `sk-` + 32 caracteres na mensagem; a saída do handler não contém nenhum dos dois. Texto livre, `deepseek_api_key=` e `Authorization: Bearer sk-...` são limpos; um halt com a URL assinada guarda o motivo limpo. |
| 1.4 (P2) | "Pode comprar agora?" é derivado três vezes (`Runtime._robot_buy_block`/`_paused`, `Executor._buy_block`/`_paused`/`_reducing`, `RiskManager._state_verdict`) com textos e relógios diferentes. | `risk.buy_block(stored_state, paused_until, now, *, clock_blocked=False)` devolve o motivo canônico. O runtime passa o momento do ciclo; o executor continua lendo o store com o relógio da hora da ordem (defesa em profundidade). | Teste parametrizado sobre (nível, pausa, relógio); os testes que fixam os textos continuam passando. |

## 4. Onda 2: arquitetura do ciclo

O `Runtime` tem cerca de 75 métodos em 2147 linhas e mistura ciclo de vida, entradas manuais e armadas, robôs DCA e grid, leitura de mercado e o pipeline de decisão. A refatoração é mecânica, sem mudar comportamento: os testes existentes são a prova, com mudança só de alvos de `monkeypatch` e imports.

### 4.1 Contexto do ciclo, em dois passos

1. `limits` (o mandato) é lido uma vez no começo de `_cycle`, antes de `_risk_state`, e passado para `_risk_state`, `_instances` (incluindo `_trade_limit` e `_instance_plan`), `_score` e `_allocate`. A leitura do mandato por par em `service.order_notional_cap` e a do order gate por ordem ficam como estão: são outra camada.
2. `CycleContext` (imutável) é montado depois de `_instances` e `_decay`, com posições, livro, patrimônio, capital, capital de tamanho, caixa, retornos, `RiskState`, taxas e `limits`: `_score` lê o livro depois que os robôs agiram, como hoje. `_score`, `_protect` e `_allocate` recebem o contexto.

Teste: contar as chamadas a `Runtime._limits` por `tick()`; uma no modo automático e uma no modo manual com três linhas Prontas.

### 4.2 `_score` sem escrita

`_score` devolve as linhas mais a lista de efeitos, cada um com o `(status, reason)` do momento da nota: ponto de patrimônio, estado `pair_blocked` com seu evento no ledger, `record_day` das violações evitadas, `add_decision` com a marcação de `_decided` só depois que a escrita deu certo. `_cycle` aplica os efeitos logo depois de `_score`, antes de `_protect`, `_auto_eligible` e `_allocate` mudarem as linhas, para o Histórico continuar igual. O recuo `state is None → self._risk_state(...)` dentro de `_score` sai.

### 4.3 Pacote `bot/cycle/`

| Módulo | Conteúdo |
|---|---|
| `market.py` | `_collect`, `_choose_pairs`, `_apply_pairs`, `_account_takers`/`_read_fees`, `_free_cash`, `_positions`, `_refuse_blind`, `_closed`, `_frame`, `_last_price`, `_age`, `_returns`, `_held`, `_held_usd` |
| `exits.py` | `_mark`, `_trail`, `_exits`, `_day_exit`, `_policy`, `_exit_reason`, `_target_reason`, `_inflight`, `_rest_level`, `_unrealized` |
| `risk_state.py` | `_equity`, `_risk_state`, `_day_change`, `_save_risk_state`, `_flatten_day_trades`, `_pause_day`. Devolve o nível e o motivo de halt; o `Runtime` executa o halt no mesmo ponto de hoje, logo depois do nível e antes de `_instances` |
| `scoring.py` | `_score`, `_signal_only_reason`, `_pair_block`, `_pair_inputs`, `_market_block`, `_macro_block`, `_setup`, `_entry_block`, `_signal_age`, `_entry_drift_r`, `_auto_eligible`, `_clears_floor` |
| `instances.py` | robôs DCA e grid, de `preview_instance` a `_round_cooldown`, mais `_robot_buy_block`, `_trade_limit`, `_quote`, `_book`, `_missing_cap_reason` |

Ficam no `Runtime`: a trava, `retired`, `_decided`, o estado da thread de taxas, `start`/`stop`/`resume`/`halt`, `publish`, `_ledger`, `_tick`, `_cycle`, `_decay`, `_fire_armed`, entradas manuais e armadas, `_protect`, `_allocate`, `_sizing_capital`, `_min_notional` e `_limits`.

Regras de ligação:
1. Nenhum módulo de `bot/cycle` importa `bot.runtime`; o `Runtime` importa os módulos.
2. Os métodos públicos do `Runtime` mantêm suas checagens de trava e `retired` e então delegam.
3. Colaboradores entram como parâmetros. O `Runtime` passa os próprios métodos ligados, buscados na hora da chamada, para os patches na instância ou na classe continuarem valendo.
4. Os módulos escrevem no ledger só pelo `ledger` que recebem; nunca chamam `append_ledger` direto.
5. A config é lida do `Runtime` a cada ciclo, nunca guardada na construção.
6. As assinaturas que os testes chamam direto continuam: `_risk_state(snapshot, moment)`, `_instances(snapshot, moment, *, clock_blocked, state)`, `_exits(snapshot, moment)`.
7. `bot.runtime` não continua importando um nome que não usa mais. Um alvo velho `bot.runtime.X` então falha com `AttributeError` em vez de não fazer nada.

Dono de cada nome de módulo que os testes trocam:

| Nome | Dono depois da mudança |
|---|---|
| `_setup`, `in_quiet_hours` | `bot.cycle.scoring` |
| `_exit_reason`, `_TREND` | `bot.cycle.exits` |
| `_last_price` | `bot.cycle.market` (os outros módulos chamam `market._last_price` pelo módulo, não por import do nome) |
| `rank_promising`, `venue_symbols`, `FEE_WARMUP_BUDGET_SECONDS` | `bot.cycle.market` |
| `weight_pressure` | `bot.cycle.market` para a leitura de taxas e pares; `bot.runtime` continua usando para o payload |
| `review_strategy` | `bot.runtime` (vai com `_decay`) |
| `append_ledger` | `bot.runtime` |

### 4.4 API

A orquestração das validações sai de `bot/api/app.py` para `bot/api/validation.py`: `_claim_validation`, `revalidate_stale`, `_queue_validations`, `_run_validation`, `_replay_timeframes`, os auxiliares de tentativas e `_validation_jobs`. O módulo importa só `ctx`/estado, `Store` e o runner, nunca `bot.api.app`. A rota de validar inicia a thread com `validation._run_validation` (atributo do módulo, não nome importado), para um patch cobrir os dois lugares que iniciam threads. `bot/__main__.py` e a partida importam `revalidate_stale` do módulo novo. O preset de day trade vai para `bot/api/presets.py`. As rotas ficam em `app.py`.

### 4.5 Fora desta onda

As compras paradas dos robôs ficam em `bot/executor.py`: o código delas e o núcleo do executor se chamam nos dois sentidos e compartilham `_now` e `weight_pressure`, que os testes trocam. Separar exigiria mudar o comportamento dos patches. `CycleReport` não entra: não teria consumidor.

Prova: suíte verde sem mudar asserções; `bot.cycle` e `bot.advisor` entram na lista de pacotes do mypy estrito; o teste de leitura única do mandato.

## 5. Onda 3: validação e sinais

Os itens 3.2 a 3.4 mudam o gate: `GATE_VERSION` passa a 3, toda validação antiga fica velha e o robô valida de novo na partida, como já acontece. Até lá as linhas ficam só sinal.

| # | Achado | Mudança | Teste |
|---|---|---|---|
| 3.1 (P2, bug) | O loader pede velas até agora, e a Binance devolve a vela ainda aberta como última; o replay decide e executa sobre ela. Índice sem checagem de buraco ou duplicata. | `BinanceLoader.fetch` fica só com velas fechadas (`open_time + intervalo <= fim`), remove duplicatas, ordena e registra quantas velas faltam. | `BinanceLoader(fetch=...)` devolve uma última vela aberta e uma duplicata; as duas saem. |
| 3.2 (P1) | A validação junta os trades de todos os pares sem os limites da carteira; o robô ao vivo pula boa parte deles. | `Trade` ganha `symbol`. `runner.constrain_portfolio(trades, limits)` ordena por entrada (empate pelo símbolo) e recusa quando `max_positions` está cheio, quando o par já está aberto, na pausa do par depois de perda e na trava de perdas. A pausa e a trava, numa entrada, contam só trades aceitos com saída até aquele momento. A decisão da trava é uma função só, chamada por `risk.protect` e por `constrain_portfolio`. Só os trades que `classify` aceitaria disputam vagas, como ao vivo. `skipped_by_limits` e os limites usados ficam gravados. Os limites não entram na chave da validação: mudar um limite não exige validar de novo, e o painel mostra os limites com que o gate rodou. Não modelados, então o resultado é o melhor caso: risco aberto total, cluster do BTC, correlação, entradas por ciclo, degraus de drawdown, caixa e vagas disputadas com outras estratégias e robôs. | Trades montados à mão que se sobrepõem: o excesso sai e conta em `skipped_by_limits`; uma perda que sai depois da entrada seguinte não conta na trava. |
| 3.3 (P2) | O hold-out passa sem nenhum trade. | Hold-out precisa de pelo menos 5 trades e de ganho por dia de pelo menos `MIN_WFE` vezes o da amostra. Menos que isso reprova com motivo. | `test_gate_fails_when_the_holdout_loses` passa a reprovar o hold-out quieto; caso novo com 4 trades. |
| 3.4 (P1) | Com a seleção automática, a validação roda nos pares de melhor momento dos últimos 28 dias, janela que cai dentro do hold-out: viés de seleção. | Com `auto_select`, o replay usa o universo líquido inteiro de onde a seleção escolhe (`RANK_POOL`, os 40 pares mais líquidos por volume), gravado como `params['symbols']`, e `pairset` é o hash desse universo. Um par escolhido fora do universo gravado fica só sinal. A revalidação segue a mudança do universo (no máximo uma vez por dia, como D12), não a troca horária de `config.symbols`. A seleção por volume no fim do período ainda carrega algum viés e não vê pares deslistados; o painel diz isso. | O universo gravado é o líquido, não o escolhido; uma troca horária de `config.symbols` dentro do universo não deixa a validação velha. |
| 3.5 (P1) | O replay entra em todo setup; ao vivo, `classify` filtra por nota, custo e ganho/risco. A taxa de acerto do Kelly e do piso vem da população maior. | `simulate` marca em cada trade se `classify` o aceitaria com o custo do backtest. Cada resultado por timeframe grava `filtered_trades`, `filtered_wins` e `filtered_sharpe`, mais os limiares usados (`score_threshold`, `cost_multiple`, `min_reward_risk`). A estimativa vira `min(Laplace(geral + vivo), Laplace(filtrado + vivo))`, só quando `filtrado + vivo` alcança `min_win_rate_sample`; abaixo disso, ou com limiares diferentes dos atuais, vale a geral. Nunca vira `None` por causa do filtrado. Desempenho mostra os dois. Não muda o gate. | Quadro sintético em que parte dos setups falha o custo: as contagens diferem; amostra filtrada pequena não muda a estimativa. |
| 3.6 (P3) | A trava de perdas conta perdas, não R: duas saídas de -1 R não pausam. | `loss_guard_r` (padrão 2): a trava pausa quando qualquer regra dispara, a contagem de `loss_guard_trades` perdas em 6 h ou a soma de R das saídas com perda em 6 h em -2 R ou menos. R é `realized_pnl / initial_risk_usd`, a mesma unidade do executor. | Três saídas de -0,1 R continuam pausando; duas de -1 R agora pausam. |
| 3.7 (P3) | Código morto e textos velhos: o bloqueio `violations_avoided < 0` nunca dispara; `POST /bot/validate` diz que só três estratégias validam; o ramo `regime_factor('trend','range')` é inalcançável; o filtro de Oportunidades lista quatro estratégias fixas. | Remover o morto, corrigir as mensagens, montar o filtro a partir de `status.strategies`. | Mensagem e opções do filtro conferidas; `npm run build` passa. |

## 6. Onda 4: consultor DeepSeek

### 6.1 API (verificada em 2026-10-28 na documentação oficial)

- `POST https://api.deepseek.com/chat/completions`, formato OpenAI, `Authorization: Bearer <chave>`.
- Modelos: `deepseek-flash` (padrão, servido pelo DeepSeek-V4.1-Flash) e `deepseek-v4-pro`. `deepseek-chat` e `deepseek-reasoner` foram aposentados em 2026-07-24.
- Modo JSON: `response_format: {"type": "json_object"}`, a palavra "json" e um exemplo no prompt, `max_tokens` suficiente. A documentação avisa que o conteúdo pode voltar vazio às vezes.
- O modo de raciocínio vem ligado; o consultor o desliga (`thinking: {"type": "disabled"}`) para ter latência e custo previsíveis.
- Cache de contexto automático por prefixo idêntico: o prompt de sistema é fixo e vem primeiro.
- Limite só de concorrência (HTTP 429 acima dele); filas longas mandam linhas vazias antes da resposta.
- `usage` traz `prompt_cache_hit_tokens`, `prompt_cache_miss_tokens` e `completion_tokens`, usados no custo.

O custo por chamada sai de uma tabela de preços no código (por modelo, cache hit, cache miss, saída, com horário de pico), datada e marcada como estimativa; o valor faturado é o do painel da DeepSeek.

### 6.2 Pacote `bot/advisor/`

- `credentials.py`: lê `deepseek.json` na raiz base do operador, a mesma de `binance.json`: `{"api_key": ..., "base_url": ...}`. `DEEPSEEK_API_KEY` vale só quando o arquivo não tem chave. Em POSIX, um arquivo com permissão mais aberta que 0600 é recusado. `python -m bot.ops advisor-key` lê a chave da entrada padrão, nunca de argumento, e grava com mkstemp, fsync, replace e 0600, como `save_config`.
- `client.py`: `httpx.Client` próprio (connect 5 s, leitura 30 s), sem tocar o peso da Binance. Erros viram `AdvisorError(kind)` com `kind` em `auth`, `rate_limited`, `timeout`, `server`, `empty`, `invalid_json`; a mensagem nunca leva o corpo. Conteúdo vazio repete uma vez. O transporte é injetável para os testes.
- `prompt.py`: prompt de sistema fixo, versão `PROMPT_VERSION`. A entrada é só número e enum montados pelo código: par, estratégia, timeframe, direção, entrada, stop e alvo em %, R esperado, nota, custo, acerto estimado e n, regime do setup (`Opportunity.regime`, novo), alfas, blocos de `features.build_features` do par, regime do BTC (EMA de 4 h), nível de risco da conta, drawdown e número de posições abertas. Nenhum texto livre de fonte externa entra no prompt, então não há vetor de injeção. Saída validada por pydantic com `allow_inf_nan=False`:
  `{"verdict": "confirm|reduce|veto", "confidence": 0..1, "size_factor": 0.25..1, "regime": "trend_up|trend_down|range|volatile", "risk_flags": [até 4 textos], "rationale": "até 240 caracteres"}`. `size_factor` fora da faixa é recortado; valor não finito ou JSON inválido vira `abstain`.
- `service.py`: um `Advisor` por processo, em `BotContext` ao lado do `collector`, passado ao `Runtime` como argumento opcional; o `conftest` o reinicia. Uma thread e uma fila limitada. Cada pedido leva o próprio `Store`, então o veredito cai no banco do modo que pediu, mesmo depois de uma troca de modo. A chave do cache é `(símbolo, estratégia, timeframe, fechamento da vela do sinal, PROMPT_VERSION, modelo)`: uma chamada por sinal, não por ciclo. O orçamento é conferido antes de cada chamada, sob uma trava própria, para dois pedidos não passarem juntos. Grava o veredito no store e um evento `advice` no ledger com o sha256 do prompt.

### 6.3 Configuração (`BotConfig.advisor`, `extra="forbid"`)

| Campo | Padrão | Faixa |
|---|---|---|
| `mode` | `off` | `off`, `shadow`, `filter` |
| `model` | `deepseek-flash` | `deepseek-flash`, `deepseek-v4-pro` |
| `daily_budget_usd` | 1,00 | 0 a 50 |
| `min_veto_confidence` | 0,6 | 0,5 a 1 |
| `require_verdict` | `false` | — |
| `wait_seconds` | 45 | 5 a 120 |

Um campo cujo nome contém `key`, `secret` ou `token` é recusado (422), para uma chave colada no painel nunca ser gravada em `bot.json`. O modo não entra no mandato: o mandato guarda tetos da corretora, e o consultor só reduz.

### 6.4 Onde o veredito age

- **Quem é consultado:** só linhas Prontas de estratégia (não teste, não robôs DCA ou grid, não armadas). Depois de aplicar os efeitos de `_score`, `_cycle` enfileira com `put_nowait`; fila cheia é ausência de veredito. O cache de vereditos entra no `CycleContext` como cópia tirada no começo do ciclo, então `_score` continua sem efeito colateral. Entradas manuais mostram o veredito e não são bloqueadas: o operador decide.
- **`shadow`:** consulta, grava e mostra. Nenhuma ordem muda e nenhuma entrada espera.
- **`filter`:**
  - `veto` com confiança de pelo menos `min_veto_confidence`: a linha não entra no automático e fica Pronta para o manual com o motivo "IA desaconselha: ...", como a regra da idade do sinal.
  - `reduce`: o fator recortado entra em `RiskManager.assess` e é aplicado dentro de `_decide` como multiplicador depois do mínimo dos tetos e do throttle, do mesmo jeito que o throttle. Quando o fator é menor que 1, `caps['advice']` recebe o tamanho final e `binding` vira `advice` ("análise IA" em `risk.SIZE_LIMITS`). Só o fator entra, nunca o veredito, e ele só reduz: uma recusa continua recusa.
  - `confirm`, ou `veto` de confiança baixa: nada muda.
  - Veredito ainda não chegou: a espera é medida entre ciclos, nunca dentro de um. A linha fica fora do automático até `wait_seconds` desde o pedido; depois, ou com erro, segue sem veredito. Com `require_verdict`, não entra no automático.
- O veredito nunca passa pelo order gate, pelo mandato, pelo halt ou pelas saídas, e `mandate_caps_for` dá o mesmo resultado em todos os modos.

### 6.5 Registro e calibração

- `advice_log`: uma linha por veredito com momento, modo, modelo, `prompt_version`, par, estratégia, timeframe, vela do sinal, veredito, confiança, fator, regime, tokens, custo, latência, preço de entrada (fechamento da vela do sinal), stop e alvo em %, se entrou, id da posição, R contrafactual e quando resolveu.
- `advisor_usage_daily`: chamadas, tokens, custo, contagem por veredito e erros por dia UTC. O orçamento lê essa tabela, então sobrevive a reinícios.
- Posições gravam `entry_advice_verdict` e `entry_advice_confidence`.
- **R contrafactual, com as regras do replay.** Toda linha de `advice_log`, tenha entrado ou não, recebe o mesmo rótulo. A entrada é o fechamento da vela do sinal, com stop e alvo a `stop_pct` e `target_pct`. A partir da vela seguinte: o stop é conferido na mínima e preenche em `min(abertura, stop)`, então um gap custa mais de -1 R; o alvo conta só quando uma vela fecha nele ou acima, e preenche nesse fechamento; depois de 48 velas, vale o último fechamento. Todo resultado desconta `(ROUND_TRIP_COST + 2 × LEG_SLIPPAGE) / stop_pct`. O resolvedor reaproveita o laço de `simulate_fixed` com um limite de 48 velas, então é igual ao replay por construção.
- **Fonte das velas:** o snapshot do coletor (velas fechadas do timeframe da linha). O passo roda uma vez por hora, fora da trava, e pula sob `weight_pressure()`. Uma linha cuja janela ainda não está no snapshot continua sem resolver.
- **Desempenho:** a calibração agrupa por `(modelo, PROMPT_VERSION)`. Uma linha entra numa média só depois que as 48 velas da janela passaram desde a vela do sinal, qualquer que seja o resultado; incluir assim que resolve traria primeiro os stops e alvos rápidos. Para cada veredito, e para todas as linhas consultadas como base, mostra n, R médio contrafactual e o erro padrão. Os vetos com confiança de pelo menos `min_veto_confidence` têm linha própria: são o que o modo `filter` removeria. Para `reduce`, mostra `(1 − fator) × R`. O R realizado das linhas que entraram fica numa coluna separada, nunca misturado na mesma média. Também mostra o uso do dia e de 30 dias.

### 6.6 API e painel

- `/bot/status` ganha `advisor`: modo, modelo, `configured` (há chave), último sucesso, último erro (redigido), uso e orçamento do dia. Nunca a chave.
- `PUT /bot/config` aceita `advisor` com a validação acima.
- `/bot/performance` ganha `advice_calibration` e `advisor_usage`.
- Painel: selo do veredito em Oportunidades (com a justificativa no título), filtro por veredito, bloco do consultor em Controles, tabelas em Desempenho, custo do dia no cabeçalho quando ligado.

### 6.7 Testes da onda

1. Cliente com transporte injetado: cada tipo de `AdvisorError`, uma repetição no conteúdo vazio, nenhuma mensagem com o corpo.
2. Orçamento lido de `advisor_usage_daily` por um `Store` novo; orçamento esgotado não chama o transporte.
3. `filter`: um veto tira a linha do automático, a deixa Pronta com o motivo e `act` zero; `POST /bot/opportunities/execute` ainda abre a linha.
4. `shadow` não muda ordem nenhuma nem atrasa entrada.
5. `reduce` com throttle 0,5 e fator 0,5 dá o tamanho base × 0,25; fator 1,5 vira 1; NaN vira `abstain` sem mudar o tamanho.
6. `require_verdict` com demora: nenhuma entrada automática.
7. Um transporte que demora 5 s não alonga o `tick()`; a linha fica sem veredito no primeiro ciclo e com ele depois.
8. A chave nunca aparece em `/bot/status`, no eco de `PUT /bot/config`, nos logs capturados, no ledger nem em `advice_log`; `PUT` com `advisor.api_key` dá 422.
9. `mandate_caps_for` é igual em `off`, `shadow` e `filter`.
10. Resolvedor: num mesmo quadro, sem o limite de 48 velas, o R contrafactual é igual a `net_return / stop_pct` de `simulate_fixed`, incluindo um gap abaixo do stop e um pavio acima do alvo que fecha abaixo dele.

### 6.8 Custo esperado

Uma chamada por sinal Pronto, não por vela: dezenas por dia. Com cerca de 2.500 tokens de entrada (700 em cache) e 300 de saída, o `deepseek-flash` custa perto de US$ 0,001 por chamada no pico. O orçamento padrão de US$ 1 por dia cobre muito mais do que o robô gera.

## 7. Onda 5: documentação

README (seção do consultor, gate v3, guia da chave), seção 10 deste documento com o que mudou em relação ao desenho, e memória do projeto.

## 8. Fora do escopo

- **Briefing de mercado por vela fechada.** Seriam cerca de 1.900 chamadas por dia para 15 pares sem decisão que as use; o veredito por setup já leva o contexto de vários timeframes e do BTC.
- **O LLM gerar sinais, escolher pares ou ajustar parâmetros.** Isso não tem como ser validado no histórico e viola o princípio 2.
- **Venda a mercado no stop atravessado, OCO no TP1 e PBO**: continuam como na seção 11 da revisão anterior.
- **Dividir o executor**, incluindo mover as compras paradas dos robôs (4.5).
- **`CycleReport`** (4.5).

## 9. Mudanças exigidas pelos architects

Segurança (S), arquitetura (R) e validade quantitativa (Q), todos com decisão "aprovado com mudanças":

| Id | Seção | Mudança |
|---|---|---|
| S-A1 | 3 (1.2) | -1021 é `InvalidNonce` no spot e hoje vira desconhecido, não recusa. Reenvio só para -1021, com `_unsent_before_write` de novo, no máximo uma vez; -1022 fora; 1000 ms só em "ahead". |
| S-A2 | 3 (1.3) | Redação num `Formatter` em todo handler, sobre o registro formatado inteiro, não num filtro do logger raiz. |
| S-A3, R-A8 | 6.4 | `reduce` como multiplicador dentro de `_decide`, depois do mínimo e do throttle; saída com `allow_inf_nan=False`; não finito vira `abstain`. |
| S-A4 | 2, 6.2, 6.4 | Só consulta ao cache e `put_nowait` numa fila limitada sob a trava; espera medida entre ciclos; a thread do consultor nunca pega trava do ciclo. |
| S-A5 | 5 (3.6) | A trava por contagem continua; a de R é uma regra a mais. |
| S-A6, R-A10, Q-A8 | 5, 6.7 | Testes nomeados nas ondas 3 e 4. |
| R-A1, R-A2 | 4.3 | Regras de ligação e tabela de donos dos nomes trocados pelos testes. |
| R-A3 | 4.1 | Mandato lido no começo do ciclo; contexto montado depois de `_instances` e `_decay`. |
| R-A4 | 4.2 | Efeitos com o status da hora da nota, aplicados antes de `_protect`; sai o recuo de `_risk_state` dentro de `_score`. |
| R-A5 | 4.5 | Compras paradas ficam no executor. |
| R-A6 | 4.4 | `validation.py` sem importar `app`; thread iniciada pelo atributo do módulo. |
| R-A7 | 6.2 | Um `Advisor` por processo, em `BotContext`; `Store` por pedido; `prompt_version` no log. |
| R-A9 | 3 (1.3) | `redaction.py` vendorizado não muda. |
| R-A11 | 4.5, 6.2 | Sem `CycleReport`; `Opportunity.regime` entra na onda 4. |
| Q-A1, Q-A2 | 6.5 | Contrafactual com as regras de preenchimento do replay, líquido de custo, para toda linha; R realizado em coluna separada. |
| Q-A3 | 6.5 | Calibração por modelo e versão do prompt, vetos de alta confiança em linha própria, n e erro padrão. |
| Q-A4 | 5 (3.2) | `Trade.symbol`, empate pelo símbolo, guardas sem olhar o futuro, uma função de trava compartilhada com `risk.protect`. |
| Q-A5 | 5 (3.2) | Limites fora da chave da validação, gravados e mostrados; emenda à decisão anterior registrada na seção 1. |
| Q-A6 | 5 (3.4) | Universo de replay é o `RANK_POOL` inteiro; `pairset` dele; par fora fica só sinal; revalidação segue o universo. |
| Q-A7 | 5 (3.5) | Taxa filtrada só com amostra suficiente e limiares iguais; nunca `None`. |

## 10. Status da implementação

Data: 2026-10-28. Todas as ondas foram implementadas, cada uma por um implementador, com um test engineer e dois ou três reviewers independentes antes do commit. Todas foram aceitas na primeira rodada, menos a onda 1, que precisou de uma correção (um erro de compra parada que devolvia a URL assinada sem redação). A suíte passou de 812 para 869 testes; o mypy estrito cobre `bot.cycle`, `bot.advisor` e `bot.logging_setup`.

| Onda | Commit | Status |
|---|---|---|
| Desenho | `f58fb62` | aprovado com mudanças (seção 9) |
| 1 | `74e48c1` | implementado |
| 2 | `23ed701`, `203620c`, `d218ba1` | implementado (`runtime.py` de 2264 para 776 linhas, `app.py` de 1543 para 1088) |
| 3 | `0dbc8cb` | implementado, `GATE_VERSION` 3 |
| 4 | `a738ec7`, `ac77a2d` | implementado; painel conferido num par API e Vite de rascunho |
| Revisão final | `64845d6` | 6 defeitos confirmados e corrigidos |

### Desvios em relação ao desenho

- **1.4.** O texto do modo só reduzir passou de "10.0%" para "10%": as duas formas viraram uma.
- **4.3.** A extração para `bot/cycle` saiu num commit só, não um por módulo: é uma mudança mecânica, com a suíte verde e sem asserção alterada. `CycleContext` e os tipos dos efeitos ficam em `bot/cycle/context.py`, para não haver import circular. Os robôs são uma dataclass `Robots` montada a cada chamada com os colaboradores do `Runtime`.
- **4.2.** `_score` também devolve um efeito `forget` para o `_decided` em memória, então não muda estado nenhum do `Runtime`.
- **4.4.** `ctx` foi para `bot/api/state.py`, para `validation.py` não importar `app.py`.
- **3.2.** Os trades que `classify` recusaria ficam na população geral do gate como replay, sem disputar vaga; só os aceitos disputam e alimentam a pausa e a trava. Assim 3.5 continua sem mudar o gate.
- **3.4.** O universo líquido vem do ranking horário (`bot_state.auto_select_universe`); sem ranking ainda, a validação lê o universo ela mesma.
- **6.2.** O prompt leva um resumo dos indicadores (últimos valores, níveis relativos, 12 retornos de 1 h), não as 120 velas, para caber nos ~2.500 tokens de 6.8. A tabela de preços usa os preços de pico da página oficial lidos em 2026-10-28.
- **6.4.** Ligar `require_verdict` pede confirmação no painel.
- **6.5.** As linhas de calibração também mostram `abstain` e quantas aguardam.

### Revisão final da branch

Uma revisão independente de toda a diferença, em seis frentes, teve cada achado testado por dois verificadores que tentavam refutá-lo. Dos 13 achados, 9 sobreviveram, num total de 6 defeitos distintos. Nenhum dimensionava ou enviava uma ordem; todos foram corrigidos com teste de regressão em `64845d6`:

1. **Olhar o futuro no prompt.** Uma consulta atrasada via velas depois do sinal, e a calibração mede o R a partir do sinal. O prompt agora usa só velas fechadas até o fechamento do sinal.
2. **Cache entre modos.** Um veredito do testnet podia valer em produção depois da troca de modo. A chave do cache leva a identidade do store, e o executor só liga um `advice_log` do próprio store.
3. **Pergunta repetida depois de reiniciar.** `advice_log` é único por sinal; o veredito gravado é relido, sem nova chamada, e a calibração o conta uma vez.
4. **Orçamento por modo.** O orçamento soma os dois stores e toda tentativa cobrada, inclusive a resposta vazia.
5. **Linhas que nunca resolviam.** Um par que saiu do snapshot resolve com velas buscadas por tempo; sem dado depois de um dia de folga, a linha expira e é contada à parte.
6. **Limiares fora da chave.** Mudar `score_threshold`, `cost_multiple` ou `min_reward_risk` deixa a validação velha.

Quatro achados foram refutados, entre eles que o reenvio do -1021 pularia a checagem do mandato.

### Revisão rigorosa

Uma segunda revisão da branch, com cada achado confirmado por dois refutadores, deixou 15 correções, cada uma com o teste que pedia:

1. **ReDoS na redação.** O padrão de `api_key` começa no literal, sem a classe ilimitada antes dele, e `redact` lê no máximo 8 KiB (começo, marcador e as linhas finais).
2. **Redação do uvicorn.** Um teste roda `main()` e confere o `RedactingFormatter` em todo handler da raiz e do uvicorn, com `signature=` limpo em `uvicorn.error`.
3. **Veredito gravado antes do reinício.** O cache recebe os vereditos recentes do store antes da cópia do ciclo (`Advisor.recall`): um `reduce` gravado reduz a primeira entrada depois do reinício.
4. **Rótulos no `tick`.** Teste do `_resolve_advice`: rotula, respeita `RESOLVE_EVERY`, nada num runtime aposentado, e uma falha não derruba o ciclo.
5. **Taxa filtrada no ciclo.** Testes de `_score` com limiares iguais, com saída fixa (`min_reward_risk` 0) e com marcas diferentes.
6. **Universo sem canal lateral.** `rank_promising` devolve a escolha e o universo; sai `Collector.universe_cache`, que dois runtimes podiam trocar.
7. **Limiares na chave ao vivo.** `validated_for(thresholds=...)` é a única comparação, usada por `_stale_cause`, pelo ciclo e por `/bot/status`: um aprovado com outros limiares fica só sinal até validar de novo, como uma troca de regra de saída.
8. **Universo lido pela validação.** Testes do caminho sem universo gravado: lê o universo líquido e o grava; universo vazio termina em erro com o motivo.
9. **Marcas do replay de saída fixa.** Teste em que o alvo da estratégia passa e o alvo fixo reprova.
10. **Marcas do replay day.** Teste com setups que alternam aprovação e reprovação.
11. **População da taxa de acerto.** O texto de `BacktestResult`, de `estimate_win_rate` e do painel diz que `trades` e `wins` são os trades que sobraram depois dos limites da carteira.
12. **Prazo total da DeepSeek.** A resposta é lida em stream e para em `TOTAL_SECONDS` (45 s): linhas em branco de uma fila longa não prendem o consultor.
13. **`Advisor.close` removido.** Ninguém o chamava; a thread é daemon. `wait_idle` diz que é só para testes.
14. **Leitura de velas que falha.** Uma falha não conta como tentativa: a linha só expira quando a leitura deu certo sem a vela ou com a janela incompleta, ou depois de três falhas após a folga (`fetch_failures`).
15. **Teto de leituras por passada.** Teste com mais de `MAX_FETCHES` linhas fora do snapshot: só `MAX_FETCHES` leituras, e as demais ficam sem rótulo e sem expirar.

### Para o operador

- Ao reiniciar com este código, toda validação fica velha (gate v3) e roda de novo na partida; até lá as linhas ficam só sinal.
- O consultor vem desligado. Para usar: grave a chave com `python -m bot.ops advisor-key`, ligue `shadow` em Controles e acompanhe a calibração em Desempenho antes de ligar `filter`.
- O bot no Docker continua com o código antigo até ser reconstruído.
