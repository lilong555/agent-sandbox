# NodeSpread Controller Evidence

Controller measurements for
[agent-sandbox PR #2179](https://github.com/kubernetes-sigs/agent-sandbox/pull/2179).

- [Results and interpretation](retest-quiet/README.md)
- [Unprofiled experimental protocol](retest-quiet/UNPROFILED_PROTOCOL.md)
- [Profiled experimental protocol](retest-quiet/PROTOCOL.md)
- [Linux Actions experimental protocol](retest-quiet/CI_PROTOCOL.md)
- [Visible-inventory diagnostic](retest-quiet/INVENTORY_DIAGNOSTIC.md)
- [Unprofiled per-run statistics](retest-quiet/results-unprofiled/summary-unprofiled.json)
- [Profiled per-run statistics](retest-quiet/results/summary.json)
- [Controller REST request counts](retest-quiet/results-unprofiled/rest-client-requests.json)

The baseline is `4d39311ba9d89001d79ff1217ed330c7ea3761ec`. The indexed
candidate is `a87fa000ec96c8e3d9be493da5c6e6e2048b37ad`. Every run includes
the source revision, executable fingerprints, controller configuration,
claim lifecycle timings, and sampled metrics. The two measurement modes have
separate registrations, manifests, and analysis.

## Recompute the Published Results

Use Python 3.10 or later, Go 1.27.3, and a candidate checkout at the exact
revision above. Prepare its module cache with `go mod download` before offline
verification. The evidence branch has an additional data commit, so create a
separate checkout for the candidate:

```bash
git worktree add --detach /path/to/candidate a87fa000ec96c8e3d9be493da5c6e6e2048b37ad
/path/to/go1.27.3/bin/go -C /path/to/candidate mod download
python3 -B /path/to/evidence/retest-quiet/reproduce_evidence.py \
  --source-repo /path/to/candidate \
  --go /path/to/go1.27.3/bin/go \
  --output /path/to/new-reproduction
```

The output directory must be new and outside both checkouts. The verifier
checks the complete SHA256 manifest, reconstructs the compressed records in
that directory, builds the three analysis helpers with network downloads
disabled, and regenerates the published statistics. It compares every JSON
statistic after normalizing artifact paths and writes `verify.json`. It also
recounts the public inventory observations and audits actual arrival times
against the fixed Go PCG(0,0) schedule.

## Data Layout

`runs/` holds individual samples. `retest-quiet/results/` holds the profiled
suite; `retest-quiet/results-unprofiled/` holds the unprofiled suite. Raw
Prometheus samples, claim JSONL, and host telemetry use deterministic gzip
encoding. Pprof files retain their original bytes.

`EXPORT.json` defines the disclosure policy. Public records omit kubeconfig,
authentication material, host process identities, full Kubernetes resource
objects, and unfiltered controller/watch logs. The inventory diagnostic
contains selected, allowlisted watch observations with source fingerprints.
`SHA256SUMS` covers the complete published tree.

The execution scripts describe the measured WSL2 setup. A fresh Linux run
requires an explicit environment registration, suitable CPU affinity, Linux
host monitoring, and newly recorded executable hashes. The offline verifier
uses the recorded workload and performs no cluster operations.
