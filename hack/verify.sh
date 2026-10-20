#!/usr/bin/env bash
# Run the contract checks C1-C9 against the live control planes, unattended.
# Needs hack/setup.sh first. Takes ~40 minutes, most of it RDS create and delete.
# Prints ok/FAIL per assertion and exits non-zero if any failed.
source "$(dirname "$0")/lib.sh"
FAILED=0
EX="${ROOT}/examples"
RNAME="$(remote_name team-a net-a)"
# Databases to create, from examples/<name>.yaml. Each needs one RDS subnet group.
DBS="${DBS:-db-a db-b}"
FIRST_DB="${DBS%% *}"

get() { local kc=$1 path=$2; shift 2; "${kc}" get "$@" -o "jsonpath=${path}" 2>/dev/null; }
same() { if [[ "$2" == "$3" && -n "$2" ]]; then ok "$1 ($2)"; else fail "$1: '$2' != '$3'"; fi; }

# Predicates for wait_for, which runs them in this shell.
remote_labelled() { [[ "$(get kc_net '{.metadata.labels.demo}' network "${RNAME}" -n "${REMOTE_NS}")" == updated ]]; }
local_cond() { [[ "$(get kc_db "{.status.conditions[?(@.type==\"$3\")].$4}" network "$1" -n "$2")" == *"$5"* ]]; }
# shellcheck disable=SC2086
gone_db() { ! kc_db get sqlinstance ${DBS} -n team-a >/dev/null 2>&1; }
gone_remote() { ! kc_net get network "${RNAME}" -n "${REMOTE_NS}" >/dev/null 2>&1; }

step "C1  Network on the database CP becomes a remote Network on the network CP"
kc_db apply -f "${EX}/network-a.yaml"
kc_db apply -f "${EX}/${FIRST_DB}.yaml"   # applied before the Network is Ready on purpose (C2)
wait_for 120 "remote Network ${REMOTE_NS}/${RNAME}" kc_net get "network.aws.platform.upbound.io/${RNAME}" -n "${REMOTE_NS}"
same "remote carries the local label" "$(get kc_net '{.metadata.labels.network}' network "${RNAME}" -n "${REMOTE_NS}")" a
same "remote carries origin-name" "$(get kc_net '{.metadata.labels.soc\.upbound\.io/origin-name}' network "${RNAME}" -n "${REMOTE_NS}")" net-a
same "remote carries origin-namespace" "$(get kc_net '{.metadata.labels.soc\.upbound\.io/origin-namespace}' network "${RNAME}" -n "${REMOTE_NS}")" team-a

step "C2  SQLInstance waits for the Network, then wires itself from Network.status"
early=$(get kc_db '{.status.conditions[?(@.type=="NetworkResolved")].reason}' sqlinstance "${FIRST_DB}" -n team-a || true)
if [[ "${early}" == "WaitingForNetwork" ]]; then ok "${FIRST_DB} waited for the Network"; else
  echo "  note: ${FIRST_DB} reported '${early:-nothing yet}' before the Network was Ready (timing)"; fi
wait_for 900 "Network net-a Ready" cond_is kc_db network.aws.platform.upbound.io/net-a Ready True -n team-a \
  && ok "net-a Ready on the database CP" || fail "net-a not Ready"
local_vpc=$(get kc_db '{.status.vpcId}' network net-a -n team-a || true)
same "status.vpcId copied back" "${local_vpc}" "$(get kc_net '{.status.vpcId}' network "${RNAME}" -n "${REMOTE_NS}")"
for db in ${DBS#"${FIRST_DB}"}; do kc_db apply -f "${EX}/${db}.yaml"; done
for db in ${DBS}; do
  wait_for 2400 "${db} Ready" cond_is kc_db "sqlinstance.aws.platform.upbound.io/${db}" Ready True -n team-a \
    && ok "${db} Ready" || fail "${db} not Ready"
  same "${db} VPC = Network VPC" "$(get kc_db '{.status.vpcId}' sqlinstance "${db}" -n team-a)" "${local_vpc}"
  same "${db} subnets = Network private subnets" \
    "$(get kc_db '{.status.subnetIds}' sqlinstance "${db}" -n team-a)" "$(get kc_db '{.status.privateSubnetIds}' network net-a -n team-a)"
done
cidr=$(kc_db get securitygroupingressrules.ec2.aws.m.upbound.io -n team-a -l "crossplane.io/composite=${FIRST_DB}" -o jsonpath='{.items[0].spec.forProvider.cidrIpv4}' || true)
same "ingress from the Network CIDR" "${cidr}" "$(get kc_db '{.spec.parameters.vpcCidrBlock}' network net-a -n team-a)"

step "C9  The connection Secret is composed from the RDS connection details"
for db in ${DBS}; do
  for key in endpoint username password; do
    if kc_db get secret "${db}-sql" -n team-a -o "jsonpath={.data.${key}}" | grep -q .; then ok "${db}-sql has ${key}"; else fail "${db}-sql lacks ${key}"; fi
  done
done

step "C3  Label and spec changes propagate"
kc_db label network net-a -n team-a demo=updated --overwrite
wait_for 180 "label on the remote" remote_labelled \
  && ok "label reached the remote" || fail "label did not reach the remote"

step "C4  The database function looks up nothing but the Network"
reqs=$(grep -c "require_resources(" "${ROOT}/database-service/functions/sqlinstance/function/fn.py" || true)
same "required-resource requests in fn.py (both Network)" "${reqs}" 2
if grep -rqi "aws-query\|boto" "${ROOT}/database-service"; then fail "an AWS lookup is referenced"; else ok "no AWS lookups"; fi

step "C5  Failure modes stay local and never touch the remote"
kc_db apply -f "${EX}/network-bad.yaml"
wait_for 300 "net-bad reports the remote error" local_cond net-bad team-a RemoteReady status False \
  && ok "remote failure surfaced: $(get kc_db '{.status.conditions[?(@.type=="RemoteReady")].message}' network net-bad -n team-a | head -c 160)" \
  || fail "remote failure not surfaced on net-bad"
kc_db delete -f "${EX}/network-bad.yaml" --wait=false
kc_db delete proxyconfig networks.aws.platform.upbound.io
wait_for 180 "net-a reports the missing ProxyConfig" local_cond net-a team-a Synced message "not found" \
  && ok "missing ProxyConfig is Fatal on the local XR" || fail "missing ProxyConfig not reported"
same "remote untouched without a ProxyConfig" "$(get kc_net '{.metadata.name}' network "${RNAME}" -n "${REMOTE_NS}")" "${RNAME}"
kc_db apply -f "${ROOT}/proxy-base/examples/proxyconfigs/networks.yaml"
token=$(kc_db get secret network-service-cp-token -n crossplane-system -o jsonpath='{.data.token}' || true)
kc_db patch secret network-service-cp-token -n crossplane-system -p '{"data":{"token":"aW52YWxpZA=="}}'
kc_db annotate object.kubernetes.m.crossplane.io "${RNAME}" -n team-a soc-poc/poke="$(date +%s)" --overwrite >/dev/null
wait_for 300 "net-a reports the broken credential" local_cond net-a team-a RemoteSynced reason ProxyError \
  && ok "broken credential surfaced as RemoteSynced=False/ProxyError" || fail "broken credential not surfaced"
same "remote untouched with a broken credential" "$(get kc_net '{.metadata.name}' network "${RNAME}" -n "${REMOTE_NS}")" "${RNAME}"
kc_db patch secret network-service-cp-token -n crossplane-system -p "{\"data\":{\"token\":\"${token}\"}}"
wait_for 300 "net-a Ready again" cond_is kc_db network.aws.platform.upbound.io/net-a Ready True -n team-a \
  && ok "recovered after the credential was restored" || fail "did not recover"

step "C7  The same name in two namespaces maps to two remote names"
kc_db create namespace team-b --dry-run=client -o yaml | kc_db apply -f -
kc_db apply -f "${EX}/network-isolation.yaml"
a="$(remote_name team-a iso)"; b="$(remote_name team-b iso)"
wait_for 180 "both remote isolation Networks" kc_net get network "${a}" "${b}" -n "${REMOTE_NS}" \
  && ok "team-a/iso -> ${a}, team-b/iso -> ${b}" || fail "isolation Networks missing"
kc_db delete -f "${EX}/network-isolation.yaml" --wait=false

step "C8  The proxy XRD is byte-identical to the service XRD"
if make -C "${ROOT}" --no-print-directory check >/dev/null 2>&1; then ok "soc_proxy_gen --check passes"; else fail "soc_proxy_gen --check failed"; fi

step "C6  Deletion order: the Network outlives every database in its VPC"
if kc_db delete network net-a -n team-a --wait=false 2>"${WORK}/c6.err"; then
  sleep 20
  if kc_db get network net-a -n team-a >/dev/null 2>&1; then ok "net-a still exists while databases use it"; else fail "net-a deleted while in use"; fi
else
  ok "deleting net-a refused while in use: $(head -c 160 "${WORK}/c6.err")"
fi
# shellcheck disable=SC2086
kc_db delete sqlinstance ${DBS} -n team-a --wait=false
wait_for 2400 "databases gone" gone_db \
  && ok "databases deleted" || fail "databases not deleted"
kc_db delete network net-a -n team-a --wait=false || true
wait_for 1800 "remote Network gone" gone_remote \
  && ok "remote Network and its VPC deleted" || fail "remote Network not deleted"
if kc_net get events -n "${REMOTE_NS}" 2>/dev/null | grep -qi DependencyViolation; then
  fail "DependencyViolation seen on the network CP"; else ok "no DependencyViolation on the network CP"; fi

bold "$([[ ${FAILED} -eq 0 ]] && echo 'All checks passed' || echo "${FAILED} check(s) failed")"
exit "${FAILED}"
