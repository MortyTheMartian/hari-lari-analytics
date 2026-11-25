"""Where the report job keeps its files (local or Cloud Storage), and the Drive client."""

from pathlib import Path

import pytest

from hari_lari_analytics.deliver.drive import DriveError, DriveUploader
from hari_lari_analytics.reporting.store import GcsStore, LocalStore

from .fakes import DriveSession, Response


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


class _Blob:
    def __init__(self, bucket, name):
        self.bucket, self.name = bucket, name

    def exists(self):
        return self.name in self.bucket.objects

    def upload_from_string(self, text, content_type=None):
        self.bucket.objects[self.name] = text

    def upload_from_filename(self, path):
        self.bucket.objects[self.name] = Path(path).read_text()

    def download_as_text(self):
        return self.bucket.objects[self.name]


class _Bucket:
    def __init__(self):
        self.objects = {}

    def blob(self, name):
        return _Blob(self, name)


class _GcsClient:
    def __init__(self):
        self.buckets = {}

    def bucket(self, name):
        return self.buckets.setdefault(name, _Bucket())


def test_gcs_store_prefixes_keys_and_urls():
    client = _GcsClient()
    store = GcsStore("b", "", client).child("dry-run")
    store.write_text("sent/weekly/2026-10-13.json", "{}")
    assert "dry-run/sent/weekly/2026-10-13.json" in client.buckets["b"].objects
    assert store.exists("sent/weekly/2026-10-13.json")
    assert store.read_text("sent/weekly/2026-10-13.json") == "{}"
    assert store.url("raw") == "gs://b/dry-run/raw"
    assert GcsStore("b", "", client).url("raw") == "gs://b/raw"
