from pathlib import Path

from typer.testing import CliRunner

from digest.cli import app
from digest.core.rank import FixedWeightRanker
from digest.core.render import TemplateRenderer
from digest.db import connect
from digest.models import Delta, InboxItem, InboxRow, Signal, User
from tests.test_ingest import _ingest, _snapshot

DATA = Path(__file__).parent.parent / "data"
MAYA = User(id="maya", name="Maya Chen", role="mechanical_engineer", team="mechanical")


def _run(db_path: Path, user: str, date: str) -> str:
    result = CliRunner().invoke(app, ["run", "--user", user, "--date", date, "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    return result.output


def test_run_twice_gives_identical_digest_and_state(tmp_path):
    db_path = tmp_path / "digest.db"
    _ingest(DATA, db_path)
    conn = connect(db_path)
    with conn:
        conn.execute("INSERT INTO digests VALUES ('maya', '2026-03-12', 'stale')")
    conn.close()

    first = _run(db_path, "maya", "2026-03-12")
    state = _snapshot(db_path)
    assert _run(db_path, "maya", "2026-03-12") == first
    assert _snapshot(db_path) == state

    conn = connect(db_path)
    assert [tuple(r) for r in conn.execute("SELECT * FROM digests")] == [("maya", "2026-03-12", first)]
    conn.close()
    assert "REQ-G06 Ingress protection: value change" in first

    other_db = tmp_path / "other.db"
    _ingest(DATA, other_db)
    _run(other_db, "tom", "2026-03-12")
    assert _run(other_db, "maya", "2026-03-12") == first


def test_unknown_user_fails(tmp_path):
    db_path = tmp_path / "digest.db"
    _ingest(DATA, db_path)
    result = CliRunner().invoke(app, ["run", "--user", "nobody", "--date", "2026-03-12", "--db", str(db_path)])
    assert result.exit_code == 1


def _item(n: int, product: str, type_: str) -> InboxItem:
    signal = Signal(
        id=f"{product}:{n}.000001", source="slack", channel=product, content_hash="x",
        last_ts=f"{n}.000001", participants=[],
    )
    delta = Delta(
        id=f"d{n}", signal_id=signal.id, type=type_, target_kind="task", target_id=f"T{n:02}",
        old_value="before", new_value="after", confidence=1.0, ts=f"{n}.000001",
    )
    return InboxItem(
        row=InboxRow(user_id="maya", delta_id=delta.id, reason=f"reason {n}", score=1.0),
        delta=delta, signal=signal, product_id=product, product_name=product.title(),
        target_title=f"Task {n}", source_url=f"https://example/{n}",
        conflicts=["clash"] if n == 1 else [],
    )


def test_top_five_cards_grouped_by_product():
    items = [
        _item(1, "drive", "deadline_change"),
        _item(2, "gripper", "value_change"),
        _item(3, "drive", "value_change"),
        _item(4, "gripper", "owner_change"),
        _item(5, "gripper", "status_change"),
        _item(6, "drive", "owner_change"),
        _item(7, "gripper", "deadline_change"),
    ]
    ranked = FixedWeightRanker().rank(MAYA, items)
    assert [s.item.delta.id for s in ranked] == ["d2", "d3", "d5", "d1", "d7"]

    body = TemplateRenderer().render(MAYA, ranked)
    assert body.index("## Gripper") < body.index("## Drive")
    assert body.count("## Gripper") == body.count("## Drive") == 1
    headings = [line for line in body.splitlines() if line.startswith("### ")]
    assert headings == [
        "### 1. T02 Task 2: value change",
        "### 2. T05 Task 5: status change",
        "### 3. T07 Task 7: deadline change",
        "### 4. T03 Task 3: value change",
        "### 5. T01 Task 1: deadline change",
    ]
    assert body.count("- Before: before") == body.count("- After: after") == 5
    assert "- Source: #drive https://example/1" in body
    assert "- Why it reaches you: reason 1" in body
    assert "- Potential conflicts:\n  - clash" in body
    assert body.count("- Potential conflicts: none found") == 4
    assert TemplateRenderer().render(MAYA, ranked) == body
