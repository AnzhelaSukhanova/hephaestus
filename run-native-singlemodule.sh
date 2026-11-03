#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    cat <<'EOF'
Usage: ./run-native-singlemodule.sh [--print-command] [Hephaestus options...]

Runs Kotlin/Native single-module generation without JaCoCo coverage.
Defaults: 1,000,000 iterations, 8 workers, disabled reified type parameters,
preserved programs and timing metrics. Uses config_1.53x_singlemodule.json,
which matches config_1.53x.json except crossmodule_probability is zero.
Batch size stays at 1 to match the cross-module timing workflow.
IR dumping is off, matching run-native-crossmodule.sh.

Environment overrides:
  ITERATIONS=1000000  WORKERS=8  NAME=<unique timestamp-based name>
  PYTHON=<repo>/.venv/bin/python
  GENERATOR_CONFIG=<repo>/config_1.53x_singlemodule.json
  INHIBIT_SLEEP=0 disables the best-effort macOS caffeinate sleep inhibitor.

The driver uses ~/kotlin/kotlin-native/dist/bin/kotlinc-native directly;
changing PATH does not override that compiler location.

Examples:
  ./run-native-singlemodule.sh
  ITERATIONS=3 WORKERS=1 ./run-native-singlemodule.sh --max-depth 2
  ITERATIONS=0 ./run-native-singlemodule.sh --seconds 3600
  ./run-native-singlemodule.sh --print-command

Additional options are forwarded last, overriding corresponding CLI defaults.
Keep --backend native, --batch 1 and --keep-everything for this workflow.
Custom generator configurations must set prob.crossmodule_probability to 0.
No JaCoCo flags, jar downloads, instrumented recompilations or coverage reports.
Programs, stats.json and time_metrics.json are saved under bugs/<name>/.
Full CLI help: .venv/bin/python hephaestus.py --help
EOF
    exit 0
fi

PRINT_COMMAND=0
if [[ ${1:-} == --print-command ]]; then
    PRINT_COMMAND=1
    shift
fi

PYTHON=${PYTHON:-"$ROOT/.venv/bin/python"}
NAME=${NAME:-"native-singlemodule-$(date +%Y%m%d-%H%M%S)-$$"}
COMMAND=(
    "$PYTHON" "$ROOT/hephaestus.py"
    --language kotlin -P --backend native
    --iterations "${ITERATIONS:-1000000}" --workers "${WORKERS:-8}" --batch 1
    --generator-config "${GENERATOR_CONFIG:-$ROOT/config_1.53x_singlemodule.json}"
    --disable-reified-type-parameters --keep-everything --time-metrics
    --name "$NAME"
    "$@"
)

printf 'Command: '
printf '%q ' "${COMMAND[@]}"
printf '\n'
if (( PRINT_COMMAND )); then
    exit 0
fi

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    printf 'Python not found: %s. Set PYTHON to a Python 3.12+ environment with Hephaestus installed.\n' "$PYTHON" >&2
    exit 1
fi
"$PYTHON" -c 'import sys; sys.exit("Python 3.12+ is required") if sys.version_info < (3, 12) else None; import networkx'
command -v java >/dev/null || { printf 'java must be on PATH.\n' >&2; exit 1; }
NATIVE_DIST="$HOME/kotlin/kotlin-native/dist"
[[ -x "$NATIVE_DIST/bin/kotlinc-native" && -f "$NATIVE_DIST/konan/lib/kotlin-native-compiler-embeddable.jar" ]] || {
    printf 'Missing Native compiler distribution: %s\n' "$NATIVE_DIST" >&2
    exit 1
}

if [[ ${INHIBIT_SLEEP:-1} != 0 ]]; then
    if command -v caffeinate >/dev/null 2>&1; then
        exec caffeinate -i "${COMMAND[@]}"
    fi
    printf 'Sleep inhibition is unavailable; running without it.\n' >&2
fi
exec "${COMMAND[@]}"