#!/bin/bash
# Cloud: step 3 "Collect results". Execute: Always, even if build stop command was issued.
# Packs the findings, their programs (with dependency modules, sources only) and provenance into
# results/final/; the build's artifact rules publish it with "Publish artifacts: Always".
set -uo pipefail
NAME="hephaestus-%teamcity.build.id%"
IMAGE="hephaestus:%teamcity.build.id%"

# Stopping the Fuzz container also terminates any checkpoint collector running via docker exec.
# A cancelled build can leave the Fuzz container running: SIGTERM it first, so ci.fuzz shuts down
# like Ctrl-C and still leaves a complete bugs/<name>/
docker stop --time 120 "$NAME" >/dev/null 2>&1 || true

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "##teamcity[message text='No image was built, so there are no results to collect' status='WARNING']"
  exit 0
fi

mkdir -p bugs results/agent results/final && chmod a+rwx results/final
# Exit code, start and end times: why the run stopped
docker inspect "$NAME" > results/agent/container.json 2>/dev/null || rm -f results/agent/container.json

# Two CPUs, or one on a one-CPU Docker host (docker refuses more than the host has)
cpus=1
[ "$(docker info --format '{{.NCPU}}' 2>/dev/null)" -ge 2 ] 2>/dev/null && cpus=2
docker run --rm --network none --cpus "$cpus" --memory 4g --entrypoint python \
  -v "$PWD/bugs:/opt/hephaestus/bugs:ro" \
  -v "$PWD/results/agent:/agent:ro" \
  -v "$PWD/results/final:/out" \
  -e TEAMCITY_VERSION \
  "$IMAGE" -m ci.results --agent /agent --out /out --teamcity
rc=$?

if [ "$rc" -ne 0 ]; then
  # Never lose the finding records: publish the run's JSON files as they are
  echo "##teamcity[publishArtifacts 'bugs/*/*.json => hephaestus-results-json.zip']"
  echo "##teamcity[buildProblem description='Collecting results failed (exit $rc); the JSON files were published as hephaestus-results-json.zip']"
  exit 1
fi
