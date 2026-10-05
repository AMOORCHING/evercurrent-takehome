"""A2 calibrated thresholds: temperature scaling per decider backend, and the ranker whose
section thresholds follow from the spec's cost ratios.

Calibration: a decider's `changes_state` should read as a probability. For each backend,
one temperature T is fit on a fixed held-out third of the gold threads (a seeded split, so
every run fits on the same third) by minimizing negative log-likelihood, and expected
calibration error is measured on the other two thirds, before and after scaling.
`digest eval` reports both numbers per backend in results.md and writes one reliability
plot per backend (an SVG with no plotting dependency).

Ranker: the spec prices a miss against a wrong inclusion per digest section - 10 to 1 for
"needs you", 3 to 1 for "changes that affect you", 1 to 1 for FYI. Including an item is
worth it when p * cost_of_miss >= (1 - p) * 1, so each section's threshold is
1 / (1 + cost): about 0.09, 0.25 and 0.5. `CalibratedRanker` keeps every item whose
calibrated confidence clears its section's threshold - no top-N cut, so inclusion follows
from the cost of a miss, not a tuned constant.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from digest.core.rank import FixedWeightRanker
from digest.models import InboxItem, Ranker, Scored, User

# One pair per thread: the backend's changes_state and whether gold has a delta for it.
Pair = tuple[float, bool]

SPLIT_SEED = 7
ECE_BINS = 10
TEMPERATURE_RANGE = (0.05, 20.0)
_EPS = 1e-6


def fit_threads(ids: Sequence[str] | Mapping[str, object]) -> set[str]:
    """The fixed held-out third used for fitting: a seeded shuffle of the sorted IDs, so
    the same IDs land in the fit set on every run."""
    ordered = sorted(ids)
    random.Random(SPLIT_SEED).shuffle(ordered)
    return set(ordered[: len(ordered) // 3])


def scale(p: float, temperature: float) -> float:
    """Temperature scaling of one probability: sigmoid(logit(p) / T). T=1 is the identity;
    T>1 softens toward 0.5, T<1 sharpens."""
    p = min(max(p, _EPS), 1.0 - _EPS)
    return 1.0 / (1.0 + math.exp(-math.log(p / (1.0 - p)) / temperature))


def fit_temperature(pairs: Sequence[Pair]) -> float:
    """The temperature minimizing negative log-likelihood on `pairs`, by ternary search on
    log T. Empty pairs fit nothing and return 1.0."""
    if not pairs:
        return 1.0

    def nll(log_t: float) -> float:
        t = math.exp(log_t)
        total = 0.0
        for p, label in pairs:
            q = min(max(scale(p, t), _EPS), 1.0 - _EPS)
            total -= math.log(q if label else 1.0 - q)
        return total

    lo, hi = (math.log(t) for t in TEMPERATURE_RANGE)
    for _ in range(100):
        third = (hi - lo) / 3.0
        if nll(lo + third) < nll(hi - third):
            hi = hi - third
        else:
            lo = lo + third
    return math.exp((lo + hi) / 2.0)


def expected_calibration_error(pairs: Sequence[Pair], bins: int = ECE_BINS) -> float:
    """Standard binned ECE: weight each bin by its share of pairs, sum the gaps between
    mean predicted probability and observed frequency."""
    if not pairs:
        return 0.0
    binned = _bins(pairs, bins)
    return sum(n * abs(observed - predicted) for predicted, observed, n in binned) / len(pairs)


def _bins(pairs: Sequence[Pair], bins: int = ECE_BINS) -> list[tuple[float, float, int]]:
    """(mean predicted, observed frequency, count) for each non-empty probability bin."""
    grouped: dict[int, list[Pair]] = {}
    for p, label in pairs:
        grouped.setdefault(min(int(p * bins), bins - 1), []).append((p, label))
    return [
        (
            sum(p for p, _ in members) / len(members),
            sum(label for _, label in members) / len(members),
            len(members),
        )
        for _, members in sorted(grouped.items())
    ]


@dataclass(frozen=True)
class Calibration:
    """One backend's fit: T from the held-out third, ECE measured on the rest.

    `eval_pairs` are the unscaled pairs of the measurement split, kept for the plot.
    """

    temperature: float
    ece_before: float
    ece_after: float
    fit_count: int
    eval_count: int
    eval_pairs: tuple[Pair, ...]

    def __str__(self) -> str:
        return (
            f"{100 * self.ece_before:.1f}% before, {100 * self.ece_after:.1f}% after "
            f"(T={self.temperature:.2f}, fit {self.fit_count}, eval {self.eval_count})"
        )


def calibrate(pairs_by_thread: Mapping[str, Pair]) -> Calibration:
    """Fit on the fixed third of the thread IDs, measure ECE on the remaining threads."""
    held_out = fit_threads(pairs_by_thread)
    fit = [pair for thread, pair in sorted(pairs_by_thread.items()) if thread in held_out]
    rest = [pair for thread, pair in sorted(pairs_by_thread.items()) if thread not in held_out]
    temperature = fit_temperature(fit)
    return Calibration(
        temperature=temperature,
        ece_before=expected_calibration_error(rest),
        ece_after=expected_calibration_error([(scale(p, temperature), label) for p, label in rest]),
        fit_count=len(fit),
        eval_count=len(rest),
        eval_pairs=tuple(rest),
    )


# --- Calibrated ranker -----------------------------------------------------------------

# Cost of missing an item relative to wrongly including one, per digest section (SPEC A2).
COST_OF_MISS: dict[str, float] = {
    "needs you": 10.0,
    "changes that affect you": 3.0,
    "FYI": 1.0,
}

# Include when p * cost_of_miss >= (1 - p) * 1, i.e. when p >= 1 / (1 + cost).
THRESHOLDS: dict[str, float] = {name: 1.0 / (1.0 + cost) for name, cost in COST_OF_MISS.items()}

_SECTION_ORDER = tuple(COST_OF_MISS)


def section(reason: str) -> str:
    """The digest section for one inbox row, from its fan-out reason. A row reached by
    several rules keeps the section costliest to miss; an unrecognized reason is FYI."""
    if "You own task" in reason or "You own requirement" in reason:
        return "needs you"
    if "next stage" in reason or "downstream" in reason or "depends on it" in reason:
        return "changes that affect you"
    return "FYI"


class CalibratedRanker(Ranker):
    """Keeps every item whose calibrated confidence clears its section's cost-ratio
    threshold, ordered by section, then confidence, then time.

    `temperature` is the fitted temperature of the backend that produced the confidences;
    the default 1.0 leaves them as they are, which is exact for replayed gold deltas.
    """

    def __init__(self, temperature: float = 1.0) -> None:
        self.temperature = temperature

    def rank(self, user: User, items: list[InboxItem]) -> list[Scored]:
        kept: list[tuple[int, Scored]] = []
        for item in items:
            p = scale(item.row.score, self.temperature)
            name = section(item.row.reason)
            if p >= THRESHOLDS[name]:
                kept.append((_SECTION_ORDER.index(name), Scored(item=item, score=p)))
        kept.sort(key=lambda pair: (pair[0], -pair[1].score, pair[1].item.delta.ts, pair[1].item.delta.id))
        return [scored for _, scored in kept]


class RankerUnavailable(RuntimeError):
    """The ranker cannot be built, for example because the name is unknown."""


RANKERS: dict[str, Callable[[], Ranker]] = {
    "fixed": FixedWeightRanker,
    "calibrated": CalibratedRanker,
}


def build_ranker(name: str) -> Ranker:
    """Raises `RankerUnavailable` for an unknown name."""
    if name not in RANKERS:
        raise RankerUnavailable(f"unknown ranker {name!r}; one of: {', '.join(RANKERS)}")
    return RANKERS[name]()


# --- Reliability plot --------------------------------------------------------------------

# Chart chrome from the validated reference palette (light surface); the two series colors
# pass the colorblind-separation and contrast checks against this surface.
_SURFACE = "#fcfcfb"
_INK = "#0b0b0b"
_INK_SECONDARY = "#52514e"
_MUTED = "#898781"
_GRID = "#e1e0d9"
_AXIS = "#c3c2b7"
_BEFORE = "#2a78d6"
_AFTER = "#eb6834"

_W, _H = 640, 460
_LEFT, _RIGHT, _TOP, _BOTTOM = 64, 24, 72, 56


def reliability_svg(calibration: Calibration, title: str) -> str:
    """A reliability diagram for one backend: observed frequency against mean predicted
    probability per bin, before and after scaling, with the diagonal as the ideal."""
    before = _bins(calibration.eval_pairs)
    after = _bins([(scale(p, calibration.temperature), label) for p, label in calibration.eval_pairs])

    def x(v: float) -> float:
        return _LEFT + v * (_W - _LEFT - _RIGHT)

    def y(v: float) -> float:
        return _H - _BOTTOM - v * (_H - _TOP - _BOTTOM)

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_W}" height="{_H}" '
        f'viewBox="0 0 {_W} {_H}" font-family="system-ui, sans-serif">',
        f'<rect width="{_W}" height="{_H}" fill="{_SURFACE}"/>',
        f'<text x="{_LEFT}" y="26" font-size="15" font-weight="600" fill="{_INK}">'
        f"Reliability — {title}</text>",
        f'<text x="{_LEFT}" y="46" font-size="12" fill="{_MUTED}">'
        f"ECE {100 * calibration.ece_before:.1f}% before, {100 * calibration.ece_after:.1f}% after "
        f"temperature scaling (T={calibration.temperature:.2f}, {calibration.eval_count} held-out "
        f"threads, fit on {calibration.fit_count})</text>",
    ]
    for tick in (0.0, 0.25, 0.5, 0.75, 1.0):
        parts += [
            f'<line x1="{x(tick):.1f}" y1="{y(0):.1f}" x2="{x(tick):.1f}" y2="{y(1):.1f}" '
            f'stroke="{_GRID}" stroke-width="1"/>',
            f'<line x1="{x(0):.1f}" y1="{y(tick):.1f}" x2="{x(1):.1f}" y2="{y(tick):.1f}" '
            f'stroke="{_GRID}" stroke-width="1"/>',
            f'<text x="{x(tick):.1f}" y="{_H - _BOTTOM + 18}" font-size="11" fill="{_MUTED}" '
            f'text-anchor="middle">{tick:g}</text>',
            f'<text x="{_LEFT - 10}" y="{y(tick) + 4:.1f}" font-size="11" fill="{_MUTED}" '
            f'text-anchor="end">{tick:g}</text>',
        ]
    parts += [
        f'<line x1="{x(0):.1f}" y1="{y(0):.1f}" x2="{x(1):.1f}" y2="{y(0):.1f}" stroke="{_AXIS}" stroke-width="1"/>',
        f'<line x1="{x(0):.1f}" y1="{y(0):.1f}" x2="{x(0):.1f}" y2="{y(1):.1f}" stroke="{_AXIS}" stroke-width="1"/>',
        f'<line x1="{x(0):.1f}" y1="{y(0):.1f}" x2="{x(1):.1f}" y2="{y(1):.1f}" '
        f'stroke="{_MUTED}" stroke-width="1" stroke-dasharray="4 4"/>',
        f'<text x="{(x(0) + x(1)) / 2:.1f}" y="{_H - 14}" font-size="12" fill="{_INK_SECONDARY}" '
        f'text-anchor="middle">Mean predicted probability</text>',
        f'<text x="18" y="{(y(0) + y(1)) / 2:.1f}" font-size="12" fill="{_INK_SECONDARY}" '
        f'text-anchor="middle" transform="rotate(-90 18 {(y(0) + y(1)) / 2:.1f})">Observed frequency</text>',
    ]
    for name, color, series in (("before scaling", _BEFORE, before), ("after scaling", _AFTER, after)):
        points = " ".join(f"{x(p):.1f},{y(o):.1f}" for p, o, _ in series)
        if len(series) > 1:
            parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2"/>')
        for predicted, observed, n in series:
            parts.append(
                f'<circle cx="{x(predicted):.1f}" cy="{y(observed):.1f}" r="4" fill="{color}" '
                f'stroke="{_SURFACE}" stroke-width="2">'
                f"<title>{name}: predicted {predicted:.2f}, observed {observed:.2f} (n={n})</title>"
                f"</circle>"
            )
    for i, (label, color) in enumerate((("before scaling", _BEFORE), ("after scaling", _AFTER))):
        ly = _TOP + 8 + 18 * i
        parts += [
            f'<rect x="{x(0) + 12:.1f}" y="{ly - 9}" width="10" height="10" rx="2" fill="{color}"/>',
            f'<text x="{x(0) + 28:.1f}" y="{ly}" font-size="12" fill="{_INK_SECONDARY}">{label}</text>',
        ]
    parts.append("</svg>")
    return "\n".join(parts) + "\n"
