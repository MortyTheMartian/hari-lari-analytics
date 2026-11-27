"""`reports/index.json`: every report and anchor in the store, for the frontend (old 12.1).

Regenerated from the listing of `reports/<report>/<anchor>/` after each report run, so it never
drifts from what is stored. The folders `reports/<anchor>/` of the weekly command before 9.7
are not listed.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

from hari_lari_analytics.core.store import Store

INDEX = "reports/index.json"
INDEX_VERSION = 1
FOLDER = re.compile(r"^reports/([a-z][a-z0-9_]*)/(\d{4}-\d{2}-\d{2})/([^/]+)$")


def build_index(store: Store, now: datetime) -> dict:
    files: dict[str, dict[str, list[str]]] = {}
    for key in store.list("reports"):
        match = FOLDER.match(key)
        if match:
            report, anchor, name = match.groups()
            files.setdefault(report, {}).setdefault(anchor, []).append(name)
    reports = {}
    for report in sorted(files):
        reports[report] = [
            {
                "anchor": anchor,
                "folder": f"reports/{report}/{anchor}/",
                "bundle": "bundle.json" in names,
                "files": sorted(names),
            }
            for anchor, names in sorted(files[report].items(), reverse=True)
        ]
    return {
        "index_version": INDEX_VERSION,
        "generated_at": now.isoformat(timespec="seconds"),
        "reports": reports,
    }


def write_index(store: Store, now: datetime) -> str:
    store.write_text(INDEX, json.dumps(build_index(store, now), indent=2) + "\n")
    return INDEX
