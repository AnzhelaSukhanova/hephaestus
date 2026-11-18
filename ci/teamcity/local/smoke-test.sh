#!/bin/bash
# Local check before touching TeamCity. Run from the Hephaestus repo root.
#
#   ci/teamcity/local/smoke-test.sh [kotlin-master-version] [oracle] [duration] [checkpoint]
#   ci/teamcity/local/smoke-test.sh 2.5.20-dev-3466 backend-comparison 5m 2m
#
# 1. builds the runtime image exactly as Cloud step 1 does
# 2. prints the provenance labels
# 3. re-runs the offline compiler check inside the final image
# 4. runs Cloud step 2 (the real Fuzz script, with checkpoints) for a few minutes
# 5. runs Cloud step 3 (Collect results) and shows what it wrote
# 6. runs Cloud step 4 (Cleanup)
set -euo pipefail
KIT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-}" ORACLE="${2:-backend-comparison}" DURATION="${3:-5m}" CHECKPOINT="${4:-2m}"
TAG="local-smoke"

sub() {  # stand-ins for the TeamCity %...% references
  sed -e "s|%build.vcs.number%|$(git rev-parse HEAD 2>/dev/null || echo unknown)|g" \
      -e "s|%teamcity.build.branch%|$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo local)|g" \
      -e "s|%teamcity.build.id%|$TAG|g" \
      -e "s|%teamcity.serverUrl%|http://localhost|g" \
      -e "s|%build.number%|0|g" \
      -e "s|%system.teamcity.buildType.id%|Local|g" \
      -e "s|%teamcity.agent.name%|$(hostname)|g" "$1"
}

export KOTLIN_MASTER_VERSION="$VERSION" KOTLIN_COMMIT="" HEPHAESTUS_ORACLE="$ORACLE" \
       HEPHAESTUS_DURATION="$DURATION" HEPHAESTUS_CHECKPOINT="$CHECKPOINT"

echo "== 1. build image"
bash <(sub "$KIT/cloud/step-1-build-image.sh")

echo "== 2. labels"
docker inspect -f '{{json .Config.Labels}}' "hephaestus:$TAG"

echo "== 3. offline compiler check in the final image"
docker run --rm --network none --entrypoint python3 "hephaestus:$TAG" src/tools/kotlin_compilers/manage.py check

echo "== 4. fuzz for $DURATION with oracle $ORACLE, checkpoint every $CHECKPOINT"
rc=0; bash <(sub "$KIT/cloud/step-2-fuzz.sh") || rc=$?
echo "Fuzz step exit code: $rc (0 = OK)"; ls -la bugs results | head -20

echo "== 5. collect results"
collect=0; bash <(sub "$KIT/cloud/step-3-collect.sh") || collect=$?
echo "Collect step exit code: $collect (0 = OK)"; ls -la results/final
python3 -c 'import json,sys; s=json.load(open(sys.argv[1])); print(s["stop_reason"], s["runs"], s["signatures"][:5], sep="\n")' \
  results/final/summary.json || true

echo "== 6. cleanup"
bash <(sub "$KIT/cloud/step-4-cleanup.sh")
[ "$rc" -eq 0 ] && exit "$collect"
exit "$rc"
