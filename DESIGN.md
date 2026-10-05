# Design notes

Decisions that aren't visible from the code alone. A1 and A2 are covered in
[EXPERIMENTS.md](EXPERIMENTS.md), since measurement drove their design; this file
covers A3 through A5 and the `explain` and `demo` commands.

## A3: stage-aware focus (`--phase`)

`PhaseRanker` (`digest/attach/phase.py`) scores each inbox item as

    score = fan_out score × 1 / (1 + stage distance) × (1 + 1 / (1 + days to gate))

A person's own stage or the one feeding it is distance 0, and a stage outside the
active process counts as the whole process length away, so a passing gate demotes
last phase's items and promotes the new phase's in one move. `tests/test_phase.py`
pins that flip with constructed items, since no real inbox mixes both phases.

- The active process is derived from gate dates rather than the `status` column,
  because `status` is a static snapshot and cannot tell day 5 from day 7. The
  active process is the one whose gate comes next, and on the gate day itself the
  exiting process still counts.
- The digest date comes from the items' timestamps, so the `Ranker` protocol keeps
  its signature; `load_inbox` already guarantees one date per inbox.
- The ranker reads the graph once at build time, and `rank` never touches the
  database.
- The YAML table is only a tiebreaker, used as a secondary sort key and never
  multiplied into the score, so the score stays explainable as distance times
  gate proximity.
- `phase_weights.yaml` is parsed by a small reader rather than adding PyYAML. It
  accepts exactly the two-level `role: {PHASE: number}` shape the file uses and
  rejects anything else with a line number.
- `--phase` replaces the ranker and refuses to combine with `--ranker`, since
  both occupy the same seam.

Eval wiring: the ranker factory takes `(data_dir, conn)` because A3 needs the
ingested graph and the weights file; the weights file lives beside the dataset,
like `gold.json`; and a missing or malformed weights file marks `a3-phase` "not
run" instead of ending the eval. On replay, a3-phase matches core exactly, which
is expected: confidences are 1.0 and no inbox exceeds three items, so the ranker
can only reorder within a day. Measured directly, `--phase` reorders 2 of 33
digests, both belonging to Tom, the one user who owns stages in both products.
What that means for A3's claim is in EXPERIMENTS.md.

## A4: entity aliases (`--aliases`)

`resolving_apply` (`digest/attach/aliases.py`) wraps core `apply`: a target_id
that is no task or requirement ID is looked up in the `aliases` table and applied
under its canonical ID, and a name that resolves nowhere is recorded in
`unresolved` and listed by `digest eval`.

- The seam is an `apply_fn` parameter on `pipeline.ingest`, since the four
  pipeline protocols don't cover apply and the pipeline is its only caller. The
  ApplyFn returns the delta as applied, because fan-out needs to see the
  canonical target; if it only returned a bool, the surface name would fan out to
  nobody.
- An unresolved name only drops that one delta, and the rest of the thread still
  applies. Raising would roll back the transaction that holds the unresolved row,
  and an unknown name is exactly the data gap A4 exists to surface, so the name
  is recorded for triage instead. With aliases off, the same delta skips the
  whole thread; `tests/test_aliases.py` pins that contrast.
- Aliases live in `data/graph_seed.json` because they are project-graph data,
  written once. The seeded surfaces are the names the four gold alias threads
  actually use.
- Surfaces compare normalized (casefold, collapsed whitespace). Unresolved rows
  keep the raw surface, which is what a human needs to write the missing alias.
- Resolution only matters under a live extractor, since replay emits canonical
  IDs, so the `a4-aliases` configuration pairs aliases with `LLMExtractor` and
  should be read against the `llm` row. Replay tests simulate model output by
  rewriting gold IDs to surface names.

## A5: phrased digest with fallback (`--render llm`)

`LLMRenderer` (`digest/attach/llm_render.py`) wraps `TemplateRenderer`: one
structured-output call rewrites the ranked cards into two-line entries, keeping
the template's grouping and order.

- The template renders first and is the fallback for everything: timeout, any
  error, or a response whose entries don't match the input items one for one.
  The check compares the ordered target IDs, which also catches a dropped item
  replaced by an invented one, and a reorder.
- The model writes only the two lines. Headings, numbering, grouping and source
  links are composed from the items, so the model cannot invent or lose
  structure.
- Timeouts are set on the API client rather than managed by the renderer: 30
  seconds for renders, and 120 seconds for the extractor, added after a stalled
  connection froze an eval run (EXPERIMENTS.md).
- `--render` mirrors `--ranker`: a registry that fails fast when the environment
  is missing, before the database is touched.

## `digest explain` and `digest demo`

`digest explain --user <id> --date <date> --item <n>` prints one digest item's
chain: item, delta, signal, decider probabilities and routing. `digest demo`
walks the pipeline in replay mode with no keys: the planted silo thread, the two
affected owners' digests, the stage-aware section, an explain trace, and
results.md.

- Decider answers are persisted at ingest (a `decisions` table), so explain shows
  what was actually answered rather than re-asking a backend that could answer
  differently. Re-ingest writes nothing new, a changed signal overwrites its row,
  and a skipped thread is re-decided on retry. For an escalated thread the stored
  answer is the one that settled it.
- Explain recomputes the ranking rather than parsing the stored digest body, and
  item numbering lives in one place (`display_order`), shared with the renderer.
  It takes the same `--ranker`/`--phase` flags as `run`, since numbering depends
  on them; A5 never renumbers, so the numbers match phrased digests too.
- The demo uses an in-memory database and writes nothing. It prints the committed
  results.md rather than running `digest eval`, which in a keyless shell would
  blank the live numbers.
- The stage-aware section is careful about what it attributes to the ranker. Day
  5 and day 7 digests differ under any ranker, because each day holds different
  changes, so the demo points that out in its own output, counts how many digests
  `--phase` actually reorders (2 of 33), and shows one of those days under both
  rankers. The gate flip itself is demonstrated by the unit test.
- Most demo choices are derived from the data: the first cross-team case that
  reaches two people, the 5th and 7th delta dates, gate dates read from
  `processes`, and the first reordered digest. The one hardcoded choice is Tom,
  whose digests are the only ones `--phase` reorders and who is also an affected
  owner in the silo case.
