package main

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"

	"github.com/prometheus/common/expfmt"
	"github.com/prometheus/common/model"
)

type sample struct {
	Name   string            `json:"name"`
	Labels map[string]string `json:"labels"`
	Value  float64           `json:"value"`
}

type report struct {
	File    string   `json:"file"`
	Samples []sample `json:"samples"`
}

func readMetrics(path string) (report, error) {
	result := report{File: path, Samples: []sample{}}
	file, err := os.Open(path)
	if err != nil {
		return result, err
	}
	defer file.Close()
	parser := expfmt.NewTextParser(model.UTF8Validation)
	families, err := parser.TextToMetricFamilies(file)
	if err != nil {
		return result, err
	}
	for name, family := range families {
		if name != "agent_sandbox_claim_creation_total" &&
			name != "agent_sandbox_warmpool_size" &&
			name != "process_cpu_seconds_total" &&
			name != "rest_client_requests_total" &&
			!strings.HasPrefix(name, "go_memstats_") {
			continue
		}
		for _, metric := range family.Metric {
			entry := sample{Name: name, Labels: map[string]string{}}
			for _, label := range metric.Label {
				entry.Labels[label.GetName()] = label.GetValue()
			}
			switch {
			case metric.Counter != nil:
				entry.Value = metric.Counter.GetValue()
			case metric.Gauge != nil:
				entry.Value = metric.Gauge.GetValue()
			default:
				continue
			}
			result.Samples = append(result.Samples, entry)
		}
	}
	sort.Slice(result.Samples, func(i, j int) bool {
		left, _ := json.Marshal(result.Samples[i])
		right, _ := json.Marshal(result.Samples[j])
		return string(left) < string(right)
	})
	return result, nil
}

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "Usage: analyze_metrics FILE.prom [...]")
		os.Exit(2)
	}
	reports := make([]report, 0, len(os.Args)-1)
	for _, path := range os.Args[1:] {
		result, err := readMetrics(path)
		if err != nil {
			fmt.Fprintf(os.Stderr, "%s: %v\n", path, err)
			os.Exit(1)
		}
		reports = append(reports, result)
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(reports); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
