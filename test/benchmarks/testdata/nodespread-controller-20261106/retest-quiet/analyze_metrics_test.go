package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestReadMetricsPreservesLaunchConditions(t *testing.T) {
	path := filepath.Join(t.TempDir(), "metrics.prom")
	data := `# TYPE agent_sandbox_claim_creation_total counter
agent_sandbox_claim_creation_total{launch_type="warm",pod_condition="ready"} 8
agent_sandbox_claim_creation_total{launch_type="warm",pod_condition="not_ready"} 2
# TYPE process_cpu_seconds_total counter
process_cpu_seconds_total 3.14
# TYPE unrelated gauge
unrelated 99
`
	if err := os.WriteFile(path, []byte(data), 0600); err != nil {
		t.Fatal(err)
	}
	report, err := readMetrics(path)
	if err != nil {
		t.Fatal(err)
	}
	if len(report.Samples) != 3 {
		t.Fatalf("got %d samples, want 3", len(report.Samples))
	}
	counts := map[string]float64{}
	for _, item := range report.Samples {
		if item.Name == "agent_sandbox_claim_creation_total" {
			counts[item.Labels["pod_condition"]] += item.Value
		}
	}
	if counts["ready"] != 8 || counts["not_ready"] != 2 {
		t.Fatalf("unexpected condition counts: %v", counts)
	}
}

func TestReadMetricsRejectsMalformedData(t *testing.T) {
	path := filepath.Join(t.TempDir(), "invalid.prom")
	if err := os.WriteFile(path, []byte("metric{broken nope\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := readMetrics(path); err == nil {
		t.Fatal("expected malformed metrics to be rejected")
	}
}

func TestReadMetricsPreservesRESTResponseCodes(t *testing.T) {
	path := filepath.Join(t.TempDir(), "rest.prom")
	data := `# TYPE rest_client_requests_total counter
rest_client_requests_total{code="200",host="127.0.0.1:6443",method="PATCH"} 120
rest_client_requests_total{code="409",host="127.0.0.1:6443",method="PATCH"} 7
rest_client_requests_total{code="<error>",host="127.0.0.1:6443",method="POST"} 2
`
	if err := os.WriteFile(path, []byte(data), 0600); err != nil {
		t.Fatal(err)
	}
	report, err := readMetrics(path)
	if err != nil {
		t.Fatal(err)
	}
	counts := map[string]float64{}
	for _, item := range report.Samples {
		counts[item.Labels["code"]] += item.Value
	}
	if len(report.Samples) != 3 || counts["200"] != 120 || counts["409"] != 7 || counts["<error>"] != 2 {
		t.Fatalf("unexpected REST counts: %v", counts)
	}
}
