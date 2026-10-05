# Design notes

Decisions that aren't visible from the code alone. A1 and A2 are covered in
[EXPERIMENTS.md](EXPERIMENTS.md), since measurement drove their design; this file
covers A3–A5 and the `explain` and `demo` commands.

## A3: stage-aware focus (`--phase`)

`PhaseRanker` (`digest/attach/phase.py`) scores each inbox item as

    score = fan_out score × 1 / (1 + stage distance) × (1 + 1 / (1 + days to gate))

A person's own stage or the one feeding it is distance 0; a stage outside the
active process counts as the whole process length away, so a passing gate demotes
last phase's items and promotes the new phase's in one move. `tests/test_phase.py`
pins that flip with constructed items, since no real inbox mixes both phases.

- The active process comes from gate dates, not the `status` column — `status` is
  a static snapshot and cannot tell day 5 from day 7. The active process is the one
  whose gate is next; on the gate day itself the exiting process still counts.
- The digest date comes from the items' timestamps, so the `Ranker` protocol keeps
  its signature; `load_inbox` guarantees one date per inbox.
- The ranker reads the graph once at build time; `rank` never touches the database.
- The YAML table is a tiebreaker only — a secondary sort key, never multiplied into
  the score — so the score stays explainable as distance × gate proximity.
- `phase_weights.yaml` is parsed by a small reader rather than adding PyYAML: it
  accepts exactly the two-level `role: {PHASE: number}` shape the file uses and
  rejects anything else with a line number.
- `--phase` replaces the ranker and refuses to combine with `--ranker`, since both
  occupy the same seam.

Eval wiring: the ranker factory takes `(data_dir, conn)` because A3 needs the
ingested graph and the weights file; the weights file lives beside the dataset,
like `gold.json`; a missing or malformed weights file marks `a3-phase` "not run"
instead of ending the eval. On replay, a3-phase matches core exactly — expected,
since confidences are 1.0 and no inbox exceeds three items, so the ranker can only
reorder within a day. Measured directly, `--phase` reorders 2 of 33 digests, both
Tom's — the one user owning stages in both products' processes. What that means
for A3's claim is in EXPERIMENTS.md.

## A4: entity aliases (`--aliases`)

`resolving_apply` (`digest/attach/aliases.py`) wraps core `apply`: a target_id that
is no task or requirement ID is looked up in the `aliases` table and applied under
its canonical ID; a name that resolves nowhere is recorded in `unresolved` and
listed by `digest eval`.

- The seam is an `apply_fn` parameter on `pipeline.ingest`, not a fifth protocol —
  the four pipeline protocols don't cover apply. The ApplyFn returns the delta *as
  applied*, because fan-out must see the canonical target; returning only a bool
  would fan the surface name out to nobody.
- An unresolved name drops that one delta, not the thread. Raising would roll back
  the transaction holding the unresolved row, and an unknown name is exactly the
  data gap A4 exists to surface — so the rest of the thread applies and the name
  is recorded for triage. With aliases off, the same delta skips the whole thread;
  `tests/test_aliases.py` pins the contrast.
- Aliases live in `data/graph_seed.json`: they are project-graph data, written
  once. The seeded surfaces are the names the four gold alias threads actually use.
- Surfaces compare normalized (casefold, collapsed whitespace). Unresolved rows
  keep the raw surface, which is what a human needs to write the missing alias.
- Resolution only matters under a live extractor — replay emits canonical IDs — so
  the `a4-aliases` configuration pairs aliases with `LLMExtractor` and reads
  against the `llm` row. Replay tests simulate model output by rewriting gold IDs
  to surface names.

## A5: phrased digest with fallback (`--render llm`)

`LLMRenderer` (`digest/attach/llm_render.py`) wraps `TemplateRenderer`: one
structured-output call rewrites the ranked cards into two-line entries, keeping the
template's grouping and order.

- The template renders first and is the fallback for everything: timeout, any
  error, or a response whose entries don't match the input items one for one. The
  check compares the ordered target IDs, which also catches a dropped item replaced
  by an invented one, and a reorder.
- The model writes only the two lines; headings, numbering, grouping and source
  links are composed from the items, so the model cannot invent or lose structure.
- Timeouts live in the client, not the renderer: 30 s for renders, 120 s for the
  extractor (added after a stalled connection froze an eval run — EXPERIMENTS.md).
- `--render` mirrors `--ranker`: a registry that fails fast on missing environment,
  before the database is touched.

## `digest explain` and `digest demo`

`digest explain --user <id> --date <date> --item <n>` prints one digest item's
chain: item, delta, signal, decider probabilities and routing. `digest demo` walks
the pipeline in replay mode with no keys: the planted silo thread, the two affected
owners' digests, the stage-aware section, an explain trace, and results.md.

- Decider answers are persisted at ingest (a `decisions` table), so explain shows
  what was actually answered rather than re-asking a backend that could answer
  differently. Re-ingest writes nothing new; a changed signal overwrites its row; a
  skipped thread is re-decided on retry. For an escalated thread the stored answer
  is the one that settled it.
- Explain recomputes the ranking rather than parsing the stored digest body; item
  numbering lives in one place (`display_order`), shared with the renderer. It
  takes the same `--ranker`/`--phase` flags as `run`, since numbering depends on
  them; A5 never renumbers, so the numbers match phrased digests too.
- The demo uses an in-memory database and writes nothing. It prints the committed
  results.md rather than running `digest eval`, which in a keyless shell would
  blank the live numbers.
- The stage-aware section separates the data from the ranker. Day 5 and day 7
  digests differ under any ranker, because each day holds different changes — so
  the demo says so, counts the digests `--phase` actually reorders (2 of 33), and
  shows one of those days under both rankers. The gate flip is credited to the
  unit test.
- Demo choices are data-driven where possible (the first cross-team case reaching
  two people, the 5th and 7th delta dates, gate dates from `processes`, the first
  reordered digest) and named where not: Tom, the one user `--phase` reorders,
  is also an affected owner in the silo case.
