// Analyze complete pprof samples using inclusive queue-stack attribution.
// From repo/: go run ../cluster-profile/analyze_profiles.go FILE.pprof [...]
// sample_count counts profile records, not events or operations. Percentages
// are null when the corresponding total is zero. Files are summarized separately.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"os"
	"strings"

	"github.com/google/pprof/profile"
)

const queuePrefix = "sigs.k8s.io/agent-sandbox/extensions/controllers/queue."

type sampleSummary struct {
	Type                 string   `json:"type"`
	Unit                 string   `json:"unit"`
	Total                int64    `json:"total"`
	QueueStackTotal      int64    `json:"queue_stack_total"`
	QueueStackPercentage *float64 `json:"queue_stack_percentage"`
}

type profileSummary struct {
	File        string          `json:"file"`
	DurationNS  int64           `json:"duration_ns"`
	SampleCount int             `json:"sample_count"`
	SampleTypes []sampleSummary `json:"sample_types"`
}

type fileError struct {
	File  string `json:"file"`
	Error string `json:"error"`
}

type report struct {
	QueueFunctionPrefix string           `json:"queue_function_prefix"`
	Attribution         string           `json:"attribution"`
	Profiles            []profileSummary `json:"profiles"`
	Errors              []fileError      `json:"errors"`
}

func hasQueueFrame(sample *profile.Sample) bool {
	for _, location := range sample.Location {
		for _, line := range location.Line {
			if line.Function != nil && strings.HasPrefix(line.Function.Name, queuePrefix) {
				return true
			}
		}
	}
	return false
}

func addExact(total *int64, value int64) error {
	if (value > 0 && *total > math.MaxInt64-value) ||
		(value < 0 && *total < math.MinInt64-value) {
		return fmt.Errorf("sample total exceeds int64 range")
	}
	*total += value
	return nil
}

func summarize(path string) (profileSummary, error) {
	result := profileSummary{File: path}
	input, err := os.Open(path)
	if err != nil {
		return result, err
	}
	defer input.Close()

	// Parse validates sample value counts and the location/function tables.
	p, err := profile.Parse(input)
	if err != nil {
		return result, err
	}
	result.DurationNS = p.DurationNanos
	result.SampleCount = len(p.Sample)
	result.SampleTypes = make([]sampleSummary, len(p.SampleType))
	for i, sampleType := range p.SampleType {
		if sampleType == nil {
			return result, fmt.Errorf("sample type %d is nil", i)
		}
		result.SampleTypes[i] = sampleSummary{Type: sampleType.Type, Unit: sampleType.Unit}
	}
	for _, sample := range p.Sample {
		inQueue := hasQueueFrame(sample)
		for i, value := range sample.Value {
			entry := &result.SampleTypes[i]
			if err := addExact(&entry.Total, value); err != nil {
				return result, fmt.Errorf("sample type %q total: %w", entry.Type, err)
			}
			if inQueue {
				if err := addExact(&entry.QueueStackTotal, value); err != nil {
					return result, fmt.Errorf("sample type %q queue total: %w", entry.Type, err)
				}
			}
		}
	}
	for i := range result.SampleTypes {
		entry := &result.SampleTypes[i]
		if entry.Total != 0 {
			percentage := 100 * (float64(entry.QueueStackTotal) / float64(entry.Total))
			entry.QueueStackPercentage = &percentage
		}
	}
	return result, nil
}

func main() {
	flag.Usage = func() {
		fmt.Fprintln(os.Stderr, "Usage: analyze_profiles FILE.pprof [FILE.pprof ...]")
	}
	flag.Parse()
	if flag.NArg() == 0 {
		flag.Usage()
		os.Exit(2)
	}

	output := report{
		QueueFunctionPrefix: queuePrefix,
		Attribution:         "inclusive: each sample contributes once if any stack frame matches the queue function prefix",
		Profiles:            make([]profileSummary, 0, flag.NArg()),
		Errors:              make([]fileError, 0),
	}
	for _, path := range flag.Args() {
		summary, err := summarize(path)
		if err != nil {
			output.Errors = append(output.Errors, fileError{File: path, Error: err.Error()})
			continue
		}
		output.Profiles = append(output.Profiles, summary)
	}
	encoder := json.NewEncoder(os.Stdout)
	encoder.SetIndent("", "  ")
	if err := encoder.Encode(output); err != nil {
		fmt.Fprintf(os.Stderr, "write JSON: %v\n", err)
		os.Exit(1)
	}
	if len(output.Errors) != 0 {
		os.Exit(1)
	}
}
