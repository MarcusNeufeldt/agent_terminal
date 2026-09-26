import time
import unittest
from decimal import Decimal

import chase as chase_mod
import hyperliquid_chase as hc
from hyperliquid_client import HyperliquidError

SYMBOL = "HL_ETH"


class FakeBackend:
    def __init__(self, exchange, books, positions=None):
        self.exchange = exchange
        self.books = list(books)
        self._positions = positions or []

    def markets(self):
        return {SYMBOL: {"instrument": {"assetId": 1, "contractValueTradePrecision": 3, "tradeable": True}}}

    def quote(self, symbol):
        return self.orderbook(symbol, fresh=True)

    def orderbook(self, symbol, fresh=False):
        bid, ask = self.books.pop(0) if len(self.books) > 1 else self.books[0]
        return {"orderBook": {"bids": [(bid, 5.0)], "asks": [(ask, 5.0)]}, "time": time.time() * 1000}

    def positions(self, fresh=False):
        return {"positions": list(self._positions)}

    def order_status(self, cloid):
        return self.exchange.status(cloid)


class FakeExchange:
    """Scripted per placed order: fill_on_read (nth status read reports filled),
    partial_on_cancel (quantity filled just before a cancel lands)."""

    def __init__(self, script=None, alo_rejects=0, place_unknown=False, ioc_fill=None):
        self.script = list(script or [])
        self.alo_rejects = alo_rejects
        self.place_unknown = place_unknown
        self.ioc_fill = ioc_fill
        self.orders = {}
        self.sent = []
        self.on_status = None

    def submit(self, action, *, placement):
        self.sent.append((action, placement))
        if action["type"] == "cancelByCloid":
            order = self.orders[action["cancels"][0]["cloid"]]
            if order["status"] == "open":
                order["remaining"] -= order["rule"].get("partial_on_cancel", Decimal(0))
                order["status"] = "canceled"
            return {"outcome": "confirmed", "rows": [{"state": "ok"}]}
        order = action["orders"][0]
        tif = order["t"]["limit"]["tif"]
        if tif == "Ioc":
            filled = self.ioc_fill if self.ioc_fill is not None else order["s"]
            return {"outcome": "confirmed", "rows": [{"state": "filled", "oid": 999, "totalSize": filled,
                                                      "averagePrice": order["p"]}]}
        assert tif == "Alo", tif
        if self.place_unknown:
            return {"outcome": "unknown", "uncertain": True, "error": "timeout", "rows": []}
        if self.alo_rejects:
            self.alo_rejects -= 1
            return {"outcome": "rejected", "rows": [{"state": "error"}],
                    "error": "Post only order would have immediately matched, bbo was 100@101"}
        rule = self.script.pop(0) if self.script else {}
        oid = 100 + len(self.orders)
        self.orders[order["c"]] = {"oid": oid, "status": "open", "orig": Decimal(order["s"]),
                                   "remaining": Decimal(order["s"]), "side": "buy" if order["b"] else "sell",
                                   "reads": 0, "rule": rule, "price": order["p"], "reduce": order["r"]}
        return {"outcome": "confirmed", "rows": [{"state": "resting", "oid": oid}]}

    def status(self, cloid):
        order = self.orders.get(cloid)
        if order is None:
            return {"found": False, "orderStatus": "unknownOid"}
        order["reads"] += 1
        if self.on_status:
            self.on_status(order)
        if order["status"] == "open" and order["rule"].get("fill_on_read") == order["reads"]:
            order["status"], order["remaining"] = "filled", Decimal(0)
        return {"found": True, "cliOrdId": cloid, "symbol": SYMBOL, "side": order["side"], "order_id": str(order["oid"]),
                "orderStatus": order["status"], "originalSizeExact": str(order["orig"]),
                "remainingSizeExact": str(order["remaining"])}

    def placed(self, tif="Alo"):
        return [a["orders"][0] for a, _ in self.sent if a["type"] == "order" and a["orders"][0]["t"]["limit"]["tif"] == tif]

    def cancels(self):
        return [a for a, _ in self.sent if a["type"] == "cancelByCloid"]


class FakeCtx:
    def __init__(self, backend, exchange, armed=True):
        self.backend = backend
        self.exchange = exchange
        self._armed = armed
        self.signer_checks = 0

    def armed(self):
        return self._armed

    def verify_signer(self):
        self.signer_checks += 1

    def submit(self, action, *, placement):
        return self.exchange.submit(action, placement=placement)


def run(spec, backend, exchange, armed=True):
    base = {"exchange": "hyperliquid", "symbol": SYMBOL, "side": "buy", "size": 1.0, "reduceOnly": False,
            "timeoutSec": 5, "maxRepegs": 50, "repegSec": 0.05, "finishMarket": False}
    worker = hc.HyperliquidChaseWorker({**base, **spec}, FakeCtx(backend, exchange, armed), lambda *_: None)
    return worker


class HyperliquidChaseTests(unittest.TestCase):
    def test_rests_at_the_best_bid_post_only_and_finishes_when_filled(self):
        exchange = FakeExchange(script=[{"fill_on_read": 1}])
        worker = run({}, FakeBackend(exchange, [(100.0, 101.0)]), exchange)
        worker.run()
        self.assertEqual(worker.status, "filled")
        [order] = exchange.placed()
        self.assertEqual((order["p"], order["s"], order["b"], order["t"]), ("100", "1", True, {"limit": {"tif": "Alo"}}))
        self.assertTrue(order["c"].startswith(hc.CLOID_PREFIX))
        self.assertEqual(exchange.cancels(), [])

    def test_repeg_cancels_reconciles_the_partial_fill_then_places_only_the_remainder(self):
        exchange = FakeExchange(script=[{"partial_on_cancel": Decimal("0.4")}, {"fill_on_read": 1}])
        books = [(100.0, 101.0), (100.5, 101.0)]
        worker = run({}, FakeBackend(exchange, books), exchange)
        worker.run()
        self.assertEqual(worker.status, "filled")
        first, second = exchange.placed()
        self.assertEqual((first["p"], first["s"]), ("100", "1"))
        self.assertEqual((second["p"], second["s"]), ("100.5", "0.6"), "only the unfilled part is re-placed")
        self.assertNotEqual(first["c"], second["c"], "every peg has its own client id")
        self.assertEqual(len(exchange.cancels()), 1)
        self.assertAlmostEqual(worker.filled, 1.0)

    def test_a_post_only_rejection_from_a_moving_book_repegs_instead_of_stopping(self):
        exchange = FakeExchange(script=[{"fill_on_read": 1}], alo_rejects=2)
        worker = run({}, FakeBackend(exchange, [(100.0, 101.0)]), exchange)
        worker.run()
        self.assertEqual(worker.status, "filled")
        self.assertEqual(len([a for a, _ in exchange.sent if a["type"] == "order"]), 3)

    def test_an_unknown_placement_outcome_stops_everything(self):
        exchange = FakeExchange(place_unknown=True)
        worker = run({}, FakeBackend(exchange, [(100.0, 101.0)]), exchange)
        worker.run()
        self.assertEqual(worker.status, "unknown")
        self.assertEqual(len(exchange.sent), 1, "nothing is sent after an uncertain outcome")

    def test_an_entry_timeout_cancels_and_never_crosses(self):
        exchange = FakeExchange()
        worker = run({"timeoutSec": 0.2}, FakeBackend(exchange, [(100.0, 101.0)]), exchange)
        worker.run()
        self.assertEqual(worker.status, "timeout")
        self.assertEqual(len(exchange.cancels()), 1)
        self.assertEqual(exchange.placed("Ioc"), [])

    def test_an_exit_timeout_closes_only_the_remainder_at_market_while_armed(self):
        exchange = FakeExchange(script=[{"partial_on_cancel": Decimal("0.25")}])
        position = {"symbol": SYMBOL, "side": "short", "sizeExact": "3"}
        backend = FakeBackend(exchange, [(100.0, 101.0)], positions=[position])
        worker = run({"timeoutSec": 0.2, "reduceOnly": True, "finishMarket": True}, backend, exchange)
        worker.run()
        [ioc] = exchange.placed("Ioc")
        self.assertEqual((ioc["s"], ioc["r"], ioc["b"]), ("0.75", True, True))
        self.assertEqual(worker.status, "filled")
        self.assertEqual(worker.stop_reason, "timeout_market")
        self.assertEqual(worker.ctx.signer_checks, 1)

    def test_an_exit_timeout_never_goes_to_market_when_disarmed(self):
        exchange = FakeExchange()
        position = {"symbol": SYMBOL, "side": "short", "sizeExact": "3"}
        backend = FakeBackend(exchange, [(100.0, 101.0)], positions=[position])
        worker = run({"timeoutSec": 0.2, "reduceOnly": True, "finishMarket": True}, backend, exchange, armed=False)
        worker.run()
        self.assertEqual(exchange.placed("Ioc"), [])
        self.assertEqual(worker.status, "timeout")

    def test_an_abort_is_cancel_only_even_for_an_exit(self):
        exchange = FakeExchange()
        position = {"symbol": SYMBOL, "side": "short", "sizeExact": "3"}
        backend = FakeBackend(exchange, [(100.0, 101.0)], positions=[position])
        worker = run({"reduceOnly": True, "finishMarket": True}, backend, exchange)
        exchange.on_status = lambda order: worker.abort()
        worker.run()
        self.assertEqual(worker.status, "aborted")
        self.assertEqual(len(exchange.cancels()), 1)
        self.assertEqual(exchange.placed("Ioc"), [])

    def test_reduce_only_is_capped_by_the_position_and_stops_when_flat(self):
        exchange = FakeExchange(script=[{"fill_on_read": 1}])
        position = {"symbol": SYMBOL, "side": "short", "sizeExact": "0.3"}
        worker = run({"reduceOnly": True}, FakeBackend(exchange, [(100.0, 101.0)], positions=[position]), exchange)
        worker.run()
        self.assertEqual(exchange.placed()[0]["s"], "0.3")
        exchange = FakeExchange()
        worker = run({"reduceOnly": True}, FakeBackend(exchange, [(100.0, 101.0)]), exchange)
        worker.run()
        self.assertEqual(exchange.sent, [])
        self.assertEqual(worker.stop_reason, "no_reducible_position")

    def test_an_external_cancel_is_not_replaced(self):
        exchange = FakeExchange()
        exchange.on_status = lambda order: order.update(status="canceled")
        worker = run({}, FakeBackend(exchange, [(100.0, 101.0)]), exchange)
        worker.run()
        self.assertEqual(worker.status, "cancelled")
        self.assertEqual(worker.stop_reason, "externally_cancelled")
        self.assertEqual(len(exchange.placed()), 1)

    def test_snapshot_names_the_venue(self):
        exchange = FakeExchange(script=[{"fill_on_read": 1}])
        worker = run({}, FakeBackend(exchange, [(100.0, 101.0)]), exchange)
        worker.run()
        snap = worker.snapshot()
        self.assertEqual(snap["exchange"], "hyperliquid")
        self.assertEqual(snap["spec"]["exchange"], "hyperliquid")

    def test_published_snapshots_carry_the_resting_peg_price(self):
        exchange = FakeExchange(script=[{"fill_on_read": 1}])
        worker = run({}, FakeBackend(exchange, [(100.0, 101.0)]), exchange)
        snaps = []
        worker.publish = lambda _kind, snap: snaps.append(snap)
        worker.run()
        resting = [s for s in snaps if s["activePrice"] is not None]
        self.assertTrue(resting)
        self.assertEqual((resting[0]["activePrice"], resting[0]["activeSize"]), (100.0, 1.0))
        self.assertIsNone(snaps[-1]["activePrice"])


class SpecAndPreviewTests(unittest.TestCase):
    def test_exits_finish_at_market_and_entries_do_not(self):
        base = {"symbol": "hl_eth", "side": "sell", "size": "0.5", "expectedArmed": False}
        self.assertTrue(hc.parse_spec({**base, "reduceOnly": True})["finishMarket"])
        self.assertFalse(hc.parse_spec({**base, "reduceOnly": False})["finishMarket"])
        for bad in ({"symbol": "PF_ETHUSD"}, {"side": "long"}, {"size": 0}, {"size": True},
                    {"reduceOnly": "yes"}, {"expectedArmed": None}, {"timeoutSec": 99999}):
            with self.assertRaises(HyperliquidError):
                hc.parse_spec({**base, "reduceOnly": False, **bad})

    def test_preview_is_the_first_post_only_order(self):
        exchange = FakeExchange()
        spec = hc.parse_spec({"symbol": SYMBOL, "side": "sell", "size": 0.5, "reduceOnly": True, "expectedArmed": False})
        order = hc.preview(spec, FakeBackend(exchange, [(100.0, 101.0)]))["action"]["orders"][0]
        self.assertEqual((order["p"], order["s"], order["r"], order["t"]["limit"]["tif"]), ("101", "0.5", True, "Alo"))
        self.assertEqual(exchange.sent, [], "a preview sends nothing")


class OrphanDetectionTests(unittest.TestCase):
    def test_hyperliquid_manager_recognises_only_its_own_client_ids(self):
        manager = chase_mod.ChaseManager(lambda *_: None, worker_factory=hc.HyperliquidChaseWorker,
                                         orphan_prefix=hc.CLOID_PREFIX, venue="hyperliquid")
        found = manager.detect_orphans([
            {"cliOrdId": hc.chase_cloid(), "order_id": "5", "symbol": SYMBOL, "side": "buy", "unfilledSize": 1},
            {"cliOrdId": "0x" + "ab" * 16, "order_id": "6", "symbol": SYMBOL, "side": "buy", "unfilledSize": 1},
            {"cliOrdId": None, "order_id": "7"},
        ])
        self.assertEqual([item["activeOrderId"] for item in found], ["5"])
        self.assertEqual(found[0]["exchange"], "hyperliquid", "the Hyperliquid ticket must be able to show it")
        kraken = chase_mod.ChaseManager(lambda *_: None)
        self.assertEqual(kraken.detect_orphans([{"cliOrdId": hc.chase_cloid(), "order_id": "5"}]), [])




class AcknowledgeTests(unittest.TestCase):
    def test_an_unknown_chase_stops_blocking_only_after_an_explicit_check(self):
        published = []
        manager = chase_mod.ChaseManager(lambda kind, payload: published.append(payload),
                                         worker_factory=hc.HyperliquidChaseWorker, orphan_prefix=hc.CLOID_PREFIX)
        exchange = FakeExchange(place_unknown=True)
        worker = manager.start({"exchange": "hyperliquid", "symbol": SYMBOL, "side": "buy", "size": 1.0,
                                "reduceOnly": False, "timeoutSec": 5, "maxRepegs": 5, "repegSec": 0.05},
                               FakeCtx(FakeBackend(exchange, [(100.0, 101.0)]), exchange))
        manager._chases[worker["id"]].join(5)
        self.assertEqual([item["status"] for item in manager.active()], ["unknown"])
        self.assertIn("error", manager.abort(worker["id"]), "an unknown chase cannot be aborted into a new state")
        self.assertEqual(manager.acknowledge(worker["id"])["ok"], True)
        self.assertEqual(manager.active(), [])
        self.assertEqual(published[-1]["status"], "acknowledged")
        self.assertIn("error", manager.acknowledge("missing"))

    def test_acknowledging_a_recovered_chase_persists_under_its_original_id(self):
        published = []
        manager = chase_mod.ChaseManager(lambda kind, payload: published.append(payload), orphan_prefix=hc.CLOID_PREFIX)
        manager.recover([{"id": "abc123", "status": "running", "exchange": "hyperliquid", "activeCliOrdId": None}], [])
        [item] = manager.active()
        self.assertEqual(item["id"], "recovery-abc123")
        manager.acknowledge("recovery-abc123")
        self.assertEqual(manager.active(), [])
        self.assertIn("abc123", {p["id"] for p in published if p["status"] == "acknowledged"},
                      "the next restart reads the original id as acknowledged, not running")


class ChaseStartRouteTests(unittest.TestCase):
    """hyperliquid_chase_start, AST-loaded from server.py without starting the server."""

    def setUp(self):
        import ast
        import threading
        from pathlib import Path
        from typing import Any
        from unittest.mock import Mock
        import hyperliquid_trading
        source = ast.parse(Path(__file__).with_name("server.py").read_text(encoding="utf-8"))
        wanted = {"_hl_chase_unresolved", "hyperliquid_chase_start"}
        nodes = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
        self.assertEqual({node.name for node in nodes}, wanted)
        exchange = FakeExchange()
        backend = FakeBackend(exchange, [(100.0, 101.0)])
        backend.account_configured, backend.network, backend.account_address = True, "mainnet", "0xabc"
        backend.watch, backend.require_agent = Mock(), Mock()
        self.backend, self.exchange = backend, exchange
        self.manager = Mock()
        self.manager.active.return_value = []
        self.manager.start.return_value = {"id": "c1"}
        trader = Mock(network="mainnet", account_address="0xabc", address="0xsigner")
        self.db = Mock()
        self.db.venue_unresolved.return_value = {"items": []}
        self.ns = {"Any": Any, "hyperliquid_chase": hc, "hyperliquid_trading": hyperliquid_trading, "hyperliquid": backend,
                   "db": self.db, "arm_lock": threading.RLock(), "armed": False, "hl_chase_manager": self.manager,
                   "hl_chase_ctx": object(), "HL_CHASE_LIMIT": 5, "hyperliquid_trader": Mock(return_value=(trader, None)),
                   "ensure_agent": lambda trader, prefetched=None: backend.require_agent(trader.address)}
        module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
        exec(compile(module, "isolated_chase_start", "exec"), self.ns)
        self.start = self.ns["hyperliquid_chase_start"]
        self.body = {"symbol": SYMBOL, "side": "buy", "size": 0.5, "reduceOnly": False, "expectedArmed": False}

    def test_disarmed_validates_and_shows_the_first_order_but_starts_nothing(self):
        result = self.start(self.body)
        self.assertEqual(result["outcome"], "simulated")
        self.assertEqual(result["action"]["orders"][0]["t"], {"limit": {"tif": "Alo"}})
        self.manager.start.assert_not_called()
        self.assertEqual(self.exchange.sent, [])

    def test_a_changed_arm_state_is_refused(self):
        with self.assertRaises(HyperliquidError):
            self.start({**self.body, "expectedArmed": True})
        self.manager.start.assert_not_called()

    def test_armed_checks_the_signer_then_starts_one_worker(self):
        self.ns["armed"] = True
        result = self.start({**self.body, "expectedArmed": True})
        self.assertEqual((result["outcome"], result["chase"]), ("confirmed", {"id": "c1"}))
        self.backend.require_agent.assert_called_once_with("0xsigner")
        self.manager.start.assert_called_once()

    def test_unresolved_state_and_limits_block_a_new_chase(self):
        self.manager.active.return_value = [{"status": "unknown", "symbol": SYMBOL}]
        with self.assertRaisesRegex(HyperliquidError, "unknown state"):
            self.start(self.body)
        self.manager.active.return_value = [{"status": "running", "symbol": SYMBOL}]
        with self.assertRaisesRegex(HyperliquidError, "One Chase per market"):
            self.start(self.body)
        self.manager.active.return_value = []
        self.db.venue_unresolved.return_value = {"items": [{"requestId": "r1"}]}
        with self.assertRaisesRegex(HyperliquidError, "prior Hyperliquid submission"):
            self.start(self.body)
        self.manager.start.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class UnclearCancelTests(unittest.TestCase):
    """A cancel whose reply is lost is settled from the order status, not declared unknown."""

    def setUp(self):
        import unittest.mock
        from unittest.mock import Mock, patch
        self.sleep = patch("hyperliquid_chase.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.worker = hc.HyperliquidChaseWorker(
            {"exchange": "hyperliquid", "symbol": SYMBOL, "side": "buy", "size": 5.0, "reduceOnly": False,
             "timeoutSec": 5, "maxRepegs": 5, "repegSec": 0.05, "finishMarket": False, "unclearCancelSec": 30},
            Mock(), lambda *_: None)
        self.worker._instrument_row = {"assetId": 1, "contractValueTradePrecision": 2}
        self.worker._base = Decimal(0)
        self.worker._active = {"cloid": "0x63686173" + "e" * 24, "cliOrdId": "0x63686173" + "e" * 24, "orderId": "7",
                               "price": Decimal("1"), "size": Decimal("5"), "seen": Decimal(0), "placedAt": 0}
        self.states = []

        def order_status(cloid):
            state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
            return {"found": True, "cliOrdId": cloid, "symbol": SYMBOL, "side": "buy", "order_id": "7",
                    "orderStatus": state, "originalSizeExact": "5", "remainingSizeExact": "0" if state == "filled" else "5"}
        self.worker.ctx.backend.order_status = order_status

    def test_a_lost_cancel_reply_whose_order_filled_counts_as_filled(self):
        self.worker.ctx.submit.return_value = {"outcome": "unknown", "uncertain": True, "error": "TLS handshake timed out"}
        self.states = ["filled"]
        self.worker._cancel_active()
        self.assertIsNone(self.worker._active)
        self.assertEqual(self.worker.filled, 5.0)

    def test_a_cancel_that_never_arrived_is_sent_again_while_the_order_reads_open(self):
        self.worker.ctx.submit.side_effect = [{"outcome": "unknown", "error": "timeout"}] + [{"outcome": "confirmed"}] * 5
        self.states = ["open"] * 7 + ["canceled"]
        with unittest.mock.patch("hyperliquid_chase.time.monotonic", side_effect=[float(i) for i in range(100)]):
            self.worker._cancel_active()
        self.assertIsNone(self.worker._active)
        self.assertGreaterEqual(self.worker.ctx.submit.call_count, 2, "the cancel is re-sent while the order reads open")
        self.assertLessEqual(self.worker.ctx.submit.call_count, 3, "at most once per RESEND_CANCEL_SEC")

    def test_an_order_still_open_at_the_deadline_stays_unknown(self):
        self.worker.spec["unclearCancelSec"] = 0
        self.worker.ctx.submit.return_value = {"outcome": "unknown", "error": "timeout"}
        self.states = ["open"]
        with self.assertRaisesRegex(hc.ChaseUnknown, "could not be settled"):
            self.worker._cancel_active()

    def test_background_resolution_settles_an_ended_unknown_chase_without_orders(self):
        self.worker.status, self.worker.state = "unknown", "UNKNOWN"
        self.states = ["open"]
        self.assertFalse(self.worker.try_resolve())
        self.assertIn("still open", self.worker.unknown_reason)
        self.states = ["filled"]
        self.assertTrue(self.worker.try_resolve())
        self.assertEqual((self.worker.status, self.worker.filled), ("filled", 5.0))
        self.worker.ctx.submit.assert_not_called()
