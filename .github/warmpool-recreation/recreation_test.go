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

package controllers

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	sandboxv1beta1 "sigs.k8s.io/agent-sandbox/api/v1beta1"
	sandboxcontrollers "sigs.k8s.io/agent-sandbox/controllers"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func TestWarmPoolRecreation(t *testing.T) {
	for _, tc := range []struct {
		name     string
		recreate bool
		advance  time.Duration
		creates  int
	}{
		{name: "same_pool_still_counts_terminating_member", creates: 0},
		{name: "recreated_pool_before_expiry", recreate: true, creates: 1},
		{name: "recreated_pool_after_expiry", recreate: true, advance: expectationsTimeout + time.Second, creates: 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			ctx := context.Background()
			pool, template := newOverCreationFixture(0)
			member := createPoolSandbox(pool.Name, pool.Namespace, sandboxcontrollers.NameHash(pool.Name), template, "-retiring")
			member.UID = "retiring-member-uid"
			member.Finalizers = []string{"test.agents.x-k8s.io/retain"}
			member.Status.Conditions = []metav1.Condition{{Type: string(sandboxv1beta1.SandboxConditionReady), Status: metav1.ConditionTrue}}
			scheme := newTestScheme()
			require.NoError(t, ctrl.SetControllerReference(pool, member, scheme))
			c := newLaggingClient(newFakeClient(scheme, pool, template, member))
			r := SandboxWarmPoolReconciler{Client: c, Scheme: scheme, MaxBatchSize: sandboxCreateDeleteMaxBatchSize}
			now := time.Now()
			r.exp().now = func() time.Time { return now }
			key := types.NamespacedName{Namespace: pool.Namespace, Name: pool.Name}
			request := ctrl.Request{NamespacedName: key}

			_, err := r.Reconcile(ctx, request)
			require.NoError(t, err)
			require.True(t, r.exp().IsPendingDeletion(key, member.UID))
			require.NoError(t, c.Get(ctx, client.ObjectKeyFromObject(member), member))
			require.False(t, member.DeletionTimestamp.IsZero())

			if tc.recreate {
				require.NoError(t, c.Delete(ctx, pool))
				pool, _ = newOverCreationFixture(1)
				pool.UID = "replacement-pool-uid"
				require.NoError(t, c.Create(ctx, pool))
			} else {
				require.NoError(t, c.Get(ctx, key, pool))
				pool.Spec.Replicas = new(int32(1))
				require.NoError(t, c.Update(ctx, pool))
			}

			now = now.Add(tc.advance)
			result, err := r.Reconcile(ctx, request)
			require.NoError(t, err)
			t.Logf("pool_uid=%s elapsed=%s creates=%d requeue=%s", pool.UID, tc.advance, c.createCount(), result.RequeueAfter)
			require.NoError(t, c.Get(ctx, client.ObjectKeyFromObject(member), member))
			require.False(t, member.DeletionTimestamp.IsZero(), "the old member remains terminating in every scenario")
			require.Equal(t, tc.creates, c.createCount(), "a recreated pool must not wait for a previous pool's expectations")
		})
	}
}
