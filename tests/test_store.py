"""Where the report job keeps its files (local or Cloud Storage), and the Drive client."""

import pytest

from hari_lari_analytics.core.store import GcsStore, LocalStore
from hari_lari_analytics.distribution.drive import DriveError, DriveUploader

from .fakes import DriveSession, GcsClient, Response


# ---- Drive client -----------------------------------------------------------------
def test_drive_reuses_the_week_folder_and_replaces_files(tmp_path):
    class Session:
        def __init__(self):
            self.requests = []

        def request(self, method, url, **kwargs):
            self.requests.append((method, url, kwargs))
            if method == "GET":
                return Response(200, {"files": [{"id": "existing"}]})
            return Response(200, {"id": "x"})

    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF")
    session = Session()
    url = DriveUploader(session, "root").upload_week("2026-10-13 (week 42)", [pdf])
    assert url == "https://drive.google.com/drive/folders/existing"
    assert [m for m, *_ in session.requests] == ["GET", "GET", "PATCH"]
    assert session.requests[2][1].endswith("/files/existing")
    assert session.requests[2][2]["data"] == b"%PDF"


def test_drive_escapes_quotes_in_names():
    session = DriveSession()
    DriveUploader(session, "root").ensure_folder("it's")  # a GET then a POST
    assert session.requests[0][0] == "GET"


def test_drive_error_text_names_the_reason():
    with pytest.raises(DriveError, match="storage quota"):
        DriveUploader(DriveSession(fail=True), "root").ensure_folder("x")


def test_drive_needs_a_folder_id():
    with pytest.raises(DriveError):
        DriveUploader(DriveSession(), "")


# ---- stores -----------------------------------------------------------------------
def test_local_store_child_and_upload(tmp_path):
    store = LocalStore(tmp_path).child("dry-run")
    src = tmp_path / "a.txt"
    src.write_text("hi")
    store.upload("reports/x/a.txt", src)
    assert (tmp_path / "dry-run" / "reports" / "x" / "a.txt").read_text() == "hi"
    assert store.exists("reports/x/a.txt") and not store.exists("nope")
    assert store.url("raw").endswith("dry-run/raw")


def test_gcs_store_prefixes_keys_and_urls():
    client = GcsClient()
    store = GcsStore("b", "", client).child("dry-run")
    store.write_text("sent/weekly/2026-10-13.json", "{}")
    assert "dry-run/sent/weekly/2026-10-13.json" in client.buckets["b"].objects
    assert store.exists("sent/weekly/2026-10-13.json")
    assert store.read_text("sent/weekly/2026-10-13.json") == "{}"
    assert store.url("raw") == "gs://b/dry-run/raw"
    assert GcsStore("b", "", client).url("raw") == "gs://b/raw"


@pytest.mark.parametrize("kind", ["local", "gcs"])
def test_stores_list_download_and_delete(tmp_path, kind):
    root = LocalStore(tmp_path / "root") if kind == "local" else GcsStore("b", "data", GcsClient())
    store = root.child("dry-run")
    for key in ["runs/2026-11-30/a.json", "runs/2026-11-30/b.json", "runs/2026-12-01/c.json"]:
        store.write_text(key, key)
    root.write_text("outside.json", "{}")
    assert store.list("runs/2026-11-30") == ["runs/2026-11-30/a.json", "runs/2026-11-30/b.json"]
    assert store.list() == [
        "runs/2026-11-30/a.json",
        "runs/2026-11-30/b.json",
        "runs/2026-12-01/c.json",
    ]
    assert store.list("runs/2026-11") == []  # a folder, not a key prefix
    assert store.list("nothing") == []
    store.download("runs/2026-12-01/c.json", tmp_path / "out" / "c.json")
    assert (tmp_path / "out" / "c.json").read_text() == "runs/2026-12-01/c.json"
    store.delete("runs/2026-12-01/c.json")
    store.delete("runs/2026-12-01/c.json")  # a missing key is not an error
    assert not store.exists("runs/2026-12-01/c.json")
