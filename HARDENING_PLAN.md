# Trading terminal hardening plan

Verified against `main` at commit `2b67d65` on 2026-09-04.

This document records the review findings that are worth acting on. It separates confirmed defects from larger design ideas so the work can proceed without turning the terminal into a framework project.

## Decisions

- Keep the existing single ARM/DISARM control.
- Do not add separate manual, AI, or protection arming modes.
- Every server restart must remain DISARMED.
- Do not change or cancel existing live orders during implementation tests.
- Check `/api/health` immediately before any synthetic trading test.
- Prefer deterministic server enforcement over AI prompt instructions.
- Keep work small and testable. Do not optimize bundle size or split large files merely because they are large.

## Priority 0: suspend live Chase

**Status: DONE**

- [x] Disable live Chase in the ticket, direct endpoint, and armed action execution.
- [x] Keep DISARMED action simulation available.
- [x] Deploy the block with a restart to DISARMED and verify positions and exact order IDs are unchanged.
- [x] Replace Chase reconciliation and re-enable it only after every acceptance criterion passes.

The reconciled engine requires nested placement/cancellation success, queries exact order status and fills before classifying disappearance or replacing, recalculates remaining size after cancellation, persists lifecycle evidence, aborts on DISARM, and restores unfinished/orphan alerts on startup. Verification passed 44 backend tests, the frontend suite/lint/build, a read-only live `/orders/status` shape probe, a DISARMED endpoint/action check, and a restart with exact positions and order IDs unchanged. No live Chase order was placed as a deployment test.

Live Chase should not be trusted until its reconciliation logic is replaced.

### Confirmed defects

- `terminal/chase.py:_place()` accepts root `result == "success"` without requiring `sendStatus.status == "placed"`.
- A Chase order missing from `/openorders` is classified as fully filled without checking fills or order status.
- `_cancel()` suppresses every cancellation exception.
- Re-pegging submits a replacement immediately after an unconfirmed cancellation.
- Disarming the terminal does not stop active Chase workers.
- Chase state exists only in process memory. A restart can leave a `ch-*` exchange order without a local worker.

Kraken documents that root-level `result: "success"` only means the request was received and assessed. The nested operation status determines whether the operation happened.

### Temporary change: DONE

Live Chase is disabled in the ticket, `/api/chase`, and armed action execution. DISARMED action simulation remains available. The UI explains that Chase is temporarily disabled pending reconciliation hardening.

### Required Chase behavior

Use explicit states:

```text
PLACING
  -> RESTING
  -> CANCEL_REQUESTED
  -> CANCEL_CONFIRMED
  -> RECONCILING
  -> REPLACING
```

Terminal outcomes:

```text
FILLED | PARTIAL | ABORTED | TIMEOUT | REJECTED | UNKNOWN
```

Rules:

1. Require `sendStatus.status == "placed"` before entering `RESTING`.
2. Assign a unique `cliOrdId` to every placement.
3. Never interpret absence from open orders as proof of a fill.
4. Reconcile by client order ID against authoritative order status and fills.
5. Confirm cancellation before submitting a replacement.
6. Recalculate remaining size from confirmed fills after cancellation.
7. If placement, cancellation, or reconciliation is uncertain, enter `UNKNOWN`, stop placing orders, and alert the UI.
8. Disarming must abort active Chase workers, cancel their resting orders, reconcile the result, and report any uncertainty.
9. On startup, detect open `ch-*` orders and show a persistent orphan-order alert. Startup remains DISARMED and must not cancel them automatically.
10. Persist Chase intent, client IDs, state changes, fills, cancellation results, and final status in SQLite.

### Chase acceptance criteria: DONE

- [x] Post-only rejection never becomes a fill.
- [x] Missing open order never becomes a fill without authoritative evidence.
- [x] Failed or timed-out cancellation never causes a replacement.
- [x] A late fill during cancellation reduces the replacement size correctly.
- [x] Disarming leaves no silently running Chase worker.
- [x] Restart identifies orphan `ch-*` orders without mutating Kraken.
- [x] Tests cover rejection, partial fill, late fill, cancel failure, timeout, disarm, restart, and API-read failure.

## Priority 1: confirmed correctness fixes

**Status: DONE**

Implemented and deployed with 28 passing backend tests, 4 passing frontend tests, a clean frontend lint run apart from pre-existing vendored warnings, and a successful production build. The server restarted DISARMED with unchanged positions and exact order IDs.

These were small confirmed defects and were fixed together with focused tests.

### Websocket fallback

`terminal/market_hub.py:_import_upstream()` imports two names from `ws_vendored` and returns the missing `WebSocketConnection` name. On a machine without the adjacent `kraken-futures-cli` checkout, the market hub can fail with `NameError`.

Fix the fallback import and test it with the adjacent checkout unavailable.

### Demo account-history host

`terminal/account_log.py:_get()` hardcodes `https://futures.kraken.com`. A demo-configured terminal therefore sends history requests to the live host.

Build the URL from `client.base_url`. Test live and demo clients without network access.

The account-log cache does not need account/environment keys while one server process owns one fixed client.

### Nonce synchronization

`KrakenFuturesClient._last_nonce` is shared by HTTP handlers, Chase, protection synchronization, equity polling, and history work without a lock.

Add a dedicated lock around nonce generation and test concurrent calls for uniqueness and monotonic order.

### Fresh Zustand verification

`executeActions()` captures `const s = get()`, awaits `s.refreshTables()`, and then verifies against `s.orders`, which is the old snapshot.

Use:

```javascript
await get().refreshTables();
const freshOrders = get().orders;
```

Prefer exact returned `orderId` or `cliOrdId` over symbol, side, type, and price signatures whenever Kraken provides an ID.

### Negative contract precision

The backend supports negative `contractValueTradePrecision`, such as lots of 100. `Ticket.jsx` and `store.js` clamp it to zero before using `toFixed()`, so the displayed size can differ from the submitted size.

Add one frontend size-normalization helper that supports positive and negative precision. Use it for USD conversion and percentage quick-sizing. Show the normalized contract quantity before submission.

### Configuration load order

`PORT` is read before `terminal/.env` loads.

Load configuration first, then derive `PORT` and create the Kraken client. Keep restart-to-DISARMED. Do not add another live-startup phrase or change the default environment as part of this fix.

### Correctness-fix acceptance criteria: DONE

- [x] Vendored websocket fallback starts without the adjacent CLI checkout.
- [x] Demo history requests target `demo-futures.kraken.com`.
- [x] A 200-request concurrency test produces no duplicate nonces.
- [x] Action verification reads the refreshed Zustand state.
- [x] Negative precision rounds to the same quantity in the UI and backend.
- [x] Configuration loads before `PORT` is read, with a source-order regression test.

## Priority 2: recoverable TP/SL replacement

**Status: DONE**

Exact-ID protection changes now use Kraken `editorder` and require `editStatus.status == "edited"`. Unsupported legacy targets use confirmed cancel/recreate with exact state preservation, known replacement rejection triggers rollback, and uncertain/failed recovery persists an audible `UNPROTECTED` alert until authoritative coverage returns. Verification passed 53 backend tests, 6 frontend tests, lint, build, browser alert checks, and a DISARMED restart with exact positions and order IDs unchanged. No live protection order was edited during deployment verification.

Before this section was implemented, manual and managed protection replacement did:

```text
cancel existing protection
-> submit replacement
```

A rejected or timed-out replacement can leave the position unprotected. The managed position-size synchronization uses the same path.

### Required behavior

1. Carry the exact exchange order ID from the chart to the backend.
2. Preserve the complete existing protection order before changing it.
3. Prefer Kraken Futures `editorder` for trigger and size changes.
4. Require `result == "success"` and `editStatus.status == "edited"`.
5. If edit fails or is uncertain, leave the original order unchanged and report the failure.
6. If edit cannot support the requested change, cancel and confirm before replacement.
7. If replacement fails, recreate the previous order.
8. If rollback also fails, persist an `UNPROTECTED` state and show a red alert with sound until authoritative protection is restored.
9. Never rewrite an intentional partial TP/SL ladder.

Dragging one TP currently sends only symbol and price. It must carry the exact order ID so one line cannot accidentally replace every same-type protection order.

### Protection acceptance criteria: DONE

- [x] A successful edit preserves the same exchange order ID and updates the requested trigger or size.
- [x] An edit rejection leaves the original protection working.
- [x] Cancel/recreate confirms each transition and restores the old order on replacement failure.
- [x] Rollback failure produces a persistent `UNPROTECTED` alert.
- [x] Partial ladders remain untouched unless a user explicitly drags one exact ladder order, in which case its size is preserved.
- [x] Tests use fake clients and DISARMED contexts only.

## Priority 3: converge write handling

**Status: DONE**

All order submissions now receive server-generated client IDs and flow through one timeout-aware executor; all Kraken writes use the shared nested-status parser. Ticket, chart, action-card, AI, protection, and Chase results expose explicit outcomes and persist their evidence. Live batches stop after a non-confirmed result. Verification passed 60 backend tests, 6 frontend tests, lint, build, persisted-outcome checks, and DISARMED ticket/cancel/action probes with unchanged orders. No live order write was used for verification.

Before this section was implemented, writes followed separate paths:

- Ticket and position close use `/api/order`.
- Chart cancellation uses `/api/cancel`.
- Action cards and AI use `actions.py`.
- Chase calls Kraken directly.

Consolidate these incrementally into the existing action engine. Do not build a large generic command framework first.

### Required outcomes

Every submitted order receives a unique `cliOrdId`. Results distinguish:

```text
simulated | confirmed | partial | rejected | unknown
```

- [x] A ladder with only 7 of 10 accepted orders returns `partial`, not top-level success.
- [x] Cancel-all reports partial cancellation failures.
- [x] HTTP timeout is `unknown`; do not blindly retry a write. Reconcile by client ID first.
- [x] Direct ticket, chart, action-card, AI, protection, and Chase paths use the same Kraken response parser.
- [x] Persist intent, submitted parameters, client ID, exchange ID, nested Kraken status, verification evidence, and final outcome.

### Important current nuance

The generic action path already rejects non-`placed` `sendStatus` values. It is not blindly accepting every root success. The larger problem is inconsistent handling across direct endpoints, cancellation, aggregate actions, and Chase.

## Priority 4: localhost write protection

Binding to `127.0.0.1` is useful but not sufficient. The server currently parses JSON regardless of content type and does not validate `Origin`, `Host`, or a session token.

Add:

- A random per-process token required by every write endpoint.
- Token injection into the served app and `X-Terminal-Token` on writes.
- `Content-Type: application/json` enforcement.
- Exact allowed `Origin` and `Host` checks, including the Vite development origin.
- A one-time arming challenge tied to the process token.
- `Path.is_relative_to()` for static path containment.
- `/api/debug/threads` only when an explicit development flag is enabled.

Read-only market endpoints may remain token-free. Tests should prove that cross-origin, wrong-host, missing-token, non-JSON, and replayed arming requests fail without changing ARM state.

## Priority 5: safe AI context compaction

Current compaction has two confirmed problems:

- `db.compact()` marks messages out of context before the summary call succeeds.
- Usage is summed across tool rounds and treated as the next request's context size.
- Compaction runs after the model call, so it cannot prevent an over-context failure.

Use this flow:

```text
select candidates without mutation
-> summarize
-> atomically store summary and mark candidates out of context
```

Track the latest prompt/input token count separately from cumulative billed tokens. Check whether compaction is needed before the next model request.

Acceptance criteria:

- A failed summarization leaves every source message in context.
- Summary update and message hiding commit atomically.
- Tool-heavy rounds do not double-count context size.
- Compaction occurs before a request that would exceed the configured threshold.

## Risk controls after execution hardening

Do not add separate AI arming.

A deterministic server-side risk policy may be useful later, but its numerical limits require an explicit product decision. It should apply equally to manual and AI writes.

Possible rules:

- Maximum order notional.
- Maximum gross and per-symbol exposure.
- Minimum available-margin reserve.
- Maximum daily realized loss.
- Maximum concurrent Chases.
- Maximum permitted market slippage.
- Reject risk-increasing actions when account, order, or mark data is stale.
- Optional PF-only symbol allowlist.

The best product addition after hardening is stop-based position sizing:

```text
risk budget / distance from entry to stop = contract size
```

## Deferred work

Do not prioritize these yet:

- Separate manual and AI ARM controls.
- ARM expiration timers.
- Bundle splitting or large-file refactors without a behavioral reason.
- A new generic command framework before targeted reconciliation works.
- MFE/MAE, trade tags, basis indicators, and expanded analytics.
- Environment-keyed history caching while the process uses one fixed Kraken client.

## Implementation order

1. Disable live Chase.
2. Apply the six correctness fixes and their tests.
3. Rewrite and verify Chase reconciliation.
4. Move protection changes to exact-ID edit with rollback.
5. Consolidate write paths and explicit outcomes.
6. Harden localhost writes.
7. Make compaction atomic and pre-request.
8. Design risk limits and stop-based sizing separately.

## Verification rules

- Use fake clients and DISARMED contexts for trading tests.
- Query `/api/health` immediately before synthetic trading tests.
- Never use existing live orders as test fixtures.
- Compare live positions and exact order IDs before and after any server restart.
- A restart must return the terminal to DISARMED.
- Run Python compilation and the full backend test suite.
- Run frontend unit tests, lint, and production build.
- Run browser checks without confirming any live chart drop.
- Report checks not performed, especially any live exchange transition that remains untested.

## References

- Kraken Futures REST calls and nested operation statuses: <https://support.kraken.com/articles/360022635572-calls-and-returns-rest-api-derivatives>
- Kraken Futures API documentation: <https://docs.kraken.com/api/docs/futures-api/>
