"""
main.py - Android (Kivy) UI for the NIFTY 50 Liquidity Sweep paper-trading bot.

The screen is built ONCE at the desktop size (1380 x 860) and then scaled to fit
the phone, so the layout looks exactly like the PC dashboard (landscape).
Engine / strategy lives in bot_core.py.  Paper trading only.
"""
from kivy.config import Config
from kivy.utils import platform

if platform != "android":                       # PC test window
    Config.set("graphics", "width", "1380")
    Config.set("graphics", "height", "860")

from kivy.app import App
from kivy.clock import Clock
from kivy.core.text import Label as CoreLabel
from kivy.core.window import Window
from kivy.graphics import Color, Line, Rectangle
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.scatter import Scatter
from kivy.uix.scrollview import ScrollView
from kivy.uix.togglebutton import ToggleButton
from kivy.uix.widget import Widget
from kivy.utils import escape_markup, get_color_from_hex as C

import bot_core as bc
from bot_core import CONFIRM_WINDOW, SYMBOLS, TIMEFRAMES, fmt, ist_now, next_expiry, STRIKE_STEP

# ----------------------------- design (desktop) size -------------------------
DW, DH = 1380, 860
FONT = 14
ROW_H = 24

BG, PANEL, FG, DIM = "#0e1117", "#161b22", "#e6edf3", "#8b949e"
GREEN, RED, YEL, BLUE = "#26a69a", "#ef5350", "#f0b429", "#4c9aff"
BTN = "#30363d"
KIND_COL = {"PDH": "#ff9800", "PDL": "#ff9800", "PDC": "#9e9e9e", "EQ_H": "#e040fb",
            "EQ_L": "#e040fb", "DAY_H": "#00e5ff", "DAY_L": "#00e5ff",
            "SWING_H": "#4c9aff", "SWING_L": "#4c9aff", "ROUND": "#5c6370"}
LOG_COL = {"good": GREEN, "bad": RED, "warn": YEL, "sig": BLUE, "info": FG}

RULES = ("1 Liquidity: PDH/PDL/PDC, equal H/L, swings,\n   day H/L, 100-pt round numbers\n"
         "2 Sweep: price trades beyond level, closes back\n"
         "3 Confirm: next candle closes beyond reclaim candle\n"
         "4 LONG signal -> buy CE   SHORT signal -> buy PE\n   strike = nearest 50 (27863 -> 27850 CE)\n"
         "5 SL/TP on spot, TP = next liquidity (>=1.5R) else 2R\n   square-off 15:15 IST")


# ------------------------------- small widgets -------------------------------
class Box(BoxLayout):
    """BoxLayout with a solid background colour."""

    def __init__(self, bg=None, **kw):
        super().__init__(**kw)
        if bg:
            with self.canvas.before:
                Color(*C(bg))
                self._r = Rectangle(pos=self.pos, size=self.size)
            self.bind(pos=self._u, size=self._u)

    def _u(self, *a):
        self._r.pos, self._r.size = self.pos, self.size


def mklabel(text="", size=FONT, color=FG, bold=False, halign="left", **kw):
    l = Label(text=text, font_size=size, color=C(color), bold=bold,
              halign=halign, valign="middle", **kw)
    l.bind(size=lambda w, s: setattr(w, "text_size", s))
    return l


def cell(text, color=FG, w=1.0, bold=False):
    l = Label(text=str(text), color=C(color), font_size=FONT - 1, bold=bold,
              halign="center", valign="middle", shorten=True, shorten_from="right",
              size_hint_x=w)
    l.bind(size=lambda inst, s: setattr(inst, "text_size", (max(s[0] - 6, 1), s[1])))
    return l


def btn(text, bg, w, cb, fg="#ffffff"):
    b = Button(text=text, background_normal="", background_down="", background_color=C(bg),
               color=C(fg), font_size=FONT, bold=True, size_hint=(None, 1), width=w)
    b.bind(on_release=lambda *_: cb())
    return b


class Table(BoxLayout):
    def __init__(self, cols, widths, scroll=False, **kw):
        super().__init__(orientation="vertical", **kw)
        self.widths, self._last = widths, None
        head = Box(bg="#21262d", size_hint_y=None, height=ROW_H)
        grid = GridLayout(cols=len(cols))
        for c, w in zip(cols, widths):
            grid.add_widget(cell(c, FG, w, bold=True))
        head.add_widget(grid)
        self.add_widget(head)
        self.body = GridLayout(cols=len(cols), row_default_height=ROW_H, row_force_default=True)
        if scroll:
            self.body.size_hint_y = None
            self.body.bind(minimum_height=self.body.setter("height"))
            sv = ScrollView(bar_width=6, do_scroll_x=False)
            sv.add_widget(self.body)
            self.add_widget(sv)
        else:
            self.add_widget(self.body)

    def set_rows(self, rows):
        """rows = [(values_tuple, colour_hex), ...]"""
        sig = tuple(rows)
        if sig == self._last:
            return
        self._last = sig
        self.body.clear_widgets()
        for vals, col in rows:
            for v, w in zip(vals, self.widths):
                self.body.add_widget(cell(v, col, w))


# ---------------------------------- chart ------------------------------------
class Chart(Widget):
    def __init__(self, **kw):
        super().__init__(**kw)
        self._tex = {}

    def _tx(self, s, x, y, col, size=11, anchor="w", bold=False):
        key = (s, size, bold)
        tex = self._tex.get(key)
        if tex is None:
            lab = CoreLabel(text=s, font_size=size, bold=bold)
            lab.refresh()
            tex = lab.texture
            if len(self._tex) > 500:
                self._tex.clear()
            self._tex[key] = tex
        w, h = tex.size
        px = x - w / 2.0 if anchor == "c" else x
        Color(*C(col))
        Rectangle(texture=tex, pos=(px, y - h / 2.0), size=(w, h))

    def draw(self, eng, sym):
        c = self.canvas
        c.clear()
        W, H, X0, Y0 = self.width, self.height, self.x, self.y
        with eng.lock:
            st = eng.st[sym]
            cs = list(st["candles"])
            if st["forming"]:
                cs.append(st["forming"])
            levels = list(st["levels"])
            pend = [dict(p) for p in st["pending"]]
            pos = [dict(p) for p in eng.positions if p["sym"] == sym]
            price, atr = st["price"], st["atr"]
        N = 90
        cs = cs[-N:]
        with c:
            Color(*C(PANEL))
            Rectangle(pos=self.pos, size=self.size)
            if W < 100 or len(cs) < 5:
                self._tx("waiting for data...", X0 + W / 2, Y0 + H / 2, DIM, 14, "c")
                return
            lo, hi = min(x["l"] for x in cs), max(x["h"] for x in cs)
            for p in pos:
                lo, hi = min(lo, p["sl"], p["tp"]), max(hi, p["sl"], p["tp"])
            pad = (hi - lo) * 0.06 or 1
            lo, hi = lo - pad, hi + pad
            mr, mt, mb = 95, 24, 20
            pw, ph = W - mr, H - mt - mb
            yy = lambda v: Y0 + mb + (v - lo) / (hi - lo) * ph
            step = pw / len(cs)

            for lv in levels:
                if lo < lv["price"] < hi:
                    col = KIND_COL.get(lv["kind"], DIM)
                    y = yy(lv["price"])
                    Color(*C(col))
                    Line(points=[X0, y, X0 + pw, y], width=1,
                         dash_length=3 if lv["kind"] == "ROUND" else 6,
                         dash_offset=4 if lv["kind"] == "ROUND" else 3)
                    self._tx("%s %s" % (lv["kind"], fmt(lv["price"])), X0 + pw + 4, y, col, 11)

            for i, k in enumerate(cs):
                x = X0 + i * step + step / 2
                col = GREEN if k["c"] >= k["o"] else RED
                Color(*C(col))
                Line(points=[x, yy(k["h"]), x, yy(k["l"])], width=1)
                top, bot = yy(max(k["o"], k["c"])), yy(min(k["o"], k["c"]))
                Rectangle(pos=(x - step * 0.3, bot), size=(step * 0.6, max(top - bot, 1)))

            ts_index = {k["t"]: i for i, k in enumerate(cs)}
            for p in pend:
                if p["t"] in ts_index:
                    x = X0 + ts_index[p["t"]] * step + step / 2
                    y = yy(p["ext"])
                    Color(*C(YEL))
                    Line(circle=(x, y, 7), width=1.5)
                    self._tx("sweep " + p["dir"], x, y + 14 if p["dir"] == "SHORT" else y - 14,
                             YEL, 11, "c")

            for p in pos:
                for v, col, nm in ((p["entry"], FG, "ENTRY"), (p["sl"], RED, "SL"), (p["tp"], GREEN, "TP")):
                    y = yy(v)
                    Color(*C(col))
                    Line(points=[X0, y, X0 + pw, y], width=2)
                    self._tx("%s %s" % (nm, fmt(v)), X0 + pw + 4, y, col, 11, bold=True)

            if price:
                y = yy(price)
                Color(*C("#30363d"))
                Rectangle(pos=(X0 + pw, y - 8), size=(mr, 16))
                self._tx(fmt(price), X0 + pw + 4, y, FG, 12, bold=True)

            nowi = ist_now()
            atm = int((price or 0) / STRIKE_STEP + 0.5) * STRIKE_STEP
            self._tx("NIFTY 50  %s   ATR %s   ATM %d   VIX %.1f   expiry %s   IST %s" %
                     (eng.tf, fmt(atr), atm, eng.vix * 100,
                      next_expiry(nowi).strftime("%d-%b"), nowi.strftime("%H:%M")),
                     X0 + 8, Y0 + H - 12, DIM, 12)


# ----------------------------------- app -------------------------------------
class Desktop(Box):
    """The whole dashboard, laid out for 1380 x 860."""

    def __init__(self, eng, **kw):
        super().__init__(bg=BG, orientation="vertical", padding=8, spacing=6,
                         size_hint=(None, None), size=(DW, DH), **kw)
        self.eng = eng
        self.sym = SYMBOLS[0]
        self.seen = 0
        self.log_buf = []

        # ---- top bar
        top = Box(size_hint_y=None, height=44, spacing=4)
        top.add_widget(btn("START BOT", GREEN, 130, self.on_start))
        top.add_widget(btn("STOP", RED, 90, eng.stop_bot))
        top.add_widget(btn("Close all", BTN, 110, eng.close_all, FG))
        top.add_widget(btn("Reset account", BTN, 140, self.on_reset, FG))
        top.add_widget(mklabel("   Timeframe", FONT, DIM, size_hint=(None, 1), width=100))
        self.tfb = {}
        for tf in TIMEFRAMES:
            b = ToggleButton(text=tf, group="tf", allow_no_selection=False, background_normal="",
                             background_down="", font_size=FONT, color=C(FG),
                             size_hint=(None, 1), width=60,
                             state="down" if tf == eng.tf else "normal")
            b.background_color = C("#30363d" if b.state == "down" else "#21262d")
            b.bind(state=lambda w, v: setattr(w, "background_color",
                                              C("#4c9aff" if v == "down" else "#21262d")))
            b.background_color = C("#4c9aff" if b.state == "down" else "#21262d")
            b.bind(on_release=lambda w: self.on_tf(w.text))
            top.add_widget(b)
            self.tfb[tf] = b
        self.status = mklabel("", 15, YEL, bold=True, halign="right")
        top.add_widget(self.status)
        self.add_widget(top)

        # ---- stats bar
        bar = Box(bg=PANEL, size_hint_y=None, height=56)
        self.lbl = {}
        for k in ("Balance", "Equity", "Unrealized", "Realized", "Trades", "Win %", "Profit factor"):
            f = BoxLayout(orientation="vertical", padding=(14, 4))
            f.add_widget(mklabel(k, 12, DIM))
            self.lbl[k] = mklabel("-", 19, FG, bold=True)
            f.add_widget(self.lbl[k])
            bar.add_widget(f)
        self.add_widget(bar)

        # ---- middle: chart | liquidity map + sweeps + rules
        mid = Box(spacing=8)
        left = BoxLayout(orientation="vertical", spacing=4)
        sel = Box(size_hint_y=None, height=30, spacing=2)
        for s_ in SYMBOLS:
            sel.add_widget(btn(s_, "#30363d", 110, lambda: None, FG))
        sel.add_widget(Widget())
        left.add_widget(sel)
        self.chart = Chart()
        left.add_widget(self.chart)
        mid.add_widget(left)

        right = BoxLayout(orientation="vertical", size_hint_x=None, width=470, spacing=2)
        right.add_widget(mklabel("LIQUIDITY MAP", 12, DIM, bold=True, size_hint_y=None, height=20))
        self.t_lv = Table(("Index", "Spot", "PDH", "PDL", "Next BSL", "Next SSL"), [1] * 6,
                          size_hint_y=None, height=ROW_H * 2)
        right.add_widget(self.t_lv)
        right.add_widget(mklabel("SWEEPS WATCHING", 12, DIM, bold=True, size_hint_y=None, height=28))
        self.t_pen = Table(("Index", "Dir", "Level", "Price", "Age", "Armed"),
                           [1.2, 1, 1.4, 1.5, 0.8, 1], size_hint_y=None, height=ROW_H * 6)
        right.add_widget(self.t_pen)
        right.add_widget(mklabel("RULES (from PDF)", 12, DIM, bold=True, size_hint_y=None, height=28))
        right.add_widget(mklabel(RULES, 13, FG, valign="top"))
        mid.add_widget(right)
        self.add_widget(mid)

        # ---- bottom tabs
        bottom = BoxLayout(orientation="vertical", size_hint_y=None, height=240)
        tabs = Box(size_hint_y=None, height=32, spacing=2)
        self.content = Box(bg=PANEL)
        pc = ("Contract", "Side", "Spot in", "SL (spot)", "TP (spot)", "Lots", "Entry Rs",
              "LTP Rs", "uPnL Rs", "R", "Setup")
        hc = ("Time", "Contract", "Side", "Spot in", "Spot out", "Reason", "PnL Rs", "R", "Setup")
        self.t_pos = Table(pc, [1.5, 0.8, 1, 1, 1, 0.7, 1, 1, 1, 0.8, 3], scroll=True)
        self.t_his = Table(hc, [1.5, 1.7, 0.8, 1, 1, 0.9, 1, 0.8, 3], scroll=True)
        self.log_sv = ScrollView(bar_width=6, do_scroll_x=False)
        self.log_lbl = Label(markup=True, size_hint_y=None, halign="left", valign="top",
                             font_size=FONT - 2, color=C(FG))
        self.log_lbl.bind(width=lambda w, v: setattr(w, "text_size", (max(v - 12, 1), None)),
                          texture_size=lambda w, v: setattr(w, "height", v[1] + 8))
        self.log_sv.add_widget(self.log_lbl)
        self.views = [self.t_pos, self.t_his, self.log_sv]
        self.tab_btns = []
        for i, name in enumerate(("Open positions", "Trade history", "Log")):
            b = ToggleButton(text=name, group="tab", allow_no_selection=False, background_normal="",
                             background_down="", font_size=FONT, color=C(FG),
                             size_hint=(None, 1), width=150, state="down" if i == 0 else "normal")
            b.bind(state=lambda w, v: setattr(w, "background_color",
                                              C("#30363d" if v == "down" else "#21262d")))
            b.background_color = C("#30363d" if i == 0 else "#21262d")
            b.bind(on_release=lambda w, i=i: self.show_tab(i))
            tabs.add_widget(b)
            self.tab_btns.append(b)
        tabs.add_widget(Widget())
        bottom.add_widget(tabs)
        bottom.add_widget(self.content)
        self.add_widget(bottom)
        self.show_tab(0)

    # ---- actions
    def show_tab(self, i):
        self.content.clear_widgets()
        self.content.add_widget(self.views[i])

    def on_tf(self, tf):
        if tf != self.eng.tf:
            self.eng.set_tf(tf)

    def on_start(self):
        self.eng.start_bot()

    def on_reset(self):
        box = BoxLayout(orientation="vertical", padding=12, spacing=12)
        box.add_widget(Label(text="Close all trades and reset paper balance?", font_size="16sp"))
        row = BoxLayout(size_hint_y=None, height="48dp", spacing=12)
        pop = Popup(title="Reset", content=box, size_hint=(0.5, 0.4), auto_dismiss=False)

        def yes(*_):
            pop.dismiss()
            self.eng.reset()
        yb = Button(text="Yes", background_normal="", background_color=C(RED), font_size="16sp")
        nb = Button(text="No", background_normal="", background_color=C(BTN), font_size="16sp")
        yb.bind(on_release=yes)
        nb.bind(on_release=pop.dismiss)
        row.add_widget(yb)
        row.add_widget(nb)
        box.add_widget(row)
        pop.open()

    # ---- refresh (1 Hz)
    def refresh(self):
        e = self.eng
        s = e.stats()
        self.lbl["Balance"].text = "Rs {:,.0f}".format(s["balance"])
        self.lbl["Equity"].text = "Rs {:,.0f}".format(s["equity"])
        self.lbl["Unrealized"].text = "{:+,.0f}".format(s["upnl"])
        self.lbl["Unrealized"].color = C(GREEN if s["upnl"] >= 0 else RED)
        self.lbl["Realized"].text = "{:+,.0f}".format(s["real"])
        self.lbl["Realized"].color = C(GREEN if s["real"] >= 0 else RED)
        self.lbl["Trades"].text = str(s["n"])
        self.lbl["Win %"].text = "%.0f%%" % s["wr"]
        self.lbl["Profit factor"].text = ("-" if s["pf"] == float("inf") and not s["n"] else
                                          ("inf" if s["pf"] == float("inf") else "%.2f" % s["pf"]))
        errs = [x for x in SYMBOLS if e.st[x]["err"]]
        self.status.text = (("BOT RUNNING (paper)" if e.active else "WATCHING ONLY - press START")
                            + ("   data error: " + e.st[errs[0]]["err"] if errs else ""))
        self.status.color = C(GREEN if e.active else YEL)

        with e.lock:
            lv_rows, pen_rows, pos_rows = [], [], []
            for sy in SYMBOLS:
                st = e.st[sy]
                px, lv = st["price"], st["levels"]
                pdh = next((l["price"] for l in lv if l["kind"] == "PDH"), None)
                pdl = next((l["price"] for l in lv if l["kind"] == "PDL"), None)
                bsl = min([l["price"] for l in lv if l["bsl"] and px and l["price"] > px] or [None]) if px else None
                ssl = max([l["price"] for l in lv if l["ssl"] and px and l["price"] < px] or [None]) if px else None
                lv_rows.append(((sy, fmt(px), fmt(pdh), fmt(pdl), fmt(bsl), fmt(ssl)), FG))
                for p in st["pending"][:5]:
                    pen_rows.append(((sy, p["dir"], p["level"]["kind"], fmt(p["level"]["price"]),
                                      "%d/%d" % (p["age"], CONFIRM_WINDOW),
                                      "yes" if p.get("armed") else "watch"), FG))
            for p in e.positions:
                u = e.upnl(p)
                spot = e.st[p["sym"]]["price"] or p["entry"]
                pos_rows.append(((e.name(p), p["side"], fmt(p["entry"]), fmt(p["sl"]), fmt(p["tp"]),
                                  p["lots"], "%.1f" % p["prem0"], "%.1f" % e.premium(p, spot),
                                  "%+.0f" % u, "%+.2f" % (u / p["risk_amt"]), p["setup"]),
                                 GREEN if u >= 0 else RED))
            his = [((h["time"], h["contract"], h["side"], fmt(h["spot_in"]), fmt(h["spot_out"]),
                     h["reason"], "%+.0f" % h["pnl"], "%+.2f" % h["r"], h["setup"]),
                    GREEN if h["pnl"] > 0 else RED)
                   for h in reversed(e.history[-60:])]
            new = e.log_n - self.seen
            logs = e.logs[-new:] if new > 0 else []
            self.seen = e.log_n

        self.t_lv.set_rows(lv_rows)
        self.t_pen.set_rows(pen_rows)
        self.t_pos.set_rows(pos_rows)
        self.t_his.set_rows(his)
        if logs:
            for ts, msg, tag in logs:
                self.log_buf.append("[color=%s]%s  %s[/color]" %
                                    (LOG_COL.get(tag, FG).lstrip("#"), ts, escape_markup(msg)))
            self.log_buf = self.log_buf[-300:]
            self.log_lbl.text = "\n".join(self.log_buf)
            Clock.schedule_once(lambda dt: setattr(self.log_sv, "scroll_y", 0), 0.1)
        self.chart.draw(e, self.sym)


class NiftySweepApp(App):
    title = "NIFTY Liquidity Sweep"

    def build(self):
        bc.set_data_dir(self.user_data_dir)
        self.eng = bc.Engine()
        self.eng.log("Paper trading only - no real orders. Spot: Yahoo ^NSEI. "
                     "Premium = Black-Scholes model (IV = India VIX).")
        self.eng.start_threads()
        Window.clearcolor = C(BG)
        try:
            Window.allow_screensaver = False        # keep screen on while app is open
        except Exception:
            pass

        root = BoxLayout()
        self.desk = Desktop(self.eng)
        self.scatter = Scatter(size_hint=(None, None), size=(DW, DH), do_scale=False,
                               do_rotation=False, do_translation=False)
        self.scatter.add_widget(self.desk)
        root.add_widget(self.scatter)
        Window.bind(size=self.fit)
        Clock.schedule_once(self.fit, 0)
        Clock.schedule_interval(self.tick, 1.0)
        return root

    def fit(self, *a):
        W, H = Window.size
        s = min(W / float(DW), H / float(DH))
        self.scatter.scale = s
        self.scatter.pos = ((W - DW * s) / 2.0, (H - DH * s) / 2.0)

    def tick(self, dt):
        try:
            self.desk.refresh()
        except Exception as ex:
            self.desk.status.text = "UI error: %s" % ex

    def on_pause(self):
        return True

    def on_resume(self):
        pass

    def on_stop(self):
        self.eng.stop()


if __name__ == "__main__":
    NiftySweepApp().run()
