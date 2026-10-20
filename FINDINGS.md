# Findings

Results from running this proof of concept: what ran, what it showed, and what
it didn't cover. "Not run" means it wasn't run.

## Environment
- **Upbound:** an Upbound Cloud Space, with both control planes in one group, on UXP `2.3.6-up.1` (Crossplane v2).
- **Network service:** `xpkg.upbound.io/upbound/configuration-aws-network:v2.2.0`, unchanged.
- **Proof-of-concept packages:** `xpkg.upbound.io/solutions/soc-poc-{proxy-base,network-proxy,database-service}:v0.1.0`.
- **AWS:** `us-west-2`, with providers authenticated by Upbound web identity (OIDC).
- **Date:** October 2026.

## Local (render and unit level)

| Check | Result |
|---|---|
| `make unit`: 37 unittest cases for the proxy logic, the SDK adapter and the generator | pass |
| `make lint` (ruff) | pass |
| `database-service`: `up test run`, 5 CompositionTests (by selector, by ref, sticky network, waiting, observed outputs) | pass. The waiting case was mutation-checked: removing `READY_FALSE` turns it red |
| Generated `network-proxy`: `up test run`, 2 CompositionTests (creates the remote Object, reads back status) | pass |
| `proxy-base`: `up test run`, 1 CompositionTest | pass |

## Live (`make verify`)

| Check | Result | Evidence |
|---|---|---|
| C1 Proxy create and read-back | pass | The remote Network `network-75561d364d` was created in the database CP's namespace on the network CP, by a robot identity (`UpboundTokens`). It carries the local label `network=a`, plus `origin-name` and `origin-namespace` |
| C2 Resolution and wiring | pass | A database applied before its Network was Ready reported `WaitingForNetwork`. Once the Network was Ready, its `status.vpcId` was copied back to the database CP. The database became Ready in the Network's VPC, on exactly `Network.status.privateSubnetIds`, with ingress from the Network's CIDR |
| C3 Update propagation | pass | A label added to the local Network reached the remote XR |
| C4 No AWS lookups | pass | The database function's only required-resource requests are the Network (by name or labels). No AWS query anywhere |
| C5 Failure modes | pass | **Invalid Network spec:** surfaced locally as `RemoteReady=False` with the service's message. **ProxyConfig deleted:** Fatal on the local XR, remote untouched. **Credential broken:** `RemoteSynced=False/ProxyError`, remote untouched, recovered once restored |
| C6 Deletion ordering | pass | Deleting the Network while a database used it was **refused** (`in-use by 3 usage(s)`). After the database was gone, Crossplane **replayed** the delete (`replayDeletion: true`). The VPC went last, with no `DependencyViolation` |
| C7 Isolation | pass | The same name in two namespaces mapped to two distinct remote names |
| C8 Contract guard | pass | `soc_proxy_gen --check` passes. The drift case is covered by unit tests |
| C9 Connection details in Python | pass | The RDS connection details reached the Python function. The composed `<db>-sql` Secret holds endpoint, host, port, username and password, is owned by the `SQLInstance`, and is listed in its `resourceRefs` |
| C10 Namespace-scoped consumer identity | not run | Upbound grants control-plane access per group (viewer, editor or admin), so the consumer's robot has the same role on every control plane in the group. Narrowing it with in-control-plane RBAC is still open |

## Things learned along the way

1. **AWS quota: DB subnet groups.** Each database needs a DB subnet group, and the AWS default is 50 per account. The test account was at that limit, so only one example database could be created. `DBS=db-b make verify` ran the checks with that one.
   - **Not exercised live:** the `autoGeneratePassword` path, and a database placed by label selector reaching Ready. The label-selector *resolution* was exercised.
2. **The Spaces API rejects `kubectl wait`.** Its filtered watch fails with `filtered HTTP handler wired to route that is either non-list…`. The scripts poll conditions instead.
3. **Deleting a namespace that holds proxy XRs can deadlock.** provider-kubernetes must create a ProviderConfigUsage to connect, and a terminating namespace forbids that, so the proxy's Object never finalizes even after the remote is gone. Delete proxy XRs before their namespace. The scripts never delete a namespace that holds them.
4. **Deletion order is two-phase.** Deleting a Network that is in use is refused at first. Once nothing uses it, Crossplane replays the delete. That's correct, but it surprises anyone who expects one step. Without `replayDeletion: true`, the Network would wait until it's deleted again.

## Go / no-go by design element

| Design element | Verdict | Notes |
|---|---|---|
| 1. Proxy generated at build time from the XRD alone (vendored Python function, no templating) | **go** | One CLI run, byte-identical XRD, single-step composition. Depends only on provider-kubernetes and proxy-base |
| 2. Proxy XR acts like a local one (labels, status, readiness, conditions) | **go** | Status, readiness, remote errors and credential errors all surface locally. Secret mirroring and `*SecretRef` forwarding are unit- and render-tested, but not live, because Network has no secrets |
| 3. ProxyConfig only points at the target | **go, with a change** | Works with `remoteNamespace`. Dropping that field needs provider-kubernetes to default an empty manifest namespace from the kubeconfig context |
| 4. Standard resolution and no AWS lookups | **go** | Selection by label and by name through required resources; the database reads only the Network's status and spec |
| 5. Deletion order by local Usages | **go** | Refused, then replayed. Mind the namespace-deletion caveat |
| 6. Typed build-time contract (a service depends on the generated proxy) | **go** | `database-service` pins `soc-poc-network-proxy:v0.1.0`; the Network API installs with it |

## Open questions and next steps
- Exercise Secret mirroring and forwarding live, with a consumer of the Database service that receives `<db>-sql`.
- Scope a consumer's identity to its own namespace on a service control plane (C10), or give each tenant group its own service control planes.
- Let provider-kubernetes take the remote namespace from the kubeconfig context, so `ProxyConfig` no longer needs `remoteNamespace`.
