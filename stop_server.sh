#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "正在安全撤销双向挂单并停止策略..."
curl -s -X POST http://127.0.0.1:8888/api/emergency_cancel > /dev/null 2>&1 || true
curl -s -X POST http://127.0.0.1:8888/api/stop > /dev/null 2>&1 || true

if command -v systemctl >/dev/null 2>&1 && [ -f /etc/systemd/system/gate_mm_bot.service ]; then
    sudo systemctl stop gate_mm_bot.service >/dev/null 2>&1 || true
fi

PID=$(cat "$DIR/server.pid" 2>/dev/null || true)
if [ -n "$PID" ]; then
    kill $PID 2>/dev/null || true
    sleep 1
    kill -9 $PID 2>/dev/null || true
    rm -f "$DIR/server.pid"
fi

pkill -f "server.py" 2>/dev/null || true
echo "[完成] Gate LTC 做市程序已安全停止"
