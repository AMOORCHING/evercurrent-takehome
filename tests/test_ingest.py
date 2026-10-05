import hashlib
import json
import logging
import shutil
from pathlib import Path

from typer.testing import CliRunner

from digest.cli import app
from digest.db import connect
from digest.models import TABLE_MODELS

DATA = Path(__file__).parent.parent / "data"


def _ingest(data_dir: Path, db_path: Path):
    result = CliRunner().invoke(app, ["ingest", str(data_dir / "slack.json"), "--replay", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    return result


def _snapshot(db_path: Path) -> dict[str, tuple[int, str]]:
    """Row count and a hash of the sorted rows, per table."""
    conn = connect(db_path)
    snapshot = {}
    for table in TABLE_MODELS:
        rows = sorted(json.dumps(list(row)) for row in conn.execute(f"SELECT * FROM {table}"))
        snapshot[table] = (len(rows), hashlib.sha256("\n".join(rows).encode()).hexdigest())
    conn.close()
    return snapshot


def test_ingest_twice_gives_identical_state(tmp_path):
    db_path = tmp_path / "digest.db"
    first_run = _ingest(DATA, db_path)
    first = _snapshot(db_path)

    second_run = _ingest(DATA, db_path)
    assert _snapshot(db_path) == first
    assert "0 new or changed signals, 0 deltas applied" in second_run.output
    assert first_run.output != second_run.output

    gold = json.loads((DATA / "gold.json").read_text())
    assert first["signals"][0] == len(gold["threads"])
    assert first["deltas"][0] == sum(len(t["deltas"]) for t in gold["threads"])


def test_ingest_stores_gold_old_and_new_values(tmp_path):
    db_path = tmp_path / "digest.db"
    _ingest(DATA, db_path)
    gold = json.loads((DATA / "gold.json").read_text())
    conn = connect(db_path)
    stored = {row["id"]: row for row in conn.execute("SELECT * FROM deltas")}
    for thread in gold["threads"]:
        for d in thread["deltas"]:
            row = stored[d["id"]]
            assert row["signal_id"] == thread["thread"]
            assert (row["old_value"], row["new_value"]) == (d["old_value"], d["new_value"])
    conn.close()


def test_malformed_delta_skips_only_its_thread(tmp_path, caplog):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    for name in ("slack.json", "graph_seed.json"):
        shutil.copy(DATA / name, data_dir / name)
    gold = json.loads((DATA / "gold.json").read_text())
    bad = next(t for t in gold["threads"] if t["deltas"])
    bad["deltas"][0]["target_kind"] = "widget"
    (data_dir / "gold.json").write_text(json.dumps(gold))

    db_path = tmp_path / "digest.db"
    with caplog.at_level(logging.ERROR):
        result = _ingest(data_dir, db_path)
    assert "1 threads skipped" in result.output
    assert bad["thread"] in caplog.text

    conn = connect(db_path)
    assert conn.execute("SELECT 1 FROM signals WHERE id = ?", (bad["thread"],)).fetchone() is None
    deltas = conn.execute("SELECT COUNT(*) FROM deltas").fetchone()[0]
    assert deltas == sum(len(t["deltas"]) for t in gold["threads"]) - 1
    conn.close()

    first = _snapshot(db_path)
    _ingest(data_dir, db_path)
    assert _snapshot(db_path) == first
