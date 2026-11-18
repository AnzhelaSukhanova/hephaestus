#!/bin/bash
# Queue "Hephaestus nightly fuzzing" on TeamCity Cloud by hand: the same REST call the trigger makes.
# Use it to test the Cloud build before the internal trigger exists.
#
#   export TEAMCITY_CLOUD_TOKEN=<personal access token on kotlinlang.teamcity.com>
#   ./queue-cloud-build.sh --build-type <HEPHAESTUS_FUZZING_BUILD_ID> \
#       [--version 2.5.20-dev-3466] [--branch hephaestus/ci-foo] [--oracle backend-comparison] [--duration 1h]
set -euo pipefail
CLOUD="${TEAMCITY_CLOUD_URL:-https://kotlinlang.teamcity.com}"
BUILD_TYPE="" VERSION="" BRANCH="" ORACLE="" DURATION=""
while [ $# -gt 0 ]; do
  case "$1" in
    --build-type) BUILD_TYPE="$2"; shift 2 ;;
    --version)    VERSION="$2";    shift 2 ;;
    --branch)     BRANCH="$2";     shift 2 ;;
    --oracle)     ORACLE="$2";     shift 2 ;;
    --duration)   DURATION="$2";   shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
: "${TEAMCITY_CLOUD_TOKEN:?set TEAMCITY_CLOUD_TOKEN}"
[ -n "$BUILD_TYPE" ] || { echo "--build-type is required" >&2; exit 2; }

BODY=$(BUILD_TYPE="$BUILD_TYPE" VERSION="$VERSION" BRANCH="$BRANCH" ORACLE="$ORACLE" DURATION="$DURATION" python3 - <<'PY'
import json, os
props = [{"name": n, "value": os.environ[k]} for n, k in [
    ("env.KOTLIN_MASTER_VERSION", "VERSION"), ("env.HEPHAESTUS_ORACLE", "ORACLE"),
    ("env.HEPHAESTUS_DURATION", "DURATION")] if os.environ[k]]
build = {"buildType": {"id": os.environ["BUILD_TYPE"]},
         "comment": {"text": "Queued by hand with queue-cloud-build.sh"},
         "properties": {"property": props}}
if os.environ["BRANCH"]:
    build["branchName"] = os.environ["BRANCH"]
print(json.dumps(build))
PY
)
echo "Payload: $BODY"
HTTP=$(curl -sS -o response.json -w '%{http_code}' -X POST "$CLOUD/app/rest/buildQueue" \
  -H "Authorization: Bearer $TEAMCITY_CLOUD_TOKEN" \
  -H "Content-Type: application/json" -H "Accept: application/json" \
  -d "$BODY")
echo "HTTP $HTTP"
python3 -c 'import json;d=json.load(open("response.json"));print({k:d.get(k) for k in ("id","state","branchName","defaultBranch","webUrl")})' 2>/dev/null || cat response.json
[ "$HTTP" = "200" ]
