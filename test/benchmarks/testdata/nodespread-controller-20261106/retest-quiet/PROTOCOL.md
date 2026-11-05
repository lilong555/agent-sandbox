# NodeSpread Controller Repeated Comparison

## Hypothesis

With low unrelated host activity, the node index reduces warm-claim latency or
controller resource consumption consistently across repeated, order-alternated
runs.

## Fixed Conditions

- Baseline: `4d39311ba9d89001d79ff1217ed330c7ea3761ec`.
- Candidate: `a87fa000ec96c8e3d9be493da5c6e6e2048b37ad`.
- Reuse the existing controller binaries and seed-zero stress executable.
- Kubernetes v1.35.8, one control plane and two workers, one 128-entry pool.
- Identical cached `node:22-alpine` image and controller flags.
- Controller: GOMAXPROCS=4, CPU affinity 6,8,10,12.
- Open-loop Poisson arrivals, PCG(0,0), 60 seconds, 1-second claim dwell.
- Arrival targets: 2 and 8 claims/second; three pairs per target.
- Each pair uses opposite implementations; starting implementation alternates.
- Profile a 30-second interval beginning approximately 10 seconds after arrivals.
- Each run creates its own namespace and starts with all pool members Ready.
- A 20-second warmup precedes the registered comparisons and is reported
  separately.

## Registered Order

| Pair | Target | First | Second |
| --- | ---: | --- | --- |
| 1 | 2/s | Baseline | Candidate |
| 1 | 8/s | Candidate | Baseline |
| 2 | 8/s | Baseline | Candidate |
| 2 | 2/s | Candidate | Baseline |
| 3 | 2/s | Baseline | Candidate |
| 3 | 8/s | Candidate | Baseline |

## Environment Checks

Record Windows total CPU, memory availability, and process CPU deltas throughout
the suite. Record WSL CPU activity, pressure stall information, available memory,
and swap page counters every five seconds. Retain pre-run and post-run node
conditions and resource inventories.

Require each previous test namespace to disappear before the next run. Leave
all unrelated user processes and containers unchanged. Use a quiet interval
between runs. Unexpected unrelated workload, pressure, missed arrivals, failures,
or inventory exhaustion are explicit validity observations; preserve all runs
and evaluate affected pairs separately.

## Readouts

Primary: per-run claim-create to observed Ready p50/p90/p99 and failure counts.
Secondary: Create acknowledgement latency, warm/cold counts, pool inventory at
profile boundaries, controller CPU, allocation, mutex delay, and queue-attributed
profile samples.

Compare complete runs and paired effect directions. Individual claims within
one run share the environment and are not independent experiment replicates.
Report per-run ranges and medians across runs. Tail quantiles at 2/s have fewer
observations and receive correspondingly cautious interpretation.

## Instrumentation Control

Run two further 8/s pairs with CPU profile capture and mutex profiling disabled.
The order is baseline, candidate, candidate, baseline. Reuse the same binaries,
traffic, pool size, topology, host monitors, and resource cleanup checks.
Report this stage separately from the profiled runs. It tests whether the
observed end-to-end effect persists without profiling instrumentation.

## Decision

Consistent user-facing or material controller resource improvement supports
presenting the index for renewed review, together with FIFO/generic-path costs.
Effects comparable to run-to-run variation favor retaining the current simple
queue and narrowing the optimization to an independently demonstrated hotspot.
The original maintainer hold stays in place while the evidence is assessed.

## Tasks

- [x] Confirm unchanged revisions and existing evidence.
- [x] Recreate the dedicated cluster and preload images.
- [x] Validate orchestration and host monitors.
- [x] Complete warmup and six registered pairs.
- [ ] Complete two unprofiled 8/s control pairs on the selected validation host.
- [x] Validate profiles, inventory, host activity, and workload completion.
- [x] Produce the comparison and a contribution recommendation.
- [x] Remove the dedicated cluster.
- [x] Complete the experiment checklist.
