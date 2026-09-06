#!/usr/bin/env bash
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# 优先使用 systemd 服务托管（自带崩溃自愈与 7x24 守护）
if command -v systemctl >/dev/null 2>&1 && [ -f /etc/systemd/system/gate_mm_bot.service ]; then
    echo "正在通过 systemd 服务启动 gate_mm_bot..."
    sudo systemctl restart gate_mm_bot.service
    sudo systemctl enable gate_mm_bot.service >/dev/null 2>&1 || true
    sleep 2
    if systemctl is-active --quiet gate_mm_bot.service; then
        echo "[启动成功] gate_mm_bot.service 已在后台长期守护运行"
        echo "Web 监控终端: http://124.156.193.157:8888"
        echo "运行日志: tail -f \"$DIR/server.log\""
        exit 0
    fi
fi

# 备用方案：setsid nohup 脱机启动
PY_BIN="/home/ubuntu/venv/bin/python3"
if [ ! -f "$PY_BIN" ]; then
    PY_BIN="python3"
fi

PID=$(pgrep -f "server.py" | head -n 1 || true)
if [ -n "$PID" ]; then
    echo "[提示] gate_mm_bot 已经在运行中 (PID: $PID)"
    echo "Web 监控地址: http://124.156.193.157:8888"
    exit 0
fi

setsid nohup "$PY_BIN" server.py "$@" < /dev/null >> "$DIR/server.log" 2>&1 &
sleep 2

NEW_PID=$(pgrep -f "server.py" | head -n 1 || true)
if [ -n "$NEW_PID" ]; then
    echo "$NEW_PID" > "$DIR/server.pid"
    echo "[启动成功] Gate MM 监控服务已脱机运行 (PID: $NEW_PID)"
    echo "Web 监控终端: http://124.156.193.157:8888"
    echo "运行日志: tail -f \"$DIR/server.log\""
else
    echo "[错误] 服务启动失败，请查看 $DIR/server.log"
    exit 1
fi
