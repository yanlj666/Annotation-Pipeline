#!/usr/bin/env bash
set -u

export PATH="/usr/bin:/bin:$PATH"

SCRIPT_PATH="${BASH_SOURCE[0]}"
SCRIPT_DIR="${SCRIPT_PATH%/*}"
if [ "$SCRIPT_DIR" = "$SCRIPT_PATH" ]; then
  SCRIPT_DIR="."
fi
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_DIR="$ROOT_DIR/run"
LOG_DIR="$ROOT_DIR/logs"

SERVE_WATCHDOG_PID="$RUN_DIR/openclaw_serve_watchdog.pid"
SERVE_CHILD_PID="$RUN_DIR/openclaw_serve_child.pid"
LABEL_PID="$RUN_DIR/openclaw_label.pid"
SERVE_LOG="$LOG_DIR/openclaw_serve.log"
LABEL_LOG="$LOG_DIR/openclaw_label.log"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8800}"
WATCHDOG_RESTART_SEC="${WATCHDOG_RESTART_SEC:-5}"

usage() {
  cat <<'EOF'
Usage:
  bash scripts/ap_openclaw.sh serve start|stop|status|restart
  bash scripts/ap_openclaw.sh label start|stop|status

Environment:
  HOST=0.0.0.0 PORT=8800 PYTHON=.venv/bin/python TASK=multiturn_eval_v0.1
EOF
}

ensure_dirs() {
  mkdir -p "$RUN_DIR" "$LOG_DIR"
}

python_bin() {
  if [ -n "${PYTHON:-}" ]; then
    printf '%s\n' "$PYTHON"
  elif [ -x "$ROOT_DIR/.venv/bin/python" ]; then
    printf '%s\n' "$ROOT_DIR/.venv/bin/python"
  else
    printf '%s\n' "python3"
  fi
}

task_args() {
  if [ -n "${TASK:-}" ]; then
    printf '%s\n%s\n' "--task" "$TASK"
  fi
}

read_pid() {
  local file="$1"
  if [ -f "$file" ]; then
    tr -d '[:space:]' < "$file"
  fi
}

is_running() {
  local pid="$1"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

remove_stale_pid() {
  local file="$1"
  local pid
  pid="$(read_pid "$file")"
  if [ -n "$pid" ] && ! is_running "$pid"; then
    rm -f "$file"
  fi
}

stop_pid_group() {
  local file="$1"
  local name="$2"
  local pid
  pid="$(read_pid "$file")"
  if ! is_running "$pid"; then
    rm -f "$file"
    echo "$name is not running"
    return 0
  fi

  echo "stopping $name pid=$pid"
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
  for _ in 1 2 3 4 5; do
    if ! is_running "$pid"; then
      rm -f "$file"
      return 0
    fi
    sleep 1
  done
  kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
  rm -f "$file"
}

serve_watchdog() {
  ensure_dirs
  echo "$$" > "$SERVE_WATCHDOG_PID"
  cd "$ROOT_DIR" || exit 1
  trap 'rm -f "$SERVE_WATCHDOG_PID" "$SERVE_CHILD_PID"; exit 0' TERM INT

  echo "[$(date -Is)] serve watchdog started pid=$$ host=$HOST port=$PORT"
  while true; do
    local py
    py="$(python_bin)"
    echo "[$(date -Is)] starting serve: $py cli.py serve --host $HOST --port $PORT"
    "$py" cli.py serve --host "$HOST" --port "$PORT" $(task_args) &
    local child=$!
    echo "$child" > "$SERVE_CHILD_PID"
    wait "$child"
    local status=$?
    rm -f "$SERVE_CHILD_PID"
    echo "[$(date -Is)] serve exited status=$status; restart in ${WATCHDOG_RESTART_SEC}s"
    sleep "$WATCHDOG_RESTART_SEC"
  done
}

serve_start() {
  ensure_dirs
  remove_stale_pid "$SERVE_WATCHDOG_PID"
  local pid
  pid="$(read_pid "$SERVE_WATCHDOG_PID")"
  if is_running "$pid"; then
    echo "serve watchdog already running pid=$pid"
    return 0
  fi

  setsid bash "$0" _serve_watchdog >> "$SERVE_LOG" 2>&1 < /dev/null &
  sleep 0.3
  pid="$(read_pid "$SERVE_WATCHDOG_PID")"
  echo "serve watchdog started pid=${pid:-unknown}"
  echo "url=http://127.0.0.1:$PORT log=$SERVE_LOG"
}

serve_stop() {
  stop_pid_group "$SERVE_WATCHDOG_PID" "serve watchdog"
  rm -f "$SERVE_CHILD_PID"
}

serve_status() {
  remove_stale_pid "$SERVE_WATCHDOG_PID"
  remove_stale_pid "$SERVE_CHILD_PID"
  local watchdog child
  watchdog="$(read_pid "$SERVE_WATCHDOG_PID")"
  child="$(read_pid "$SERVE_CHILD_PID")"
  if is_running "$watchdog"; then
    echo "serve: running"
    echo "watchdog_pid=$watchdog"
    if is_running "$child"; then
      echo "child_pid=$child"
    else
      echo "child_pid=starting_or_restarting"
    fi
    echo "url=http://127.0.0.1:$PORT"
    echo "log=$SERVE_LOG"
  else
    echo "serve: stopped"
    echo "log=$SERVE_LOG"
  fi
}

label_start() {
  ensure_dirs
  remove_stale_pid "$LABEL_PID"
  local pid
  pid="$(read_pid "$LABEL_PID")"
  if is_running "$pid"; then
    echo "label already running pid=$pid"
    return 0
  fi

  cd "$ROOT_DIR" || exit 1
  local py
  py="$(python_bin)"
  echo "[$(date -Is)] starting label: $py cli.py label --status pending,failed --strict" >> "$LABEL_LOG"
  setsid "$py" cli.py label --status pending,failed --strict $(task_args) >> "$LABEL_LOG" 2>&1 < /dev/null &
  echo "$!" > "$LABEL_PID"
  echo "label started pid=$(read_pid "$LABEL_PID") log=$LABEL_LOG"
}

label_stop() {
  stop_pid_group "$LABEL_PID" "label"
}

label_status() {
  remove_stale_pid "$LABEL_PID"
  local pid
  pid="$(read_pid "$LABEL_PID")"
  if is_running "$pid"; then
    echo "label: running"
    echo "pid=$pid"
    echo "log=$LABEL_LOG"
  else
    echo "label: stopped"
    echo "log=$LABEL_LOG"
  fi
}

if [ "${1:-}" = "_serve_watchdog" ]; then
  serve_watchdog
  exit $?
fi

if [ "$#" -ne 2 ]; then
  usage
  exit 2
fi

target="$1"
action="$2"

case "$target:$action" in
  serve:start) serve_start ;;
  serve:stop) serve_stop ;;
  serve:status) serve_status ;;
  serve:restart) serve_stop; serve_start ;;
  label:start) label_start ;;
  label:stop) label_stop ;;
  label:status) label_status ;;
  *) usage; exit 2 ;;
esac
