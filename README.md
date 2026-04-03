# AutoTrade Bot 🤖
**NIFTY + Bank Nifty Options Auto-Trading | Dhan Super Orders | IST-safe for any VPS**

---

## How It Works

```
START BOT (dashboard) 
    → Scans every 5min during 09:15–14:00 IST weekdays only
    → 5 strategies evaluated (EMA Cross, ABC Pullback, Breakout, Price Action, VWAP)
    → If 3+ strategies agree + confluence ≥ 0.60 + ADX > 20
    → Dhan Super Order placed (Entry + SL + Target + Trailing in 1 call)
    → Exclusive mode: locks other instrument once signal found
```

**You press Start once. Everything else is automatic.**

---

## First-Time VPS Deployment (Hetzner CX23 / Ubuntu 24.04)

### Step 1 — Create GitHub repo and push code

On your local machine:
```bash
mkdir autotrade-bot && cd autotrade-bot
git init
git remote add origin https://github.com/YOUR_USERNAME/autotrade-bot.git

# Copy all project files here, then:
git add .
git commit -m "initial commit"
git push -u origin main
```

### Step 2 — SSH into your VPS

```bash
ssh root@YOUR_HETZNER_IP
```

### Step 3 — One-command deploy

```bash
curl -O https://raw.githubusercontent.com/YOUR_USERNAME/autotrade-bot/main/deploy.sh
bash deploy.sh
```

The script will:
- Install Python, nginx, git
- Clone your repo
- Create virtual environment + install packages
- Prompt you to fill `.env`
- Install systemd service (auto-start on reboot)
- Configure nginx (dashboard on port 80, API on 8001)
- Open firewall ports

---

## .env Setup (Critical)

```bash
nano /root/autotrade/.env
```

```env
DHAN_CLIENT_ID=1108455416
DHAN_ACCESS_TOKEN=your_actual_token_here
PAPER_TRADE=true          # Keep true until validated!
CAPITAL=500000
VPS_IP=YOUR_HETZNER_IP
```

**Your Dhan token is only in `.env` — never in code, never in git.**

---

## After Deploy — Dhan API Whitelist

1. Get your VPS IP:  `curl ifconfig.me`
2. Go to Dhan → Settings → API → IP Whitelist
3. Add your Hetzner fixed IP
4. Required for Super Orders to work

---

## Dashboard Access

Open in browser: `http://YOUR_HETZNER_IP/`

In dashboard:
- Set **Server URL** to `http://YOUR_HETZNER_IP:8001`
- Press **START BOT**
- Bot will sleep until market opens (9:15 IST) automatically

---

## Useful Commands

```bash
# View live bot logs
journalctl -u autotrade -f

# Check bot health + IST time
curl http://localhost:8001/

# Check market status
curl http://localhost:8001/status

# Manual signal check (bypasses time gate — for testing)
curl http://localhost:8001/scan

# Restart bot
systemctl restart autotrade

# Stop bot  
systemctl stop autotrade

# Update to latest code
bash /root/autotrade/update.sh
```

---

## Market Session Rules (IST — hardcoded, VPS timezone irrelevant)

| Time (IST)     | Bot behaviour                          |
|----------------|----------------------------------------|
| Before 09:15   | Pre-market sleep                       |
| 09:15 – 14:00  | Active scanning + order execution      |
| 14:00 – 15:00  | Monitoring only, NO new entries        |
| 15:00+         | Force exit warning, sleep until next day |
| Saturday       | Full skip — NSE weekend                |
| Sunday         | Full skip — NSE weekend                |
| NSE Holidays   | Skip (list in `multi_strategy_bot.py`) |

---

## Switching to Live Mode

Only after paper trade validation (minimum 30 days, 55%+ win rate):

```bash
nano /root/autotrade/.env
# Change:  PAPER_TRADE=false
systemctl restart autotrade
```

Or toggle Live mode directly on the dashboard.

---

## Project Structure

```
autotrade/
├── multi_strategy_bot.py   ← Main FastAPI bot
├── bot_dashboard.html      ← Control panel UI
├── requirements.txt        ← Python deps
├── .env                    ← Secrets (NOT in git)
├── .env.example            ← Template (safe to commit)
├── .gitignore
├── autotrade.service       ← systemd service
├── nginx.conf              ← Nginx reverse proxy
├── deploy.sh               ← First-time deploy
├── update.sh               ← Pull + restart
└── README.md
```
