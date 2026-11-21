"""Check stale handles against an isolated Kubernetes API server.

Claim binding is a status fixture. Controllers, Pods and garbage collection
are outside this probe; API reads and UID-preconditioned deletes are real.
"""

import asyncio
import inspect
import json
import logging
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time

import requests
import urllib3
import yaml
from kubernetes import client
from kubernetes_asyncio import client as async_client

from k8s_agent_sandbox.async_sandbox_client import AsyncSandboxClient
from k8s_agent_sandbox.models import SandboxDirectConnectionConfig
from k8s_agent_sandbox.sandbox_client import SandboxClient

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "_artifacts"
ASSETS = Path(os.environ["KUBEBUILDER_ASSETS"])
CLAIMS = "/apis/extensions.agents.x-k8s.io/v1beta1/namespaces/default/sandboxclaims"
SANDBOXES = "/apis/agents.x-k8s.io/v1beta1/namespaces/default/sandboxes"
RESULTS = []
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def invoke(method, *args, **kwargs):
    result = method(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def record(**result):
    RESULTS.append(result)
    print(json.dumps(result), flush=True)


async def probe(request, host, token):
    def create_claim(name):
        claim = request("POST", CLAIMS, {
            "apiVersion": "extensions.agents.x-k8s.io/v1beta1", "kind": "SandboxClaim",
            "metadata": {"name": name}, "spec": {"warmPoolRef": {"name": "probe-pool"}},
        })
        request("PATCH", f"{CLAIMS}/{name}/status", {"status": {
            "sandbox": {"name": name}, "conditions": [{
                "type": "Ready", "status": "True", "reason": "FixtureReady",
                "message": "", "lastTransitionTime": "2026-11-21T00:00:00Z",
                "observedGeneration": claim["metadata"]["generation"],
            }],
        }})
        return claim["metadata"]["uid"]

    for mode in ("sync", "async"):
        config_class = client.Configuration if mode == "sync" else async_client.Configuration
        api_class = client.ApiClient if mode == "sync" else async_client.ApiClient
        config = config_class(host=host)
        config.verify_ssl = False
        config.api_key["authorization" if mode == "sync" else "BearerToken"] = f"Bearer {token}"
        api = api_class(config)
        sdk_class = SandboxClient if mode == "sync" else AsyncSandboxClient
        sdk = sdk_class(api_client=api, cleanup=False,
                        connection_config=SandboxDirectConnectionConfig(api_url="http://127.0.0.1:9"))
        try:
            for method in ("get", "adopt", "get-after-refresh"):
                name = f"{mode}-{method}"
                request("POST", SANDBOXES, {
                    "apiVersion": "agents.x-k8s.io/v1beta1", "kind": "Sandbox",
                    "metadata": {"name": name},
                    "spec": {"podTemplate": {"spec": {"containers": [{
                        "name": "app", "image": "registry.k8s.io/pause:3.10",
                    }]}}},
                })
                old_uid = create_claim(name)
                if method == "adopt":
                    stale = await invoke(sdk.create_sandbox, "probe-pool", claim_name=name,
                                         adopt_existing=True, sandbox_ready_timeout=3)
                else:
                    stale = await invoke(sdk.get_sandbox, name)
                request("DELETE", f"{CLAIMS}/{name}")
                new_uid = create_claim(name)
                assert old_uid != new_uid
                current = None
                if method == "get-after-refresh":
                    current = await invoke(sdk.get_sandbox, name)
                    assert current is not stale and current.sandbox_id == stale.sandbox_id
                for attempt in range(2):
                    try:
                        await invoke(stale.terminate)
                    except (client.ApiException, async_client.ApiException) as error:
                        assert error.status == 409
                    else:
                        raise AssertionError("A stale handle deleted its replacement")
                    assert request("GET", f"{CLAIMS}/{name}")["metadata"]["uid"] == new_uid
                record(case=name, old_uid=old_uid, replacement_uid=new_uid,
                       replacement_preserved=True, rejected_attempts=2)
                if current is None:
                    current = await invoke(sdk.get_sandbox, name)
                await invoke(current.terminate)
                response = request("GET", f"{CLAIMS}/{name}", allow_missing=True)
                assert response is None
                record(case=name + "-matching-uid", deleted=True)
        finally:
            if mode == "async":
                await sdk.close()
                await api.close()
            else:
                api.close()


def main():
    processes, logs = [], []
    with tempfile.TemporaryDirectory(prefix="uid-api-") as directory:
        directory = Path(directory)
        token = secrets.token_hex(32)
        (directory / "tokens.csv").write_text(f"{token},probe,probe,system:masters\n")
        subprocess.run(["openssl", "genrsa", "-out", str(directory / "key.pem"), "2048"],
                       capture_output=True, check=True)
        etcd_port, peer_port, api_port = free_port(), free_port(), free_port()

        def start(name, args):
            log = (ARTIFACTS / f"{name}.log").open("w")
            logs.append(log)
            processes.append(subprocess.Popen([str(ASSETS / name), *args],
                                               stdout=log, stderr=subprocess.STDOUT))

        try:
            start("etcd", ["--data-dir", str(directory / "etcd"),
                "--listen-client-urls", f"http://127.0.0.1:{etcd_port}",
                "--advertise-client-urls", f"http://127.0.0.1:{etcd_port}",
                "--listen-peer-urls", f"http://127.0.0.1:{peer_port}",
                "--initial-advertise-peer-urls", f"http://127.0.0.1:{peer_port}",
                "--initial-cluster", f"default=http://127.0.0.1:{peer_port}"])
            start("kube-apiserver", ["--etcd-servers", f"http://127.0.0.1:{etcd_port}",
                "--bind-address=127.0.0.1", "--advertise-address=127.0.0.1",
                "--endpoint-reconciler-type=none", f"--secure-port={api_port}",
                "--service-cluster-ip-range=10.42.0.0/24", "--authorization-mode=AlwaysAllow",
                "--anonymous-auth=false", f"--token-auth-file={directory / 'tokens.csv'}",
                f"--cert-dir={directory / 'certs'}", f"--service-account-key-file={directory / 'key.pem'}",
                f"--service-account-signing-key-file={directory / 'key.pem'}",
                "--service-account-issuer=https://uid-probe.local"])
            host = f"https://127.0.0.1:{api_port}"
            with requests.Session() as session:
                session.verify = False
                session.headers["Authorization"] = f"Bearer {token}"
                for _ in range(300):
                    if any(process.poll() is not None for process in processes):
                        raise RuntimeError("Isolated control plane exited")
                    try:
                        if session.get(host + "/readyz", timeout=1).status_code == 200:
                            break
                    except requests.RequestException:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError("Isolated control plane startup timed out")

                def request(method, path, body=None, allow_missing=False):
                    response = session.request(method, host + path, json=body, timeout=10,
                        headers={"Content-Type": "application/merge-patch+json"} if method == "PATCH" else {})
                    if allow_missing and response.status_code == 404:
                        return None
                    response.raise_for_status()
                    return response.json()

                for group, plural in (("agents.x-k8s.io", "sandboxes"),
                                      ("extensions.agents.x-k8s.io", "sandboxclaims")):
                    crd = yaml.safe_load((ROOT / f"k8s/crds/{group}_{plural}.yaml").read_text())
                    request("POST", "/apis/apiextensions.k8s.io/v1/customresourcedefinitions", crd)
                    path = f"/apis/apiextensions.k8s.io/v1/customresourcedefinitions/{plural}.{group}"
                    for _ in range(100):
                        if any(c["type"] == "Established" and c["status"] == "True"
                               for c in request("GET", path).get("status", {}).get("conditions", [])):
                            break
                        time.sleep(0.1)
                    else:
                        raise RuntimeError(f"CRD {plural} did not become Established")
                asyncio.run(probe(request, host, token))
                assert len(RESULTS) == 12
                (ARTIFACTS / "real-api.json").write_text(json.dumps(RESULTS, indent=2) + "\n")
        finally:
            for process in reversed(processes):
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for log in logs:
                log.close()


if __name__ == "__main__":
    logging.disable(logging.CRITICAL)
    main()
