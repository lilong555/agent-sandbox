# NodeSpread Controller Comparison

## Assessment

**Recommendation: keep the maintainer's hold on the NodeSpread index in
[PR #2179](https://github.com/kubernetes-sigs/agent-sandbox/pull/2179).**
The registered unprofiled primary comparison shows a **+1.111%** change in
claim-create-to-Ready p50 for the candidate, with a pair-bootstrap 95% interval
of **[-8.461%, +9.433%]** and a two-sided exact sign-flip **p = 0.875**.
The effect is the geometric mean of six candidate/baseline run-level ratios.
Four pairs have higher candidate p50 and two have lower candidate p50.
Tail latency and resource changes vary across pairs, and the same-binary
controls also show substantial variation.

All **24 unprofiled formal runs** completed: **18,128 claims** were created,
became Ready, and completed deletion, with **zero recorded failures**.
The comparison includes **53 cold launches**, including six in a candidate
primary run. These observations remain in the registered analysis.
The complete summary reports `status: complete`, `validation_status: valid`,
and `errors: []`. Lifecycle and protocol validation coexist with the host
pressure and inventory observations described below.

The earlier **12 profiled runs**, covering **3,300 additional formal claims**,
are a separate descriptive experiment. Their favorable 8/s p50 signal did
not establish a consistent CPU or allocation saving. Taken together, the
measurements provide insufficient evidence of a repeatable end-to-end benefit
to justify lifting the hold in this workload. Both suites were measured
locally on WSL2; the recommendation concerns the existing candidate revision.

## Conditions and Registration

| Condition | Value |
| --- | --- |
| Baseline (A) | `4d39311ba9d89001d79ff1217ed330c7ea3761ec` |
| Candidate (B) | `a87fa000ec96c8e3d9be493da5c6e6e2048b37ad` |
| Toolchain | Go 1.27.3; matched dependency versions and compiler configuration |
| Runtime | WSL2, Docker Desktop, kind v0.33.0, Kubernetes v1.35.8 |
| Cluster | One control plane and two workers; one 128-member warm pool |
| Sandbox | Cached `node:22-alpine` image; one-second claim dwell |
| Controller | GOMAXPROCS=4; CPU affinity 6,8,10,12; identical worker/refill flags |
| Workload | Open-loop Poisson arrivals; shared stress executable using PCG(0,0) |
| Between runs | Dedicated namespace deletion completes, followed by 15 quiet seconds |
| Unprofiled observation | CPU profiling disabled; mutex/block sampling disabled; metrics every five seconds and at traffic boundaries |

The [unprofiled protocol](UNPROFILED_PROTOCOL.md) registers the order before
collection. `long8` comprises six pairs at 8 claims/s for 120 seconds, with
orders AB, BA, AB, BA, AB, BA. `plain60` comprises two 8/s, 60-second pairs
(4: AB; 5: BA). `mid4` comprises two 4/s, 120-second pairs (1: BA; 2: AB).
One AA and one BB pair at 8/s for 120 seconds measure same-binary variation.
The groups are interleaved in the
[execution manifest](results-unprofiled/manifest.json), and remain separate
in all comparisons.

Actual seeded arrivals are 907 per 8/s, 120-second run, 452 per 8/s,
60-second run, and 452 per 4/s, 120-second run. Target rate multiplied by
duration is a nominal expectation; the seeded Poisson schedule supplies the
actual count. A separate 20-second warmup completed 137 claims and is excluded
from formal totals and inference. The unprofiled session spans
2026-11-05 15:55:08 to 17:06:32 UTC, including setup, warmup, and cleanup.

## Primary Result: long8

The endpoint is the nearest-rank p50 of each run's exported Go
`sandboxReadyMs`, covering all requests and their eventual readiness,
including readiness after arrivals finish. Negative changes indicate a lower
candidate value. Each pair contains 907 claims in each revision.

| Pair | Order | A p50 (ms) | B p50 (ms) | p50 change | A p99 (ms) | B p99 (ms) | p99 change |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | AB | 68.765 | 78.773 | +14.55% | 232.369 | 663.561 | +185.56% |
| 2 | BA | 66.995 | 64.318 | -4.00% | 712.519 | 279.156 | -60.82% |
| 3 | AB | 106.556 | 86.130 | -19.17% | 438.660 | 1460.058 | +232.85% |
| 4 | BA | 72.414 | 79.883 | +10.31% | 280.555 | 2036.695 | +625.95% |
| 5 | AB | 67.503 | 72.276 | +7.07% | 343.882 | 1007.357 | +192.94% |
| 6 | BA | 65.642 | 66.801 | +1.77% | 269.211 | 254.682 | -5.40% |

Pair 4 includes six `cold/not_ready` launches in the candidate. Its 907
requests, tail latencies, and resource readings remain part of the primary
comparison. Source statistics and all paired endpoints are in
[summary-unprofiled.json](results-unprofiled/summary-unprofiled.json).

Controller resources below are start/end Prometheus counter differences.
Allocated GB means cumulative allocated bytes divided by 1,000,000,000;
it measures allocation activity, separate from retained heap.

| Pair | CPU seconds A -> B | CPU change | Allocated GB A -> B | Allocation change |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 43.07 -> 42.59 | -1.11% | 8.243 -> 7.585 | -7.99% |
| 2 | 41.82 -> 41.89 | +0.17% | 8.237 -> 8.253 | +0.18% |
| 3 | 44.47 -> 40.87 | -8.10% | 7.142 -> 7.145 | +0.05% |
| 4 | 44.47 -> 39.49 | -11.20% | 8.006 -> 7.459 | -6.83% |
| 5 | 42.83 -> 41.56 | -2.97% | 8.208 -> 7.796 | -5.02% |
| 6 | 42.63 -> 42.82 | +0.45% | 8.320 -> 8.238 | -0.98% |

| Endpoint | A median [min, max] across six runs | B median [min, max] across six runs |
| --- | ---: | ---: |
| p50 (ms) | 68.134 [65.642, 106.556] | 75.525 [64.318, 86.130] |
| p99 (ms) | 312.219 [232.369, 712.519] | 835.459 [254.682, 2036.695] |
| Create acknowledgement p50 (ms) | 10.618 [10.173, 16.023] | 11.575 [9.644, 13.786] |
| Median Ready-minus-ack (ms) | 55.394 [53.637, 88.229] | 62.343 [52.838, 70.656] |
| Controller CPU seconds | 42.950 [41.820, 44.470] | 41.725 [39.490, 42.820] |
| Allocated GB | 8.223 [7.142, 8.320] | 7.690 [7.145, 8.253] |

These revision medians summarize runs independently. The primary +1.111%
estimate instead gives equal weight to each of the six paired log ratios.
The four CPU reductions and four allocation reductions are descriptive;
their direction reverses in the other two pairs and the same-binary controls
provide an important comparison for their magnitude.

### Statistical Interpretation

The independent replication unit is a run pair, giving **n = 6**.
The sign-flip calculation enumerates all 64 sign assignments to the paired
log ratios. Its inferential interpretation assumes independent pairs with
sign-exchangeable differences under the null. Execution order was fixed and
alternated, rather than randomized. Serial host drift and order effects can
violate those assumptions; the exact enumeration does not provide a
randomized-experiment guarantee.

The percentile bootstrap uses 20,000 resamples of six whole pairs with seed
zero. With six pairs, its interval is sensitive to the observed pairs and
has limited ability to represent rare environmental states. Individual claims
are not bootstrap replicates. The interval leaves both a modest improvement
and a modest regression compatible with these observations. Only `long8`
p50 is inferential; p99, resources, auxiliary timings, and other groups are
descriptive.

The registered primary includes every complete pair and the candidate's six
cold launches. Any inventory-healthy or warm-only subset is a post-hoc
description and must retain that label alongside, rather than replace, this
primary result.

## Same-Binary Controls

AA and BB each contain two executions of the same executable at 8/s for
120 seconds. Changes below are **second/first**, with one pair per binary.

| Control | p50 ms, first -> second | p50 change | p99 ms, first -> second | p99 change | CPU seconds, first -> second | Allocated GB, first -> second |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| AA | 109.458 -> 96.032 | -12.27% | 308.770 -> 1736.460 | +462.38% | 47.19 -> 41.12 | 6.985 -> 7.009 |
| BB | 67.736 -> 66.719 | -1.50% | 265.162 -> 396.965 | +49.71% | 43.21 -> 42.92 | 8.183 -> 8.201 |

AA CPU changes by -12.86% and allocation by +0.35%; BB changes by -0.67%
and +0.22%, respectively. These controls demonstrate within-binary variation
in this session. One AA and one BB pair provide descriptive checks, with
limited coverage of the full noise distribution.

## Other Unprofiled Groups

### plain60: 8/s for 60 Seconds

Each run contains 452 claims. These two pairs preserve the shorter workload
duration and are separate from `long8`.

| Pair | Order | p50 ms A -> B | p50 change | p99 ms A -> B | p99 change | CPU seconds A -> B | Allocated GB A -> B |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 4 | AB | 86.740 -> 60.842 | -29.86% | 211.369 -> 195.251 | -7.63% | 22.07 -> 21.06 | 3.941 -> 4.187 |
| 5 | BA | 193.224 -> 62.342 | -67.74% | 17026.586 -> 275.261 | -98.38% | 24.14 -> 21.71 | 4.096 -> 4.148 |

Median run p50 is 139.982 ms for A (range 86.740-193.224) and 61.592 ms
for B (60.842-62.342). Pair 5's baseline has 47 cold launches and sampled
Ready inventory reaching zero. Its large latency difference describes a
mixed cold-start/depletion scenario. Both complete pairs remain reported.

### mid4: 4/s for 120 Seconds

Each run contains 452 claims; all launches are `warm/ready`.

| Pair | Order | p50 ms A -> B | p50 change | p99 ms A -> B | p99 change | CPU seconds A -> B | Allocated GB A -> B |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | BA | 31.946 -> 32.176 | +0.72% | 103.349 -> 1256.146 | +1115.44% | 20.09 -> 19.24 | 5.217 -> 4.906 |
| 2 | AB | 33.202 -> 31.452 | -5.27% | 1176.419 -> 74.164 | -93.70% | 20.75 -> 20.12 | 5.023 -> 5.347 |

Median run p50 is 32.574 ms for A (31.946-33.202) and 31.814 ms for B
(31.452-32.176). The tail reversals in opposite directions remain visible
despite similar central latencies.

## Request-Level Auxiliary Timing

For each request, Ready-minus-ack is `sandboxReadyMs - createAckMs`.
The table reports the median of those individual differences, not the
difference between two independently computed latency quantiles. For an even
request count, the median averages the middle two observations. The timings
combine client acknowledgement and observed readiness, so controller, API,
watch delivery, and host scheduling all matter to their interpretation.

| Group / pair | Create-ack p50 ms A -> B | Median Ready-minus-ack ms A -> B | Difference B - A (ms) |
| --- | ---: | ---: | ---: |
| long8 / 1 | 10.833 -> 12.171 | 55.069 -> 65.382 | +10.312 |
| long8 / 2 | 10.173 -> 9.644 | 55.327 -> 52.838 | -2.489 |
| long8 / 3 | 16.023 -> 13.786 | 88.229 -> 70.656 | -17.573 |
| long8 / 4 | 11.514 -> 12.436 | 59.002 -> 65.152 | +6.150 |
| long8 / 5 | 10.403 -> 10.978 | 55.462 -> 59.533 | +4.071 |
| long8 / 6 | 10.392 -> 9.823 | 53.637 -> 54.865 | +1.228 |
| plain60 / 4 | 12.467 -> 9.384 | 69.029 -> 49.265 | -19.763 |
| plain60 / 5 | 23.235 -> 9.566 | 160.523 -> 51.642 | -108.881 |
| mid4 / 1 | 4.958 -> 5.000 | 26.826 -> 27.287 | +0.461 |
| mid4 / 2 | 5.271 -> 4.855 | 27.765 -> 26.469 | -1.296 |

For AA, median Ready-minus-ack changes from 89.271 to 78.673 ms
(-10.598 ms); for BB, it changes from 55.706 to 53.644 ms (-2.062 ms).
Those directions again mean second minus first.

## Lifecycle and Inventory

All 18,128 formal requests have complete creation, Ready, and deletion
records. Final controller counters reconcile to **18,075 `warm/ready`**
and **53 `cold/not_ready`** launches, with zero `warm/not_ready` adoptions.
Every formal run's start scrape records **128 Ready pool members**.
The `pod_condition` label records Sandbox readiness at adoption completion.

| Sample | Cold / created | Sampled Ready min / max / start / end | Sampled zero inventory | p99 (ms) |
| --- | ---: | --- | --- | ---: |
| `quiet-plain-p5-baseline` | 47 / 452 | 0 / 128 / 128 / 0 | Yes, one scrape | 17026.586 |
| `local-long8-p4-candidate` | 6 / 907 | 1 / 128 / 128 / 109 | No zero sampled | 2036.695 |

The remaining 22 formal runs have zero cold launches. Sampled minimum Ready
inventory ranges down to 109 in `mid4`, 37 in AA, and 78 in BB.
Five-second inventory sampling records discrete states; a minimum of one
leaves the between-scrape trajectory unresolved.

The [public inventory diagnostic](results-unprofiled/inventory-diagnostic.json)
retains 47 selected cold-event snapshots for `quiet-plain-p5-baseline`.
Their observed Ready count spans 0-2 and pending count spans 107-128; 25
snapshots have zero Ready. Its public verifier recounts all 47 snapshots and
two Pod timing rows. These are visible object/watch observations, with their
own observation timing. They support a depleted-ready-inventory diagnosis;
the cause of each individual fallback remains unresolved.

## Observation Windows and Pacing

Unprofiled controller CPU and cumulative allocation use the difference
between traffic-boundary scrapes. Window duration uses monotonic scrape
midpoints. Actual durations are 59.918-59.935 seconds for `plain60`,
119.835-119.836 for `mid4`, and 119.926-119.933 for `long8`;
the longest AA/BB window is 119.937 seconds. Maximum spacing between
telemetry scrape starts is 5.043 seconds; the longest scrape is 52.530 ms.
Exact timestamps and scrape uncertainty bounds remain in the summary.

Request latency continues through eventual Ready after the arrival boundary.
Across formal runs, 83 requests became Ready after that boundary, including
53 in `quiet-plain-p5-baseline`. Resource counters and host statistics cover
their defined traffic windows; drain latency therefore extends beyond those
windows for some requests.

The pacing audit matches every request ID to the exact PCG(0,0) schedule.
It aligns each run's first arrival and computes relative timing errors.
The reported envelope is the largest minus the smallest relative error
within a run. The schedule-origin timestamp is unrecorded, so this measures
relative pacing jitter rather than absolute launch delay.

| Group | Maximum envelope (ms) | Sample |
| --- | ---: | --- |
| long8 | 51.339574 | `local-long8-p4-baseline` |
| plain60 | 15.435085 | `quiet-plain-p5-baseline` |
| mid4 | 9.024717 | `local-mid4-p2-baseline` |
| AA | 15.635216 | `local-noise-baseline-s2` |
| BB | 72.604433 | `local-noise-candidate-s2` |
| Earlier profiled suite | 6.580691 | `quiet-r8-p1-baseline` |

The largest positive unprofiled relative offset is **62.828707 ms** in BB's
second run. The most negative is **-33.951131 ms** in `long8` pair 4's
baseline. Up to seven adjacent scheduled IDs have out-of-order observed
creation timestamps within one unprofiled run. Counts and identities still
match the complete seeded schedule in every formal run. The shared seed
provides equal intended arrivals; these measured deviations document
execution-time variation.

See the [unprofiled arrival audit](results-unprofiled/arrival-audit.json)
and [profiled arrival audit](results/arrival-audit.json).

## Host Resource Pressure

Per-run host statistics align sample UTC timestamps with the observed
traffic-trigger-to-`arrivals_finished` interval. They retain CPU, available
memory, paging, swap, and Linux pressure measurements independently of
claim success. Memory values below are MiB, corresponding to the exported
`available_mb` fields.

| Unprofiled group | Windows run CPU medians (%) | Windows peak CPU (%) | Minimum available MiB | Peak page input (pages/s) |
| --- | ---: | ---: | ---: | ---: |
| long8 | 63-81 | 100 | 413 | 5320 |
| plain60 | 68-85 | 96 | 509 | 4944 |
| mid4 | 34-39 | 90 | 816 | 4370 |
| AA | 72-78 | 97 | 579 | 2213 |
| BB | 66-71 | 94 | 749 | 4696 |

| Unprofiled group | WSL run CPU medians (%) | Minimum available MiB | Maximum CPU / memory / I/O PSI `some avg10` (%) |
| --- | ---: | ---: | --- |
| long8 | 51.42-59.95 | 1862.27 | 58.71 / 3.84 / 18.00 |
| plain60 | 50.75-58.44 | 2020.74 | 47.60 / 1.84 / 12.22 |
| mid4 | 24.47-28.77 | 2221.15 | 16.78 / 3.86 / 9.09 |
| AA | 56.87-67.67 | 2119.95 | 54.03 / 2.33 / 13.19 |
| BB | 52.93-53.24 | 2124.90 | 37.99 / 0.63 / 16.45 |

The full session logs also include warmup, setup, drain, and quiet intervals.
Across their 763 Windows samples, available memory reaches **320 MiB** and
page input reaches **332,985 pages/s**. These full-session extrema are
distinct from the formal traffic-window extrema above. Across 856 WSL
samples, peak CPU is 77.52%, I/O wait reaches 16.92%, and reported steal
is zero. Formal run windows include up to 3,091 swap-in pages and 17,815
swap-out pages in a run. Within formal traffic windows, maximum host sample
gaps are 6.583 seconds on Windows and 5.047 seconds on WSL.

For the 47-cold baseline run, Windows CPU median is 85%, minimum available
memory is 509 MiB, and peak page input is 4,063 pages/s. For the six-cold
candidate run, the corresponding values are 70%, 810 MiB, and 2,029 pages/s.
These readings document resource pressure and temporal variability.
They do not identify paging, another process, or the queue implementation as
the cause of a particular latency excursion or cold launch.

## Controller REST Requests

The structured Prometheus analysis preserves HTTP code and method while
aggregating host/URL labels out of its public result. Counters are monotonic
across the selected snapshots. The scope is the controller REST client;
stress-client, kubelet, and API-server-owned requests were not collected by
this metric.

| HTTP code | Sum of formal traffic-window differences | Sum of process-start-through-final-scrape totals |
| --- | ---: | ---: |
| 200 | 231737 | 247022 |
| 201 | 90100 | 103193 |
| 404 | 38 | 39 |
| 409 | 20153 | 22253 |
| Total | 342028 | 372507 |

There are no recorded 429, 5xx, or `<error>` requests in these counters.
HTTP 409 includes optimistic conflicts handled inside the controller;
the lifecycle result remains zero final claim failures. The two columns
have different windows: process totals include setup and subsequent
drain/cleanup up to the final scrape. They are descriptive accounting across
the suite, with per-run and per-method values retained in
[rest-client-requests.json](results-unprofiled/rest-client-requests.json).

`quiet-plain-p5-baseline` has 558 HTTP 409 requests in its traffic window
and 658 through the final scrape. Its final three periodic-to-periodic
intervals contain 39, 85, and 87 HTTP 409 requests, alongside successful
200/201 responses. The six-cold candidate run has 1,088 HTTP 409 and two
HTTP 404 requests in its window. These aggregates localize REST activity;
they do not resolve the cause of individual cold launches.

## Earlier Profiled Experiment

The [profiled protocol](PROTOCOL.md) and [summary](results/summary.json)
cover three pairs at each of 2/s and 8/s, all with 60 seconds of traffic.
Each 2/s run creates 98 claims; each 8/s run creates 452.
All **3,300 formal claims** become Ready and complete deletion with zero
failures and zero cold launches. A separate 137-claim warmup is excluded.

| Rate | Pair | Order | p50 ms A -> B | p99 ms A -> B |
| --- | ---: | --- | ---: | ---: |
| 2/s | 1 | AB | 25.881 -> 25.127 | 101.987 -> 58.614 |
| 2/s | 2 | BA | 26.595 -> 26.790 | 68.361 -> 110.136 |
| 2/s | 3 | AB | 26.307 -> 25.646 | 54.566 -> 41.452 |
| 8/s | 1 | BA | 68.163 -> 53.087 | 264.015 -> 224.466 |
| 8/s | 2 | AB | 65.114 -> 52.798 | 1081.526 -> 124.372 |
| 8/s | 3 | BA | 58.590 -> 56.304 | 144.924 -> 229.770 |

The 8/s paired p50 changes are -22.12%, -18.91%, and -3.90%; the last pair
also has a +58.54% p99 change. At 2/s the p50 distributions overlap.
Profile capture begins ten seconds after the sustained-arrival trigger,
with CPU, delta mutex, and delta allocation requests covering a matched
30-second window. All 36 profiles complete within the observed workload;
recorded profile durations span 30.000-30.032 seconds.

Full-run latency and profile measurements refer to different cohorts and
windows. Each CPU window includes 49 arrivals at 2/s or 232 at 8/s, and the
summary retains their separate latency statistics. For example, pair 3 at
8/s has a profile-arrival-cohort p50 of 58.859 -> 62.962 ms while its
full-run p50 is 58.590 -> 56.304 ms.

| Rate | Median controller CPU seconds A -> B | Median cumulative allocated GB A -> B |
| --- | ---: | ---: |
| 2/s | 2.06 -> 2.02 | 0.641 -> 0.646 |
| 8/s | 10.43 -> 10.64 | 2.000 -> 2.279 |

These resources use counter differences around the profile window.
At 8/s the paired CPU changes are +2.68%, +13.07%, and -5.72%;
allocation changes are +21.04%, +13.94%, and -7.87%.
The profiled measurements show no consistent CPU or allocation saving.

At **8/s**, inclusive queue-stack CPU attribution spans **0-0.118%**
across both revisions. Across the separate 2/s profiles, one candidate
records a 10 ms queue-attributed CPU sample, or 0.578%; the other 2/s
profiles record zero. Queue-attributed mutex delay is at most **1.636 ms**
per 30-second capture; the maximum is 0.442% of that profile's total mutex
delay. Zero attribution describes sampling resolution, not absence of work.
Attribution sums complete samples containing a
`sigs.k8s.io/agent-sandbox/extensions/controllers/queue.` frame once each,
with units preserved; it is not a sum of displayed top nodes.
See [all profile sample-type totals](results/profiles.json).

During the full 60-second traffic windows of profiled 8/s runs, Windows CPU
medians span 56-79% and peak at 94%; WSL medians span 47.59-54.92%.
Across the profiled formal runs,
minimum available memory is 1,672 MiB on Windows and 2,372 MiB on WSL.
These environmental readings are retained separately from the later,
more memory-constrained unprofiled session. The two modes and durations
are not pooled into one effect estimate.

## Independent Linux Validation

A public-fork GitHub Actions run is a reasonable next environment check.
For public repositories, `ubuntu-24.04` x64 uses a fresh VM with 4 vCPUs,
16 GB RAM, and 14 GB SSD according to the
[GitHub-hosted runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners#standard-github-hosted-runners-for-public-repositories).
GitHub-hosted jobs have a
[six-hour execution limit](https://docs.github.com/en/actions/reference/limits).
These specifications support a validation design, subject to calibration
of the whole kind cluster's resource demand.

The [first Linux round's protocol](CI_PROTOCOL.md) specifies one job and one
VM in the public `lilong555/agent-sandbox` fork. Both fixed controller revisions
and the shared seeded workload are built, and images are cached, before
measurement. Baseline-only calibration tests 2/4/8 claims/s for 30 seconds
per level. A passing level requires complete error-free lifecycles, 128
initial Ready members, sampled Ready inventory of at least 64, zero cold
launches, and median Linux CPU active time at most 85%. Failure at 2/s stops
the round; otherwise the highest passing rate selects the formal workload.

The formal sequence is **A, B, B, A**, with 60 seconds of arrivals per run
in the same job and cluster. The first two runs form the AB pair and the
last two form the BA pair. Their geometric mean candidate/baseline p50 ratio
is descriptive, with two pairs and no confidence interval or significance
test. Calibration remains separate, and all complete formal runs retain
their cold-launch and inventory observations. Subsequent experiments can
add independent AA/BB controls and repeat complete paired blocks on
additional fresh VMs to assess within-binary and between-environment
variation.

The controller, three kind nodes, workload generator, and sandbox containers
share four vCPUs and the disk budget. Choose affinity from the runner's
actual CPU set, register Linux host telemetry in place of Windows monitoring,
and validate namespace cleanup and inventory before every run. Keep
compilation, image downloads, and profiling outside the primary unprofiled
measurement windows. Preserve complete artifacts even when capacity
pressure or lifecycle failures occur. Such a run would provide separate
environment evidence; the numerical results above are from the local suites.

## Public Evidence and Offline Reproduction

The [evidence-root README](../README.md#recompute-the-published-results)
contains the complete offline commands, pinned candidate checkout, cache
preparation, and verifier requirements. The verifier checks `SHA256SUMS`,
reconstructs raw records in a new directory, builds the three analysis
helpers using cached dependencies, and compares the regenerated JSON
statistics. Inventory verification uses the public selected-event records.

| Evidence | Unprofiled suite | Profiled suite |
| --- | --- | --- |
| Run and paired statistics | [JSON](results-unprofiled/summary-unprofiled.json), [CSV](results-unprofiled/summary-unprofiled.csv) | [JSON](results/summary.json), [CSV](results/summary.csv) |
| Execution manifest | [Manifest](results-unprofiled/manifest.json) | [Manifest](results/manifest.json) |
| Arrival pacing | [Audit](results-unprofiled/arrival-audit.json) | [Audit](results/arrival-audit.json) |
| Windows telemetry | [JSONL.gz](results-unprofiled/windows-load.jsonl.gz) | [JSONL.gz](results/windows-load.jsonl.gz) |
| WSL telemetry | [JSONL.gz](results-unprofiled/linux-load.jsonl.gz) | [JSONL.gz](results/linux-load.jsonl.gz) |

Individual runs live under `../runs/<sample>/` in the public tree.
For example, `local-long8-p4-candidate` provides its
[configuration](../runs/local-long8-p4-candidate/config.json),
[lifecycle records](../runs/local-long8-p4-candidate/workload/sandboxes.jsonl.gz),
[telemetry timing](../runs/local-long8-p4-candidate/telemetry/run.json),
[start metrics](../runs/local-long8-p4-candidate/telemetry/metrics-0000.prom.gz),
and [final metrics](../runs/local-long8-p4-candidate/metrics-final.prom.gz).
All intermediate telemetry artifacts use the same `.prom.gz` layout.
Profiled runs retain raw binary
[CPU](../runs/quiet-r8-p1-baseline/profile/cpu.pprof),
[mutex](../runs/quiet-r8-p1-baseline/profile/mutex.pprof), and
[allocation](../runs/quiet-r8-p1-baseline/profile/allocs.pprof) captures.

The [REST summary](results-unprofiled/rest-client-requests.json),
[inventory diagnostic](results-unprofiled/inventory-diagnostic.json),
and [parsed profiled metrics](results/metrics.json) provide additional
structured evidence. Raw metrics, claims, and anonymized host JSONL are
gzip-compressed; run/configuration JSON and pprof bytes retain their
published formats. Public host records retain numeric process CPU values
while omitting process identities. Disclosure policy and checksums are
defined at the evidence root.
