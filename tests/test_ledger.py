"""The run ledger (D31): a record per step, "already sent" from the ledger, a re-run that sends
only to the targets that missed the report, `reports/index.json` and `hari-lari-runs list`."""

from __future__ import annotations

import io
import json
import shutil
from datetime import date

from hari_lari_analytics.orchestration import runs_cli
from hari_lari_analytics.orchestration.ledger import read_records, sent_record
from hari_lari_analytics.orchestration.report_index import INDEX

from .fakes import ADMIN, GROUP
from .test_report_run import ANCHOR, NOW, TRACER, Env, report


def records(env, dry=False):
    store = env.settings.store.child("dry-run") if dry else env.settings.store
    return read_records(store)


def test_a_run_writes_one_record_per_step_with_one_run_id(tmp_path):
    env = Env(tmp_path)
    assert env.run() == 0
    extract, warehouse, tracer = records(env)
    assert [r.step for r in (extract, warehouse, tracer)] == [
        "extract-moka", "warehouse", "report-tracer",
    ]  # fmt: skip
    assert len({r.run_id for r in (extract, warehouse, tracer)}) == 1
    assert extract.status == warehouse.status == "ok" and tracer.status == "sent"
    assert extract.outputs == ["landing/moka/2026-10-14"]
    assert warehouse.inputs["landing"] == {"moka": "2026-10-14"}
    assert sorted(warehouse.inputs["config"]) == [
        "calendar_events.csv", "category_group.csv", "item_aliases.csv",
    ]  # fmt: skip
    assert tracer.inputs["anchor"] == "2026-10-13" and tracer.inputs["landing"] == {
        "moka": "2026-10-14"
    }
    assert tracer.inputs["warehouse"].endswith("warehouse/2026-10-14/warehouse.duckdb")
    assert "reports/tracer/2026-10-13/bundle.json" in tracer.outputs
    drive, telegram = tracer.deliveries
    assert drive.channel == "drive" and drive.status == "ok" and drive.url.endswith("/id1")
    assert (telegram.target, telegram.status, len(telegram.message_ids)) == ("group", "ok", 2)
    assert {"model", "report", "upload", "drive", "deliver"} <= set(tracer.seconds_by_phase)
    assert tracer.trigger == "manual" and tracer.error is None
    key = f"runs/2026-10-14/{tracer.run_id}-report-tracer.json"
    assert json.loads((env.root / key).read_text())["status"] == "sent"


def test_the_record_names_targets_never_chat_ids(tmp_path):
    env = Env(tmp_path)
    env.run()
    text = "".join(p.read_text() for p in (env.root / "runs").rglob("*.json"))
    assert GROUP not in text and ADMIN not in text


def test_a_second_run_says_already_sent_from_the_ledger(tmp_path):
    env = Env(tmp_path)
    assert env.run() == 0
    shutil.rmtree(env.root / "sent")  # only the ledger says it was sent
    calls = len(env.telegram.calls)
    assert env.run() == 0
    assert len(env.telegram.calls) == calls
    said = [e["message"] for e in env.log() if e.get("already_sent")]
    assert said and "(ledger runs/2026-10-14/" in said[0]
    assert len([r for r in records(env) if r.step == "report-tracer"]) == 1  # no second record


def test_an_old_sent_marker_is_still_honoured(tmp_path):
    env = Env(tmp_path)
    (env.root / "sent").mkdir(parents=True)
    (env.root / "sent" / "2026-10-13.json").write_text("{}")  # before 9.7, weekly only
    assert env.run(report(id="weekly")) == 0
    assert env.telegram.calls == [] and records(env) == []


def test_force_sends_again_and_records_it(tmp_path):
    env = Env(tmp_path)
    env.run()
    env.settings.force = True
    env.services.now = lambda: NOW.replace(minute=5)  # a later run id: the records' order
    assert env.run() == 0
    sent = [r for r in records(env) if r.step == "report-tracer"]
    assert [r.force for r in sent] == [False, True]


def test_a_dry_run_or_a_run_to_one_chat_never_counts_as_sent(tmp_path):
    env = Env(tmp_path, dry_run=True)
    assert env.run() == 0
    (dry,) = [r for r in records(env, dry=True) if r.step == "report-tracer"]
    assert dry.dry_run and [d.status for d in dry.deliveries] == ["dry_run", "dry_run"]
    assert not (env.root / "runs").exists()
    env.settings.dry_run, env.settings.to = False, "admin"
    assert env.run() == 0
    (private,) = [r for r in records(env) if r.step == "report-tracer"]
    assert private.to == "admin" and private.deliveries[-1].target == "admin"
    assert sent_record(env.settings.store, "tracer", ANCHOR) is None
    env.settings.to = None
    assert env.run() == 0
    assert env.telegram.sent(GROUP) == ["sendPhoto", "sendDocument"]


def test_a_failed_send_is_recorded_and_the_rerun_sends_only_to_the_target_that_missed_it(
    tmp_path,
):
    two = report(deliver={
        **TRACER["deliver"],
        "telegram": [
            {"to": "group", "photo": "overview", "text": "summary"},
            {"to": "admin", "document": "pdf"},
        ],
    })  # fmt: skip
    env = Env(tmp_path, fail_on="sendDocument")
    assert env.run(two) == 1
    (failed,) = [r for r in records(env) if r.step == "report-tracer"]
    assert failed.status == "failed" and "chat not found" in failed.error
    statuses = [(d.target, d.status) for d in failed.deliveries if d.channel == "telegram"]
    assert statuses == [("group", "ok"), ("admin", "failed")]
    env.telegram.fail_on = None
    env.telegram.calls.clear()
    assert env.run(two) == 0
    assert env.telegram.sent(GROUP) == [] and env.telegram.sent(ADMIN) == ["sendDocument"]
    assert sent_record(env.settings.store, "tracer", ANCHOR) is not None


def test_a_failed_extraction_records_the_extract_and_skips_the_report(tmp_path):
    env = Env(tmp_path)
    env.extract_error = RuntimeError("Moka is down")
    assert env.run() == 1
    extract, tracer = records(env)
    assert (extract.step, extract.status) == ("extract-moka", "failed")
    assert "RuntimeError: Moka is down" == extract.error
    assert (tracer.step, tracer.status, tracer.reason) == (
        "report-tracer", "skipped", "step extract failed",
    )  # fmt: skip


def test_a_report_whose_conditions_do_not_hold_is_quiet(tmp_path):
    env = Env(tmp_path)
    quiet = report(deliver={**TRACER["deliver"], "when": [{"insight": "discount_rate_high"}]})
    assert env.run(quiet) == 0
    (tracer,) = [r for r in records(env) if r.step == "report-tracer"]
    assert tracer.status == "quiet" and tracer.deliveries == []
    assert sent_record(env.settings.store, "tracer", ANCHOR) is None


def test_the_index_lists_every_report_and_anchor(tmp_path):
    env = Env(tmp_path)
    assert env.run() == 0
    env.settings.anchor = date(2026, 10, 12)
    assert env.run() == 0
    assert env.run() == 0  # already sent
    index = json.loads((env.root / INDEX).read_text())
    anchors = [entry["anchor"] for entry in index["reports"]["tracer"]]
    assert anchors == ["2026-10-13", "2026-10-12"]
    assert index["reports"]["tracer"][0]["bundle"] is True
    assert "tracer_2026-10-13.pdf" in index["reports"]["tracer"][0]["files"]


def test_runs_list_prints_the_history(tmp_path):
    env = Env(tmp_path)
    env.run()
    env.settings.anchor = date(2026, 10, 12)
    env.run()
    out = io.StringIO()
    args = ["list", "--data-root", str(env.root)]
    assert runs_cli.main(args, out) == 0
    lines = out.getvalue().splitlines()
    assert len(lines) == 6 and "extract-moka" in lines[0]
    (sent,) = [line for line in lines if "report-tracer" in line and "2026-10-13" in line]
    assert "  sent  " in sent and "→ drive ok, telegram group ok" in sent
    out = io.StringIO()
    assert runs_cli.main([*args, "--report", "tracer"], out) == 0
    assert len(out.getvalue().splitlines()) == 2
    out = io.StringIO()
    assert runs_cli.main([*args, "--since", "2026-10-15"], out) == 0
    assert out.getvalue().startswith("No ledger records")
