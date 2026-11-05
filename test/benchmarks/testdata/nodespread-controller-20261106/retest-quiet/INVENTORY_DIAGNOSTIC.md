# Visible Inventory Diagnostic: p5 Baseline

## Finding

`quiet-plain-p5-baseline` experienced depletion of **controller-visible Ready
inventory** while the observed pool population remained substantial. Its final
launch counters record **405 warm/ready and 47 cold/not_ready**, agreeing with
controller completion logs and the 452 workload records. Two selected Pods also
show large differences between embedded status timestamps and local watch
observation timestamps. The responsible lifecycle stage and the individual cold
fallback branches remain unresolved.

This is a post-hoc, descriptive diagnosis of one fixed run. Retain the complete
run in the registered comparison and report healthy-inventory and depleted
scenarios separately as diagnostic views. These observations alone do not
attribute a candidate/baseline latency difference to the queue implementation.
Source: [public diagnostic JSON](results-unprofiled/inventory-diagnostic.json),
fields `metrics`, `workload`, `controller`, and `watch`.

## Metric Timeline

Times below are **2026-11-06, UTC+08:00**. Table timestamps identify scrape
**start**, rounded to milliseconds; the JSON retains both scrape endpoints.
The arrival trigger was observed at `00:03:02.093010`, and the arrival boundary
at `00:04:02.009106`. These are local observations of workload log triggers.
Source: [telemetry timing](../runs/quiet-plain-p5-baseline/telemetry/run.json).

| Scrape | Started | Ready | Pending | Total | Warm/ready cumulative | Cold/not_ready cumulative |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| [0000](../runs/quiet-plain-p5-baseline/telemetry/metrics-0000.prom.gz) | 00:03:02.093 | 128 | 0 | 128 | 0 | 0 |
| [0001](../runs/quiet-plain-p5-baseline/telemetry/metrics-0001.prom.gz) | 00:03:07.096 | 117 | 11 | 128 | 29 | 0 |
| [0002](../runs/quiet-plain-p5-baseline/telemetry/metrics-0002.prom.gz) | 00:03:12.106 | 119 | 9 | 128 | 60 | 0 |
| [0003](../runs/quiet-plain-p5-baseline/telemetry/metrics-0003.prom.gz) | 00:03:17.112 | 114 | 12 | 126 | 97 | 0 |
| [0004](../runs/quiet-plain-p5-baseline/telemetry/metrics-0004.prom.gz) | 00:03:22.123 | 112 | 16 | 128 | 137 | 0 |
| [0005](../runs/quiet-plain-p5-baseline/telemetry/metrics-0005.prom.gz) | 00:03:27.152 | 119 | 8 | 127 | 173 | 0 |
| [0006](../runs/quiet-plain-p5-baseline/telemetry/metrics-0006.prom.gz) | 00:03:32.157 | 104 | 23 | 127 | 211 | 0 |
| [0007](../runs/quiet-plain-p5-baseline/telemetry/metrics-0007.prom.gz) | 00:03:37.165 | 86 | 41 | 127 | 258 | 0 |
| [0008](../runs/quiet-plain-p5-baseline/telemetry/metrics-0008.prom.gz) | 00:03:42.176 | 61 | 51 | 112 | 286 | 0 |
| [0009](../runs/quiet-plain-p5-baseline/telemetry/metrics-0009.prom.gz) | 00:03:47.184 | 34 | 74 | 108 | 315 | 0 |
| [0010](../runs/quiet-plain-p5-baseline/telemetry/metrics-0010.prom.gz) | 00:03:52.187 | 2 | 107 | 109 | 356 | 6 |
| [0011](../runs/quiet-plain-p5-baseline/telemetry/metrics-0011.prom.gz) | 00:03:57.203 | 3 | 125 | 128 | 382 | 23 |
| [0012](../runs/quiet-plain-p5-baseline/telemetry/metrics-0012.prom.gz) | 00:04:02.010 | 0 | 128 | 128 | 399 | 41 |

Failed and succeeded inventory are zero throughout these samples. Total is the
sum of all four gauge categories. The launch labels describe status at
creation/adoption, rather than final workload readiness. The
[final metrics](../runs/quiet-plain-p5-baseline/metrics-final.prom.gz) add six warm
and six cold launches beyond the last scrape. They contain no pool gauge series;
the diagnostic preserves that absence as `null`, rather than a zero inventory.
Metric series are decoded by the existing official Prometheus parser
`analyze_metrics`; its stdout is parsed as JSON.

## Watch Reconstruction

For each of the 47 cold Sandbox `ADDED` records, the diagnostic reconstructs the
latest preceding Sandbox and Pod observations **in watch-file order**. It keeps
nonterminating, pool-owned Sandboxes and excludes members after adoption or
deletion is observed. This is a separate observer from the controller cache,
with no claim of an atomic cluster or queue snapshot.

| Cold observation | Claim | Ready | Pending | Total | Pending without Sandbox / Pod IP | Pending node aliases |
| --- | --- | ---: | ---: | ---: | --- | --- |
| 00:03:52.104753601 | `p1-sclaim-355` | 2 | 107 | 109 | 107 / 107 | worker-1: 53; worker-2: 54 |
| 00:04:02.074855131 | `p1-sclaim-438` | 0 | 128 | 128 | 128 / 128 | worker-1: 64; worker-2: 64 |

Across all 47 observations, Ready ranges from **0 to 2**, Pending from **107 to
128**, and Total from **109 to 128**. Ready is zero in **25/47** observations.
Every Pending **Sandbox observation** lacks PodIPs. In **4/47** snapshots,
one or two corresponding Pods are already observed Running with an IP while
their Sandbox observations remain Pending without PodIPs. Another **8/47**
snapshots contain one to three Pending members whose Pods have not yet been
observed. These two cases are counted separately from observed Pending Pods
without IPs. The first and last snapshots shown above contain observed Pending
Pods without IPs for every Pending Sandbox. Node aliases represent the two test
workers; balanced visible placement does not establish independent host resources.
Source: JSON `watch.cold_events`, `watch.summary`, and `watch.selected_events`.
The first and last cold events are decompressed watch lines **11492** and
**13924**.

For example, at `00:03:54.371955352`, cold claim `p1-sclaim-379` is observed
with 127 Pending Sandboxes: 125 corresponding Pods are observed Pending without
IPs and two are observed Running with IPs (cold-event source line **12136**).
At `00:03:53.190229381`, `p1-sclaim-366` has 122 Pending Sandboxes and three
corresponding Pods still unobserved (line **11807**). Source: JSON
`watch.cold_events` and their referenced member observations.

Each snapshot lists `members` by `sandbox_line` and `pod_line`. Those references
resolve to allowlisted objects in `watch.selected_events`; readers can recompute
every snapshot count without receiving full resource objects. A missing Pod
observation is counted separately from an observed Pod lacking an IP.

## Two Pod Observations

All displayed times use UTC+08:00. Embedded times retain their original timezone
in JSON. The two Pods were selected during diagnosis; they are examples, not a
random sample or an estimate of the distribution.

| Evidence | `p1-spool-qsm76` | `p1-spool-jgtc8` |
| --- | --- | --- |
| Embedded container `startedAt` | 00:03:38 | 00:03:42 |
| Embedded `PodReadyToStartContainers=True` transition | 00:03:39 | 00:03:43 |
| First locally observed Running Pod | 00:03:53.807849045 | 00:04:04.172214570 |
| First locally observed Ready Sandbox | 00:03:53.837937467 | 00:04:04.202416041 |
| `startedAt` to Running observation | 15.807849045 s | 22.172214570 s |
| Network-condition transition to Running observation | 14.807849045 s | 21.172214570 s |
| Running Pod to Ready Sandbox observation | 30.088422 ms | 30.201471 ms |
| Running Pod / Ready Sandbox source lines | 11989 / 12001 | 14325 / 14327 |

Source: JSON `watch.pod_timings` and their referenced `watch.selected_events`.
The arithmetic preserves nanoseconds; the embedded lifecycle timestamps here
have whole-second precision. Kubernetes defines `PodReadyToStartContainers` in
terms of runtime sandbox creation and networking configuration.
[Official Pod condition semantics](https://v1-35.docs.kubernetes.io/docs/concepts/workloads/pods/pod-condition/).

These are directly computed differences between recorded timestamps. They do
not isolate kubelet execution, status submission, API processing, watch delivery,
client processing, or clock alignment. In particular, the later embedded
`startedAt` values show why a previously observed Pending status should not be
interpreted as proof that the physical container was still unstarted at that
observation time.

## Refill and Error Evidence

The scoped controller log contains **534** successful pool creation records,
including prewarm. Creation remains active during the cold interval: **11**
records in logged second `00:03:52` and **7** in `00:04:02`. These counts describe
completion-log seconds, not token issuance times or a rate-limit audit.
Source: JSON `controller.event_counts` and `controller.refill_by_logged_second`;
the first relevant log lines are **8369** and **9787**.

There are five expectations-gate records: four during prewarm
(`00:02:53`, twice; `00:02:54`; `00:02:55`) and one at `00:03:31`.
Their source lines are **150, 153, 290, 484, 6306**. No gate record occurs during
the observed cold-creation interval. Controller status records independently
show Ready=0 with replicas=115 at `00:03:53` and replicas=128 at `00:03:58`
(lines **8553**, **9294**). Logged inactive replicas remain zero during arrivals.
Source: JSON `controller`.

The inspected baseline implementation gates refill on **total population**
below desired replicas, so Pending members consume capacity even when Ready
inventory is zero. Implementation locations, relative to the local
`queue-index-production/baseline` tree:

- `extensions/controllers/sandboxwarmpool_controller.go:598`: population count;
  `:662`: total-population refill gate; `:705`: expectations gate.
- `extensions/controllers/sandboxclaim_controller.go:1279`: candidate removal;
  `:1348`: missing-PodIPs skip; `:1385`: empty/pending candidate outcomes;
  `:2227`: grace deadline; `:2423`: pending-network retry/cold fallback.
- `internal/metrics/warmpool_collector.go:129`: cached member list and owner
  filtering; `:189`: status classification.
- `internal/metrics/metrics.go:250`: launch counter and adoption-time status.

The scoped controller log has **zero error-level records** and **zero records
with a non-null error field**; the namespace watch has **zero Warning event
records**. The [REST counter analysis](results-unprofiled/rest-client-requests.json)
records **558 PATCH/409 responses during the sampled arrival window** and
**658 from process start through the final scrape**, including setup and
drain/cleanup preceding that scrape. All 452 claims completed successfully.
These HTTP counts describe internally handled optimistic conflicts.

The arrival-window counter interval spans the scrape midpoints around
`00:03:02.093` and `00:04:02.010`, lasting 59.934696 seconds. The adjacent
periodic intervals `00:03:47.184` to `00:03:52.187` and `00:03:52.187` to
`00:03:57.203` record 85 and 87 PATCH/409 responses respectively. These
intervals overlap the onset of observed cold creation. The REST counters
aggregate controller requests; per-claim attribution requires request-level
traces.

The baseline includes internal conflict handling: at
`extensions/controllers/sandboxclaim_controller.go:784`, a status PATCH conflict
enters `retryStatusPatchOnFreshBase`; a successful retry returns without
error-level logging, and a further conflict can yield to a watch update
(`:805`, `:922`). Thus error-level logs and conflict/retry activity measure
different behavior. The aggregate responses leave individual branch choices
unresolved. `analyze_rest.py` computes the window and periodic differences
from the formally parsed series; the inventory extractor handles inventory
and launch counters separately.

## Unresolved Questions

- Per-claim queue-empty versus PodIPs-grace-expiry attribution: the discriminating
  messages require V(4), and the available records do not prove each branch.
- Availability of the remaining one or two Ready members: concurrent adoption
  and temporary queue removal remain possible.
- Stage attribution for the timestamp differences, including clock alignment
  and status propagation. Host paging is a reported validity concern; its causal
  contribution is not established by these scoped artifacts.

## Reproduction and Disclosure

From `retest-quiet`, using existing local data and the already-built parser:

```bash
nice -n 15 ionice -c 3 python3 -B -m unittest -v test_diagnose_inventory.py
nice -n 15 ionice -c 3 python3 -B diagnose_inventory.py
nice -n 15 ionice -c 3 python3 -B diagnose_inventory.py \
  --verify-public results-unprofiled/inventory-diagnostic.json
```

The first command runs in-memory unit tests. The second reads only this fixed
run and writes the diagnostic JSON. The third recomputes **47 cold snapshots**
and **two timing rows** from public fields, without reading local logs or
executing the metrics parser. The scripts share the existing `read_metrics` and
nanosecond timestamp helpers from `summarize_unprofiled.py`; no compilation or
cluster operation is performed.

Public evidence contains scoped names, selected conditions, boolean IP
presence, selected container timestamps, aggregate counts, node aliases, and
source-line references. It excludes full Pod specs, IP addresses, UIDs, resource
annotations, credentials, arbitrary log messages, and unrelated resources.
The complete controller and watch logs remain local. Their SHA256 identifiers
are:

| Local source | SHA256 |
| --- | --- |
| `controller.log` | `58399b0a190a5d8590fd27a804217ad3ed3a115cebb81375be4166b46248f9c0` |
| `workload/watch.jsonl.gz` | `e4b702e32beee46a769cd1202e7a592cee2540d228e7c0598847d8348511c745` |

The watch digest covers the **compressed file bytes**; watch source lines refer
to its decompressed JSONL. Metric `raw_sha256` values in the diagnostic cover
uncompressed Prometheus text. The linked `.prom.gz` files and this document use
the public evidence-tree layout. Public recounting checks the selected evidence
and arithmetic; full extraction can be reproduced locally against the
fingerprinted source logs.
