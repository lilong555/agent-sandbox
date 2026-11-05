// Copyright 2026 The Kubernetes Authors.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

// emit_schedule uses the stress workload's Go PCG and exponential sampler.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"math"
	"math/rand/v2"
	"os"
	"time"
)

func main() {
	rate := flag.Float64("rate", 8, "Poisson arrivals per second")
	seconds := flag.Int("seconds", 120, "Arrival window length")
	flag.Parse()
	if math.IsNaN(*rate) || math.IsInf(*rate, 0) || *rate <= 0 || *seconds <= 0 ||
		*rate*float64(*seconds) > 100000 {
		fmt.Fprintln(os.Stderr, "Use a positive, finite, bounded arrival schedule")
		os.Exit(2)
	}
	rng := rand.New(rand.NewPCG(0, 0))
	offsets := make([]int64, 0)
	var next time.Duration
	for {
		gap := time.Duration(rng.ExpFloat64() / *rate * float64(time.Second))
		if gap <= 0 {
			gap = time.Nanosecond
		}
		next += gap
		if next > time.Duration(*seconds)*time.Second {
			break
		}
		offsets = append(offsets, int64(next))
	}
	report := struct {
		Rate      float64 `json:"rate"`
		Seconds   int     `json:"seconds"`
		Seed      [2]int  `json:"seed"`
		OffsetsNS []int64 `json:"offsets_ns"`
	}{*rate, *seconds, [2]int{0, 0}, offsets}
	if err := json.NewEncoder(os.Stdout).Encode(report); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
