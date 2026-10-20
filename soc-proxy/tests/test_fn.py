"""Tests for the SDK adapter: requests in, response fields out."""

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy_function"))

try:
    from crossplane.function import resource
    from crossplane.function.proto.v1 import run_function_pb2 as fnv1
    from function import config, fn
except ImportError:  # pragma: no cover - the SDK only exists in the test container
    fnv1 = None

XR = {
    "apiVersion": "aws.platform.upbound.io/v1alpha1",
    "kind": "Network",
    "metadata": {"name": "net-a", "namespace": "team-a"},
    "spec": {"parameters": {"id": "net-a"}},
}
PC = {
    "apiVersion": "soc.upbound.io/v1alpha1",
    "kind": "ProxyConfig",
    "metadata": {"name": "networks.aws.platform.upbound.io"},
    "spec": {"providerConfigRef": {"name": "network-service-cp"}, "remoteNamespace": "ctp-database"},
}


def request(required=None, observed=None):
    req = fnv1.RunFunctionRequest(observed=fnv1.State(composite=fnv1.Resource(resource=resource.dict_to_struct(XR))))
    for key, body in (observed or {}).items():
        req.observed.resources[key].resource.CopyFrom(resource.dict_to_struct(body))
    for name, items in (required or {}).items():
        req.required_resources[name].SetInParent()
        for item in items:
            req.required_resources[name].items.add().resource.CopyFrom(resource.dict_to_struct(item))
    return req


@unittest.skipIf(fnv1 is None, "crossplane-function-sdk-python not installed")
class Runner(unittest.TestCase):
    def setUp(self):
        config.PROXIES["aws.platform.upbound.io/Network"] = {
            "proxyConfigName": "networks.aws.platform.upbound.io", "namePrefix": "network", "secretRefPaths": []}

    def run_fn(self, req):
        return asyncio.run(fn.FunctionRunner().RunFunction(req, None))

    def test_first_call_declares_requirement_only(self):
        rsp = self.run_fn(request())
        self.assertEqual(rsp.requirements.resources["proxyconfig"].match_name, "networks.aws.platform.upbound.io")
        self.assertEqual(len(rsp.desired.resources), 0)
        self.assertEqual(len(rsp.results), 0)

    def test_missing_proxyconfig_is_fatal(self):
        rsp = self.run_fn(request(required={"proxyconfig": []}))
        self.assertEqual(rsp.results[0].severity, fnv1.SEVERITY_FATAL)

    def test_composes_remote_object_not_ready(self):
        rsp = self.run_fn(request(required={"proxyconfig": [PC]}))
        remote = resource.struct_to_dict(rsp.desired.resources["remote"].resource)
        self.assertEqual(remote["kind"], "Object")
        self.assertEqual(remote["spec"]["forProvider"]["manifest"]["metadata"]["namespace"], "ctp-database")
        self.assertEqual(rsp.desired.resources["remote"].ready, fnv1.READY_FALSE)
        self.assertEqual([c.type for c in rsp.conditions], ["RemoteReady"])

    def test_status_written_to_composite(self):
        obs = {"remote": {"status": {
            "conditions": [{"type": "Ready", "status": "True"}, {"type": "Synced", "status": "True"}],
            "atProvider": {"manifest": {"status": {"vpcId": "vpc-1", "conditions": []}}}}}}
        rsp = self.run_fn(request(required={"proxyconfig": [PC]}, observed=obs))
        self.assertEqual(resource.struct_to_dict(rsp.desired.composite.resource)["status"], {"vpcId": "vpc-1"})
        self.assertEqual(rsp.desired.resources["remote"].ready, fnv1.READY_TRUE)

    def test_unknown_kind_is_fatal(self):
        del config.PROXIES["aws.platform.upbound.io/Network"]
        rsp = self.run_fn(request())
        self.assertEqual(rsp.results[0].severity, fnv1.SEVERITY_FATAL)


if __name__ == "__main__":
    unittest.main()
