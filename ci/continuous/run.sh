#!/bin/bash
# Continuous fuzzing without TeamCity. Runs ci.fuzz in segments (default 24h each), one after another.
# Each segment is its own run, so its zip covers exactly that segment. After a segment is packed into
# <OUT>/segment-<n>/hephaestus-results.zip (with summary.json and provenance.json), its bugs/ folder is deleted.
# The TeamCity setup is not used or changed.
#
#   ci/continuous/run.sh [oracle] [segments] [segment-duration]
#   ci/continuous/run.sh backend-comparison 7 24h    # one week
#   ci/continuous/run.sh backend-comparison 0 24h    # until stopped
#
# Environment:
#   OUT=...          where segments go (default: continuous/ in the repository)
#   IMAGE=...        image name (default: hephaestus:continuous)
#   BUILD=0          reuse IMAGE instead of building it (default: build with --no-cache first)
#   CPUS, MEMORY     container limits (default: all Docker host CPUs, host memory minus 1 GiB)
#   MIN_FREE_GIB=10  don't start a segment with less free disk than this
# Ctrl-C or SIGTERM stops the running segment like `docker stop`: it is still packed, then the script exits.
# Rerunning the same command skips segments whose zip already exists.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
ORACLE="${1:-backend-comparison}"
SEGMENTS="${2:-7}"
SEGMENT="${3:-24h}"
OUT="${OUT:-$ROOT/continuous}"
IMAGE="${IMAGE:-hephaestus:continuous}"
MIN_FREE_GIB="${MIN_FREE_GIB:-10}"
NAME="hephaestus-continuous"

[[ "$SEGMENTS" =~ ^[0-9]+$ ]] || { echo "segments must be a number (0 = until stopped)" >&2; exit 2; }
[[ "$SEGMENT" =~ ^[0-9]+[smhd]$ ]] || { echo "segment duration must look like 24h" >&2; exit 2; }

# Same sizing as the TeamCity Fuzz step: the Docker host's CPUs, and its memory minus 1 GiB for the host
read -r DOCKER_CPUS DOCKER_MIB < <(docker info --format '{{.NCPU}} {{.MemTotal}}' | awk '{print $1, int($2 / 1048576)}')
CPUS="${CPUS:-$DOCKER_CPUS}"
MEMORY="${MEMORY:-$(( DOCKER_MIB > 2048 ? DOCKER_MIB - 1024 : DOCKER_MIB / 2 ))m}"

if [ "${BUILD:-1}" = 1 ]; then
  docker build --no-cache --platform linux/amd64 --target runtime -t "$IMAGE" .
fi

STOP=0
on_signal() {
  STOP=1
  echo "== stop requested: stopping the running segment (it is still packed)"
  docker stop --time 120 "$NAME" >/dev/null 2>&1 || true
}
trap on_signal INT TERM

n=0
while [ "$SEGMENTS" -eq 0 ] || [ "$n" -lt "$SEGMENTS" ]; do
  [ "$STOP" = 0 ] || break
  n=$((n + 1))
  dir="$OUT/segment-$n"
  bugs="$dir/bugs"

  if [ -f "$dir/hephaestus-results.zip" ]; then
    echo "== segment $n already packed: skipping"
    continue
  fi
  if [ -e "$dir" ]; then
    echo "$dir exists without a zip: inspect it or remove it (docker can delete its root-owned files), then rerun" >&2
    exit 1
  fi
  free=$(df --output=avail -BG "$ROOT" | tail -1 | tr -dc '0-9')
  if [ "$free" -lt "$MIN_FREE_GIB" ]; then
    echo "== stop: $free GiB free, need $MIN_FREE_GIB GiB to start segment $n"
    break
  fi

  mkdir -p "$bugs" && chmod a+rwx "$dir" "$bugs"
  echo "== segment $n: $ORACLE for $SEGMENT, started $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  rc=0
  docker run --rm --name "$NAME" --network none --cpus "$CPUS" --memory "$MEMORY" \
    -v "$bugs:/opt/hephaestus/bugs" "$IMAGE" "$ORACLE" --duration "$SEGMENT" || rc=$?
  echo "== segment $n: fuzzer exit $rc (0 budget, 74 disk, 124 watchdog, 143 stopped)"
  if [ "$STOP" = 0 ]; then
    case "$rc" in
      0|74|124|143) ;;
      *) echo "segment $n failed with exit $rc; left in $bugs" >&2; exit "$rc" ;;
    esac
  fi

  docker run --rm --network none --entrypoint python \
    -v "$bugs:/opt/hephaestus/bugs:ro" -v "$dir:/out" "$IMAGE" -m ci.results --out /out
  if [ ! -f "$dir/hephaestus-results.zip" ]; then
    echo "packing segment $n failed; left in $bugs" >&2
    exit 1
  fi

  # Compress and delete: the zip keeps the findings and their programs; the raw folder goes.
  # Deleted as root inside a container, because the fuzzer wrote its files as another user.
  docker run --rm --user 0 --entrypoint sh -v "$bugs:/bugs" "$IMAGE" -c 'find /bugs -mindepth 1 -delete'
  rmdir "$bugs"
  echo "== segment $n packed: $dir/hephaestus-results.zip"
done

echo "== done: segments in $OUT"
ls -l "$OUT"/segment-*/hephaestus-results.zip 2>/dev/null || echo "(no zips yet)"
