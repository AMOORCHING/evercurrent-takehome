# Experiments: what the evaluation changed about the design

This is the experiment log: three evaluation rounds, what each showed, and the design
changes each caused. The largest change is the two-stage escalation added to the A1
classifier cascade on Oct 4, 2026. Metric definitions and the full results table live
in results.md (regenerate with `digest eval`).

Terms used throughout:

- **Gate accuracy** — how often the decider's `changes_state` answer (at or above 0.5)
  agrees with the gold label for whether a thread changes state.
- **Silo recall** — the share of the 12 planted cross-team changes that reach the
  affected person's digest.
- **Calibration error (ECE)** — the gap between the decider's confidence and its actual
  hit rate; 0% means "when it says 80%, it is right 80% of the time".
- **Escalation band** — threads the decider scores between 0.2 and 0.8, i.e. the ones
  it is unsure about.

## The starting point

The core pipeline extracts state changes from every thread. A1 puts a cheap classifier
in front of it: a `Decider` answers four questions per thread, and the `changes_state`
probability routes the thread — at or above 0.8 it goes to the extractor, at or below
0.2 it is dropped, and the 0.2–0.8 band went to the extractor anyway, just logged as
escalated. Nothing stronger ever looked at the uncertain threads; escalation was only
a cost signal.

A2 then fit temperature scaling per backend on a held-out third of the gold labels
(ECE reported before and after, reliability plots under `calibration/`) and derived
the ranker's section thresholds from stated cost ratios — include an item when
`p × cost_of_miss ≥ (1 − p)`, giving 1/11 for "needs you" (a miss is 10× worse than
noise), 1/4 for "changes that affect you" (3×), and 1/2 for FYI (1×).

## Round 1: the cascade works, but wastes its own signal

Per backend, on the 120-thread dataset (counts in results.md):

| Observation | jev | LLM decider |
| --- | --- | --- |
| Gate accuracy (changes_state) | 87.5% | 96.7% |
| Silo recall through the cascade | 11 of 12 | 12 of 12 |
| ECE before scaling | 9.4% | 1.7% |
| Escalation rate | 24.2% | 3.3% |
| Median latency per thread | 178 ms | 2,719 ms |
| Cost per digest | $0.0022 | $0.0146 |

Four observations drove the next change:

1. **Only one answer matters for routing.** jev's weak change_type accuracy (42.9%)
   costs nothing, because the extractor re-derives the type anyway; the LLM's
   over-flagging of contradictions (78.3% against a 96.7% always-no baseline) is not
   wired into routing either. Everything rides on `changes_state`.
2. **jev is a good gate but often unsure.** 87.5% gate accuracy at 178 ms and
   ~$0.002 per digest set the cost floor, but it put a quarter of all threads in the
   uncertain band — and the original routing paid for a full extraction on every one.
3. **The hard drop contradicted A2's own cost model.** The gate dropped threads below
   a fixed 0.2 while the ranker priced a "needs you" miss at 10:1 (threshold ≈ 0.09).
   jev's one missed cross-team case was a *confident* drop, below 0.2 — and a drop at
   ingest is unrecoverable downstream.
4. **Calibration was not the problem.** jev (9.4% ECE) and the LLM decider (1.7%)
   arrived near-calibrated, so temperature scaling moved them only within noise.
   Rescaling would have nudged jev's 0.2 drops to about 0.23 — so the lever on the
   missed case is the drop threshold, not the calibration.

## Alternatives considered

1. **Escalate the uncertain band to the stronger decider** — jev decides every thread;
   the LLM decider re-decides only the 0.2–0.8 band, and that answer is final.
   Expected cost roughly $0.0023 + 0.24 × $0.0146 ≈ $0.006 per digest, against
   $0.0146 for running the LLM decider on everything, with near-LLM accuracy on
   exactly the threads jev finds hard. (In replay the extractor is free, so the
   measured cost rises slightly; the saving appears at live volume, where a cheap
   second opinion prevents paid extractions on noise.)
2. **Derive the drop threshold from the cost ratio** — drop only below a calibrated
   1/11 instead of a fixed 0.2. The only option that would have rescued the
   confidently-dropped cross-team case.
3. **No gate at all** — at 120 threads, running the LLM decider on everything costs
   pennies and has perfect recall; a cascade only earns its complexity at real Slack
   volume.

Training a custom classifier on 120 labeled threads was rejected outright.

## The change (Oct 4): second-stage escalation

Option 1 was built. `Cascade` accepts an optional second-stage decider: the uncertain
band is re-decided and that answer is final — drop or extract. Confident first-stage
answers never pay for the second call, and if the second call fails, the thread goes
to the extractor rather than being lost. Exposed as `digest ingest --escalate-to llm`
and as the `a1-jev-llm` configuration; the answer that settled each thread is stored,
so the hybrid gets its own calibration row and reliability plot. The default routing,
with no second stage, is unchanged.

Accepted limit: this hardens the uncertain band but cannot rescue a confident
first-stage drop — jev's missed cross-team case stays missed. Fixing that is
option 2, which remains open.

## Round 2: the hybrid, measured

All three columns from the same live run on Oct 4. The backends are nondeterministic,
so numbers drift a thread or two between runs (jev's gate accuracy read 87.5% in
round 1 and 88.3% here); single-count gaps are noise.

| Metric | a1-jev | a1-jev-llm | a1-llm |
| --- | --- | --- | --- |
| Silo recall | 91.7% (11 of 12) | 91.7% (11 of 12) | 100.0% (12 of 12) |
| Digest precision | 100.0% (31 of 31) | 100.0% (29 of 29) | 100.0% (39 of 39) |
| Gate accuracy (changes_state) | 88.3% | 89.2% | 95.8% |
| Change type accuracy | 42.9% | 62.9% | 88.6% |
| Calibration error (before → after) | 9.7% → 12.8% | 5.1% → 5.4% | 2.5% → 3.2% |
| Escalation rate | 24.2% | 24.2% | 2.5% |
| Median decider latency | 175 ms | 193 ms | 2,804 ms |
| Cost per digest | $0.0022 | $0.0074 | $0.0147 |

What the numbers say:

- **The gate's probabilities got markedly more usable.** The hybrid's ECE before
  scaling is 5.1% against jev's 9.7%, because the band — where jev's uncertainty
  lives — now carries LLM answers. Change type accuracy rose twenty points for the
  same reason. Median latency barely moved (193 ms), since only the band pays the
  2.8-second call.
- **Cost landed near the estimate**: $0.0074 against the predicted ~$0.006, half the
  cost of the all-LLM decider. The larger saving appears at live volume: the second
  stage now drops the band's noise before it reaches a paid extractor call.
- **There is a real tradeoff.** The second stage can drop an escalated thread that
  holds a real change — extractor accuracy 90.0% against jev's 91.7%, 29 digest items
  against 31 — threads the old route-everything-to-the-extractor behaviour kept. And
  as predicted, silo recall stays 11 of 12: the missed case was a confident
  first-stage drop the second stage never sees.

Net: the hybrid bought a better-calibrated, better-typed gate at half the all-LLM
cost and flat latency, in exchange for a small recall tax on the band. The open item
is unchanged: align the drop threshold with the 10:1 cost model (option 2) to go
after the last cross-team miss.

## Round 3 (Oct 5): A3, A4, and a hung eval

The first full-table attempt did not finish: it hung 71 minutes on one dead HTTPS
connection. The decider calls carry a 60-second timeout, but the extractor's client
had none, so one stalled request froze a ten-configuration eval.

**Design change:** the extractor client now carries a 120-second timeout, and a failed
model call is treated exactly like a malformed response — log the thread ID, skip the
thread, continue; the thread retries on the next ingest. One flaky call can no longer
take down a run. The rerun finished in about 11 minutes.

The A4 comparison — the live extractor with and without alias resolution:

| Metric | llm | a4-aliases |
| --- | --- | --- |
| Silo recall | 100.0% (12 of 12) | 100.0% (12 of 12) |
| Digest precision | 95.1% (39 of 41) | 95.3% (41 of 43) |
| Extractor accuracy | 95.0% (114 of 120) | 95.8% (115 of 120) |

What the numbers say:

- **A4's gain is small and within the noise rule.** The aliased run delivered 43
  digest items — exactly the replay count, two more than the plain llm run — but
  every gap here is one or two counts, and the two rows come from separate
  nondeterministic runs. A4's value is a zero-cost safety net for loosely named
  parts (the four gold alias threads pass under their surface names in
  tests/test_aliases.py), not a percentage swing; recall was already perfect, so
  there was no headroom to show one.
- **The unresolved-names list came back empty.** Every name the model emitted was
  either canonical or resolved through the table — largely because the extractor
  prompt lists task IDs, so the model rarely invents a free-text part name.
- **A3 can only reorder, and its reordering was measured directly.** Replay
  confidences are 1.0 and no inbox holds more than three items, so the top-five cut
  never drops anything and a3-phase matches core on every membership metric. Ranking
  every real (user, day) inbox with both rankers, `--phase` changes the order in
  **2 of 33 digests** — both Tom's, the one user owning stages in both products'
  processes. The demo therefore shows the same day under both rankers, where the
  ranker is the only variable, and the gate flip is pinned by a unit test with
  constructed items, since no real day mixes items from both phases.
- **The decider numbers reproduced round 2** within a thread or two: jev 88.3% gate
  accuracy at 174 ms, the hybrid 90.0% with 3.1% ECE at 184 ms, the all-LLM decider
  95.0% at 3,070 ms. The same cross-team case is still lost to a confident
  first-stage drop, so option 2 remains open.

## Pricing became real (Oct 5)

Both backends' dollar figures started as estimates; by the end of Oct 5 both were
derived from billed spend.

**jev.** TypeSafe's usage dashboard reports billed spend: $0.0339 for the week's 720
jev requests (884,983 tokens), i.e. $0.0056 per 120-thread pass of ~134k input and
~13k output tokens. That is exactly one tenth of what the estimated constants
predicted, so the constants moved from $0.30/$1.20 to **$0.03 in / $0.12 out per
Mtok**. One caveat: the dashboard reports token totals, not separate input and output
prices, so the 1:4 input:output ratio is assumed; every pass has the same token mix,
so the data cannot distinguish between splits.

**llm (gpt-6-sol).** The usage API reports per-day tokens (Oct 4: 920,469 in /
31,247 out; Oct 5: 1,372,701 in / 100,474 out) and the cost API the matching dollars
($2.3195 and $4.0858). Two days and two unknowns solve exactly: **$2.12 in / $11.63
out per Mtok**, reproducing both bills to the cent, and landing within ~15% of the
earlier $2.50/$10.00 estimate — so the fit confirms the table's llm dollars rather
than changing them (about a 6% shift, within the noise rule). These are effective
rates: the input mix spans cached and uncached tokens, so the cache discount is
folded into the blend.

Costs re-derived with the measured rates: a1-jev **$0.0022 → $0.0002** per digest
(exact — that configuration's spend is pure jev, and all jev spend scales by one
tenth); a1-jev-llm **$0.0075 → ~$0.0055** (the jev share becomes negligible and the
LLM escalation share dominates); a1-llm unchanged. The cost cells in results.md were
updated with these numbers; a keyed `digest eval` rerun regenerates them exactly.
At billed prices, the gate costs about 1/70th of the all-LLM decider per digest,
and the whole three-round evaluation program cost $6.41 of OpenAI spend plus $0.03
of TypeSafe — about **$6.44 end to end**. Dollar figures in the earlier sections are
left as written; they record what was known when those decisions were made.

## The "measured gain or cut" rule, applied to A3 (Oct 5)

The spec's third rule says every attachment must show a measured gain in the results
table or be cut. Strictly read, that would cut A3: it matches the core ranker on
every membership metric, and its ordering effect touches 2 of 33 digests.

It stays, with a narrower claim, for two reasons. The build order names A1, A2 and A3
as the minimum feature set, and the mechanism itself is real and tested — stage
distance and gate proximity provably flip a ranking when a gate passes. So A3 is
presented as a working mechanism that this dataset is too shallow to reward (no
inbox exceeds three items, so ordering is all it can change), rather than as a
measured win. Two things would settle it properly: deeper inboxes, and an
ordering-sensitive metric — such as the rank of the gold item — in the eval table.
