"""Generic SOC proxy: make a remote composite resource look like a local one.

The proxy wraps the local XR in a provider-kubernetes Object that creates the
same XR on the service control plane, then mirrors the remote state back:
status, readiness, and the Secrets the remote XR composes. Secrets the local
spec references through `*SecretRef` fields are forwarded to the service.

Everything here works on plain dicts so it can be tested without the SDK. The
per-XRD constants (ProxyConfig name, name prefix, secret-ref paths) are
generated into config.py at build time from the XRD.
"""

import copy
import hashlib
from dataclasses import dataclass, field

OBJECT_API = "kubernetes.m.crossplane.io/v1alpha1"
PROXYCONFIG_API = "soc.upbound.io/v1alpha1"
PROXYCONFIG_KIND = "ProxyConfig"
LABEL_PREFIX = "soc.upbound.io/"

REMOTE_KEY = "remote"
PROXYCONFIG_REQ = "proxyconfig"

_LABEL_VALUE_MAX = 63


@dataclass
class Condition:
    """An XR condition the proxy sets, mirroring the remote one."""

    typ: str
    status: str
    reason: str
    message: str = ""


@dataclass
class Plan:
    """What the function should put in its response."""

    requirements: dict[str, dict] = field(default_factory=dict)
    desired: dict[str, dict] = field(default_factory=dict)
    ready: dict[str, bool] = field(default_factory=dict)
    status: dict | None = None
    conditions: list[Condition] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fatal: str | None = None


def short_hash(s: str, n: int = 10) -> str:
    """Return the first n hex characters of the sha256 of s."""
    return hashlib.sha256(s.encode()).hexdigest()[:n]


def remote_name(prefix: str, namespace: str, name: str) -> str:
    """The deterministic name of the remote XR for a local XR."""
    return f"{prefix}-{short_hash(f'{namespace}/{name}')}"


def dig(obj, *path, default=None):
    """Walk nested dicts, returning default when any step is missing."""
    for p in path:
        if not isinstance(obj, dict) or p not in obj:
            return default
        obj = obj[p]
    return obj


def condition(obj: dict | None, typ: str) -> dict:
    """Return the status condition of the given type, or an Unknown one."""
    for c in dig(obj, "status", "conditions", default=None) or []:
        if c.get("type") == typ:
            return c
    return {"type": typ, "status": "Unknown"}


def is_true(obj: dict | None, typ: str) -> bool:
    """True if the object reports the condition with status True."""
    return condition(obj, typ).get("status") == "True"


def origin_labels(xr: dict) -> dict:
    """Labels that record where a remote object came from."""
    meta = xr.get("metadata", {})
    labels = {}
    for key, value in (
        ("origin-namespace", meta.get("namespace")),
        ("origin-name", meta.get("name")),
        ("origin-uid", meta.get("uid")),
    ):
        if value and len(value) <= _LABEL_VALUE_MAX:
            labels[LABEL_PREFIX + key] = value
    return labels


def forwarded_labels(xr: dict) -> dict:
    """The local XR's own labels, minus the ones Crossplane manages."""
    labels = dig(xr, "metadata", "labels", default={}) or {}
    return {k: v for k, v in labels.items() if not k.startswith("crossplane.io/")}


def _object(name: str, manifest: dict, pc_ref: dict, **spec) -> dict:
    body = {
        "apiVersion": OBJECT_API,
        "kind": "Object",
        "metadata": {"name": name},
        "spec": {
            "providerConfigRef": pc_ref,
            "watch": True,
            "forProvider": {"manifest": manifest},
        },
    }
    body["spec"].update(spec)
    return body


def _ns(remote_ns: str | None) -> dict:
    # Without a namespace the manifest relies on provider-kubernetes defaulting
    # it from the kubeconfig context, which is where the design is heading.
    return {"namespace": remote_ns} if remote_ns else {}


def _manifest_of(observed: dict | None) -> dict | None:
    return dig(observed, "status", "atProvider", "manifest", default=None)


def local_secret_name(local: str, remote: str, secret: str) -> str:
    """Map a Secret composed by the remote XR to its local mirror's name."""
    if secret.startswith(remote):
        return local + secret[len(remote):]
    return f"{local}-{secret}"


def compose(xr: dict, observed: dict[str, dict], required: dict[str, list[dict]], cfg: dict) -> Plan:
    """Plan the desired state for one proxy XR.

    `observed` maps composition keys to observed composed resources, and
    `required` maps requirement names to the resources Crossplane fetched.
    A requirement missing from `required` has not been resolved yet.
    """
    plan = Plan()
    meta = xr.get("metadata", {})
    xr_name, xr_ns = meta.get("name", ""), meta.get("namespace", "")

    plan.requirements[PROXYCONFIG_REQ] = {
        "api_version": PROXYCONFIG_API,
        "kind": PROXYCONFIG_KIND,
        "match_name": cfg["proxyConfigName"],
    }

    spec = copy.deepcopy(xr.get("spec", {}))
    spec.pop("crossplane", None)
    root = {"spec": spec}

    rname = remote_name(cfg["namePrefix"], xr_ns, xr_name)

    # Every set *SecretRef needs the local Secret looked up, so declare those
    # requirements before deciding anything else.
    refs = []
    for path in cfg.get("secretRefPaths", []):
        ref = dig(root, *path)
        if not isinstance(ref, dict) or not ref.get("name"):
            continue
        field_name = path[-1].lower()
        if ref.get("namespace") and ref["namespace"] != xr_ns:
            plan.fatal = (
                f"{'.'.join(path)} references namespace {ref['namespace']!r}; "
                f"a proxy only forwards Secrets from its own namespace {xr_ns!r}"
            )
            return plan
        req_name = f"secretref-{field_name}"
        plan.requirements[req_name] = {
            "api_version": "v1",
            "kind": "Secret",
            "match_name": ref["name"],
            "namespace": xr_ns,
        }
        refs.append((path, field_name, ref, req_name))

    if PROXYCONFIG_REQ not in required:
        # First pass: Crossplane resolves the requirements and calls again.
        return plan

    pcs = required[PROXYCONFIG_REQ]
    if not pcs:
        plan.fatal = (
            f"{PROXYCONFIG_KIND} {cfg['proxyConfigName']!r} not found; "
            "create it to point this API at its service control plane"
        )
        return plan

    pc_spec = pcs[0].get("spec", {})
    pc_ref = dict(pc_spec.get("providerConfigRef") or {})
    if not pc_ref.get("name"):
        plan.fatal = f"{PROXYCONFIG_KIND} {cfg['proxyConfigName']!r} has no spec.providerConfigRef.name"
        return plan
    pc_ref.setdefault("kind", "ClusterProviderConfig")
    remote_ns = pc_spec.get("remoteNamespace")

    labels = {**forwarded_labels(xr), **origin_labels(xr)}
    remote_refs = []

    # Forward referenced Secrets, and point the remote spec at the copies.
    for path, field_name, ref, req_name in refs:
        local_name = ref["name"]
        secret_name = f"{rname}-{field_name}"
        ref["name"] = secret_name
        if "namespace" in ref:
            if remote_ns:
                ref["namespace"] = remote_ns
            else:
                del ref["namespace"]

        key = f"forward-{field_name}"
        found = required.get(req_name) or []
        if not found and key not in observed:
            # Nothing to forward, e.g. the service generates the Secret.
            continue
        stype = found[0].get("type", "Opaque") if found else dig(
            _manifest_of(observed.get(key)), "type", default="Opaque")
        manifest = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": secret_name, **_ns(remote_ns), "labels": dict(labels)},
            "type": stype,
        }
        plan.desired[key] = _object(
            secret_name,
            manifest,
            pc_ref,
            references=[{
                "patchesFrom": {
                    "apiVersion": "v1",
                    "kind": "Secret",
                    "name": local_name,
                    "namespace": xr_ns,
                    "fieldPath": "data",
                },
                "toFieldPath": "data",
            }],
        )
        plan.ready[key] = is_true(observed.get(key), "Ready")
        remote_refs.append({"dependsOn": {
            "apiVersion": OBJECT_API, "kind": "Object", "name": secret_name, "namespace": xr_ns}})

    manifest = {
        "apiVersion": xr.get("apiVersion"),
        "kind": xr.get("kind"),
        "metadata": {"name": rname, **_ns(remote_ns), "labels": labels},
        "spec": spec,
    }
    extra = {"readiness": {"policy": "DeriveFromObject"}}
    if remote_refs:
        extra["references"] = remote_refs
    plan.desired[REMOTE_KEY] = _object(rname, manifest, pc_ref, **extra)

    obs = observed.get(REMOTE_KEY)
    remote = _manifest_of(obs)
    plan.ready[REMOTE_KEY] = is_true(obs, "Ready")

    # Status and conditions come back from the remote XR.
    if remote:
        status = copy.deepcopy(remote.get("status") or {})
        status.pop("conditions", None)
        plan.status = status

    synced = condition(obs, "Synced")
    if obs is not None and synced.get("status") == "False":
        plan.conditions.append(Condition(
            "RemoteSynced", "False", "ProxyError", synced.get("message", "")))
    elif remote:
        rs = condition(remote, "Synced")
        plan.conditions.append(Condition(
            "RemoteSynced", rs.get("status", "Unknown"), rs.get("reason") or "Remote", rs.get("message", "")))
    if remote:
        rr = condition(remote, "Ready")
        plan.conditions.append(Condition(
            "RemoteReady", rr.get("status", "Unknown"), rr.get("reason") or "Remote", rr.get("message", "")))
    else:
        where = f"{remote_ns}/{rname}" if remote_ns else rname
        plan.conditions.append(Condition("RemoteReady", "Unknown", "Pending", f"waiting for remote {where}"))

    # Mirror every Secret the remote XR composes, so connection details
    # appear locally the same way they do on the service control plane.
    secrets = []
    if remote:
        for r in dig(remote, "spec", "crossplane", "resourceRefs", default=[]) or []:
            if r.get("kind") == "Secret" and r.get("apiVersion") == "v1" and r.get("name"):
                secrets.append(r["name"])
    else:
        # Remote state unknown: keep the mirrors we already have.
        for key, o in observed.items():
            if key.startswith("mirror-"):
                name = dig(o, "spec", "forProvider", "manifest", "metadata", "name")
                if name:
                    secrets.append(name)

    for secret in sorted(set(secrets)):
        local = local_secret_name(xr_name, rname, secret)
        mkey, skey = f"mirror-{local}", f"secret-{local}"
        plan.desired[mkey] = _object(
            f"{rname}-m-{short_hash(secret, 6)}",
            {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": secret, **_ns(remote_ns)}},
            pc_ref,
            managementPolicies=["Observe"],
        )
        plan.ready[mkey] = is_true(observed.get(mkey), "Ready")

        mirrored = _manifest_of(observed.get(mkey))
        data = dig(mirrored, "data")
        stype = dig(mirrored, "type")
        if data is None and skey in observed:
            # Keep the last copy rather than blank it while the mirror catches up.
            data, stype = dig(observed[skey], "data"), dig(observed[skey], "type")
        if data is None:
            continue
        plan.desired[skey] = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": local, "labels": origin_labels(xr)},
            "type": stype or "Opaque",
            "data": data,
        }
        plan.ready[skey] = True

    return plan
