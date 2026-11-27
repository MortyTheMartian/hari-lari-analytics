"""The landing zone (plan t12, D26): the manifest contract, the commit order, and the Moka
extraction landing as `moka/<run date>/` with a manifest the JSON schema accepts."""

from __future__ import annotations

import gzip
import json
from datetime import date, datetime
from types import SimpleNamespace

import jsonschema
import pytest

from hari_lari_analytics.core import landing
from hari_lari_analytics.core.landing import Manifest, land, landed_dates, latest_landed
from hari_lari_analytics.core.project import project
from hari_lari_analytics.core.store import GcsStore, LocalStore
from hari_lari_analytics.sources.moka import fetch
from hari_lari_analytics.sources.moka.run import ExtractionFailed, run_extraction
from hari_lari_analytics.warehouse.db import open_run

from .api_fixture import api_tables
from .conftest import FIXTURES
from .fakes import GcsClient

NOW = datetime(2026, 10, 14, 4, 0, 12, tzinfo=project().tz)
SCHEMA = json.loads(landing.schema_path().read_text())
CATALOG = ["items", "item_variants"]


def _manifest(**changes) -> Manifest:
    fields = {
        "source": "moka",
        "run_date": date(2026, 10, 14),
        "run_id": "040012-a3f9",
        "started_at": NOW,
        "finished_at": NOW,
        "tables": {},
        **changes,
    }
    return Manifest(**fields)


class RecordingStore(LocalStore):
    def __init__(self, root):
        super().__init__(root)
        self.calls: list[tuple[str, str]] = []

    def delete(self, key):
        self.calls.append(("delete", key))
        super().delete(key)

    def upload(self, key, path):
        self.calls.append(("upload", key))
        super().upload(key, path)

    def write_text(self, key, text):
        self.calls.append(("write", key))
        super().write_text(key, text)


def test_land_deletes_the_old_manifest_first_and_writes_the_new_one_last(tmp_path):
    staging = tmp_path / "staging"
    (staging / "tables").mkdir(parents=True)
    (staging / "tables" / "a.parquet").write_bytes(b"x")
    (staging / "request_log.csv").write_text("time\n")
    store = RecordingStore(tmp_path / "landing")
    url = land(store, _manifest(), staging)
    assert url == str(tmp_path / "landing" / "moka" / "2026-10-14")
    assert store.calls == [
        ("delete", "moka/2026-10-14/manifest.json"),
        ("upload", "moka/2026-10-14/request_log.csv"),
        ("upload", "moka/2026-10-14/tables/a.parquet"),
        ("write", "moka/2026-10-14/manifest.json"),
    ]


def test_only_folders_with_a_manifest_have_landed(tmp_path):
    store = LocalStore(tmp_path)
    for day in ["2026-10-12", "2026-10-14"]:
        store.write_text(f"moka/{day}/manifest.json", _manifest().to_json())
    store.write_text("moka/2026-10-15/tables/transactions.parquet", "half-written run")
    store.write_text("jibble/2026-10-16/manifest.json", "{}")
    assert landed_dates(store, "moka") == [date(2026, 10, 12), date(2026, 10, 14)]
    assert latest_landed(store, "moka") == date(2026, 10, 14)
    assert latest_landed(store, "moka", date(2026, 10, 13)) == date(2026, 10, 12)
    assert latest_landed(store, "moka", date(2026, 10, 1)) is None
    assert latest_landed(store, "nothing") is None


@pytest.mark.parametrize(
    "change",
    [{"source": "Moka"}, {"run_id": "0400-a3f9"}, {"surprise": 1}, {"manifest_version": 2}],
)
def test_the_manifest_is_strict(change):
    with pytest.raises(ValueError):
        _manifest(**change)


def test_the_run_id_is_the_local_time_and_four_hex_digits():
    assert landing.new_run_id(NOW).startswith("040012-")
    _manifest(run_id=landing.new_run_id(NOW))  # valid


def test_the_columns_are_the_types_written_and_events_have_their_latest_record(tmp_path):
    (tmp_path / "tables").mkdir()
    for name, df in api_tables().items():
        df.to_parquet(tmp_path / "tables" / f"{name}.parquet", index=False)
    df = api_tables()["transactions"]
    entry = landing.table_entry(tmp_path, "transactions", df, "events")
    assert entry.columns["created_at"] == "timestamp[us]"
    assert list(entry.columns) == list(df.columns)
    assert entry.latest_at == df["created_at"].max().isoformat()
    items = landing.table_entry(tmp_path, "items", api_tables()["items"], "snapshot")
    assert items.latest_at is None and items.file == "tables/items.parquet"


# --- the Moka extraction --------------------------------------------------------------------------


class _Frame:
    def __init__(self, df):
        self.df = df

    def to_frame(self):
        return self.df


def _client():
    tokens = SimpleNamespace(ensure_fresh=lambda: None, expires_at=lambda: 1_800_000_000,
                             refreshed=False)  # fmt: skip
    return SimpleNamespace(tokens=tokens, log=[["t", "/v1/x", 200, 10, 0.1, None]])


@pytest.fixture
def moka(monkeypatch):
    """The fetch functions return the fixture tables; a test may replace `tables`."""
    state = SimpleNamespace(tables=api_tables())

    def catalog(client, outs, raw, log):
        raw("items_1", {"items": []})
        return {k: _Frame(v) for k, v in state.tables.items() if k in CATALOG}

    def transactions(client, outs, since, until, raw, log):
        raw("transactions", {"payments": []})
        return {k: _Frame(v) for k, v in state.tables.items() if k not in CATALOG}

    monkeypatch.setattr(fetch, "outlets", lambda client: (1, [2]))
    monkeypatch.setattr(fetch, "build_catalog", catalog)
    monkeypatch.setattr(fetch, "build_transactions", transactions)
    return state


def _extract(out, **kwargs):
    return run_extraction(_client(), out, now=lambda: NOW, log=lambda m: None, **kwargs)


def test_the_extraction_lands_a_run_the_schema_and_the_models_accept(tmp_path, moka):
    result = _extract(str(tmp_path / "landing"), run_id="040012-beef")
    run = tmp_path / "landing" / "moka" / "2026-10-14"
    assert result.location == str(run)
    manifest = json.loads((run / "manifest.json").read_text())
    jsonschema.validate(manifest, SCHEMA)
    assert manifest["run_id"] == "040012-beef"
    assert manifest["window"] == {
        "since": "2026-01-01T00:00:00+08:00", "until": "2026-10-14T04:00:12+08:00"
    }  # fmt: skip
    kinds = {name: t["kind"] for name, t in manifest["tables"].items()}
    assert kinds["items"] == kinds["item_variants"] == "snapshot"
    assert kinds["transactions"] == kinds["transaction_lines"] == "events"
    assert manifest["tables"]["transaction_lines"]["rows"] == len(api_tables()["transaction_lines"])
    assert manifest["source_info"]["http_calls"] == 1
    assert sorted(p.name for p in (run / "raw").iterdir()) == [
        "items_1.json.gz", "transactions.json.gz"
    ]  # fmt: skip
    with gzip.open(run / "raw" / "transactions.json.gz", "rt") as f:
        assert json.load(f) == {"payments": []}
    db = open_run(run, FIXTURES)
    assert str(db.con.execute("select extracted_at from run_context").fetchone()[0]) == (
        "2026-10-14 04:00:12"
    )


def test_the_extraction_lands_in_cloud_storage(moka):
    client = GcsClient()
    result = _extract(GcsStore("bucket", "landing", client))
    assert result.location == "gs://bucket/landing/moka/2026-10-14"
    keys = sorted(client.buckets["bucket"].objects)
    assert keys[-1] == "landing/moka/2026-10-14/tables/transactions.parquet"
    assert "landing/moka/2026-10-14/manifest.json" in keys


def test_a_failed_check_lands_nothing_and_keeps_the_earlier_run_of_the_day(tmp_path, moka):
    _extract(str(tmp_path))
    before = (tmp_path / "moka" / "2026-10-14" / "manifest.json").read_text()
    moka.tables = {**api_tables(), "transactions": api_tables()["transactions"].iloc[0:0]}
    with pytest.raises(ExtractionFailed, match="zero payments"):
        _extract(str(tmp_path))
    assert (tmp_path / "moka" / "2026-10-14" / "manifest.json").read_text() == before


def test_the_extract_command_writes_to_the_landing_folder(tmp_path, moka, monkeypatch, capsys):
    from hari_lari_analytics.sources.moka import cli

    monkeypatch.setattr(cli, "MokaClient", lambda manager: _client())
    monkeypatch.setattr(cli, "run_extraction", lambda client, out, **kw: _extract(out))
    assert cli.main(["run", "--out", str(tmp_path / "landing")]) == 0
    assert (tmp_path / "landing" / "moka" / "2026-10-14" / "manifest.json").exists()


def test_old_manifests_are_still_read(tmp_path):
    """The runs of data/extract and raw/ have the shape from before t12."""
    from hari_lari_analytics.warehouse.inputs import ApiRun

    old = ApiRun("raw/2026-11-25", {}, {"warnings": ["last sale is old"], "errors": []})
    new = ApiRun("moka/2026-11-25", {}, {"checks": {"warnings": ["w"], "errors": []}})
    assert old.check_messages("warnings") == ["last sale is old"]
    assert new.check_messages("warnings") == ["w"]
