#!/usr/bin/env bash
# RezFi start/restart for /home/www/revfi (revfi.karyva.ai).
# Adapted from ~/freshfi/start.sh (IONOS Apache-jail gunicorn).
#
# IONOS SSH cannot see Apache-jail gunicorn: `ps aux` is empty, pidfiles
# are not killable, and `gunicorn --daemon` from this login dies on logout.
# Durable stop/start goes through Apache CGI:
#   ./start.sh cgi            # write revfi_proxy.cgi + .htaccess (FreshFi: htaccess)
#   ./start.sh stop|start|status
#   START_LOCAL=1 ./start.sh   # SSH-jail gunicorn (dies when you disconnect)
#
# Public front: DirectoryIndex + FallbackResource → revfi_proxy.cgi →
# 127.0.0.1:8099. FreshFi keeps 8090. Do not copy ~/freshfi/start.sh as-is,
# use AddHandler, RewriteRule, or bare Passenger*.
set -euo pipefail
umask 022

APP_NAME="revfi"
APPDIR="$(cd "$(dirname "$0")" && pwd)"
cd "$APPDIR"

PUBLIC_URL="${REZFI_PUBLIC_URL:-https://revfi.karyva.ai}"
TOKEN_FILE="${CTL_TOKEN_FILE:-$APPDIR/data/.ctl_token}"
if [[ -z "${BIND:-}" && -f "$APPDIR/tmp/revfi.bind" ]]; then
  BIND="$(tr -d '[:space:]' < "$APPDIR/tmp/revfi.bind")"
  BIND="${BIND#http://}"
  BIND="${BIND#https://}"
fi
BIND="${BIND:-127.0.0.1:8099}"
if [[ "$BIND" == *":8090" ]]; then
  echo "Refusing BIND=$BIND — FreshFi already uses 127.0.0.1:8090." >&2
  echo "Use 8099 (default): BIND=127.0.0.1:8099 $0" >&2
  exit 1
fi
FRONT="${FRONT:-cgi}"
if [[ "$FRONT" == "php" || "$FRONT" == "proxy" || "$FRONT" == "off" || "$FRONT" == "passenger" ]]; then
  echo "FRONT=$FRONT is not the working public front; using cgi (FallbackResource → gunicorn)" >&2
  FRONT=cgi
fi
WORKERS="${WORKERS:-1}"
TIMEOUT="${TIMEOUT:-120}"
PID_FILE="${PID_FILE:-$APPDIR/tmp/${APP_NAME}.pid}"
# Never /tmp/aifinance.pid — FreshFi may still use that leftover name.
LEGACY_PIDS=("$APPDIR/tmp/aifinance.pid" "/tmp/${APP_NAME}.pid")
LOG_DIR="${LOG_DIR:-$APPDIR/logs}"
ACCESS_LOG="${ACCESS_LOG:-$LOG_DIR/${APP_NAME}.log}"
ERROR_LOG="${ERROR_LOG:-$LOG_DIR/${APP_NAME}-error.log}"
BOOT_LOG="${BOOT_LOG:-$LOG_DIR/${APP_NAME}-boot.log}"

CMD="${1:-restart}"

find_venv_python() {
  local bin_dirs=(
    "$APPDIR/venv/bin"
    "$APPDIR/.venv/bin"
  )
  [[ -n "${VIRTUAL_ENV:-}" ]] && bin_dirs+=("$VIRTUAL_ENV/bin")
  local name child ver
  if [[ -d "$HOME/virtualenv" ]]; then
    for child in "$HOME/virtualenv"/*; do
      [[ -d "$child" ]] || continue
      name="$(basename "$child" | tr '[:upper:]' '[:lower:]')"
      case "$name" in
        *freshfi*)
          continue
          ;;
        *revfi*|*aifinance*|*karyva*|"$(basename "$APPDIR" | tr '[:upper:]' '[:lower:]')")
          bin_dirs+=("$child/bin")
          for ver in "$child"/*/bin; do
            [[ -d "$ver" ]] && bin_dirs+=("$ver")
          done
          ;;
      esac
    done
  fi
  local dir cand
  for dir in "${bin_dirs[@]}"; do
    for cand in "$dir/python3" "$dir/python" "$dir/python3.9" "$dir/python3.9_bin" "$dir/python3.10" "$dir/python3.11" "$dir/python3.12"; do
      if [[ -x "$cand" ]]; then
        echo "$cand"
        return 0
      fi
    done
  done
  return 1
}

if PYTHON="$(find_venv_python)"; then
  :
else
  case "${CMD}" in
    -h|--help|help|backup|backup-cron|keep-alive-cron|pull)
      PYTHON="$(command -v python3 || true)"
      ;;
    *)
      echo "No venv Python found. Expected: $APPDIR/venv/bin/python" >&2
      echo "Create it with:  python3 -m venv venv && ./venv/bin/pip install -r requirements.txt" >&2
      exit 1
      ;;
  esac
fi

mkdir -p "$APPDIR/tmp" "$LOG_DIR" "$APPDIR/data" "$APPDIR/backups"
chmod 755 "$APPDIR" "$APPDIR/tmp" "$LOG_DIR" "$APPDIR/data" "$APPDIR/backups" 2>/dev/null || true

ensure_live_db_writable() {
  local f
  mkdir -p "$APPDIR/data" "$APPDIR/backups"
  chmod 755 "$APPDIR/data" "$APPDIR/backups" 2>/dev/null || true
  for f in \
      "$APPDIR/data/revfi.db" "$APPDIR/data/revfi.db-wal" "$APPDIR/data/revfi.db-shm" \
      "$APPDIR/data/aifinance.db" "$APPDIR/data/aifinance.db-wal" "$APPDIR/data/aifinance.db-shm"; do
    if [[ -f "$f" ]]; then
      chmod 664 "$f" 2>/dev/null || true
    fi
  done
}
ensure_live_db_writable

ensure_runtime_deps() {
  if [[ -z "${PYTHON:-}" || ! -x "$PYTHON" ]]; then
    echo "No Python for pip install" >&2
    return 1
  fi
  if "$PYTHON" -c "from PIL import Image" 2>/dev/null; then
    return 0
  fi
  echo "Pillow missing — installing from requirements.txt with $PYTHON"
  "$PYTHON" -m pip install -r "$APPDIR/requirements.txt"
  if ! "$PYTHON" -c "from PIL import Image" 2>/dev/null; then
    echo "Pillow still missing after pip install. Run: $PYTHON -m pip install Pillow" >&2
    return 1
  fi
  echo "Pillow OK"
}

ensure_venv_readable() {
  local root bindir
  bindir="$(cd "$(dirname "$PYTHON")" && pwd)"
  root="$(cd "$bindir/.." && pwd)"
  chmod 755 "$APPDIR" "$root" "$bindir" 2>/dev/null || true
  chmod 755 "$PYTHON" 2>/dev/null || true
  if [[ -d "$root" ]]; then
    find "$root" -type d -exec chmod 755 {} + 2>/dev/null || true
    find "$root" -type f -exec chmod a+r {} + 2>/dev/null || true
    find "$bindir" -maxdepth 1 -type f -exec chmod a+rx {} + 2>/dev/null || true
  fi
  chmod 644 "$APPDIR/passenger_wsgi.py" "$APPDIR/app.py" 2>/dev/null || true
  echo "venv perms: $(stat -c '%a' "$root" 2>/dev/null || echo '?') $root"
  echo "python perms: $(stat -c '%a' "$PYTHON" 2>/dev/null || echo '?') $PYTHON"
}

process_is_freshfi() {
  local cmd="$1"
  [[ "$cmd" == *"[freshfi]"* || "$cmd" == *"--name freshfi"* || "$cmd" == *"/freshfi/"* ]]
}

stop_pidfile() {
  local pidfile="$1"
  [[ -f "$pidfile" ]] || return 0
  local pid cmd
  pid="$(tr -d '[:space:]' < "$pidfile" 2>/dev/null || true)"
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    cmd="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    if process_is_freshfi "$cmd"; then
      echo "Refusing to stop FreshFi pid $pid from $pidfile" >&2
      return 0
    fi
    echo "Stopping $APP_NAME gunicorn (pid $pid)"
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.25
    done
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$pidfile"
}

remove_freshfi_leftovers() {
  local leftover
  for leftover in \
    "$APPDIR/freshfi_proxy.cgi" \
    "$APPDIR/freshfi_proxy.php" \
    "$APPDIR/tmp/freshfi.bind" \
    "$APPDIR/tmp/freshfi.pid"
  do
    if [[ -e "$leftover" ]]; then
      echo "Removing leftover FreshFi file from RezFi dir: $leftover"
      rm -f "$leftover"
    fi
  done
}

stop_revfi_gunicorn() {
  stop_pidfile "$PID_FILE"
  local extra
  for extra in "${LEGACY_PIDS[@]}"; do
    [[ "$extra" == "$PID_FILE" ]] && continue
    stop_pidfile "$extra"
  done
  if command -v pkill >/dev/null 2>&1; then
    pkill -f -- "gunicorn: master \\[${APP_NAME}\\]" 2>/dev/null || true
    pkill -f -- "gunicorn: worker \\[${APP_NAME}\\]" 2>/dev/null || true
    sleep 0.3
  fi
}

on_ionos_webspace() {
  [[ "${IONOS:-}" == "1" ]] && return 0
  [[ "$APPDIR" == "/home/www/revfi" ]]
}

ensure_ctl_token() {
  mkdir -p "$APPDIR/data"
  if [[ ! -s "$TOKEN_FILE" ]]; then
    "$PYTHON" -c 'import os, secrets, sys
p = sys.argv[1]
open(p, "w").write(secrets.token_urlsafe(32) + "\n")
os.chmod(p, 0o600)' "$TOKEN_FILE"
    echo "Wrote Apache ctl token: $TOKEN_FILE" >&2
  fi
  chmod 600 "$TOKEN_FILE" 2>/dev/null || true
}

apache_ctl() {
  local action="$1"
  ensure_ctl_token
  local token
  token="$(tr -d '[:space:]' < "$TOKEN_FILE")"
  local url="${PUBLIC_URL%/}/__ctl__/${action}"
  local body code
  echo "Apache-jail ctl $action via $url"
  echo "(SSH ps/kill cannot see these PIDs; CGI can.)"
  set +e
  body="$(
    curl -sS -k --max-time 45 \
      -H "X-Ctl-Token: ${token}" \
      -G --data-urlencode "token=${token}" \
      "$url"
  )"
  code=$?
  set -e
  if [[ "$code" -ne 0 ]]; then
    echo "curl failed ($code). Is ${PUBLIC_URL} reachable from this SSH session?" >&2
    printf '%s\n' "$body"
    return 1
  fi
  printf '%s\n' "$body"
}

boot_check() {
  set +e
  "$PYTHON" -c '
from passenger_wsgi import application
name = type(application).__name__
print("WSGI_OK", name)
if name != "Flask":
    raise SystemExit("expected Flask, got %s (see data/startup_error.log)" % name)
' >"$BOOT_LOG" 2>"$ERROR_LOG"
  local rc=$?
  set -e
  return "$rc"
}

reload_passenger() {
  mkdir -p "$APPDIR/tmp"
  chmod 755 "$APPDIR/tmp" 2>/dev/null || true
  touch "$APPDIR/tmp/restart.txt"
  echo "Passenger reload: $APPDIR/tmp/restart.txt"
}

revfi_htaccess_block() {
  cat <<EOF

# BEGIN REZFI
DirectoryIndex revfi_proxy.cgi
FallbackResource /revfi_proxy.cgi
RedirectMatch 404 ^/backups/
# END REZFI
EOF
}

install_cgi_proxy() {
  local src="$APPDIR/revfi_proxy.cgi"
  local tmp
  if [[ ! -f "$src" ]]; then
    echo "Missing $src — restore it: git checkout HEAD -- revfi_proxy.cgi" >&2
    return 1
  fi
  mkdir -p "$APPDIR/tmp"
  tmp="$(mktemp "$APPDIR/tmp/cgi.XXXXXX")"
  {
    echo "#!$PYTHON"
    tail -n +2 "$src"
  } > "$tmp"
  chmod 755 "$tmp"
  mv "$tmp" "$src"
  chmod 755 "$src"
  chmod go-w "$src" 2>/dev/null || true
  echo "CGI proxy: $src -> $BIND (mode $(stat -c '%a' "$src" 2>/dev/null || echo '?'))"
}

ensure_passenger_htaccess() {
  local ht="$APPDIR/.htaccess"
  local tmp mode
  chmod 755 "$APPDIR" 2>/dev/null || true
  tmp="$(mktemp "$APPDIR/tmp/.htaccess.XXXXXX")"
  if [[ -f "$ht" ]]; then
    awk '
      /CLOUDLINUX PASSENGER CONFIGURATION BEGIN/ {keep=1}
      keep {print; if (/CLOUDLINUX PASSENGER CONFIGURATION END/) keep=0; next}
      $0=="# BEGIN REZFI" {skip=1; next}
      skip && $0=="# END REZFI" {skip=0; next}
      skip {next}
      $0=="# BEGIN FRESHFI" {skip=1; next}
      skip && $0=="# END FRESHFI" {skip=0; next}
      skip {next}
      $1=="PassengerEnabled" {next}
      $1=="PassengerAppRoot" {next}
      $1=="PassengerPython" {next}
      $1=="PassengerAppLogFile" {next}
      $1=="PassengerBaseURI" {next}
      $1=="PassengerAppType" {next}
      $1=="PassengerStartupFile" {next}
      $1=="RewriteEngine" {next}
      $1=="RewriteRule" {next}
      $1=="FallbackResource" {next}
      $1=="DirectoryIndex" {next}
      $1=="ErrorDocument" {next}
      $1=="AddHandler" {next}
      $1=="Options" {next}
      {print}
    ' "$ht" > "$tmp"
  else
    : > "$tmp"
  fi
  revfi_htaccess_block >> "$tmp"
  chmod 644 "$tmp"
  mv "$tmp" "$ht"
  chmod 644 "$ht"
  printf '%s\n' "$BIND" > "$APPDIR/tmp/revfi.bind"
  chmod 644 "$APPDIR/tmp/revfi.bind" 2>/dev/null || true
  install_cgi_proxy
  : >> "$LOG_DIR/passenger.log"
  chmod 644 "$LOG_DIR/passenger.log" 2>/dev/null || true
  chmod 755 "$APPDIR" 2>/dev/null || true
  mode="$(stat -c '%a' "$ht" 2>/dev/null || stat -f '%OLp' "$ht" 2>/dev/null || echo '?')"
  echo "Apache FRONT=$FRONT"
  echo "Passenger python: $PYTHON"
  echo "Wrote $ht (mode $mode, dir $(stat -c '%a' "$APPDIR" 2>/dev/null || echo '?'))"
  echo "----- $ht -----"
  cat "$ht"
  echo "---------------"
  if [[ "$mode" != "644" && "$mode" != "664" && "$mode" != "666" ]]; then
    echo "WARNING: $ht mode is $mode; Apache needs 644:  chmod 644 $ht" >&2
  fi
}

print_apache_help() {
  cat <<EOF
Public HTTPS is FallbackResource → revfi_proxy.cgi → gunicorn $BIND.
FreshFi keeps 127.0.0.1:8090. Do not copy ~/freshfi/start.sh as-is.
On IONOS, stop/start talk to Apache CGI (SSH cannot kill those PIDs).

  chmod 644 /home/www/revfi/.htaccess
  chmod 755 /home/www/revfi /home/www/revfi/revfi_proxy.cgi
  ./start.sh cgi
  ./start.sh start
  curl -sS -o /dev/null -w 'public /login %{http_code}\\n' --max-time 10 https://revfi.karyva.ai/login
  curl -sS -o /dev/null -w 'public /health %{http_code}\\n' --max-time 10 https://revfi.karyva.ai/health
EOF
}

check_local_http() {
  local url="http://${BIND}"
  echo "Local gunicorn check ($url):"
  if command -v curl >/dev/null 2>&1; then
    curl -sS -o /dev/null -w "  ${url}/login  %{http_code}\n" --max-time 5 "${url}/login" || echo "  ${url}/login  (curl failed)"
    curl -sS -o /dev/null -w "  ${url}/health %{http_code}\n" --max-time 5 "${url}/health" || echo "  ${url}/health (curl failed)"
  else
    echo "  (curl not installed)"
  fi
  echo "If those are 200 but https://revfi.karyva.ai is 500, Apache is not using this process."
}

print_sqlite_info() {
  echo "=== sqlite (Python stdlib; do not pip install sqlite3) ==="
  echo "Python: $PYTHON ($("$PYTHON" -V 2>&1))"
  "$PYTHON" "$APPDIR/show_db.py"
}

print_diagnose() {
  echo "=== Python ==="
  echo "$PYTHON ($("$PYTHON" -V 2>&1))"
  echo "=== .htaccess ==="
  if [[ -f "$APPDIR/.htaccess" ]]; then
    ls -la "$APPDIR/.htaccess"
    cat "$APPDIR/.htaccess"
  else
    echo "(none)"
  fi
  echo "=== revfi_proxy.cgi ==="
  ls -la "$APPDIR/revfi_proxy.cgi" 2>/dev/null || echo "(missing)"
  echo "=== data/passenger_boot.log ==="
  cat "$APPDIR/data/passenger_boot.log" 2>/dev/null || echo "(none — Apache is not running passenger_wsgi.py)"
  echo "=== data/cgi_hit.log (tail) ==="
  tail -n 40 "$APPDIR/data/cgi_hit.log" 2>/dev/null || echo "(none)"
  echo "=== data/startup_error.log ==="
  tail -n 80 "$APPDIR/data/startup_error.log" 2>/dev/null || echo "(none)"
  echo "=== $ERROR_LOG ==="
  tail -n 80 "$ERROR_LOG" 2>/dev/null || echo "(none)"
  echo "=== Apache error logs ==="
  local found=0 f
  for f in \
    "$HOME/logs/error_log" \
    "$HOME/logs/error.log" \
    "$HOME/logs/revfi.karyva.ai.error.log" \
    "$HOME/logs/revfi.karyva.ai.error_log" \
    "$HOME/logs/${APP_NAME}.error.log" \
    "$APPDIR/logs/error_log"
  do
    if [[ -f "$f" ]]; then
      found=1
      echo "----- $f -----"
      tail -n 40 "$f"
    fi
  done
  if [[ "$found" -eq 0 ]]; then
    echo "(no Apache error log found — paste: ls -la ~/logs; tail -n 50 ~/logs/*error*)"
  fi
  check_local_http
}

print_status() {
  echo "App:     $APP_NAME"
  echo "Root:    $APPDIR"
  echo "Python:  $PYTHON ($("$PYTHON" -V 2>&1))"
  echo "FRONT:   $FRONT"
  echo "BIND:    $BIND"
  echo "Logs:    $ERROR_LOG"
  print_sqlite_info
  if [[ -f "$APPDIR/.htaccess" ]]; then
    echo "htaccess: $(stat -c '%a' "$APPDIR/.htaccess" 2>/dev/null || echo '?') $APPDIR/.htaccess"
  else
    echo "htaccess: missing"
  fi
  if [[ -f "$APPDIR/revfi_proxy.cgi" ]]; then
    echo "cgi:     $(stat -c '%a' "$APPDIR/revfi_proxy.cgi" 2>/dev/null || echo '?') $APPDIR/revfi_proxy.cgi"
  else
    echo "cgi:     missing — run: git checkout HEAD -- revfi_proxy.cgi && $0 cgi"
  fi
  local running=0
  if boot_check; then
    echo "Import:  OK"
    grep WSGI_OK "$BOOT_LOG" 2>/dev/null || true
  else
    echo "Import:  FAILED"
    echo "---- $ERROR_LOG ----"
    cat "$ERROR_LOG" 2>/dev/null || true
    if [[ -f "$APPDIR/data/startup_error.log" ]]; then
      echo "---- data/startup_error.log ----"
      cat "$APPDIR/data/startup_error.log"
    fi
  fi
  echo "Gunicorn [$APP_NAME]:"
  echo "Note: IONOS SSH is a different jail than Apache. Empty \`ps aux\` is expected"
  echo "while https://revfi.karyva.ai is still up. Use ./start.sh status (Apache ctl)."
  if ps -eo pid,cmd 2>/dev/null | grep -E -- "gunicorn: (master|worker) \\[${APP_NAME}\\]" | grep -v grep; then
    running=1
    echo "(those PIDs are in THIS SSH jail and die on logout)"
  else
    echo "(no gunicorn visible in this SSH jail — expected on IONOS)"
  fi
  if on_ionos_webspace; then
    apache_ctl status || true
  elif [[ -f "$APPDIR/tmp/revfi.bind" ]]; then
    echo "Bind file: $(tr -d '[:space:]' < "$APPDIR/tmp/revfi.bind")"
    check_local_http
  fi
  if [[ -f "$APPDIR/tmp/revfi.stopped" ]]; then
    echo "Hold file: $APPDIR/tmp/revfi.stopped (public site will not auto-start)"
  fi
  if [[ "$running" -eq 1 ]]; then
    echo "STARTED (SSH-jail)"
  fi
}

start_gunicorn() {
  if ! "$PYTHON" -c "import gunicorn" 2>/dev/null; then
    echo "gunicorn is not installed in the venv. Run: $PYTHON -m pip install -r requirements.txt" >&2
    exit 1
  fi
  stop_revfi_gunicorn
  echo "Starting $APP_NAME gunicorn on $BIND"
  "$PYTHON" -m gunicorn passenger_wsgi:application \
    --chdir "$APPDIR" \
    --env "REZFI_DB_PATH=$APPDIR/data/revfi.db" \
    --bind "$BIND" \
    --workers "$WORKERS" \
    --timeout "$TIMEOUT" \
    --graceful-timeout 30 \
    --keep-alive 5 \
    --max-requests 400 \
    --max-requests-jitter 40 \
    --name "$APP_NAME" \
    --daemon \
    --pid "$PID_FILE" \
    --access-logfile "$ACCESS_LOG" \
    --error-logfile "$ERROR_LOG"
  local i
  for i in $(seq 1 20); do
    [[ -f "$PID_FILE" ]] && break
    sleep 0.1
  done
  if [[ -f "$PID_FILE" ]] && kill -0 "$(tr -d '[:space:]' < "$PID_FILE")" 2>/dev/null; then
    ln -sfn "$PID_FILE" "/tmp/${APP_NAME}.pid" 2>/dev/null || cp "$PID_FILE" "/tmp/${APP_NAME}.pid"
    echo "$APP_NAME gunicorn pid=$(cat "$PID_FILE") bind=$BIND"
  else
    echo "$APP_NAME gunicorn failed to start" >&2
    tail -n 40 "$ERROR_LOG" 2>/dev/null || true
    exit 1
  fi
}

case "$CMD" in
  stop)
    echo "Stopping $APP_NAME"
    if on_ionos_webspace && [[ "${START_LOCAL:-}" != "1" ]]; then
      apache_ctl stop
    fi
    stop_revfi_gunicorn
    echo "$APP_NAME stop requested (Apache hold + SSH-jail leftovers)"
    exit 0
    ;;
  status)
    print_status
    exit 0
    ;;
  diagnose|logs)
    print_status
    print_diagnose
    exit 0
    ;;
  db)
    print_sqlite_info
    if command -v curl >/dev/null 2>&1; then
      echo "=== public /health ==="
      curl -sS -D - -o /dev/null --max-time 10 "${PUBLIC_URL%/}/health" || true
    fi
    exit 0
    ;;
  backup)
    ensure_live_db_writable
    chmod 755 "$APPDIR/backup.sh" 2>/dev/null || true
    "$APPDIR/backup.sh"
    exit 0
    ;;
  backup-cron)
    ensure_live_db_writable
    chmod 755 "$APPDIR/backup.sh" 2>/dev/null || true
    cron_line="0 3 * * * $APPDIR/backup.sh >> $LOG_DIR/backup.log 2>&1"
    if command -v crontab >/dev/null 2>&1; then
      existing="$(crontab -l 2>/dev/null || true)"
      {
        printf '%s\n' "$existing" | grep -vF "$APPDIR/backup.sh" || true
        echo "$cron_line"
      } | crontab -
      echo "Installed daily 03:00 backup cron:"
      crontab -l
    else
      echo "crontab is not available in this shell. Add this line on the host:"
      echo "$cron_line"
      exit 1
    fi
    exit 0
    ;;
  keep-alive-cron)
    # Public /health hits CGI, which respawns Apache-jail gunicorn if it died.
    ka_line="*/2 * * * * curl -fsS --max-time 20 ${PUBLIC_URL%/}/health >/dev/null || curl -fsS --max-time 25 -o /dev/null ${PUBLIC_URL%/}/login"
    if command -v crontab >/dev/null 2>&1; then
      existing="$(crontab -l 2>/dev/null || true)"
      {
        printf '%s\n' "$existing" | grep -vF "${PUBLIC_URL%/}/health" || true
        echo "$ka_line"
      } | crontab -
      echo "Installed 2-minute keepalive cron:"
      crontab -l
    else
      echo "crontab is not available in this shell. Add this line on the host:"
      echo "$ka_line"
      exit 1
    fi
    exit 0
    ;;
  pull)
    ensure_live_db_writable
    # Never `git pull` / `git pull --rebase` on this host. Logs, pid files, and
    # leftover rebase state make pull conflict; this path is the only update.
    git rebase --abort >/dev/null 2>&1 || true
    git merge --abort >/dev/null 2>&1 || true
    git cherry-pick --abort >/dev/null 2>&1 || true
    rm -rf .git/rebase-merge .git/rebase-apply .git/MERGE_HEAD
    git config --local pull.rebase false
    git config --local rebase.autoStash false
    git config --local pull.ff only
    git fetch origin
    origin_dbs="$(git ls-tree -r --name-only origin/master | grep -E '(^data/.*\.db$|\.db-wal$|\.db-shm$)' || true)"
    if [[ -n "$origin_dbs" ]]; then
      echo "Refusing git reset: origin/master still tracks sqlite files, which would wipe invoices." >&2
      printf '%s\n' "$origin_dbs" >&2
      exit 1
    fi
    git checkout -B master origin/master
    git reset --hard origin/master
    mkdir -p "$APPDIR/tmp" "$LOG_DIR" "$APPDIR/data" "$APPDIR/backups"
    ensure_live_db_writable
    echo "Updated to origin/master. Live sqlite in data/ is untracked and was not replaced."
    echo "Do not run git pull on this host."
    echo "Gunicorn still has the old code until you recycle it:"
    echo "  $0 stop && $0 start"
    exit 0
    ;;
  deps)
    ensure_venv_readable
    echo "Installing $APPDIR/requirements.txt with $PYTHON"
    "$PYTHON" -m pip install -r "$APPDIR/requirements.txt"
    if ! "$PYTHON" -c "from PIL import Image" 2>/dev/null; then
      echo "Pillow still missing after pip install" >&2
      exit 1
    fi
    echo "Pillow OK — invoice upload can stitch pages"
    exit 0
    ;;
  htaccess|cgi)
    remove_freshfi_leftovers
    ensure_venv_readable
    install_cgi_proxy
    ensure_passenger_htaccess
    reload_passenger
    print_apache_help
    exit 0
    ;;
  gunicorn)
    CMD=restart
    ;;
  start|restart) ;;
  -h|--help|help)
    echo "Usage: $0 [restart|start|status|stop|cgi|htaccess|diagnose|db|backup|backup-cron|keep-alive-cron|pull|deps|gunicorn]"
    echo "App root: $APPDIR"
    echo "On IONOS, stop/start talk to Apache CGI (SSH cannot kill those PIDs)."
    echo "./start.sh cgi          write revfi_proxy.cgi + .htaccess only"
    echo "./start.sh db           show which sqlite file and invoice counts (Python, no sqlite3 CLI)"
    echo "./start.sh backup       copy data/revfi.db to backups/revfi_YYYYMMDD.db"
    echo "./start.sh backup-cron  install daily 03:00 cron; prune backups older than 30 days"
    echo "./start.sh keep-alive-cron  ping /health every 2 minutes so CGI restarts gunicorn"
    echo "./start.sh pull         git fetch + reset origin/master without touching live sqlite"
    echo "./start.sh deps         pip install -r requirements.txt (Pillow, Flask, gunicorn, …)"
    echo "START_LOCAL=1 $0        start gunicorn in this SSH jail (dies on logout)"
    echo "FreshFi: cd /home/www/freshfi && ./start.sh stop|start"
    exit 0
    ;;
  *)
    echo "Unknown command: $CMD (use restart, start, status, stop, cgi, htaccess, diagnose, db, backup, backup-cron, keep-alive-cron, pull, deps, or gunicorn)" >&2
    exit 1
    ;;
esac

echo "Using Python: $PYTHON ($("$PYTHON" -V 2>&1))"
remove_freshfi_leftovers
ensure_venv_readable
if ! ensure_runtime_deps; then
  echo "Install deps with: $0 deps   or   $PYTHON -m pip install -r $APPDIR/requirements.txt" >&2
  exit 1
fi
if ! install_cgi_proxy; then
  exit 1
fi
ensure_passenger_htaccess
if ! boot_check; then
  echo "Import: FAILED (need Flask, not a stub function)" >&2
  echo "---- $ERROR_LOG ----" >&2
  cat "$ERROR_LOG" >&2 || true
  if [[ -f "$APPDIR/data/startup_error.log" ]]; then
    echo "---- data/startup_error.log ----" >&2
    cat "$APPDIR/data/startup_error.log" >&2
  fi
  echo "CGI front is installed; fix the import, then rerun $0" >&2
  exit 1
fi
echo "Import: OK"
grep WSGI_OK "$BOOT_LOG" 2>/dev/null || true
ensure_live_db_writable
ensure_venv_readable
if on_ionos_webspace && [[ "${START_LOCAL:-}" != "1" ]]; then
  echo "Starting via Apache CGI so gunicorn survives SSH logout."
  echo "Recycling workers so disk code from ./start.sh pull is loaded."
  apache_ctl stop || true
  apache_ctl start
else
  if on_ionos_webspace; then
    echo "WARNING: START_LOCAL=1 gunicorn lives in this SSH jail and dies on logout." >&2
  fi
  start_gunicorn
fi
install_cgi_proxy
ensure_passenger_htaccess
echo "STARTED"
check_local_http
print_apache_help
echo "Status:   $0 status"
echo "Stop:     $0 stop"
