"""Incremental, persisted Kraken account log: one small request per refresh instead of
re-downloading the whole history into Kraken's rate limit."""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError

import account_log


def row(i, second):
    return {"id": i, "date": f"2026-09-01T00:00:{second:02d}.000Z", "info": "futures trade", "realized_pnl": i}


class FakeKraken:
    """Serves the log ascending from ?since=<ms>, 1000 rows a page, like Kraken."""

    def __init__(self, rows):
        self.rows, self.calls, self.fail_with = rows, [], []

    def get(self, client, path, params=""):
        self.calls.append(params)
        if self.fail_with:
            raise self.fail_with.pop(0)
        since = int(params.split("since=")[1]) if "since=" in params else 0
        return {"logs": [r for r in self.rows if account_log._row_ms(r) >= since][:1000]}


def too_many():
    return HTTPError("https://x", 429, "Too Many Requests", {}, None)


class AccountLogCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"TERMINAL_DB_PATH": os.path.join(self.tmp.name, "terminal.db")})
        self.env.start()
        account_log._page_cache, account_log._cache_ts, account_log._cache_identity = None, 0.0, None
        self.client = SimpleNamespace(base_url="https://futures.kraken.com", api_key="key-a")
        self.sleeps = patch("account_log.time.sleep")
        self.sleeps.start()

    def tearDown(self):
        self.sleeps.stop()
        self.env.stop()
        self.tmp.cleanup()
        account_log._page_cache, account_log._cache_ts, account_log._cache_identity = None, 0.0, None

    def test_a_refresh_asks_only_for_rows_since_the_newest_held(self):
        kraken = FakeKraken([row(1, 1), row(2, 2), row(3, 3)])
        with patch("account_log._get", kraken.get):
            self.assertEqual([r["id"] for r in account_log.full_log(self.client)], [1, 2, 3])
            kraken.rows.append(row(4, 4))
            rows = account_log.full_log(self.client, force=True)
        self.assertEqual([r["id"] for r in rows], [1, 2, 3, 4])
        self.assertIn(f"since={account_log._row_ms(row(3, 3))}", kraken.calls[-1],
                      "the second refresh starts at the newest held row, not the beginning")

    def test_rows_sharing_the_newest_millisecond_are_not_skipped(self):
        kraken = FakeKraken([row(1, 5)])
        with patch("account_log._get", kraken.get):
            account_log.full_log(self.client)
            kraken.rows.append(row(2, 5))  # same timestamp as the held row
            rows = account_log.full_log(self.client, force=True)
        self.assertEqual([r["id"] for r in rows], [1, 2])

    def test_a_restart_reads_the_saved_log_instead_of_redownloading(self):
        kraken = FakeKraken([row(i, i) for i in range(1, 6)])
        with patch("account_log._get", kraken.get):
            account_log.full_log(self.client)
        saved = json.loads(open(os.path.join(self.tmp.name, "account_log_cache.json"), encoding="utf-8").read())
        self.assertEqual(len(saved["rows"]), 5)
        self.assertNotIn("key-a", json.dumps(saved), "the API key is never written to disk")
        account_log._page_cache, account_log._cache_identity = None, None  # a restart
        kraken.calls.clear()
        with patch("account_log._get", kraken.get):
            self.assertEqual(len(account_log.full_log(self.client)), 5)
        self.assertEqual(len(kraken.calls), 1)
        self.assertIn("since=", kraken.calls[0])

    def test_another_account_never_reads_this_saved_log(self):
        with patch("account_log._get", FakeKraken([row(1, 1)]).get):
            account_log.full_log(self.client)
        other = SimpleNamespace(base_url="https://demo-futures.kraken.com", api_key="key-b")
        self.assertEqual(account_log.cached_rows(other), [])
        with patch("account_log._get", FakeKraken([row(9, 9)]).get):
            self.assertEqual([r["id"] for r in account_log.full_log(other)], [9])

    def test_a_429_backs_off_and_retries(self):
        kraken = FakeKraken([row(1, 1)])
        kraken.fail_with = [too_many(), too_many()]
        with patch("account_log._get", kraken.get):
            self.assertEqual([r["id"] for r in account_log.full_log(self.client)], [1])
        self.assertEqual(len(kraken.calls), 3)

    def test_a_rate_limited_backfill_keeps_its_progress_and_resumes(self):
        kraken = FakeKraken([row(i, i % 60) | {"date": f"2026-09-01T00:{i // 60:02d}:{i % 60:02d}.000Z"}
                             for i in range(1, 2501)])
        real_get = kraken.get

        def flaky(client, path, params=""):
            if len(kraken.calls) >= 1 and "since=" in params and not getattr(flaky, "done", False):
                flaky.done = True
                kraken.fail_with = [too_many()] * 4  # exhausts the backoff
            return real_get(client, path, params)
        with patch("account_log._get", flaky):
            with self.assertRaisesRegex(RuntimeError, "after 1000 new rows"):
                account_log.full_log(self.client)
            self.assertEqual(len(account_log.cached_rows(self.client)), 1000, "the first page is kept")
            rows = account_log.full_log(self.client, force=True)
        self.assertEqual(len(rows), 2500)
        self.assertEqual(len({r["id"] for r in rows}), 2500, "resumed without duplicates")

    def test_a_failed_refresh_with_nothing_new_keeps_what_is_held(self):
        with patch("account_log._get", FakeKraken([row(1, 1)]).get):
            account_log.full_log(self.client)
        with patch("account_log._get", side_effect=OSError("gateway down")):
            with self.assertRaises(RuntimeError):
                account_log.full_log(self.client, force=True)
        self.assertEqual([r["id"] for r in account_log.cached_rows(self.client)], [1])


if __name__ == "__main__":
    unittest.main()
