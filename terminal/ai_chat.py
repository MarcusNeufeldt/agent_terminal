"""AI chat assistant backed by OpenRouter.

Reads the key from ~/.pi/agent/auth.json (provider "openrouter") at request
time. Each request gets a live account/market snapshot in the system prompt so
the assistant can reason about real balances, positions, and prices.

Action proposals use NATIVE tool calling (propose_actions function): the model
returns structured JSON arguments — no fenced-block parsing. Fenced blocks are
still parsed as a fallback for models that ignore tools.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable

import requests
import time

from actions import ACTIONS_PROMPT, ActionError, normalize_actions

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemini-3.8-flash"


class ChatError(Exception):
    pass


def _load_openrouter_key() -> str:
    path = Path.home() / ".pi" / "agent" / "auth.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        key = data.get("openrouter", {}).get("key")
    except Exception as exc:
        raise ChatError(f"cannot read OpenRouter key from {path}: {exc}") from exc
    if not key:
        raise ChatError("no openrouter key in ~/.pi/agent/auth.json")
    return key


def build_context_snapshot(
    *,
    account: dict[str, Any] | None,
    positions: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    symbol: str,
    ticker: dict[str, Any] | None,
    candles: list[list[float]],
    signal: dict[str, Any] | None = None,
) -> str:
    lines: list[str] = []
    if account:
        lines.append(
            "ACCOUNT: "
            + json.dumps(
                {
                    k: account.get(k)
                    for k in (
                        "balanceValue", "portfolioValue", "collateralValue",
                        "pnl", "availableMargin", "initialMargin", "maintenanceMargin",
                        "type",
                    )
                    if account.get(k) is not None
                }
            )
        )
    else:
        lines.append("ACCOUNT: unavailable (private API not configured or errored)")
    if positions:
        slim = [
            {
                k: p.get(k)
                for k in ("symbol", "side", "size", "price", "netIfClosed", "grossAtBest", "bookWalkCost", "exitFee",
                          "entryFee", "fundingOnClose", "avgExitPrice", "beyondVisibleBook", "netBasis", "krakenMarkPnl",
                          "liqPriceEstimate", "atr14d")
                if p.get(k) is not None
            }
            for p in positions
        ]
        lines.append(f"OPEN POSITIONS: {json.dumps(slim)}")
        nets = [p.get("netIfClosed") for p in positions]
        if nets and all(isinstance(n, (int, float)) for n in nets):
            lines.append(f"NET IF CLOSED, ALL POSITIONS: {round(sum(nets), 2)}")
    else:
        lines.append("OPEN POSITIONS: none")
    if orders:
        slim = []
        for o in orders:
            size = o.get("size")
            if size is None and (o.get("filledSize") is not None or o.get("unfilledSize") is not None):
                size = float(o.get("filledSize") or 0) + float(o.get("unfilledSize") or 0)
            slim.append({
                "orderId": o.get("order_id") or o.get("orderId"),
                "cliOrdId": o.get("cliOrdId"),
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "size": size,
                "unfilledSize": o.get("unfilledSize"),
                "limitPrice": o.get("limitPrice"),
                "stopPrice": o.get("stopPrice"),
                "orderType": o.get("orderType"),
                "reduceOnly": o.get("reduceOnly"),
                "triggerSignal": o.get("triggerSignal"),
            })
        lines.append(f"OPEN ORDERS: {json.dumps(slim)}")
    else:
        lines.append("OPEN ORDERS: none")
    if ticker:
        lines.append(
            f"MARKET {symbol}: "
            + json.dumps(
                {
                    k: ticker.get(k)
                    for k in ("last", "markPrice", "bid", "ask", "open24h", "high24h", "low24h", "change24h", "fundingRate", "openInterest")
                    if ticker.get(k) is not None
                }
            )
        )
    if candles:
        recent = candles[-30:]
        first, last = recent[0], recent[-1]
        hi = max(c[2] for c in recent)
        lo = min(c[3] for c in recent)
        lines.append(
            f"RECENT 1m CANDLES for {symbol}: last close {last[4]}, "
            f"{len(recent)}-candle high {hi:.6g}, low {lo:.6g}, "
            f"open of window {first[1]:.6g} (times are epoch seconds)"
        )
        lines.append("LAST CANDLES (time, open, high, low, close, vol): " + json.dumps(recent[-6:]))
    if signal and not signal.get("error"):
        lines.append(
            f"EMA SIGNAL {symbol}: " + json.dumps(
                {k: signal.get(k) for k in ("side", "price", "emaFast", "emaSlow", "fast", "slow")}
            )
        )
    return "\n".join(lines)


SYSTEM_PROMPT = """You are the AI assistant inside a Kraken Futures trading terminal.
You help with market questions, position math, risk sanity checks, and drafting trades.
For requests to cancel a symbol's buy or sell orders, call cancel_all_for_symbol ONCE with symbol and side. Do not cancel those orders one at a time. Never omit side for a side-specific request, because that could cancel opposite-side protection.
For grid moves, use move_grid, never cancel_all_for_symbol followed by place_ladder.
Use get_grids for saved grid settings and pending operation IDs. Current remaining orders are not the original grid definition.
On retry, resume the saved operationId. Do not replenish filled/cancelled rungs or increase size unless explicitly requested.
Always explain partial/unknown execution and your own cancellations. Execution records in prior replies are authoritative evidence of those actions.

MEMORY:
You maintain a persistent memory file via the `update_memory` tool. It is injected back into
your context on every future message, so it is how you remember across turns even after older
messages are compacted away. Store only durable preferences, strategy rules, lessons, confirmed
target levels, and open plans clearly labelled as plans. Never store current positions, working
orders, protection status, balances, or a proposed/unconfirmed action as completed. The LIVE
SNAPSHOT and read tools are always authoritative over memory. Rewrite the FULL file each time
(it replaces the previous version); keep it under 400 words. Don't call it for pure questions.

""" + ACTIONS_PROMPT + """

TOOLS — READ:
You have live data tools: get_market_data (ticker/orderbook/candles for ANY symbol),
get_positions, get_account, get_orders, get_fills, get_instrument (tickSize/contractSize),
get_chases, get_trade_history, and scan_markets. Use them whenever the snapshot lacks data you
need — NEVER ask the user for prices, balances, fills, order state, or contract specs you can fetch.
get_performance gives net-after-costs aggregates (today/week/month/all-time: price PnL, trading fees,
liquidation penalties, funding, worst symbols); get_trade_history gives exact recent per-symbol executions.
POSITION PnL: always quote netIfClosed, the same "Net if closed" the user sees on screen: the whole
trade's result if closed at market right now, after walking the order book, the exit taker fee, the entry
fee already paid (entryFee, estimated at the taker rate) and funding settled on close. grossAtBest is the
pre-cost value at the best bid/ask; bookWalkCost, exitFee and entryFee are the gap between them.
krakenMarkPnl is Kraken's mark-price figure; never present it as the position's profit.

TOOLS — WRITE (ARM-GATED):
place_order, place_ladder, close_position, replace_tp, replace_sl, cancel_order, cancel_all_for_symbol
execute IMMEDIATELY. ARMED terminal = real orders on the account; DISARMED = simulated plan,
nothing sent. Every result tells you which happened — always state it to the user.
When the user asks you to open, close, cancel or move something, call the tool directly instead
of asking for more confirmation. Sizes are in CONTRACTS; the server rounds prices to the
instrument tick and enforces size precision. Only trade PF_ perpetual contracts.
propose_actions instead renders actions as review cards for the human to click — use it when the
user asks for a draft, plan or proposal rather than execution.

More rules:
- Format answers with GitHub-flavored Markdown. Tables are supported. Never emit LaTeX, TeX delimiters, or commands; write arithmetic as plain text with Unicode symbols (for example: 4.944 × ($2,528 - $2,386)).
- Be concise and concrete. Ground analysis in fetched data and state the numbers.
- Never invent balances, positions or prices that you have not fetched or seen in the snapshot.
- The chase engine (moving post-only peg) stays a human decision — propose it, never start it.
"""

ORDER_BLOCK_RE = re.compile(r"```order\s*(\{.*?\})\s*```", re.DOTALL)
ACTIONS_BLOCK_RE = re.compile(r"```actions\s*(\[.*?\])\s*```", re.DOTALL)
VOLATILE_MEMORY_RE = re.compile(
    r"^\s*(?:(?:#{1,6}\s*)?(?:open|current|active|live)\s+(?:positions?|orders?|protection)\b"
    r"|-\s*PF_[A-Z0-9]+\s*:\s*(?:long|short)\b)",
    re.IGNORECASE | re.MULTILINE,
)


def extract_order_blocks(text: str) -> list[dict[str, Any]]:
    blocks = []
    for match in ORDER_BLOCK_RE.finditer(text):
        try:
            data = json.loads(match.group(1))
            if isinstance(data, dict):
                blocks.append(data)
        except json.JSONDecodeError:
            continue
    return blocks


def extract_action_blocks(text: str) -> list[list[dict[str, Any]]]:
    """Returns one list of actions per ```actions``` block found (fallback path)."""
    blocks = []
    for match in ACTIONS_BLOCK_RE.finditer(text):
        try:
            data = json.loads(match.group(1))
            if isinstance(data, list) and data and all(isinstance(x, dict) for x in data):
                blocks.append(data)
        except json.JSONDecodeError:
            continue
    return blocks


# ---- native tool calling ---------------------------------------------------

ACTION_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["order", "ladder", "close", "replace_tp", "replace_sl", "cancel_all", "cancel", "chase"]},
        "symbol": {"type": "string"},
        "side": {"type": "string", "enum": ["buy", "sell"]},
        "orderType": {"type": "string", "enum": ["mkt", "lmt", "post", "ioc", "stp", "take_profit"]},
        "size": {"type": "number"},
        "limitPrice": {"type": "number"},
        "stopPrice": {"type": "number"},
        "reduceOnly": {"type": "boolean"},
        "notional": {"type": "number"},
        "orders": {"type": "number"},
        "depthPercent": {"type": "number"},
        "orderType_ladder": {"type": "string"},
        "includeCurrent": {"type": "boolean"},
        "percent": {"type": "number"},
        "timeoutSec": {"type": "number"},
        "repegSec": {"type": "number"},
        "maxRepegs": {"type": "number"},
        "offsetTicks": {"type": "number"},
        "cliOrdId": {"type": "string"},
        "orderId": {"type": "string"},
    },
    "required": ["type"],
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "propose_actions",
            "description": (
                "Create review cards only when the user explicitly asks for a draft, plan, proposal, "
                "or review. Explicit execution requests use direct write tools instead. Chase is the "
                "exception and always remains proposal-only. Actions run top to bottom."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "actions": {"type": "array", "items": ACTION_ITEM_SCHEMA, "minItems": 1, "maxItems": 25},
                },
                "required": ["actions"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_data",
            "description": (
                "Fetch live market data for any Kraken futures symbol (e.g. PF_ENAUSD): "
                "mark/bid/ask, 24h stats, funding, orderbook top levels and recent 1m closes. "
                "Use this whenever a symbol's data is needed but missing from the snapshot — "
                "never ask the user for prices or sizes you can fetch yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Instrument symbol, e.g. PF_ENAUSD"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_memory",
            "description": (
                "Rewrite durable memory only: preferences, strategy rules, lessons, confirmed target "
                "levels, and plans labelled as plans. Never store current positions/orders/protection "
                "or treat proposed, simulated, or failed actions as completed. The full file replaces "
                "the previous one."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "memory": {"type": "string", "description": "The complete rewritten memory file, under 400 words."},
                },
                "required": ["memory"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_performance",
            "description": (
                "The user's realized trading performance from the Kraken account log: "
                "net after all costs, price PnL before costs, trading fees, liquidation penalties, "
                "funding, win rate and best/worst symbols "
                "for today / this week / this month / all-time. Use it when the user asks "
                "how they are doing, about their stats or leaks, or when judging whether a "
                "planned trade repeats a past costly pattern."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_chases",
            "description": "Read recent maker-chase status, progress, fills, and events. Does not start or abort a chase.",
            "parameters": {
                "type": "object",
                "properties": {"runningOnly": {"type": "boolean", "description": "Return only currently running chases"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_trade_history",
            "description": "Exact recent executions for one symbol from the full Kraken account log, including side, size, price, realized PnL, funding, and fees.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "PF_ perpetual symbol"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 50},
                    "refresh": {"type": "boolean", "description": "Refresh the account log before reading; defaults true"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scan_markets",
            "description": "Rank tradeable perpetuals by recent realized volatility with volume and spread filters. Read-only and cached.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    "windowMinutes": {"type": "integer", "minimum": 3, "maximum": 60},
                    "minVolumeQuote": {"type": "number", "minimum": 0},
                    "maxSpreadPercent": {"type": "number", "minimum": 0.01, "maximum": 5},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_positions",
            "description": ("Your current open positions: symbol, side, size, entry price, netIfClosed (what closing at "
                            "market now would add, after book depth, taker fee and funding) with its breakdown, "
                            "krakenMarkPnl (Kraken's mark-based figure, not realizable), liquidation estimate."),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_account",
            "description": ("Account state: portfolio/collateral value, available margin, margin used. For position "
                            "PnL use get_positions netIfClosed, not the account's mark-based unrealized total."),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_orders",
            "description": "All currently working (open) orders.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fills",
            "description": "Recent execution fills, most recent first.",
            "parameters": {
                "type": "object",
                "properties": {"symbol": {"type": "string", "description": "Optional filter, e.g. PF_ENAUSD"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_instrument",
            "description": "Contract specs for one symbol: tickSize, contractSize, type, maxLeverage. Check before sizing an order.",
            "parameters": {
                "type": "object",
                "properties": {"symbol": {"type": "string", "description": "e.g. PF_ENAUSD"}},
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "place_order",
            "description": (
                "Place an order IMMEDIATELY. Subject to the terminal ARM state: disarmed returns a "
                "simulated plan (nothing sent), armed executes on the real account. Prices are "
                "server-rounded to the instrument tick. Sizes are in CONTRACTS."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "e.g. PF_ENAUSD"},
                    "side": {"type": "string", "enum": ["buy", "sell"]},
                    "size": {"type": "number", "description": "Contracts, positive"},
                    "orderType": {"type": "string", "enum": ["mkt", "lmt", "post", "stp", "take_profit", "ioc"], "description": "post = post-only limit"},
                    "limitPrice": {"type": "number", "description": "Required for lmt/post"},
                    "stopPrice": {"type": "number", "description": "Trigger price for stp/take_profit"},
                    "reduceOnly": {"type": "boolean"},
                },
                "required": ["symbol", "side", "size", "orderType"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "place_ladder",
            "description": "Place a whole grid in ONE call, not separate place_order calls. For an exact range use startPrice, endPrice and either size (total contracts) OR notional (USD), with 2-20 orders. Buy steps down, sell steps up. For position multiples, read the current position then pass its multiplied total size. Legacy percentage grids use notional plus depthPercent. IMMEDIATE like place_order.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "side": {"type": "string", "enum": ["buy", "sell"]},
                    "notional": {"type": "number", "description": "Total USD notional"},
                    "orders": {"type": "integer", "description": "Number of rungs, 1-20"},
                    "depthPercent": {"type": "number", "description": "Legacy span from current price, percent"},
                    "startPrice": {"type": "number"},
                    "endPrice": {"type": "number"},
                    "size": {"type": "number", "description": "Total contracts, not per rung. Use instead of notional."},
                    "reduceOnly": {"type": "boolean"},
                    "orderType": {"type": "string", "enum": ["lmt", "post"]},
                },
                "required": ["symbol", "side", "orders"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_position",
            "description": "Reduce-only market close of part or all of an existing position. IMMEDIATE.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "percent": {"type": "number", "description": "Percent of the position to close, 1-100 (default 100)"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_tp",
            "description": "Edit an exact take-profit by orderId, or edit/create the only unambiguous managed full-position TP. IMMEDIATE.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "stopPrice": {"type": "number", "description": "New TP trigger price"},
                    "orderId": {"type": "string", "description": "Exact exchange order ID from the live snapshot"},
                },
                "required": ["symbol", "stopPrice"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "replace_sl",
            "description": "Edit an exact stop-loss by orderId, or edit/create the only unambiguous managed full-position SL. IMMEDIATE.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "stopPrice": {"type": "number", "description": "New stop-loss trigger price"},
                    "orderId": {"type": "string", "description": "Exact exchange order ID from the live snapshot"},
                },
                "required": ["symbol", "stopPrice"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_order",
            "description": "Cancel one working order by orderId or cliOrdId. IMMEDIATE.",
            "parameters": {
                "type": "object",
                "properties": {
                    "orderId": {"type": "string"},
                    "cliOrdId": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_all_for_symbol",
            "description": "Cancel matching working orders in ONE call. For 'cancel ZRO buy orders', supply symbol PF_ZROUSD and side buy. Omitting side cancels both sides, including protection; do that only when explicitly requested. IMMEDIATE.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string"},
                    "side": {"type": "string", "enum": ["buy", "sell"], "description": "Required for side-specific cancellation; preserves all orders on the opposite side."},
                    "reduceOnly": {"type": "boolean", "description": "Optional filter: true selects exits, false selects non-reduce-only orders. Omit to include both."},
                },
                "required": ["symbol"],
            },
        },
    },
]


TOOLS.extend([
    {"type": "function", "function": {
        "name": "get_grids", "description": "Read saved grid identity, original settings, working-order counts and pending move IDs. Use this instead of guessing the original grid from remaining orders.",
        "parameters": {"type": "object", "properties": {"symbol": {"type": "string"}, "side": {"type": "string", "enum": ["buy", "sell"]}}, "required": ["symbol", "side"]},
    }},
    {"type": "function", "function": {
        "name": "move_grid", "description": "Move a recorded entry grid in ONE ARM-gated call using exact-ID price amendments. Preserves working quantities and absolute spacing. NEVER cancel/recreate a grid to move it, and NEVER replenish filled or cancelled rungs. Resolves one unambiguous grid automatically; otherwise use get_grids. Defaults to last price, rejects crossing prices before editing. Resume interrupted moves with the returned operationId, keeping the original target. Partial failures must be reported, not hidden.",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string"}, "side": {"type": "string", "enum": ["buy", "sell"]},
            "gridId": {"type": "string"}, "operationId": {"type": "string"},
            "anchor": {"type": "string", "enum": ["last", "best_bid", "best_ask"]},
        }, "required": ["symbol", "side"]},
    }},
])

WRITE_TOOLS = {"place_order", "place_ladder", "close_position", "replace_tp", "replace_sl", "cancel_order", "cancel_all_for_symbol", "move_grid"}


def execution_receipt(name, args, result):
    rows = result.get("results") or result.get("responses") or []
    counts = {}
    for row in rows:
        status = row.get("outcome", "unknown")
        counts[status] = counts.get(status, 0) + 1
    if name == "move_grid":
        counts = result.get("counts", {})
    detail = ", ".join(f"{n} {status}" for status, n in counts.items())
    error = result.get("error") or next((r.get("error") for r in rows if r.get("error")), "")
    identity = f"; operationId={result['operationId']}" if result.get("operationId") else ""
    return f"{name} {args.get('symbol', '')}: {result.get('outcome', 'unknown')}. {detail}{identity}" + (f". {error}" if error else "")


def _compaction_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        message for message in messages
        if not ((message.get("meta") or {}).get("trace")
                or (message.get("meta") or {}).get("actionProposals")
                or (message.get("meta") or {}).get("orderProposals"))
    ]


def summarize_for_compaction(
    messages: list[dict[str, Any]],
    existing_summary: str,
    *,
    model: str | None = None,
    usage_sink: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    """Roll older messages into non-authoritative conversational context."""
    key = _load_openrouter_key()
    model = model or os.getenv("AI_CHAT_MODEL", DEFAULT_MODEL)
    transcript = "\n".join(
        f"{m.get('role', '?')}: {str(m.get('content', ''))[:400]}" for m in _compaction_messages(messages)
    )
    prompt = (
        "Merge durable user preferences, decisions, lessons, and open questions from these older "
        "messages into non-authoritative conversational context under 250 words. Never record current "
        "positions, orders, balances, or protection. Never treat a proposal or assistant claim as proof "
        "that execution occurred; live snapshots and tool results are authoritative.\n\n"
        f"EXISTING SUMMARY:\n{existing_summary or '(none)'}\n\nOLDER MESSAGES:\n{transcript}"
    )
    try:
        resp = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost:8787",
                "X-Title": "Kraken Futures Terminal",
            },
            json={"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": 500},
            timeout=60,
        )
    except requests.RequestException as exc:
        raise ChatError(f"compaction LLM request failed: {exc}") from exc
    if resp.status_code != 200:
        raise ChatError(f"compaction LLM call failed: HTTP {resp.status_code}")
    try:
        data = resp.json()
        summary = data["choices"][0]["message"]["content"].strip()
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
        raise ChatError("compaction LLM returned an invalid response") from exc
    if not summary:
        raise ChatError("compaction LLM returned an empty summary")
    if usage_sink:
        usage_sink(data.get("usage") or {})
    return summary


def _openrouter_completion(key: str, model: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Call OpenRouter and retry one transient HTTP or JSON-level gateway failure."""
    for attempt in range(2):
        try:
            resp = requests.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "http://localhost:8787",
                    "X-Title": "Kraken Futures Terminal",
                },
                json={
                    "model": model,
                    "messages": messages,
                    "tools": TOOLS,
                    "tool_choice": "auto",
                    "temperature": 0.3,
                },
                timeout=90,
            )
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(2.5)
                continue
            raise ChatError(f"OpenRouter request failed: {exc}") from exc

        if resp.status_code in (502, 503, 504) and attempt == 0:
            time.sleep(2.5)
            continue
        if resp.status_code != 200:
            raise ChatError(f"OpenRouter HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ChatError(f"OpenRouter returned invalid JSON: {resp.text[:300]}") from exc

        error = data.get("error") if isinstance(data, dict) else None
        code = str(error.get("code") if isinstance(error, dict) else "")
        if code in {"502", "503", "504"} and attempt == 0:
            time.sleep(2.5)
            continue
        if error:
            raise ChatError(f"OpenRouter error: {json.dumps(error)[:300]}")
        return data

    raise ChatError("OpenRouter transient gateway failure after retry")


def _payload_messages(
    messages: list[dict[str, Any]],
    context_snapshot: str,
    session_summary: str = "",
    session_memory: str = "",
) -> list[dict[str, Any]]:
    system = SYSTEM_PROMPT
    if session_memory:
        system += "\n\nDURABLE MEMORY (never authoritative for live state):\n" + session_memory
    if session_summary:
        system += "\n\nCONVERSATION SUMMARY (non-authoritative older context):\n" + session_summary
    system += "\n\nLIVE SNAPSHOT (AUTHORITATIVE — overrides memory and summary):\n" + context_snapshot
    return [{"role": "system", "content": system}, *messages[-200:]]


def estimate_prompt_tokens(
    messages: list[dict[str, Any]],
    context_snapshot: str,
    *,
    session_summary: str = "",
    session_memory: str = "",
) -> int:
    """Conservative model-independent estimate for a pending OpenRouter request."""
    payload = {"messages": _payload_messages(messages, context_snapshot, session_summary, session_memory), "tools": TOOLS}
    return (len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 2) // 3


def respond(
    messages: list[dict[str, str]],
    context_snapshot: str,
    *,
    model: str | None = None,
    session_summary: str = "",
    session_memory: str = "",
    tool_executor: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    tool_audit: Callable[[dict[str, Any]], None] | None = None,
    max_rounds: int = 5,
) -> dict[str, Any]:
    key = _load_openrouter_key()
    model = model or os.getenv("AI_CHAT_MODEL", DEFAULT_MODEL)
    payload_messages = _payload_messages(messages, context_snapshot, session_summary, session_memory)

    action_blocks: list[list[dict[str, Any]]] = []
    memory_update: str | None = None
    final_text = ""
    receipts = []
    writes_blocked = False
    usage: dict[str, Any] = {
        "prompt_tokens": 0,
        "billed_prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }

    for _round in range(max_rounds):
        try:
            data = _openrouter_completion(key, model, payload_messages)
        except Exception:
            if not receipts:
                raise
            final_text = "The assistant response failed after tool execution. The execution record below is preserved; do not blindly retry."
            break
        u = data.get("usage") or {}
        prompt_tokens = int(u.get("prompt_tokens") or u.get("input_tokens") or 0)
        completion_tokens = int(u.get("completion_tokens") or u.get("output_tokens") or 0)
        usage["prompt_tokens"] = prompt_tokens
        usage["billed_prompt_tokens"] += prompt_tokens
        usage["completion_tokens"] += completion_tokens
        usage["total_tokens"] += int(u.get("total_tokens") or (prompt_tokens + completion_tokens))
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ChatError(f"unexpected OpenRouter response: {json.dumps(data)[:300]}") from exc

        final_text = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            break

        tool_msgs = []
        for tc in tool_calls:
            fn = tc.get("function") or {}
            name = fn.get("name")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            if name == "propose_actions":
                try:
                    fixed = normalize_actions(args.get("actions"))
                except ActionError as exc:
                    content = f"proposal rejected: {exc}. Correct the whole batch and call propose_actions again."
                else:
                    action_blocks.append(fixed)
                    content = "proposals recorded — they render as review cards for the human; do not repeat them in text"
            elif name == "update_memory":
                mem = args.get("memory")
                if not isinstance(mem, str) or not mem.strip():
                    content = "memory update rejected: memory must be a non-empty string"
                elif VOLATILE_MEMORY_RE.search(mem):
                    content = "memory update rejected: store durable preferences/plans only, never current positions, orders, or protection"
                else:
                    memory_update = mem.strip()
                    content = "memory file updated"
            elif tool_executor is not None:
                try:
                    result = {"outcome": "blocked", "error": "A previous write was not confirmed. Inspect state and report before retrying in a new user request."} if writes_blocked and name in WRITE_TOOLS else tool_executor(name, args)
                except Exception as exc:  # tool errors feed back to the model, never crash the chat
                    result = {"error": str(exc)}
                if name in WRITE_TOOLS:
                    summary = execution_receipt(name, args, result)
                    receipts.append(summary)
                    writes_blocked = writes_blocked or result.get("outcome") in {"unknown", "partial", "rejected", "blocked"} or bool(result.get("error"))
                    # Put complete counts before verbose acknowledgments can exhaust the tool budget.
                    result = {"executionSummary": summary, **result}
                content = json.dumps(result, default=str)[:6000]
            else:
                content = f"tool {name} unavailable"
            if tool_audit:
                try:
                    tool_audit({"round": _round + 1, "callId": tc.get("id"), "name": name, "args": args, "result": content[:2000]})
                except Exception:
                    pass  # auditing must not break the trading assistant
            tool_msgs.append({"role": "tool", "tool_call_id": tc.get("id") or "", "content": content})

        payload_messages.append({"role": "assistant", "content": final_text or None, "tool_calls": tool_calls})
        payload_messages.extend(tool_msgs)
    else:
        final_text = "Stopped at the tool-round limit. No further actions were executed."

    # fallback path: fenced blocks for models that ignore tools
    fence_blocks = extract_action_blocks(final_text)
    for block in fence_blocks:
        try:
            block = normalize_actions(block)
        except ActionError:
            continue
        if block not in action_blocks:
            action_blocks.append(block)

    # strip any raw blocks from the displayed text — they render as cards
    clean_text = ORDER_BLOCK_RE.sub("", final_text)
    clean_text = ACTIONS_BLOCK_RE.sub("", clean_text)
    clean_text = re.sub(r"\n{3,}", "\n\n", clean_text).strip()
    if receipts:
        clean_text += "\n\nExecution record:\n" + "\n".join(f"- {line}" for line in receipts)

    return {
        "text": clean_text,
        "orderProposals": extract_order_blocks(final_text),
        "actionProposals": action_blocks,
        "memory": memory_update,
        "usage": usage,
        "model": model,
    }
