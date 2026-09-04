# Agent Terminal

A self-hosted Kraken Futures trading terminal with live market data, chart-based order management, account analytics, a volatility scanner, and an ARM-gated AI trading assistant.

> [!WARNING]
> This software can submit real orders. Every server restart returns it to **DISARMED**. Review the safety model and test against Kraken's demo environment before using live credentials.

## What is included

- Kraken Futures positions, orders, fills, margin, and account history
- Binance USDT-M candles for charting, with Kraken as the trading authority
- Market, limit, post-only, stop, take-profit, ladder, and chase orders
- Draggable TP/SL controls with confirmation and actual protection coverage
- Managed full-position TP/SL resizing after confirmed position-size changes
- React 19 workspace with lightweight-charts, Zustand, and Markdown chat
- OpenRouter assistant with audited read and ARM-gated write tools
- SQLite chat, execution, tool-call, equity, and account-history persistence

## Safety model

- The server always starts **DISARMED**.
- DISARMED writes return an exact simulation and do not call Kraken's trading endpoints.
- Arming requires the sidebar control and an `ARM` confirmation.
- Managed protection changes run only while ARMED.
- Partial TP/SL ladders are not automatically resized.
- Kraken credentials stay in `terminal/.env`, which Git ignores.
- Runtime databases, logs, screenshots, and local auth files are not committed.

Arming is not a sandbox switch. If `KRAKEN_FUTURES_ENV=live`, ARMED actions affect the live account.

## Requirements

- Python 3.11 or newer
- Node.js 22 or newer
- A Kraken Futures API key for private account data and trading
- Pi OpenRouter credentials in `~/.pi/agent/auth.json` for the AI assistant

The market-data UI can start without private credentials, but account and trading features will be unavailable.

## Quick start

```bash
git clone https://github.com/MarcusNeufeldt/agent_terminal.git
cd agent_terminal

python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

cp terminal/.env.example terminal/.env
# Add your Kraken credentials to terminal/.env.

cd frontend
npm ci
npm run build

cd ../terminal
python run.py
```

Open <http://127.0.0.1:8787>.

Start with `KRAKEN_FUTURES_ENV=demo`. Do not copy live keys into source files, shell history, screenshots, issues, or chat messages.

## Development

Run the backend:

```bash
cd terminal
python run.py
```

Run the frontend with hot reload in another terminal:

```bash
cd frontend
npm ci
npm run dev
```

The Vite server proxies API traffic to port `8787`.

Run all checks:

```bash
cd terminal
python -m unittest -v

cd ../frontend
npm run lint
npm run build
```

## Project layout

```text
frontend/           React/Vite source
terminal/           Python server, trading engine, persistence, and tests
terminal/static/    production frontend bundle served by Python
terminal/legacy/    original vanilla implementation kept for reference
```

See [`terminal/README.md`](terminal/README.md) for the API, data flow, execution model, persistence schema, and known limits.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `KRAKEN_FUTURES_API_KEY` | none | Kraken Futures API key |
| `KRAKEN_FUTURES_API_SECRET` | none | Kraken Futures API secret |
| `KRAKEN_FUTURES_ENV` | `live` | Set to `demo` for Kraken's demo environment |
| `PORT` | `8787` | Local HTTP port |
| `AI_CHAT_MODEL` | `google/gemini-3.8-flash` | OpenRouter model |
| `CHAT_CONTEXT_LIMIT` | `200000` | AI conversation compaction threshold |

## Security

Never commit `terminal/.env`, `~/.pi/agent/auth.json`, SQLite files, logs, or screenshots containing account data. The repository ignore rules cover the standard local paths, but check every staged change before pushing.

Report security problems through GitHub's private vulnerability reporting. See [`SECURITY.md`](SECURITY.md).

## Disclaimer

This is personal trading software, not financial advice. It comes without guarantees of profitability, availability, or protection from exchange, network, model, or software failures.
