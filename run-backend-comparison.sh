#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    cat <<'EOF'
Usage: ./run-backend-comparison.sh [--print-command] [Hephaestus options...]

Compiles the same Kotlin source as JS, Native and Wasm KLIBs and as a linked
JVM JAR, and compares acceptance and error diagnostics as is. This does not
execute generated programs.
Defaults: 1,000,000 iterations, 8 workers, batch size 1, disabled reified type
parameters, preserved programs and timing metrics. Uses config_1.53x.json with
the driver's cross-module defaults (import probability 0.5, chain depth 3).
IR dumping and JaCoCo coverage are off.

Environment overrides:
  ITERATIONS=1000000  WORKERS=8  NAME=<unique timestamp-based name>
  PYTHON=<repo>/.venv/bin/python  GENERATOR_CONFIG=<repo>/config_1.53x.json
  INHIBIT_SLEEP=0 disables the best-effort macOS caffeinate sleep inhibitor.

Requires Python 3.12+, Hephaestus dependencies and java on PATH.
The driver selects ~/kotlin/kotlin-native/dist/bin/kotlinc-native,
~/kotlin/dist/kotlinc/bin/kotlinc-js, ~/kotlin/dist/kotlinc/bin/kotlinc-wasm
and ~/kotlin/dist/kotlinc/bin/kotlinc-jvm.
Native requires ~/kotlin/kotlin-native/dist/klib/common/stdlib; JS and Wasm
require kotlin-stdlib-{js,wasm-js}-2.4.255-SNAPSHOT.klib under
~/kotlin/libraries/stdlib/build/libs/; JVM requires
~/kotlin/dist/kotlinc/lib/kotlin-stdlib.jar.
The driver checks all four installations, matching compiler versions and
fixture artifact compilation before fuzzing. Changing PATH does not select them.

Examples:
  ./run-backend-comparison.sh
  ITERATIONS=3 WORKERS=1 ./run-backend-comparison.sh --max-depth 2
  ITERATIONS=0 ./run-backend-comparison.sh --seconds 3600
  GENERATOR_CONFIG=config_1.53x_singlemodule.json ./run-backend-comparison.sh
  ./run-backend-comparison.sh --print-command

Additional options are forwarded last, overriding corresponding CLI defaults.
Keep --language kotlin, -P, --oracle backend-comparison and --batch 1.
Do not add --rerun, --dump-ir or live JaCoCo flags; comparison rejects them.
--backend does not change the fixed four-backend comparison preset.
Programs, comparison evidence and timing metrics are saved under bugs/<name>/.
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
NAME=${NAME:-"backend-comparison-$(date +%Y%m%d-%H%M%S)-$$"}
COMMAND=(
    "$PYTHON" "$ROOT/hephaestus.py"
    --language kotlin -P --oracle backend-comparison
    --iterations "${ITERATIONS:-1000000}" --workers "${WORKERS:-8}" --batch 1
    --generator-config "${GENERATOR_CONFIG:-$ROOT/config_1.53x.json}"
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

if [[ ${INHIBIT_SLEEP:-1} != 0 ]]; then
    if command -v caffeinate >/dev/null 2>&1; then
        exec caffeinate -i "${COMMAND[@]}"
    fi
    printf 'Sleep inhibition is unavailable; running without it.\n' >&2
fi
exec "${COMMAND[@]}"