# Linux Controller Comparison

This experiment measures the controller behavior of
[PR #2179](https://github.com/kubernetes-sigs/agent-sandbox/pull/2179) on one
GitHub-hosted Ubuntu 24.04 runner. All runs share one job, one kind cluster,
and the same workload executable.

## Fixed Inputs

| Input | Value |
| --- | --- |
| Baseline | `4d39311ba9d89001d79ff1217ed330c7ea3761ec` |
| Indexed candidate | `a87fa000ec96c8e3d9be493da5c6e6e2048b37ad` |
| Compiler | Go 1.27.3 |
| Cluster | kind v0.33.0; one control plane and two workers |
| Kubernetes image | `kindest/node:v1.35.8@sha256:07b2536e30b803ed61d1677a79df6115f798ce64c80f9e22f6ed45afd09323c0` |
| Sandbox image | `node@sha256:dbcedd8aeab47fbc0f4dd4bffa55b7c3c729a707875968d467aaaea42d6225af`, tagged `node:22-alpine` |
| Pool | 128 members |
| Arrivals | Open-loop Poisson schedule with PCG(0,0) |
| Claim dwell | 1 second |
| Client connections | 4 |
| Controller | `GOMAXPROCS=4`, Linux affinity detected from the runner |
| Profiling | Disabled |
| Telemetry | Prometheus and Linux host samples every 5 seconds |

The workflow builds both unchanged controller revisions with the repository's
`make build-controller` target. A source overlay sets the existing stress
workload's random seed. Downloads, compilation, and image loading finish before
the first calibration run. Every run starts a fresh controller process and
uses a new namespace. The harness waits for cleanup before continuing.

## Capacity Calibration

Run the baseline at 2, 4, and 8 claims/s, with 30 seconds of arrivals per
level. A level passes when all of these conditions hold:

- Every observed claim reaches Ready and is deleted, with zero failures.
- The workload starts with 128 Ready members.
- Sampled Ready inventory stays at or above 64.
- The claim creation metrics record zero cold starts.
- Median Linux CPU active time during the arrival window stays at or below 85%.
- Required lifecycle, timing, and telemetry records are complete.

Select the highest passing rate. Stop when the 2 claims/s level fails.
Execution errors stop the experiment and retain the available diagnostics.
Calibration uses baseline capacity and excludes candidate latency from rate
selection.

## Paired Comparison

Run baseline, candidate, candidate, baseline in that order at the selected
rate. Each run has a 60-second arrival window. Pair the first two runs and
the final two runs. Report complete-window create-to-Ready p50, p90, p99,
controller CPU seconds, cumulative allocated bytes, warm/cold counts, and
host pressure. Preserve every formal run, including any capacity violation.

The primary comparison is the geometric mean of the two paired
candidate/baseline p50 ratios. Two run pairs provide descriptive evidence.
Host samples, inventory observations, request pacing, and within-round
differences accompany the latency result. Interpret this Linux round
separately from the WSL2 measurements.

The scheduler's end log marks completion of lifecycle goroutine dispatch.
A dispatched goroutine can call Create after that log. The report retains
these lifecycles and records their count and timing offsets. Controller
counter windows remain anchored to the recorded boundary scrapes.

## Artifacts

The workflow uploads its raw run directories and setup diagnostics on success
and failure. Kubeconfig and authentication material stay outside these
directories. Artifacts retain source revisions, executable fingerprints,
runtime versions, claim lifecycle records, controller metrics, and numeric
host telemetry. The Actions summary links the result to its job and commit.
Artifacts remain available for seven days; the experiment owner also downloads
them for durable verification.
