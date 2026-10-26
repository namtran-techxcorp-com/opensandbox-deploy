"""Platform smoke test of this OpenSandbox server, as a client sees it. Needs no credential.

It checks what clients rely on, through the server's proxy (`use_server_proxy`) and with a
generic image: a sandbox starts and runs a command, egress outside the allow-list is refused,
the Credential Vault puts its header on a matching request and replaces one the sandbox sent
itself, a vault PATCH reaches the next request, a command inside the sandbox cannot use the
egress sidecar's own API, and a command's own `envs` are seen by that command only. The vault
values are dummies; an echo service shows the header it received.

curl is given the sidecar's MITM CA from SSL_CERT_FILE, which OpenSandbox sets in every
credential-proxy sandbox. Whether an image's own tools read that variable is for the image's
owner to check: some do not (git, curl builds with a fixed bundle).

    make smoke
"""

import asyncio
import os
import sys
from datetime import timedelta

from opensandbox import Sandbox
from opensandbox.config import ConnectionConfig
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


def _out(execution) -> str:
    text = "".join(part.text for part in execution.logs.stdout)
    errors = "".join(part.text for part in execution.logs.stderr)
    return (text + errors).strip()


def _credential(value: str) -> Credential:
    return Credential(name="dummy", source={"value": value})


async def _check(
    sandbox: Sandbox, name: str, command: str, ok, envs: dict[str, str] | None = None
) -> bool:
    execution = await sandbox.commands.run(
        ["sh", "-c", command], opts=RunCommandOpts(timeout=timedelta(seconds=30), envs=envs)
    )
    out = _out(execution)
    passed = ok(out, execution.exit_code)
    print(f"{'PASS' if passed else 'FAIL'}  {name}  (exit {execution.exit_code})")
    if not passed:
        print(f"      {out[:400]}")
    return passed


async def main() -> int:
    key = os.environ.get("OPENSANDBOX_SERVER_API_KEY", "")
    if not key:
        print("OPENSANDBOX_SERVER_API_KEY is empty: copy .env.example to .env")
        return 2
    config = ConnectionConfig(
        domain=os.environ.get("OPENSANDBOX_DOMAIN", "127.0.0.1:8081"),
        api_key=key,
        request_timeout=timedelta(seconds=60),
        use_server_proxy=True,
    )
    sandbox = await Sandbox.create(
        os.environ.get("SMOKE_IMAGE", "curlimages/curl:8.10.1"),
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
    try:
        print(f"sandbox {sandbox.id} RUNNING")
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
    finally:
        await sandbox.kill()
        await sandbox.close()
        print(f"sandbox {sandbox.id} killed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
