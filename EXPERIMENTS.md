# Experiment log

Three eval rounds, what each showed, and what changed because of it. Metric
definitions and the full table are in results.md (regenerate with `digest eval`).

Terms:

- **Gate accuracy**: how often the decider's `changes_state` answer agrees with
  the gold label for whether a thread changes state.
- **Silo recall**: how many of the 12 planted cross-team changes reach the
  affected person's digest.
- **ECE** (calibration error): the gap between stated confidence and actual hit
  rate. 0% means that when the decider says 80%, it is right 80% of the time.
- **Unsure band**: threads the decider scores between 0.2 and 0.8. results.md
  reports the share of these as "escalation rate".

## Setup

A1 puts a classifier in front of the extractor: `changes_state` at or above 0.8
goes to the extractor, at or below 0.2 is dropped, and the unsure band went to the
extractor anyway, just logged. A2 fits temperature scaling per backend on a
held-out third of the gold labels (reliability plots under `calibration/`) and sets
the digest section thresholds from cost ratios: 1/11 for "needs you", 1/4 for
"affects you", 1/2 for FYI.

## Round 1

| Observation | jev | LLM decider |
| --- | --- | --- |
| Gate accuracy (changes_state) | 87.5% | 96.7% |
| Silo recall through the cascade | 11 of 12 | 12 of 12 |
| ECE before scaling | 9.4% | 1.7% |
| Share in the unsure band | 24.2% | 3.3% |
| Median latency per thread | 178 ms | 2,719 ms |
| Cost per digest | $0.0022 | $0.0146 |

What it showed:

1. Routing only uses `changes_state`. The other answers aren't wired in, and the
   extractor re-derives the change type anyway.
2. jev is a cheap, decent gate (87.5% at 178 ms), but it puts a quarter of all
   threads in the unsure band, and each of those paid for a full extraction.
3. The fixed 0.2 drop threshold is inconsistent with A2's own cost model, which
   prices a "needs you" miss at 10:1 and so implies dropping only below about
   0.09. jev's one missed cross-team case was dropped below 0.2, and a drop at
   ingest can't be recovered later.
4. Calibration wasn't the problem, since jev (9.4% ECE) and the LLM decider
   (1.7%) were already close. The more promising fix is the drop threshold itself.

## Second-stage escalation

Three options were considered: re-decide the unsure band with the LLM decider
(estimated around $0.006 per digest, against $0.0146 for running the LLM on
everything); derive the drop threshold from the cost ratio, which is the only fix
for the missed case; or drop the gate entirely, which works fine at 120 threads
but defeats the purpose at real volume. Training a custom classifier on 120
threads was rejected.

The first option was built. `Cascade` takes an optional second-stage decider that
re-decides the unsure band; it either drops the thread or sends it to the
extractor, and that answer is final. Confident first-stage answers never pay for
the second call, and if the second call fails, the thread goes to the extractor.
This is exposed as `digest ingest --escalate-to llm` and the `a1-jev-llm`
configuration. One known limit: escalation only touches the unsure band, so it
can't rescue a thread the first stage confidently dropped. jev's missed case stays
missed, and the threshold fix is still open.

## Round 2

Same live run for all three columns. The backends are nondeterministic, so numbers
drift a thread or two between runs; single-count gaps are noise.

| Metric | a1-jev | a1-jev-llm | a1-llm |
| --- | --- | --- | --- |
| Silo recall | 91.7% (11 of 12) | 91.7% (11 of 12) | 100.0% (12 of 12) |
| Digest precision | 100.0% (31 of 31) | 100.0% (29 of 29) | 100.0% (39 of 39) |
| Gate accuracy (changes_state) | 88.3% | 89.2% | 95.8% |
| Change type accuracy | 42.9% | 62.9% | 88.6% |
| Calibration error (before → after) | 9.7% → 12.8% | 5.1% → 5.4% | 2.5% → 3.2% |
| Share in the unsure band | 24.2% | 24.2% | 2.5% |
| Median decider latency | 175 ms | 193 ms | 2,804 ms |
| Cost per digest | $0.0022 | $0.0074 | $0.0147 |

- The band now carries LLM answers, so the hybrid's ECE improved to 5.1% from
  jev's 9.7% and change-type accuracy rose twenty points. Latency barely moved,
  since only the band pays the 2.8-second call.
- Cost landed at $0.0074 against the estimate of around $0.006, about half of
  what the all-LLM decider costs.
- There is a cost to the second opinion: it sometimes drops an escalated thread
  that holds a real change (29 digest items against 31), and silo recall stays at
  11 of 12, as predicted.

## Round 3

The first full run hung for 71 minutes on one dead connection. The decider calls
had a 60-second timeout, but the extractor's client had none. The fix was a
120-second timeout on the extractor's client, plus treating a failed call like a
malformed response: log it, skip the thread, and retry on the next ingest. The
rerun took 11 minutes.

A4, the live extractor with and without alias resolution:

| Metric | llm | a4-aliases |
| --- | --- | --- |
| Silo recall | 100.0% (12 of 12) | 100.0% (12 of 12) |
| Digest precision | 95.1% (39 of 41) | 95.3% (41 of 43) |
| Extractor accuracy | 95.0% (114 of 120) | 95.8% (115 of 120) |

- A4's gain is one or two counts, inside the noise rule, across separate
  nondeterministic runs. It is better understood as a safety net for loosely
  named parts; the four gold alias threads pass under their surface names in
  tests/test_aliases.py.
- The unresolved-names list came back empty. The extractor prompt lists task IDs,
  so the model rarely invents a free-text name.
- A3 can only reorder on this dataset, since replay confidences are 1.0 and no
  inbox holds more than three items. Ranking every real inbox both ways,
  `--phase` changes the order in 2 of 33 digests, both belonging to Tom, the one
  user who owns stages in both products. No real inbox mixes items from both
  sides of a gate, so the gate flip is shown by a unit test with constructed
  items, and the demo compares the same day under both rankers instead.
- The decider numbers reproduced round 2 within a thread or two.

## Pricing

Both backends' prices started as estimates and are now derived from billed spend.

jev: TypeSafe's dashboard reports billed spend, which came to $0.0056 per
120-thread pass, exactly one tenth of the estimate, so the constants moved to
$0.03 in / $0.12 out per Mtok. The dashboard only gives token totals, so the 1:4
input-to-output ratio is assumed.

llm (gpt-6-sol): the usage and cost APIs give two billed days of tokens and
dollars, which is two equations in two unknowns. Solving them gives $2.12 in /
$11.63 out per Mtok, which reproduces both bills to the cent and lands within
about 15% of the earlier estimate. These are effective rates, with the cache
discount blended in.

With the measured rates, a1-jev comes to $0.0002 per digest, a1-jev-llm to about
$0.0055, and a1-llm is unchanged. The gate ends up costing about 1/70th of the
all-LLM decider per digest, and the whole three-round evaluation came to about
$6.44. Earlier sections keep their original figures, since those are what was
known at the time.
