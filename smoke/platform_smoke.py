"""Platform smoke test of this OpenSandbox server, as a client sees it. Needs no credential.

It calls the API the way xora-sandbox does (through the server's proxy, `use_server_proxy`)
with a generic image, and checks what clients rely on:

- the API: `/health` answers without a key, `/v1` refuses a request with no key or a wrong one;
- the lifecycle: a sandbox starts Running, a new handle connects to it by id, a renew moves its
  expiry later, a kill removes it, and a sandbox nobody renews is gone after its timeout;
- commands: an argv command runs, and a command's own `envs` are seen by that command only;
- egress: a host outside the allow-list is refused, and a sandbox cannot reach the cluster even
  when its allow-list names a cluster host (the NetworkPolicy `sandboxes`, not the sidecar);
- the Credential Vault: its header reaches an allowed host and replaces one the sandbox sent
  itself, a vault PATCH reaches the next request, and a command inside the sandbox cannot use
  the egress sidecar's own API.

The vault values are dummies; an echo service shows the header it received. curl is given the
sidecar's MITM CA from SSL_CERT_FILE, which OpenSandbox sets in every credential-proxy sandbox.
Whether an image's own tools read that variable is for the image's owner to check: some do not
(git, curl builds with a fixed bundle).

    make smoke
"""

import asyncio
import os
import sys
import time
from datetime import timedelta

import httpx
from opensandbox import Sandbox, SandboxManager
from opensandbox.config import ConnectionConfig
from opensandbox.exceptions import SandboxApiException
from opensandbox.models.execd import RunCommandOpts
from opensandbox.models.sandboxes import (
    Credential,
    CredentialBinding,
    CredentialMutationSet,
    CredentialProxyConfig,
    NetworkPolicy,
    NetworkRule,
)

ECHO_HOST = "postman-echo.com"
ECHO = f"https://{ECHO_HOST}/headers"
FIRST, SECOND = "smoke-dummy-1", "smoke-dummy-2"
CURL = 'curl -sS --cacert "$SSL_CERT_FILE"'
#: The egress sidecar shares the sandbox's network namespace; its API answers on this port.
SIDECAR_API = "http://127.0.0.1:18080/credential-vault"
#: A host inside the cluster: the API server, on a private address.
CLUSTER_HOST = "kubernetes.default.svc.cluster.local"
#: OpenSandbox refuses a shorter timeout at create.
SHORT_TIMEOUT = timedelta(seconds=60)
API_KEY_HEADER = "OPEN-SANDBOX-API-KEY"


def _out(execution) -> str:
    text = "".join(part.text for part in execution.logs.stdout)
    errors = "".join(part.text for part in execution.logs.stderr)
    return (text + errors).strip()


def _credential(value: str) -> Credential:
    return Credential(name="dummy", source={"value": value})


def _report(name: str, passed: bool, detail: str = "") -> bool:
    print(f"{'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    return passed


async def _check(
    sandbox: Sandbox, name: str, command: str, ok, envs: dict[str, str] | None = None
) -> bool:
    execution = await sandbox.commands.run(
        ["sh", "-c", command], opts=RunCommandOpts(timeout=timedelta(seconds=30), envs=envs)
    )
    out = _out(execution)
    passed = _report(name, ok(out, execution.exit_code), f"exit {execution.exit_code}")
    if not passed:
        print(f"      {out[:400]}")
    return passed


async def _status(manager: SandboxManager, sandbox_id: str) -> int | str:
    """The sandbox's state, or the HTTP status the server answered instead."""
    try:
        return (await manager.get_sandbox_info(sandbox_id)).status.state
    except SandboxApiException as exc:
        return exc.status_code or 0


async def _api_checks(domain: str, key: str) -> list[bool]:
    async with httpx.AsyncClient(base_url=f"http://{domain}", timeout=10) as http:
        health = await http.get("/health")
        no_key = await http.get("/v1/sandboxes")
        wrong_key = await http.get("/v1/sandboxes", headers={API_KEY_HEADER: key + "-wrong"})
        right_key = await http.get("/v1/sandboxes", headers={API_KEY_HEADER: key})
    return [
        _report(
            "/health answers without a key", health.status_code == 200, f"{health.status_code}"
        ),
        _report("/v1 refuses no key", no_key.status_code == 401, f"{no_key.status_code}"),
        _report(
            "/v1 refuses a wrong key", wrong_key.status_code == 401, f"{wrong_key.status_code}"
        ),
        _report("/v1 accepts the key", right_key.status_code == 200, f"{right_key.status_code}"),
    ]


async def _main_sandbox(
    image: str, config: ConnectionConfig, manager: SandboxManager
) -> list[bool]:
    sandbox = await Sandbox.create(
        image,
        timeout=timedelta(minutes=5),
        ready_timeout=timedelta(seconds=120),
        metadata={"purpose": "smoke"},
        network_policy=NetworkPolicy(
            defaultAction="deny", egress=[NetworkRule(action="allow", target=ECHO_HOST)]
        ),
        credential_proxy=CredentialProxyConfig(enabled=True),
        connection_config=config,
    )
    results: list[bool] = []
    killed = False
    try:
        print(f"sandbox {sandbox.id} created")
        info = await manager.get_sandbox_info(sandbox.id)
        results.append(
            _report("the sandbox is Running", info.status.state == "Running", info.status.state)
        )
        renewed = await manager.renew_sandbox(sandbox.id, timedelta(minutes=10))
        results.append(
            _report(
                "a renew moves the expiry later",
                info.expires_at is not None and renewed.expires_at > info.expires_at,
                f"{info.expires_at:%H:%M:%S} -> {renewed.expires_at:%H:%M:%S}",
            )
        )
        await sandbox.credential_vault.create(
            credentials=[_credential(FIRST)],
            bindings=[
                CredentialBinding(
                    name="echo",
                    match={
                        "schemes": ["https"],
                        "hosts": [ECHO_HOST],
                        "methods": ["GET"],
                        "paths": ["/headers"],
                    },
                    auth={"type": "bearer", "credential": "dummy"},
                )
            ],
        )
        results += [
            await _check(sandbox, "a command runs", "echo ok", lambda out, code: out == "ok"),
            await _check(
                sandbox,
                "egress outside the allow-list is refused",
                "curl -sS -m 5 -o /dev/null https://example.com",
                lambda out, code: code not in (0, None),
            ),
            await _check(
                sandbox,
                "the vault header reaches an allowed host",
                f"{CURL} -m 15 {ECHO}",
                lambda out, code: f"Bearer {FIRST}" in out,
            ),
            await _check(
                sandbox,
                "the vault header replaces the sandbox's own",
                f"{CURL} -m 15 -H 'Authorization: Bearer from-the-sandbox' {ECHO}",
                lambda out, code: f"Bearer {FIRST}" in out and "from-the-sandbox" not in out,
            ),
            await _check(
                sandbox,
                "the sidecar API refuses the sandbox",
                f"curl -sS -m 5 -o /dev/null -w '%{{http_code}}' {SIDECAR_API}",
                lambda out, code: out == "401",
            ),
        ]
        await sandbox.credential_vault.patch(
            credentials=CredentialMutationSet(replace=[_credential(SECOND)])
        )
        results.append(
            await _check(
                sandbox,
                "a vault PATCH reaches the next request",
                f"{CURL} -m 15 {ECHO}",
                lambda out, code: f"Bearer {SECOND}" in out and FIRST not in out,
            )
        )
        results += [
            await _check(
                sandbox,
                "a command sees its own envs",
                "echo $SMOKE_ENV",
                lambda out, code: out == "per-command",
                envs={"SMOKE_ENV": "per-command"},
            ),
            await _check(
                sandbox,
                "the next command does not see them",
                "echo ${SMOKE_ENV:-unset}",
                lambda out, code: out == "unset",
            ),
        ]
        again = await Sandbox.connect(sandbox.id, connection_config=config)
        try:
            results.append(
                await _check(
                    again,
                    "a new handle connects by id",
                    "echo again",
                    lambda out, c: out == "again",
                )
            )
        finally:
            await again.close()
        await manager.kill_sandbox(sandbox.id)
        killed = True
        status = await _status(manager, sandbox.id)
        results.append(_report("a kill removes the sandbox", status == 404, f"{status}"))
    finally:
        if not killed:
            await sandbox.kill()
        await sandbox.close()
    return results


async def _short_sandbox(
    image: str, config: ConnectionConfig, manager: SandboxManager
) -> list[bool]:
    """A sandbox with the shortest timeout, no vault, and a cluster host on its allow-list."""
    sandbox = await Sandbox.create(
        image,
        timeout=SHORT_TIMEOUT,
        ready_timeout=timedelta(seconds=55),
        metadata={"purpose": "smoke"},
        network_policy=NetworkPolicy(
            defaultAction="deny", egress=[NetworkRule(action="allow", target=CLUSTER_HOST)]
        ),
        connection_config=config,
    )
    started = time.monotonic()
    print(f"sandbox {sandbox.id} created, timeout {SHORT_TIMEOUT.seconds} s")
    try:
        results = [
            await _check(
                sandbox,
                "the cluster is out of reach even on the allow-list",
                f"curl -sk -m 5 -o /dev/null -w '%{{http_code}}' https://{CLUSTER_HOST}",
                lambda out, code: out == "000" and code not in (0, None),
            )
        ]
    finally:
        await sandbox.close()
    status: int | str = "Running"
    while time.monotonic() - started < SHORT_TIMEOUT.seconds + 90:
        status = await _status(manager, sandbox.id)
        if status == 404:
            break
        await asyncio.sleep(5)
    waited = round(time.monotonic() - started)
    results.append(
        _report("an unrenewed sandbox is gone after its timeout", status == 404, f"{waited} s")
    )
    if status != 404:
        await manager.kill_sandbox(sandbox.id)
    return results


async def main() -> int:
    key = os.environ.get("OPENSANDBOX_SERVER_API_KEY", "")
    if not key:
        print("OPENSANDBOX_SERVER_API_KEY is empty: copy .env.example to .env")
        return 2
    domain = os.environ.get("OPENSANDBOX_DOMAIN", "127.0.0.1:8081")
    image = os.environ.get("SMOKE_IMAGE", "curlimages/curl:8.10.1")
    config = ConnectionConfig(
        domain=domain,
        api_key=key,
        request_timeout=timedelta(seconds=60),
        use_server_proxy=True,
    )
    results = await _api_checks(domain, key)
    manager = await SandboxManager.create(connection_config=config)
    try:
        results += await _main_sandbox(image, config, manager)
        results += await _short_sandbox(image, config, manager)
    finally:
        await manager.close()
    print(f"{results.count(True)} of {len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
