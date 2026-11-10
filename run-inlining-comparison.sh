#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$ROOT"

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    cat <<'EOF'
Usage: ./run-inlining-comparison.sh [--print-command] [Hephaestus options...]

Asks for a KLIB backend (Native, JS or Wasm) with an arrow-key menu, then
compiles the same Kotlin source as a KLIB of that backend twice, with the IR
inliner disabled (-Xklib-ir-inliner=disabled) and with cross-module inlining
(-Xklib-ir-inliner=full), and compares acceptance and error diagnostics as is.
This does not execute generated programs.
Defaults: 1,000,000 iterations, 8 workers, batch size 1, disabled reified type
parameters, preserved programs and timing metrics. Uses config_1.53x.json with
the driver's cross-module defaults (import probability 0.5, chain depth 3).
IR dumping and JaCoCo coverage are off.

Backend menu: Up/Down (or k/j, 1-3) moves, Enter confirms, q cancels.
The menu is skipped when BACKEND is set or --backend is forwarded; one of them
is required when stdin or stderr is not a terminal.

Environment overrides:
  BACKEND=native|js|wasm  ITERATIONS=1000000  WORKERS=8
  NAME=<unique timestamp-based name including the backend>
  PYTHON=<repo>/.venv/bin/python  GENERATOR_CONFIG=<repo>/config_1.53x.json
  INHIBIT_SLEEP=0 disables the best-effort macOS caffeinate sleep inhibitor.

Requires Python 3.12+, Hephaestus dependencies and java on PATH.
Only the selected backend's installation is used: Native uses
~/kotlin/kotlin-native/dist/bin/kotlinc-native and
~/kotlin/kotlin-native/dist/klib/common/stdlib; JS (Wasm) uses
~/kotlin/dist/kotlinc/bin/kotlinc-js (kotlinc-wasm) with
kotlin-stdlib-js (kotlin-stdlib-wasm-js)-2.4.255-SNAPSHOT.klib under
~/kotlin/libraries/stdlib/build/libs/.
The driver checks the installation and fixture KLIB compilation in both
inliner modes before fuzzing. Changing PATH does not select compilers.

Examples:
  ./run-inlining-comparison.sh
  BACKEND=js ./run-inlining-comparison.sh
  ITERATIONS=3 WORKERS=1 ./run-inlining-comparison.sh --max-depth 2
  ITERATIONS=0 ./run-inlining-comparison.sh --seconds 3600
  GENERATOR_CONFIG=config_1.53x_singlemodule.json ./run-inlining-comparison.sh
  ./run-inlining-comparison.sh --print-command --backend wasm

Additional options are forwarded last, overriding corresponding CLI defaults.
Keep --language kotlin, -P, --oracle inlining-comparison and --batch 1.
Do not add --rerun, --dump-ir or live JaCoCo flags; comparison rejects them.
--backend jvm is rejected: JVM has no KLIB IR inliner.
Programs, comparison evidence and timing metrics are saved under bugs/<name>/.
For the JS/Native/Wasm/JVM agreement use ./run-backend-comparison.sh.
Full CLI help: .venv/bin/python hephaestus.py --help
EOF
    exit 0
fi

PRINT_COMMAND=0
if [[ ${1:-} == --print-command ]]; then
    PRINT_COMMAND=1
    shift
fi

BACKENDS=(native js wasm)
DESCRIPTIONS=('Kotlin/Native KLIB' 'Kotlin/JS KLIB' 'Kotlin/Wasm KLIB')

forwarded_backend() {
    local backend=
    while (( $# > 0 )); do
        case $1 in
            --backend) backend=${2:-}; shift ;;
            --backend=*) backend=${1#--backend=} ;;
        esac
        shift
    done
    printf '%s' "$backend"
}

choose_backend() {
    local selected=0 count=${#BACKENDS[@]} index key rest
    printf 'Select the backend for inlining comparison (Up/Down, Enter; q cancels):\n' >&2
    printf '\033[?25l' >&2
    trap 'printf "\033[?25h" >&2' EXIT
    trap 'printf "\033[?25h\n" >&2; exit 130' INT TERM
    while true; do
        for index in "${!BACKENDS[@]}"; do
            if (( index == selected )); then
                printf '\r\033[K  \033[7m> %-6s %s\033[0m\n' "${BACKENDS[index]}" "${DESCRIPTIONS[index]}" >&2
            else
                printf '\r\033[K    %-6s %s\n' "${BACKENDS[index]}" "${DESCRIPTIONS[index]}" >&2
            fi
        done
        IFS= read -rsn1 key || key=q
        case $key in
            $'\033')
                rest=
                IFS= read -rsn2 -t 1 rest || true
                case $rest in
                    '[A'|'OA') selected=$(( (selected + count - 1) % count )) ;;
                    '[B'|'OB') selected=$(( (selected + 1) % count )) ;;
                esac
                ;;
            k) selected=$(( (selected + count - 1) % count )) ;;
            j) selected=$(( (selected + 1) % count )) ;;
            [1-9]) if (( key <= count )); then selected=$(( key - 1 )); fi ;;
            '') break ;;
            q|Q)
                trap - EXIT
                printf '\033[?25hCancelled.\n' >&2
                exit 130
                ;;
        esac
        printf '\033[%dA' "$count" >&2
    done
    printf '\033[?25h' >&2
    trap - EXIT INT TERM
    BACKEND=${BACKENDS[selected]}
}

ARG_BACKEND=$(forwarded_backend "$@")
if [[ -n $ARG_BACKEND ]]; then
    BACKEND=$ARG_BACKEND
elif [[ -z ${BACKEND:-} ]]; then
    if [[ ! -t 0 || ! -t 2 ]]; then
        printf 'No terminal for the backend menu. Set BACKEND=native|js|wasm or forward --backend.\n' >&2
        exit 2
    fi
    choose_backend
fi
case $BACKEND in
    native|js|wasm) ;;
    *)
        printf 'Unsupported backend: %s. Inlining comparison requires native, js or wasm.\n' "$BACKEND" >&2
        exit 2
        ;;
esac
printf 'Backend: %s\n' "$BACKEND" >&2

PYTHON=${PYTHON:-"$ROOT/.venv/bin/python"}
NAME=${NAME:-"inlining-comparison-$BACKEND-$(date +%Y%m%d-%H%M%S)-$$"}
COMMAND=(
    "$PYTHON" "$ROOT/hephaestus.py"
    --language kotlin -P --oracle inlining-comparison --backend "$BACKEND"
    --iterations "${ITERATIONS:-1000000}" --workers "${WORKERS:-8}" --batch 1
    --generator-config "${GENERATOR_CONFIG:-$ROOT/config_1.53x.json}"
    --disable-reified-type-parameters --keep-successful --time-metrics
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
