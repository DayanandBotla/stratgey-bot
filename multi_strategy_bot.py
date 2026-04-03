"""
╔══════════════════════════════════════════════════════════════════════╗
║         MULTI-STRATEGY AUTO TRADE BOT — DHAN SUPER ORDERS           ║
║  Instruments : NIFTY Options (5min) + Bank Nifty Options (5min)     ║
║  Strategies  : EMA Cross | ABC Pullback | Breakout | Price Action    ║
║                VWAP Momentum                                         ║
║  Execution   : Dhan Super Orders (SL+Target+Trailing) | Paper Mode  ║
║                                                                      ║
║  ⚠️  TIMEZONE : All time logic uses IST (Asia/Kolkata) via pytz      ║
║                 Safe to deploy on Singapore VPS / any cloud server   ║
╚══════════════════════════════════════════════════════════════════════╝

HOW IT WORKS:
─────────────
1. Run the bot:     uvicorn multi_strategy_bot:app --host 0.0.0.0 --port 8001
2. Open dashboard:  bot_dashboard.html → enter server URL → click START BOT
3. The dashboard polls /signals/NIFTY and /signals/BANKNIFTY every N minutes
4. If a signal is found it auto-executes via Dhan Super Orders

The bot itself also has a built-in scheduler. The scheduler:
  • Runs continuously in the background
  • Uses IST timezone for ALL time checks — safe from Singapore/any VPS
  • Skips Saturday + Sunday (NSE holiday)
  • Scans only between 09:15 – 15:00 IST
  • Takes new entries only before 14:00 IST
  • Force-exits all positions at 15:00 IST (square-off warning)
  • Sleeps smartly: if market closed → sleeps until next market open
  • Resets daily trade counter at midnight IST

You do NOT need to click anything after starting — it runs fully automated.
"Scan Now" on the dashboard is only for manual testing.

Setup:
    pip install fastapi uvicorn httpx pandas numpy pytz

Run:
    uvicorn multi_strategy_bot:app --host 0.0.0.0 --port 8001

Endpoints:
    GET  /              → Health + IST time + market status
    GET  /scan          → Manual trigger (ignores time gate, for testing)
    GET  /signals/NIFTY → Signal breakdown for NIFTY
    GET  /signals/BANKNIFTY
    GET  /trades        → Paper / live trade log
    GET  /performance   → P&L summary
    GET  /status        → Detailed scheduler status
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta, time as dtime
from dataclasses import dataclass
from typing import Optional
from enum import Enum
from pathlib import Path

import httpx
import pandas as pd
import numpy as np
import pytz
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from dotenv import load_dotenv

# Load .env from same directory as this file
load_dotenv(dotenv_path=Path(__file__).parent / ".env")


# ══════════════════════════════════════════════════════════════════════
# TIMEZONE  — always IST regardless of server location
# ══════════════════════════════════════════════════════════════════════

IST = pytz.timezone("Asia/Kolkata")

def now_ist() -> datetime:
    """Current datetime in IST. Works correctly from any VPS timezone."""
    return datetime.now(IST)

def today_ist():
    return now_ist().date()


# ══════════════════════════════════════════════════════════════════════
# MARKET SESSION RULES  (all IST)
# ══════════════════════════════════════════════════════════════════════

MARKET_OPEN_IST  = dtime(9, 15)    # NSE market open
LAST_ENTRY_IST   = dtime(14, 0)    # No new positions after this
FORCE_EXIT_IST   = dtime(15, 0)    # Square-off all at this time
MARKET_CLOSE_IST = dtime(15, 30)   # Official close

# Weekdays are 0=Mon … 4=Fri. 5=Sat 6=Sun are NSE holidays.
NSE_WEEKEND = {5, 6}

# Additional manual holidays — add dates as "YYYY-MM-DD" strings
NSE_HOLIDAYS = {
    # 2025 NSE holidays (add more as needed)
    "2025-01-26",  # Republic Day
    "2025-03-14",  # Holi
    "2025-04-14",  # Dr. Ambedkar Jayanti
    "2025-04-18",  # Good Friday
    "2025-05-01",  # Maharashtra Day
    "2025-08-15",  # Independence Day
    "2025-10-02",  # Gandhi Jayanti
    "2025-10-24",  # Dussehra
    "2025-11-05",  # Diwali Laxmi Puja
    "2025-12-25",  # Christmas
    # 2026 holidays — update from NSE website each year
    "2026-01-26",  # Republic Day
    "2026-03-20",  # Holi
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-08-15",  # Independence Day
}

def is_market_day() -> bool:
    """True if today is a trading day (weekday + not NSE holiday)."""
    n = now_ist()
    if n.weekday() in NSE_WEEKEND:
        return False
    if n.strftime("%Y-%m-%d") in NSE_HOLIDAYS:
        return False
    return True

def is_market_open() -> bool:
    """True if current IST time is within market hours AND it's a trading day."""
    if not is_market_day():
        return False
    t = now_ist().time()
    return MARKET_OPEN_IST <= t < FORCE_EXIT_IST

def is_entry_allowed() -> bool:
    """True if we can still take new positions (before 14:00 IST)."""
    if not is_market_day():
        return False
    t = now_ist().time()
    return MARKET_OPEN_IST <= t < LAST_ENTRY_IST

def is_force_exit_time() -> bool:
    """True if it's time to square off all positions."""
    if not is_market_day():
        return False
    t = now_ist().time()
    return t >= FORCE_EXIT_IST

def seconds_until_next_market_open() -> int:
    """How many seconds until next market open (9:15 IST next trading day)."""
    n = now_ist()
    # Start with today's 9:15
    target = IST.localize(datetime.combine(n.date(), MARKET_OPEN_IST))
    if n >= target:
        # Already past today's open — go to next calendar day
        target += timedelta(days=1)
    # Skip weekends and holidays
    while target.weekday() in NSE_WEEKEND or target.strftime("%Y-%m-%d") in NSE_HOLIDAYS:
        target += timedelta(days=1)
    diff = (target - n).total_seconds()
    return max(int(diff), 60)


# ══════════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════════

# ── Read from .env (falls back to defaults if not set) ────────────────
DHAN_CLIENT_ID    = os.getenv("DHAN_CLIENT_ID", "")
DHAN_ACCESS_TOKEN = os.getenv("DHAN_ACCESS_TOKEN", "")

PAPER_TRADE       = os.getenv("PAPER_TRADE", "true").lower() == "true"
CAPITAL           = float(os.getenv("CAPITAL", "500000"))
MAX_RISK_PCT      = float(os.getenv("MAX_RISK_PCT", "0.03"))
MAX_DAILY_TRADES  = int(os.getenv("MAX_DAILY_TRADES", "4"))
MAX_DAILY_LOSS    = float(os.getenv("MAX_DAILY_LOSS", "0.02"))
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "300"))
MIN_CONFLUENCE    = float(os.getenv("MIN_CONFLUENCE", "0.60"))
MIN_STRATEGY_AGREE= int(os.getenv("MIN_STRATEGY_AGREE", "3"))

# Parse instruments from env: "NIFTY,BANKNIFTY" → ["NIFTY","BANKNIFTY"]
_inst_env = os.getenv("SCAN_INSTRUMENTS", "NIFTY,BANKNIFTY")
SCAN_INSTRUMENTS = [i.strip().upper() for i in _inst_env.split(",") if i.strip()]

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8001"))

# Validate required secrets at startup
if not DHAN_CLIENT_ID or not DHAN_ACCESS_TOKEN:
    raise RuntimeError(
        "❌  DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN must be set in .env file\n"
        "    Copy .env.example → .env and fill in your credentials."
    )

STRATEGY_WEIGHTS = {
    "ema_cross"     : 0.20,
    "abc_pullback"  : 0.25,
    "breakout"      : 0.25,
    "price_action"  : 0.20,
    "vwap_momentum" : 0.10,
}

INSTRUMENTS = {
    "NIFTY": {
        "security_id" : "13",
        "exchange"    : "NSE_FNO",
        "lot_size"    : 65,
        "strike_step" : 50,
        "expiry_day"  : 1,    # Tuesday
    },
    "BANKNIFTY": {
        "security_id" : "25",
        "exchange"    : "NSE_FNO",
        "lot_size"    : 30,
        "strike_step" : 100,
        "expiry_day"  : 2,    # Wednesday
    },
}


# ══════════════════════════════════════════════════════════════════════
# DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════

class Direction(Enum):
    LONG    = "LONG"
    SHORT   = "SHORT"
    NEUTRAL = "NEUTRAL"


@dataclass
class Signal:
    strategy    : str
    direction   : Direction
    strength    : float
    entry_price : float
    sl_price    : float
    target_price: float
    reason      : str


@dataclass
class TradeDecision:
    instrument       : str
    direction        : Direction
    confluence_score : float
    signals          : list
    entry_price      : float
    sl_price         : float
    target_price     : float
    trailing_jump    : float
    option_type      : str
    timestamp        : datetime


# ══════════════════════════════════════════════════════════════════════
# INDICATOR HELPERS
# ══════════════════════════════════════════════════════════════════════

def calc_vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    return (typical * df["volume"]).cumsum() / df["volume"].cumsum()

def calc_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()

def calc_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    delta    = prices.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calc_adx(df: pd.DataFrame, period: int = 14) -> float:
    high, low, close = df["high"], df["low"], df["close"]
    plus_dm  = high.diff().clip(lower=0)
    minus_dm = (-low.diff()).clip(lower=0)
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr      = tr.ewm(span=period, adjust=False).mean()
    plus_di  = 100 * plus_dm.ewm(span=period, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(span=period, adjust=False).mean() / atr
    dx       = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di)
    adx      = dx.ewm(span=period, adjust=False).mean()
    return adx.iloc[-1]


# ══════════════════════════════════════════════════════════════════════
# STRATEGY 1 — EMA CROSSOVER (9/21/50)
# ══════════════════════════════════════════════════════════════════════

class EMACrossStrategy:
    def __init__(self, fast=9, slow=21, trend=50):
        self.fast, self.slow, self.trend = fast, slow, trend

    def analyze(self, df: pd.DataFrame) -> Optional[Signal]:
        if len(df) < self.trend + 5:
            return None
        closes    = df["close"]
        ema_fast  = calc_ema(closes, self.fast)
        ema_slow  = calc_ema(closes, self.slow)
        ema_trend = calc_ema(closes, self.trend)
        vwap      = calc_vwap(df)
        cf, pf    = ema_fast.iloc[-1], ema_fast.iloc[-2]
        cs, ps    = ema_slow.iloc[-1], ema_slow.iloc[-2]
        price     = closes.iloc[-1]

        if pf <= ps and cf > cs and price > vwap.iloc[-1] and price > ema_trend.iloc[-1]:
            sep = (cf - cs) / cs * 100
            sl  = df["low"].iloc[-3:].min() * 0.999
            return Signal("ema_cross", Direction.LONG, min(0.55 + sep*8, 0.95),
                          price, sl, price + (price - sl)*2.0,
                          f"9/21 Golden Cross | >VWAP | Above 50EMA")

        if pf >= ps and cf < cs and price < vwap.iloc[-1] and price < ema_trend.iloc[-1]:
            sep = (cs - cf) / cs * 100
            sl  = df["high"].iloc[-3:].max() * 1.001
            return Signal("ema_cross", Direction.SHORT, min(0.55 + sep*8, 0.95),
                          price, sl, price - (sl - price)*2.0,
                          f"9/21 Death Cross | <VWAP | Below 50EMA")
        return None


# ══════════════════════════════════════════════════════════════════════
# STRATEGY 2 — ABC PULLBACK
# ══════════════════════════════════════════════════════════════════════

class ABCPullbackStrategy:
    FIB_MIN, FIB_MAX = 0.382, 0.618

    def analyze(self, df: pd.DataFrame) -> Optional[Signal]:
        if len(df) < 20:
            return None
        window = df.iloc[-25:]
        sh_idx = window["high"].idxmax()
        sl_idx = window["low"].idxmin()
        sh     = window.loc[sh_idx, "high"]
        sl_val = window.loc[sl_idx, "low"]
        price  = df["close"].iloc[-1]
        move   = sh - sl_val

        if sl_idx < sh_idx:
            f382 = sh - move * self.FIB_MIN
            f618 = sh - move * self.FIB_MAX
            if f618 <= price <= f382 and self._bull_rev(df.iloc[-3:]):
                st = 0.85 - (price - f618)/(f382 - f618) * 0.3
                sl = f618 - move * 0.05
                return Signal("abc_pullback", Direction.LONG, min(st, 0.90),
                              price, sl, sh + move*0.618,
                              f"ABC LONG | C in fib {f618:.0f}–{f382:.0f}")

        elif sh_idx < sl_idx:
            f382 = sl_val + move * self.FIB_MIN
            f618 = sl_val + move * self.FIB_MAX
            if f382 <= price <= f618 and self._bear_rev(df.iloc[-3:]):
                st = 0.85 - (f618 - price)/(f618 - f382) * 0.3
                sl = f618 + move * 0.05
                return Signal("abc_pullback", Direction.SHORT, min(st, 0.90),
                              price, sl, sl_val - move*0.618,
                              f"ABC SHORT | C in fib {f382:.0f}–{f618:.0f}")
        return None

    def _bull_rev(self, c):
        cur, prv = c.iloc[-1], c.iloc[-2]
        body = abs(cur["close"] - cur["open"])
        wick = min(cur["open"], cur["close"]) - cur["low"]
        eng  = (cur["close"] > cur["open"] and prv["close"] < prv["open"] and
                cur["open"] < prv["close"] and cur["close"] > prv["open"])
        return (body > 0 and wick >= 2*body) or eng

    def _bear_rev(self, c):
        cur, prv = c.iloc[-1], c.iloc[-2]
        body = abs(cur["close"] - cur["open"])
        wick = cur["high"] - max(cur["open"], cur["close"])
        eng  = (cur["close"] < cur["open"] and prv["close"] > prv["open"] and
                cur["open"] > prv["close"] and cur["close"] < prv["open"])
        return (body > 0 and wick >= 2*body) or eng


# ══════════════════════════════════════════════════════════════════════
# STRATEGY 3 — BREAKOUT / BREAKDOWN
# ══════════════════════════════════════════════════════════════════════

class BreakoutBreakdownStrategy:
    def __init__(self, vol_mult=1.5):
        self.vol_mult = vol_mult

    def analyze(self, df: pd.DataFrame) -> Optional[Signal]:
        if len(df) < 25:
            return None
        levels  = self._get_levels(df)
        curr    = df.iloc[-1]
        prev    = df.iloc[-2]
        price   = curr["close"]
        avg_vol = df["volume"].iloc[-21:-1].mean()
        rsi     = calc_rsi(df["close"]).iloc[-1]
        best, best_str = None, 0.0

        for level, ltype, touches in levels:
            if ltype == "resistance" and prev["close"] <= level < curr["close"]:
                if curr["volume"] >= avg_vol * self.vol_mult and rsi < 75:
                    vol_r = curr["volume"] / avg_vol
                    st    = min(0.50 + (vol_r-1)*0.20 + touches*0.05, 0.95)
                    if st > best_str:
                        sl   = level * 0.997
                        best = Signal("breakout", Direction.LONG, st, price,
                                      sl, price + (price - sl)*2.5,
                                      f"Breakout R={level:.0f} | Vol {vol_r:.1f}x")
                        best_str = st

            elif ltype == "support" and prev["close"] >= level > curr["close"]:
                if curr["volume"] >= avg_vol * self.vol_mult and rsi > 25:
                    vol_r = curr["volume"] / avg_vol
                    st    = min(0.50 + (vol_r-1)*0.20 + touches*0.05, 0.95)
                    if st > best_str:
                        sl   = level * 1.003
                        best = Signal("breakout", Direction.SHORT, st, price,
                                      sl, price - (sl - price)*2.5,
                                      f"Breakdown S={level:.0f} | Vol {vol_r:.1f}x")
                        best_str = st
        return best

    def _get_levels(self, df):
        dh, dl, dc = df["high"].max(), df["low"].min(), df["close"].iloc[-1]
        pivot = (dh + dl + dc) / 3
        pivots = [
            (2*pivot - dl, "resistance", 2),
            (pivot + (dh - dl), "resistance", 2),
            (2*pivot - dh, "support", 2),
            (pivot - (dh - dl), "support", 2),
        ]
        swings = []
        for i in range(2, len(df)-2):
            h, l = df["high"], df["low"]
            if h.iloc[i] == h.iloc[i-2:i+3].max():
                swings.append((h.iloc[i], "resistance", 1))
            if l.iloc[i] == l.iloc[i-2:i+3].min():
                swings.append((l.iloc[i], "support", 1))
        return pivots + self._cluster(swings)

    @staticmethod
    def _cluster(levels, tol=0.003):
        out, used = [], set()
        for i, (p, lt, t) in enumerate(levels):
            if i in used: continue
            grp = [(p, t)]
            for j, (p2, lt2, t2) in enumerate(levels):
                if j != i and j not in used and abs(p-p2)/p < tol:
                    grp.append((p2, t2)); used.add(j)
            avg_p = sum(x for x, _ in grp) / len(grp)
            total = sum(x for _, x in grp)
            out.append((avg_p, lt, total)); used.add(i)
        return out


# ══════════════════════════════════════════════════════════════════════
# STRATEGY 4 — PRICE ACTION (12 candlestick patterns)
# ══════════════════════════════════════════════════════════════════════

class PriceActionStrategy:
    def analyze(self, df: pd.DataFrame) -> Optional[Signal]:
        if len(df) < 5: return None
        vwap  = calc_vwap(df).iloc[-1]
        rsi   = calc_rsi(df["close"]).iloc[-1]
        price = df["close"].iloc[-1]

        patterns = [
            ("Bullish Engulfing",    Direction.LONG,  0.78, self._bull_engulf(df)),
            ("Bearish Engulfing",    Direction.SHORT, 0.78, self._bear_engulf(df)),
            ("Hammer",               Direction.LONG,  0.68, self._hammer(df)),
            ("Shooting Star",        Direction.SHORT, 0.68, self._shooting_star(df)),
            ("Morning Star",         Direction.LONG,  0.82, self._morning_star(df)),
            ("Evening Star",         Direction.SHORT, 0.82, self._evening_star(df)),
            ("3 White Soldiers",     Direction.LONG,  0.88, self._three_white(df)),
            ("3 Black Crows",        Direction.SHORT, 0.88, self._three_black(df)),
            ("Inside Bar Breakout",  Direction.LONG,  0.72, self._inside_break(df, "long")),
            ("Inside Bar Breakdown", Direction.SHORT, 0.72, self._inside_break(df, "short")),
            ("Bullish Harami",       Direction.LONG,  0.62, self._bull_harami(df)),
            ("Bearish Harami",       Direction.SHORT, 0.62, self._bear_harami(df)),
        ]

        for name, direction, base_str, matched in patterns:
            if not matched: continue
            if direction == Direction.LONG  and rsi > 72: continue
            if direction == Direction.SHORT and rsi < 28: continue
            ctx      = 0.12 if abs(price - vwap)/vwap < 0.004 else 0.0
            rsi_ok   = (direction == Direction.LONG and 40 < rsi < 62) or \
                       (direction == Direction.SHORT and 38 < rsi < 60)
            strength = min(base_str + ctx + (0.08 if rsi_ok else 0.0), 0.97)
            if direction == Direction.LONG:
                sl = df["low"].iloc[-3:].min() * 0.998
                return Signal("price_action", direction, strength, price, sl,
                              price + (price-sl)*1.8, f"{name} | RSI={rsi:.0f}")
            else:
                sl = df["high"].iloc[-3:].max() * 1.002
                return Signal("price_action", direction, strength, price, sl,
                              price - (sl-price)*1.8, f"{name} | RSI={rsi:.0f}")
        return None

    def _bull_engulf(self, df):
        c, p = df.iloc[-1], df.iloc[-2]
        return (p["close"] < p["open"] and c["close"] > c["open"] and
                c["open"] < p["close"] and c["close"] > p["open"] and
                (c["close"]-c["open"]) > (p["open"]-p["close"])*1.1)

    def _bear_engulf(self, df):
        c, p = df.iloc[-1], df.iloc[-2]
        return (p["close"] > p["open"] and c["close"] < c["open"] and
                c["open"] > p["close"] and c["close"] < p["open"] and
                (c["open"]-c["close"]) > (p["close"]-p["open"])*1.1)

    def _hammer(self, df):
        c = df.iloc[-1]
        body = abs(c["close"]-c["open"])
        wick = min(c["open"],c["close"]) - c["low"]
        return body > 0 and wick >= 2*body and (c["high"]-max(c["open"],c["close"])) <= 0.3*body

    def _shooting_star(self, df):
        c = df.iloc[-1]
        body = abs(c["close"]-c["open"])
        wick = c["high"] - max(c["open"],c["close"])
        return body > 0 and wick >= 2*body and (min(c["open"],c["close"])-c["low"]) <= 0.3*body

    def _morning_star(self, df):
        a, b, c = df.iloc[-3], df.iloc[-2], df.iloc[-1]
        return (a["close"] < a["open"] and
                abs(b["close"]-b["open"]) < abs(a["close"]-a["open"])*0.3 and
                c["close"] > c["open"] and c["close"] > (a["open"]+a["close"])/2)

    def _evening_star(self, df):
        a, b, c = df.iloc[-3], df.iloc[-2], df.iloc[-1]
        return (a["close"] > a["open"] and
                abs(b["close"]-b["open"]) < abs(a["close"]-a["open"])*0.3 and
                c["close"] < c["open"] and c["close"] < (a["open"]+a["close"])/2)

    def _three_white(self, df):
        c1, c2, c3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
        return (c1["close"]>c1["open"] and c2["close"]>c2["open"] and c3["close"]>c3["open"] and
                c2["close"]>c1["close"] and c3["close"]>c2["close"])

    def _three_black(self, df):
        c1, c2, c3 = df.iloc[-3], df.iloc[-2], df.iloc[-1]
        return (c1["close"]<c1["open"] and c2["close"]<c2["open"] and c3["close"]<c3["open"] and
                c2["close"]<c1["close"] and c3["close"]<c2["close"])

    def _inside_break(self, df, side):
        c, p, pp = df.iloc[-1], df.iloc[-2], df.iloc[-3]
        if p["high"] < pp["high"] and p["low"] > pp["low"]:
            return (side=="long" and c["close"] > pp["high"]) or \
                   (side=="short" and c["close"] < pp["low"])
        return False

    def _bull_harami(self, df):
        c, p = df.iloc[-1], df.iloc[-2]
        return (p["close"]<p["open"] and c["close"]>c["open"] and
                c["open"]>p["close"] and c["close"]<p["open"])

    def _bear_harami(self, df):
        c, p = df.iloc[-1], df.iloc[-2]
        return (p["close"]>p["open"] and c["close"]<c["open"] and
                c["open"]<p["close"] and c["close"]>p["open"])


# ══════════════════════════════════════════════════════════════════════
# STRATEGY 5 — VWAP MOMENTUM
# ══════════════════════════════════════════════════════════════════════

class VWAPMomentumStrategy:
    def analyze(self, df: pd.DataFrame) -> Optional[Signal]:
        if len(df) < 35: return None
        vwap   = calc_vwap(df)
        rsi    = calc_rsi(df["close"])
        ema200 = calc_ema(df["close"], 200)
        price  = df["close"].iloc[-1]
        pvwap  = vwap.iloc[-2]; cvwap = vwap.iloc[-1]
        pprice = df["close"].iloc[-2]
        crsi   = rsi.iloc[-1]; e200 = ema200.iloc[-1]
        avg_vol = df["volume"].iloc[-21:-1].mean()
        vol_r   = df["volume"].iloc[-1] / avg_vol

        if pprice < pvwap and price > cvwap and price > e200 and 35 < crsi < 62 and vol_r >= 1.15:
            sl = df["low"].iloc[-3:].min() * 0.998
            return Signal("vwap_momentum", Direction.LONG, min(0.50 + (vol_r-1)*0.25, 0.90),
                          price, sl, price + (price-sl)*2.0,
                          f"VWAP Reclaim | RSI={crsi:.0f} | Vol {vol_r:.1f}x")

        if pprice > pvwap and price < cvwap and price < e200 and 38 < crsi < 65 and vol_r >= 1.15:
            sl = df["high"].iloc[-3:].max() * 1.002
            return Signal("vwap_momentum", Direction.SHORT, min(0.50 + (vol_r-1)*0.25, 0.90),
                          price, sl, price - (sl-price)*2.0,
                          f"VWAP Rejection | RSI={crsi:.0f} | Vol {vol_r:.1f}x")
        return None


# ══════════════════════════════════════════════════════════════════════
# SIGNAL AGGREGATOR
# ══════════════════════════════════════════════════════════════════════

class SignalAggregator:
    def __init__(self):
        self.strategies = {
            "ema_cross"     : EMACrossStrategy(),
            "abc_pullback"  : ABCPullbackStrategy(),
            "breakout"      : BreakoutBreakdownStrategy(),
            "price_action"  : PriceActionStrategy(),
            "vwap_momentum" : VWAPMomentumStrategy(),
        }

    def evaluate(self, df: pd.DataFrame, instrument: str) -> Optional[TradeDecision]:
        try:
            adx = calc_adx(df)
        except Exception:
            adx = 25
        if adx < 20:
            logging.info(f"[{instrument}] CHOP (ADX={adx:.1f}) — skip")
            return None

        signals = []
        for name, strat in self.strategies.items():
            try:
                sig = strat.analyze(df)
                if sig:
                    signals.append(sig)
            except Exception as e:
                logging.warning(f"[{instrument}] {name}: {e}")

        if not signals:
            return None

        longs  = [s for s in signals if s.direction == Direction.LONG]
        shorts = [s for s in signals if s.direction == Direction.SHORT]

        if len(longs) >= MIN_STRATEGY_AGREE and len(longs) > len(shorts):
            direction, active = Direction.LONG, longs
        elif len(shorts) >= MIN_STRATEGY_AGREE and len(shorts) > len(longs):
            direction, active = Direction.SHORT, shorts
        else:
            return None

        raw   = sum(STRATEGY_WEIGHTS.get(s.strategy, 0.10) * s.strength for s in active)
        maxp  = sum(STRATEGY_WEIGHTS.get(s.strategy, 0.10) for s in active)
        score = raw / maxp if maxp else 0

        if score < MIN_CONFLUENCE:
            return None

        price = df["close"].iloc[-1]
        if direction == Direction.LONG:
            sl = max(s.sl_price for s in active)
            tg = min(s.target_price for s in active)
            trail = (price - sl) * 0.40
            opt   = "CE"
        else:
            sl = min(s.sl_price for s in active)
            tg = max(s.target_price for s in active)
            trail = (sl - price) * 0.40
            opt   = "PE"

        return TradeDecision(
            instrument=instrument, direction=direction,
            confluence_score=round(score, 3), signals=active,
            entry_price=price, sl_price=sl, target_price=tg,
            trailing_jump=trail, option_type=opt,
            timestamp=now_ist(),
        )


# ══════════════════════════════════════════════════════════════════════
# DHAN API CLIENT
# ══════════════════════════════════════════════════════════════════════

class DhanClient:
    BASE = "https://api.dhan.co"

    def __init__(self, client_id: str, token: str):
        self.client_id = client_id
        self.headers   = {"access-token": token, "Content-Type": "application/json"}

    async def get_candles(self, security_id, exchange, from_date, to_date, interval="5"):
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{self.BASE}/v2/charts/intraday", headers=self.headers,
                json={"securityId": security_id, "exchangeSegment": exchange,
                      "instrument": "INDEX", "interval": interval,
                      "fromDate": from_date, "toDate": to_date})
            return r.json()

    async def get_option_chain(self, underlying, expiry):
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{self.BASE}/v2/optionchain", headers=self.headers,
                json={"UnderlyingScrip": underlying, "ExpiryDate": expiry})
            return r.json()

    async def place_super_order(self, security_id, exchange, quantity, side,
                                entry_price, sl_price, target_price, trailing_jump):
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{self.BASE}/v2/super/orders", headers=self.headers,
                json={
                    "dhanClientId"   : self.client_id,
                    "transactionType": side,
                    "exchangeSegment": exchange,
                    "productType"    : "INTRADAY",
                    "orderType"      : "LIMIT",
                    "validity"       : "DAY",
                    "tradingSymbol"  : "",
                    "securityId"     : security_id,
                    "quantity"       : quantity,
                    "price"          : round(entry_price, 2),
                    "triggerPrice"   : 0,
                    "superOrderLegDetails": [
                        {"orderType": "STOP_LOSS",          "triggerPrice": round(sl_price, 2)},
                        {"orderType": "TARGET",             "price": round(target_price, 2)},
                        {"orderType": "TRAILING_STOP_LOSS", "triggerPrice": round(trailing_jump, 2)},
                    ]
                })
            return r.json()


# ══════════════════════════════════════════════════════════════════════
# DATA FETCHER  — uses IST date for Dhan API
# ══════════════════════════════════════════════════════════════════════

class DataFetcher:
    def __init__(self, dhan: DhanClient):
        self.dhan = dhan

    async def get_ohlcv(self, instrument: str, interval: str = "5") -> pd.DataFrame:
        cfg   = INSTRUMENTS[instrument]
        # Use IST date — critical for VPS in foreign timezone
        today = now_ist().strftime("%Y-%m-%d")
        raw   = await self.dhan.get_candles(
            cfg["security_id"], cfg["exchange"], today, today, interval
        )
        data = raw.get("data", {})
        if not data or not data.get("timestamp"):
            return pd.DataFrame()
        df = pd.DataFrame({
            "timestamp": pd.to_datetime(data["timestamp"], unit="s", utc=True)
                            .dt.tz_convert("Asia/Kolkata"),
            "open"  : data["open"],
            "high"  : data["high"],
            "low"   : data["low"],
            "close" : data["close"],
            "volume": data["volume"],
        })
        return df.sort_values("timestamp").reset_index(drop=True)


# ══════════════════════════════════════════════════════════════════════
# ORDER EXECUTOR
# ══════════════════════════════════════════════════════════════════════

class OrderExecutor:
    def __init__(self, dhan: DhanClient, paper_trade: bool = True):
        self.dhan          = dhan
        self.paper_trade   = paper_trade
        self.paper_log     = []
        self.live_log      = []
        self.daily_trades  = 0
        self.daily_pnl     = 0.0
        self._last_reset   = today_ist()

    def _check_daily_reset(self):
        """Reset daily counters at midnight IST."""
        today = today_ist()
        if today != self._last_reset:
            self.daily_trades = 0
            self.daily_pnl    = 0.0
            self._last_reset  = today
            logging.info("[RESET] Daily trade counter reset (midnight IST)")

    async def execute(self, decision: TradeDecision) -> dict:
        self._check_daily_reset()

        if self.daily_trades >= MAX_DAILY_TRADES:
            return {"status": "skipped", "reason": f"Max daily trades ({MAX_DAILY_TRADES}) reached"}
        if self.daily_pnl <= -(CAPITAL * MAX_DAILY_LOSS):
            return {"status": "skipped", "reason": "Daily loss limit hit"}

        cfg      = INSTRUMENTS[decision.instrument]
        risk_amt = CAPITAL * MAX_RISK_PCT
        risk_pts = abs(decision.entry_price - decision.sl_price)
        qty_lots = max(int(risk_amt / (risk_pts * cfg["lot_size"])), 1)
        quantity = qty_lots * cfg["lot_size"]

        record = {
            "id"              : f"{decision.instrument}_{now_ist().strftime('%H%M%S')}",
            "timestamp_ist"   : now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
            "instrument"      : decision.instrument,
            "option_type"     : decision.option_type,
            "direction"       : decision.direction.value,
            "entry"           : round(decision.entry_price, 2),
            "sl"              : round(decision.sl_price, 2),
            "target"          : round(decision.target_price, 2),
            "trailing_jump"   : round(decision.trailing_jump, 2),
            "confluence_score": decision.confluence_score,
            "signals"         : [f"[{s.strategy}] {s.reason}" for s in decision.signals],
            "lots"            : qty_lots,
            "quantity"        : quantity,
            "mode"            : "PAPER" if self.paper_trade else "LIVE",
        }

        if self.paper_trade:
            self.paper_log.append(record)
            self.daily_trades += 1
            logging.info(
                f"[PAPER] {decision.instrument} {decision.direction.value} "
                f"| Entry={decision.entry_price:.2f} SL={decision.sl_price:.2f} "
                f"T={decision.target_price:.2f} | Score={decision.confluence_score:.2f} "
                f"| IST={now_ist().strftime('%H:%M:%S')}"
            )
            return {"status": "paper_logged", "trade": record}

        # ── LIVE ──────────────────────────────────────────────────────
        expiry_date = self._next_expiry(decision.instrument)
        chain       = await self.dhan.get_option_chain(decision.instrument, expiry_date)
        atm_strike  = round(decision.entry_price / cfg["strike_step"]) * cfg["strike_step"]
        atm_data    = self._find_option(chain, atm_strike, decision.option_type)

        if not atm_data:
            return {"status": "error", "reason": "ATM option not found in chain"}

        prem   = atm_data.get("ltp", 0)
        sl_p   = round(prem * 0.70, 2)
        tgt_p  = round(prem * 1.50, 2)
        trl    = round(prem * 0.08, 2)
        side   = "BUY" if decision.direction == Direction.LONG else "SELL"

        result = await self.dhan.place_super_order(
            security_id=atm_data["securityId"], exchange=cfg["exchange"],
            quantity=quantity, side=side,
            entry_price=prem, sl_price=sl_p,
            target_price=tgt_p, trailing_jump=trl,
        )
        record.update({"premium": prem, "atm_strike": atm_strike, "order_result": result})
        self.live_log.append(record)
        self.daily_trades += 1
        logging.info(f"[LIVE] Super Order → {result}")
        return {"status": "live_order", "trade": record}

    def _next_expiry(self, instrument: str) -> str:
        """Next expiry using IST date."""
        target_day = INSTRUMENTS[instrument]["expiry_day"]
        n = now_ist()
        ahead = (target_day - n.weekday() + 7) % 7 or 7
        return (n + timedelta(days=ahead)).strftime("%Y-%m-%d")

    def _find_option(self, chain, strike, opt_type):
        for item in chain.get("data", {}).get("oc", []):
            if item.get("strikePrice") == strike and item.get("optionType") == opt_type:
                return item
        return None


# ══════════════════════════════════════════════════════════════════════
# FASTAPI APP
# ══════════════════════════════════════════════════════════════════════

app = FastAPI(title="Multi-Strategy Bot", version="2.0")

# Allow dashboard (any origin) to call API — safe because Dhan auth is in backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

dhan     = DhanClient(DHAN_CLIENT_ID, DHAN_ACCESS_TOKEN)
fetcher  = DataFetcher(dhan)
aggreg   = SignalAggregator()
executor = OrderExecutor(dhan, paper_trade=PAPER_TRADE)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

# Scheduler state
_sched_state = {
    "running"       : False,
    "last_scan_ist" : None,
    "next_scan_ist" : None,
    "last_status"   : "not started",
    "scans_done"    : 0,
}


# ── Core scan ─────────────────────────────────────────────────────────

async def run_scan(force: bool = False):
    """
    force=True → skip time/weekend gates (for manual /scan endpoint testing).
    force=False → normal gated scan used by scheduler.
    """
    n   = now_ist()
    ist = n.strftime("%H:%M:%S IST")
    day = n.strftime("%A")

    # ── Weekend gate ──────────────────────────────────────────────────
    if not force and not is_market_day():
        msg = f"Market holiday / weekend ({day}) — no scan"
        _sched_state["last_status"] = msg
        logging.info(f"[SCHED] {msg}")
        return {"status": "market_holiday", "day": day, "ist": ist}

    # ── Market hours gate ─────────────────────────────────────────────
    if not force and not is_market_open():
        msg = f"Market closed at {ist}"
        _sched_state["last_status"] = msg
        logging.info(f"[SCHED] {msg}")
        return {"status": "market_closed", "ist": ist}

    # ── Force exit gate ───────────────────────────────────────────────
    if not force and is_force_exit_time():
        logging.warning(f"[SCHED] FORCE EXIT at {ist} — square off all positions manually")
        _sched_state["last_status"] = f"Force exit time {ist}"
        return {"status": "force_exit", "ist": ist}

    # ── Past entry gate ───────────────────────────────────────────────
    if not force and not is_entry_allowed():
        _sched_state["last_status"] = f"Monitoring only — past 14:00 IST ({ist})"
        return {"status": "past_last_entry", "ist": ist}

    # ── SCAN ──────────────────────────────────────────────────────────
    _sched_state["last_scan_ist"] = ist
    _sched_state["scans_done"] += 1
    results = {}

    for instrument in SCAN_INSTRUMENTS:
        try:
            df = await fetcher.get_ohlcv(instrument)
            if df.empty or len(df) < 30:
                results[instrument] = "insufficient_data"
                continue

            decision = aggreg.evaluate(df, instrument)

            if decision:
                logging.info(
                    f"[SIGNAL ✓] {instrument} {decision.direction.value} "
                    f"Score={decision.confluence_score:.2f} "
                    f"[{', '.join(s.strategy for s in decision.signals)}] @ {ist}"
                )
                result = await executor.execute(decision)
                results[instrument] = result
                _sched_state["last_status"] = f"SIGNAL: {instrument} {decision.direction.value}"
            else:
                results[instrument] = "no_signal"
                _sched_state["last_status"] = f"Scanned {ist} — no signal"

        except Exception as e:
            logging.error(f"[SCAN ERROR] {instrument}: {e}")
            results[instrument] = f"error: {e}"

    return {"ist": ist, "results": results}


# ── Smart Scheduler ──────────────────────────────────────────────────
# Behaviour:
#   • If market is open → scan every SCAN_INTERVAL_SEC seconds
#   • If market is closed / weekend → sleep until next 9:15 IST
#   • This means the bot is SILENT on weekends and nights — no pointless scans

async def scheduler():
    _sched_state["running"] = True
    logging.info(
        f"[SCHED] Bot started | Paper={PAPER_TRADE} | IST={now_ist().strftime('%H:%M:%S')}"
    )

    while True:
        n = now_ist()

        if is_market_open() and is_entry_allowed():
            # Market is open → scan
            await run_scan()
            await asyncio.sleep(SCAN_INTERVAL_SEC)

        elif is_market_open() and not is_entry_allowed():
            # Between 14:00–15:00 → monitor only (no new entries)
            logging.info(f"[SCHED] Monitoring mode (past 14:00) @ {n.strftime('%H:%M:%S')} IST")
            await asyncio.sleep(60)

        elif is_force_exit_time():
            # 15:00+ → wait for midnight
            secs = seconds_until_next_market_open()
            hrs  = secs // 3600
            logging.info(
                f"[SCHED] Market closed. Sleeping {hrs}h until next market open. IST={n.strftime('%H:%M:%S')}"
            )
            _sched_state["last_status"] = f"Market closed. Next open in ~{hrs}h"
            _sched_state["next_scan_ist"] = (n + timedelta(seconds=secs)).strftime("%Y-%m-%d %H:%M IST")
            await asyncio.sleep(min(secs, 3600))  # Wake up hourly to log heartbeat

        elif not is_market_day():
            # Weekend or holiday
            secs = seconds_until_next_market_open()
            hrs  = secs // 3600
            day  = n.strftime("%A")
            logging.info(
                f"[SCHED] {day} — NSE holiday/weekend. Sleeping {hrs}h until next market open."
            )
            _sched_state["last_status"] = f"{day} — market holiday. Next open in ~{hrs}h"
            _sched_state["next_scan_ist"] = (n + timedelta(seconds=secs)).strftime("%Y-%m-%d %H:%M IST")
            await asyncio.sleep(min(secs, 3600))

        else:
            # Before 9:15 — waiting for market open
            secs = seconds_until_next_market_open()
            mins = secs // 60
            logging.info(f"[SCHED] Pre-market. Market opens in {mins}min @ {n.strftime('%H:%M:%S')} IST")
            _sched_state["last_status"] = f"Pre-market. Opens in {mins}min"
            await asyncio.sleep(min(secs, 300))


@app.on_event("startup")
async def startup():
    asyncio.create_task(scheduler())


# ── Endpoints ─────────────────────────────────────────────────────────

@app.get("/")
def health():
    n = now_ist()
    return {
        "bot_version"     : "2.0",
        "server_timezone" : "Asia/Kolkata (IST) — hardcoded, VPS timezone irrelevant",
        "ist_now"         : n.strftime("%Y-%m-%d %H:%M:%S IST"),
        "ist_weekday"     : n.strftime("%A"),
        "is_market_day"   : is_market_day(),
        "is_market_open"  : is_market_open(),
        "is_entry_allowed": is_entry_allowed(),
        "paper_trade"     : PAPER_TRADE,
        "instruments"     : SCAN_INSTRUMENTS,
        "daily_trades"    : executor.daily_trades,
        "daily_pnl"       : executor.daily_pnl,
        "sched_status"    : _sched_state["last_status"],
        "next_scan_ist"   : _sched_state.get("next_scan_ist"),
        "scans_done"      : _sched_state["scans_done"],
    }


@app.get("/status")
def status():
    n = now_ist()
    secs_to_open = seconds_until_next_market_open()
    return {
        "ist"                 : n.strftime("%Y-%m-%d %H:%M:%S"),
        "weekday"             : n.strftime("%A"),
        "is_market_day"       : is_market_day(),
        "is_market_open"      : is_market_open(),
        "is_entry_allowed"    : is_entry_allowed(),
        "is_force_exit_time"  : is_force_exit_time(),
        "seconds_to_next_open": secs_to_open,
        "scheduler_status"    : _sched_state["last_status"],
        "last_scan_ist"       : _sched_state["last_scan_ist"],
        "next_scan_ist"       : _sched_state["next_scan_ist"],
        "total_scans_today"   : _sched_state["scans_done"],
        "paper_trade"         : PAPER_TRADE,
        "daily_trades"        : executor.daily_trades,
    }


@app.get("/scan")
async def manual_scan():
    """Manual scan — bypasses time gate. Use for testing."""
    result = await run_scan(force=True)
    return {"triggered_at_ist": now_ist().strftime("%H:%M:%S IST"), "results": result}


@app.get("/signals/{instrument}")
async def get_signals(instrument: str):
    inst = instrument.upper()
    if inst not in INSTRUMENTS:
        return {"error": f"Unknown. Use: {list(INSTRUMENTS.keys())}"}

    df = await fetcher.get_ohlcv(inst)
    if df.empty:
        return {"error": "No data from Dhan API", "instrument": inst,
                "ist": now_ist().strftime("%H:%M:%S")}

    breakdown = {}
    for name, strat in aggreg.strategies.items():
        try:
            sig = strat.analyze(df)
            breakdown[name] = {
                "signal"  : sig.direction.value if sig else "NEUTRAL",
                "strength": round(sig.strength, 3) if sig else 0,
                "reason"  : sig.reason if sig else "—",
            }
        except Exception as e:
            breakdown[name] = {"error": str(e)}

    decision = aggreg.evaluate(df, inst)
    return {
        "instrument"    : inst,
        "ist"           : now_ist().strftime("%H:%M:%S IST"),
        "is_market_open": is_market_open(),
        "last_price"    : df["close"].iloc[-1],
        "candles_loaded": len(df),
        "strategies"    : breakdown,
        "decision"      : {
            "direction"       : decision.direction.value,
            "confluence_score": decision.confluence_score,
            "entry"           : decision.entry_price,
            "sl"              : decision.sl_price,
            "target"          : decision.target_price,
            "option"          : decision.option_type,
        } if decision else "NO_TRADE",
    }


@app.get("/trades")
def get_trades():
    return {
        "mode"        : "PAPER" if PAPER_TRADE else "LIVE",
        "ist"         : now_ist().strftime("%H:%M:%S IST"),
        "daily_count" : executor.daily_trades,
        "paper_trades": executor.paper_log,
        "live_trades" : executor.live_log,
    }


@app.get("/performance")
def get_performance():
    trades = executor.paper_log if PAPER_TRADE else executor.live_log
    wins   = sum(1 for t in trades if t.get("pnl", 0) > 0)
    total  = len(trades)
    return {
        "total_trades": total,
        "wins"        : wins,
        "losses"      : total - wins,
        "win_rate"    : f"{wins/total*100:.1f}%" if total else "N/A",
        "daily_pnl"   : executor.daily_pnl,
        "ist"         : now_ist().strftime("%H:%M:%S IST"),
    }


if __name__ == "__main__":
    uvicorn.run("multi_strategy_bot:app", host=HOST, port=PORT, log_level="info", reload=False)
