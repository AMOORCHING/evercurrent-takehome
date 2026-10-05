# Design notes

Why the code is shaped the way it is — the decisions that are not visible from the
code alone, with the reasoning. A1 (the classifier cascade) and A2 (calibration) are
covered by the experiment log in [EXPERIMENTS.md](EXPERIMENTS.md), since measurement
drove their design; this file covers A3–A5 and the `explain` and `demo` commands.

## A3: Stage-aware focus (`--phase`)

`PhaseRanker` in `digest/attach/phase.py` scores each inbox item as

    score = fan_out score × 1 / (1 + stage distance) × (1 + 1 / (1 + days to gate))

Distance 0 is the person's own stage or the one feeding it; a stage outside the
active process counts as the whole process length away, so a passing gate demotes
last phase's items and promotes the new phase's in one move. `tests/test_phase.py`
pins that flip either side of EVT exit (gate 2026-03-09) and shows the scores, using
constructed items — no real inbox mixes items from both phases (see the measurement
note under "Eval wiring" below).

Decisions worth recording:

- **The active process is derived from gate dates, not the `status` column.** The
  seed's `status` is a snapshot ("active"/"planned") and never changes during replay,
  so it cannot tell day 5 from day 7. The process whose gate is the next one on or
  after the digest date is active; on the gate day itself the exiting process still
  counts as active, matching "the gate passes on day 6" meaning focus shifts on day 7.
- **The digest date comes from the items' delta timestamps.** The `Ranker` protocol
  has no date parameter and the seam stays untouched; `load_inbox` already guarantees
  all items share one date.
- **The ranker reads the graph once at build time** (`load_phase_graph`), keeping the
  convention that `rank` itself never touches the database.
- **The YAML table is a pure tiebreaker**: a secondary sort key, never multiplied into
  the score, so "breaks ties" stays literally true and the score remains explainable
  as distance × gate proximity alone.
- **`data/phase_weights.yaml` is read without PyYAML.** The project deliberately keeps
  its dependency list small, so `load_phase_weights` parses just the two-level
  `role: {PHASE: number}` mapping the file uses — comments and blank lines included —
  and rejects anything else with a line number. If the table ever needs more YAML than
  that, add PyYAML rather than growing the reader.
- **`--phase` replaces the ranker** and refuses to combine with a non-default
  `--ranker`, since both occupy the same seam.

### Eval wiring (`a3-phase`)

- **The eval registry's ranker factory takes `(data_dir, conn)`**, mirroring how the
  extractor factory takes the dataset directory. A3's ranker needs the ingested graph
  and the weights file; threading them through the factory keeps `Configuration`
  declarative, and the ranker is built after ingest, when the graph exists. The
  simpler factories ignore the arguments they don't need.
- **In eval, the weights file is `phase_weights.yaml` beside the dataset**
  (`--data`), the same convention as `gold.json` and `graph_seed.json`. The CLI's
  `--phase` keeps its repo-relative `data/phase_weights.yaml` default because
  `digest run` has no data-directory argument.
- **A missing or malformed weights file marks `a3-phase` "not run"** (via
  `RankerUnavailable`, caught in `collect_runs` alongside the extractor and decider
  equivalents) instead of ending the whole eval — the same treatment as a missing
  API key.
- **On the replay dataset, a3-phase matches core exactly** — silo recall 12 of 12,
  digest precision 43 of 43 — and that is expected: replay confidences are all 1.0
  and no inbox holds more than three items, so the top-five cut never drops
  anything, and a digest covers one day's changes, so the ranker can only reorder
  within a day. Measured directly — every real (user, day) inbox ranked both ways,
  which `digest demo` computes live — **A3 reorders 2 of 33 digests**, both Tom's
  (2026-03-05 and 2026-03-11); he is the one user owning stages in both products'
  processes. No real inbox holds items from both sides of a gate at once, which is
  why the gate flip lives in `test_evt_gate_passing_flips_the_ranking`, which
  constructs that inbox. What this means for A3's claim is taken up in EXPERIMENTS.md.

## A4: Entity aliases (`--aliases`)

`resolving_apply` in `digest/attach/aliases.py` wraps the core `apply`: a delta whose
target_id is no task or requirement ID is looked up in the `aliases` table and applied
under its canonical ID; a name that resolves nowhere lands in the `unresolved` table
and `digest eval` lists it.

- **The seam is an `apply_fn` parameter on `pipeline.ingest`**, not a fifth protocol.
  The four pipeline protocols don't cover `apply`, and the pipeline is the only
  caller. The ApplyFn returns the delta *as applied* (target possibly rewritten),
  because fan-out must see the canonical target — returning only a bool, as core
  `apply` does, would fan the surface name out to nobody. Core `apply` itself is
  untouched.
- **An unresolved name drops that one delta; it does not skip the thread.** The
  malformed-response path (raise, roll back, retry next ingest) would roll back the
  very transaction holding the unresolved row. And an unknown name is not a broken
  response — it is precisely the data gap A4 exists to surface. So the signal is
  saved, the rest of the thread applies, and the name is recorded for triage. With
  aliases off, the same delta raises `InvalidDelta` and skips the thread, which is
  the before/after contrast `tests/test_aliases.py` pins.
- **Aliases live in `data/graph_seed.json`** under an `aliases` key: they are project
  graph data (solved once, at write time), and the seed loader already gives every
  table idempotent inserts. The seeded surfaces are the names the four gold alias
  threads actually use — "MDB rev A", "fingertips", "HD-20", "AksIM".
- **Surfaces compare normalized** (casefold, collapse whitespace), enforced by a
  validator on the `Alias` model so the table itself stores normalized keys.
  Unresolved rows keep the raw surface the extractor wrote, since that is what a
  human needs to see to write the alias.
- **Resolution only matters under a model extractor.** Replay emits canonical gold
  IDs, so the `a4-aliases` eval configuration pairs aliases with `LLMExtractor` and
  is meant to be read against the `llm` row; replay-plus-aliases would measure
  nothing. Replay tests instead simulate model output by rewriting gold target IDs
  to surface names.

## A5: Phrased digest with fallback (`--render llm`)

`LLMRenderer` in `digest/attach/llm_render.py` wraps `TemplateRenderer`: one
structured-output call rewrites the ranked cards into two-line entries (headline,
why-plus-source), keeping the template's heading, product grouping and order.

- **The template renders first and is the fallback for everything**: timeout, any
  API or validation error (one broad `except` — the template is the contract, so no
  failure is worth distinguishing), and any response whose entries don't match the
  input items one for one. The identity check compares the ordered list of target
  IDs, which is stricter than a count check alone: it also catches a dropped item
  replaced by an invented one, and a reorder — both of which "drops or adds an
  item" is meant to forbid.
- **The model only writes the two lines**; headings, numbering, grouping and source
  links are composed deterministically from the items, so the model cannot invent
  or lose structure even when its text is accepted.
- **Timeouts are enforced at the client**, not by the renderer managing timers:
  `OpenAIClient` takes an optional `timeout` (30 s for renders), and the extractor's
  client carries its own 120 s timeout — added after one stalled connection froze a
  full eval run (the story is in EXPERIMENTS.md).
- **`--render` mirrors `--ranker`**: a `RENDERERS` registry with `build_renderer`,
  failing fast before the database is touched when the environment is missing.

## `digest explain` and `digest demo`

`digest explain --user <id> --date <date> --item <n>` prints one digest item's chain —
item, delta, signal, decider probabilities and routing (`digest/explain.py`).
`digest demo` walks the pipeline in replay mode with no API keys (`digest/demo.py`):
the planted silo thread, the two affected owners' digests for that day, the
stage-aware focus section, an explain trace, and results.md.

- **Decider answers are persisted at ingest** — a `decisions` table, one row per
  decided signal, written by `save_decisions` in `digest/attach/deciders.py` — so
  `explain` shows what was actually answered rather than re-asking a backend, which
  would need keys and could answer differently. A re-ingest decides nothing new
  (unchanged hashes), so the table is idempotent like every other; a changed signal
  overwrites its row, and a skipped thread's decision is not stored, so the retry
  re-decides it. For an escalated thread the stored probabilities are the
  second-stage answer that settled it, and `settled` records the final action.
- **Explain recomputes the ranking instead of parsing the stored digest body.** Item
  numbers are display order — the template groups cards by product — so the counting
  lives in one place: `display_order` in `digest/core/render.py`, used by both the
  renderer and explain. Explain takes the same `--ranker`/`--phase` flags as `run`
  because the numbering depends on them; the stored body may also have been phrased
  by A5, which never renumbers (the fallback contract), so the numbers still match.
- **The demo runs on an in-memory database and writes nothing**, so it is trivially
  idempotent and safe from a fresh clone. It prints the committed results.md rather
  than running `digest eval`, which in a keyless shell would overwrite the live
  jev and llm numbers with "not run" rows.
- **The stage-aware section separates the data from the ranker.** One user's day 5
  and day 7 digests differ under any ranker, because each day's digest covers only
  that day's changes — that pair shows focus moving across the gate, not the ranker
  at work. So the demo says exactly that in its own output, counts live how many
  real digests `--phase` reorders against the core ranker (2 of 33), and renders one
  of those days under both rankers side by side — the one comparison where the
  ranker is the only variable. The gate flip is credited to the constructed-item
  unit test, not to real data.
- **Demo choices are data-driven where the data can decide** (the first cross-team
  case reaching two people, day 5 and day 7 as the 5th and 7th distinct delta dates,
  gate dates between them read from `processes`, the side-by-side day as the first
  real reordered digest) **and named where it cannot**: Tom is the followed user,
  because his are the only real digests `--phase` reorders — he owns stages in both
  products' processes — and he is also an affected owner in the silo case, keeping
  one thread of narrative through the demo.
