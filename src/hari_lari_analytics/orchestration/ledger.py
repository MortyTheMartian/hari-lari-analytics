"""The run ledger (D31, docs/architecture.md, "The run ledger"): one JSON record per step of a
run, `runs/<run date>/<run id>-<step>.json` under the data root (`dry-run/runs/` for a dry run).

The steps are `extract-<source>`, `warehouse` and `report-<report id>`; a report and its
deliveries are one record. A step writes its record when it ends, so a step that crashes the
job leaves none and the next tick redoes it. Targets are recorded by name, never by chat id,
and `error` holds the error type and the scrubbed message, as the alerts do.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from hari_lari_analytics.core.store import Store

LEDGER = "runs"
LEDGER_VERSION = 1

Trigger = Literal["scheduler", "manual"]
Status = Literal["ok", "failed", "sent", "quiet", "skipped"]
KEY = re.compile(r"^(\d{4}-\d{2}-\d{2})/(\d{6}-[0-9a-z]{4})-([a-z0-9_-]+)\.json$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Delivery(_Strict):
    """One channel endpoint of a report: Drive, or a Telegram target by name. `late_notice` is
    the message that tells a chat the report is late (it does not count as the report)."""

    channel: Literal["drive", "telegram"]
    kind: Literal["report", "late_notice"] = "report"
    target: str | None = None
    status: Literal["ok", "failed", "dry_run"]
    folder: str | None = None
    url: str | None = None
    message_ids: list[int] = []
    error: str | None = None


class Record(_Strict):
    ledger_version: Literal[1] = LEDGER_VERSION
    run_id: str
    run_date: date
    step: str
    trigger: Trigger
    image: str | None = None
    dry_run: bool = False
    force: bool = False
    to: str | None = None
    status: Status
    reason: str | None = None
    started_at: datetime
    finished_at: datetime
    seconds: float
    seconds_by_phase: dict[str, float] = {}
    inputs: dict[str, Any] = {}
    outputs: list[str] = []
    deliveries: list[Delivery] = []
    warnings: list[str] = []
    error: str | None = None

    @property
    def key(self) -> str:
        return record_key(self.run_date, self.run_id, self.step)

    @property
    def counts_as_sent(self) -> bool:
        """Whether this record means the report went out: sent, not a dry run, not `--to`."""
        return self.status == "sent" and not self.dry_run and self.to is None


def record_key(run_date: date, run_id: str, step: str) -> str:
    return f"{LEDGER}/{run_date}/{run_id}-{step}.json"


def write_record(store: Store, record: Record) -> str:
    store.write_text(record.key, record.model_dump_json(indent=2) + "\n")
    return record.key


def read_records(
    store: Store, *, since: date | None = None, step: str | None = None
) -> list[Record]:
    """The records in the store, by run and within a run in step order; `since` is the first
    run date, `step` a step name such as `report-weekly`."""
    found = []
    for key in store.list(LEDGER):
        match = KEY.match(key.removeprefix(f"{LEDGER}/"))
        if match is None:
            continue
        run_date, _, name = match.groups()
        if since is not None and date.fromisoformat(run_date) < since:
            continue
        if step is not None and name != step:
            continue
        found.append(Record.model_validate_json(store.read_text(key)))
    return sorted(found, key=lambda r: (r.run_date, r.run_id, _order(r.step), r.started_at))


def _order(step: str) -> int:
    """The steps of one run in the order they run."""
    return 0 if step.startswith("extract-") else 1 if step == "warehouse" else 2


def report_records(store: Store, report_id: str, anchor: date) -> list[Record]:
    """The records of one report for one anchor, oldest first."""
    return [
        r
        for r in read_records(store, step=f"report-{report_id}")
        if r.inputs.get("anchor") == f"{anchor:%Y-%m-%d}"
    ]


def sent_record(store: Store, report_id: str, anchor: date) -> Record | None:
    """The newest record that says the report went out for the anchor, if any."""
    sent = [r for r in report_records(store, report_id, anchor) if r.counts_as_sent]
    return sent[-1] if sent else None


def delivered_targets(store: Store, report_id: str, anchor: date) -> set[str]:
    """The Telegram targets that got the report for the anchor in an earlier run that did not
    finish (a real run, not `--to`), so a re-run sends only to the others."""
    done = set()
    for record in report_records(store, report_id, anchor):
        if record.dry_run or record.to is not None:
            continue
        done |= {
            d.target
            for d in record.deliveries
            if d.channel == "telegram" and d.kind == "report" and d.status == "ok" and d.target
        }
    return done
