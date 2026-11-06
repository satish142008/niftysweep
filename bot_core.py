"""
bot_core.py  -  NIFTY 50 Liquidity Sweep PAPER-TRADING engine (options CE/PE).
Same logic as nifty_liquidity_sweep_dashboard.py, GUI removed (UI lives in main.py).
No real orders are ever sent.  Standard library only.
Headless test on PC:  python bot_core.py
"""
import csv
import json
import math
import os
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

PRINT_LOG = False                      # True = also print log lines (headless run)

# =============================== CONFIG ====================================
SYMBOLS      = ["NIFTY"]
YF_SYMBOL    = "^NSEI"
YF_VIX       = "^INDIAVIX"
TIMEFRAME    = "5m"                    # PDF examples use 3m / 5m (Yahoo has no 3m)
TIMEFRAMES   = ["1m", "5m", "15m"]
POLL_SEC     = 5
IST          = timezone(timedelta(hours=5, minutes=30))

# --- sessions (IST, minutes since midnight)
ENTRY_START  = 9 * 60 + 25            # no entries before 09:25
ENTRY_END    = 14 * 60 + 45           # no entries after 14:45
SQUAREOFF    = 15 * 60 + 15           # close everything 15:15

# --- option contract
STRIKE_STEP    = 50
STRIKE_OFFSET  = 0                    # 0 = ATM, 1 = one strike ITM, ...
LOT_SIZE       = 65                   # CHECK the current NSE Nifty lot size
EXPIRY_WEEKDAY = 1                    # 0=Mon 1=Tue ... (Nifty weekly expiry day)
RATE           = 0.065
DEFAULT_IV     = 0.14                 # used if India VIX can't be fetched
MIN_PREMIUM    = 5.0                  # skip if option cheaper than Rs 5

# --- liquidity levels
SWING_N        = 4
SWING_LOOKBACK = 250
EQ_TOL         = 0.0006               # 0.06 % ~ 17 pts = "equal"
ROUND_STEP     = 100.0                # round-number levels every 100 pts
ROUND_RANGE    = 0.015
DEDUP_PCT      = 0.0002

# --- sweep / confirmation
MAX_EXC        = 3
MIN_SWEEP_ATR  = 0.05
MAX_SWEEP_ATR  = 3.0
CONFIRM_WINDOW = 3
COOLDOWN       = 24
USE_EMA_FILTER = False                # charts show EMA 50/200 but no stated rule
EMA_LEN        = 50

# --- trade plan (spot terms)
SL_BUF_ATR   = 0.10
MIN_RISK_PCT = 0.0004                 # ~11 pts
MAX_RISK_PCT = 0.0050                 # ~140 pts
MIN_RR       = 1.5
DEFAULT_RR   = 2.0
MAX_RR       = 5.0
BE_AT_R      = 0.0                    # 1.0 = SL to entry after +1R spot move (0=off)
SLIP_PTS     = 1.0                    # spot slippage on SL exits
PREM_SLIP    = 0.003                  # 0.3 % option bid/ask slippage each side

# --- paper account (Rs)
START_BALANCE = 100000.0
RISK_PCT      = 0.01                  # 1 % of equity risked per trade
MAX_CAP_PCT   = 0.60                  # premium paid <= 60 % of equity
MAX_LOTS      = 10
MAX_OPEN      = 1
BROKERAGE     = 20.0                  # per order
CHARGES_PCT   = 0.0005                # STT/exchange/GST approx, each side

TF_MS = {"1m": 60000, "5m": 300000, "15m": 900000}
YF_RANGE = {"1m": "5d", "5m": "5d", "15m": "1mo"}
HOLD = MAX_EXC + 1
TRADE_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "nifty_sweep_trades.csv")


def set_data_dir(d):
    """Android: call with App.user_data_dir so the trades CSV is writable."""
    global TRADE_CSV
    os.makedirs(d, exist_ok=True)
    TRADE_CSV = os.path.join(d, "nifty_sweep_trades.csv")
PRIORITY = {"PDH": 1, "PDL": 1, "EQ_H": 2, "EQ_L": 2, "DAY_H": 3, "DAY_L": 3,
            "SWING_H": 4, "SWING_L": 4, "PDC": 5, "ROUND": 6}


# ============================== DATA LAYER =================================
_CTX = None


def _ssl_ctx():
    global _CTX
    if _CTX is None:
        try:
            import certifi
            _CTX = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _CTX = ssl.create_default_context()
    return _CTX


def http_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
        return json.loads(r.read().decode())


def yahoo(symbol, interval, rng):
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=%s&range=%s"
           % (urllib.parse.quote(symbol), interval, rng))
    res = http_json(url)["chart"]["result"][0]
    return res


def get_candles(interval):
    res = yahoo(YF_SYMBOL, interval, YF_RANGE[interval])
    q = res["indicators"]["quote"][0]
    ms = TF_MS.get(interval, 86400000)
    out = []
    for i, ts in enumerate(res["timestamp"]):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        out.append(dict(t=int(ts) * 1000, o=float(o), h=float(h), l=float(l),
                        c=float(c), ct=int(ts) * 1000 + ms - 1))
    price = res.get("meta", {}).get("regularMarketPrice")
    return out[-300:], (float(price) if price else None)


def get_daily():
    res = yahoo(YF_SYMBOL, "1d", "10d")
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res["timestamp"]):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        out.append(dict(t=int(ts) * 1000, o=float(o), h=float(h), l=float(l),
                        c=float(c), ct=int(ts) * 1000 + 86399999))
    return out


def get_vix():
    res = yahoo(YF_VIX, "1d", "5d")
    v = res.get("meta", {}).get("regularMarketPrice")
    return float(v) / 100.0 if v else None


def ist_now():
    return datetime.now(IST)


# ----------------------- option pricing (Black-Scholes) --------------------
def ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S, K, T, iv, typ, r=RATE):
    T = max(T, 1e-5)
    sd = iv * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / sd
    d2 = d1 - sd
    if typ == "CE":
        return S * ncdf(d1) - K * math.exp(-r * T) * ncdf(d2)
    return K * math.exp(-r * T) * ncdf(-d2) - S * ncdf(-d1)


def next_expiry(now):
    d = now.date()
    for k in range(0, 9):
        day = d + timedelta(days=k)
        if day.weekday() == EXPIRY_WEEKDAY:
            exp = datetime(day.year, day.month, day.day, 15, 30, tzinfo=IST)
            if exp > now:
                return exp
    return now + timedelta(days=7)


def years_to(exp, now):
    return max((exp - now).total_seconds(), 60.0) / (365.0 * 86400.0)


def fmt(x):
    if x is None:
        return "-"
    if abs(x) >= 1000:
        return "{:,.1f}".format(x)
    if abs(x) >= 10:
        return "{:.2f}".format(x)
    return "{:.4f}".format(x)


def calc_atr(cs, n=14):
    if len(cs) < 2:
        return 0.0
    cs = cs[-(n + 1):]
    trs = []
    for a, b in zip(cs[:-1], cs[1:]):
        trs.append(max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"])))
    return sum(trs) / len(trs)


def calc_ema(vals, n):
    if not vals:
        return 0.0
    k = 2.0 / (n + 1)
    e = vals[0]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
    return e


# ============================ LIQUIDITY LEVELS =============================
def _lv(price, kind, bsl, ssl):
    return {"price": price, "kind": kind, "bsl": bsl, "ssl": ssl}


def swing_points(cs, n, lookback):
    N = len(cs)
    hs, ls = [], []
    for i in range(max(n, N - lookback), N - n):
        hi, lo = cs[i]["h"], cs[i]["l"]
        if all(hi >= cs[j]["h"] for j in range(i - n, i)) and \
           all(hi > cs[j]["h"] for j in range(i + 1, i + n + 1)):
            hs.append(i)
        if all(lo <= cs[j]["l"] for j in range(i - n, i)) and \
           all(lo < cs[j]["l"] for j in range(i + 1, i + n + 1)):
            ls.append(i)
    return hs, ls


def build_levels(cs, daily, hold=0):
    """All liquidity levels known at the end of cs[:len-hold]."""
    hist = cs[:len(cs) - hold] if hold else cs
    out = []
    if len(hist) < 30:
        return out
    price = hist[-1]["c"]
    day0 = (hist[-1]["t"] + 19800000) // 86400000 * 86400000 - 19800000
    prev = [d for d in daily if d["t"] < day0]
    if prev:
        d = prev[-1]
        out += [_lv(d["h"], "PDH", True, False), _lv(d["l"], "PDL", False, True),
                _lv(d["c"], "PDC", True, True)]
    today = [c for c in hist if c["t"] >= day0]
    if len(today) >= 3:
        out += [_lv(max(c["h"] for c in today), "DAY_H", True, False),
                _lv(min(c["l"] for c in today), "DAY_L", False, True)]
    hs, ls = swing_points(hist, SWING_N, SWING_LOOKBACK)
    vh = [(i, hist[i]["h"]) for i in hs
          if max([c["h"] for c in hist[i + 1:]] or [0]) <= hist[i]["h"]]
    vl = [(i, hist[i]["l"]) for i in ls
          if min([c["l"] for c in hist[i + 1:]] or [1e18]) >= hist[i]["l"]]
    for i, p in vh:
        eq = any(k != i and abs(i - k) >= 3 and abs(p - q) / p <= EQ_TOL for k, q in vh)
        out.append(_lv(p, "EQ_H" if eq else "SWING_H", True, False))
    for i, p in vl:
        eq = any(k != i and abs(i - k) >= 3 and abs(p - q) / p <= EQ_TOL for k, q in vl)
        out.append(_lv(p, "EQ_L" if eq else "SWING_L", False, True))
    k0 = int(math.floor(price * (1 - ROUND_RANGE) / ROUND_STEP))
    k1 = int(math.ceil(price * (1 + ROUND_RANGE) / ROUND_STEP))
    for k in range(k0, k1 + 1):
        out.append(_lv(k * ROUND_STEP, "ROUND", True, True))
    out.sort(key=lambda x: PRIORITY[x["kind"]])
    kept = []
    for lv in out:
        dup = any(abs(lv["price"] - q["price"]) / lv["price"] < DEDUP_PCT and
                  ((lv["bsl"] and q["bsl"]) or (lv["ssl"] and q["ssl"])) for q in kept)
        if not dup:
            kept.append(lv)
    return kept


# ============================== SWEEP LOGIC ================================
def detect_sweeps(cs, levels, atr):
    """Sweep ending on the LAST candle of cs (candle that closes back inside)."""
    i = len(cs) - 1
    found = []
    if atr <= 0:
        return found
    for lv in levels:
        P = lv["price"]
        if lv["bsl"] and cs[i]["c"] < P:                      # bearish sweep
            m, j = 0, i - 1
            while j >= 0 and m < MAX_EXC and cs[j]["c"] > P:
                m += 1
                j -= 1
            if j >= 0 and cs[j]["c"] <= P:
                ext = max(c["h"] for c in cs[i - m:i + 1])
                depth = ext - P
                if MIN_SWEEP_ATR * atr <= depth <= MAX_SWEEP_ATR * atr:
                    found.append(dict(dir="SHORT", level=lv, ext=ext, depth=depth,
                                      t=cs[i]["t"], trig=cs[i]["l"], age=0, bars=m + 1))
        if lv["ssl"] and cs[i]["c"] > P:                      # bullish sweep
            m, j = 0, i - 1
            while j >= 0 and m < MAX_EXC and cs[j]["c"] < P:
                m += 1
                j -= 1
            if j >= 0 and cs[j]["c"] >= P:
                ext = min(c["l"] for c in cs[i - m:i + 1])
                depth = P - ext
                if MIN_SWEEP_ATR * atr <= depth <= MAX_SWEEP_ATR * atr:
                    found.append(dict(dir="LONG", level=lv, ext=ext, depth=depth,
                                      t=cs[i]["t"], trig=cs[i]["h"], age=0, bars=m + 1))
    return found


# ================================ ENGINE ===================================
class Engine:
    def __init__(self):
        self.lock = threading.RLock()
        self.tf = TIMEFRAME
        self.active = False
        self.start_time = None
        self.balance = START_BALANCE
        self.positions = []
        self.history = []
        self.logs = []
        self.log_n = 0
        self.trade_id = 0
        self.running = True
        self.vix = DEFAULT_IV
        self.vix_ts = 0
        self.daily = {s: [] for s in SYMBOLS}
        self.daily_ts = {s: 0 for s in SYMBOLS}
        self.st = {s: self._new_state(self.tf) for s in SYMBOLS}
        self.threads = []

    @staticmethod
    def _new_state(tf):
        return dict(tf=tf, price=None, candles=[], forming=None, last_proc=None,
                    pending=[], cool={}, levels=[], atr=0.0, err="", upd=0)

    # ---- helpers
    def log(self, msg, tag="info"):
        with self.lock:
            self.logs.append((datetime.now().strftime("%H:%M:%S"), msg, tag))
            self.logs = self.logs[-500:]
            self.log_n += 1
        if PRINT_LOG:
            print(self.logs[-1][0], msg)

    def premium(self, pos, spot, now=None):
        now = now or ist_now()
        return bs_price(spot, pos["strike"], years_to(pos["exp"], now),
                        pos["iv"], pos["typ"]) * (1 - PREM_SLIP)

    def upnl(self, p):
        spot = self.st[p["sym"]]["price"] or p["entry"]
        return (self.premium(p, spot) - p["prem0"]) * p["qty"]

    def equity(self):
        with self.lock:
            return self.balance + sum(self.upnl(p) for p in self.positions)

    def start_threads(self):
        for s in SYMBOLS:
            t = threading.Thread(target=self._loop, args=(s,), daemon=True)
            t.start()
            self.threads.append(t)

    def stop(self):
        self.running = False

    def set_tf(self, tf):
        with self.lock:
            self.tf = tf
            for s in SYMBOLS:
                self.st[s] = self._new_state(tf)
        self.log("Timeframe -> %s (pending sweeps cleared)" % tf)

    def start_bot(self):
        with self.lock:
            self.active = True
            self.start_time = time.time()
            for s in SYMBOLS:                   # nothing seen before Start may trade
                self.st[s]["pending"] = [p for p in self.st[s]["pending"] if p.get("armed")]
        self.log("BOT STARTED - entries only from sweeps formed from now on", "good")

    def stop_bot(self):
        with self.lock:
            self.active = False
        self.log("BOT STOPPED - no new entries (open trades still managed)", "warn")

    # ---- data loop
    def _loop(self, sym):
        while self.running:
            try:
                self.refresh(sym)
                with self.lock:
                    self.st[sym]["err"] = ""
            except Exception as e:
                with self.lock:
                    self.st[sym]["err"] = str(e)[:70]
            time.sleep(POLL_SEC)

    def refresh(self, sym):
        tf = self.tf
        raw, live = get_candles(tf)
        now = int(time.time() * 1000)
        daily = None
        if time.time() - self.daily_ts[sym] > 300:
            try:
                daily = get_daily()
            except Exception:
                daily = None
        if time.time() - self.vix_ts > 60:
            try:
                v = get_vix()
                if v:
                    self.vix = v
            except Exception:
                pass
            self.vix_ts = time.time()
        with self.lock:
            st = self.st[sym]
            if st["tf"] != tf:
                self.st[sym] = st = self._new_state(tf)
            if daily is not None:
                self.daily[sym] = daily
                self.daily_ts[sym] = time.time()
            closed = [c for c in raw if c["ct"] < now]
            forming = [c for c in raw if c["ct"] >= now]
            st["forming"] = forming[-1] if forming else None
            st["price"] = live or (raw[-1]["c"] if raw else st["price"])
            st["candles"] = closed
            st["upd"] = time.time()
            if not closed:
                return
            if st["last_proc"] is None:
                st["last_proc"] = closed[-1]["t"]
            st["atr"] = calc_atr(closed)
            st["levels"] = build_levels(closed, self.daily[sym], 0)
            if st["price"]:
                self.on_price(sym, st["price"])
            for i, c in enumerate(closed):
                if c["t"] > st["last_proc"]:
                    self.process_candle(sym, closed[:i + 1])
                    st["last_proc"] = c["t"]
            n = ist_now()
            if self.positions and n.hour * 60 + n.minute >= SQUAREOFF:
                for pos in list(self.positions):
                    self.close(pos, st["price"] or pos["entry"], "EOD")

    # ---- per-candle logic
    def process_candle(self, sym, cs):
        st = self.st[sym]
        c = cs[-1]
        atr = calc_atr(cs)
        tfms = TF_MS[self.tf]
        for pos in list(self.positions):
            if pos["sym"] == sym and c["t"] > pos["bar_t"]:
                self.check_candle_exit(pos, c)
        keep = []
        for p in st["pending"]:
            p["age"] += 1
            if p["dir"] == "SHORT":
                inval = c["c"] > p["ext"]
                conf = c["c"] < p["trig"] and c["c"] < c["o"]
            else:
                inval = c["c"] < p["ext"]
                conf = c["c"] > p["trig"] and c["c"] > c["o"]
            name = "%s %s sweep %s" % (sym, p["level"]["kind"], p["dir"])
            if inval:
                self.log("%s invalidated (price closed beyond sweep extreme)" % name)
                continue
            if conf:
                if p.get("armed") and self.active:
                    self.try_entry(sym, p, cs, atr)
                else:
                    self.log("%s confirmed but bot was not started -> ignored" % name)
                continue
            if p["age"] >= CONFIRM_WINDOW:
                self.log("%s expired (no confirmation)" % name)
                continue
            keep.append(p)
        st["pending"] = keep

        levels = build_levels(cs, self.daily[sym], HOLD)
        found = detect_sweeps(cs, levels, atr)
        for d in ("SHORT", "LONG"):
            cand = [f for f in found if f["dir"] == d]
            if not cand:
                continue
            cand.sort(key=lambda f: (PRIORITY[f["level"]["kind"]], -f["depth"]))
            f = cand[0]
            key = (f["level"]["kind"], round(f["level"]["price"], 6), d)
            if c["t"] - st["cool"].get(key, 0) < COOLDOWN * tfms:
                continue
            st["cool"][key] = c["t"]
            f["armed"] = self.active
            st["pending"].append(f)
            self.log("%s %s sweep of %s @ %s (%d bar) -> waiting confirmation%s" %
                     (sym, f["level"]["kind"], d, fmt(f["level"]["price"]), f["bars"],
                      "" if self.active else "  [bot not started: watch only]"), "sig")

    def try_entry(self, sym, p, cs, atr):
        c = cs[-1]
        if any(x["sym"] == sym for x in self.positions) or len(self.positions) >= MAX_OPEN:
            self.log("%s signal skipped: a trade is already open" % sym)
            return
        now = ist_now()
        hm = now.hour * 60 + now.minute
        if not (ENTRY_START <= hm <= ENTRY_END):
            self.log("%s signal skipped: outside entry window (%s IST)" % (sym, now.strftime("%H:%M")))
            return
        if USE_EMA_FILTER:
            e = calc_ema([x["c"] for x in cs[-(EMA_LEN * 3):]], EMA_LEN)
            if (p["dir"] == "LONG" and c["c"] < e) or (p["dir"] == "SHORT" and c["c"] > e):
                self.log("%s signal skipped: EMA filter" % sym)
                return
        long_ = p["dir"] == "LONG"
        entry = c["c"]
        sl = p["ext"] - SL_BUF_ATR * atr if long_ else p["ext"] + SL_BUF_ATR * atr
        risk = entry - sl if long_ else sl - entry
        if risk <= 0 or risk / entry < MIN_RISK_PCT or risk / entry > MAX_RISK_PCT:
            self.log("%s signal skipped: stop distance %.0f pts outside limits" % (sym, risk))
            return
        lv_all = build_levels(cs, self.daily[sym], 0)
        tp, tp_name = None, "%.1fR" % DEFAULT_RR
        if long_:
            for l in sorted([l for l in lv_all if l["bsl"] and l["price"] > entry],
                            key=lambda l: l["price"]):
                tgt = l["price"] - 0.05 * atr
                if MIN_RR <= (tgt - entry) / risk <= MAX_RR:
                    tp, tp_name = tgt, l["kind"]
                    break
            if tp is None:
                tp = entry + DEFAULT_RR * risk
        else:
            for l in sorted([l for l in lv_all if l["ssl"] and l["price"] < entry],
                            key=lambda l: -l["price"]):
                tgt = l["price"] + 0.05 * atr
                if MIN_RR <= (entry - tgt) / risk <= MAX_RR:
                    tp, tp_name = tgt, l["kind"]
                    break
            if tp is None:
                tp = entry - DEFAULT_RR * risk
        # ---- option contract: LONG -> CE, SHORT -> PE, nearest strike
        typ = "CE" if long_ else "PE"
        atm = int(entry / STRIKE_STEP + 0.5) * STRIKE_STEP
        strike = atm - STRIKE_OFFSET * STRIKE_STEP if long_ else atm + STRIKE_OFFSET * STRIKE_STEP
        exp = next_expiry(now)
        pos = dict(sym=sym, strike=strike, typ=typ, exp=exp, iv=self.vix)
        prem0 = bs_price(entry, strike, years_to(exp, now), self.vix, typ) * (1 + PREM_SLIP)
        prem_sl = self.premium(pos, sl, now)
        risk_prem = prem0 - prem_sl
        if prem0 < MIN_PREMIUM or risk_prem <= 0:
            self.log("%s signal skipped: option premium %.1f too low" % (sym, prem0))
            return
        eq = self.equity()
        budget = eq * RISK_PCT
        lots = int(budget / (risk_prem * LOT_SIZE))
        if lots < 1:
            if risk_prem * LOT_SIZE > 2 * budget:
                self.log("%s signal skipped: 1 lot risks Rs %.0f (> 2x budget Rs %.0f)" %
                         (sym, risk_prem * LOT_SIZE, budget))
                return
            lots = 1
        used = sum(x["qty"] * x["prem0"] for x in self.positions)
        max_lots = int((eq * MAX_CAP_PCT - used) / (prem0 * LOT_SIZE))
        lots = min(lots, max_lots, MAX_LOTS)
        if lots < 1:
            self.log("%s signal skipped: not enough capital for 1 lot (premium Rs %.1f)" % (sym, prem0))
            return
        qty = lots * LOT_SIZE
        fee = BROKERAGE + prem0 * qty * CHARGES_PCT
        self.balance -= fee
        self.trade_id += 1
        pos.update(id=self.trade_id, side=p["dir"], entry=entry, sl=sl, sl0=sl, tp=tp,
                   qty=qty, lots=lots, risk=risk, risk_amt=risk_prem * qty, prem0=prem0,
                   t_open=time.time(), bar_t=c["t"], fee=fee,
                   setup="%s sweep -> TP %s" % (p["level"]["kind"], tp_name))
        self.positions.append(pos)
        self.log("ENTRY BUY %d %s  (spot %s, %s sweep)  premium %.1f x %d lots  "
                 "SL(spot) %s  TP(spot) %s (%.1fR)  IV %.1f%%" %
                 (strike, typ, fmt(entry), p["level"]["kind"], prem0, lots, fmt(sl), fmt(tp),
                  abs(tp - entry) / risk, self.vix * 100), "good")

    # ---- exits (triggered on SPOT, filled at option model premium)
    def on_price(self, sym, price):
        for pos in list(self.positions):
            if pos["sym"] != sym:
                continue
            long_ = pos["side"] == "LONG"
            if BE_AT_R > 0 and pos["sl"] == pos["sl0"]:
                if (long_ and price >= pos["entry"] + BE_AT_R * pos["risk"]) or \
                   (not long_ and price <= pos["entry"] - BE_AT_R * pos["risk"]):
                    pos["sl"] = pos["entry"]
                    self.log("%s moved to break-even (spot)" % self.name(pos))
            if (long_ and price <= pos["sl"]) or (not long_ and price >= pos["sl"]):
                self.close(pos, pos["sl"] - SLIP_PTS if long_ else pos["sl"] + SLIP_PTS,
                           "BE" if pos["sl"] == pos["entry"] else "SL")
            elif (long_ and price >= pos["tp"]) or (not long_ and price <= pos["tp"]):
                self.close(pos, pos["tp"], "TP")

    def check_candle_exit(self, pos, c):
        long_ = pos["side"] == "LONG"
        hit_sl = c["l"] <= pos["sl"] if long_ else c["h"] >= pos["sl"]
        hit_tp = c["h"] >= pos["tp"] if long_ else c["l"] <= pos["tp"]
        if hit_sl:                                  # both in one candle -> SL first
            self.close(pos, pos["sl"] - SLIP_PTS if long_ else pos["sl"] + SLIP_PTS,
                       "BE" if pos["sl"] == pos["entry"] else "SL")
        elif hit_tp:
            self.close(pos, pos["tp"], "TP")

    @staticmethod
    def name(pos):
        return "NIFTY %d %s" % (pos["strike"], pos["typ"])

    def close(self, pos, spot_exit, reason):
        if pos not in self.positions:
            return
        self.positions.remove(pos)
        prem = self.premium(pos, spot_exit)
        gross = (prem - pos["prem0"]) * pos["qty"]
        fee_out = BROKERAGE + prem * pos["qty"] * CHARGES_PCT
        net = gross - fee_out - pos["fee"]
        self.balance += gross - fee_out
        r = net / pos["risk_amt"] if pos["risk_amt"] else 0.0
        rec = dict(id=pos["id"], time=datetime.now().strftime("%m-%d %H:%M:%S"),
                   contract=self.name(pos), side=pos["side"], spot_in=pos["entry"],
                   spot_out=spot_exit, prem_in=round(pos["prem0"], 2), prem_out=round(prem, 2),
                   lots=pos["lots"], reason=reason, pnl=net, r=r, setup=pos["setup"])
        self.history.append(rec)
        new = not os.path.exists(TRADE_CSV)
        try:
            with open(TRADE_CSV, "a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=list(rec.keys()))
                if new:
                    w.writeheader()
                w.writerow(rec)
        except Exception:
            pass
        self.log("EXIT %s [%s]  premium %.1f -> %.1f  PnL Rs %+.0f (%+.2fR)" %
                 (self.name(pos), reason, pos["prem0"], prem, net, r),
                 "good" if net > 0 else "bad")

    def close_all(self):
        with self.lock:
            for pos in list(self.positions):
                self.close(pos, self.st[pos["sym"]]["price"] or pos["entry"], "MANUAL")

    def reset(self):
        with self.lock:
            self.close_all()
            self.balance = START_BALANCE
            self.history = []
        self.log("Paper account reset to Rs %.0f" % START_BALANCE, "warn")

    def stats(self):
        with self.lock:
            h = self.history
            wins = [x["pnl"] for x in h if x["pnl"] > 0]
            loss = [x["pnl"] for x in h if x["pnl"] <= 0]
            real = sum(x["pnl"] for x in h)
            up = sum(self.upnl(p) for p in self.positions)
            pf = (sum(wins) / -sum(loss)) if loss and sum(loss) < 0 else (float("inf") if wins else 0.0)
            return dict(balance=self.balance, equity=self.balance + up, upnl=up,
                        real=real, n=len(h), wr=100.0 * len(wins) / len(h) if h else 0.0, pf=pf)



if __name__ == "__main__":                     # headless run on PC
    PRINT_LOG = True
    eng = Engine()
    eng.start_threads()
    eng.start_bot()
    try:
        while True:
            time.sleep(30)
            s = eng.stats()
            print("equity %.2f  trades %d  win %.0f%%" % (s["equity"], s["n"], s["wr"]))
    except KeyboardInterrupt:
        pass
