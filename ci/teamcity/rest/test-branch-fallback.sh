#!/bin/bash
# Checks that `#! fallbackToDefault: false` really stops a bad branch name from running ci-main.
# Queues a branch that doesn't exist, reports what TeamCity did, and cancels the build if one was queued.
#
#   export TEAMCITY_CLOUD_TOKEN=<personal access token on kotlinlang.teamcity.com>
#   ./test-branch-fallback.sh <HEPHAESTUS_FUZZING_BUILD_ID>
set -euo pipefail
CLOUD="${TEAMCITY_CLOUD_URL:-https://kotlinlang.teamcity.com}"
BUILD_TYPE="${1:?usage: $0 <cloud-build-configuration-id>}"
: "${TEAMCITY_CLOUD_TOKEN:?set TEAMCITY_CLOUD_TOKEN}"
BAD="hephaestus/ci-does-not-exist-$(date +%s)"
AUTH=(-H "Authorization: Bearer $TEAMCITY_CLOUD_TOKEN" -H "Content-Type: application/json" -H "Accept: application/json")

HTTP=$(curl -sS -o response.json -w '%{http_code}' -X POST "$CLOUD/app/rest/buildQueue" "${AUTH[@]}" \
  -d "{\"branchName\":\"$BAD\",\"buildType\":{\"id\":\"$BUILD_TYPE\"},\"comment\":{\"text\":\"fallbackToDefault test, safe to ignore\"},\"properties\":{\"property\":[{\"name\":\"env.HEPHAESTUS_DURATION\",\"value\":\"1m\"}]}}")
echo "HTTP $HTTP"; cat response.json; echo

if [ "$HTTP" != "200" ]; then
  echo "PASS: TeamCity refused the unknown branch instead of falling back to the default."
  exit 0
fi

ID=$(python3 -c 'import json;print(json.load(open("response.json"))["id"])')
sleep 10
STATE=$(curl -sS "$CLOUD/app/rest/builds/id:$ID?fields=id,state,branchName,defaultBranch,statusText" "${AUTH[@]}")
echo "Build $ID: $STATE"
curl -sS -o /dev/null -X POST "$CLOUD/app/rest/buildQueue/id:$ID" "${AUTH[@]}" \
  -d '{"comment":"fallbackToDefault test","readdIntoQueue":false}' || true
curl -sS -o /dev/null -X POST "$CLOUD/app/rest/builds/id:$ID" "${AUTH[@]}" \
  -d '{"comment":"fallbackToDefault test","readdIntoQueue":false}' || true
echo "Cancel requested for build $ID."
if echo "$STATE" | grep -q '"defaultBranch":true'; then
  echo "FAIL: TeamCity fell back to the default branch. Check the branch spec contains '#! fallbackToDefault: false'."
  exit 1
fi
echo "CHECK: a build was queued but not on the default branch. Open it and confirm it failed or never started."
