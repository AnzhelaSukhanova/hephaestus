#!/bin/bash
# Cloud: step 1 "Build image" (runs in the checkout of the selected Hephaestus branch).
set -euo pipefail
echo "kt=${KOTLIN_MASTER_VERSION:-<prerelease>} oracle=$HEPHAESTUS_ORACLE branch=%teamcity.build.branch% commit=%build.vcs.number%"

docker build --no-cache --platform linux/amd64 --target runtime \
  --build-arg KOTLIN_MASTER_VERSION="$KOTLIN_MASTER_VERSION" \
  --build-arg KOTLIN_COMMIT="$KOTLIN_COMMIT" \
  --build-arg HEPHAESTUS_COMMIT="%build.vcs.number%" \
  --build-arg HEPHAESTUS_BRANCH="%teamcity.build.branch%" \
  -t "hephaestus:%teamcity.build.id%" .
