# Kraken Futures Trading Terminal

A web trading terminal for Kraken Futures multi-collateral accounts. The Python HTTP server, trading engine, SSE hub, and SQLite persistence live here. The React/Vite frontend lives in `../frontend` and builds into `static/`.

## Run

```bash
pip install -r requirements.txt
cp terminal/.env.example terminal/.env
cd terminal
python run.py
# open http://127.0.0.1:8787
```

## Frontend (React)

The UI is a React/Vite SPA in `../frontend`; the Python server serves its production bundle from `static/`.

```bash
cd frontend
npm ci               # install locked dependencies
npm run dev          # dev server with /api proxied to :8787
npm run build        # production bundle -> ../terminal/static
```

The original vanilla JS app is kept in `terminal/legacy/` for reference. `/volatility` remains a standalone page (`frontend/public/volatility.html`). Chart rendering is imperative lightweight-charts (v5) wrapped in one React component; state lives in a zustand store (`src/store.js`); SSE streams into the store.

Kraken credentials load from `terminal/.env` or process environment variables. A local compatibility fallback checks the author's adjacent `kraken-futures-cli/.env` checkout when present. Copy `.env.example` to `.env`; never commit the populated file. Supported variables are `PORT`, `KRAKEN_FUTURES_API_KEY`, `KRAKEN_FUTURES_API_SECRET`, `KRAKEN_FUTURES_ENV=demo`, `AI_CHAT_MODEL` (default `google/gemini-3.8-flash` via OpenRouter), and `CHAT_CONTEXT_LIMIT` (default 200000 tokens).

## Safety model

- The terminal starts **DISARMED** on every server start. Disarmed, orders/cancels/chases return the exact dry-run plan without touching Kraken.
- Arming requires clicking the sidebar button and typing `ARM`. Armed state is in-memory only.
- The AI assistant reads everything itself (market data, positions, account, fills, contract specs, performance) and can also place orders directly through the same tools the Execute button uses - **all write tools are ARM-gated**: disarmed they return the exact plan simulated, armed they hit the live account. `propose_actions` cards (human clicks Execute) remain available for draft/plan requests; the chase engine stays propose-only.
- Order responses are logged in `actions_log`; bounded AI tool calls/results are logged as `ai_tool` events. Rejections surface with Kraken's actual reason.

## Layout

- **Sidebar** — env/armed/feed badges, instrument search, live watchlist, balance + available margin (with **Pro-Mode** toggle: display-only +$5,600 sticker — never feeds sizing), arm toggle.
- **Header** — last price, 24h stats, mark/index, funding, open interest, **Stats** (performance modal), **Volatility Pairs** button, fill-sound bell, and a pulsing **risk chip** (worst position's distance to liquidation) whenever any position is under 12% from LIQ.
- **Chart** — lightweight-charts candles (1m–1w), formatted per instrument tick size. Candles: **Binance USDT-M klines** (`PF_X` → `XUSDT`, XBT→BTC) for history via REST and the live edge via `/market/ws` websocket; falls back to Kraken data when Binance lacks a symbol. Trading stays 100% Kraken. Overlays: position entry, liquidation estimate, TP/SL triggers, and open limit orders; each open order has a chart-side × control with confirmation before cancellation. Drag the TP/SL handle beside a position: the profit side previews a take profit, the loss side previews a stop loss, both tick-snapped with live estimated PnL; live drops require confirmation. Drag-created full-position protection is marked as managed and, while ARMED, auto-resizes after a confirmed position-size change; partial TP/SL ladders are never rewritten. Live trades merge into the current candle; quiet minutes carry forward flat (same as Kraken's own charts). EMA 400/800 trend badge.
- **Order book** — top levels with depth bars, ~2.5s refresh.
- **Order ticket** — market / limit / post-only / stop / take-profit / **chase**; % of available margin quick-sizing with a **leverage selector** (1x–10x, persisted); reduce-only. Sizes and prices are rounded server-side to the instrument's contract precision / tick.
- **Bottom tabs** — Positions (live mark + uPnL per tick, per-row close), Orders (per-row cancel, filtered cancel-all), Fills (newest first), **Scanner**.
- **Scanner** — perpetuals ranked by realized volatility from closed 1m mark candles; click a row to open its chart.
- **AI assistant** — persistent chat (SQLite), direct ARM-gated execution, optional proposal cards, audited tool calls, execution reports.

## The AI assistant

Backed by OpenRouter (key from `~/.pi/agent/auth.json`) with **native tool calling**:

- `propose_actions` drafts review cards only when explicitly requested; chase remains proposal-only.
- **Read tools**: `get_market_data`, `get_positions`, `get_account`, `get_orders`, `get_fills`, `get_instrument`, `get_performance`, `get_chases`, `get_trade_history`, and `scan_markets`.
- **Write tools (ARM-gated, immediate)**: `place_order`, `place_ladder`, `close_position`, `replace_tp`, `replace_sl`, `cancel_order`, and `cancel_all_for_symbol` — thin wrappers over the same `actions.py` engine the Execute button uses.
- `update_memory` stores durable preferences, plans, lessons, and target levels only. Live positions, working orders, and protection are always read from Kraken.
- Every message carries a live snapshot with account, positions, complete open-order IDs/sizes/triggers, mark/last, EMA signal, symbol metadata, and recent candles.
- Sequential action batches invalidate cached state after every action and wait for a close to settle before sizing later protection.
- After execution, the server verifies the action against the live book and persists the report. Every AI tool call and bounded result is also written to SQLite as an `ai_tool` event.

## Chase engine (`chase.py`)

Post-only limit orders that rest at best bid (buy) / best ask (sell) and re-peg as the market moves until filled, timeout, or max re-pegs. Partial fills are preserved across re-pegs; post-only rejections (would-cross) step a tick more passive. One thread per chase, status broadcast over SSE, fills ping the bell. Requires an armed terminal. Also available from the CLI: `python -m kraken_futures_cli chase SYMBOL buy|sell SIZE [--chase-timeout --repeg --max-repegs --offset --no-wait --json --terminal]`.

## Persistence (`db.py`, SQLite WAL — `terminal.db`)

- `sessions` — one auto-continuing Trading session + `context_tokens` (exact usage from OpenRouter) + the AI's **living memory file**
- `messages` — full chat history with proposal payloads; survives refreshes and server restarts
- `actions_log` — every executed action batch with results
- `events` — arm toggles, executions, managed-protection resizes, chase lifecycles, bounded AI tool calls/results, compactions, rejections
- **Compaction** — token-tracked from OpenRouter's exact usage; at `CHAT_CONTEXT_LIMIT` the oldest turns beyond the last 20 are summarized as non-authoritative context and dropped from model context. Proposal cards and execution traces are excluded; the living memory file is never compacted.
- **Contract types matter:** `futures_inverse` (PI_*): 1 contract = contractSize USD notional, whole contracts. `flexible_futures` (PF_*): 1 contract = 1 unit of underlying, notional = size × price. Sizing is handled server-side for ladders/chases/closes and sizes are rounded to the instrument's precision everywhere.

## Liquidation price

Kraken's REST API does not expose it. The terminal computes it the way Kraken's UI does (verified against a live position to 4 decimals): liquidation when `collateral + unrealizedPnl(p) = maintenanceMargin`, i.e. `liq = entry ∓ (collateral − maintenanceMargin) / (size × contractValue)`, maintenance margin split pro-rata across positions. Labeled "Liq (est)".

## Data flow

- One upstream websocket (`wss://futures.kraken.com/ws/v1`, public) subscribes `ticker` + `trade` for the watchlist; the hub normalizes field names and fans out to browsers over SSE (`/api/stream`).
- Candle history from the public charts API (`/api/charts/v1/trade/{symbol}/{res}`, ms→s converted; that API reports volume 0, so traded volume is merged in from the hub's trade aggregation).
- Scanner + EMA signal from `/api/charts/v1/mark/{symbol}/1m` (ported from the CLI's `volatility.py` and `experimental/ema_volatility_bot`).

## API

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/health` | armed state, env, hub status |
| GET | `/api/instruments` | tradeable instruments |
| GET | `/api/tickers?symbols=A,B` | live snapshot; adds symbols to the feed |
| GET | `/api/candles?symbol=&res=1m..1w` | REST history + hub trade volume |
| GET | `/api/orderbook?symbol=` | top of book |
| GET | `/api/account` `/api/positions` `/api/orders` `/api/fills` | private, cached 2–10s |
| GET | `/api/volatility` | scanner: `window`, `limit`, `minVolume`, `maxSpread` |
| GET | `/api/signal?symbol=` | EMA 400/800 trend signal |
| GET | `/api/stats` | full Kraken account log, projected rows (realized PnL / funding / interest) |
| GET | `/api/equity` | account equity snapshots (30s cadence, realized + unrealized) |
| GET | `/api/chat/history` | persisted chat (server owns history) |
| POST | `/api/chat` `{message, symbol, lastExecution?}` | one new message; server appends + compacts + replies |
| POST | `/api/chat/note` `{role, content}` | store-only message (execution reports) |
| POST | `/api/arm` `{armed, confirm:"yes"}` | arm/disarm order entry |
| POST | `/api/order` `{symbol, side, orderType, size, ...}` | simulated unless armed |
| POST | `/api/cancel` `{cliOrdId}` or `{orderId}` | simulated unless armed |
| POST | `/api/action` `{actions: [...]}` | execute action proposals; dry-run unless armed |
| POST | `/api/chase` / `/api/chase/abort` / `GET /api/chase` | chase lifecycle |
| POST | `/api/chat` → see above | AI reply + proposals |
| GET | `/api/stream` | SSE: `ticker`, `trade`, `status`, `armed`, `chase` |
| GET | `/api/debug/threads` | thread-stack dump (debugging) |
| GET | `/volatility` | standalone volatility pairs page |

## Files

```
terminal/
  run.py            launcher
  server.py         HTTP server: REST API, SSE, order entry guard, chat handler
  db.py             SQLite persistence: sessions, messages, actions_log, events
  actions.py        trade actions: order, ladder/grid, close, replace_tp, chase, cancels + AI prompt
  chase.py          post-only limit chase engine
  scanner.py        volatility scan + EMA signal
  market_hub.py     upstream Kraken websocket -> tickers/trades/1m candles hub
  ai_chat.py        OpenRouter chat: tool calling, compaction summarizer, snapshot builder
  kraken_client.py  REST client (vendored from kraken-futures-cli)
  ws_vendored.py    minimal websocket client (vendored fallback)
  static/           React build output (served by run.py)
  legacy/           original vanilla JS app (kept for reference)
  terminal.db       SQLite: chats, memory, audit trail
frontend/           React (Vite) source, `npm run build` outputs to terminal/static
```

## Not checked / known limits

- Liquidation estimates are pro-rata approximations with multiple positions open.
- The hub keeps ~25h of 1m candles built from the trade feed as a charts-API fallback.
- Stop/take-profit triggers use `triggerSignal=mark` by default.
- Pro-Mode is display-only by design — Kraken rejects orders sized beyond real margin.
- `PI_*` (inverse) symbols may be trade-forbidden on some accounts (`CONTRACT_ACCESS_FORBIDDEN`); the terminal surfaces that error.
