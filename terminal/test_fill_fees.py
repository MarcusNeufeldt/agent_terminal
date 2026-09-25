"""fills_with_fees, AST-loaded from server.py without starting the server."""
import ast
import calendar
import threading
import time
import unittest
from pathlib import Path
from typing import Any


def load(rows):
    source = ast.parse(Path(__file__).with_name("server.py").read_text(encoding="utf-8"))
    wanted = {"fills_with_fees", "_quiet"}
    nodes = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    refreshes = []

    class AccountLog:
        @staticmethod
        def cached_rows(client):
            return rows

        @staticmethod
        def full_log(client):
            refreshes.append(1)

    ns = {"Any": Any, "account_log": AccountLog, "client": object(), "time": time, "calendar": calendar,
          "threading": threading, "_fee_refresh": {"at": 0.0}}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "fill_fees", "exec"), ns)
    return ns, refreshes


# The account log books a trade as two rows under one execution id: the contract leg
# (fee None) and the USD leg carrying the fee.
LOG = [
    {"execution": "13b68511", "asset": "pf_hypeusd", "fee": None},
    {"execution": "13b68511", "asset": "usd", "fee": 0.4768},
    {"execution": "0166bf03", "asset": "usd", "fee": 3.038},
    {"execution": None, "info": "funding rate change", "fee": None},
]


class FillFeeTests(unittest.TestCase):
    def test_each_fill_gets_the_fee_booked_under_its_execution_id(self):
        ns, refreshes = load(LOG)
        fills = ns["fills_with_fees"]([
            {"fill_id": "13b68511", "fillType": "maker", "fillTime": "2026-09-25T09:14:04Z"},
            {"fill_id": "0166bf03", "fillType": "taker", "fillTime": "2026-09-25T08:17:06Z"},
            {"error": "fills unavailable"},
        ])
        self.assertEqual([f.get("fee") for f in fills[:2]], [0.4768, 3.038])
        self.assertEqual(fills[2], {"error": "fills unavailable"})
        self.assertEqual(refreshes, [])

    def test_a_fill_newer_than_the_log_waits_for_one_throttled_refresh(self):
        ns, refreshes = load(LOG)
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        fresh = [{"fill_id": "new-exec", "fillTime": now}]
        self.assertIsNone(ns["fills_with_fees"](fresh)[0]["fee"])
        ns["fills_with_fees"](fresh)
        time.sleep(0.2)
        self.assertEqual(refreshes, [1], "a second poll inside 30 s does not refresh again")

    def test_an_old_fill_without_a_fee_does_not_trigger_refreshes(self):
        ns, refreshes = load(LOG)
        self.assertIsNone(ns["fills_with_fees"]([{"fill_id": "gone", "fillTime": "2026-01-01T00:00:00Z"}])[0]["fee"])
        time.sleep(0.1)
        self.assertEqual(refreshes, [])


if __name__ == "__main__":
    unittest.main()
