#!/bin/bash
# Cloud: step 2 "Fuzz". Runs the image's entrypoint, `python -m ci.fuzz <oracle> --duration <d>`.
# ci.fuzz rejects unknown oracles and durations (exit 2), stops by itself when the time budget
# runs out (exit 0) and leaves a complete bugs/<name>/. 143 = stopped by SIGTERM, 124 = watchdog
# (still running 15 minutes past the budget); both still leave complete results.
# Every HEPHAESTUS_CHECKPOINT, `docker exec` packs the live bugs/ with
# `python -m ci.results --checkpoint` and the archive is published under checkpoint/.
# The collector shares the Fuzz container's limits and is the first OOM-kill candidate.
# The Fuzz container gets every CPU and all but 1 GiB of the Docker host's memory for the
# TeamCity agent (HEPHAESTUS_CPUS / HEPHAESTUS_MEMORY override that); ci.fuzz sizes workers
# and the compiler heap from those limits.
set -euo pipefail
NAME="hephaestus-%teamcity.build.id%"
IMAGE="hephaestus:%teamcity.build.id%"

# Containers run as the non-root `fuzzer` user, so every mounted folder must be writable by it
rm -rf bugs results && mkdir -p bugs results/agent results/final && chmod a+rwx bugs results/final

# Facts only the agent knows, for provenance.json (read by ci.results through --agent)
cat > results/agent/teamcity.env <<EOF
server=%teamcity.serverUrl%
build_id=%teamcity.build.id%
build_number=%build.number%
build_type=%system.teamcity.buildType.id%
build_url=%teamcity.serverUrl%/buildConfiguration/%system.teamcity.buildType.id%/%teamcity.build.id%
branch=%teamcity.build.branch%
commit=%build.vcs.number%
agent=%teamcity.agent.name%
oracle=$HEPHAESTUS_ORACLE
duration=$HEPHAESTUS_DURATION
checkpoint_interval=$HEPHAESTUS_CHECKPOINT
EOF
# The Docker host, not the agent's shell: under Colima or Docker Desktop that is the VM
if ! read -r docker_cpus docker_memory < <(docker info --format '{{.NCPU}} {{.MemTotal}}'); then
  echo "##teamcity[buildProblem description='docker info failed: cannot size the Fuzz container']"
  exit 1
fi
docker_mib=$((docker_memory / 1048576))
# 1 GiB for the TeamCity agent; half of hosts with 2 GiB or less
reserve_mib=$((docker_mib > 2048 ? 1024 : docker_mib / 2))
CPUS="${HEPHAESTUS_CPUS:-$docker_cpus}"
MEMORY="${HEPHAESTUS_MEMORY:-$((docker_mib - reserve_mib))m}"
cat > results/agent/host.env <<EOF
kernel=$(uname -sr)
cpus=$(nproc 2>/dev/null)
memory_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null)
docker_cpus=$docker_cpus
docker_memory_bytes=$docker_memory
fuzz_cpus=$CPUS
fuzz_memory=$MEMORY
EOF
echo "Fuzz container: $CPUS CPUs, $MEMORY memory (Docker host: $docker_cpus CPUs, ${docker_mib}m)"
docker image inspect "$IMAGE" > results/agent/image.json || rm -f results/agent/image.json

checkpoints() {
  # Children run in the background and are waited for, so TERM is handled at once and
  # no `sleep` is left holding the build log open after the step.
  local n=0 child="" last=""
  trap '[ -n "$child" ] && kill "$child" 2>/dev/null; exit 0' TERM
  while :; do
    sleep "$HEPHAESTUS_CHECKPOINT" & child=$!
    wait "$child"
    n=$((n + 1))
    local out="results/checkpoint-$n"
    # Created here: a folder Docker creates for a mount belongs to root
    mkdir -p "$out" && chmod a+rwx "$out"
    # The collector shares the Fuzz container's memory cgroup. Raise its OOM score so
    # memory pressure kills this checkpoint before a compiler or the fuzzer itself.
    docker exec "$NAME" sh -c \
      'echo 1000 > /proc/self/oom_score_adj && exec python -m ci.results --checkpoint --agent /results/agent --out "$1"' \
      checkpoint "/$out" & child=$!
    if wait "$child"; then
      # Leading newline: the fuzzer's output may have left a line unfinished
      printf "\n##teamcity[publishArtifacts '%s/hephaestus-results.zip => checkpoint']\n" "$out"
      printf "##teamcity[publishArtifacts '%s/summary.json => checkpoint']\n" "$out"
      printf "##teamcity[publishArtifacts '%s/provenance.json => checkpoint']\n" "$out"
      # Only the latest is kept on the agent: the previous one was published an interval
      # ago, while the latest may still be uploading
      [ -z "$last" ] || rm -rf "$last"
      last=$out
    else
      printf "\n##teamcity[message text='Checkpoint %s failed' status='WARNING']\n" "$n"
      rm -rf "$out"
    fi
  done
}
checkpoints &
loop=$!

set +e
# No --rm: step 3 reads the container's exit code and start/end times for provenance.json.
# TEAMCITY_VERSION turns on service messages (progress, statistics, status);
# HEPHAESTUS_JAVA_OPTS, when set on the agent, replaces the compiler heap ci.fuzz would choose.
docker run --name "$NAME" \
  --network none --cpus "$CPUS" --memory "$MEMORY" \
  -e TEAMCITY_VERSION -e HEPHAESTUS_JAVA_OPTS \
  -v "$PWD/bugs:/opt/hephaestus/bugs" \
  -v "$PWD/results:/results" \
  "$IMAGE" \
  "$HEPHAESTUS_ORACLE" --duration "$HEPHAESTUS_DURATION"
rc=$?
# Killing the docker client doesn't stop its container, hence the rm -f by name
kill "$loop" 2>/dev/null
wait "$loop" 2>/dev/null
set -e

# 0 = the time budget ran out, which is how a normal run ends
if [ "$rc" -ne 0 ]; then
  echo "##teamcity[buildProblem description='Fuzzer exited with code $rc']"
  exit "$rc"
fi
