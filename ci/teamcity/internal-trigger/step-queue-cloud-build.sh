#!/bin/bash
# Internal buildserver: step of "[Trigger] Hephaestus fuzzing for published Kotlin".
# Copy of Kotlin_KotlinDev_TriggerWasmUiTestsTcc; only the queued Cloud build and payload differ.
set -euo pipefail

KOTLIN_VERSION=$(echo -n "%dep.Kotlin_KotlinDev_DeployMavenArtifacts_Nightly_kotlin_space_packages.DeployVersion%" | tr -d '[:space:]')
# Same revision as the nightly, thanks to the snapshot dependency
KOTLIN_COMMIT="%build.vcs.number%"
# Optional overrides; empty = use the Cloud build's defaults (VCS default branch, oracle default)
BRANCH=$(echo -n "%hephaestus.branch%" | tr -d '[:space:]')
ORACLE=$(echo -n "%hephaestus.oracle%" | tr -d '[:space:]')

CLOUD="https://kotlinlang.teamcity.com"
BUILD_TYPE="<HEPHAESTUS_FUZZING_BUILD_ID>"
SELF="%teamcity.serverUrl%/buildConfiguration/%system.teamcity.buildType.id%/%teamcity.build.id%"

fail() { echo "##teamcity[buildProblem description='$1']"; exit 1; }
[ -n "$KOTLIN_VERSION" ] || fail "DeployVersion is empty"
[[ "$KOTLIN_VERSION" =~ ^[0-9A-Za-z.+-]+$ ]] || fail "Unexpected DeployVersion: $KOTLIN_VERSION"
[[ -z "$BRANCH" || "$BRANCH" =~ ^hephaestus/ci-[0-9A-Za-z._/-]+$ ]] || fail "hephaestus.branch must look like hephaestus/ci-*"
[[ -z "$ORACLE" || "$ORACLE" =~ ^[a-z0-9-]+$ ]] || fail "Unexpected hephaestus.oracle: $ORACLE"

PROPS="{\"name\":\"env.KOTLIN_MASTER_VERSION\",\"value\":\"$KOTLIN_VERSION\"},{\"name\":\"env.KOTLIN_COMMIT\",\"value\":\"$KOTLIN_COMMIT\"}"
[ -z "$ORACLE" ] || PROPS="$PROPS,{\"name\":\"env.HEPHAESTUS_ORACLE\",\"value\":\"$ORACLE\"}"
BRANCH_JSON=""
# branchName is the logical name and belongs on the build object, not inside buildType
[ -z "$BRANCH" ] || BRANCH_JSON="\"branchName\":\"$BRANCH\","

BODY="{${BRANCH_JSON}\"buildType\":{\"id\":\"$BUILD_TYPE\"},\"comment\":{\"text\":\"Kotlin $KOTLIN_VERSION, triggered from $SELF\"},\"properties\":{\"property\":[$PROPS]}}"
echo "Payload: $BODY"

# %% is TeamCity's escape for a literal % (curl's write-out format)
HTTP=$(curl -sS -o response.json -w '%%{http_code}' -X POST "$CLOUD/app/rest/buildQueue" \
  -H "Authorization: Bearer %teamcity.cloud.serviceUser.token%" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json" \
  -H "Origin: $SELF" \
  -d "$BODY")
cat response.json; echo
[ "$HTTP" = "200" ] || fail "TeamCity Cloud returned HTTP $HTTP"
