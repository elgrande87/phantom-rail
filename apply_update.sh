#!/usr/bin/env bash
# Safely apply phantom-rail-optimize.zip to a phantom-rail clone, then rebuild and verify it.
set -Eeuo pipefail
IFS=$'\n\t'

log() { printf '[apply_update] %s\n' "$*"; }
die() { printf '[apply_update] ERROR: %s\n' "$*" >&2; exit 1; }

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"

is_project_dir() {
  local dir="$1"
  [[ -d "$dir" && -f "$dir/app.py" && ( -f "$dir/docker-compose.yml" || -f "$dir/docker-compose.yaml" || -f "$dir/compose.yml" || -f "$dir/compose.yaml" ) ]]
}

# Find a likely clone without assuming the caller's working directory or a placeholder path.
find_project_dir() {
  local root compose candidate
  local -a roots=() found=()
  [[ -n "${1:-}" ]] && roots+=("$1")
  roots+=("$PWD" "$SCRIPT_DIR" /home /root /opt /var /srv)
  declare -A seen=()
  for root in "${roots[@]}"; do
    [[ -d "$root" ]] || continue
    root="$(cd -- "$root" 2>/dev/null && pwd -P)" || continue
    [[ -z "${seen[$root]+x}" ]] || continue
    seen[$root]=1
    if is_project_dir "$root"; then found+=("$root"); continue; fi
    # Search common locations only, with a depth limit to avoid traversing the whole filesystem.
    while IFS= read -r -d '' compose; do
      candidate="$(dirname -- "$compose")"
      is_project_dir "$candidate" && found+=("$candidate")
    done < <(find "$root" -maxdepth 5 -type f \( -name docker-compose.yml -o -name docker-compose.yaml -o -name compose.yml -o -name compose.yaml \) -print0 2>/dev/null)
  done
  if ((${#found[@]})); then
    # Prefer a directory explicitly named phantom-rail, then retain discovery order.
    for candidate in "${found[@]}"; do
      if [[ "$(basename -- "$candidate")" == phantom-rail* ]]; then printf '%s' "$candidate"; return 0; fi
    done
    printf '%s' "${found[0]}"
    return 0
  fi
  return 1
}

REQUESTED_PROJECT="${1:-}"
if ! PROJECT_DIR="$(find_project_dir "$REQUESTED_PROJECT")"; then
  die "Could not auto-detect the project. Locate its app.py and docker-compose.yml, then run: bash /path/to/apply_update.sh /path/to/phantom-rail"
fi
if [[ -n "$REQUESTED_PROJECT" && "$PROJECT_DIR" != "$REQUESTED_PROJECT" ]]; then
  log "Using detected project directory: $PROJECT_DIR"
fi

ARCHIVE=""
for candidate in "$PWD/phantom-rail-optimize.zip" "$SCRIPT_DIR/phantom-rail-optimize.zip" "$(dirname -- "$SCRIPT_DIR")/phantom-rail-optimize.zip" /mnt/user-data/outputs/phantom-rail-optimize.zip; do
  if [[ -f "$candidate" ]]; then ARCHIVE="$(cd -- "$(dirname -- "$candidate")" && pwd -P)/$(basename -- "$candidate")"; break; fi
done
[[ -n "$ARCHIVE" ]] || die "phantom-rail-optimize.zip was not found. Keep it beside this script or in the current directory."
command -v python3 >/dev/null 2>&1 || die "python3 is required to validate and extract the ZIP archive safely."
command -v docker >/dev/null 2>&1 || die "docker is required but was not found."

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  die "Neither 'docker compose' nor 'docker-compose' is available."
fi

TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/phantom-rail-update.XXXXXXXX")"
cleanup() { rm -rf -- "$TMP_DIR"; }
trap cleanup EXIT
STAGE="$TMP_DIR/extracted"
mkdir -p "$STAGE"

# Validate archive paths and payload entries, then extract with Python's standard-library zipfile.
python3 - "$ARCHIVE" "$STAGE" <<'PY'
import pathlib, stat, sys, zipfile
archive, destination = map(pathlib.Path, sys.argv[1:])
required = ('app.py', 'templates/index.html', 'static/folders.js')
try:
    with zipfile.ZipFile(archive) as zf:
        matches = {name: [] for name in required}
        for info in zf.infolist():
            name = info.filename
            p = pathlib.PurePosixPath(name)
            if not name or p.is_absolute() or '\\' in name or any(part in ('..', '.') for part in p.parts):
                raise ValueError(f"unsafe archive path: {name!r}")
            mode = (info.external_attr >> 16) & 0xFFFF
            if stat.S_ISLNK(mode):
                raise ValueError(f"symbolic link not allowed: {name!r}")
            if info.is_dir():
                continue
            if name == 'apply_update.sh' or name.endswith('/apply_update.sh'):
                continue
            target = next((r for r in required if name == r or name.endswith('/' + r)), None)
            if target is None:
                raise ValueError(f"unexpected file in update archive: {name!r}")
            matches[target].append(name)
        missing = [k for k, v in matches.items() if len(v) != 1]
        if missing:
            details = ', '.join(f"{k} ({len(matches[k])} matches)" for k in missing)
            raise ValueError(f"archive must contain each required file exactly once: {details}")
        for target, names in matches.items():
            output = destination.joinpath(*pathlib.PurePosixPath(names[0]).parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(names[0]) as source, output.open('wb') as sink:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    sink.write(chunk)
except (OSError, zipfile.BadZipFile, ValueError) as exc:
    print(f"[apply_update] ERROR: invalid update archive: {exc}", file=sys.stderr)
    sys.exit(1)
PY
log "Validated and extracted archive using Python: $(basename -- "$ARCHIVE")"

STAGED_APP="$STAGE/$(python3 - "$STAGE" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1])
for rel in ('app.py',):
    matches = list(root.rglob(rel))
    if len(matches) != 1: raise SystemExit(1)
    print(matches[0].relative_to(root))
PY
)" || die "Expected staged app.py missing or duplicated."
STAGED_HTML="$STAGE/$(python3 - "$STAGE" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1]); matches = list(root.rglob('index.html'))
if len(matches) != 1 or matches[0].parent.name != 'templates': raise SystemExit(1)
print(matches[0].relative_to(root))
PY
)" || die "Expected staged templates/index.html missing or duplicated."
STAGED_JS="$STAGE/$(python3 - "$STAGE" <<'PY'
import pathlib, sys
root = pathlib.Path(sys.argv[1]); matches = list(root.rglob('folders.js'))
if len(matches) != 1 or matches[0].parent.name != 'static': raise SystemExit(1)
print(matches[0].relative_to(root))
PY
)" || die "Expected staged static/folders.js missing or duplicated."

# A missing requirements file is permitted. Create it only if this update needs to declare mutagen.
REQUIREMENTS="$PROJECT_DIR/requirements.txt"
if [[ -f "$REQUIREMENTS" ]] && grep -Eq '^[[:space:]]*(from[[:space:]]+mutagen([.]|[[:space:]]|$)|import[[:space:]]+mutagen([.]|[[:space:]]|$))' "$STAGED_APP"; then
  if ! grep -Eiq '^[[:space:]]*mutagen([[:space:]<>=!~;\[]|$)' "$REQUIREMENTS"; then NEED_MUTAGEN=1; else NEED_MUTAGEN=0; fi
elif [[ ! -f "$REQUIREMENTS" ]] && grep -Eq '^[[:space:]]*(from[[:space:]]+mutagen([.]|[[:space:]]|$)|import[[:space:]]+mutagen([.]|[[:space:]]|$))' "$STAGED_APP"; then
  NEED_MUTAGEN=1
else
  NEED_MUTAGEN=0
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$PROJECT_DIR/backups/phantom-rail-update-$STAMP"
mkdir -p "$BACKUP_DIR"
backup_file() {
  local rel="$1"
  if [[ -f "$PROJECT_DIR/$rel" ]]; then
    mkdir -p "$BACKUP_DIR/$(dirname -- "$rel")"
    cp -a -- "$PROJECT_DIR/$rel" "$BACKUP_DIR/$rel"
  fi
}
backup_file app.py
backup_file templates/index.html
backup_file static/folders.js
[[ "$NEED_MUTAGEN" -eq 0 ]] || backup_file requirements.txt

log "Applying update to: $PROJECT_DIR"
log "Backups saved under: ${BACKUP_DIR#"$PROJECT_DIR"/}"
mkdir -p "$PROJECT_DIR/templates" "$PROJECT_DIR/static"
if [[ ! -f "$REQUIREMENTS" ]]; then
  log "requirements.txt is missing; creating a baseline manifest for the Docker build."
  cat > "$REQUIREMENTS" <<'REQ'
Flask
yt-dlp[default]
yt-dlp-ejs
certifi
gunicorn
REQ
fi
install -m 0644 -- "$STAGED_APP" "$PROJECT_DIR/app.py"
install -m 0644 -- "$STAGED_HTML" "$PROJECT_DIR/templates/index.html"
install -m 0644 -- "$STAGED_JS" "$PROJECT_DIR/static/folders.js"
if [[ "$NEED_MUTAGEN" -eq 1 ]]; then
  touch "$REQUIREMENTS"
  printf '\nmutagen\n' >> "$REQUIREMENTS"
  log "Added mutagen to requirements.txt."
fi

log "Rebuilding and restarting the container (compose up -d --build)."
(
  cd -- "$PROJECT_DIR"
  "${COMPOSE[@]}" up -d --build
)

HEALTH_URL="${PHANTOM_RAIL_HEALTH_URL:-http://127.0.0.1:5000/}"
log "Waiting for the service at $HEALTH_URL"
healthy=0
for attempt in $(seq 1 60); do
  if python3 - "$HEALTH_URL" <<'PY' >/dev/null 2>&1
import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=4) as response:
        sys.exit(0 if 200 <= response.status < 400 else 1)
except Exception:
    sys.exit(1)
PY
  then healthy=1; break; fi
  sleep 2
done
if [[ "$healthy" -ne 1 ]]; then
  log "Compose status follows; service did not return HTTP 2xx/3xx within 120 seconds."
  (cd -- "$PROJECT_DIR" && "${COMPOSE[@]}" ps) || true
  die "Update was applied and backup is available at $BACKUP_DIR; service health check failed."
fi
log "Update applied successfully; HTTP service check passed."
(cd -- "$PROJECT_DIR" && "${COMPOSE[@]}" ps) || true
