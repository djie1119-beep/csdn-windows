#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_DIR="$ROOT_DIR/.run"

stop_by_pidfile() {
  local name="$1"
  local pidfile="$2"

  if [[ ! -f "$pidfile" ]]; then
    echo "- $name: 未找到 PID 文件，跳过"
    return 0
  fi

  local pid
  pid="$(cat "$pidfile" || true)"
  if [[ -z "$pid" ]]; then
    rm -f "$pidfile"
    echo "- $name: PID 文件为空，已清理"
    return 0
  fi

  if kill -0 "$pid" 2>/dev/null; then
    echo "- 停止 $name (PID: $pid)"
    kill "$pid" 2>/dev/null || true

    for _ in {1..8}; do
      if kill -0 "$pid" 2>/dev/null; then
        sleep 0.5
      else
        break
      fi
    done

    if kill -0 "$pid" 2>/dev/null; then
      echo "  $name 未退出，强制结束"
      kill -9 "$pid" 2>/dev/null || true
    fi
  else
    echo "- $name: 进程不存在，清理 PID 文件"
  fi

  rm -f "$pidfile"
}

stop_by_pidfile "uvicorn" "$RUN_DIR/uvicorn.pid"
stop_by_pidfile "uvicorn-watcher" "$RUN_DIR/uvicorn-watcher.pid"
stop_by_pidfile "frontend" "$RUN_DIR/frontend.pid"
stop_by_pidfile "nps" "$RUN_DIR/nps.pid"
stop_by_pidfile "npc" "$RUN_DIR/npc.pid"
stop_by_pidfile "chrome" "$RUN_DIR/chrome.pid"

# 兜底清理（防止 PID 文件丢失）
pkill -f "uvicorn main:app --host 127.0.0.1 --port 8000" 2>/dev/null || true
pkill -f "csdn-npc/nps" 2>/dev/null || true
pkill -f "csdn-nps/windows_client/nps" 2>/dev/null || true
pkill -f "csdn-npc/npc.*-server=119.29.79.53:8024.*-vkey=234567.*-type=tcp" 2>/dev/null || true
pkill -f "csdn-nps/windows_client/npc.*-server=119.29.79.53:8024.*-vkey=234567.*-type=tcp" 2>/dev/null || true
pkill -f "csdn-nps/windows_amd64_client/npc.*-server=119.29.79.53:8024.*-vkey=234567.*-type=tcp" 2>/dev/null || true
pkill -f "Google Chrome.*--remote-debugging-port=9222.*csdn-profile" 2>/dev/null || true

echo "停止完成。"
