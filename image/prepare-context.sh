#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
TOOLKIT="${1:?usage: $0 <path-to-toolkit-checkout>}"
TOOLKIT="$(cd "$TOOLKIT" && pwd)"
[ -f "$TOOLKIT/src/aap_migration/analysis/premigration.py" ] || {
  echo "ERROR: $TOOLKIT does not contain the toolkit with premigration-report (src/aap_migration/analysis/premigration.py)" >&2
  exit 1; }
OUT="$HERE/build"
rm -rf "$OUT" && mkdir -p "$OUT/toolkit" "$OUT/migration_ai"
if git -C "$TOOLKIT" rev-parse --git-dir >/dev/null 2>&1; then
  git -C "$TOOLKIT" archive --format=tar HEAD | tar -x -C "$OUT/toolkit"
  echo "toolkit: $(git -C "$TOOLKIT" rev-parse --short HEAD) ($(git -C "$TOOLKIT" rev-parse --abbrev-ref HEAD))"
else
  tar -C "$TOOLKIT" --exclude=.venv --exclude=.git --exclude=exports --exclude=xformed --exclude=reports \
      --exclude=logs --exclude='.env' --exclude='*.db' -cf - . | tar -x -C "$OUT/toolkit"
fi
RETRY="$OUT/toolkit/src/aap_migration/cli/commands/retry.py"
if [ -f "$RETRY" ] && grep -q 'progress.status = None' "$RETRY"; then
  python3 - "$RETRY" <<'PY'
import sys, pathlib
p = pathlib.Path(sys.argv[1])
p.write_text(p.read_text().replace(
    "progress.status = None",
    'progress.status = "pending"  # NOT NULL column (fix applied by aap-migration-ai prepare-context.sh)'))
PY
  echo "toolkit fix applied: retry failed resets failed objects to 'pending' (was NULL)"
fi
tar -C "$ROOT/migration_ai" --exclude='*.egg-info' --exclude=__pycache__ -cf - . | tar -x -C "$OUT/migration_ai"
cp "$HERE/Containerfile" "$OUT/Containerfile"
echo "build context ready: $OUT"
du -sh "$OUT"
