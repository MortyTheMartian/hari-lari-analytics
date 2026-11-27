import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from hari_lari_analytics.sources.moka.checks import last_week_bounds, run_checks
from hari_lari_analytics.sources.moka.client import MokaClient
from hari_lari_analytics.sources.moka.fetch import tables_from_payments
from hari_lari_analytics.sources.moka.tables import Table, naive_timestamp
from hari_lari_analytics.sources.moka.tokens import (
    DAY,
    FileTokenStore,
    SecretManagerTokenStore,
    TokenManager,
)

FIXTURE = Path(__file__).parent / "fixtures" / "moka_payment.json"


@pytest.fixture
def frames():
    payments = json.loads(FIXTURE.read_text())
    return {k: t.to_frame() for k, t in tables_from_payments(payments).items()}


# ---- 1.2 / 1.3 parent key ----------------------------------------------------------
def test_line_payment_id_matches_transaction_id(frames):
    lines, tx = frames["transaction_lines"], frames["transactions"]
    assert len(lines) == 2
    assert lines["payment_id"].isin(tx["id"]).all()
    assert (lines["payment_no"] == "TEST01").all()


def test_clashing_child_field_is_kept_with_prefix(frames):
    assert (frames["transaction_lines"]["child_payment_id"] == 0).all()


def test_children_carry_parent_keys(frames):
    disc = frames["transaction_line_discounts"]
    mods = frames["transaction_line_modifiers"]
    assert disc["discount_name"].tolist() == ["FOUNDER25"]
    assert disc["line_uuid"].tolist() == ["line-uuid-1"]
    assert mods["line_uuid"].tolist() == ["line-uuid-1"]
    assert mods["payment_id"].iloc[0] == "aaaaaaaa-0000-0000-0000-000000000001"


def test_table_add_parent_wins():
    t = Table()
    t.add({"payment_id": 0, "x": 1}, payment_id="real")
    assert t.rows[0]["payment_id"] == "real"
    assert t.rows[0]["child_payment_id"] == 0


# ---- 1.10 timestamps ---------------------------------------------------------------
def test_offset_is_dropped_not_converted():
    assert naive_timestamp("2026-11-22T09:29:17.991000+07:00") == "2026-11-22T09:29:17.991000"
    assert naive_timestamp("not a date") == "not a date"


def test_created_at_is_naive_datetime_with_same_clock_time(frames):
    ts = frames["transactions"]["created_at"].iloc[0]
    assert ts == pd.Timestamp("2026-11-16 10:15:30.123")
    assert frames["transactions"]["created_at"].dt.tz is None


def test_frames_are_parquet_safe(frames, tmp_path):
    for name, df in frames.items():
        df.to_parquet(tmp_path / f"{name}.parquet", index=False)


# ---- 1.6 / 1.7 tokens --------------------------------------------------------------
class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def token(created=0, expires_in=180 * DAY, refresh="r1"):
    return {"access_token": "a1", "refresh_token": refresh, "created_at": created,
            "expires_in": expires_in}  # fmt: skip


def test_no_refresh_with_more_than_30_days_left(tmp_path):
    store = FileTokenStore(tmp_path / "t.json")
    store.save(token())
    calls = []
    mgr = TokenManager(store, clock=Clock(149 * DAY), refresher=lambda t: calls.append(t) or {})
    assert mgr.access_token() == "a1"
    assert calls == []


def test_refresh_when_fewer_than_30_days_left(tmp_path):
    store = FileTokenStore(tmp_path / "t.json")
    store.save(token())
    new = {"access_token": "a2", "expires_in": 180 * DAY, "created_at": 151 * DAY}
    mgr = TokenManager(store, clock=Clock(151 * DAY), refresher=lambda t: dict(new))
    assert mgr.access_token() == "a2"
    saved = store.load()
    assert saved["access_token"] == "a2"
    assert saved["refresh_token"] == "r1"  # not rotated -> kept
    assert mgr.refreshed


def test_rotated_refresh_token_is_saved(tmp_path):
    store = FileTokenStore(tmp_path / "t.json")
    store.save(token())
    new = {"access_token": "a2", "refresh_token": "r2", "expires_in": 1, "created_at": 160 * DAY}
    TokenManager(store, clock=Clock(160 * DAY), refresher=lambda t: dict(new)).ensure_fresh()
    assert store.load()["refresh_token"] == "r2"


def test_check_runs_once_per_process(tmp_path):
    store = FileTokenStore(tmp_path / "t.json")
    store.save(token(expires_in=DAY))  # short-lived: always "about to expire"
    n = []
    mgr = TokenManager(
        store, clock=Clock(0),
        refresher=lambda t: n.append(1) or {"access_token": "a2", "expires_in": DAY},
    )  # fmt: skip
    mgr.access_token()
    mgr.access_token()
    assert len(n) == 1
    with pytest.raises(RuntimeError):
        mgr.force_refresh()


def test_no_refresh_token_means_no_refresh(tmp_path):
    store = FileTokenStore(tmp_path / "t.json")
    store.save(token(refresh=None))
    mgr = TokenManager(store, clock=Clock(179 * DAY), refresher=lambda t: pytest.fail("refreshed"))
    assert mgr.access_token() == "a1"


class FakeSecrets:
    """A Secret Manager client that keeps each version's number, payload and state."""

    PARENT = "projects/p/secrets/moka-oauth-token"

    def __init__(self, fail_disable: Exception | None = None):
        self.versions = []  # [{"name", "data", "state"}]
        self.fail_disable = fail_disable

    @property
    def enabled(self):
        return [v for v in self.versions if v["state"] == "ENABLED"]

    def access_secret_version(self, name):
        assert name == f"{self.PARENT}/versions/latest"
        data = self.enabled[-1]["data"]
        return type("R", (), {"payload": type("P", (), {"data": data})()})()

    def add_secret_version(self, parent, payload):
        assert parent == self.PARENT
        number = len(self.versions) + 1
        self.versions.append(
            {"name": f"{parent}/versions/{number}", "data": payload["data"], "state": "ENABLED"}
        )

    def list_secret_versions(self, request):
        assert request["parent"] == self.PARENT
        assert request["filter"] == "state:ENABLED"
        # newest first, like the API; a pager is only iterated
        return iter([type("V", (), {"name": v["name"]})() for v in reversed(self.enabled)])

    def disable_secret_version(self, request):
        if self.fail_disable:
            raise self.fail_disable
        for v in self.versions:
            if v["name"] == request["name"]:
                v["state"] = "DISABLED"


class ListLog:
    def __init__(self):
        self.warnings = []

    def warning(self, message, **fields):
        self.warnings.append((message, fields))


def test_secret_manager_store_adds_a_version_on_save():
    fake = FakeSecrets()
    store = SecretManagerTokenStore("p", client=fake)
    store.save(token())
    store.save({**token(), "access_token": "a2"})
    assert len(fake.versions) == 2
    assert store.load()["access_token"] == "a2"


def test_save_leaves_only_the_new_version_enabled():
    fake = FakeSecrets()
    store = SecretManagerTokenStore("p", client=fake)
    store.save(token())
    assert [v["state"] for v in fake.versions] == ["ENABLED"]  # the first one is not disabled
    for n in range(2, 6):
        store.save({**token(), "access_token": f"a{n}"})
    # older versions are disabled, not destroyed: they can be enabled again
    assert [v["state"] for v in fake.versions] == ["DISABLED"] * 4 + ["ENABLED"]
    assert store.load()["access_token"] == "a5"


def test_forced_refresh_through_the_store_leaves_one_enabled_version():
    fake = FakeSecrets()
    store = SecretManagerTokenStore("p", client=fake)
    store.save(token())
    manager = TokenManager(
        store, clock=Clock(151 * DAY),  # fewer than 30 days left: ensure_fresh refreshes
        refresher=lambda old: {"access_token": "a2", "expires_in": 180 * DAY},
    )  # fmt: skip
    assert manager.access_token() == "a2"
    assert manager.refreshed
    assert len(fake.versions) == 2 and len(fake.enabled) == 1
    assert json.loads(fake.enabled[0]["data"])["access_token"] == "a2"


def test_failing_to_disable_never_fails_the_save():
    log = ListLog()
    fake = FakeSecrets(fail_disable=PermissionError("secretmanager.versions.disable denied"))
    store = SecretManagerTokenStore("p", client=fake, log=log)
    store.save(token())
    store.save({**token(), "access_token": "a2"})  # must not raise
    assert store.load()["access_token"] == "a2"  # the new token is stored and is the latest
    assert len(log.warnings) == 1
    message, fields = log.warnings[0]
    assert "could not disable the older versions" in message and "PermissionError" in message
    assert fields == {"step": "token"}


# ---- 1.5 client --------------------------------------------------------------------
class FakeResp:
    def __init__(self, status, body=b"{}"):
        self.status, self._body, self.headers = status, body, {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_client_retries_with_backoff_and_logs(tmp_path):
    store = FileTokenStore(tmp_path / "t.json")
    store.save(token(created=10**10))
    seen, sleeps = [], []
    responses = iter([FakeResp(503), FakeResp(429), FakeResp(200, b'{"ok": 1}')])

    def opener(req, timeout):
        seen.append(timeout)
        return next(responses)

    c = MokaClient(TokenManager(FileTokenStore(tmp_path / "t.json")),
                   sleep=sleeps.append, opener=opener)  # fmt: skip
    assert c.get("/v1/x") == {"ok": 1}
    assert seen == [120, 120, 120]
    assert sleeps == [1, 2]
    assert [r[2] for r in c.log] == [503, 429, 200]


# ---- 1.11 checks -------------------------------------------------------------------
NOW = datetime(2026, 11, 23, 4, 0)  # Monday


def tx(*stamps):
    return pd.DataFrame({"id": [str(i) for i in range(len(stamps))],
                         "created_at": pd.to_datetime(list(stamps))})  # fmt: skip


def lines_for(t):
    return pd.DataFrame({"payment_id": t["id"]})


def test_last_week_bounds():
    assert last_week_bounds(NOW) == (datetime(2026, 11, 16), datetime(2026, 11, 23))


def test_zero_payments_fails():
    errors, _ = run_checks(tx(), pd.DataFrame(), NOW)
    assert errors == ["zero payments extracted"]


def test_missing_last_week_fails():
    t = tx("2026-11-10 10:00")
    errors, _ = run_checks(t, lines_for(t), NOW)
    assert any("last week" in e for e in errors)


def test_good_run_passes_without_warnings():
    t = tx("2026-11-22 18:00", "2026-11-17 10:00")
    assert run_checks(t, lines_for(t), NOW) == ([], [])


def test_stale_last_sale_only_warns():
    t = tx("2026-11-18 10:00")
    errors, warnings = run_checks(t, lines_for(t), NOW)
    assert errors == []
    assert "last sale" in warnings[0]


def test_orphan_lines_fail():
    t = tx("2026-11-22 18:00")
    errors, _ = run_checks(t, pd.DataFrame({"payment_id": ["0", "zzz"]}), NOW)
    assert errors == ["1 lines do not match a payment"]


def test_client_retries_on_dropped_connection(tmp_path):
    import http.client

    store = FileTokenStore(tmp_path / "t.json")
    store.save(token(created=10**10))
    calls = []

    def opener(req, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise http.client.RemoteDisconnected("Remote end closed connection without response")
        return FakeResp(200, b'{"ok": 1}')

    c = MokaClient(TokenManager(store), sleep=lambda s: None, opener=opener)
    assert c.get("/v1/x") == {"ok": 1}
    assert [r[2] for r in c.log] == [0, 200]
