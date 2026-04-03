#!/bin/bash
# Quick update script — pull latest code and restart bot
# Usage: bash update.sh

set -e
cd /root/autotrade

echo "[→] Pulling latest from git…"
git pull

echo "[→] Updating Python dependencies…"
source venv/bin/activate
pip install -r requirements.txt -q

echo "[→] Restarting bot service…"
systemctl restart autotrade
sleep 2

if systemctl is-active --quiet autotrade; then
    echo "[✓] Bot restarted successfully"
    echo "[→] Health: $(curl -s http://localhost:8001/ | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get(\"ist_now\",\"?\"),\"|\",d.get(\"sched_status\",\"?\"))')"
else
    echo "[✗] Service failed — check: journalctl -u autotrade -n 30"
fi
