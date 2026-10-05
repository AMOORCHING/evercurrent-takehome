import datetime as dt
import json
from pathlib import Path

from typer.testing import CliRunner

from digest.cli import app
from digest.core.rank import FixedWeightRanker
from digest.eval import (
    CONFIGURATIONS,
    METRICS,
    Configuration,
    Gold,
    GoldDelta,
    GoldThread,
    NotRun,
    Ratio,
    Run,
    digest_precision,
    evaluate,
    extractor_accuracy,
    results_markdown,
    silo_recall,
)
from digest.models import Delta, InboxItem, InboxRow, Scored, Signal
from tests.test_extract import FakeClient, fake_openai, gold_responses

DATA = Path(__file__).parent.parent / "data"
DAY = dt.date(2026, 3, 3)
CORE, LLM = CONFIGURATIONS[:2]
ACCURACY = next(m for m in METRICS if m.name == "Extractor accuracy")


def _delta(thread: str, target_id: str, type_: str, new_value: str = "after") -> Delta:
    return Delta(
        id=f"minted-{target_id}", signal_id=thread, type=type_, target_kind="task",
        target_id=target_id, new_value=new_value, confidence=1.0, ts="1.0",
    )


def _scored(user_id: str, thread: str, target_id: str, type_: str) -> Scored:
    signal = Signal(id=thread, source="slack", channel="c", content_hash="x", last_ts="1.0")
    delta = _delta(thread, target_id, type_)
    item = InboxItem(
        row=InboxRow(user_id=user_id, delta_id=delta.id, reason="r", score=1.0), delta=delta,
        signal=signal, product_id="p", product_name="P", target_title="t", source_url="u", conflicts=[],
    )
    return Scored(item=item, score=1.0)


def _gold() -> Gold:
    return Gold(threads=[
        GoldThread(thread="c:1", kind="cross_team", deltas=[
            GoldDelta(type="deadline_change", target_kind="task", target_id="T01",
                      new_value="2026-03-04", affected=["priya", "tom"]),
        ]),
        GoldThread(thread="c:2", kind="background", deltas=[
            GoldDelta(type="status_change", target_kind="task", target_id="T02",
                      new_value="done", affected=["tom"]),
        ]),
        GoldThread(thread="c:3", kind="noise", deltas=[]),
    ])


def test_metrics_match_on_content_and_count_each_person():
    run = Run(gold=_gold(), digests={
        ("priya", DAY): [_scored("priya", "c:1", "T01", "deadline_change")],
        ("tom", DAY): [
            _scored("tom", "c:2", "T02", "status_change"),
            _scored("tom", "c:1", "T01", "status_change"),
            _scored("tom", "c:3", "T09", "status_change"),
        ],
        ("maya", DAY): [_scored("maya", "c:2", "T02", "status_change")],
    })
    assert silo_recall(run) == Ratio(1, 2)
    assert digest_precision(run) == Ratio(2, 5)


def test_extractor_accuracy_needs_exact_changes_per_thread():
    gold = _gold()
    gold = Gold(threads=[*gold.threads, GoldThread(thread="c:4", kind="noise", deltas=[])])
    run = Run(gold=gold, digests={}, extracted={
        "c:1": [_delta("c:1", "T01", "deadline_change", new_value=" 2026-03-04 ")],
        "c:2": [_delta("c:2", "T02", "status_change", new_value="blocked")],
        "c:3": [_delta("c:3", "T09", "status_change")],
    })
    assert extractor_accuracy(run) == Ratio(1, 4)

    run.extracted["c:4"] = []
    run.extracted["c:2"] = [_delta("c:2", "T02", "status_change", new_value="Done")]
    assert extractor_accuracy(run) == Ratio(3, 4)


def test_ratio_shows_counts_beside_percentage():
    assert str(Ratio(9, 12)) == "75.0% (9 of 12)"
    assert str(Ratio(0, 0)) == "n/a (0 of 0)"


def test_core_reaches_every_cross_team_owner():
    rows = {(r.metric, r.configuration): r.value for r in evaluate(DATA, CONFIGURATIONS, METRICS)}
    recall = rows[("Silo recall", "core")]
    assert recall.total == sum(
        len(d["affected"]) for t in _raw_gold()["threads"] if t["kind"] == "cross_team" for d in t["deltas"]
    )
    assert recall.hits == recall.total
    precision = rows[("Digest precision", "core")]
    assert precision.total > 0 and precision.hits == precision.total
    assert rows[("Extractor accuracy", "core")] == Ratio(len(_raw_gold()["threads"]), len(_raw_gold()["threads"]))


def test_llm_configuration_not_run_without_model_and_key():
    rows = evaluate(DATA, [CORE, LLM], METRICS)
    llm = [r for r in rows if r.configuration == "llm"]
    assert len(llm) == len(METRICS)
    assert all(r.value == NotRun("set DIGEST_EXTRACTOR_MODEL and OPENAI_API_KEY") for r in llm)

    body = results_markdown(rows, [CORE, LLM], METRICS, gold_path=Path("data/gold.json"))
    assert "| Silo recall | llm | not run: set DIGEST_EXTRACTOR_MODEL and OPENAI_API_KEY |" in body


def test_llm_configuration_scores_extractor_against_gold(monkeypatch):
    responses = gold_responses()
    noise = next(t["thread"] for t in _raw_gold()["threads"] if not t["deltas"])
    responses[noise] = "not json"
    client = FakeClient(responses)
    fake_openai(monkeypatch, client)

    [row] = evaluate(DATA, [LLM], [ACCURACY])
    total = len(_raw_gold()["threads"])
    assert row.value == Ratio(total - 1, total)
    assert len(client.calls) == total


def test_failed_model_call_skips_its_thread_and_the_run_continues(monkeypatch):
    responses = gold_responses()
    noise = next(t["thread"] for t in _raw_gold()["threads"] if not t["deltas"])
    responses[noise] = TimeoutError("read timed out")
    fake_openai(monkeypatch, FakeClient(responses))

    [row] = evaluate(DATA, [LLM], [ACCURACY])
    total = len(_raw_gold()["threads"])
    assert row.value == Ratio(total - 1, total)


def test_new_configuration_adds_rows_grouped_by_metric():
    one_item = Configuration(
        name="top-one", description="Core with a one-item digest.",
        extractor=CORE.extractor, ranker=lambda data_dir, conn: FixedWeightRanker(limit=1),
    )
    configs = [CORE, one_item]
    rows = evaluate(DATA, configs, METRICS)
    assert [(r.metric, r.configuration) for r in rows] == [
        (m.name, c.name) for m in METRICS for c in configs
    ]
    by_key = {(r.metric, r.configuration): r.value for r in rows}
    assert by_key[("Silo recall", "top-one")].total == by_key[("Silo recall", "core")].total
    assert by_key[("Digest precision", "top-one")].total < by_key[("Digest precision", "core")].total
    assert by_key[("Extractor accuracy", "top-one")] == by_key[("Extractor accuracy", "core")]

    body = results_markdown(rows, configs, METRICS, gold_path=Path("data/gold.json"))
    assert f"| Silo recall | top-one | {by_key[('Silo recall', 'top-one')]} |" in body
    assert "- **top-one**: Core with a one-item digest." in body


def test_a3_phase_configuration_runs_offline_and_matches_core_on_replay():
    """A3 reorders digests rather than changing membership here: replay confidences are
    all 1.0 and no inbox overflows the top five, so recall and precision stay at core's
    numbers and the measured difference lives in ordering (tests/test_phase.py)."""
    core = next(c for c in CONFIGURATIONS if c.name == "core")
    a3 = next(c for c in CONFIGURATIONS if c.name == "a3-phase")
    metrics = [m for m in METRICS if m.name in ("Silo recall", "Digest precision")]
    by_key = {(r.metric, r.configuration): r.value for r in evaluate(DATA, [core, a3], metrics)}
    assert by_key[("Silo recall", "a3-phase")] == by_key[("Silo recall", "core")] == Ratio(12, 12)
    assert by_key[("Digest precision", "a3-phase")] == by_key[("Digest precision", "core")]


def test_a3_phase_without_weights_file_is_not_run(tmp_path):
    for name in ("slack.json", "graph_seed.json", "gold.json"):
        (tmp_path / name).write_bytes((DATA / name).read_bytes())
    a3 = next(c for c in CONFIGURATIONS if c.name == "a3-phase")
    [row] = evaluate(tmp_path, [a3], [METRICS[0]])
    assert isinstance(row.value, NotRun)
    assert "phase_weights.yaml" in str(row.value)


def test_eval_writes_same_results_twice_and_touches_no_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "results.md"
    args = ["eval", "--data", str(DATA), "--out", str(out)]
    first = CliRunner().invoke(app, args)
    assert first.exit_code == 0, first.output
    body = out.read_text()
    assert "| Silo recall | core | 100.0% (" in body
    assert "| Digest precision | core | 100.0% (" in body
    assert "| Extractor accuracy | core | 100.0% (" in body
    assert "| Extractor accuracy | llm | not run: " in body

    plots = sorted(p.name for p in (tmp_path / "calibration").iterdir())
    assert "reliability-a1-passthrough.svg" in plots
    svg = (tmp_path / "calibration" / plots[0]).read_text()
    assert "| Calibration error | a1-passthrough | " in body
    assert "![reliability-a1-passthrough](calibration/reliability-a1-passthrough.svg)" in body

    second = CliRunner().invoke(app, args)
    assert second.exit_code == 0, second.output
    assert out.read_text() == body
    assert (tmp_path / "calibration" / plots[0]).read_text() == svg
    assert sorted(p.name for p in tmp_path.iterdir()) == ["calibration", "results.md"]


def _raw_gold() -> dict:
    return json.loads((DATA / "gold.json").read_text())
