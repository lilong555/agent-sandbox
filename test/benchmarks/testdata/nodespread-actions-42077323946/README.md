# NodeSpread Linux Actions Results

## Assessment

Keep the maintainer's hold on
[PR #2179](https://github.com/kubernetes-sigs/agent-sandbox/pull/2179).
This Linux round shows lower candidate p50 in both run pairs, higher p99 in
both pairs, and slightly higher controller CPU and cumulative allocation.
The two-pair p50 geometric mean change is **-10.293%**. These descriptive
observations support further investigation, while the end-to-end benefit
needed to justify the queue rewrite remains unproven.

The [successful Actions run](https://github.com/lilong555/agent-sandbox/actions/runs/42077323946)
completed all three calibration runs and four formal runs. The registered
calibration selected **2 claims/s**. All **392 formal claims** reached Ready
and completed deletion, with **zero failures and zero cold launches**.
The downloaded measurements were recomputed locally with **zero differences**.

## Environment

| Input | Value |
| --- | --- |
| Runner | GitHub-hosted Ubuntu 24.04, 4 vCPUs, AMD EPYC 7763 |
| Baseline A | `4d39311ba9d89001d79ff1217ed330c7ea3761ec` |
| Candidate B | `a87fa000ec96c8e3d9be493da5c6e6e2048b37ad` |
| Workflow commit | `a91ae4b04d91695d0dae042392ec8c8ae05573da` |
| Toolchain | Go 1.27.3 |
| Cluster | kind v0.33.0, Kubernetes v1.35.8, one control plane and two workers |
| Pool | 128 Ready members before each run |
| Workload | Shared executable, open-loop Poisson arrivals, PCG(0,0), one-second dwell |
| Execution | One job, serial ABBA, new controller process and namespace per run |
| Profiling | CPU, mutex and block profiling disabled |
| Observation | Five-second Prometheus and Linux host samples |

The [registered protocol](../nodespread-controller-20261106/retest-quiet/CI_PROTOCOL.md)
and [workflow](https://github.com/lilong555/agent-sandbox/blob/a91ae4b04d91695d0dae042392ec8c8ae05573da/.github/workflows/nodespread-controller-evidence.yml)
specify the complete build, calibration, cleanup and comparison procedure.
The implementation branch remains at the measured candidate revision.

## Capacity Calibration

Each baseline calibration has 30 seconds of arrivals. A passing level requires
complete lifecycles, sampled Ready inventory of at least 64, zero cold
launches, and median host CPU active time at most 85%.

| Nominal rate | Actual claims | Ready minimum | Cold launches | Host CPU median | Result |
| ---: | ---: | ---: | ---: | ---: | --- |
| 2/s | 44 | 127 | 0 | 55.60% | Pass |
| 4/s | 98 | 114 | 0 | 93.88% | CPU threshold exceeded |
| 8/s | 211 | 0 | 50 | 99.93% | CPU and inventory thresholds exceeded |

The highest passing level is 2/s. The 4/s and 8/s measurements remain in the
archive as calibration evidence. The candidate's latency has no role in
selecting the rate.

## Formal Comparison

Each run has 60 seconds of arrivals and exactly 98 claims from the same seeded
schedule. A and B identify the baseline and candidate. Percent changes below
are candidate relative to baseline.

| Pair | Order | p50 A -> B, ms | p50 change | p99 A -> B, ms | p99 change |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | AB | 39.098 -> 33.417 | -14.53% | 187.618 -> 231.968 | +23.64% |
| 2 | BA | 38.192 -> 35.960 | -5.84% | 143.819 -> 204.215 | +42.00% |

| Pair | Controller CPU seconds A -> B | CPU change | Allocated GB A -> B | Allocation change |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 5.77 -> 5.79 | +0.35% | 1.1468 -> 1.1782 | +2.74% |
| 2 | 5.70 -> 5.84 | +2.46% | 1.1666 -> 1.1761 | +0.82% |

Allocated GB is cumulative allocated bytes divided by 1,000,000,000.
Controller counters cover the arrival window. Latency covers every created
claim through eventual readiness, including the drain period.

Formal-run host CPU medians range from 50.91% to 51.93%. Sampled Ready
inventory stays between 122 and 128. Every formal launch is recorded as
`warm/ready`. Every formal schedule matches the seeded arrival count, with
zero out-of-order arrivals and zero creates after the observed end boundary.
Per-run pacing and telemetry remain in `summary.json`.

There are two run-level pairs. The primary ratio is the geometric mean
of their candidate/baseline p50 ratios, **0.897075**. The protocol reports a
descriptive effect without a confidence interval or significance test.
Each run has 98 claims, so its nearest-rank p99 is its maximum observation.
The tail measurements deserve attention and are especially sensitive to
individual slow requests at this sample size.

The [separate WSL2 measurements](../nodespread-controller-20261106/retest-quiet/README.md)
contain six primary pairs and same-binary controls. They show a +1.111%
primary p50 change with substantial variation. The Linux and WSL2 results
retain their separate workloads and interpretations.

## Evidence

- `round-results.tar.gz`: all seven runs' registered inputs, raw claim
  durations, raw Prometheus samples, numeric host telemetry and summaries.
- `summary.json`: the complete Actions comparison.
- `verification-original.json`: offline recomputation of the downloaded
  original tree.
- `verification-public.json`: offline recomputation of the published archive.
- `provenance.json`: source run, original artifact digest and disclosure policy.
- `setup-evidence/`: compiler, CPU, Kubernetes and image provenance.
- `verify_ci_round.py` and `test_verify_ci_round.py`: offline verifier and tests.
- `SHA256SUMS`: file integrity manifest.

Controller logs, stress logs, watch objects, additional timeseries and final
cluster inventories stay outside this published archive. The retained inputs
reproduce the calibration decision and every formal statistic. Kubeconfig and
authentication material are absent from the published files.

## Recompute

Use the dependency versions from the candidate's `go.mod` and Python 3.10 or
later. In an evidence checkout, set these paths to absolute locations:

```bash
EVIDENCE=/path/to/checkout/test/benchmarks/testdata
HARNESS="$EVIDENCE/nodespread-controller-20261106/retest-quiet"
ROUND="$EVIDENCE/nodespread-actions-42077323946"
TOOLS=/path/to/local-analysis-tools
EXTRACTED=/path/to/new-extraction
VERIFIED=/path/to/new-verification

cd "$ROUND"
sha256sum --check SHA256SUMS
mkdir -p "$TOOLS" "$EXTRACTED"
go build -o "$TOOLS/analyze_metrics" "$HARNESS/analyze_metrics.go"
go build -o "$TOOLS/emit_schedule" "$HARNESS/emit_schedule.go"
tar -xzf "$ROUND/round-results.tar.gz" -C "$EXTRACTED"
PYTHONPATH="$HARNESS" python3 -B "$ROUND/verify_ci_round.py" \
  --source "$EXTRACTED/round-results" --output "$VERIFIED" \
  --metrics-parser "$TOOLS/analyze_metrics" \
  --schedule-emitter "$TOOLS/emit_schedule"
PYTHONPATH="$HARNESS" python3 -B -m unittest discover \
  -s "$ROUND" -p 'test_verify_ci_round.py'
```

The verifier reads artifact files as data and uses the explicitly supplied
local helpers. It checks source revisions, binary-hash consistency, workload
configuration, all seven schedules, lifecycle records, calibration gates and
paired statistics. It writes `verification.json` and a recomputed summary.
