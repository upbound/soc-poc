#!/usr/bin/env bash
# Delete what the demo created on AWS, in dependency order, then (only with
# DELETE_CONTROL_PLANES=1) the control planes, group, robot and team.
source "$(dirname "$0")/lib.sh"

step "Databases, then Networks (the Usages enforce this order anyway)"
kc_db delete sqlinstance --all -n team-a --wait=false 2>/dev/null || true
wait_for 2400 "databases gone" sh -c "! kubectl --kubeconfig '${WORK}/database.kubeconfig' get sqlinstance -n team-a -o name | grep -q ." || true
for ns in team-a team-b; do
  kc_db delete network --all -n "${ns}" --wait=false 2>/dev/null || true
done
wait_for 1800 "remote Networks gone" sh -c "! kubectl --kubeconfig '${WORK}/network.kubeconfig' get network -n '${REMOTE_NS}' -o name | grep -q ." || true
kc_net get network -n "${REMOTE_NS}" 2>/dev/null || true

if [[ "${DELETE_CONTROL_PLANES:-0}" != 1 ]]; then
  bold "AWS resources removed. Control planes kept; DELETE_CONTROL_PLANES=1 removes them, the group, robot and team."
  exit 0
fi

step "Control planes, group, robot and team"
for cp in "${DB_CP}" "${NET_CP}"; do
  up ctp delete "${cp}" -g "${GROUP}" --kubeconfig "${WORK}/space-root.kubeconfig" || true
done
up group delete "${GROUP}" --kubeconfig "${WORK}/space-root.kubeconfig" || true
up robot delete "${ROBOT}" --force 2>/dev/null || up robot delete "${ROBOT}" || true
up team delete "${TEAM}" --force 2>/dev/null || up team delete "${TEAM}" || true
rm -rf "${WORK}"
bold "Everything removed."
