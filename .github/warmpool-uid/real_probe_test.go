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
	"os"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	toolscache "k8s.io/client-go/tools/cache"
	cachetesting "k8s.io/client-go/tools/cache/testing"
	"k8s.io/client-go/util/workqueue"

	sandboxv1beta1 "sigs.k8s.io/agent-sandbox/api/v1beta1"
	sandboxcontrollers "sigs.k8s.io/agent-sandbox/controllers"
	extensionsv1beta1 "sigs.k8s.io/agent-sandbox/extensions/api/v1beta1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/cache"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

type uidPoolNotification struct {
	uid  types.UID
	kind string
}

func waitUIDNotification(t *testing.T, notifications <-chan uidPoolNotification, uid types.UID, kind string) {
	t.Helper()
	timer := time.NewTimer(10 * time.Second)
	defer timer.Stop()
	for {
		select {
		case notification := <-notifications:
			if notification.uid == uid && (kind == "" || notification.kind == kind) {
				return
			}
		case <-timer.C:
			t.Fatalf("timed out waiting for %s event for UID %s", kind, uid)
		}
	}
}

func TestWarmPoolUIDAPIServer(t *testing.T) {
	assets := os.Getenv("KUBEBUILDER_ASSETS")
	require.NotEmpty(t, assets)
	environment := &envtest.Environment{
		BinaryAssetsDirectory: assets,
		CRDDirectoryPaths:     []string{filepath.Join("..", "..", "k8s", "crds")},
		ErrorIfCRDPathMissing: true,
	}
	config, err := environment.Start()
	require.NoError(t, err)
	t.Cleanup(func() { require.NoError(t, environment.Stop()) })
	scheme := newTestScheme()
	direct, err := client.New(config, client.Options{Scheme: scheme})
	require.NoError(t, err)

	for _, reconcileMissing := range []bool{false, true} {
		name := "coalesced_delete_and_recreate"
		if reconcileMissing {
			name = "missing_pool_reconciled"
		}
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 40*time.Second)
			defer cancel()
			namespace := "uid-coalesced"
			if reconcileMissing {
				namespace = "uid-missing"
			}
			require.NoError(t, direct.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}}))
			pool, template := newOverCreationFixture(0)
			pool.Namespace, template.Namespace, pool.UID = namespace, namespace, ""
			require.NoError(t, direct.Create(ctx, template))
			require.NoError(t, direct.Create(ctx, pool))
			oldUID := pool.UID
			member := createPoolSandbox(pool.Name, namespace, sandboxcontrollers.NameHash(pool.Name), template, "-retiring")
			member.CreationTimestamp = metav1.Time{}
			member.Finalizers = []string{"test.agents.x-k8s.io/retain"}
			require.NoError(t, ctrl.SetControllerReference(pool, member, scheme))
			require.NoError(t, direct.Create(ctx, member))

			reader, err := cache.New(config, cache.Options{
				Scheme:            scheme,
				DefaultNamespaces: map[string]cache.Config{namespace: {}},
			})
			require.NoError(t, err)
			require.NoError(t, reader.IndexField(ctx, &sandboxv1beta1.Sandbox{}, sandboxWarmPoolLabelIndex, sandboxWarmPoolLabelIndexer))
			poolInformer, err := reader.GetInformer(ctx, &extensionsv1beta1.SandboxWarmPool{}, cache.BlockUntilSynced(false))
			require.NoError(t, err)
			sandboxInformer, err := reader.GetInformer(ctx, &sandboxv1beta1.Sandbox{}, cache.BlockUntilSynced(false))
			require.NoError(t, err)
			cached, err := client.New(config, client.Options{Scheme: scheme, Cache: &client.CacheOptions{Reader: reader}})
			require.NoError(t, err)
			r := &SandboxWarmPoolReconciler{Client: cached, Scheme: scheme, MaxBatchSize: sandboxCreateDeleteMaxBatchSize}
			var clock atomic.Int64
			clock.Store(time.Now().UnixNano())
			r.exp().now = func() time.Time { return time.Unix(0, clock.Load()) }
			queue := workqueue.NewTypedRateLimitingQueue(workqueue.DefaultTypedControllerRateLimiter[reconcile.Request]())
			defer queue.ShutDown()
			poolEvents := make(chan uidPoolNotification, 64)
			sandboxEvents := make(chan uidPoolNotification, 64)
			rootHandler := &handler.EnqueueRequestForObject{}
			_, err = poolInformer.AddEventHandler(toolscache.ResourceEventHandlerFuncs{
				AddFunc: func(obj any) {
					object := obj.(*extensionsv1beta1.SandboxWarmPool)
					rootHandler.Create(ctx, event.CreateEvent{Object: object}, queue)
					poolEvents <- uidPoolNotification{uid: object.UID, kind: "add"}
				},
				UpdateFunc: func(oldObj, newObj any) {
					oldObject := oldObj.(*extensionsv1beta1.SandboxWarmPool)
					newObject := newObj.(*extensionsv1beta1.SandboxWarmPool)
					rootHandler.Update(ctx, event.UpdateEvent{ObjectOld: oldObject, ObjectNew: newObject}, queue)
					poolEvents <- uidPoolNotification{uid: newObject.UID, kind: "update"}
				},
				DeleteFunc: func(obj any) {
					object := obj.(*extensionsv1beta1.SandboxWarmPool)
					rootHandler.Delete(ctx, event.DeleteEvent{Object: object}, queue)
					poolEvents <- uidPoolNotification{uid: object.UID, kind: "delete"}
				},
			})
			require.NoError(t, err)
			mapper := meta.NewDefaultRESTMapper([]schema.GroupVersion{extensionsv1beta1.GroupVersion})
			mapper.Add(extensionsv1beta1.GroupVersion.WithKind("SandboxWarmPool"), meta.RESTScopeNamespace)
			sandboxHandler := &warmPoolSandboxEventHandler{
				EventHandler: handler.EnqueueRequestForOwner(scheme, mapper, &extensionsv1beta1.SandboxWarmPool{}, handler.OnlyControllerOwner()),
				expectations: r.exp(),
			}
			_, err = sandboxInformer.AddEventHandler(toolscache.ResourceEventHandlerFuncs{
				AddFunc: func(obj any) {
					object := obj.(*sandboxv1beta1.Sandbox)
					sandboxHandler.Create(ctx, event.CreateEvent{Object: object}, queue)
					sandboxEvents <- uidPoolNotification{uid: object.UID, kind: "add"}
				},
				UpdateFunc: func(oldObj, newObj any) {
					sandboxHandler.Update(ctx, event.UpdateEvent{ObjectOld: oldObj.(*sandboxv1beta1.Sandbox), ObjectNew: newObj.(*sandboxv1beta1.Sandbox)}, queue)
				},
				DeleteFunc: func(obj any) {
					sandboxHandler.Delete(ctx, event.DeleteEvent{Object: obj.(*sandboxv1beta1.Sandbox)}, queue)
				},
			})
			require.NoError(t, err)
			cacheDone := make(chan error, 1)
			go func() { cacheDone <- reader.Start(ctx) }()
			defer func() {
				cancel()
				require.NoError(t, <-cacheDone)
			}()
			require.True(t, reader.WaitForCacheSync(ctx))
			waitUIDNotification(t, poolEvents, oldUID, "add")
			waitUIDNotification(t, sandboxEvents, member.UID, "add")
			request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(pool)}
			_, err = r.Reconcile(ctx, request)
			require.NoError(t, err)
			require.True(t, r.exp().IsPendingDeletion(warmPoolKey{NamespacedName: request.NamespacedName, UID: oldUID}, member.UID))
			require.Eventually(t, func() bool {
				observed := &sandboxv1beta1.Sandbox{}
				return reader.Get(ctx, client.ObjectKeyFromObject(member), observed) == nil && !observed.DeletionTimestamp.IsZero()
			}, 5*time.Second, 10*time.Millisecond)
			require.NoError(t, direct.Delete(ctx, pool))
			waitUIDNotification(t, poolEvents, oldUID, "delete")
			if reconcileMissing {
				_, err = r.Reconcile(ctx, request)
				require.NoError(t, err)
				require.True(t, r.exp().SatisfiedExpectations(warmPoolKey{NamespacedName: request.NamespacedName, UID: oldUID}))
			}
			replacement, _ := newOverCreationFixture(1)
			replacement.Namespace, replacement.UID = namespace, ""
			require.NoError(t, direct.Create(ctx, replacement))
			waitUIDNotification(t, poolEvents, replacement.UID, "add")
			require.NotEqual(t, oldUID, replacement.UID)
			require.Equal(t, 1, queue.Len(), "delete and recreate notifications share one reconcile key")
			item, shutdown := queue.Get()
			require.False(t, shutdown)
			result, err := r.Reconcile(ctx, item)
			queue.Done(item)
			require.NoError(t, err)
			countOwned := func() int {
				members := &sandboxv1beta1.SandboxList{}
				require.NoError(t, direct.List(ctx, members, client.InNamespace(namespace)))
				count := 0
				for i := range members.Items {
					if metav1.IsControlledBy(&members.Items[i], replacement) {
						count++
					}
				}
				return count
			}
			before := countOwned()
			t.Logf("API_PROBE missing_reconcile=%t old_uid=%s new_uid=%s new_members=%d requeue=%s", reconcileMissing, oldUID, replacement.UID, before, result.RequeueAfter)
			if before == 0 {
				require.Eventually(t, func() bool {
					observed := &extensionsv1beta1.SandboxWarmPool{}
					return reader.Get(ctx, request.NamespacedName, observed) == nil && observed.Status.ObservedGeneration == replacement.Generation
				}, 5*time.Second, 10*time.Millisecond)
				clock.Add(int64(expectationsTimeout + time.Second))
				_, err = r.Reconcile(ctx, request)
				require.NoError(t, err)
				t.Logf("API_PROBE after_expectation_timeout new_members=%d", countOwned())
				require.Equal(t, 1, countOwned())
			}
			retiring := &sandboxv1beta1.Sandbox{}
			require.NoError(t, direct.Get(ctx, client.ObjectKeyFromObject(member), retiring))
			require.False(t, retiring.DeletionTimestamp.IsZero())
			require.Equal(t, oldUID, metav1.GetControllerOf(retiring).UID)
			if before != 1 {
				t.Errorf("new pool created %d members before timeout, want 1", before)
			}
		})
	}
}

func TestWarmPoolUIDInformerOwner(t *testing.T) {
	for _, previousOwner := range []bool{true, false} {
		name := "current_owner"
		if previousOwner {
			name = "previous_owner"
		}
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			pool, template := newOverCreationFixture(1)
			pool.UID = "current-pool-uid"
			owner := pool.DeepCopy()
			if previousOwner {
				owner.UID = "old-pool-uid"
			}
			member := createPoolSandbox(pool.Name, pool.Namespace, sandboxcontrollers.NameHash(pool.Name), template, "-delayed")
			member.UID = "delayed-member-uid"
			scheme := newTestScheme()
			require.NoError(t, ctrl.SetControllerReference(owner, member, scheme))
			tracker := newWarmPoolExpectations()
			mapper := meta.NewDefaultRESTMapper([]schema.GroupVersion{extensionsv1beta1.GroupVersion})
			mapper.Add(extensionsv1beta1.GroupVersion.WithKind("SandboxWarmPool"), meta.RESTScopeNamespace)
			queue := workqueue.NewTypedRateLimitingQueue(workqueue.DefaultTypedControllerRateLimiter[reconcile.Request]())
			defer queue.ShutDown()
			h := &warmPoolSandboxEventHandler{
				EventHandler: handler.EnqueueRequestForOwner(scheme, mapper, &extensionsv1beta1.SandboxWarmPool{}, handler.OnlyControllerOwner()),
				expectations: tracker,
			}
			server := cachetesting.NewFakeControllerSource()
			defer server.Shutdown()
			informer := toolscache.NewSharedIndexInformer(server, &sandboxv1beta1.Sandbox{}, 0, toolscache.Indexers{})
			entered := make(chan struct{})
			release := make(chan struct{})
			observed := make(chan struct{})
			var once sync.Once
			unblock := func() { once.Do(func() { close(release) }) }
			_, err := informer.AddEventHandler(toolscache.ResourceEventHandlerFuncs{AddFunc: func(obj any) {
				close(entered)
				select {
				case <-release:
				case <-ctx.Done():
					return
				}
				h.Create(ctx, event.CreateEvent{Object: obj.(*sandboxv1beta1.Sandbox)}, queue)
				close(observed)
			}})
			require.NoError(t, err)
			done := make(chan struct{})
			go func() {
				informer.RunWithContext(ctx)
				close(done)
			}()
			defer func() {
				unblock()
				cancel()
				<-done
			}()
			require.True(t, toolscache.WaitForCacheSync(ctx.Done(), informer.HasSynced))
			server.Add(member)
			select {
			case <-entered:
			case <-ctx.Done():
				t.Fatal("informer callback did not start")
			}
			cached, exists, err := informer.GetIndexer().GetByKey(member.Namespace + "/" + member.Name)
			require.NoError(t, err)
			require.True(t, exists)
			require.Equal(t, owner.UID, metav1.GetControllerOf(cached.(*sandboxv1beta1.Sandbox)).UID)
			key := client.ObjectKeyFromObject(pool)
			require.True(t, tracker.TryExpectCreations(warmPoolKey{NamespacedName: key, UID: pool.UID}, 1))
			unblock()
			select {
			case <-observed:
			case <-ctx.Done():
				t.Fatal("informer callback did not finish")
			}
			require.Equal(t, 1, queue.Len())
			satisfied := tracker.SatisfiedExpectations(warmPoolKey{NamespacedName: key, UID: pool.UID})
			t.Logf("INFORMER_PROBE previous_owner=%t event_owner=%s current_owner=%s creation_satisfied=%t", previousOwner, owner.UID, pool.UID, satisfied)
			if satisfied == previousOwner {
				t.Errorf("creation satisfaction = %t, want %t", satisfied, !previousOwner)
			}
		})
	}
}
