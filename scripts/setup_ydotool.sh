#!/bin/bash
# ydotoold を自動起動にする（1 回だけ実行）
set -e
sudo cp "$(dirname "$0")/ydotoold.service" /etc/systemd/system/ydotoold.service
sudo systemctl daemon-reload
sudo pkill -x ydotoold 2>/dev/null || true
sudo systemctl enable --now ydotoold
sleep 1
systemctl status ydotoold --no-pager | head -3
ls -l /tmp/.ydotool_socket
