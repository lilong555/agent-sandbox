# Local Unprofiled Controller Comparison

## Question

Does the maintained NodeSpread index produce a repeatable end-to-end warm-claim
latency improvement when controller profiling is disabled? Does the effect
exceed the variation between consecutive executions of the same binary?

## Fixed Conditions

- Baseline: `4d39311ba9d89001d79ff1217ed330c7ea3761ec`.
- Candidate: `a87fa000ec96c8e3d9be493da5c6e6e2048b37ad`.
- Reuse the exact registered manager binaries and PCG(0,0) stress executable.
- One dedicated kind cluster: one control plane, two workers, Kubernetes v1.35.8.
- One 128-member pool, all members Ready before each arrival window.
- Cached `node:22-alpine` sandbox image, one-second claim dwell.
- Controller GOMAXPROCS=4, affinity 6,8,10,12, identical worker and refill flags.
- The stress process inherits the same environment in both revisions.
- CPU profile capture disabled, mutex fraction zero, block profile rate zero.
- Loopback Prometheus samples at the arrival boundaries and every five seconds.
- Windows and WSL host telemetry throughout; node conditions before and after.
- Dedicated namespace per run; full namespace deletion while the controller is
  alive; a further 15-second quiet interval before the next run.
- A separate 20-second, 8/s baseline warmup precedes the comparisons.

## Registered Schedule

Pairs run serially. Both members have the same rate, duration, inventory,
traffic seed, and sampling configuration. A means baseline; B means candidate.

| Position | Group | Pair | Target rate | Seconds | Order |
| ---: | --- | ---: | ---: | ---: | --- |
| 1 | plain60 | 4 | 8/s | 60 | AB |
| 2 | plain60 | 5 | 8/s | 60 | BA |
| 3 | noise-baseline | 1 | 8/s | 120 | AA |
| 4 | long8 | 1 | 8/s | 120 | AB |
| 5 | mid4 | 1 | 4/s | 120 | BA |
| 6 | long8 | 2 | 8/s | 120 | BA |
| 7 | long8 | 3 | 8/s | 120 | AB |
| 8 | noise-candidate | 1 | 8/s | 120 | BB |
| 9 | long8 | 4 | 8/s | 120 | BA |
| 10 | mid4 | 2 | 4/s | 120 | AB |
| 11 | long8 | 5 | 8/s | 120 | AB |
| 12 | long8 | 6 | 8/s | 120 | BA |

Total: 24 formal runs plus one warmup. The 60-second controls retain the
previous traffic duration. The six longer pairs are the primary experiment.
The moderate-rate group and identical-binary groups are diagnostic controls.

## Readouts

Primary endpoint: full-arrival-window claim-create to observed Ready p50 in
the six `long8` pairs. Report every paired percentage change, medians and
ranges, the geometric mean paired ratio, a pair-level bootstrap 95% interval
(20,000 resamples, seed zero), and a two-sided exact paired sign-flip statistic.
Six pairs form six replicates; individual claims share their run's environment.
The intervals describe this fixed workload and local environment.

Secondary: p90/p99, Create acknowledgement, Ready minus Create acknowledgement,
controller CPU seconds and cumulative allocated bytes across the sampled
arrival window, sampled Ready inventory, warm/cold counters, created/Ready/
deleted counts, and host resource pressure. Report exact resource-window
timestamps and generated arrival counts, separately from requested rates.

Same-binary controls report second/first ratios as descriptive noise checks.
Keep all groups and earlier profiled measurements separately identifiable.
Retain all samples, including contrary effects and unsuccessful runs.

## Validity

Require matching binary hashes, source revisions, configuration, initial
Ready inventory, and per-pair generated arrivals. Confirm every claim's
creation, readiness, deletion, and error status from raw lifecycle records.
Reconcile controller adoption counters with the workload count.

Record sampled inventory dips, cold launches, node pressure, missed arrival
windows, telemetry errors, paging, and unrelated host activity explicitly.
Unexpected failures stop the suite for diagnosis. Preserve the failed run
under its original name; any rerun receives a new name and explicit grouping.
Host pressure and inventory observations accompany the result rather than
silently selecting or removing unfavorable pairs.

## Decision

A stable primary latency improvement, supported by resource and tail behavior
and larger than same-binary variation, supports renewed design discussion.
An effect that reverses across pairs or overlaps environmental variation
supports keeping the current simple queue for this workload and using the
profile to identify a more consequential optimization target.

The maintainer receives an English evidence summary with reproducible artifacts
and all observed tradeoffs. The reviewer's hold remains under maintainer control.

## Execution

```bash
python3 -m unittest test_repeat_suite test_run_unprofiled test_summarize -v
python3 repeat_suite.py --extended --output results-unprofiled
python3 summarize_unprofiled.py results-unprofiled
```

## Tasks

- [x] Register comparisons, controls, and analysis before collecting results.
- [x] Isolate controller environment settings from the stress process.
- [x] Validate the runtime and primary Python analyzer tests.
- [x] Restore the dedicated cluster and complete the independent warmup.
- [x] Complete the 24 registered formal runs.
- [ ] Audit lifecycle, inventory, sampling, and host telemetry.
- [ ] Publish evidence and communicate the findings to the maintainer.
- [ ] Remove dedicated resources and complete the experiment checklist.
