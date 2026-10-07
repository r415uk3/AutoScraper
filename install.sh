#!/usr/bin/env bash
# KBTU AutoScraper - Linux server installation (Ubuntu/Debian).
# Run from the repo folder:  sudo ./install.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
    echo "[ERROR] Run with sudo: sudo ./install.sh"
    exit 1
fi

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_USER="${SUDO_USER:-root}"

# Minimal VPS images often ship without sudo, so only use it to drop privileges.
as_user() {
    if [ "$APP_USER" = root ]; then "$@"; else sudo -u "$APP_USER" "$@"; fi
}

echo "[1/6] Setting timezone to Asia/Almaty (ACTIVE_HOURS uses local time)..."
timedatectl set-timezone Asia/Almaty 2>/dev/null \
    || ln -sf /usr/share/zoneinfo/Asia/Almaty /etc/localtime

echo "[2/6] Installing system packages..."
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip

echo "[3/6] Adding swap if RAM is under 2 GB..."
mem_mb=$(awk '/MemTotal/ {print int($2 / 1024)}' /proc/meminfo)
if [ "$mem_mb" -lt 2000 ] && ! swapon --show | grep -q .; then
    # Container VPS (OpenVZ/LXC) don't allow swap files - skip if it fails.
    if fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile; then
        echo "/swapfile none swap sw 0 0" >> /etc/fstab
    else
        rm -f /swapfile
        echo "[WARN] Could not add swap (container VPS?). Keep to 1-2 users on low RAM."
    fi
fi

echo "[4/6] Creating virtual environment and installing dependencies..."
as_user python3 -m venv "$APP_DIR/venv"
as_user "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "[5/6] Installing Chromium and its system libraries..."
"$APP_DIR/venv/bin/playwright" install-deps chromium
as_user "$APP_DIR/venv/bin/playwright" install chromium

echo "[6/6] Installing systemd service..."
cat > /etc/systemd/system/autoscraper.service <<EOF
[Unit]
Description=KBTU AutoScraper
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python open_kbtu.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable autoscraper

for f in .env users.json; do
    [ -f "$APP_DIR/$f" ] && chmod 600 "$APP_DIR/$f" && chown "$APP_USER" "$APP_DIR/$f"
done

echo
echo "Installation complete."
if [ -f "$APP_DIR/.env" ] && [ -f "$APP_DIR/users.json" ]; then
    systemctl restart autoscraper
    echo "Service started. Logs: journalctl -u autoscraper -f"
else
    echo "Next: create .env (see .env.example) and users.json, then run:"
    echo "  chmod 600 .env users.json && sudo systemctl start autoscraper"
fi
