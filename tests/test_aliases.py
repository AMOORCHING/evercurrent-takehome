"""A4 entity aliases: gold alias cases resolve, unknown names are recorded, eval lists them.

The four gold alias threads (cases A1-A4) carry canonical target IDs, so replay never
needs the table; these tests re-run them the way an LLM extractor would report them -
under the surface names the thread text actually uses.
"""

from pathlib import Path

from typer.testing import CliRunner

from digest.attach.aliases import resolve, resolving_apply
from digest.cli import app
from digest.core.assemble import SlackExport
from digest.core.extract import ReplayExtractor
from digest.core.rank import FixedWeightRanker
from digest.db import connect, create_schema, load_graph_seed
from digest.eval import METRICS, Configuration, Run, collect_runs, results_markdown, score_rows, unresolved_names
from digest.models import Delta, Extractor, GraphSlice, Signal
from digest.pipeline import core_apply, ingest
from tests.test_ingest import _ingest, _snapshot

DATA = Path(__file__).parent.parent / "data"

# Gold delta ID to the surface name its thread uses (data/slack.json) instead of the
# canonical task ID. All four are seeded in the aliases table of the graph seed.
SURFACES = {
    "gold-A1": "MDB rev A",  # T04 Bring up gripper motor driver board
    "gold-A3": "fingertips",  # T03 Machine EVT jaw fingers
    "gold-A2": "HD-20",       # T22 Source DVT gearbox
    "gold-A4": "AksIM",       # T23 Integrate drive encoder firmware
}


class SurfaceNameExtractor(Extractor):
    """Replay, except chosen deltas name their target the way the thread text does."""

    def __init__(self, gold_path: Path, surfaces: dict[str, str] = SURFACES) -> None:
        self.inner = ReplayExtractor(gold_path)
        self.surfaces = surfaces

    def extract(self, signal: Signal, ctx: GraphSlice) -> list[Delta]:
        return [
            d.model_copy(update={"target_id": self.surfaces[d.id]}) if d.id in self.surfaces else d
            for d in self.inner.extract(signal, ctx)
        ]


def _fresh(extractor: Extractor, apply_fn):
    conn = connect(":memory:")
    create_schema(conn)
    load_graph_seed(conn, DATA / "graph_seed.json")
    export = SlackExport.model_validate_json((DATA / "slack.json").read_text())
    result = ingest(conn, export, extractor, apply_fn=apply_fn)
    return conn, result


def _dump(conn, table: str) -> list[tuple]:
    return sorted(tuple(r) for r in conn.execute(f"SELECT * FROM {table}"))


def test_gold_alias_cases_resolve_to_the_canonical_state():
    """Surface-named deltas through resolving_apply land exactly where canonical replay
    lands: same deltas, tasks and inbox, nothing skipped, nothing unresolved."""
    canonical, _ = _fresh(ReplayExtractor(DATA / "gold.json"), core_apply)
    aliased, result = _fresh(SurfaceNameExtractor(DATA / "gold.json"), resolving_apply)

    assert result.skipped == 0
    for table in ("tasks", "deltas", "inbox", "signals"):
        assert _dump(aliased, table) == _dump(canonical, table), table
    assert _dump(aliased, "unresolved") == []
    canonical.close()
    aliased.close()


def test_without_aliases_the_same_surface_names_skip_their_threads():
    """The contrast that makes A4's case: core apply raises on the surface names, so all
    four alias threads are skipped and their deltas never land."""
    conn, result = _fresh(SurfaceNameExtractor(DATA / "gold.json"), core_apply)
    assert result.skipped == len(SURFACES)
    applied = {r["id"] for r in conn.execute("SELECT id FROM deltas")}
    assert applied.isdisjoint(SURFACES)
    conn.close()


def test_unknown_name_is_recorded_and_only_that_delta_dropped():
    thread = "hw-electrical:1772622900.000121"  # case A1, one gold delta
    extractor = SurfaceNameExtractor(DATA / "gold.json", {"gold-A1": "flux capacitor"})
    conn, result = _fresh(extractor, resolving_apply)

    assert result.skipped == 0
    assert _dump(conn, "unresolved") == [("flux capacitor", "task", thread)]
    assert conn.execute("SELECT 1 FROM signals WHERE id = ?", (thread,)).fetchone()
    assert conn.execute("SELECT 1 FROM deltas WHERE id = 'gold-A1'").fetchone() is None
    total = conn.execute("SELECT COUNT(*) FROM deltas").fetchone()[0]

    # Idempotent: the thread was processed, so re-ingesting changes nothing.
    export = SlackExport.model_validate_json((DATA / "slack.json").read_text())
    again = ingest(conn, export, extractor, apply_fn=resolving_apply)
    assert (again.signals, again.deltas, again.skipped) == (0, 0, 0)
    assert conn.execute("SELECT COUNT(*) FROM deltas").fetchone()[0] == total
    assert _dump(conn, "unresolved") == [("flux capacitor", "task", thread)]
    conn.close()


def test_surfaces_match_case_and_whitespace_insensitively():
    conn, _ = _fresh(ReplayExtractor(DATA / "gold.json"), core_apply)
    delta = Delta(
        id="test-norm", signal_id="hw-electrical:1772622900.000121", type="status_change",
        target_kind="task", target_id="  mdb REV a ", old_value=None, new_value="open",
        confidence=1.0, ts="1772622900.000121",
    )
    applied = resolving_apply(conn, delta)
    assert applied is not None and applied.target_id == "T04"
    assert conn.execute("SELECT status FROM tasks WHERE id = 'T04'").fetchone()[0] == "open"
    assert resolve(conn, delta.model_copy(update={"target_id": "T04"})).target_id == "T04"
    conn.close()


def test_ingest_with_aliases_flag_is_idempotent_and_matches_replay(tmp_path):
    plain, flagged = tmp_path / "plain.db", tmp_path / "flagged.db"
    _ingest(DATA, plain)
    for _ in range(2):
        result = CliRunner().invoke(
            app, ["ingest", str(DATA / "slack.json"), "--replay", "--aliases", "--db", str(flagged)]
        )
        assert result.exit_code == 0, result.output
    # Replay emits canonical IDs, so --aliases changes nothing - including on the rerun.
    assert _snapshot(flagged) == _snapshot(plain)


def test_eval_lists_unresolved_names():
    config = Configuration(
        name="aliased",
        description="Replay with one unknown surface name, aliases on.",
        extractor=lambda data_dir: SurfaceNameExtractor(
            data_dir / "gold.json", {"gold-A1": "flux capacitor", "gold-A2": "HD-20"}
        ),
        ranker=lambda data_dir, conn: FixedWeightRanker(),
        resolve_aliases=True,
    )
    runs = collect_runs(DATA, [config])
    run = runs["aliased"]
    assert isinstance(run, Run)
    assert run.unresolved == [("flux capacitor", "task", "hw-electrical:1772622900.000121")]

    rows = score_rows(runs, [config], METRICS[:1])
    body = results_markdown(
        rows, [config], METRICS[:1], gold_path=DATA / "gold.json", unresolved=unresolved_names(runs)
    )
    assert "## Unresolved names" in body
    assert "- **aliased**: 'flux capacitor' (task) in hw-electrical:1772622900.000121" in body

    empty = results_markdown(rows, [config], METRICS[:1], gold_path=DATA / "gold.json")
    assert "None recorded by any configuration that ran." in empty


def test_seeded_aliases_load_normalized():
    conn = connect(":memory:")
    create_schema(conn)
    load_graph_seed(conn, DATA / "graph_seed.json")
    rows = {r["surface"]: r["target_id"] for r in conn.execute("SELECT * FROM aliases")}
    assert rows["mdb rev a"] == "T04"
    assert rows["hd-20"] == "T22"
    assert all(s == " ".join(s.split()).casefold() for s in rows)
    conn.close()
