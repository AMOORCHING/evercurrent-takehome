"""A2: temperature scaling fit on a seeded held-out third, cost-ratio section thresholds,
and the calibrated ranker behind --ranker. No test makes a network call."""

import json
import math
from pathlib import Path

import pytest
from typer.testing import CliRunner

from digest.attach.calibrate import (
    COST_OF_MISS,
    THRESHOLDS,
    Calibration,
    CalibratedRanker,
    RankerUnavailable,
    build_ranker,
    calibrate,
    expected_calibration_error,
    fit_temperature,
    fit_threads,
    reliability_svg,
    scale,
    section,
)
from digest.cli import app
from digest.core.rank import FixedWeightRanker
from digest.eval import CONFIGURATIONS, METRICS, Configuration, collect_runs, score_rows, write_reliability_plots
from digest.models import Decision, Delta, InboxItem, InboxRow, Signal, User
from tests.test_ingest import _ingest

DATA = Path(__file__).parent.parent / "data"
MAYA = User(id="maya", name="Maya Chen", role="mechanical_engineer", team="mechanical")
CALIBRATION = next(m for m in METRICS if m.name == "Calibration error")

# Over- and under-confident pairs that mix labels in every bin, so scaling has work to do.
OVERCONFIDENT = [(0.95, True), (0.95, False), (0.92, True), (0.91, False), (0.9, True),
                 (0.09, False), (0.08, True), (0.07, False), (0.05, False), (0.04, False)]


def test_thresholds_follow_the_spec_cost_ratios():
    assert COST_OF_MISS == {"needs you": 10.0, "changes that affect you": 3.0, "FYI": 1.0}
    assert THRESHOLDS["needs you"] == pytest.approx(1 / 11)
    assert THRESHOLDS["changes that affect you"] == pytest.approx(1 / 4)
    assert THRESHOLDS["FYI"] == pytest.approx(1 / 2)


def test_section_maps_fan_out_reasons():
    assert section("You own task T07 (Spin up line).") == "needs you"
    assert section("You own requirement REQ-G02 (Grip force).") == "needs you"
    assert section("You own sourcing, the next stage of grip EVT.") == "changes that affect you"
    assert section("Your task T12 (Order samples) is one handoff downstream.") == "changes that affect you"
    assert section("Your requirement REQ-G03 (Weight) depends on it.") == "changes that affect you"
    assert section("some future reason") == "FYI"
    # A row reached by several rules keeps the costliest section to miss.
    combined = "You own task T07 (Spin up line). Your task T12 (Order samples) is one handoff downstream."
    assert section(combined) == "needs you"


def _item(n: int, reason: str, confidence: float) -> InboxItem:
    signal = Signal(id=f"c:{n}.0", source="slack", channel="c", content_hash="x", last_ts=f"{n}.0")
    delta = Delta(
        id=f"d{n}", signal_id=signal.id, type="status_change", target_kind="task",
        target_id=f"T{n:02}", new_value="done", confidence=confidence, ts=f"{n}.0",
    )
    return InboxItem(
        row=InboxRow(user_id="maya", delta_id=delta.id, reason=reason, score=confidence),
        delta=delta, signal=signal, product_id="p", product_name="P",
        target_title=f"Task {n}", source_url=f"https://example/{n}", conflicts=[],
    )


def test_calibrated_ranker_cuts_each_section_at_its_threshold():
    own, affects = "You own task T01 (t).", "Your task T02 (t) is one handoff downstream."
    items = [
        _item(1, own, 0.15),      # needs you, above 1/11
        _item(2, own, 0.05),      # needs you, below 1/11: cut
        _item(3, affects, 0.30),  # affects you, above 1/4
        _item(4, affects, 0.20),  # affects you, below 1/4: cut
        _item(5, "fyi", 0.60),    # FYI, above 1/2
        _item(6, "fyi", 0.45),    # FYI, below 1/2: cut
    ]
    ranked = CalibratedRanker().rank(MAYA, items)
    assert [s.item.delta.id for s in ranked] == ["d1", "d3", "d5"]  # section order, not score order
    assert [round(s.score, 2) for s in ranked] == [0.15, 0.30, 0.60]


def test_calibrated_ranker_has_no_top_n_cut():
    items = [_item(n, "You own task T (t).", 1.0) for n in range(1, 9)]
    ranked = CalibratedRanker().rank(MAYA, items)
    assert len(ranked) == 8
    assert [s.item.delta.id for s in ranked] == [f"d{n}" for n in range(1, 9)]  # score ties go to time


def test_calibrated_ranker_applies_its_temperature():
    # Sharpening at T=0.3 pushes 0.30 down to ~0.06, under the affects-you threshold of 1/4.
    affects = "Your task T02 (t) is one handoff downstream."
    items = [_item(1, affects, 0.30), _item(2, affects, 0.90)]
    assert len(CalibratedRanker().rank(MAYA, items)) == 2
    ranked = CalibratedRanker(temperature=0.3).rank(MAYA, items)
    assert [s.item.delta.id for s in ranked] == ["d2"]
    assert ranked[0].score == pytest.approx(scale(0.90, 0.3))


def test_scale_is_identity_at_one_softens_above_sharpens_below():
    for p in (0.1, 0.5, 0.9):
        assert scale(p, 1.0) == pytest.approx(p, abs=1e-6)
    assert 0.5 < scale(0.9, 3.0) < 0.9
    assert scale(0.9, 0.3) > 0.99
    assert scale(0.5, 7.0) == pytest.approx(0.5)


def test_fit_temperature_softens_overconfident_probabilities():
    t = fit_temperature(OVERCONFIDENT)
    assert t > 1.0
    scaled = [(scale(p, t), label) for p, label in OVERCONFIDENT]
    assert expected_calibration_error(scaled) < expected_calibration_error(OVERCONFIDENT)
    assert fit_temperature([]) == 1.0


def test_fit_threads_is_a_fixed_seeded_third():
    ids = [f"c:{n}" for n in range(120)]
    first = fit_threads(ids)
    assert len(first) == 40
    assert first == fit_threads(list(reversed(ids)))  # order of arrival changes nothing
    assert first < set(ids)


def test_calibrate_fits_on_the_third_and_measures_on_the_rest():
    pairs = {f"c:{n}": OVERCONFIDENT[n % len(OVERCONFIDENT)] for n in range(12)}
    result = calibrate(pairs)
    assert result.fit_count == 4
    assert result.eval_count == 8
    assert len(result.eval_pairs) == 8
    assert 0.0 <= result.ece_after <= result.ece_before <= 1.0
    text = str(result)
    assert "before" in text and "after" in text and f"T={result.temperature:.2f}" in text
    assert calibrate(pairs) == result  # deterministic


def test_reliability_svg_plots_both_series():
    svg = reliability_svg(calibrate({f"c:{n}": p for n, p in enumerate(OVERCONFIDENT)}), title="stub")
    assert svg.startswith("<svg ") and svg.rstrip().endswith("</svg>")
    assert "Reliability — stub" in svg
    assert "before scaling" in svg and "after scaling" in svg
    assert "Observed frequency" in svg


class GoldAwareDecider:
    """changes_state 0.9 when gold has a delta for the thread, 0.3 when it does not -
    confidently right, so the digests stay populated, but visibly miscalibrated."""

    spent_usd = 0.0

    def __init__(self, gold_path: Path) -> None:
        gold = json.loads(gold_path.read_text())
        self.p = {t["thread"]: 0.9 if t["deltas"] else 0.3 for t in gold["threads"]}

    def decide(self, signal, ctx):
        return Decision(changes_state=self.p[signal.id], change_type={"none": 1.0},
                        contradicts=0.0, risk=0.0)


def _stub_configuration() -> Configuration:
    core = CONFIGURATIONS[0]
    return Configuration(
        name="stub", description="Replay extractor behind a gold-aware stub decider.",
        extractor=core.extractor, ranker=lambda data_dir, conn: FixedWeightRanker(),
        decider=lambda data_dir: GoldAwareDecider(data_dir / "gold.json"),
    )


def test_eval_reports_calibration_per_backend_and_writes_plots(tmp_path):
    configs = [CONFIGURATIONS[0], _stub_configuration()]
    runs = collect_runs(DATA, configs)
    rows = {r.configuration: r.value for r in score_rows(runs, configs, [CALIBRATION])}
    assert rows["core"].reason == "no decider"
    report = rows["stub"]
    assert isinstance(report, Calibration)
    assert report.fit_count == 40 and report.eval_count == 80
    assert report.ece_after <= report.ece_before

    paths = write_reliability_plots(runs, tmp_path / "calibration")
    assert paths == [tmp_path / "calibration" / "reliability-stub.svg"]
    body = paths[0].read_text()
    assert "Reliability — stub" in body
    assert write_reliability_plots(runs, tmp_path / "calibration") == paths
    assert paths[0].read_text() == body  # idempotent bytes


def test_run_with_calibrated_ranker_is_idempotent_and_sectioned(tmp_path):
    db_path = tmp_path / "digest.db"
    _ingest(DATA, db_path)
    args = ["run", "--user", "maya", "--date", "2026-03-12", "--db", str(db_path),
            "--ranker", "calibrated"]
    first = CliRunner().invoke(app, args)
    assert first.exit_code == 0, first.output
    assert "# Digest for Maya Chen" in first.output
    second = CliRunner().invoke(app, args)
    assert second.output == first.output

    fixed = CliRunner().invoke(app, args[:-2])  # default ranker still works
    assert fixed.exit_code == 0, fixed.output


def test_run_with_unknown_ranker_exits_clearly(tmp_path):
    db_path = tmp_path / "digest.db"
    _ingest(DATA, db_path)
    result = CliRunner().invoke(
        app, ["run", "--user", "maya", "--date", "2026-03-12", "--db", str(db_path),
              "--ranker", "nope"],
    )
    assert result.exit_code == 1
    assert "unknown ranker 'nope'" in result.output


def test_build_ranker_registry():
    assert isinstance(build_ranker("fixed"), FixedWeightRanker)
    assert isinstance(build_ranker("calibrated"), CalibratedRanker)
    with pytest.raises(RankerUnavailable, match="unknown ranker 'nope'"):
        build_ranker("nope")
