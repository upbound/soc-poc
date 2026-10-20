"""Unit tests for the generic proxy logic (no SDK needed)."""

import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy_function"))

from function import proxy

CFG = {
    "proxyConfigName": "sqlinstances.aws.platform.upbound.io",
    "namePrefix": "sqlinstance",
    "secretRefPaths": [["spec", "parameters", "passwordSecretRef"]],
}
PC = {
    "apiVersion": "soc.upbound.io/v1alpha1",
    "kind": "ProxyConfig",
    "metadata": {"name": "sqlinstances.aws.platform.upbound.io"},
    "spec": {"providerConfigRef": {"name": "database-service-cp"}, "remoteNamespace": "ctp-a"},
}
RNAME = "sqlinstance-" + hashlib.sha256(b"team-a/db-a").hexdigest()[:10]


def xr(**params):
    return {
        "apiVersion": "aws.platform.upbound.io/v1alpha1",
        "kind": "SQLInstance",
        "metadata": {
            "name": "db-a",
            "namespace": "team-a",
            "uid": "1234",
            "labels": {"team": "a", "crossplane.io/composite": "db-a"},
        },
        "spec": {
            "parameters": {"region": "us-west-2", "engine": "postgres", **params},
            "crossplane": {"compositionRef": {"name": "x"}, "resourceRefs": []},
        },
    }


def observed_remote(ready="True", synced="True", remote_status=None, resource_refs=None, obj_synced="True"):
    manifest = {
        "apiVersion": "aws.platform.upbound.io/v1alpha1",
        "kind": "SQLInstance",
        "metadata": {"name": RNAME, "namespace": "ctp-a"},
        "spec": {"crossplane": {"resourceRefs": resource_refs or []}},
        "status": {
            **(remote_status or {}),
            "conditions": [
                {"type": "Ready", "status": ready, "reason": "Creating", "message": "rds pending"},
                {"type": "Synced", "status": synced, "reason": "ReconcileSuccess"},
            ],
        },
    }
    return {
        "status": {
            "conditions": [
                {"type": "Ready", "status": ready},
                {"type": "Synced", "status": obj_synced, "message": "cannot connect" if obj_synced == "False" else ""},
            ],
            "atProvider": {"manifest": manifest},
        },
    }


class FirstPass(unittest.TestCase):
    def test_requests_proxyconfig_and_composes_nothing(self):
        plan = proxy.compose(xr(), {}, {}, CFG)
        self.assertIsNone(plan.fatal)
        self.assertEqual(plan.desired, {})
        self.assertEqual(plan.requirements["proxyconfig"], {
            "api_version": "soc.upbound.io/v1alpha1",
            "kind": "ProxyConfig",
            "match_name": "sqlinstances.aws.platform.upbound.io",
        })

    def test_requests_referenced_secret_in_own_namespace(self):
        plan = proxy.compose(xr(passwordSecretRef={"name": "pw", "key": "password"}), {}, {}, CFG)
        self.assertEqual(plan.requirements["secretref-passwordsecretref"], {
            "api_version": "v1", "kind": "Secret", "match_name": "pw", "namespace": "team-a"})

    def test_missing_proxyconfig_is_fatal(self):
        plan = proxy.compose(xr(), {}, {"proxyconfig": []}, CFG)
        self.assertIn("not found", plan.fatal)
        self.assertEqual(plan.desired, {})

    def test_proxyconfig_without_providerconfig_is_fatal(self):
        bad = {**PC, "spec": {"remoteNamespace": "ctp-a"}}
        plan = proxy.compose(xr(), {}, {"proxyconfig": [bad]}, CFG)
        self.assertIn("providerConfigRef", plan.fatal)

    def test_cross_namespace_secret_ref_is_fatal(self):
        plan = proxy.compose(xr(passwordSecretRef={"name": "pw", "namespace": "other"}), {}, {}, CFG)
        self.assertIn("own namespace", plan.fatal)


class RemoteObject(unittest.TestCase):
    def setUp(self):
        self.plan = proxy.compose(xr(storageGB=5), {}, {"proxyconfig": [PC]}, CFG)
        self.obj = self.plan.desired["remote"]
        self.manifest = self.obj["spec"]["forProvider"]["manifest"]

    def test_object_targets_service_cp(self):
        self.assertEqual(self.obj["apiVersion"], "kubernetes.m.crossplane.io/v1alpha1")
        self.assertEqual(self.obj["metadata"]["name"], RNAME)
        self.assertEqual(self.obj["spec"]["providerConfigRef"],
                         {"name": "database-service-cp", "kind": "ClusterProviderConfig"})
        self.assertEqual(self.obj["spec"]["readiness"], {"policy": "DeriveFromObject"})
        self.assertTrue(self.obj["spec"]["watch"])

    def test_manifest_is_the_same_api(self):
        self.assertEqual(self.manifest["apiVersion"], "aws.platform.upbound.io/v1alpha1")
        self.assertEqual(self.manifest["kind"], "SQLInstance")
        self.assertEqual(self.manifest["metadata"]["name"], RNAME)
        self.assertEqual(self.manifest["metadata"]["namespace"], "ctp-a")

    def test_spec_forwarded_without_crossplane_machinery(self):
        self.assertEqual(self.manifest["spec"], {
            "parameters": {"region": "us-west-2", "engine": "postgres", "storageGB": 5}})

    def test_labels_forwarded_with_origin(self):
        self.assertEqual(self.manifest["metadata"]["labels"], {
            "team": "a",
            "soc.upbound.io/origin-namespace": "team-a",
            "soc.upbound.io/origin-name": "db-a",
            "soc.upbound.io/origin-uid": "1234",
        })

    def test_not_ready_until_observed(self):
        self.assertFalse(self.plan.ready["remote"])
        self.assertIsNone(self.plan.status)
        self.assertEqual([(c.typ, c.status, c.reason) for c in self.plan.conditions],
                         [("RemoteReady", "Unknown", "Pending")])

    def test_remote_name_differs_per_namespace(self):
        other = xr()
        other["metadata"]["namespace"] = "team-b"
        plan = proxy.compose(other, {}, {"proxyconfig": [PC]}, CFG)
        self.assertNotEqual(plan.desired["remote"]["metadata"]["name"], RNAME)

    def test_without_remote_namespace_the_manifest_has_none(self):
        pc = {**PC, "spec": {"providerConfigRef": {"name": "x"}}}
        plan = proxy.compose(xr(), {}, {"proxyconfig": [pc]}, CFG)
        self.assertNotIn("namespace", plan.desired["remote"]["spec"]["forProvider"]["manifest"]["metadata"])


class ReadBack(unittest.TestCase):
    def test_status_copied_without_conditions(self):
        obs = {"remote": observed_remote(remote_status={"endpoint": "db.example", "port": 5432})}
        plan = proxy.compose(xr(), obs, {"proxyconfig": [PC]}, CFG)
        self.assertEqual(plan.status, {"endpoint": "db.example", "port": 5432})
        self.assertTrue(plan.ready["remote"])

    def test_remote_conditions_surface_locally(self):
        obs = {"remote": observed_remote(ready="False")}
        plan = proxy.compose(xr(), obs, {"proxyconfig": [PC]}, CFG)
        conds = {c.typ: c for c in plan.conditions}
        self.assertEqual((conds["RemoteReady"].status, conds["RemoteReady"].message), ("False", "rds pending"))
        self.assertEqual(conds["RemoteSynced"].status, "True")
        self.assertFalse(plan.ready["remote"])

    def test_object_sync_error_wins(self):
        obs = {"remote": observed_remote(obj_synced="False")}
        plan = proxy.compose(xr(), obs, {"proxyconfig": [PC]}, CFG)
        conds = {c.typ: c for c in plan.conditions}
        self.assertEqual((conds["RemoteSynced"].status, conds["RemoteSynced"].reason,
                          conds["RemoteSynced"].message), ("False", "ProxyError", "cannot connect"))


MIRROR_REFS = [{"apiVersion": "v1", "kind": "Secret", "name": f"{RNAME}-sql"},
               {"apiVersion": "rds.aws.m.upbound.io/v1beta1", "kind": "Instance", "name": "x"}]


class SecretMirror(unittest.TestCase):
    refs = MIRROR_REFS

    def test_mirror_object_for_each_composed_secret(self):
        obs = {"remote": observed_remote(resource_refs=self.refs)}
        plan = proxy.compose(xr(), obs, {"proxyconfig": [PC]}, CFG)
        mirror = plan.desired["mirror-db-a-sql"]
        self.assertEqual(mirror["spec"]["managementPolicies"], ["Observe"])
        self.assertEqual(mirror["spec"]["forProvider"]["manifest"],
                         {"apiVersion": "v1", "kind": "Secret",
                          "metadata": {"name": f"{RNAME}-sql", "namespace": "ctp-a"}})
        # No data observed yet, so no local Secret.
        self.assertNotIn("secret-db-a-sql", plan.desired)
        self.assertEqual(sorted(k for k in plan.desired if k.startswith("mirror-")), ["mirror-db-a-sql"])

    def test_local_secret_from_mirrored_data(self):
        mirrored = {"status": {"conditions": [{"type": "Ready", "status": "True"}], "atProvider": {
            "manifest": {"apiVersion": "v1", "kind": "Secret", "type": "connection.crossplane.io/v1alpha1",
                         "data": {"password": "cA=="}}}}}
        obs = {"remote": observed_remote(resource_refs=self.refs), "mirror-db-a-sql": mirrored}
        plan = proxy.compose(xr(), obs, {"proxyconfig": [PC]}, CFG)
        secret = plan.desired["secret-db-a-sql"]
        self.assertEqual(secret["metadata"]["name"], "db-a-sql")
        self.assertEqual(secret["data"], {"password": "cA=="})
        self.assertEqual(secret["type"], "connection.crossplane.io/v1alpha1")
        self.assertTrue(plan.ready["secret-db-a-sql"])
        self.assertTrue(plan.ready["mirror-db-a-sql"])

    def test_mirrors_kept_while_remote_unknown(self):
        mirror_obs = {"spec": {"forProvider": {"manifest": {"metadata": {"name": f"{RNAME}-sql"}}}}}
        local_obs = {"type": "Opaque", "data": {"password": "cA=="}}
        obs = {"mirror-db-a-sql": mirror_obs, "secret-db-a-sql": local_obs}
        plan = proxy.compose(xr(), obs, {"proxyconfig": [PC]}, CFG)
        self.assertIn("mirror-db-a-sql", plan.desired)
        self.assertEqual(plan.desired["secret-db-a-sql"]["data"], {"password": "cA=="})

    def test_mirror_dropped_when_remote_stops_composing_it(self):
        mirror_obs = {"spec": {"forProvider": {"manifest": {"metadata": {"name": f"{RNAME}-sql"}}}}}
        obs = {"remote": observed_remote(resource_refs=[]), "mirror-db-a-sql": mirror_obs}
        plan = proxy.compose(xr(), obs, {"proxyconfig": [PC]}, CFG)
        self.assertNotIn("mirror-db-a-sql", plan.desired)


LOCAL_SECRET = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": {"name": "pw"}}


class SecretForward(unittest.TestCase):
    local = LOCAL_SECRET

    def test_forwarded_and_ref_rewritten(self):
        plan = proxy.compose(xr(passwordSecretRef={"name": "pw", "key": "password"}), {},
                             {"proxyconfig": [PC], "secretref-passwordsecretref": [self.local]}, CFG)
        fwd = plan.desired["forward-passwordsecretref"]
        self.assertEqual(fwd["metadata"]["name"], f"{RNAME}-passwordsecretref")
        self.assertEqual(fwd["spec"]["forProvider"]["manifest"]["metadata"]["namespace"], "ctp-a")
        self.assertEqual(fwd["spec"]["references"], [{
            "patchesFrom": {"apiVersion": "v1", "kind": "Secret", "name": "pw",
                            "namespace": "team-a", "fieldPath": "data"},
            "toFieldPath": "data"}])
        remote = plan.desired["remote"]
        self.assertEqual(remote["spec"]["forProvider"]["manifest"]["spec"]["parameters"]["passwordSecretRef"],
                         {"name": f"{RNAME}-passwordsecretref", "key": "password"})
        self.assertEqual(remote["spec"]["references"], [{"dependsOn": {
            "apiVersion": "kubernetes.m.crossplane.io/v1alpha1", "kind": "Object",
            "name": f"{RNAME}-passwordsecretref", "namespace": "team-a"}}])

    def test_absent_local_secret_rewrites_but_does_not_forward(self):
        plan = proxy.compose(xr(passwordSecretRef={"name": "pw", "key": "password"}, autoGeneratePassword=True), {},
                             {"proxyconfig": [PC], "secretref-passwordsecretref": []}, CFG)
        self.assertNotIn("forward-passwordsecretref", plan.desired)
        remote = plan.desired["remote"]
        self.assertEqual(remote["spec"]["forProvider"]["manifest"]["spec"]["parameters"]["passwordSecretRef"],
                         {"name": f"{RNAME}-passwordsecretref", "key": "password"})
        self.assertNotIn("references", remote["spec"])

    def test_ref_namespace_rewritten_to_remote(self):
        plan = proxy.compose(xr(passwordSecretRef={"name": "pw", "namespace": "team-a"}), {},
                             {"proxyconfig": [PC], "secretref-passwordsecretref": [self.local]}, CFG)
        ref = plan.desired["remote"]["spec"]["forProvider"]["manifest"]["spec"]["parameters"]["passwordSecretRef"]
        self.assertEqual(ref["namespace"], "ctp-a")


if __name__ == "__main__":
    unittest.main()
