#!/usr/bin/env bash
# Narrated walkthrough of the SOC contract between the Network and Database
# services, in six phases. Pauses between phases; DEMO_AUTO=1 runs straight through.
# Needs hack/setup.sh and `make push` first.
source "$(dirname "$0")/lib.sh"
EX="${ROOT}/examples"
RNAME="$(remote_name team-a net-a)"

pause() { [[ "${DEMO_AUTO:-0}" == 1 ]] || read -r -p $'\n  [enter] to continue ' _; }
say() { printf '\n\033[1;36m%s\033[0m\n' "$*"; }
run() { printf '\033[2m$ %s\033[0m\n' "$*"; eval "$@" || true; }
trace_db() { KUBECONFIG="${WORK}/database.kubeconfig" crossplane beta trace "$@" || true; }

say "Phase 1 - build time. The proxy is generated from the service XRD, nothing else."
run "make -C '${ROOT}' --no-print-directory gen check"
run "cat '${ROOT}/_output/network-proxy/apis/networks/composition.yaml'"
run "cat '${ROOT}/_output/network-proxy/functions/proxy/function/config.py'"
say "The proxy package ships the same XRD, byte for byte, plus a vendored function."
pause

say "Phase 2 - a Network on the database CP. It is a proxy XR: the network CP builds the VPC."
run "kubectl --kubeconfig '${WORK}/database.kubeconfig' apply -f '${EX}/network-a.yaml'"
run "sleep 15"
trace_db network.aws.platform.upbound.io/net-a -n team-a
say "On the network CP, in the database CP's namespace ${REMOTE_NS}:"
run "kubectl --kubeconfig '${WORK}/network.kubeconfig' get network -n ${REMOTE_NS} --show-labels"
say "Waiting for the VPC (a couple of minutes)..."
wait_for 900 "net-a Ready" cond_is kc_db network.aws.platform.upbound.io/net-a Ready True -n team-a || true
run "kubectl --kubeconfig '${WORK}/database.kubeconfig' get network net-a -n team-a -o jsonpath='{.status}' | yq -P"
say "Status came back: on the database CP the Network reads like a local resource."
pause

say "Phase 3 - a database placed by label. It reads the Network the standard way: a local, typed XR."
run "cat '${EX}/db-a.yaml'"
run "kubectl --kubeconfig '${WORK}/database.kubeconfig' apply -f '${EX}/db-a.yaml'"
run "sleep 20"
run "kubectl --kubeconfig '${WORK}/database.kubeconfig' get sqlinstance db-a -n team-a -o jsonpath='{.status}' | yq -P"
run "kubectl --kubeconfig '${WORK}/database.kubeconfig' get subnetgroups.rds.aws.m.upbound.io,securitygroups.ec2.aws.m.upbound.io -n team-a -o custom-columns=KIND:.kind,NAME:.metadata.name,SUBNETS:.spec.forProvider.subnetIds,VPC:.spec.forProvider.vpcId"
trace_db sqlinstance.aws.platform.upbound.io/db-a -n team-a
say "RDS takes ~10 minutes. Waiting for db-a and its connection Secret..."
wait_for 2400 "db-a Ready" cond_is kc_db sqlinstance.aws.platform.upbound.io/db-a Ready True -n team-a || true
run "kubectl --kubeconfig '${WORK}/database.kubeconfig' get secret db-a-sql -n team-a -o jsonpath='{.data}' | yq -P 'keys'"
pause

say "Phase 4 - the Network cannot go while a database sits in its VPC."
run "kubectl --kubeconfig '${WORK}/database.kubeconfig' delete network net-a -n team-a --wait=false"
run "kubectl --kubeconfig '${WORK}/database.kubeconfig' get usages.protection.crossplane.io -n team-a"
pause

say "Phase 5 - failure stays local: remove the ProxyConfig, and the remote is left alone."
run "kubectl --kubeconfig '${WORK}/database.kubeconfig' delete proxyconfig networks.aws.platform.upbound.io"
run "sleep 75"
run "kubectl --kubeconfig '${WORK}/database.kubeconfig' get network net-a -n team-a -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.message}{\"\\n\"}{end}'"
run "kubectl --kubeconfig '${WORK}/network.kubeconfig' get network ${RNAME} -n ${REMOTE_NS}"
run "kubectl --kubeconfig '${WORK}/database.kubeconfig' apply -f '${ROOT}/proxy-base/examples/proxyconfigs/networks.yaml'"
pause

say "Phase 6 - teardown in order: the database first, then the Network and its VPC."
run "kubectl --kubeconfig '${WORK}/database.kubeconfig' delete sqlinstance db-a -n team-a --wait=false"
wait_for 2400 "db-a gone" sh -c "! kubectl --kubeconfig '${WORK}/database.kubeconfig' get sqlinstance db-a -n team-a" || true
run "kubectl --kubeconfig '${WORK}/database.kubeconfig' delete network net-a -n team-a --wait=false"
wait_for 1800 "remote Network gone" sh -c "! kubectl --kubeconfig '${WORK}/network.kubeconfig' get network ${RNAME} -n ${REMOTE_NS}" || true
run "kubectl --kubeconfig '${WORK}/network.kubeconfig' get network -n ${REMOTE_NS}"
run "kubectl --kubeconfig '${WORK}/network.kubeconfig' get events -n ${REMOTE_NS} | grep -i DependencyViolation || echo 'no DependencyViolation'"
say "Done."
