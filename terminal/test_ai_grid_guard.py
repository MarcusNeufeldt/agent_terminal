"""_grid_entry_block, AST-loaded from server.py without starting the server."""
import ast
import unittest
from pathlib import Path
from typing import Any


def load(grids, working, fail=False):
    source = ast.parse(Path(__file__).with_name("server.py").read_text(encoding="utf-8"))
    nodes = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == "_grid_entry_block"]

    class Db:
        @staticmethod
        def recorded_grids(symbol, side):
            return grids.get((symbol, side), [])

    class GridMove:
        @staticmethod
        def list_grids(db, ctx, symbol, side):
            if fail:
                raise RuntimeError("openorders down")
            return [{"workingOrders": working}]

    ns = {"Any": Any, "db": Db, "grid_move": GridMove, "action_ctx": None, "_normalize_symbol": lambda s: s.upper()}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), "guard", "exec"), ns)
    return ns["_grid_entry_block"]


GRID = {("PF_XLMUSD", "sell"): [{"gridId": "1593:0"}]}


class AiGridGuardTests(unittest.TestCase):
    def test_an_entry_order_beside_a_working_grid_is_refused_and_points_to_move_grid(self):
        block = load(GRID, working=7)
        for name in ("place_order", "place_ladder"):
            result = block(name, {"symbol": "PF_XLMUSD", "side": "sell", "size": 2369, "limitPrice": 0.238})
            self.assertEqual(result["outcome"], "blocked")
            self.assertIn("move_grid", result["error"])

    def test_exits_other_sides_other_tools_and_finished_grids_pass(self):
        block = load(GRID, working=7)
        self.assertIsNone(block("place_order", {"symbol": "PF_XLMUSD", "side": "buy", "size": 1}))
        self.assertIsNone(block("place_order", {"symbol": "PF_XLMUSD", "side": "sell", "reduceOnly": True}))
        self.assertIsNone(block("replace_tp", {"symbol": "PF_XLMUSD", "side": "sell"}))
        self.assertIsNone(block("place_order", {"symbol": "PF_SOLUSD", "side": "sell"}))
        self.assertIsNone(load(GRID, working=0)("place_order", {"symbol": "PF_XLMUSD", "side": "sell"}))

    def test_unreadable_orders_fail_closed(self):
        result = load(GRID, working=7, fail=True)("place_order", {"symbol": "PF_XLMUSD", "side": "sell"})
        self.assertEqual(result["outcome"], "blocked")


if __name__ == "__main__":
    unittest.main()
