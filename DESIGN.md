# Design notes

Decisions that are not visible from the spec or the code alone, with the reasoning.

## A3: Stage-aware focus (`--phase`)

`PhaseRanker` in `digest/attach/phase.py` scores each inbox item as

    score = fan_out score × 1 / (1 + stage distance) × (1 + 1 / (1 + days to gate))

Distance 0 is the person's own stage or the one feeding it; a stage outside the
active process counts as the whole process length, so a passing gate demotes last
phase's items and promotes the new phase's in one move. `tests/test_phase.py` pins
that flip either side of EVT exit (gate 2026-03-09) with constructed items and
shows the scores — constructed, because no real inbox mixes items from both phases
(the measured-effect notes under "Eval wiring" below).

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
  the score, so "breaks ties" in the spec stays literally true and the score remains
  explainable as distance × gate proximity alone.
- **`data/phase_weights.yaml` is read without PyYAML.** The stack pins the dependency
  list (CLAUDE.md), so `load_phase_weights` parses just the two-level
  `role: {PHASE: number}` mapping the file uses — comments and blank lines included —
  and rejects anything else with a line number. If the table ever needs more YAML than
  that, add PyYAML rather than growing the reader.
- **`--phase` replaces the ranker** and refuses to combine with a non-default
  `--ranker`, since both occupy the same seam.

### Eval wiring (`a3-phase`)

- **The eval registry's ranker factory now takes `(data_dir, conn)`** instead of no
  arguments, mirroring how `extractor` already takes the dataset directory. A3's
  ranker needs the ingested graph and the weights file, and threading them through
  the factory keeps `Configuration` declarative; the ranker is built after ingest,
  which is when the graph exists. The three simple factories (`_fixed_ranker`,
  `_calibrated_ranker`, `_phase_ranker`) ignore what they don't need.
- **In eval, the weights file is `phase_weights.yaml` beside the dataset**
  (`--data`), the same convention as `gold.json` and `graph_seed.json`. The CLI's
  `--phase` keeps its repo-relative `data/phase_weights.yaml` default because
  `digest run` has no data-directory argument.
- **A missing or malformed weights file marks `a3-phase` "not run"** (via
  `RankerUnavailable`, now caught in `collect_runs` alongside the extractor and
  decider equivalents) instead of ending the whole eval, matching how a missing API
  key is treated.
- **On the replay dataset, a3-phase matches core exactly**: silo recall 12 of 12 and
  digest precision 43 of 43. That is expected, not a bug — replay confidences are
  all 1.0 and no inbox holds more than three items, so the top-five cut never drops
  anything and A3 can only reorder within a day (a digest covers one day's changes,
  so it can never move an item across days). Measured directly — rank every real
  (user, day) inbox under both rankers and compare the orders, which is what
  `digest demo` now computes live — **A3 reorders 2 of 33 digests**, both tom's
  (2026-03-05 and 2026-03-11), because he is the user owning stages in both
  products' processes. Neither reorder sits at the EVT gate: no real inbox holds
  items from both phases at once, so the gate flip the spec describes only appears
  in `test_evt_gate_passing_flips_the_ranking`, which constructs that inbox. Under
  the "measured gain or cut" rule the honest statement is that A3's measured effect
  on this dataset is 2 reordered digests and no membership change; the mechanism is
  real and pinned, but it needs inboxes deeper than three items to earn a table row.
- **results.md was not regenerated with this change.** It holds live jev/llm numbers
  from the Oct 4 run; regenerating in a shell without API keys would replace those
  rows with "not run". Rerun `digest eval` with keys set to refresh the table — the
  a3-phase rows will appear then.

## A4: Entity aliases (`--aliases`)

`resolving_apply` in `digest/attach/aliases.py` wraps the core `apply`: a delta whose
target_id is no task or requirement ID is looked up in the `aliases` table and applied
under its canonical ID; a name that resolves nowhere lands in the `unresolved` table
and `digest eval` lists it.

- **The seam is an `apply_fn` parameter on `pipeline.ingest`**, not a fifth protocol.
  The spec's four protocols don't cover `apply`, and the pipeline is the only caller.
  The ApplyFn returns the delta *as applied* (target possibly rewritten), because
  fan-out must see the canonical target — returning only a bool, as core `apply`
  does, would fan the surface name out to nobody. Core `apply` itself is untouched.
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
  IDs, which is stricter than the count check alone: it also catches a dropped item
  replaced by an invented one, and a reorder — both of which "drops or adds an
  item" is meant to forbid.
- **The model only writes the two lines**; headings, numbering, grouping and source
  links are composed deterministically from the items, so the model cannot invent
  or lose structure even when its text is accepted.
- **Timeout is enforced at the client**: `OpenAIClient` gained an optional `timeout`
  passed to the OpenAI constructor (30 s for renders), rather than the renderer
  managing timers. The extractor kept no timeout when A5 landed; it gained its own
  120 s one (`EXTRACT_TIMEOUT_S` in `extract.py`) on Oct 5, after a stalled
  connection froze a full eval — the story is in EXPERIMENTS.md.
- **`--render` mirrors `--ranker`**: a `RENDERERS` registry with `build_renderer`,
  failing fast before the database is touched when the environment is missing.

## `digest explain` and `digest demo` (added Oct 5)

`digest explain --user <id> --date <date> --item <n>` prints one digest item's chain -
item, delta, signal, decider probabilities and routing (`digest/explain.py`).
`digest demo` walks the pipeline in replay mode with no API keys
(`digest/demo.py`): the X1 silo thread, the two affected owners' digests for that
day, the `--phase` section, an explain trace, and results.md.

- **Decider answers are now persisted.** The cascade used to keep decisions, routes
  and escalation outcomes only in memory for `digest eval`, so explain had nothing to
  read. `digest ingest` now writes a `decisions` table (one row per decided signal,
  written by `save_decisions` in `digest/attach/deciders.py`); explain shows what was
  actually answered at ingest time rather than re-asking a backend, which would need
  keys and could answer differently. A re-ingest decides nothing new (unchanged
  hashes), so the table is idempotent like every other; a changed signal overwrites
  its row, and a skipped thread's decision is not stored so the retry re-decides it.
  For an escalated thread the stored probabilities are the settling (second-stage)
  answer, matching `Cascade.decisions`; `settled` records the final action.
- **Explain recomputes the ranking instead of parsing the stored digest body.** Item
  numbers are display order - the template groups cards by product - so the counting
  lives in one place: `display_order` in `digest/core/render.py`, used by both the
  renderer and explain. Explain takes the same `--ranker`/`--phase` flags as `run`
  because the item numbering depends on them; the stored body may also have been
  phrased by A5, which never renumbers (the fallback contract), so numbers still match.
- **The demo runs on an in-memory database and writes nothing**, so it is trivially
  idempotent and safe from a fresh clone. It prints the committed results.md rather
  than running `digest eval`, which would overwrite the live jev/llm numbers with
  "not run" rows in a keyless shell (same reasoning as the A3 note above).
- **The --phase section was restructured on Oct 5 to show what A3 actually does.**
  The first version showed tom's day 5 and day 7 digests and implied the difference
  was phase-awareness; in fact those two digests hold different items under any
  ranker, because a digest covers one day's changes — the difference was the data.
  The section now keeps the day 5 / day 7 pair (the spec names that demo), states
  that caveat in its own output, computes live how many real digests `--phase`
  reorders against the core ranker (2 of 33), and renders one of those days under
  both rankers side by side — the only comparison where the ranker, not the
  calendar, is the variable. The gate flip is credited to the constructed-item unit
  test, not to real data.
- **Demo choices are data-driven where the data can decide** (the first cross-team
  case reaching two people, day 5/7 as the 5th/7th distinct delta dates, gate dates
  between them read from `processes`, the side-by-side day as the first real
  reordered digest, preferring the followed user's) **and named where it cannot**:
  `tom` is the `--phase` user, because his are the only real digests `--phase`
  reorders — he owns stages in both products' processes — and he is also an
  affected owner in the silo case.
