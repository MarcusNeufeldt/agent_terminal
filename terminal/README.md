# Agent Trading Terminal

Kraken Futures trading and Hyperliquid native-perpetual market/account views. The Python HTTP server, trading engine, SSE feeds, and SQLite persistence live here. The React/Vite frontend lives in `../frontend` and builds into `static/`.

## Exchange selection

The sidebar selector switches the entire terminal between Kraken Futures and Hyperliquid. Kraken is the server-start default. A switch disarms under the shared ARM lock, rejects in-flight requests and active or unresolved Chase workers, and reloads each browser tab. Open orders and positions are **not** cancelled or closed. Kraken's automatic protection resizing and TP cleanup pause while disarmed. Review remaining exposure before switching.

Every venue-aware request names its exchange. POSTs also carry the selection epoch, so stale tabs cannot write after a switch, including Kraken -> Hyperliquid -> Kraken. Existing untagged Kraken clients work only before the first switch. SSE sends selection changes to other tabs; browser storage and focus checks also follow the selected venue. The selector stays disabled against an old backend. A backend restart returns to Kraken, rotates the token, and starts disarmed.

Kraken's client, execution context, background workers, and `TERMINAL_DB_PATH` database remain permanently bound to Kraken. Hyperliquid never imports them. Its client, public websocket, and read-only backend live in separate files. Market symbols use `HL_BTC`, `HL_APT`, etc., not Kraken's `PF_*` IDs. Chart layouts and selected symbols have separate browser-storage keys. Ticket drafts, proposals, request IDs, account rows, and chart providers cannot carry over through the full reload.

### Hyperliquid support in this phase

- Native USDC perpetuals only. Spot and HIP-3 markets are not included.
- Public instrument catalog, mark-based watchlist, real last trades, order book, and native Hyperliquid candles. Kraken still uses its existing Binance chart feed.
- Read-only balances, exchange-reported liquidation prices, positions, open orders, and recent fills when `HYPERLIQUID_ACCOUNT_ADDRESS` is configured. Use the actual master or subaccount address, not an API-agent address. No signer or private key is read.
- `HYPERLIQUID_NETWORK=mainnet` is the public-data default; `testnet` selects the separate testnet host. No funds, wallets, or credentials are created by this change.
- Position Last and displayed PnL use actual Hyperliquid trades. Mark updates never replace missing Last. Watchlist prices and 24h change are explicitly mark-based. The account footer shows **withdrawable USDC**, not an invented available-margin estimate. Cumulative funding is separate from Kraken's unsettled funding.
- Missing account configuration and failed reads show unavailable, not an empty account or zero balance. Cached last-known views belong only to Hyperliquid.
- Hyperliquid Grid placement, Chase, ordinary limit-order price dragging, AI/chat, statistics, scanner, and Alt/BTC remain disabled. Manual trading and exact-ID cancellation use the signed-trading gate below. Unsupported reads return 501 without a Kraken fallback.

### Hyperliquid signed trading

Three independent gates must all pass before an action is signed.

1. `HYPERLIQUID_TRADING` names `testnet` or `mainnet`. The default `off` refuses to sign.
2. `HYPERLIQUID_SECRET_KEY` and `HYPERLIQUID_ACCOUNT_ADDRESS` are both valid.
3. The terminal is ARMED, read under the same `arm_lock` as every other write.

DISARMED validates the request and returns the exact wire action as a simulation, matching Kraken's contract. ARMED with trading disabled also simulates, and the message names the missing setting. Neither path signs or sends.

`POST /api/order`, `POST /api/cancel`, `POST /api/leverage` and `POST /api/chart-order` are the Hyperliquid execution routes. Other allowed POSTs perform read-only exchange checks, local reconciliation, fill-history sync or Grid preview. Execution uses scoped, persisted request IDs; unknown submissions are not replayed. See `../HYPERLIQUID_PARITY.md` for current coverage.

Only `order`, `cancel`, `cancelByCloid`, `modify`, `batchModify`, and `updateLeverage` can be signed. `withdraw3`, transfers, and `approveAgent` are refused before signing, so no code path reaches them even with a valid key configured.

Order types map onto Hyperliquid's time-in-force: `mkt`/`ioc` to `Ioc`, `lmt` to `Gtc`, `post` to `Alo`. Stop and take-profit become reduce-only trigger orders, and a trigger without `reduceOnly` is refused. Prices obey five significant figures and `6 - szDecimals` decimals; sizes round **down** so a request never exceeds its intent.

`hyperliquid_signing.py` validates inputs and delegates to the official `hyperliquid-python-sdk==0.24.0`, with `eth-account==0.13.7`. There is no custom cryptographic fallback. Both info and exchange requests use the SDK's HTTP API through `hyperliquid_sdk_http.py`, which bounds responses, rejects redirects and does not retry. `hyperliquid_trading.py` remains the only execution entry point. Prepared action fields, order IDs and nonces stay under the terminal's existing guards rather than being rebuilt by SDK convenience methods.

Tests cover published EIP-155 and key/address vectors, a frozen pre-migration signature, SDK signer recovery across networks, exact prepared payloads and a disposable loopback HTTP server. These checks do not place exchange orders or prove live fill behavior.

A transport failure returns `unknown` with `uncertain: true`, never a silent success, and is never replayed. An HTTP 4xx returns `rejected` with its HTTP status; an exchange `status: "err"` preserves the exchange rejection message.

Use read-only `extraAgents` approval checks for account binding. Legacy signer diagnostics send exchange actions and must not be run as a harmless connectivity check. Their sentinel order ID is not guaranteed unused; a cancellation probe could affect an order.

Hyperliquid chart position handles create full-size reduce-only TP/SL when none of that type exists. Individual TP/SL lines amend one exact ID without changing other ladder orders; stop-limit prices stay unchanged when moving the trigger. Each amendment explicitly confirms Hyperliquid's native always-place behavior: an in-flight fill/cancel may leave an additional reduce-only exit. Prepared actions and a signed 30-second request deadline support observational recovery, never replay. Request expiry does not expire the standing protection order. Ordinary limit-order dragging remains disabled because concurrent fills can replenish its replacement quantity. Mirrored risk lines are display-only.

The Hyperliquid ticket supports Market, Limit, Post-only, IOC and reduce-only triggers. Market orders derive conservative IOC bounds from fresh quotes. Grid placement, Chase, automatic protection resizing and TP cleanup remain disabled for Hyperliquid.

Percentage buttons use `GET /api/trading-capacity`, backed by `activeAssetData`. Its buy/long and sell/short maxima already include current leverage. The ticket floors 25/50/75/100% to contract lots without multiplying leverage again. Reduce-only percentages use current position quantity instead. The server rechecks capacity and expected leverage/margin mode, caps size at the reviewed quantity, and does not infer capacity from balance or withdrawable funds.

Leverage buttons change the actual exchange setting through the SDK, keeping the current cross/isolated mode. Confirmation and a matching ARM snapshot are required; DISARMED requests simulate. Prepared leverage requests have a persisted, signed 30-second expiry and share the unresolved-submission barrier. After a lost response, a matching setting readback only resolves uncertainty once fresh exchange time passes that expiry. It never resends the setting or proves that this request caused the observed value. A mismatch remains blocked for investigation.

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

The original vanilla JS app is kept in `terminal/legacy/` for reference. `/volatility` remains a standalone page (`frontend/public/volatility.html`). The main trading surface is a Vela 0.6.15 workspace; a terminal-owned renderer layer adds ephemeral account and execution overlays without turning live orders into persisted user drawings. The statistics modal still uses lightweight-charts. State lives in a Zustand store (`src/store.js`), and one SSE connection fans live candles into every Vela cell.

Kraken credentials load from `terminal/.env` or process environment variables. A local compatibility fallback checks the author's adjacent `kraken-futures-cli/.env` checkout when present. Copy `.env.example` to `.env`; never commit the populated file. Supported variables include `PORT`, the Kraken credentials and environment, `AI_CHAT_MODEL` (default `google/gemini-3.8-flash`), `CHAT_CONTEXT_LIMIT`, `VITE_DEV_ORIGINS`, and `TERMINAL_DEBUG`.

## Safety model

- The terminal starts **DISARMED** on every server start. Disarmed, orders/cancels/chases return the exact dry-run plan without touching Kraken.
- Arming requires clicking the sidebar button and typing `ARM`. The server then consumes a one-time challenge signed with its per-process token. Armed state is in-memory only.
- Every POST requires JSON, an exact local Host and Origin, and the per-process token injected into the served app. Restarting the server invalidates open tabs, so reload before the next write. `/api/debug/threads` exists only with `TERMINAL_DEBUG=true`.
- The AI assistant reads everything itself (market data, positions, account, fills, contract specs, performance) and can also place orders directly through the same tools the Execute button uses - **all write tools are ARM-gated**: disarmed they return the exact plan simulated, armed they hit the live account. `propose_actions` cards (human clicks Execute) remain available for draft/plan requests; the chase engine stays propose-only.
- Every intentional HTTP write carries a persisted request ID. Replays return the stored result instead of submitting again; pending or interrupted requests return `unknown`. Every submitted order also receives a fresh exchange client ID.
- Account, position, order, and market failures remain `unavailable`, with last-known data and age shown in the UI and passed to the AI. New exposure requires a current account response and market data no more than five seconds old.
- Ticket, chart, action-card, AI, protection, and Chase writes share the same nested-status parser and report `simulated`, `confirmed`, `partial`, `rejected`, or `unknown` in `actions_log`.

## Layout

- **Sidebar** — env/armed/feed badges, instrument search, live watchlist, balance + available margin (with **Pro-Mode** toggle: display-only +$4,400 sticker — never feeds sizing), arm toggle.
- **Header** — last price, 24h stats, mark/index, funding, open interest, **Stats** (performance modal), **Volatility Pairs** button, fill-sound bell, and a pulsing **risk chip** (worst position's distance to liquidation) whenever any position is under 12% from LIQ.
- **Chart workspace** — Vela candles (1m–1w), exact instrument tick formatting, persisted layouts, and independent symbols per cell. Click a chart to make it active; the header, sidebar, order book, ticket, scanner, and AI then follow that chart. The layout menu supports one chart, side-by-side charts, stacked charts, and larger grids. History comes from the terminal `/api/candles` endpoint; one shared `/api/stream` feed updates every visible cell from Binance USDT-M 1m klines (`PF_X` → `XUSDT`, XBT→BTC), while Kraken remains the sole trading authority. Terminal-owned overlays show position entry, liquidation estimate, TP/SL triggers, and open limits on every visible symbol. Every order has a chart-side × control with confirmation before cancellation. Drag a TP line to amend its exact order, or drag the `TP / SL` position handle to preview tick-snapped full-position protection with live estimated PnL; live drops require confirmation. Managed protection still auto-resizes by exact-ID `editorder`, partial ladders are never collapsed, and failed rollback raises the persistent audible `UNPROTECTED` alert. The RISK toggle shows display-only 1:1, 1:2, and 1:3 mirrored loss levels; FIT RISK adds them to Vela autoscale on demand. EMA 400/800 follows the active chart.
- **Order book** — top levels with depth bars, ~2.5s refresh.
- **Order ticket** — market / limit / post-only / stop / take-profit / **chase**; % of available margin quick-sizing with a **leverage selector** (1x–10x, persisted); reduce-only. Sizes and prices are rounded server-side to the instrument's contract precision / tick.
- **Grid tab** uses explicit start/end prices, 2–20 orders, and total contracts or USD notional. The position-row Grid shortcut copies the current position size; ½×/1×/2× buttons adjust it. Best bid/ask and percentage buttons fill the range. The server calculates tick-aligned rungs and lot-sized quantities, shown in a table and temporary chart lines before submission. Post-only is the default. Reduce-only exits preserve existing TP/SL and cannot exceed the current position after existing limit exits. One ARM-gated, idempotent request places the grid sequentially and stops at the first non-confirmed rung. A lost response can be checked with the same request ID. DISARMED submissions simulate only. No automatic cancellation or replenishment occurs. AI `place_ladder` also supports exact ranges and total contracts in one call.
- **Bottom tabs** — Positions (Kraken last price and last-price uPnL per tick, mark-based risk distances, per-row close), Orders (per-row cancel, filtered cancel-all), Fills (newest first), **Scanner**. The stop icon performs an emergency flatten: it confirms every market close before canceling the current global order set, then reports success only after a final account reread is flat with no open orders. The double-chevron icon performs a soft flatten with one reduce-only Chase exit per open position. Each position row also has a double-chevron button beside Close, scoped to that position only. Emergency flatten stops/reconciles active Chase workers only after every close is confirmed and before global cancellation; soft flatten refuses to stack over an existing active or unresolved Chase.
- **Scanner** — perpetuals ranked by realized volatility from closed 1m mark candles; click a row to open its chart.
- **AI assistant** — persistent chat (SQLite), direct ARM-gated execution, optional proposal cards, audited tool calls, execution reports.

### Cleanup after take profit

A background monitor observes reduce-only TPs covering the full position. Once the exact TP reports full execution and fresh position data confirms the pair is flat, it stops the pair's captured Chase workers and cancels remaining orders by exact ID. Other pairs are untouched. Cleanup progress survives restart, stops on uncertainty, and appears in notifications and `/api/tp-cleanup`.

The shared ARM gate applies. While DISARMED, cleanup is deferred. Orders added after a deferred cleanup was captured are not included. A reopened position pauses unfinished cleanup rather than risking its protection. Partial TP fills, partial TP ladders, price touches, and manual closes do not trigger this full-position cleanup. Monitoring starts from observed live TP orders, not arbitrary historical fills.

### Moving a grid

The AI tools `get_grids` and `move_grid` use the grid's saved execution record and exact exchange order IDs. `move_grid` shifts only currently working entry-limit orders, preserving absolute spacing and remaining quantities. The nearest working rung anchors to last price by default, or an explicitly selected best bid/ask. Crossing prices and unrelated-order overlaps fail before amendments. Existing TP/SL is untouched.

Moves use price-only `editorder` calls, never cancel/recreate. Each amendment intent and result is persisted in SQLite. Resume an interrupted move with its `operationId`; its original target stays fixed. Confirmed edits are not repeated. An uncertain edit is reconciled against the exact working order before continuing; ambiguity stays blocked. Filled/cancelled rungs are not replenished, and missing or ambiguous grid identity requires clarification.

Chat appends execution receipts to replies, including round-limit and post-execution model failures. After a non-confirmed write, further writes in that chat turn are blocked while read tools remain available.

## The AI assistant

Backed by OpenRouter (key from `~/.pi/agent/auth.json`) with **native tool calling**:

- `propose_actions` drafts review cards only when explicitly requested; chase remains proposal-only.
- **Read tools**: `get_market_data`, `get_positions`, `get_account`, `get_orders`, `get_fills`, `get_instrument`, `get_performance`, `get_chases`, `get_trade_history`, and `scan_markets`.
- **Write tools (ARM-gated, immediate)**: `place_order`, `place_ladder`, `close_position`, `replace_tp`, `replace_sl`, `cancel_order`, and `cancel_all_for_symbol` — thin wrappers over the same `actions.py` engine the Execute button uses.
- `update_memory` stores durable preferences, plans, lessons, and target levels only. Live positions, working orders, and protection are always read from Kraken.
- Every message carries a live snapshot with account, positions, complete open-order IDs/sizes/triggers, mark/last, EMA signal, symbol metadata, and recent candles.
- Sequential action batches invalidate cached state after every action and wait for a close to settle before sizing later protection.
- After execution, the server verifies the action against the live book and persists the report. Every AI tool call and bounded result is also written to SQLite as an `ai_tool` event.

## Alt/BTC modal

The header's **Alt/BTC** button opens a read-only, searchable relative-strength list with **1h**, **6h**, and **24h** selectors. The default is 24h. Binance USDT-M quotes are filtered to tradable, unexpired Kraken `PF_*` crypto contracts, excluding BTC. Sort best/worst first or show only outperformers/underperformers. Clicking a coin opens its Kraken USD chart, not a BTC-denominated order ticket.

Relative return is `(ALT_last / ALT_open) / (BTC_last / BTC_open) - 1`, not a subtraction of percentage changes. Price in BTC is the derived ALT/USDT divided by BTC/USDT ratio. `GET /api/alt-btc?window=24h` shares one bulk public Binance fetch per minute. The 1h and 6h windows use the same 72 completed five-minute candles per matching coin, cached until the next candle boundary with at most eight concurrent public requests. Both windows end at the same BTC candle close, not at a still-forming candle. The modal shows that timestamp; volume also covers the selected window. The first short-window load takes longer, but switching between 1h and 6h reuses the cache.

Stale, invalid, unavailable, or incomplete windows are omitted with a count. Missing benchmark/source data produces an explicit unavailable response, never invented zero returns or a fallback to 24h. Rate-limit responses stop the remaining requests and respect a cooldown. The modal aborts obsolete responses when switching windows and stops polling when closed.

## Chase engine (`chase.py`)

A verified exchange-side cancellation ends Chase as `cancelled` or `partial`, preserving fills and never replacing that order automatically. Exact terminal status or historical cancellation evidence resolves saved warnings on startup without exchange writes. Missing quantities, contradictory identity, and unavailable data remain blocked. Reduce-only Chase stops without submitting when fresh positions show no exposure it can reduce.

Live Chase uses unique client IDs, strict nested Kraken statuses, exact order-status and fill reconciliation, confirmed cancellation before replacement, disarm aborts, and startup orphan alerts. It places post-only limits at best bid (buy) / best ask (sell) and re-pegs until filled, timeout, or max re-pegs. Soft-flatten workers set `reduceOnly` on every placement, re-check/cap size against the current position before startup and every replacement, and refuse to stack over unresolved orphan/unknown Chase orders. Also available from the CLI: `python -m kraken_futures_cli chase SYMBOL buy|sell SIZE [--chase-timeout --repeg --max-repegs --offset --no-wait --json --terminal]`.

## Persistence (`db.py`, SQLite WAL — `terminal.db`)

- `sessions` — one auto-continuing Trading session, latest prompt tokens, cumulative billed tokens, and the AI's **living memory file**
- `messages` — full chat history with proposal payloads; survives refreshes and server restarts
- `actions_log` — every executed action batch with results
- `write_requests` — request identity, canonical input, pending/completed state, HTTP status, and replayable result
- `events` — arm toggles, executions, managed-protection edits, chase lifecycles, bounded AI tool calls/results, compactions, rejections
- `protection_alerts` — active `UNPROTECTED` states retained until live order coverage is restored
- **Compaction** — before each model request, a conservative estimate checks the pending prompt against `CHAT_CONTEXT_LIMIT`. Older turns beyond the last 20 are selected without mutation, summarized, then hidden in the same SQLite transaction that stores the summary. A failed summary leaves them visible. Proposal cards and execution traces are excluded; the living memory file is never compacted.
- **Contract types matter:** the terminal submits only `PF_*` flexible futures, where 1 contract = 1 unit of underlying and notional = size × price. Sizes are rounded to each instrument's precision for tickets, ladders, Chase, protection, and closes.

## Liquidation price

Kraken's REST API does not expose it. The terminal computes it the way Kraken's UI does (verified against a live position to 4 decimals): liquidation when `collateral + unrealizedPnl(p) = maintenanceMargin`, i.e. `liq = entry ∓ (collateral − maintenanceMargin) / (size × contractValue)`, maintenance margin split pro-rata across positions. Labeled "Liq (est)".

## Data flow

- One upstream websocket (`wss://futures.kraken.com/ws/v1`, public) subscribes `ticker` + `trade` for the watchlist; the hub normalizes field names and fans out to browsers over SSE (`/api/stream`).
- Candle history from the public charts API (`/api/charts/v1/trade/{symbol}/{res}`, ms→s converted; that API reports volume 0, so traded volume is merged in from the hub's trade aggregation).
- Scanner + EMA signal from `/api/charts/v1/mark/{symbol}/1m` (ported from the CLI's `volatility.py` and `experimental/ema_volatility_bot`).

## API

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/health` | armed state, env, hub status, exchange and routing epoch |
| GET | `/api/exchanges` | process-wide selection and venue capabilities |
| POST | `/api/exchange?exchange=CURRENT` | `{exchange: TARGET}`; requires token and current `X-Terminal-Exchange-Epoch`; disarms and switches |
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
| POST | `/api/flatten` `{mode:"emergency|chase", requestId, symbol?}` | idempotent flatten; optional PF_ symbol scopes soft close to one position |
| POST | `/api/chase` / `/api/chase/abort` / `GET /api/chase` | chase lifecycle |
| POST | `/api/chat` → see above | AI reply + proposals |
| GET | `/api/stream` | SSE: `ticker`, `trade`, `status`, `armed`, `chase` |
| GET | `/api/debug/threads` | thread-stack dump (debugging) |
| GET | `/volatility` | standalone volatility pairs page |

## Files

```
terminal/
  run.py            launcher
  server.py         shared HTTP/security and explicit venue dispatch; legacy Kraken handlers
  exchange_routing.py    shared selection epoch, request leases, and disarm-on-switch guard
  hyperliquid_client.py  allowlisted read-only info API and field validation
  hyperliquid_backend.py native-perp normalization and isolated read-only routes/cache
  hyperliquid_feed.py    separate lazy public trades/context/candle websocket
  hyperliquid_signing.py validated official-SDK signing bridge
  hyperliquid_sdk_http.py bounded official-SDK HTTP transport
  hyperliquid_trading.py allowlisted signed actions; the only /exchange transport
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
- Trading endpoints reject `PI_*` inverse symbols; executable symbols must use the `PF_*` family.
- The reconciled Chase engine is enabled, but a controlled Kraken demo lifecycle has not been run.
