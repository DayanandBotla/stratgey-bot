#!/bin/bash
# ╔══════════════════════════════════════════════════════════════════╗
# ║  AutoTrade Bot — VPS Deploy Script                               ║
# ║  Run on a fresh Ubuntu 24.04 Hetzner CX23                       ║
# ║  Usage:  bash deploy.sh                                          ║
# ╚══════════════════════════════════════════════════════════════════╝

set -e  # Exit on any error

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }
info() { echo -e "${CYAN}[→]${NC} $1"; }

echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}   AutoTrade Bot — VPS Deployment              ${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# ── 1. System update ──────────────────────────────────────────────
info "Updating system packages…"
apt-get update -qq && apt-get upgrade -y -qq
log "System updated"

# ── 2. Install dependencies ───────────────────────────────────────
info "Installing Python, nginx, git…"
apt-get install -y -qq python3 python3-pip python3-venv nginx git curl
log "System packages installed"

# ── 3. Project directory ──────────────────────────────────────────
PROJECT_DIR="/root/autotrade"
info "Setting up project at $PROJECT_DIR…"

if [ -d "$PROJECT_DIR/.git" ]; then
    warn "Repo already cloned — pulling latest…"
    cd "$PROJECT_DIR"
    git pull
else
    # ── Replace with your actual GitHub repo URL ──
    REPO_URL="https://github.com/YOUR_GITHUB_USERNAME/autotrade-bot.git"
    info "Cloning repo from $REPO_URL…"
    git clone "$REPO_URL" "$PROJECT_DIR"
    cd "$PROJECT_DIR"
fi

log "Project files ready"

# ── 4. Python virtual environment ────────────────────────────────
info "Creating Python virtual environment…"
if [ ! -d "$PROJECT_DIR/venv" ]; then
    python3 -m venv "$PROJECT_DIR/venv"
fi
source "$PROJECT_DIR/venv/bin/activate"
pip install --upgrade pip -q
pip install -r requirements.txt -q
log "Python dependencies installed"

# ── 5. .env file ──────────────────────────────────────────────────
if [ ! -f "$PROJECT_DIR/.env" ]; then
    warn ".env not found — creating from template…"
    cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"
    echo ""
    echo -e "${RED}╔══════════════════════════════════════════════╗${NC}"
    echo -e "${RED}║  ACTION REQUIRED — Edit .env before starting ║${NC}"
    echo -e "${RED}╚══════════════════════════════════════════════╝${NC}"
    echo ""
    echo "  nano $PROJECT_DIR/.env"
    echo ""
    echo "  Fill in:"
    echo "    DHAN_CLIENT_ID    = your Dhan client ID"
    echo "    DHAN_ACCESS_TOKEN = your Dhan access token"
    echo "    VPS_IP            = $(curl -s ifconfig.me 2>/dev/null || echo 'YOUR_IP')"
    echo "    PAPER_TRADE       = true (keep until validated)"
    echo ""
    read -p "Press ENTER after editing .env to continue…"
fi

# Validate .env has required keys
source "$PROJECT_DIR/.env"
[ -z "$DHAN_CLIENT_ID" ]    && err "DHAN_CLIENT_ID is empty in .env"
[ -z "$DHAN_ACCESS_TOKEN" ] && err "DHAN_ACCESS_TOKEN is empty in .env"
log ".env validated"

# ── 6. Detect and print VPS IP ────────────────────────────────────
VPS_IP=$(curl -s ifconfig.me 2>/dev/null || echo "UNKNOWN")
info "Your VPS fixed IP: ${YELLOW}$VPS_IP${NC}"
info "Make sure this IP is whitelisted in Dhan API settings"

# ── 7. systemd service ────────────────────────────────────────────
info "Installing systemd service…"
cp "$PROJECT_DIR/autotrade.service" /etc/systemd/system/autotrade.service
systemctl daemon-reload
systemctl enable autotrade
systemctl restart autotrade
sleep 2

if systemctl is-active --quiet autotrade; then
    log "systemd service is RUNNING"
else
    err "Service failed to start — check: journalctl -u autotrade -n 50"
fi

# ── 8. Nginx ──────────────────────────────────────────────────────
info "Configuring nginx…"
# Replace placeholder IP in nginx config
sed "s/YOUR_HETZNER_IP_HERE/$VPS_IP/g" "$PROJECT_DIR/nginx.conf" \
    > /etc/nginx/sites-available/autotrade

# Disable default site
rm -f /etc/nginx/sites-enabled/default

# Enable autotrade
ln -sf /etc/nginx/sites-available/autotrade /etc/nginx/sites-enabled/autotrade

nginx -t && systemctl reload nginx
log "Nginx configured and reloaded"

# ── 9. Firewall ───────────────────────────────────────────────────
info "Opening firewall ports…"
if command -v ufw &>/dev/null; then
    ufw allow 22/tcp    # SSH
    ufw allow 80/tcp    # HTTP (nginx → dashboard)
    ufw allow 8001/tcp  # Bot API direct access
    ufw --force enable
    log "UFW firewall configured"
else
    warn "ufw not found — configure firewall manually (ports 22, 80, 8001)"
fi

# ── 10. Health check ──────────────────────────────────────────────
info "Running health check…"
sleep 3
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8001/ 2>/dev/null || echo "000")

if [ "$HTTP_STATUS" = "200" ]; then
    log "Health check PASSED — bot API responding"
else
    warn "Health check returned HTTP $HTTP_STATUS — check logs"
fi

# ── Done ──────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}   ✅  Deployment Complete!                    ${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  Dashboard:   http://$VPS_IP/"
echo "  Bot API:     http://$VPS_IP:8001/"
echo "  Health:      http://$VPS_IP:8001/"
echo "  Status:      http://$VPS_IP:8001/status"
echo ""
echo "  Useful commands:"
echo "  ├── View logs:    journalctl -u autotrade -f"
echo "  ├── Restart bot:  systemctl restart autotrade"
echo "  ├── Stop bot:     systemctl stop autotrade"
echo "  └── Update bot:   cd /root/autotrade && git pull && systemctl restart autotrade"
echo ""
echo -e "${YELLOW}  ⚠️  Open dashboard → set Server URL to http://$VPS_IP:8001${NC}"
echo -e "${YELLOW}  ⚠️  Keep PAPER_TRADE=true until strategy is validated${NC}"
echo ""
