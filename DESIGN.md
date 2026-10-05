# Design notes

Decisions that are not visible from the spec or the code alone, with the reasoning.

## A3: Stage-aware focus (`--phase`)

`PhaseRanker` in `digest/attach/phase.py` scores each inbox item as

    score = fan_out score × 1 / (1 + stage distance) × (1 + 1 / (1 + days to gate))

Distance 0 is the person's own stage or the one feeding it; a stage outside the
active process counts as the whole process length, so a passing gate demotes last
phase's items and promotes the new phase's in one move. The day 5 / day 7 demo either
side of EVT exit (gate 2026-03-09) rests on exactly that: `tests/test_phase.py`
pins the flip and shows the scores.

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
  all 1.0 and no inbox holds more than five items, so the top-five cut never drops
  anything and A3 can only reorder. Its measured effect is ordering and day-to-day
  focus, pinned by the day 5 / day 7 tests, not the membership metrics. Under the
  "measured gain or cut" rule, A3's case in the writeup therefore rests on the demo,
  and an ordering-sensitive metric (e.g. rank of the gold item) is the natural
  follow-up if the table alone must justify it.
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
  managing timers. The extractor's behaviour is unchanged (no timeout, as before).
- **`--render` mirrors `--ranker`**: a `RENDERERS` registry with `build_renderer`,
  failing fast before the database is touched when the environment is missing.
