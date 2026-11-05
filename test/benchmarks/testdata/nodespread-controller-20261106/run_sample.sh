#!/usr/bin/env bash
set -euo pipefail

root="$(realpath "$(dirname "${BASH_SOURCE[0]}")")"
revision="${1:?Usage: run_sample.sh baseline|candidate sample-name [pool-size] [claims-per-second]}"
sample="${2:?A unique sample name is required}"
pool="${3:-128}"
rate="${4:-8}"
seconds="${RUN_SECONDS:-60}"
profile_seconds="${PROFILE_SECONDS:-30}"
profile_delay="${PROFILE_DELAY:-10}"

case "$revision" in
  baseline) repo="$root/../baseline" ;;
  candidate) repo="$root/../repo" ;;
  *) printf 'Unknown revision: %s\n' "$revision" >&2; exit 2 ;;
esac
if [[ ! "$sample" =~ ^[a-z0-9][a-z0-9-]*$ ]] || ((${#sample} > 40)); then
  printf 'Use a lowercase sample name of at most 40 characters.\n' >&2
  exit 2
fi
if [[ ! "$pool" =~ ^[0-9]+$ || ! "$rate" =~ ^[0-9]+$ ]] ||
   ((rate < 1 || pool < 1 || pool % rate != 0)); then
  printf 'Pool size must be positive and divisible by the positive arrival rate.\n' >&2
  exit 2
fi

export KUBECONFIG="$root/kubeconfig"
context="$(kubectl config current-context)"
if [[ "$context" != "kind-as-nodespread-2179" ]]; then
  printf 'Unexpected dedicated kubeconfig context: %s\n' "$context" >&2
  exit 2
fi
if curl --noproxy '*' --silent --max-time 1 http://127.0.0.1:18081/healthz >/dev/null; then
  printf 'The controller health port is already occupied.\n' >&2
  exit 2
fi

mkdir -p "$root/runs"
run="$root/runs/$sample"
mkdir "$run"
manager_pid=""
cleanup() {
  if [[ -n "$manager_pid" ]]; then
    kill -TERM "$manager_pid" 2>/dev/null || true
    wait "$manager_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

controller_args=(
  --leader-elect=false
  --extensions
  --metrics-bind-address=127.0.0.1:18080
  --health-probe-bind-address=127.0.0.1:18081
  --metrics-secure-serving=false
  --enable-pprof-debug
  --pprof-mutex-profile-fraction=1
  --pprof-block-profile-rate=0
  --sandbox-concurrent-workers=100
  --sandbox-claim-concurrent-workers=50
  --sandbox-warm-pool-concurrent-workers=1
  --sandbox-template-concurrent-workers=1
  --sandbox-warm-pool-max-batch-size=32
  --sandbox-warm-pool-max-refill-rate=16
)
commit="$(git -C "$repo" rev-parse HEAD)"
jq -n \
  --arg revision "$revision" \
  --arg commit "$commit" \
  --arg image "node:22-alpine" \
  --argjson pool "$pool" \
  --argjson rate "$rate" \
  --argjson seconds "$seconds" \
  --argjson profile_seconds "$profile_seconds" \
  --argjson profile_delay "$profile_delay" \
  --args \
  '{revision: $revision, commit: $commit, image: $image,
    pool: $pool, claims_per_second: $rate, seconds: $seconds,
    profile_seconds: $profile_seconds, profile_delay: $profile_delay,
    gomaxprocs: 4, cpus: [6, 8, 10, 12], seed: [0, 0],
    controller_args: $ARGS.positional}' \
  -- "${controller_args[@]}" >"$run/config.json"
kubectl get nodes -o json >"$run/nodes.json"
sha256sum "$repo/bin/manager" "$root/tools/stress" >"$run/binaries.sha256"

GOMAXPROCS=4 taskset -c 6,8,10,12 "$repo/bin/manager" \
  "${controller_args[@]}" >"$run/controller.log" 2>&1 &
manager_pid=$!
ready=false
for _ in {1..60}; do
  if ! kill -0 "$manager_pid" 2>/dev/null; then
    printf 'Controller exited; see %s/controller.log\n' "$run" >&2
    exit 1
  fi
  if curl --noproxy '*' --fail --silent --max-time 1 \
      http://127.0.0.1:18081/readyz >/dev/null; then
    ready=true
    break
  fi
  sleep 1
done
if [[ "$ready" != true ]]; then
  printf 'Controller readiness timed out; see %s/controller.log\n' "$run" >&2
  exit 1
fi

headroom="$((pool / rate))s"
set +e
python3 "$root/profile_workload.py" \
  --output-dir "$run/profile" \
  --metrics-url http://127.0.0.1:18080 \
  --profile-seconds "$profile_seconds" \
  --profile-delay "$profile_delay" \
  -- "$root/tools/stress" \
  -namespace="np2179-$sample" \
  -output-dir="$run/workload" \
  -image=node:22-alpine \
  -phases=claims-warm-sustained \
  -sustained-rate="$rate" \
  -sustained-seconds="$seconds" \
  -sustained-pool-headroom="$headroom" \
  -claim-dwell=1s \
  -client-connections=4 \
  -profile-controller=false \
  -profile-apiserver=false \
  -collect-metrics=false \
  -timeout=8m \
  -per-sandbox-timeout=2m
status=$?
set -e
curl --noproxy '*' --fail --silent --show-error --max-time 10 \
  http://127.0.0.1:18080/metrics >"$run/metrics-final.prom" || true
printf 'Sample %s completed with status %s. Artifacts: %s\n' "$sample" "$status" "$run"
exit "$status"
