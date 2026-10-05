# Experiments: what the eval changed about the design

This file records the design changes made from measurement rather than from the spec —
the largest being the two-stage escalation added to the A1 cascade on Oct 4, 2026.
The working spec carries the dated amendments; this is the longer story — what the code looked
like, what the numbers said, the tradeoffs weighed, and what was done. It ends with
the spec's "measured gain or cut" rule applied to A3, and why the claim was
downgraded rather than the attachment cut. Metric definitions and the full table
live in results.md (regenerate with `digest eval`).

## The original design

The core pipeline extracts state changes from every thread. A1 put a cheap classifier in
front of it: a `Decider` answers four questions per thread, and `changes_state` routes —
at or above 0.8 the thread goes to the extractor, at or below 0.2 it is dropped, and the
0.2–0.8 middle band **went straight to the extractor**, logged as escalated. "Escalate"
was spelled "extract anyway": the band was tracked as a cost signal, but nothing stronger
ever looked at those threads.

A2 then fit temperature scaling per backend on a fixed seeded third of the gold labels
(ECE reported before and after, reliability plots under `calibration/`) and derived the
ranker's section thresholds from the spec's cost ratios — include when
`p × cost_of_miss ≥ (1 − p)`, so 1/11 for "needs you" (10:1), 1/4 for "changes that
affect you" (3:1), 1/2 for FYI (1:1).

## What the first eval round showed

Per-backend, on the 120-thread dataset (counts in results.md):

| Observation | jev | LLM decider |
| --- | --- | --- |
| Gate accuracy (changes_state) | 87.5% | 96.7% |
| Silo recall through the cascade | 11 of 12 | 12 of 12 |
| ECE before scaling | 9.4% | 1.7% |
| Escalation rate (middle band) | 24.2% | 3.3% |
| Median latency per thread | 178 ms | 2,719 ms |
| Cost per digest | $0.0022 | $0.0146 |

Tradeoffs noticed:

1. **The weak answers were contained by the seam.** jev's change_type accuracy (42.9%)
   costs nothing because the extractor re-derives the type; the LLM's contradicts
   over-flagging (78.3% against a 96.7% always-no baseline) is not wired into routing.
   The only load-bearing answer is `changes_state`.
2. **jev is a good gate and a bad oracle.** 87.5% at 178 ms and ~$0.002/digest is the
   cost floor, but it put a quarter of all threads in the middle band — and under the
   original routing every one of those paid for a full extraction anyway.
3. **The hard drop contradicted A2's own cost model.** The gate dropped at a tuned 0.2
   while the ranker priced a "needs you" miss at 10:1 (threshold 1/11 ≈ 0.09). jev's one
   missed cross-team case was a *confident* drop (changes_state ≤ 0.2), thrown away
   irreversibly at ingest.
4. **Calibration was not the problem.** jev (9.4% ECE) and the LLM (1.7%) were already
   near-calibrated; temperature scaling fit on 40 threads moved ECE within noise (and
   slightly up on the eval split). The uncalibratable reference is passthrough: 73.8%
   ECE with T pinned at the clamp. jev's fitted T=1.13 would have moved its 0.2 drops to
   only ~0.23 — so the lever on the missed case is the drop threshold, not the
   temperature.

## Alternatives considered

1. **Escalate the middle band to the stronger decider, not just to the extractor** —
   jev → LLM decider on the 0.2–0.8 band → extractor. Pays LLM prices on ~24% of
   threads; expected cost roughly $0.0023 + 0.24 × $0.0146 ≈ $0.006/digest against
   $0.0146 for LLM-everywhere, with near-LLM accuracy on exactly the threads jev finds
   hard. With a live extractor, a second decider call (~$0.0001) before each full
   extraction is the saving; in replay the extractor is free, so replay cost per digest
   goes *up* slightly — the win is a live-volume argument.
2. **Derive the drop threshold from the cost ratio** — drop only below a calibrated
   1/11 instead of a raw 0.2, aligning the gate with the ranker's cost model. The only
   option that would have rescued the confidently-dropped cross-team case.
3. **No gate at all** — passthrough or LLM-everywhere. Perfect recall, pennies at 120
   threads; the cascade only earns its complexity at real Slack volume.

Rejected outright: training a bespoke classifier on 120 labeled threads.

## Action taken (Oct 4)

Option 1. `Cascade` now accepts an optional second-stage decider: the middle band is
re-decided and that answer is final — drop, or extract (a second middle answer
extracts). Confident first-stage answers never pay for the second call, and an
escalation failure fails open to the extractor. Exposed as `digest ingest
--escalate-to llm` and as the `a1-jev-llm` eval configuration; `decisions` records the
answer that settled each thread, so the hybrid gets its own calibration row and
reliability plot. The default routing — no escalation decider — is unchanged from the
spec.

Known limit, accepted: this hardens the middle band but cannot rescue a confident
first-stage drop. jev's missed cross-team case (silo recall 11 of 12) stays missed under
option 1; fixing it is option 2's territory and remains open.

## What the second eval round showed

All three columns from the same live run on Oct 4 (the backends are nondeterministic, so
numbers drift a thread or two between runs — e.g. jev's gate accuracy read 87.5% in the
first round and 88.3% here; treat single-count gaps as noise):

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

Reading it honestly:

- **The gate's probabilities got markedly more usable.** The hybrid's ECE before
  scaling is 5.1% against jev's 9.7%, because the band — where jev's uncertainty
  lives — now carries LLM answers. Change type accuracy rose twenty points for the
  same reason. Median latency barely moved (193 ms): only the band pays the 2.8 s call.
- **Cost landed near the estimate:** $0.0074 against the predicted ~$0.006, a
  2× saving over LLM-everywhere. The larger live-volume saving — not visible in
  replay, where extraction is free — is that the second stage now drops the band's
  noise share before it reaches a paid extractor call.
- **The costs are real too.** Escalate-then-drop is a new way to lose a thread: the
  second stage dropped escalated threads holding real changes (extractor accuracy
  90.0% against jev's 91.7%, digests 29 items against 31), which the old
  "escalate = extract anyway" routing would have kept under a gold extractor. And as
  predicted, silo recall stays 11 of 12 — the missed cross-team case was a confident
  first-stage drop the second stage never sees.

Net: option 1 bought a better-calibrated, better-typed gate at a third of
LLM-everywhere cost and flat latency, in exchange for a small recall tax on the band.
The open item is unchanged: align the confident-drop threshold with the 10:1 cost model
(option 2) to go after the last cross-team miss.

## What the third eval round showed (Oct 5)

The first run with A3 and A4 in the table — and the first full-table attempt did not
finish. It hung 71 minutes on a single dead HTTPS connection: 1 second of CPU, one
socket that never changed. The decider backends go through urllib with a 60 s timeout,
but the extractor's OpenAI client had none, so one stalled request in the `llm`
configuration froze a ten-configuration eval.

**Design change:** the extractor client now carries a 120 s timeout
(`EXTRACT_TIMEOUT_S`), and a failed model call raises `ExtractionFailed`, which the
pipeline treats exactly like a malformed response — log the thread ID, skip the
thread, continue; the unsaved signal retries next ingest. One flaky call can no
longer take down a run. The rerun with the fix completed in ~11 minutes.

The A4 comparison, llm against llm-plus-aliases:

| Metric | llm | a4-aliases |
| --- | --- | --- |
| Silo recall | 100.0% (12 of 12) | 100.0% (12 of 12) |
| Digest precision | 95.1% (39 of 41) | 95.3% (41 of 43) |
| Extractor accuracy | 95.0% (114 of 120) | 95.8% (115 of 120) |

Reading it honestly:

- **A4's gain is real but small, and within our own noise rule.** The aliased run
  delivered 43 digest items — exactly core's count, two more than plain llm — and
  every gap here is one or two counts, which the spec says to treat as noise. The
  two rows also come from separate nondeterministic extractor runs, so aliasing is
  confounded with run-to-run variance.
- **The unresolved list came back empty.** Every name the model emitted was either
  canonical or resolved through the table — largely because the extractor prompt
  lists task IDs, so the model rarely free-texts a part name. A4's case for the
  writeup is a zero-cost safety net for loosely-named targets (the four gold alias
  threads re-run under their surface names in tests/test_aliases.py), not a
  percentage swing; llm already sits at 100% silo recall, leaving no headroom.
- **A3 matches core on every membership metric by construction**: replay confidences
  are 1.0 and no inbox overflows the top five (none holds more than three items), so
  the phase ranker can only reorder within a day. The ordering effect was then
  measured directly (Oct 5, later the same day): ranking every real (user, day)
  inbox under both rankers, **A3 changes the order in 2 of 33 digests** — tom on
  2026-03-05 and 2026-03-11, the one user owning stages in both products' processes —
  and neither sits at the EVT gate, since no real inbox mixes items from both phases.
  That measurement forced a demo fix: the day 5 / day 7 pair differs under *any*
  ranker (different days hold different changes), so showing it as A3's effect
  overclaimed. `digest demo` now states that caveat, prints the live 2-of-33 count,
  shows one reordered day under both rankers side by side, and credits the gate flip
  to the constructed-item unit test (`test_evt_gate_passing_flips_the_ranking`).
  On this dataset A3 is a mechanism demonstration, not a measured gain.
- **The decider story reproduced round two within a thread or two**: jev 88.3% gate
  accuracy at $0.0022 and 174 ms; the jev→llm cascade 90.0% with ECE 3.1% before
  scaling at $0.0075 and 184 ms; LLM-everywhere 95.0% at $0.0149 and 3,070 ms. The
  same cross-team case is still lost to a confident first-stage drop (11 of 12), so
  option 2 — aligning the drop threshold with the 10:1 cost model — remains open.

## Pricing became real (Oct 5)

TypeSafe's usage dashboard turned out to report billed spend, not just tokens:
$0.0339 for the week's 720 jev requests (884,983 tokens), and $0.0056 per
120-thread pass (~134k input + ~13k output tokens, six passes over Oct 4–5). That
is exactly one tenth of what the placeholder constants predicted ($0.0560 per
pass), so the constants in `digest/attach/deciders.py` moved from $0.30/$1.20 to
**$0.03 in / $0.12 out per Mtok** — spend-matched rather than guessed. One honest
caveat: the dashboard reports token totals, not an input/output price split, so
the 1:4 ratio is carried over from the placeholder; any split consistent with
$0.0056 per pass fits the data equally well, and every pass has the same token
mix, so more passes cannot disambiguate it.

Costs re-derived from the Oct 5 table: a1-jev $0.0022 → **$0.0002** per digest
(exact — that configuration's spend is pure jev, and all jev spend scales by 0.1);
a1-jev-llm $0.0075 → **~$0.0055** (the jev share drops to noise and the LLM
escalation share, still at placeholder llm rates, dominates). a1-llm is unchanged.
The cascade's cost argument strengthens accordingly: the gate now costs ~1/70th of
LLM-everywhere per digest rather than ~1/7th, and the dollar case for gating at
real Slack volume gets harder to argue against, not easier. The cost cells in
results.md were hand-updated with these two numbers; a keyed `digest eval` rerun
would regenerate them exactly. Historical dollar figures earlier in this file are
left as written — they record what was believed when those decisions were made.

The OpenAI side became real the same day, by a different route. The usage API
reports per-day gpt-6-sol tokens (Oct 4: 920,469 in / 31,247 out; Oct 5:
1,372,701 in / 100,474 out) and the cost API the matching dollars ($2.3195 and
$4.0858). Two days and two unknowns solve exactly: **$2.12 in / $11.63 out per
Mtok**, reproducing both bills to the cent — and landing within ~15% of the
$2.50/$10.00 placeholder, which validates the table's llm dollars rather than
overturning them (a ~6% shift on decider-shaped traffic, inside the noise rule;
rerun `digest eval` to regenerate the cells at the fitted rates). These are
effective rates: the token mix spans cached, cache-write and uncached input, so
the cache discount is folded into the blend, calibrated for eval-shaped traffic.
The headline for the writeup: the entire three-round evaluation program cost
$6.41 of OpenAI spend plus $0.03 of TypeSafe — about **$6.44 end to end**.

## "Measured gain or cut", applied to A3 (Oct 5)

Spec rule 3 says every attachment must show a measured gain in the table or be cut.
Applied honestly, the rule produced one downgrade — recorded here so the reasoning
survives the deadline:

- **A3 — kept, but the claim downgraded (Oct 5, honesty).** Strictly read, rule 3
  would cut A3 too: it shows no gain in the membership table, and measuring its
  ordering effect directly found 2 of 33 real digests reordered. It stays for two
  reasons. The spec's own build order names A1+A2+A3 as the minimum for the
  submission, and the mechanism is real and pinned — the gate flip test, the
  stage-distance scores. The price of keeping it is the claim: writeup, README and
  demo now present A3 as a mechanism demonstration on a dataset too shallow to
  reward it (no inbox exceeds three items), not as a measured win. The original
  day 5 / day 7 framing — which implied the ranker caused a difference the
  calendar caused — was the overclaim this downgrade removes. What would change
  the verdict: deeper inboxes (more than five candidate items a day) and an
  ordering-sensitive metric such as rank-of-gold-item in the eval table.
