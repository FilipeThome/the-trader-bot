# Bot de trading automático

Serviço local que analisa pares USDT na Binance e só envia ordem pelo order gate portado do Vibe-Trading. A interface lê o estado e manda pausar, parar ou ajustar limites. Ela não fala com a exchange.

Isto não é recomendação de investimento. Nenhuma estratégia garante lucro. O retorno do painel é líquido de taxa, slippage e funding.

## O que a v1 faz

- Spot em BTC, ETH, SOL, BNB e XRP. Futuros USD-M são só leitura: a carrega de funding aparece no painel e não executa.
- Três famílias: tendência (1h e 4h), reversão à média (15m e 1h) e carrega de funding (sinal apenas).
- Testnet por padrão (`binance-paper-trade`). Produção (`binance-live-trade-bot`) só depois dos gates, de 30 dias com PnL líquido positivo, de um mandato restrito a cripto e da palavra `PRODUCAO`.
- Nenhuma ordem sem mandato, latch e registro no journal antes do envio. Halt não se desarma sozinho.

A origem de cada módulo portado está em [ORIGIN.md](ORIGIN.md).

## Operação

1. Python 3.12 e Node 22. Na raiz:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
pytest
cd ui
npm install
npm run dev
```

2. Noutro terminal, com a raiz de runtime explícita (não use a pasta do código para chaves):

```powershell
$env:BOT_RUNTIME_ROOT = "$env:USERPROFILE\.the-trader-bot"
python -m bot
```

3. A interface fica em `http://127.0.0.1:5173` e a API em `http://127.0.0.1:8000`.

4. Primeira configuração, na tela Controles:

- Grave as chaves de **Spot Testnet** em `%USERPROFILE%\.the-trader-bot\binance.json` ou nas variáveis `BINANCE_TESTNET_API_KEY` e `BINANCE_TESTNET_API_SECRET`. A interface não recebe chave. A chave de produção, quando existir, precisa de restrição de IP e sem permissão de saque.
- Defina o capital, ligue uma estratégia só depois do backtest (Sharpe acima de 1 e drawdown abaixo de 20%) e confirme o mandato. O bot grava `mandate.json` com instrumentos só `crypto`.
- Inicie. O ciclo é de 60 segundos. O selo cinza significa Spot Testnet.

5. Parar tudo (`POST /bot/halt` ou o botão) cancela ordens abertas, mantém a posição (ou zera, se flatten estiver marcado) e não volta a operar. Retomar é `POST /bot/resume`, manual.

6. Produção é outro diretório (`testnet/` e `production/` não compartilham store, journal nem posição). O interruptor volta para testnet se qualquer pré-condição faltar, e o motivo aparece na tela. Sair de produção é sempre permitido.

`binance.json` continua sendo o arquivo de credencial do conector portado. `bot.json` guarda modo, capital, estratégias e limites, e esses limites nunca podem afrouxar o mandato.

## Testes

```powershell
pytest
```

A suíte cobre store, coletor (dado velho e queda do websocket para REST), score, painel, cada limite de risco, o gate na frente do conector, idempotência, relatório contra o ledger, decaimento de Sharpe e o perfil de produção.
