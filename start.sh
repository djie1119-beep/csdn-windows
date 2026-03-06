#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT_DIR/.run"
DEFAULT_LOG_DIR="$ROOT_DIR/.logs/csdn"
LOG_DIR="${LOG_DIR:-$DEFAULT_LOG_DIR}"

CHROME_BIN="${CHROME_BIN:-}"
PROFILE_DIR="$ROOT_DIR/csdn-profile"
TUNNEL_MODE="${TUNNEL_MODE:-npc}"
NPS_BIN="${NPS_BIN:-}"
NPC_BIN="${NPC_BIN:-}"
NPS_CONF="${NPS_CONF:-$ROOT_DIR/csdn-nps/windows_amd64_client/conf/nps.conf}"
PYTHON_BIN="${PYTHON_BIN:-}"
UNLOCKER_DIR="$ROOT_DIR/csdn-unlocker"
WATCHER_PID_FILE="$RUN_DIR/uvicorn-watcher.pid"

mkdir -p "$RUN_DIR"

resolve_log_dir() {
  local preferred="$1"
  local candidates=("$preferred" "$ROOT_DIR/.logs/csdn" "$ROOT_DIR/.logs")

  for candidate in "${candidates[@]}"; do
    if mkdir -p "$candidate" 2>/dev/null; then
      if [[ -w "$candidate" ]]; then
        LOG_DIR="$candidate"
        return 0
      fi
    fi
  done

  return 1
}

if ! resolve_log_dir "$LOG_DIR"; then
  echo "无法找到可写日志目录。"
  echo "你可手动指定并重试，例如："
  echo "  LOG_DIR=\"$ROOT_DIR/.logs/csdn\" ./start.sh"
  exit 1
fi

if [[ "$LOG_DIR" != "$DEFAULT_LOG_DIR" ]]; then
  echo "日志目录自动回退为: $LOG_DIR"
fi

START_LOG_FILE="$LOG_DIR/start.log"
touch "$START_LOG_FILE"
exec > >(tee -a "$START_LOG_FILE") 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] start.sh begin"

is_pid_running() {
  local pid="$1"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

is_windows_shell() {
  local os="$(uname -s 2>/dev/null || true)"
  [[ "$os" == MINGW* || "$os" == MSYS* || "$os" == CYGWIN* ]]
}

is_executable_file() {
  local file="$1"
  if [[ -z "$file" ]]; then
    return 1
  fi
  if is_windows_shell; then
    [[ -f "$file" ]]
  else
    [[ -x "$file" ]]
  fi
}

is_port_listening() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
    return $?
  fi

  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | grep -E "[:.]$port[[:space:]]" >/dev/null 2>&1
    return $?
  fi

  if command -v netstat >/dev/null 2>&1; then
    netstat -an 2>/dev/null | grep -Ei "[:.]$port[[:space:]].*LISTEN(ING)?" >/dev/null 2>&1
    return $?
  fi

  (echo > "/dev/tcp/127.0.0.1/$port") >/dev/null 2>&1
}

is_chrome_cdp_ready() {
  local endpoint="http://127.0.0.1:9222/json/version"

  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 1 "$endpoint" >/dev/null 2>&1
    return $?
  fi

  if command -v wget >/dev/null 2>&1; then
    wget -q -T 1 -O - "$endpoint" >/dev/null 2>&1
    return $?
  fi

  if command -v python3 >/dev/null 2>&1; then
    python3 - <<'PY' >/dev/null 2>&1
import sys
import urllib.request

try:
    urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=1)
except Exception:
    sys.exit(1)
sys.exit(0)
PY
    return $?
  fi

  return 1
}

detect_chrome_bin() {
  if [[ -n "$CHROME_BIN" ]]; then
    if [[ -f "$CHROME_BIN" ]]; then
      return 0
    fi
    echo "  - CHROME_BIN 已设置但文件不存在: $CHROME_BIN"
  fi

  local os="$(uname -s 2>/dev/null || true)"
  local candidates=()

  case "$os" in
    Darwin*)
      candidates=(
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
      )
      ;;
    MINGW*|MSYS*|CYGWIN*)
      local win_home="${HOME:-}"
      local win_user="${USER:-${USERNAME:-}}"
      candidates=(
        "/c/Program Files/Google/Chrome/Application/chrome.exe"
        "/c/Program Files (x86)/Google/Chrome/Application/chrome.exe"
      )
      if [[ -n "$win_home" ]]; then
        candidates+=("$win_home/AppData/Local/Google/Chrome/Application/chrome.exe")
      fi
      if [[ -n "$win_user" ]]; then
        candidates+=("/c/Users/$win_user/AppData/Local/Google/Chrome/Application/chrome.exe")
      fi
      ;;
    *)
      candidates=(
        "/usr/bin/google-chrome"
        "/usr/bin/google-chrome-stable"
        "/usr/bin/chromium"
        "/usr/bin/chromium-browser"
      )
      ;;
  esac

  for candidate in "${candidates[@]}"; do
    if [[ -f "$candidate" ]]; then
      CHROME_BIN="$candidate"
      return 0
    fi
  done

  if command -v google-chrome >/dev/null 2>&1; then
    CHROME_BIN="$(command -v google-chrome)"
    return 0
  fi
  if command -v google-chrome-stable >/dev/null 2>&1; then
    CHROME_BIN="$(command -v google-chrome-stable)"
    return 0
  fi
  if command -v chromium >/dev/null 2>&1; then
    CHROME_BIN="$(command -v chromium)"
    return 0
  fi
  if command -v chromium-browser >/dev/null 2>&1; then
    CHROME_BIN="$(command -v chromium-browser)"
    return 0
  fi

  return 1
}

detect_tunnel_bin() {
  local mode="$1"
  local win_mode="0"
  if is_windows_shell; then
    win_mode="1"
  fi

  if [[ "$mode" == "nps" ]]; then
    if [[ -n "$NPS_BIN" ]] && is_executable_file "$NPS_BIN"; then
      if [[ "$win_mode" == "1" && "$NPS_BIN" != *.exe ]]; then
        :
      else
        return 0
      fi
    fi

    if command -v nps >/dev/null 2>&1; then
      NPS_BIN="$(command -v nps)"
      return 0
    fi

    if [[ "$win_mode" == "1" ]]; then
      local candidates=(
        "$ROOT_DIR/csdn-npc/nps.exe"
        "$ROOT_DIR/csdn-nps/windows_amd64_client/nps.exe"
        "$ROOT_DIR/csdn-nps/windows_client/nps.exe"
      )
      for candidate in "${candidates[@]}"; do
        if is_executable_file "$candidate"; then
          NPS_BIN="$candidate"
          return 0
        fi
      done
      return 1
    fi

    if [[ -n "$NPS_BIN" ]] && is_executable_file "$NPS_BIN"; then
      return 0
    fi

    local candidates=(
      "$ROOT_DIR/csdn-npc/nps.exe"
      "$ROOT_DIR/csdn-npc/nps"
      "$ROOT_DIR/csdn-nps/windows_amd64_client/nps.exe"
      "$ROOT_DIR/csdn-nps/windows_amd64_client/nps"
      "$ROOT_DIR/csdn-nps/windows_client/nps.exe"
      "$ROOT_DIR/csdn-nps/windows_client/nps"
    )
    for candidate in "${candidates[@]}"; do
      if is_executable_file "$candidate"; then
        NPS_BIN="$candidate"
        return 0
      fi
    done
    return 1
  fi

  if [[ -n "$NPC_BIN" ]] && is_executable_file "$NPC_BIN"; then
    if [[ "$win_mode" == "1" && "$NPC_BIN" != *.exe ]]; then
      :
    else
      return 0
    fi
  fi

  if command -v npc >/dev/null 2>&1; then
    NPC_BIN="$(command -v npc)"
    return 0
  fi

  if [[ "$win_mode" == "1" ]]; then
    local candidates=(
      "$ROOT_DIR/csdn-npc/npc.exe"
      "$ROOT_DIR/csdn-nps/windows_amd64_client/npc.exe"
      "$ROOT_DIR/csdn-nps/windows_client/npc.exe"
    )
    for candidate in "${candidates[@]}"; do
      if is_executable_file "$candidate"; then
        NPC_BIN="$candidate"
        return 0
      fi
    done
    return 1
  fi

  if [[ -n "$NPC_BIN" ]] && is_executable_file "$NPC_BIN"; then
    return 0
  fi

  local candidates=(
    "$ROOT_DIR/csdn-npc/npc.exe"
    "$ROOT_DIR/csdn-npc/npc"
    "$ROOT_DIR/csdn-nps/windows_amd64_client/npc.exe"
    "$ROOT_DIR/csdn-nps/windows_amd64_client/npc"
    "$ROOT_DIR/csdn-nps/windows_client/npc.exe"
    "$ROOT_DIR/csdn-nps/windows_client/npc"
  )
  for candidate in "${candidates[@]}"; do
    if is_executable_file "$candidate"; then
      NPC_BIN="$candidate"
      return 0
    fi
  done
  return 1
}

detect_python_bin() {
  is_windowsapps_python() {
    local candidate="$1"
    [[ "$candidate" == *"/Microsoft/WindowsApps/"* || "$candidate" == *"\\Microsoft\\WindowsApps\\"* ]]
  }

  local preferred_python="/c/Users/ZhuanZ/AppData/Local/Programs/Python/Python312/python.exe"
  if [[ -f "$preferred_python" ]]; then
    PYTHON_BIN="$preferred_python"
    return 0
  fi

  local venv_candidates=(
    "$UNLOCKER_DIR/.venv/Scripts/python.exe"
    "$UNLOCKER_DIR/.venv/bin/python3"
    "$UNLOCKER_DIR/.venv/bin/python"
  )

  for candidate in "${venv_candidates[@]}"; do
    if [[ -f "$candidate" ]]; then
      PYTHON_BIN="$candidate"
      return 0
    fi
  done

  local local_candidates=(
    "/c/Users/${USER:-$USERNAME}/AppData/Local/Programs/Python/Python*/python.exe"
    "/c/Python*/python.exe"
    "/c/Program Files/Python*/python.exe"
    "/c/Program Files (x86)/Python*/python.exe"
  )

  for pattern in "${local_candidates[@]}"; do
    for candidate in $pattern; do
      if [[ -f "$candidate" ]]; then
        PYTHON_BIN="$candidate"
        return 0
      fi
    done
  done

  if [[ -n "$PYTHON_BIN" ]] && command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    if is_windowsapps_python "$PYTHON_BIN"; then
      :
    else
      return 0
    fi
  fi

  if [[ -n "$PYTHON_BIN" ]] && [[ -f "$PYTHON_BIN" ]]; then
    if is_windowsapps_python "$PYTHON_BIN"; then
      :
    else
      return 0
    fi
  fi

  if command -v python3 >/dev/null 2>&1; then
    local py3_bin
    py3_bin="$(command -v python3)"
    if ! is_windowsapps_python "$py3_bin"; then
      PYTHON_BIN="$py3_bin"
      return 0
    fi
  fi

  if command -v python >/dev/null 2>&1; then
    local py_bin
    py_bin="$(command -v python)"
    if ! is_windowsapps_python "$py_bin"; then
      PYTHON_BIN="$py_bin"
      return 0
    fi
  fi

  return 1
}

wait_for_port() {
  local port="$1"
  local retries="${2:-30}"
  local delay="${3:-1}"
  for ((i=1; i<=retries; i++)); do
    if is_port_listening "$port"; then
      return 0
    fi
    sleep "$delay"
  done
  return 1
}

wait_for_chrome_cdp() {
  local retries="${1:-30}"
  local delay="${2:-1}"
  for ((i=1; i<=retries; i++)); do
    if is_chrome_cdp_ready; then
      return 0
    fi
    sleep "$delay"
  done
  return 1
}

start_chrome() {
  echo "[1/3] 启动 Chrome（CDP 9222）..."

  local chrome_auto_start="${CHROME_AUTO_START:-1}"

  if is_chrome_cdp_ready || is_port_listening 9222; then
    echo "  - 检测到 9222 已就绪，复用现有 Chrome。"
    return 0
  fi

  if [[ "$chrome_auto_start" != "1" ]]; then
    echo "  - 已禁用自动启动 Chrome（CHROME_AUTO_START=$chrome_auto_start），跳过启动。"
    echo "  - 如需下载类功能，请先手动把浏览器以 9222 调试端口启动。"
    return 0
  fi

  if ! detect_chrome_bin; then
    echo "  - 未找到 Chrome 可执行文件。"
    echo "  - 可手动指定后重试："
    echo "    CHROME_BIN=\"/c/Program Files/Google/Chrome/Application/chrome.exe\" ./start.sh"
    exit 1
  fi

  echo "  - 使用 Chrome: $CHROME_BIN"

  nohup "$CHROME_BIN" \
    --user-data-dir="$PROFILE_DIR" \
    --remote-debugging-port=9222 \
    >"$LOG_DIR/chrome.log" 2>&1 &
  echo $! > "$RUN_DIR/chrome.pid"

  if wait_for_chrome_cdp 30 1; then
    echo "  - Chrome 已就绪（9222）。"
  else
    echo "  - Chrome 启动超时，请查看日志: $LOG_DIR/chrome.log"
    exit 1
  fi
}

start_tunnel_service() {
  local mode="$TUNNEL_MODE"

  if [[ "$mode" == "none" || "$mode" == "off" || "$mode" == "skip" ]]; then
    echo "[2/3] 跳过隧道进程（TUNNEL_MODE=$mode）"
    return 0
  fi

  if [[ "$mode" == "nps" ]]; then
    echo "[2/3] 启动 nps..."

    if [[ -f "$RUN_DIR/nps.pid" ]]; then
      local pid
      pid="$(cat "$RUN_DIR/nps.pid" || true)"
      if is_pid_running "$pid"; then
        echo "  - nps 已在运行 (PID: $pid)"
        return 0
      fi
    fi

    if ! detect_tunnel_bin "nps"; then
      echo "  - 未找到可执行 nps（尝试过: csdn-npc/*, csdn-nps/windows_amd64_client/*, csdn-nps/windows_client/*）"
      exit 1
    fi

    if [[ ! -f "$NPS_CONF" ]]; then
      echo "  - 未找到 nps 配置文件: $NPS_CONF"
      echo "  - 可通过 NPS_CONF 指定，例如："
      echo "    NPS_CONF=\"$ROOT_DIR/csdn-nps/windows_amd64_client/conf/nps.conf\" ./start.sh"
      exit 1
    fi

    local nps_args="${NPS_START_ARGS:-start -c \"$NPS_CONF\"}"
    eval "nohup \"$NPS_BIN\" $nps_args >\"$LOG_DIR/nps.log\" 2>&1 &"
    echo $! > "$RUN_DIR/nps.pid"
    echo "  - nps 已启动 (PID: $(cat "$RUN_DIR/nps.pid"))"
    return 0
  fi

  echo "[2/3] 启动 npc..."

  if [[ -f "$RUN_DIR/npc.pid" ]]; then
    local pid
    pid="$(cat "$RUN_DIR/npc.pid" || true)"
    if is_pid_running "$pid"; then
      echo "  - npc 已在运行 (PID: $pid)"
      return 0
    fi
  fi

  if ! detect_tunnel_bin "npc"; then
    echo "  - 未找到可执行 npc（尝试过: csdn-npc/*, csdn-nps/windows_amd64_client/*, csdn-nps/windows_client/*）"
    echo "  - 解决方法 A：将 npc.exe 放到 $ROOT_DIR/csdn-nps/windows_amd64_client/"
    echo "  - 解决方法 B：直接指定路径启动，例如："
    echo "    NPC_BIN=\"/c/path/to/npc.exe\" ./start.sh"
    echo "  - 解决方法 C：临时跳过隧道，仅启动本地服务："
    echo "    TUNNEL_MODE=none ./start.sh"
    exit 1
  fi

  local npc_args="${NPC_START_ARGS:--server=119.29.79.53:8024 -vkey=234567 -type=tcp}"
  eval "nohup \"$NPC_BIN\" $npc_args >\"$LOG_DIR/npc.log\" 2>&1 &"
  echo $! > "$RUN_DIR/npc.pid"
  echo "  - npc 已启动 (PID: $(cat "$RUN_DIR/npc.pid"))"
}

start_uvicorn() {
  echo "[3/3] 启动主程序 uvicorn..."

  local desired_reload="${UVICORN_RELOAD:-0}"

  uvicorn_cmd_has_reload() {
    local pid="$1"
    if [[ -z "$pid" ]]; then
      return 1
    fi
    local cmd
    cmd="$(ps -p "$pid" -o command= 2>/dev/null || true)"
    [[ "$cmd" == *"uvicorn"* && "$cmd" == *"main:app"* && "$cmd" == *" --reload"* ]]
  }

  get_listen_pid() {
    lsof -tiTCP:8000 -sTCP:LISTEN 2>/dev/null | head -n1
  }

  if is_port_listening 8000; then
    local running_pid=""
    if [[ -f "$RUN_DIR/uvicorn.pid" ]]; then
      local pid
      pid="$(cat "$RUN_DIR/uvicorn.pid" || true)"
      if is_pid_running "$pid"; then
        running_pid="$pid"
      fi
    fi

    if [[ -z "$running_pid" ]]; then
      running_pid="$(get_listen_pid || true)"
    fi

    if [[ "$desired_reload" == "1" ]] && ! uvicorn_cmd_has_reload "$running_pid"; then
      echo "  - 检测到现有 uvicorn 未启用热更新，正在自动重启为 --reload 模式..."
      if [[ -n "$running_pid" ]]; then
        kill "$running_pid" 2>/dev/null || true
        for _ in {1..12}; do
          if is_port_listening 8000; then
            sleep 0.5
          else
            break
          fi
        done
      fi
      rm -f "$RUN_DIR/uvicorn.pid"
    else
      echo "  - 检测到 8000 已监听，复用现有 uvicorn。"
      return 0
    fi
  fi

  if [[ -f "$RUN_DIR/uvicorn.pid" ]]; then
    local pid
    pid="$(cat "$RUN_DIR/uvicorn.pid" || true)"
    if is_pid_running "$pid"; then
      echo "  - 发现旧 uvicorn 进程 (PID: $pid)，但 8000 未监听，先清理重启。"
      kill "$pid" 2>/dev/null || true
      rm -f "$RUN_DIR/uvicorn.pid"
    fi
  fi

  if [[ ! -f "$UNLOCKER_DIR/main.py" ]]; then
    echo "  - 未找到主程序入口: $UNLOCKER_DIR/main.py"
    exit 1
  fi

  if ! detect_python_bin; then
    echo "  - 未找到可用 Python（python3/python）。"
    echo "  - 请先安装 Python 并确保命令可用，或手动指定："
    echo "    PYTHON_BIN=python ./start.sh"
    exit 1
  fi
  echo "  - 使用 Python: $PYTHON_BIN"

  if ! "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import uvicorn
PY
  then
    echo "  - 当前 Python 环境缺少 uvicorn 依赖。"
    echo "  - 请先安装依赖（在仓库根目录执行）："
    echo "    cd csdn-unlocker && python -m pip install -r requirements.txt"
    echo "  - 或指定已安装依赖的解释器："
    echo "    PYTHON_BIN=/c/path/to/python ./start.sh"
    exit 1
  fi

  (
    cd "$UNLOCKER_DIR"
    local reload_flag=""
    local reload_extra=""
    if [[ "$desired_reload" == "1" ]]; then
      reload_flag="--reload"
      reload_extra="--reload-dir $UNLOCKER_DIR --reload-include *.py"
    fi

    nohup "$PYTHON_BIN" -m uvicorn main:app --host 127.0.0.1 --port 8000 $reload_flag $reload_extra \
      >"$LOG_DIR/uvicorn.log" 2>&1 &
    echo $! > "$RUN_DIR/uvicorn.pid"
  )

  if wait_for_port 8000 30 1; then
    if [[ "$desired_reload" == "1" ]]; then
      echo "  - uvicorn 已就绪（热更新模式） (PID: $(cat "$RUN_DIR/uvicorn.pid"))"
    else
      echo "  - uvicorn 已就绪 (PID: $(cat "$RUN_DIR/uvicorn.pid"))"
    fi
  else
    echo "  - uvicorn 启动超时，请查看日志: $LOG_DIR/uvicorn.log"
    exit 1
  fi
}

start_uvicorn_watcher() {
  local enable_watch="${UVICORN_AUTO_RESTART:-1}"
  if [[ "$enable_watch" != "1" ]]; then
  echo "[watcher] 已关闭自动重启监控（UVICORN_AUTO_RESTART=$enable_watch）"
  return 0
  fi

  if [[ -f "$WATCHER_PID_FILE" ]]; then
  local pid
  pid="$(cat "$WATCHER_PID_FILE" || true)"
  if is_pid_running "$pid"; then
    echo "[watcher] 已在运行 (PID: $pid)"
    return 0
  fi
  rm -f "$WATCHER_PID_FILE"
  fi

  echo "[watcher] 启动代码变更监控（改代码自动重启 uvicorn）..."
  if ! detect_python_bin; then
  echo "[watcher] 未找到可用 Python，跳过 watcher"
  return 0
  fi

  nohup "$PYTHON_BIN" - "$UNLOCKER_DIR" "$RUN_DIR/uvicorn.pid" "$LOG_DIR/uvicorn.log" "$PYTHON_BIN" >"$LOG_DIR/uvicorn-watcher.log" 2>&1 <<'PY' &
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

unlocker_dir = Path(sys.argv[1]).resolve()
pid_file = Path(sys.argv[2]).resolve()
uvicorn_log = Path(sys.argv[3]).resolve()
python_cmd = sys.argv[4]


def tree_signature(root: Path) -> tuple:
  rows = []
  for p in root.rglob("*.py"):
    if not p.is_file():
      continue
    try:
      st = p.stat()
    except Exception:
      continue
    rows.append((str(p), int(st.st_mtime_ns), int(st.st_size)))
  rows.sort()
  return tuple(rows)


def read_pid() -> int | None:
  try:
    text = pid_file.read_text(encoding="utf-8").strip()
    if not text:
      return None
    return int(text)
  except Exception:
    return None


def is_running(pid: int | None) -> bool:
  if not pid:
    return False
  try:
    os.kill(pid, 0)
    return True
  except Exception:
    return False


def stop_pid(pid: int | None) -> None:
  if not is_running(pid):
    return
  try:
    os.kill(pid, signal.SIGTERM)
  except Exception:
    return
  for _ in range(12):
    if not is_running(pid):
      break
    time.sleep(0.5)
  if is_running(pid):
    try:
      os.kill(pid, signal.SIGKILL)
    except Exception:
      pass


def start_uvicorn() -> int:
  uvicorn_log.parent.mkdir(parents=True, exist_ok=True)
  with uvicorn_log.open("a", encoding="utf-8") as fp:
    proc = subprocess.Popen(
      [python_cmd, "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8000"],
      cwd=str(unlocker_dir),
      stdout=fp,
      stderr=subprocess.STDOUT,
      start_new_session=True,
    )
  pid_file.write_text(str(proc.pid), encoding="utf-8")
  return proc.pid


last = tree_signature(unlocker_dir)
while True:
  time.sleep(1.0)
  now = tree_signature(unlocker_dir)
  if now == last:
    continue
  last = now

  old_pid = read_pid()
  stop_pid(old_pid)
  new_pid = start_uvicorn()
  print(f"[watcher] 代码变更，已重启 uvicorn: {old_pid} -> {new_pid}", flush=True)
PY
  echo $! > "$WATCHER_PID_FILE"
  echo "[watcher] 已就绪 (PID: $(cat "$WATCHER_PID_FILE"))"
}

start_chrome
start_tunnel_service
start_uvicorn
start_uvicorn_watcher

echo ""
echo "全部启动完成。"
echo "- 前端/反代按你的现有方式访问"
echo "- 后端: http://127.0.0.1:8000"
echo "- 日志目录: $LOG_DIR"
