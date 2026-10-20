"""Unit tests for soc_proxy_gen."""

import pathlib
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from soc_proxy_gen import generate

TESTDATA = pathlib.Path(__file__).resolve().parent / "testdata"
NETWORK_XRD = str(TESTDATA / "network-definition.yaml")
DB_XRD = str(ROOT.parent / "database-service" / "apis" / "sqlinstances" / "definition.yaml")
EXAMPLE = str(ROOT.parent / "examples" / "network-a.yaml")
DEPS = {
    "providerKubernetes": "xpkg.upbound.io/upbound/provider-kubernetes",
    "providerKubernetesVersion": ">=v1.3.3 <v2.0.0",
    "proxyBase": "xpkg.upbound.io/acme/soc-poc-proxy-base",
    "proxyBaseVersion": "v0.1.0",
}
REPO = "xpkg.upbound.io/acme/soc-poc-network-proxy"


class Naming(unittest.TestCase):
    def test_function_ref_matches_up(self):
        # up names the embedded function ToDNSLabel("<org>/<repo>_<fn>"), dropping '_'.
        self.assertEqual(generate.function_ref(REPO), "acme-soc-poc-network-proxyproxy")

    def test_dns_label_truncates_like_up(self):
        self.assertEqual(len(generate.to_dns_label("a" * 100)), 63)
        self.assertEqual(generate.to_dns_label("-Ab_c.d/"), "bc-d")


class Describe(unittest.TestCase):
    def test_network_has_no_secret_refs(self):
        info = generate.describe(yaml.safe_load(pathlib.Path(NETWORK_XRD).read_bytes()))
        self.assertEqual(info["proxyConfigName"], "networks.aws.platform.upbound.io")
        self.assertEqual((info["group"], info["kind"], info["version"]),
                         ("aws.platform.upbound.io", "Network", "v1alpha1"))
        self.assertEqual(info["namePrefix"], "network")
        self.assertEqual(info["secretRefPaths"], [])

    @unittest.skipUnless(pathlib.Path(DB_XRD).exists(), "database-service XRD not present")
    def test_database_password_ref_found_by_convention(self):
        info = generate.describe(yaml.safe_load(pathlib.Path(DB_XRD).read_bytes()))
        self.assertEqual(info["secretRefPaths"], [["spec", "parameters", "passwordSecretRef"]])


class Generate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = pathlib.Path(self.tmp.name) / "network-proxy"
        generate.generate([NETWORK_XRD], self.out, "soc-poc-network-proxy", REPO, DEPS, [EXAMPLE])

    def tearDown(self):
        self.tmp.cleanup()

    def test_xrd_is_byte_identical(self):
        self.assertEqual((self.out / "apis/networks/definition.yaml").read_bytes(),
                         pathlib.Path(NETWORK_XRD).read_bytes())
        self.assertEqual(generate.check([NETWORK_XRD], self.out), [])

    def test_check_catches_drift(self):
        xrd = self.out / "apis/networks/definition.yaml"
        xrd.write_text(xrd.read_text() + "# drift\n")
        self.assertEqual(len(generate.check([NETWORK_XRD], self.out)), 1)

    def test_composition_uses_embedded_function(self):
        comp = yaml.safe_load((self.out / "apis/networks/composition.yaml").read_text())
        self.assertEqual(comp["spec"]["compositeTypeRef"],
                         {"apiVersion": "aws.platform.upbound.io/v1alpha1", "kind": "Network"})
        self.assertEqual(comp["spec"]["pipeline"],
                         [{"step": "proxy", "functionRef": {"name": "acme-soc-poc-network-proxyproxy"}}])

    def test_project_depends_only_on_provider_kubernetes_and_base(self):
        proj = yaml.safe_load((self.out / "upbound.yaml").read_text())
        self.assertEqual(proj["spec"]["repository"], REPO)
        self.assertEqual([d["package"] for d in proj["spec"]["dependsOn"]],
                         [DEPS["providerKubernetes"], DEPS["proxyBase"]])

    def test_function_vendored_with_generated_config(self):
        fn = self.out / "functions/proxy"
        self.assertTrue((fn / "pyproject.toml").exists())
        self.assertTrue((fn / "function/proxy.py").exists())
        ns = {}
        exec((fn / "function/config.py").read_text(), ns)  # noqa: S102 - generated, trusted
        self.assertEqual(ns["PROXIES"], {"aws.platform.upbound.io/Network": {
            "proxyConfigName": "networks.aws.platform.upbound.io",
            "namePrefix": "network",
            "secretRefPaths": []}})

    def test_smoke_tests_generated(self):
        docs = list(yaml.safe_load_all((self.out / "tests/test-proxy-networks/test.yaml").read_text()))
        self.assertEqual([d["metadata"]["name"] for d in docs],
                         ["networks-creates-remote", "networks-reads-back-status"])
        self.assertEqual(docs[1]["spec"]["assertResources"][0]["status"], {
            "vpcId": "test-vpcId", "subnetIds": ["test-subnetIds"], "publicSubnetIds": ["test-publicSubnetIds"],
            "privateSubnetIds": ["test-privateSubnetIds"], "securityGroupIds": ["test-securityGroupIds"]})


if __name__ == "__main__":
    unittest.main()
