"""Manual, confirmed native TP/SL chart operations. No retries or cancel/recreate loop."""
import time
from decimal import Decimal

from hyperliquid_client import HyperliquidError
from hyperliquid_fills import decimal_text
from hyperliquid_lifecycle import CANCELED, REJECTED
from hyperliquid_trading import CLOID_RE, format_price, order_action_for


def position_identity(position):
    if position.get('side') not in {'long', 'short'}:
        raise HyperliquidError('Invalid position side')
    return {'side': position['side'], 'sizeExact': decimal_text(position.get('sizeExact'), positive=True),
            'price': decimal_text(position.get('price'), positive=True)}


def order_identity(order):
    oid = str(order.get('order_id', ''))
    if not 1 <= len(oid) <= 20 or not oid.isascii() or not oid.isdigit() or not 0 < int(oid) < 2**64:
        raise HyperliquidError('Exact exchange order ID is required')
    if type(order.get('triggerMarket')) is not bool or order.get('triggerKind') not in {'tp', 'sl'}:
        raise HyperliquidError('Trigger type is unavailable')
    return {'order_id': oid, 'symbol': order.get('symbol'), 'side': order.get('side'),
            'orderType': order.get('orderType'), 'cliOrdId': order.get('cliOrdId'),
            'reduceOnly': order.get('reduceOnly'), 'triggerMarket': order['triggerMarket'],
            'triggerKind': order['triggerKind'],
            'positionTpsl': order.get('positionTpsl') is True,
            'unfilledSizeExact': decimal_text(order.get('unfilledSizeExact'), positive=order.get('positionTpsl') is not True),
            'stopPrice': decimal_text(order.get('stopPrice'), positive=True),
            'limitPrice': decimal_text(order.get('limitPrice'), positive=True)}


def current(backend, symbol, kind, expected_position, expected_order, *, full_position=False):
    positions = [p for p in backend.positions(fresh=True)['positions'] if p.get('symbol') == symbol]
    if len(positions) != 1 or position_identity(positions[0]) != expected_position:
        raise HyperliquidError('Position changed, closed or flipped. Refresh and drag again')
    opposite = 'sell' if expected_position['side'] == 'long' else 'buy'
    orders = [o for o in backend.orders(fresh=True)['orders'] if o.get('symbol') == symbol]
    ids = [str(o.get('order_id', '')) for o in orders]
    if len(ids) != len(set(ids)):
        raise HyperliquidError('Ambiguous order snapshot')
    if expected_order is None:
        # Position handles create only when no protection of this type exists.
        # Existing partial ladders must be moved by their individual exact IDs.
        if any(o.get('reduceOnly') is True and o.get('orderType') == ('stp' if kind == 'sl' else 'take_profit') for o in orders):
            raise HyperliquidError('Protection already exists. Drag its individual line instead')
        return None
    matches = [o for o in orders if str(o.get('order_id')) == expected_order['order_id']]
    if len(matches) != 1 or order_identity(matches[0]) != expected_order:
        raise HyperliquidError('Target order changed or disappeared. Refresh and drag again')
    order = matches[0]
    if (order.get('symbol') != symbol or order.get('side') != opposite or order.get('reduceOnly') is not True or
            order.get('triggerKind') != kind or order.get('orderType') != ('stp' if kind == 'sl' else 'take_profit') or
            (not full_position and not expected_order.get('positionTpsl') and
             Decimal(expected_order['unfilledSizeExact']) > Decimal(expected_position['sizeExact']))):
        raise HyperliquidError('Only matching reduce-only position protection can be dragged')
    if full_position and not expected_order.get('positionTpsl'):
        peers = [o for o in orders if o.get('reduceOnly') is True and o.get('triggerKind') == kind]
        if len(peers) != 1:
            raise HyperliquidError('Multiple same-kind exits exist. Keep the partial ladder or review it manually')
    return order


def prepare(body, backend):
    symbol, kind = body.get('symbol'), body.get('kind')
    if kind not in {'tp', 'sl'} or type(body.get('expectedArmed')) is not bool:
        raise HyperliquidError('TP/SL kind and expected ARM state are required')
    if not isinstance(symbol, str) or not symbol.startswith('HL_'):
        raise HyperliquidError('Native Hyperliquid symbol required')
    instrument = backend.markets().get(symbol, {}).get('instrument')
    if not instrument or not instrument.get('tradeable'):
        raise HyperliquidError('Instrument is not tradeable')
    if not CLOID_RE.fullmatch(str(body.get('cloid') or '')):
        raise HyperliquidError('New immutable client ID required')
    expected = body.get('position')
    if not isinstance(expected, dict):
        raise HyperliquidError('Reviewed position is required')
    expected = position_identity(expected)
    target = body.get('target')
    if target is not None:
        if not isinstance(target, dict) or body.get('acknowledgeReplacement') is not True:
            raise HyperliquidError('Native trigger replacement risk must be acknowledged')
        target = order_identity(target)
        if target.get('cliOrdId') and target['cliOrdId'].lower() == body['cloid'].lower():
            raise HyperliquidError('Replacement requires a new client ID')
    full_position = body.get('fullPosition') is True or bool(target and target.get('positionTpsl'))
    if full_position and body.get('acknowledgeFullPosition') is not True:
        raise HyperliquidError('Full-position protection follows future position changes and must be acknowledged')
    current(backend, symbol, kind, expected, target, full_position=full_position)
    price = format_price(body.get('price'), instrument['contractValueTradePrecision'])
    # Do not intentionally place an already-triggered protection order.
    book = backend.orderbook(symbol, fresh=True)
    stamp = book.get('time')
    if type(stamp) not in (int, float) or not -5000 <= time.time() * 1000 - stamp <= 15000:
        raise HyperliquidError('Fresh quote required')
    bid, ask = (Decimal(str(book['orderBook'][side][0][0])) for side in ('bids', 'asks'))
    if not 0 < bid < ask:
        raise HyperliquidError('Invalid quote')
    above = (expected['side'] == 'long') == (kind == 'tp')
    if not (Decimal(price) > ask if above else Decimal(price) < bid):
        raise HyperliquidError('Protection price is already executable. Drag beyond the current market')
    side = 'sell' if expected['side'] == 'long' else 'buy'
    size = target['unfilledSizeExact'] if target and not full_position else expected['sizeExact']
    market = target['triggerMarket'] if target else True
    limit = target['limitPrice'] if target and not market else price
    action = order_action_for(instrument, side, size, limit, reduce_only=True,
        trigger={'market': market, 'triggerPx': price, 'kind': kind}, cloid=body['cloid'])
    if Decimal(action['orders'][0]['s']) != Decimal(size):
        raise HyperliquidError('Reviewed quantity is not an exact contract lot')
    if full_position:
        # Native position TP/SL uses the zero-size sentinel, not a frozen quantity.
        # The exchange resolves the full position at trigger time, even while offline.
        action['orders'][0]['s'] = '0'
        action['grouping'] = 'positionTpsl'
    if target:
        # Native API requires action-level a=true for trigger modifications.
        # This is NOT conditional cancellation: the user's explicit confirmation
        # authorizes replacement even if the old trigger disappears in transit.
        action = {'type': 'batchModify', 'modifies': [{'oid': int(target['order_id']), 'order': action['orders'][0]}], 'a': True}
    return {'type': 'chartIntent', 'action': action, 'expiresAfter': int(time.time() * 1000) + 30000,
            'symbol': symbol, 'kind': kind, 'position': expected, 'target': target, 'quoteTime': stamp,
            'fullPosition': full_position}


def validate(intent, backend):
    current(backend, intent['symbol'], intent['kind'], intent['position'], intent['target'],
            full_position=intent.get('fullPosition') is True)
    action = intent['action']
    order = action['modifies'][0]['order'] if action['type'] == 'batchModify' else action['orders'][0]
    instrument = backend.markets().get(intent['symbol'], {}).get('instrument', {})
    if instrument.get('assetId') != order['a'] or not instrument.get('tradeable'):
        raise HyperliquidError('Instrument identity or tradeability changed')
    if not -5000 <= time.time() * 1000 - intent['quoteTime'] <= 15000:
        raise HyperliquidError('Reviewed quote expired. Refresh and drag again')


def _misread_trigger(result):
    """A saved 'rejected' that was really Hyperliquid accepting the trigger: older builds read
    the bare 'waitingForTrigger' status as an error. Such a request may well be live, so it
    must stay reconcilable instead of blocking chart edits for good."""
    rows = result.get('rows') if isinstance(result, dict) else None
    return (result.get('outcome') == 'rejected' and isinstance(rows, list) and bool(rows) and
            all(isinstance(row, dict) and row.get('error') == 'waitingForTrigger' for row in rows))


def reconcile(db, backend, request_id, body, intent):
    expiry = intent.get('expiresAfter')
    if type(expiry) is not int or not 0 < expiry < 2**64 or body.get('symbol') != intent.get('symbol'):
        raise HyperliquidError('Invalid chart preparation')
    action = intent['action']
    order = action['modifies'][0]['order'] if action.get('type') == 'batchModify' else action['orders'][0]
    if (order.get('c') != body.get('cloid') or order.get('r') is not True or
            (intent.get('fullPosition') and order.get('s') != '0')):
        raise HyperliquidError('Chart client identity mismatch')
    saved = db.venue_recovery_state(request_id, backend.network, backend.account_address)
    if saved['result'].get('outcome') in {'simulated', 'rejected'} and not _misread_trigger(saved['result']):
        raise HyperliquidError('No uncertain live chart operation to reconcile')
    book = backend.orderbook(intent['symbol'], fresh=True)
    stamp = book.get('time')
    if type(stamp) not in (int, float) or not -5000 <= time.time() * 1000 - stamp <= 15000 or not stamp > expiry + 2000:
        raise HyperliquidError('Wait for the signed chart request to expire, then check again')
    status = backend.order_status(body['cloid'])
    if (not status.get('found') or status.get('uncertain') or
            status.get('orderStatus') not in CANCELED | REJECTED | {'open', 'filled', 'triggered'} or
            str(status.get('cliOrdId') or '').lower() != body['cloid'].lower() or
            status.get('symbol') != intent['symbol'] or status.get('reduceOnly') is not True or
            status.get('side') != ('buy' if order['b'] else 'sell') or
            (not (status.get('positionTpsl') is True and status.get('isTrigger') is True and
                  Decimal(str(status.get('triggerPrice', '0'))) == Decimal(order['t']['trigger']['triggerPx']))
             if intent.get('fullPosition') else Decimal(status.get('originalSizeExact', '-1')) != Decimal(order['s']))):
        raise HyperliquidError('Replacement identity is not confirmed. Do not retry')
    if intent['target']:
        old = backend.order_status(intent['target']['order_id'])
        if (not old.get('found') or old.get('symbol') != intent['symbol'] or
                str(old.get('order_id')) != intent['target']['order_id'] or
                old.get('orderStatus') not in CANCELED | {'filled', 'triggered'}):
            raise HyperliquidError('Original trigger is not known terminal. Replacement remains unresolved')
    evidence = {'kind': 'chart', 'requestId': request_id, 'outcome': 'reconciled', 'state': 'current',
                'status': status, 'expiresAfter': expiry, 'exchangeTime': stamp, 'canReplace': False}
    db.save_write_reconciliation(request_id, evidence)
    return evidence
