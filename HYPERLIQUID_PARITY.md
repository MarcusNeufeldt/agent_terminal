# Hyperliquid parity

Target: as much functional parity with Kraken as the venue permits, with separate execution backends. This is not complete. Checkmarks below describe source/test coverage, not live validation.

## Execution foundation

- [x] Explicit venue routing and stale-page epochs; no Kraken fallback.
- [x] Public market data and account views, including unified-account USDC.
- [x] Signed limit, post-only, IOC and reduce-only trigger orders.
- [x] Market Buy/Sell with automatic fresh-book pricing and a 0.5% default slippage limit, configurable from 0.01% to 5%. Orders use conservatively rounded IOC bounds. USD sizing can shrink to fit the prepared nominal budget; partial or no fill remains possible. No manual price is required.
- [x] Exact order-id/client-id cancellation and current-data row controls.
- [x] Shared ARM health and event reporting; final submission serialized with disarm.
- [x] Journal request identity includes Hyperliquid network and account.
- [x] Persist the exact validated order before submission; preserve it immutably for lot-rounded quantity reconciliation.
- [x] Reject incomplete/mismatched response confirmations.
- [x] Read-only exact oid/cloid status lookup; unknownOid does not authorize retry.
- [x] Browser saves submission identity before POST, restores after reload and blocks unresolved resubmission.
- [x] Manual read-only reconciliation of that saved client ID.
- [x] Recent fills retain exact order IDs, fee currency and reported realized PnL.
- [x] Server-owned unresolved submission discovery across browsers/restarts from scoped write journal records.
- [x] Fresh server readback persists separate reconciliation evidence without changing original execution receipts.
- [x] Atomic new-order claim blocks other browser clients while a scoped submission remains unresolved.
- [ ] Full order lifecycle and automated cancellation recovery/adoption. Manual cancellation readback does not authorize replacement.
- [ ] Legacy journal rows without venue/account identity or a client ID need explicit manual investigation; never guess their ownership.
- [x] Bounded userFillsByTime backfill, persistent deduplication, page checkpoints and exact observed per-order totals.
- [x] Retention limits, full single-timestamp pages and page-budget/API gaps are explicit; no false full-history claim.
- [x] Read-only single-order lifecycle checks compare exact exchange quantities with observed fills and persisted validated intent.
- [x] Lifecycle checks return stop on terminal cancellations and never authorize replacement for missing/conflicting fills, triggered children or unknown statuses. No Hyperliquid automation consumes these checks yet.
- [ ] Reconcile automated child orders and amendments across the complete lifecycle before replacing or resizing orders.
- [ ] Historical coverage older than Hyperliquid's latest 10,000 fills requires another authoritative source.
- [x] Reject malformed/duplicate response order IDs, mixed success/error rows, and missing/nonfinite/nonpositive fill fields.
- [x] Refuse confirmation when a returned fill exceeds requested order size.
- [ ] Reconcile summed stored fills with terminal/open order status and all automated child orders.
- [x] Disable execution when read and signing networks disagree.
- [x] Fresh API-agent approval and read/signing account identity checks before each live server submission; failure occurs before signing.
- [x] Migrated signing to the official `hyperliquid-python-sdk==0.24.0` and `eth-account==0.13.7`. Custom Keccak, curve arithmetic, EIP-712 and MessagePack implementations are removed. This is not a claim of an independent security audit.
- [ ] Browser interaction and explicitly authorized live placement/fill/cancel validation.

## Kraken feature parity

- [x] USD notional entry shares exact lot-rounded conversion with the Kraken ticket. Hyperliquid also preserves the entered budget in the immutable request and rejects final rounded order values above it, before signing. This nominal cap excludes fees/funding and does not support market triggers.
- [x] 25/50/75/100% sizing uses fresh, account-bound `activeAssetData` directional maximum trade sizes. It floors exact contract lots without multiplying leverage twice. Reduce-only percentages use current position quantity. Submission rechecks capacity and the expected leverage/margin mode, and never increases the reviewed quantity.
- [x] Exchange leverage buttons submit SDK-signed `updateLeverage` actions, preserve the current cross/isolated mode, require confirmation and matching ARM state, and share durable submission guards. Signed request expiry plus a matching later exchange readback can resolve uncertainty without replay.
- [x] Trigger-price gross PnL and position-coverage estimates for current native-perp positions. Stale/malformed data stays unavailable; estimates exclude fees/funding and never claim a guaranteed loss cap.
- [ ] General available-margin calculation and margin-aware risk previews. Percentage sizing now uses the exchange's per-asset capacity directly; no account-wide available margin is inferred.
- [x] Direct position-row Close uses a confirmed reduce-only market-style IOC with a 0.5% slippage bound. The clicked row's exact size, side, entry and ARM state are checked before submission. A fresh uncached position read distinguishes flatness, residual exposure and unavailable verification. Unknown/simulated submissions never imply closure or trigger retries. Existing orders are not cancelled. Legacy explicit-limit close drafts remain supported. Deployed in `index-CLzL6CU_.js`.
- [x] Native entire-position TP/SL creation from position handles uses zero-size orders with `positionTpsl` grouping. Hyperliquid follows future position-size changes at trigger time; no terminal polling/amendment loop is needed. Chart PnL, coverage, stop observations and order-table quantities use the current position for these orders.
- [x] Confirmed conversion of one exact fixed-size TP/SL through the Orders table's Full position control. Multiple same-kind exits block conversion, preserving intentional partial ladders. Existing fixed-size orders are never silently converted.
- [ ] Automated management of fixed-size protection/partial ladders and persistent unprotected alerts.
- [x] Exact-ID chart cancellation reuses the venue-bound cancel path, with current-data/gate checks and duplicate-click protection.
- [x] Individual native TP/SL dragging with confirmation, current target/position checks, preserved trigger type and reviewed remaining quantity, immutable journal, signed request expiry and read-only recovery. Stop-limit amendments retain their separate limit price. Native trigger replacement can leave an additional reduce-only exit after an in-flight fill/cancel; confirmation explicitly acknowledges this limitation.
- [x] Mirrored TP risk lines and Fit Risk are enabled as display-only controls. Cancelled gestures, changed targets, rejected and simulated requests do not leave a fake moved line.
- [ ] Ordinary limit-order price dragging. Native modification cannot guarantee no remaining-size replenishment across concurrent fills.
- [ ] Chase with restart adoption, exact fills, external cancellation terminal behavior and no blind replacement.
- [x] Preview-only Grid planning reuses Kraken's lot/budget arithmetic and UI, with Hyperliquid side-conservative price precision, distinct-rung checks and minimum-notional warnings. It does not validate margin or working orders.
- [x] Prepared single-symbol batch recovery for 2 to 20 orders. Immutable per-order identities and partial progress survive restart; only complete matched receipts or verified readbacks release the submission guard. Each call reads at most one unresolved identity. Replacement remains forbidden.
- [ ] Grid placement integration and durable exact-ID Grid moves without replenishing fills. UI, API client and server continue to reject Hyperliquid Grid placement.
- [x] Symbol-wide bulk cancellation freezes up to 100 exact IDs, sends one batch, blocks duplicate clicks and reports each result independently.
- [x] Scoped cancellation history and durable independent oid/cloid status snapshots, without changing original receipts. Latest 20 requests are discoverable; the UI also restores older receipts by exact request ID. Missing receipts never prove non-submission, and partial receipts retain row errors and unknown targets.
- [x] Account-wide cancellation for supported native perps freezes up to 100 exact symbol/ID targets in one batch. Confirmation warns that TP/SL orders are included and positions remain open. Recovery retains each target's symbol; spot/HIP-3 are outside this scope.
- [ ] Complete cancellation recovery/adoption for automation.
- [ ] Emergency flatten: close positions, verify flatness, then cancel orders.
- [ ] Soft flatten using reduce-only Chase.
- [ ] AI venue-bound tools, proposal claims, durable receipts and uncertain-outcome handling.
- [ ] Persistent account analytics, realized results, funding, fees and historical statistics.
- [ ] Scanner/relative-strength views with native Hyperliquid universe eligibility.
- [ ] Remove legacy readOnly-as-venue checks in favor of explicit capabilities when these features are implemented.

## Safety boundaries

Do not enable a button merely to imply parity. Each execution feature needs real handler tests, exact identities and failure-path coverage. Never interpret missing state as empty, infer fills from disappearance, retry uncertain writes, or alter Kraken automation to serve Hyperliquid.

Marcus superseded the earlier restriction and authorized deployment, production validation, ARM changes, approvals, transfers and live testing, specifying a minimum-size altcoin trade around $10. Financial trade submission remains manual. No approval or transfer change is currently needed or planned.

## Latest market-close activation

Source and tests now route Hyperliquid Close directly through the existing durable order submission path instead of opening a ticket. Confirmations show market-style execution and slippage, requests are reduce-only, and duplicate clicks/unresolved submissions are blocked. Exact current position and ARM checks run before submission. Confirmed submissions and later reconciliations receive a fresh position readback; a missing or malformed response cannot mean flat. No cleanup/cancellation or automatic retry is added.

388 backend and 91 frontend tests, lint and build pass. After Marcus approved deployment, the terminal was disarmed with no active Chase workers or pending writes. Backend PID 49376 was replaced by 19964; served bundle `index-CLzL6CU_.js` matches the candidate. The original DB was backed up and retained. Kraken's four positions/four exact orders and Hyperliquid's one position/zero orders were unchanged. Fresh position reads and market-close preparation/position validation passed through the real read-only adapter. No order was signed, placed, amended or cancelled. Final state: healthy, Kraken selected, DISARMED, zero pending writes. Reload tabs for the new session token and Close button. Browser interaction and live close execution are untested. Evidence: `C:/Users/marcu/AppData/Local/Temp/hl-market-close-iek9cnj4/`.

## Latest implemented additions

The deployed native sizing change passes 386 backend and 86 frontend tests, lint and a production build. It changes position-handle creation to native entire-position mode and preserves that mode when dragging. A fixed-size exit can be explicitly converted using Full position in Orders. Confirmation covers future position increases/decreases and the native always-place amendment caveat. Fixed-size ladders remain fixed. Recovery recognizes the zero-size sentinel and requires matching native trigger identity before releasing uncertainty.

Activated as backend PID 49376 and verified served bundle `index-C7N8gufa.js`. The original runtime DB was backed up and retained. Kraken's four positions/four exact orders and Hyperliquid's one position/zero orders were unchanged against fresh preflight snapshots. Both native TP and SL prepared successfully through the real read-only adapter without signing or submission. Final state: healthy, Kraken selected, DISARMED, zero pending writes. Hyperliquid had no open orders before activation, so no existing TP was converted. Reload tabs for the new process token and controls. Evidence: `C:/Users/marcu/AppData/Local/Temp/hl-position-tpsl-su6zltqu/`. Protocol: [official position TP/SL behavior](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/take-profit-and-stop-loss-orders-tp-sl) and the public Hyperliquid UI's zero-size position-order builder and modifier. Live native placement/modification and browser interaction are not tested.

Chart protection uses `POST /api/chart-order`. Native trigger modification requires action-level `a: true`, meaning replacement may still be placed if the original disappears during transmission. This is not an atomic conditional edit and local ARM locking cannot prevent remote fills. The user must acknowledge that behavior for each amendment; all supported chart orders are reduce-only. Bare acknowledgments remain unknown. Recovery waits for the signed request deadline, checks the new client ID and the old target's terminal status, and never replays the operation. Other ladder orders are untouched. These controls do not enable ordinary limit-order dragging or a background order-replacement loop.

Protocol source: [official modify semantics](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint). The official SDK supplies signing and HTTP; the terminal preserves the exact prepared action rather than invoking convenience methods that choose new nonces.

Grid preview now offers an explicit read-only order/quote check. It fetches uncached book, positions and orders, rejects missing/stale/future book timestamps, and reuses the existing duplicate-price, post-only-crossing and reduce-only reservation rules. Coverage uses exact position and remaining-order quantities. Ordinary arithmetic previews do not fetch account state. Passing checks never enable placement and do not validate margin or guarantee execution.

The Hyperliquid position table also shows read-only stop observations: no opposite-side reduce-only stop, only smaller stops, a full-size stop, or unknown coverage. Exact quantity comparison avoids floating-point boundary mistakes. Stale, malformed or duplicate-order snapshots cannot establish protection. Combined stop-ladder coverage and guaranteed fills are explicitly not claimed. This is not a persistent alert journal or automated TP/SL management.

Grid batch preparation is also implemented and tested, but not wired to the placement route. It uses one metadata snapshot, requires the reviewed preview hash and distinct per-rung client IDs, rejects wire-rounding differences, and enforces contract/USD budgets and a conservative $10-per-rung minimum. Preparation does not check account state or grant signing permission. Fake-transport tests exercise prepared actions through signing, partial responses, lost responses, immutable journaling and restart recovery without resubmission. UI, client and server Grid placement remain disabled.

The earlier sizing/leverage activation passed 371 backend and 76 frontend tests and deployed `index-CAX-RZRN.js`. The chart activation below supersedes it. Live capacity and quote-based calculations passed without signing or submission. Browser interaction and real exchange execution remain untested.

## Official SDK migration

Both info and exchange requests now use the SDK HTTP API. A bounded-response adapter preserves the existing size caps, blocks redirects, keeps sessions thread-local and never retries. SDK HTTP failures still map to rejected or unknown outcomes without resubmission. The existing action allowlist, exact prepared orders, monotonic nonces, ARM lock, fresh approval checks and scoped journals remain in place. SDK convenience order methods are not used to regenerate previously prepared actions.

Published signing/key vectors, a frozen pre-migration signature, six old/new signature comparisons and a disposable loopback HTTP server passed. The live service passed account, order, quote and recovery reads after restart. No exchange execution endpoint was invoked during verification.

Dependency installation added packages without replacing existing packages. `pip check` reports an unrelated `innertube` / `httpx` version conflict; this migration did not change either package. Quick-size and exchange-leverage controls were implemented after this migration; the SDK migration alone did not provide them.

## Latest chart activation

The chart code passes 381 backend and 82 frontend tests, lint and a production build. Served bundle `index-B4anPXUo.js` matches the candidate byte-for-byte. Tests cover fake pointer drops, declined confirmation, stale identities, partial-ladder targeting, signed expiry, uncertainty and restart recovery. Browser interaction and real exchange modifications have not been tested.

Marcus authorized disarming and deployment after the initial ARMED preflight pause. The first read-only smoke check caught an orders-adapter argument mismatch. It was fixed, a real-signature regression test was added, and both TP and SL preparation passed through the real read-only adapter before the final restart.

Final backend PID: 40272, replacing 11136 after the correction. The original runtime DB was backed up and retained. Kraken's two positions and two exact order tuples were unchanged; Hyperliquid's one position and zero orders were unchanged. Capacity, account and recovery reads passed; Hyperliquid leverage remained cross 10x. TP/SL preparation used read-only exchange calls, with no signing or submission. No order, cancellation, chart-order or leverage execution endpoint was invoked during verification.

Final observed selection was Kraken, healthy and DISARMED, with zero pending writes. Reload browser tabs because process tokens rotated. Final evidence and backup: `C:/Users/marcu/AppData/Local/Temp/hl-chart-final-au8wkhi7/`. Earlier chart activation evidence is retained in `C:/Users/marcu/AppData/Local/Temp/hl-chart-verify-bk3w9jad/`.

## Previous sizing activation

A later preflight confirmed healthy and DISARMED, with no Chase workers or pending writes. The approved restart and paired frontend activation completed.

- Latest backend restart: PID 37016 to 51972 using the verified launcher and interpreter.
- Runtime DB remains `F:/explore/trading_terminal_ui/terminal/terminal.db`; a consistent backup was taken before restart.
- Served bundle: `index-CAX-RZRN.js`, verified byte-for-byte against the candidate. It includes Market Buy/Sell, percentage sizing and actual exchange leverage controls. Grid placement remains disabled and the order-book panel remains removed.
- Tests: 371 backend and 76 frontend pass. Lint/build pass with existing warnings.
- Kraken's four position tuples and four order tuples were unchanged across restart.
- Hyperliquid's one position and zero orders were unchanged against a pre-restart snapshot. Account and recovery reads passed, with zero unresolved submissions or cancellations.
- The deployed capacity endpoint passed both directional percentage checks at 25/50/75/100%. Exchange leverage remained cross 10x. Account-wide available margin remains unavailable rather than inferred.
- Validation temporarily selected Hyperliquid, then restored Kraken. Final observed health was healthy and DISARMED. No exchange orders, cancellations, transfers, approvals or ARM changes were performed.
- Browser interaction and real placement/fill/cancel/leverage execution remain untested. Reload open tabs because restart rotated session tokens.

Previous sizing activation evidence and backup: `C:/Users/marcu/AppData/Local/Temp/hl-capacity-activation-i09n6o6r/`. No order, cancellation or leverage endpoint was invoked during deployment verification.
