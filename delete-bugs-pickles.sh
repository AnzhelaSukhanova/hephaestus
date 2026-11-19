#!/usr/bin/env bash
# Deletes serialized program pickles (*.bin) from all subfolders of bugs/.
# Usage: ./delete-bugs-pickles.sh [-n|--dry-run] [bugs_dir]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUGS_DIR="$SCRIPT_DIR/bugs"
DRY_RUN=0

for arg in "$@"; do
    case "$arg" in
        -n|--dry-run)
            DRY_RUN=1
            ;;
        -h|--help)
            cat <<'EOF'
Usage: delete-bugs-pickles.sh [-n|--dry-run] [bugs_dir]

Deletes every *.bin file (pickled programs) under bugs_dir, recursively.
Default bugs_dir is the bugs/ folder next to this script.
  -n, --dry-run  only count the files that would be deleted
EOF
            exit 0
            ;;
        *)
            BUGS_DIR="$arg"
            ;;
    esac
done

if [ ! -d "$BUGS_DIR" ]; then
    echo "Error: bugs directory not found: $BUGS_DIR" >&2
    exit 1
fi

if [ "$DRY_RUN" -eq 1 ]; then
    count=$(find "$BUGS_DIR" -type f -name '*.bin' | wc -l | tr -d ' ')
    echo "Dry run: $count pickle file(s) under $BUGS_DIR would be deleted."
    exit 0
fi

# No pre-count: matches stream straight into rm in batches of up to 1000 files.
# Each batch reports how many it handled, and the loop shows a live running total.
deleted=0
echo "Deleting pickle files under $BUGS_DIR (live progress, no pre-count)..."
while IFS= read -r batch; do
    deleted=$((deleted + batch))
    printf '\rDeleted %s pickle file(s), %ss elapsed' "$deleted" "$SECONDS" >&2
done < <(find "$BUGS_DIR" -type f -name '*.bin' -print0 \
    | xargs -0 -n 1000 sh -c 'rm -f -- "$@"; printf "%s\n" "$#"' _)
printf '\n' >&2
echo "Deleted $deleted pickle file(s) under $BUGS_DIR."
