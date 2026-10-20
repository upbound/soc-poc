"""soc_proxy_gen: generate (or check) a proxy up project from service XRDs.

    python -m soc_proxy_gen --xrd <path|url> --name <project> --repository <repo> --out <dir>
    python -m soc_proxy_gen --check --xrd <path|url> --out <dir>
"""

import argparse
import pathlib
import sys

from soc_proxy_gen import generate


def main() -> int:
    """Run the CLI."""
    p = argparse.ArgumentParser(prog="soc_proxy_gen", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--xrd", action="append", required=True, help="service XRD path or URL (repeatable)")
    p.add_argument("--out", required=True, type=pathlib.Path, help="output project directory")
    p.add_argument("--check", action="store_true", help="fail if the generated XRDs differ from the sources")
    p.add_argument("--name", help="proxy project name, e.g. soc-poc-network-proxy")
    p.add_argument("--repository", help="proxy package repository, e.g. xpkg.upbound.io/acme/soc-poc-network-proxy")
    p.add_argument("--example", action="append", default=[], help="example XR to copy into the project")
    p.add_argument("--provider-kubernetes", default="xpkg.upbound.io/upbound/provider-kubernetes")
    p.add_argument("--provider-kubernetes-version", default=">=v1.3.3 <v2.0.0")
    p.add_argument("--proxy-base", required=False, help="proxy-base package, e.g. xpkg.upbound.io/acme/soc-poc-proxy-base")
    p.add_argument("--proxy-base-version", default=">=v0.0.0-0")
    a = p.parse_args()

    if a.check:
        problems = generate.check(a.xrd, a.out)
        for msg in problems:
            print(f"soc_proxy_gen: {msg}", file=sys.stderr)
        if not problems:
            print(f"soc_proxy_gen: {len(a.xrd)} XRD(s) in {a.out} match their sources")
        return 1 if problems else 0

    missing = [f for f in ("name", "repository") if not getattr(a, f)]
    if missing:
        p.error("generating needs " + ", ".join("--" + m.replace("_", "-") for m in missing))

    deps = {
        "providerKubernetes": a.provider_kubernetes,
        "providerKubernetesVersion": a.provider_kubernetes_version,
        "proxyBase": a.proxy_base or "",
        "proxyBaseVersion": a.proxy_base_version,
    }
    infos = generate.generate(a.xrd, a.out, a.name, a.repository, deps, a.example)
    for i in infos:
        refs = ", ".join(".".join(x) for x in i["secretRefPaths"]) or "none"
        print(f"soc_proxy_gen: {i['xrdName']} -> {a.out} (prefix {i['namePrefix']}, secret refs: {refs})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
