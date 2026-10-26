# opensandbox-deploy

Deploys [OpenSandbox](https://github.com/opensandbox-group/OpenSandbox) **1.1.1** on Kubernetes:
on `techx-xora-nonprod-eks`, and on a local kind cluster shaped like it to try changes first.
This README is the hand-off to whoever deploys it on EKS. Owner: Nam Tran
(nam.tran@techxcorp.com).

## What it is

OpenSandbox runs short-lived sandbox pods for Xora services. A client asks the server for a
sandbox with an image and an egress allow-list, then runs commands and code in it. Each sandbox
pod adds two things to the client's image: `execd`, copied in by an init container, which runs
the commands, and an `egress` sidecar, which enforces the allow-list and can inject credentials
into outbound requests (the Credential Vault), so a token never enters the sandbox.

The first client is `xora-sandbox`, which runs `gh`, `git` and the AWS CLI for a tenant;
`xora-gateway` takes its place later. Clients bring their own sandbox images.

| Component | Kind | Namespace | Does |
|---|---|---|---|
| `opensandbox-server` | Deployment, 1 replica (HA is not supported upstream) | `opensandbox-system` | The API clients call. Creates a `BatchSandbox` per sandbox, proxies calls to `execd` and the sidecar. |
| `opensandbox-controller-manager` | Deployment, 1 replica | `opensandbox-system` | Turns each `BatchSandbox` into a pod, deletes it at its `expireTime`. |
| CRDs | `sandbox.opensandbox.io` (`batchsandboxes`, `pools`, `sandboxsnapshots`), `sandbox.fast.io` (4 kinds) | cluster | The fast-sandbox runtime is off, but the server's informers watch its kinds, so its CRDs are installed. |
| Sandbox pods | Pod per sandbox | `opensandbox` | The client's image (running `execd`) + the `egress` sidecar. |

Clients reach the server in the cluster at `http://opensandbox-server.opensandbox-system.svc`
(port 80, plain HTTP) with an API key in the header. It has no Ingress or load balancer and
should not get one: every call to a sandbox goes through the server.

## Images

All four are multi-arch (amd64, arm64) on Docker Hub. The cluster should pull them from the
account's ECR (`098640806013`), not Docker Hub (anonymous pull limits, `docs/base-images.md` in
`infra-xora-platform`). `k8s/values-eks.yaml` assumes the paths below; change it if they are
mirrored elsewhere. Copy the whole index (e.g. `crane copy`, `regctl image copy`) so the digest
stays the same.

| Image | Index digest | Proposed ECR repository |
|---|---|---|
| `docker.io/opensandbox/server:release-1.1.1` | `sha256:bcd5f264101262cebfa7e1ff9cd0424403e42f31a6596e4364cf90d8cb5c936b` | `opensandbox/server` |
| `docker.io/opensandbox/controller:release-1.1.1` | `sha256:aca5cda09a0fbc5867e6028f8b6aaa60fdcd78ebee634f71a263d45d5f6c2bab` | `opensandbox/controller` |
| `docker.io/opensandbox/execd:release-1.1.1` | `sha256:cda2bc35409bbf57561599b316cafb899598f7dce671332e7ceda14eb1cfeded` | `opensandbox/execd` |
| `docker.io/opensandbox/egress:release-1.1.1` | `sha256:bce2cc2e4dc3a3594678b91ac29efb0669fccebfdfc137727018d3b087fbb13a` | `opensandbox/egress` |

The image committer named in `k8s/values.yaml` is only for snapshots, which are not used; it
is never pulled.

The client's sandbox image (`xora-sandbox/toolbox` today) is built by the client's own CI and
must also be in a registry the sandbox nodes can pull from. The client names it at create.

## Cluster prerequisites

1. **Nodes for sandboxes.** `k8s/manifests.yaml` (ConfigMap `opensandbox-sandbox-template`)
   puts every sandbox pod on nodes labelled `workload: sandbox` and tolerates the taint
   `workload=sandbox:NoSchedule`. The cluster has only `general-arm64` today, so sandboxes stay
   `Pending` until that changes. **Recommendation: a separate node group, labelled and tainted
   as above**, because a sandbox runs whatever CLI commands its client sends, and a runaway one
   should not compete with api, worker or Temporal on `general-arm64`, which is already close
   to its CPU and branch-ENI limits (`docs/cluster-capacity.md`). The node group code in
   `infra-xora-platform` takes labels but no taints, so this needs an infra change. To run on
   `general-arm64` instead, remove `nodeSelector` and `tolerations` from that ConfigMap.
2. **Sizing.** A client that sets no resources gets the SDK default: a limit of 1 CPU and 2 GiB
   per sandbox and no request, so Kubernetes sets the request equal to the limit and reserves
1 CPU and 2 GiB per sandbox. On an
   m7g.xlarge (3920m allocatable, 480m of DaemonSets) that is three sandboxes per node.
   `xora-sandbox` keeps one sandbox per tenant + environment, alive 5 minutes after its last
   command. Server: 100m / 256Mi request; controller: 10m / 64Mi.
3. **NetworkPolicy enforcement.** Both policies in `k8s/manifests.yaml` need it; the VPC CNI
   add-on already has `enableNetworkPolicy: "true"`.
4. **Pod Security.** The egress sidecar adds `NET_ADMIN`, which the `baseline` and `restricted`
   levels refuse, so namespace `opensandbox` is labelled `enforce: privileged`. If an admission
   policy (Kyverno, Gatekeeper) also checks capabilities, `opensandbox` needs an exception for
   `NET_ADMIN` on the `egress` container.
5. **Egress.** Sandbox pods need outbound TCP 443 to the public hosts their clients allow:
   for `xora-sandbox`, `github.com`, `api.github.com` and the AWS API endpoints of a tenant's
   region. The smoke test also needs `postman-echo.com`. If egress from the VPC is filtered
   (the hub's firewall), these must be open. Each sandbox's sidecar narrows this further to its
   own allow-list.
6. **Client namespaces.** A namespace whose pods call the server gets the label
   `opensandbox.io/client=true` (NetworkPolicy `opensandbox-server`). Pods in `xora-dev` and
   `xora-stg` use their own security group (security groups for pods); that group must be
   allowed to reach the server pod on TCP 80, which runs with the node's security group.

## Permissions

**To install**: cluster-admin, or what ArgoCD already has. The chart creates CRDs,
ClusterRoles and ClusterRoleBindings, and `k8s/manifests.yaml` creates both namespaces.

**At run time**, the chart gives the two service accounts in `opensandbox-system`:

| Service account | Binding | Rules |
|---|---|---|
| `opensandbox-server` | Role + RoleBinding in `opensandbox` only | `pods`, `pods/status`, `pods/log`, `events`, `services`, `configmaps`: create, delete, get, list, patch, update, watch · `secrets`: create, delete, get (pull secrets for sandboxes whose client gives registry credentials) · `persistentvolumeclaims`: create, delete, get, list, patch · `batchsandboxes`, `sandboxsnapshots` (+ `/status`, `/finalizers`) and the `sandbox.opensandbox.io` pool kinds: all verbs · `sandbox.fast.io` `sandboxes`, `sandboxsnapshots`: get, list, watch · `sandboxtemplates`: create, delete, get, list, watch |
| `opensandbox-server` | ClusterRole `opensandbox-server-role-read` | `namespaces`, `runtimeclasses`: get, list |
| `opensandbox-controller-manager` | ClusterRole `opensandbox-manager-role` | `pods`, `events`: all verbs · `pods/status`: get, patch, update · `configmaps`: get, list, watch · `jobs` (+ `/status`) · `batchsandboxes`, `pools`, `sandboxsnapshots` (+ `/status`, `/finalizers`). **No `secrets`.** |
| `opensandbox-controller-manager` | Role `opensandbox-leader-election-role` in `opensandbox-system` | `configmaps`, `leases`: all verbs · `events`: create, patch |

The chart also creates nine unbound ClusterRoles (`batchsandbox-`, `pool-`,
`sandboxsnapshot-` × `admin`, `editor`, `viewer`) for granting people access to the CRDs.

**AWS IAM**: none. Neither service account uses Pod Identity or calls AWS. Nodes need only to
pull the images from ECR.

What differs from the upstream charts (`k8s/chart.patch`, already applied in `charts/`):

- The server's RBAC is a Role in `opensandbox`, plus read-only `namespaces` and
  `runtimeclasses`. Upstream grants pods and Secrets in every namespace.
- The controller cannot read Secrets. Upstream gives it get, list and watch on Secrets in every
  namespace, which only snapshots use.

## API key

The server reads its key from Secret `opensandbox-api-key`, key `api-key`, in
`opensandbox-system`. Every client sends the same value, so it lives in Secrets Manager and
reaches both the server and each client through the External Secrets Operator. Nothing in this
repo creates that Secret on a real cluster. Any long random string works
(`openssl rand -hex 32`).

## Deploy

The chart is vendored in `charts/` (umbrella `charts/opensandbox`, with local `file://`
dependencies on its sibling charts), so it installs from this repo with no download. It is the
official OpenSandbox chart: `manifests/charts` at tag
[`release-1.1.1`](https://github.com/opensandbox-group/OpenSandbox/tree/release-1.1.1/manifests/charts)
of the upstream repo, which publishes no Helm repository, with `k8s/chart.patch` applied. The
patch changes three files, all RBAC (see Permissions): `charts/server/templates/rbac.yaml`,
`charts/server/values.yaml` and `charts/controller/templates/clusterrole.yaml`. Every other file
is as upstream ships it; `make charts` rebuilds `charts/` from the tag and the patch (see
Changing the release).

Install only the umbrella chart, `charts/opensandbox`: it pulls in the others and turns three of
them off. The disabled ones stay in `charts/` only because the umbrella lists them as
dependencies, and `helm dependency build` fails without them.

| Chart | Deployed | Creates |
|---|---|---|
| `charts/opensandbox` | Yes: the one to install | Nothing itself; brings in the charts below |
| `charts/base` | Yes | The CRDs: `sandbox.opensandbox.io` and the `sandbox.fast.io` kinds the server watches; the nine unbound CRD access ClusterRoles |
| `charts/controller` | Yes | `opensandbox-controller-manager`: Deployment, service account, ClusterRole, leader-election Role |
| `charts/server` | Yes | `opensandbox-server`: Deployment, Service, service account, ConfigMap with `configToml`, Role in `opensandbox`, read-only ClusterRole |
| `charts/fast-sandbox` | No (`fast-sandbox.enabled: false`) | The fast-sandbox (Firecracker) control plane and node runtime: another runtime, not used |
| `charts/ingress-gateway` | No (`ingress-gateway.enabled: false`) | The OpenSandbox ingress gateway, not used: clients go through the server |
| `charts/node-agent` | No (`opensandbox-node-agent.enabled: false`) | A DaemonSet for node-level sandbox data collection, not used |

1. Mirror the images (above) and adjust `k8s/values-eks.yaml` if the paths differ.
2. Apply `k8s/manifests.yaml`: namespaces, NetworkPolicies, sandbox template.
3. Create the Secret `opensandbox-api-key` in `opensandbox-system` (above).
4. Install the chart, release `opensandbox`, namespace `opensandbox-system`, values
   `k8s/values.yaml` then `k8s/values-eks.yaml`:

   ```bash
   helm dependency build charts/opensandbox
   helm upgrade --install opensandbox charts/opensandbox -n opensandbox-system \
     -f k8s/values.yaml -f k8s/values-eks.yaml --wait --timeout 10m
   ```

   The same through the Makefile: `make install KUBE_CONTEXT=<context> VALUES=k8s/values-eks.yaml`
   (steps 2 and 4). `make render VALUES=k8s/values-eks.yaml` prints every manifest the cluster
   gets, for review or for a GitOps repo. With ArgoCD, an Application on `charts/opensandbox`
   with both values files works only if ArgoCD builds the chart's `file://` dependencies;
   otherwise commit the output of `make render` instead.
5. Label the client namespaces `opensandbox.io/client=true`.
6. Verify (below).

`[kubernetes].namespace` in `configToml` must equal `sandboxNamespace` (`opensandbox`): the
server's Role covers that namespace only.

## Verify

The deploy is done when all three pass, in this order. Run them from a clone of this repo on
a machine with `make`, `kubectl` and [`uv`](https://docs.astral.sh/uv/) (it fetches Python 3.11
and the OpenSandbox SDK itself), and `kubectl` access to the cluster (the EKS API is private:
from the VPC or through WARP). `SMOKE_IMAGE` is any image with `sh` and `curl` the nodes can
pull (the default, `curlimages/curl:8.10.1`, is on Docker Hub), and the sandbox nodes need
egress to `postman-echo.com`.

```bash
export OPENSANDBOX_SERVER_API_KEY="$(kubectl --context <context> -n opensandbox-system \
  get secret opensandbox-api-key -o jsonpath='{.data.api-key}' | base64 -d)"

make smoke    KUBE_CONTEXT=<context> SMOKE_IMAGE=<image>   # the server API and real sandboxes
make netcheck KUBE_CONTEXT=<context> SMOKE_IMAGE=<image>   # who may call the server
make check    KUBE_CONTEXT=<context>                       # RBAC and pod placement
```

**`make smoke`** reads the key from `OPENSANDBOX_SERVER_API_KEY` (above). It port-forwards the server to `127.0.0.1:8082`, calls the API the way the client does, and starts
two sandboxes. It takes about two minutes and ends with `18 of 18 passed`.

| Area | Checks |
|---|---|
| API | `/health` answers `200` with no key; `/v1/sandboxes` answers `401` with no key and with a wrong key, `200` with the key |
| Lifecycle | A new sandbox is `Running`; a renew moves its expiry later; a new handle connects to it by id and runs a command; a kill makes it `404`; a sandbox created with the shortest timeout (60 s) and never renewed is `404` within 90 s after its timeout |
| Commands | An argv command runs; a command's own `envs` are seen by that command and not by the next one |
| Egress | A host outside the allow-list is refused; a sandbox cannot reach `kubernetes.default.svc` even when its allow-list names it (NetworkPolicy `sandboxes`) |
| Credential Vault | Its header reaches an allowed host (`postman-echo.com` echoes it) and replaces one the sandbox sent itself; a PATCH reaches the next request; a command inside the sandbox gets `401` from the sidecar's own API |

The vault values are dummies. The SDK also logs a few lines while it runs (`retrying ...
/ping ... 502` while a sandbox starts, `Sandbox ... not found` after a kill): they are expected;
only the `PASS`/`FAIL` lines count.

**`make netcheck`** runs a curl pod against `http://opensandbox-server.opensandbox-system.svc/health`
from two namespaces: one labelled `opensandbox.io/client=true` must get `200`, one without the
label must get no answer. By default both namespaces are made for the check and deleted after.
`CLIENT_NS=xora-dev` runs the first pod in a real client namespace instead, which also checks
that its pod security group may reach the server.

**`make check`** prints which namespaces the two service accounts may read Secrets in (the
server: `opensandbox` only; the controller: none) and the node each pod runs on.

These prove the platform. The client's own image (its CLIs, the sidecar CA, its providers) is
the client's check: for xora-sandbox, `RUN_OPENSANDBOX_TESTS=1 make test` in that repo, pointed
at this server.

## Upgrade and remove

- Changing values: run step 4 again. A change to `configToml` replaces the server pod; running
  sandboxes keep running (their `BatchSandbox` objects stay), but client calls fail until the
  new pod is Ready. A change to the sandbox template ConfigMap (`k8s/manifests.yaml`) does not
  restart the server: restart it with `kubectl -n opensandbox-system rollout restart
  deployment/opensandbox-server`.
- `helm uninstall opensandbox -n opensandbox-system` keeps the CRDs (`helm.sh/resource-policy:
  keep`). Deleting the CRDs, or the namespace `opensandbox`, deletes every sandbox at once.

## What clients can count on

| | |
|---|---|
| Release | Server, `execd` and `egress` all 1.1.1. A client pins the Python SDK to the same release; an older `execd` rejects argv commands. |
| Access | One address and one API key. Clients use the server's proxy (`use_server_proxy=True`) for `execd` and sidecar calls too. |
| Egress | Sidecar in `dns+nft` mode: a sandbox reaches only the hosts its `networkPolicy` allows, and the NetworkPolicy `sandboxes` keeps it off private ranges (the cluster, the VPC, 100.64.0.0/10) even if the sidecar fails. |
| Credential Vault | On every sandbox created with `credentialProxy.enabled`. Injects a credential into matching requests and can be patched while the sandbox runs. |
| Sidecar CA | At `/opt/opensandbox/mitmproxy-ca-cert.pem`, named by `SSL_CERT_FILE`. Tools that ignore that variable (`git`, some `curl` builds) are the image owner's job. |
| Lifetime | `max_sandbox_timeout_seconds = 3600` caps the TTL given at create; renewals are not capped. A sandbox is gone at expiry. |
| Create time | `sandbox_create_timeout_seconds = 180`: a fresh node pulls three images before the pod is ready. |
| Per-command env | `RunCommandOpts.envs` is that command's own environment and the next command does not see it. |

## Known gaps

- **The controller manages pods in every namespace.** It watches cluster-wide; no option in
  1.1.1 to narrow it was found. It reads no Secrets.
- **One server replica.** Restarting it does not kill running sandboxes, but calls fail until
  it is back.
- **Sandbox pods have no security group of their own.** They run with the node's security
  group; the NetworkPolicy `sandboxes` is what keeps them off the VPC.
- kind (below) does not reproduce security groups for pods, EKS access entries, pulls from ECR
  or egress through the hub: those are first tried on the cluster itself.

## On kind (local)

A local cluster shaped like `techx-xora-nonprod-eks`, to try chart and values changes before
they go to EKS. Needs Docker Desktop, `kind`, `helm`, `kubectl` and `uv`. `cp .env.example .env`
and set `OPENSANDBOX_SERVER_API_KEY` first: on kind the Secret is made from it.

| Command | Does |
|---|---|
| `make kind-up` | `kind-cluster`, `kind-install`, `smoke`, `netcheck`, `check` |
| `make kind-cluster` | Cluster `osb-nonprod`: Kubernetes 1.35, three arm64 nodes (control plane, `workload: general`, `workload: sandbox` with the `NoSchedule` taint), Calico for NetworkPolicy |
| `make kind-install` | `k8s/manifests.yaml`, the API key Secret from `.env`, then the chart with `k8s/values-kind.yaml` (Docker Hub images) |
| `make forward` | Hold a port-forward to the server on `127.0.0.1:8081` for a client on this machine (`OPENSANDBOX_DOMAIN=127.0.0.1:8081`); Ctrl-C ends it |
| `make kind-load IMAGE=<name:tag>` | Copy an image from the local Docker onto the nodes: the cluster pulls only public images |
| `make kind-down` | Delete the cluster |

## Changing the release

`make charts` takes `manifests/charts` from tag `release-$(OSB_VERSION)` of the upstream repo,
applies `k8s/chart.patch` and replaces `charts/`. For a new release: change `OSB_VERSION` in
the Makefile and every `release-1.1.1` in `k8s/`, run `make charts`, fix the patch if it no
longer applies, review the diff of `charts/`, and run `make kind-up`.

## Layout

```
charts/                     the upstream release-1.1.1 charts with k8s/chart.patch applied
k8s/values.yaml             chart values for every cluster: controller and server, RBAC namespace
k8s/values-eks.yaml         techx-xora-nonprod-eks: ECR images, server config
k8s/values-kind.yaml        kind: Docker Hub images, server config
k8s/manifests.yaml          what the chart does not create: namespaces, NetworkPolicies, the
                            BatchSandbox template
k8s/chart.patch             changes to the upstream charts (RBAC)
k8s/kind.yaml               the kind cluster
smoke/platform_smoke.py     the platform smoke test (run by `uv`, no virtualenv to keep)
Makefile                    deps, render, install, smoke, netcheck, forward, check, charts,
                            kind-*
```
