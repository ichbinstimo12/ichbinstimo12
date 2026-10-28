"""Research-Labor: Strategien, kostenrealistischer Backtest, Walk-Forward, Signifikanz, Urteil.

Grundregeln (alle per Test abgesichert):
  * Signal nur aus Kerzen <= i (kein Blick in die Zukunft), Ausfuehrung zur Eroeffnung der Folgekerze.
  * Kosten: voller Spread pro Round-Trip + Slippage (Anteil ATR), mit cost_mult skalierbar.
  * Intrabar-Konflikt (Stop und Ziel in derselben Kerze): Stop zuerst (konservativ). Luecken: Fill zum schlechteren Open.
  * Jede getestete Variante wird gezaehlt; Signifikanz-Huerde steigt mit der Zahl der Versuche (Bonferroni).
Reine Standardbibliothek. Ergebnisse in R-Vielfachen (R = Stop-Abstand = 1 Risikoeinheit).
"""
from __future__ import annotations

import itertools
import math
import random
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import NormalDist

RES_SECONDS = {"MINUTE": 60, "MINUTE_5": 300, "MINUTE_15": 900, "MINUTE_30": 1800,
               "HOUR": 3600, "HOUR_4": 14400, "DAY": 86400, "WEEK": 604800}


def parse_ts(t) -> float | None:
    if isinstance(t, (int, float)):
        return float(t)
    if not t:
        return None
    try:
        dt = datetime.fromisoformat(str(t).replace("Z", "").split("+")[0])
        return dt.replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def normalize(candles: list) -> list:
    """Akzeptiert Format {o,h,l,c,ts,spread} und Desk-Format {open,high,low,close,t,volume}."""
    out = []
    for c in candles:
        if "o" in c:
            out.append(c)
            continue
        ts = parse_ts(c.get("t"))
        if ts is None:
            continue
        out.append({"ts": ts, "o": float(c["open"]), "h": float(c["high"]), "l": float(c["low"]),
                    "c": float(c["close"]), "spread": float(c.get("spread") or 0.0), "v": c.get("volume") or 0})
    return out


# ------------------------------------------------------------------ Indikatoren
class Ind:
    """Kausale Indikator-Arrays (Wert bei Index i nutzt nur Kerzen <= i bzw. < i fuer Kanaele)."""
    def __init__(self, candles):
        self.k = candles
        self.n = len(candles)
        self.o = [c["o"] for c in candles]; self.h = [c["h"] for c in candles]
        self.l = [c["l"] for c in candles]; self.c = [c["c"] for c in candles]
        self._cache = {}

    def _memo(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def ema(self, n):
        def f():
            out, k = [], 2 / (n + 1)
            for i, v in enumerate(self.c):
                out.append(v if i == 0 else out[-1] + k * (v - out[-1]))
            return out
        return self._memo(("ema", n), f)

    def atr(self, n=14):
        def f():
            out = []
            for i in range(self.n):
                pc = self.c[i - 1] if i else self.c[i]
                tr = max(self.h[i] - self.l[i], abs(self.h[i] - pc), abs(self.l[i] - pc))
                out.append(tr if i == 0 else (out[-1] * (n - 1) + tr) / n if i >= n else (out[-1] * i + tr) / (i + 1))
            return out
        return self._memo(("atr", n), f)

    def hh(self, n):  # hoechstes Hoch der VORHERIGEN n Kerzen
        def f():
            return [max(self.h[max(0, i - n):i]) if i > 0 else self.h[0] for i in range(self.n)]
        return self._memo(("hh", n), f)

    def ll(self, n):
        def f():
            return [min(self.l[max(0, i - n):i]) if i > 0 else self.l[0] for i in range(self.n)]
        return self._memo(("ll", n), f)

    def rsi(self, n=2):
        def f():
            out, ag, al = [50.0], 0.0, 0.0
            for i in range(1, self.n):
                d = self.c[i] - self.c[i - 1]
                g, ls = max(d, 0), max(-d, 0)
                if i <= n:
                    ag, al = (ag * (i - 1) + g) / i, (al * (i - 1) + ls) / i
                else:
                    ag, al = (ag * (n - 1) + g) / n, (al * (n - 1) + ls) / n
                out.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
            return out
        return self._memo(("rsi", n), f)

    def ret(self, n):
        return self._memo(("ret", n), lambda: [(self.c[i] / self.c[i - n] - 1) if i >= n else 0.0 for i in range(self.n)])


# ------------------------------------------------------------------- Strategien
@dataclass
class Strategy:
    name: str = "base"
    max_bars: int = 0     # Zeit-Stop (0 = aus)

    def warmup(self) -> int: return 60
    def entry(self, ind: Ind, i: int):          # -> (side, stop_dist, tp_dist|None) | None
        raise NotImplementedError
    def exit(self, ind: Ind, i: int, side: str) -> bool: return False
    def params(self) -> dict: return {}
    def prepare(self, ind: Ind, train_end: int) -> None:
        """Lernende Strategien passen sich hier an; nur Kerzen < train_end duerfen verwendet werden."""
        return None


@dataclass
class EmaState(Strategy):
    """Dein bisheriges Desk-Verfahren (Baseline): Zustand EMA12/EMA48, Luecke >= 0.25 ATR, Stop 1.5 ATR, Ziel 3 ATR."""
    name: str = "ema_state"
    fast: int = 12; slow: int = 48; min_strength: float = 0.25; stop_atr: float = 1.5; tp_atr: float = 3.0

    def warmup(self): return self.slow + 20
    def _state(self, ind, i):
        a = ind.atr(14)[i]
        gap = ind.ema(self.fast)[i] - ind.ema(self.slow)[i]
        if a <= 0 or abs(gap) < self.min_strength * a:
            return None
        if gap > 0 and ind.c[i] > ind.ema(self.slow)[i]: return "BUY"
        if gap < 0 and ind.c[i] < ind.ema(self.slow)[i]: return "SELL"
        return None
    def entry(self, ind, i):
        s = self._state(ind, i)
        if not s: return None
        a = ind.atr(14)[i]
        return s, self.stop_atr * a, self.tp_atr * a
    def exit(self, ind, i, side):
        s = self._state(ind, i)
        return s is not None and s != side
    def params(self): return dict(fast=self.fast, slow=self.slow, stop_atr=self.stop_atr, tp_atr=self.tp_atr)


@dataclass
class DonchianTrend(Strategy):
    """Ausbruch ueber das N-Kerzen-Hoch/-Tief in Richtung des langen Trends. Kein festes Ziel (Gewinner laufen lassen),
    Ausstieg ueber den kuerzeren Gegenkanal, Stop weit (ATR-Vielfaches). Edge-These: Trendpersistenz in Krisen/Trendphasen."""
    name: str = "donchian_trend"
    entry_n: int = 48; exit_n: int = 24; trend_ema: int = 200; stop_atr: float = 3.0
    vol_min_pct: float = 0.0      # Volatilitaetsfilter: ATR/Preis muss >= Wert sein (z. B. 0.001)

    def warmup(self): return max(self.entry_n, self.trend_ema) + 20
    def entry(self, ind, i):
        a, c = ind.atr(14)[i], ind.c[i]
        if a <= 0 or a / c < self.vol_min_pct: return None
        if c > ind.hh(self.entry_n)[i] and c > ind.ema(self.trend_ema)[i]:
            return "BUY", self.stop_atr * a, None
        if c < ind.ll(self.entry_n)[i] and c < ind.ema(self.trend_ema)[i]:
            return "SELL", self.stop_atr * a, None
        return None
    def exit(self, ind, i, side):
        c = ind.c[i]
        return c < ind.ll(self.exit_n)[i] if side == "BUY" else c > ind.hh(self.exit_n)[i]
    def params(self): return dict(entry_n=self.entry_n, exit_n=self.exit_n, trend_ema=self.trend_ema, stop_atr=self.stop_atr)


@dataclass
class TsMomentum(Strategy):
    """Zeitreihen-Momentum: Vorzeichen der Rendite ueber L Kerzen, nur wenn der Betrag > k x Rausch-Niveau liegt."""
    name: str = "ts_momentum"
    lookback: int = 72; k: float = 1.0; stop_atr: float = 3.0

    def warmup(self): return self.lookback + 30
    def _dir(self, ind, i):
        r, a = ind.ret(self.lookback)[i], ind.atr(14)[i] / ind.c[i]
        thr = self.k * a * math.sqrt(self.lookback)
        return "BUY" if r > thr else "SELL" if r < -thr else None
    def entry(self, ind, i):
        d = self._dir(ind, i)
        return (d, self.stop_atr * ind.atr(14)[i], None) if d else None
    def exit(self, ind, i, side):
        d = self._dir(ind, i)
        return d is not None and d != side
    def params(self): return dict(lookback=self.lookback, k=self.k, stop_atr=self.stop_atr)


@dataclass
class RsiReversion(Strategy):
    """Kurzfrist-Rueckkehr im Trend-Rahmen: RSI(2) extrem + Preis auf der 'richtigen' Seite der langen EMA."""
    name: str = "rsi_reversion"
    rsi_n: int = 2; lo: float = 10.0; hi: float = 90.0; trend_ema: int = 200; exit_ema: int = 5
    stop_atr: float = 3.0; max_bars: int = 24

    def warmup(self): return self.trend_ema + 20
    def entry(self, ind, i):
        r, c, a = ind.rsi(self.rsi_n)[i], ind.c[i], ind.atr(14)[i]
        if a <= 0: return None
        if r < self.lo and c > ind.ema(self.trend_ema)[i]: return "BUY", self.stop_atr * a, None
        if r > self.hi and c < ind.ema(self.trend_ema)[i]: return "SELL", self.stop_atr * a, None
        return None
    def exit(self, ind, i, side):
        e = ind.ema(self.exit_ema)[i]
        return ind.c[i] > e if side == "BUY" else ind.c[i] < e
    def params(self): return dict(rsi_n=self.rsi_n, lo=self.lo, hi=self.hi, stop_atr=self.stop_atr)


# ----------------------------------------------------------- lernendes Modell
def _features(ind: Ind):
    """Merkmalsmatrix (eine Zeile je Kerze), ausschliesslich aus Kerzen <= i. Rueckgabe: Liste von Listen."""
    def f():
        a, c = ind.atr(14), ind.c
        e50, e200, r2, r14 = ind.ema(50), ind.ema(200), ind.rsi(2), ind.rsi(14)
        hh, ll = ind.hh(48), ind.ll(48)
        rows = []
        vol = [a[i] / c[i] if c[i] else 0.0 for i in range(ind.n)]
        for i in range(ind.n):
            ap = vol[i] or 1e-9
            row = []
            for n in (1, 4, 12, 24, 72):
                r = (c[i] / c[i - n] - 1) if i >= n and c[i - n] else 0.0
                row.append(r / (ap * math.sqrt(n)))
            row.append((c[i] - e50[i]) / (a[i] or 1e-9))
            row.append((c[i] - e200[i]) / (a[i] or 1e-9))
            row.append(r2[i] / 100 - 0.5)
            row.append(r14[i] / 100 - 0.5)
            lo = max(0, i - 100)
            mv = sum(vol[lo:i + 1]) / (i + 1 - lo)
            row.append(vol[i] / mv - 1 if mv else 0.0)
            rng = hh[i] - ll[i]
            row.append(((c[i] - ll[i]) / rng - 0.5) if rng > 0 else 0.0)
            ts = ind.k[i].get("ts")
            if ts:
                h = (ts % 86400) / 3600.0
                row += [math.sin(2 * math.pi * h / 24), math.cos(2 * math.pi * h / 24)]
            else:
                row += [0.0, 0.0]
            rows.append([max(-10.0, min(10.0, v)) for v in row])
        return rows
    return ind._memo(("features",), f)


def _barrier_labels(ind: Ind, horizon: int, k: float):
    """Triple-Barrier: 1 = +k ATR zuerst, 0 = -k ATR zuerst, None = keine Barriere in `horizon` Kerzen (oder gleiche Kerze)."""
    def f():
        a, out = ind.atr(14), []
        for i in range(ind.n):
            up, dn, lab_ = ind.c[i] + k * a[i], ind.c[i] - k * a[i], None
            for j in range(i + 1, min(ind.n, i + 1 + horizon)):
                hu, hd = ind.h[j] >= up, ind.l[j] <= dn
                if hu and hd:
                    break
                if hu:
                    lab_ = 1; break
                if hd:
                    lab_ = 0; break
            out.append(lab_)
        return out
    return ind._memo(("labels", horizon, k), f)


def fit_logistic(X, y, l2=1.0, iters=60):
    """L2-regularisierte logistische Regression (Newton/IRLS, numpy). Rueckgabe (w, b, mu, sd) oder None."""
    import numpy as np
    if len(y) < 200 or len(set(y)) < 2:
        return None
    X = np.asarray(X, float); y = np.asarray(y, float)
    mu, sd = X.mean(0), X.std(0)
    sd[sd < 1e-9] = 1.0
    Z = np.hstack([(X - mu) / sd, np.ones((len(X), 1))])
    w = np.zeros(Z.shape[1])
    R = np.eye(Z.shape[1]) * l2
    R[-1, -1] = 0.0
    for _ in range(iters):
        p = 1 / (1 + np.exp(-np.clip(Z @ w, -30, 30)))
        g = Z.T @ (p - y) + R @ w
        H = (Z * (p * (1 - p))[:, None]).T @ Z + R
        try:
            step = np.linalg.solve(H, g)
        except np.linalg.LinAlgError:
            return None
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return [float(v) for v in w[:-1]], float(w[-1]), [float(v) for v in mu], [float(v) for v in sd]


@dataclass
class MLLogit(Strategy):
    """Lernendes Modell: logistische Regression auf kausalen Merkmalen (Momentum, Trendabstand, RSI, Volatilitaetsregime,
    Kanalposition, Tageszeit) sagt voraus, ob +k ATR vor -k ATR erreicht wird (Triple-Barrier). Gehandelt wird nur, wenn
    die Wahrscheinlichkeit die Schwelle uebersteigt; Stop = Ziel = k ATR, Zeit-Stop = Horizont.
    Training nur auf Kerzen vor dem Testabschnitt (Labels muessen vollstaendig VOR train_end liegen)."""
    name: str = "ml_logit"
    horizon: int = 24; k: float = 2.0; thr: float = 0.58; l2: float = 3.0
    weights: list = None; bias: float = 0.0; mu: list = None; sd: list = None

    def __post_init__(self):
        self.max_bars = self.horizon

    def warmup(self): return 220
    def prepare(self, ind, train_end):
        X, Y = [], []
        feats, labels = _features(ind), _barrier_labels(ind, self.horizon, self.k)
        for i in range(self.warmup(), max(self.warmup(), train_end - self.horizon - 1)):
            if labels[i] is not None:
                X.append(feats[i]); Y.append(labels[i])
        fit = fit_logistic(X, Y, l2=self.l2)
        if fit:
            self.weights, self.bias, self.mu, self.sd = fit
        else:
            self.weights = None
    def prob(self, ind, i):
        if not self.weights:
            return None
        x = _features(ind)[i]
        z = self.bias + sum(w * (v - m) / s for w, v, m, s in zip(self.weights, x, self.mu, self.sd))
        return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))
    def entry(self, ind, i):
        a = ind.atr(14)[i]
        p = self.prob(ind, i)
        if p is None or a <= 0:
            return None
        if p >= self.thr: return "BUY", self.k * a, self.k * a
        if p <= 1 - self.thr: return "SELL", self.k * a, self.k * a
        return None
    def params(self): return dict(horizon=self.horizon, k=self.k, thr=self.thr)
    def export(self):
        """Parameter inkl. gelernter Gewichte (fuer den Live-Desk: gleiche Entscheidungen wie im Labor)."""
        return dict(self.params(), weights=self.weights, bias=self.bias, mu=self.mu, sd=self.sd)


REGISTRY = {"ema_state": EmaState, "donchian_trend": DonchianTrend, "ts_momentum": TsMomentum, "rsi_reversion": RsiReversion,
            "ml_logit": MLLogit}
GRIDS = {
    "ema_state": {"stop_atr": [1.5, 2.5], "tp_atr": [3.0, 5.0]},
    "donchian_trend": {"entry_n": [24, 48, 96], "exit_n": [12, 24], "stop_atr": [2.5, 3.5]},
    "ts_momentum": {"lookback": [48, 96], "k": [0.5, 1.0], "stop_atr": [3.0]},
    "rsi_reversion": {"lo": [5.0, 10.0], "stop_atr": [2.5, 3.5]},
    "ml_logit": {"horizon": [12, 24], "k": [1.5, 2.5], "thr": [0.56, 0.60]},
}


def grid_points(name):
    g = GRIDS[name]
    keys = list(g)
    return [dict(zip(keys, v)) for v in itertools.product(*(g[k] for k in keys))]


# ------------------------------------------------------------------- Backtest
def backtest(candles, strat: Strategy, cost_mult=1.0, slip_atr=0.03, spread_default_pct=0.0004,
             overnight_r_per_bar=0.0, first=None, overnight_pct_day=0.0, bar_seconds=3600):
    """Alle Kosten stecken in den Fill-Preisen. `first`: ab diesem Index duerfen Einstiege erfolgen (Warm-up davor).
    Lernende Strategien werden vorher nur auf Kerzen < first trainiert (ohne `first`: Training auf den ersten 60 %,
    gehandelt wird erst danach). `overnight_pct_day`: Finanzierungskosten je gehaltenem Tag in Anteil des Preises."""
    ind = Ind(candles)
    if first is None and type(strat).prepare is not Strategy.prepare and not getattr(strat, "weights", None):
        first = int(ind.n * 0.6)
    if first is not None and type(strat).prepare is not Strategy.prepare and not getattr(strat, "weights", None):
        strat.prepare(ind, first)
    n, warm = ind.n, max(strat.warmup(), first or 0)
    fin = {"pct_day": overnight_pct_day * cost_mult, "bar_s": bar_seconds}
    trades, pos = [], None
    atr = ind.atr(14)

    def half_spread(j):
        sp = candles[j].get("spread") or candles[j]["o"] * spread_default_pct
        return 0.5 * sp * cost_mult

    for i in range(warm, n - 1):
        nxt = candles[i + 1]
        if pos:
            c = candles[i]
            d = 1 if pos["side"] == "BUY" else -1
            hit_stop = (c["l"] <= pos["stop"]) if d == 1 else (c["h"] >= pos["stop"])
            hit_tp = pos["tp"] is not None and ((c["h"] >= pos["tp"]) if d == 1 else (c["l"] <= pos["tp"]))
            if i >= pos["i"] and (hit_stop or hit_tp):
                if hit_stop:  # Stop zuerst (konservativ); Luecke -> schlechterer Open
                    px = pos["stop"]
                    px = min(px, c["o"]) if d == 1 else max(px, c["o"])
                    why = "stop"
                else:
                    px, why = pos["tp"], "target"
                px -= d * (half_spread(i) + slip_atr * pos["atr"] * cost_mult * (1 if why == "stop" else 0))
                trades.append(_close(pos, px, i, why, overnight_r_per_bar, fin))
                pos = None
            else:
                age = i - pos["i"]
                if (strat.max_bars and age >= strat.max_bars) or strat.exit(ind, i, pos["side"]):
                    px = nxt["o"] - d * (half_spread(i + 1) + slip_atr * pos["atr"] * cost_mult)
                    trades.append(_close(pos, px, i + 1, "signal", overnight_r_per_bar, fin))
                    pos = None
                    continue
        if pos is None:
            sig = strat.entry(ind, i)
            if sig:
                side, sd, tp = sig
                if sd and sd > 0:
                    d = 1 if side == "BUY" else -1
                    entry = nxt["o"] + d * (half_spread(i + 1) + slip_atr * atr[i] * cost_mult)
                    pos = {"side": side, "entry": entry, "stop": entry - d * sd, "dist": sd, "atr": atr[i], "i": i + 1,
                           "tp": None if tp is None else entry + d * tp, "ts": nxt["ts"]}
    return trades


def _close(pos, px, i, why, ovn, fin=None):
    d = 1 if pos["side"] == "BUY" else -1
    bars = max(0, i - pos["i"])
    r = d * (px - pos["entry"]) / pos["dist"] - ovn * bars
    if fin and fin["pct_day"] > 0 and bars:
        nights = int(bars * fin["bar_s"] // 86400)  # ANNAHME: je volle 24 h Haltedauer eine Finanzierung
        r -= nights * fin["pct_day"] * pos["entry"] / pos["dist"]
    return {"r": r, "side": pos["side"], "bars": bars, "why": why, "ts": pos["ts"], "entry_i": pos["i"], "exit_i": i}


def metrics(trades, risk_pct=0.005, span_days=None):
    n = len(trades)
    if not n:
        return {"trades": 0, "win_rate": None, "avg_win_r": None, "avg_loss_r": None, "payoff": None, "profit_factor": None,
                "expectancy_r": 0.0, "t_stat": None, "max_dd_r": 0.0, "max_dd_pct": 0.0, "total_r": 0.0}
    rs = [t["r"] for t in trades]
    w, l = [r for r in rs if r > 0], [r for r in rs if r <= 0]
    eq = peak = dd = 0.0
    for r in rs:
        eq += r; peak = max(peak, eq); dd = max(dd, peak - eq)
    sd = statistics.pstdev(rs) if n > 1 else 0.0
    gl = -sum(l)
    return {"trades": n, "win_rate": len(w) / n, "avg_win_r": (sum(w) / len(w)) if w else 0.0,
            "avg_loss_r": (sum(l) / len(l)) if l else 0.0, "payoff": ((sum(w) / len(w)) / (-sum(l) / len(l))) if w and l and sum(l) else None,
            "profit_factor": (sum(w) / gl) if gl > 0 else None, "expectancy_r": sum(rs) / n,
            "t_stat": (sum(rs) / n) / (sd / math.sqrt(n)) if sd > 0 else None, "max_dd_r": dd,
            "max_dd_pct": dd * risk_pct * 100, "total_r": sum(rs)}


def bootstrap_ci(rs, runs=2000, seed=7, alpha=0.05):
    if len(rs) < 5:
        return (None, None)
    rnd, m = random.Random(seed), []
    for _ in range(runs):
        m.append(sum(rnd.choice(rs) for _ in rs) / len(rs))
    m.sort()
    return (m[int(alpha / 2 * runs)], m[int((1 - alpha / 2) * runs) - 1])


def required_t(trials: int, alpha=0.05) -> float:
    """Einseitige Huerde mit Bonferroni-Korrektur: je mehr Varianten probiert, desto hoeher die Latte."""
    return NormalDist().inv_cdf(1 - alpha / max(1, trials))


# ----------------------------------------------------------------- Walk-Forward
def walk_forward(candles, name, n_folds=5, min_train_trades=12, cost_mult=1.0, **bt):
    """Expandierendes Training; Parameter je Fold NUR auf Trainingsdaten gewaehlt; Auswertung nur auf dem Folgeabschnitt."""
    pts, n = grid_points(name), len(candles)
    chunk = n // (n_folds + 1)
    oos, folds, trials = [], [], 0
    for k in range(1, n_folds + 1):
        tr_end, te_end = chunk * k, chunk * (k + 1) if k < n_folds else n
        best, best_e = None, -1e9
        for p in pts:
            trials += 1
            s = REGISTRY[name](**p)
            m = metrics(backtest(candles[:tr_end], s, cost_mult, **bt))
            if m["trades"] >= min_train_trades and m["expectancy_r"] > best_e:
                best, best_e = p, m["expectancy_r"]
        if best is None:
            folds.append({"fold": k, "params": None, "trades": 0}); continue
        t = backtest(candles[:te_end], REGISTRY[name](**best), cost_mult, first=tr_end, **bt)
        oos += t
        folds.append({"fold": k, "params": best, "train_expectancy_r": best_e, "trades": len(t),
                      "oos_expectancy_r": metrics(t)["expectancy_r"]})
    return oos, folds, trials


def research(epic, candles, names=None, trial_counter_in=0, min_oos_trades=40, **bt):
    """Vollstaendige Pruefung aller Kandidaten fuer einen Markt. Rueckgabe: Liste Berichte (+ neue Gesamtzahl Versuche)."""
    candles = normalize(candles)
    names = names or list(REGISTRY)
    reports, total_trials = [], trial_counter_in
    for nm in names:
        rep = {"epic": epic, "strategy": nm, "bars": len(candles)}
        if len(candles) < 1500:
            rep.update(verdict="INSUFFICIENT_DATA", reasons=[f"nur {len(candles)} Kerzen (mind. 1500 noetig)"], trials=0)
            reports.append(rep); continue
        oos, folds, trials = walk_forward(candles, nm, cost_mult=1.0, **bt)
        oos2, _, trials2 = walk_forward(candles, nm, cost_mult=2.0, **bt)
        total_trials += trials + trials2
        m, m2 = metrics(oos), metrics(oos2)
        lo, hi = bootstrap_ci([t["r"] for t in oos])
        full_pos = sum(1 for p in grid_points(nm) if metrics(backtest(candles, REGISTRY[nm](**p), **bt))["expectancy_r"] > 0)
        total_trials += len(grid_points(nm))
        nb = full_pos / len(grid_points(nm))
        need = required_t(total_trials)
        reasons = []
        if m["trades"] < min_oos_trades: reasons.append(f"nur {m['trades']} Out-of-Sample-Trades (<{min_oos_trades}): statistisch zu duenn")
        if m["expectancy_r"] < 0.10: reasons.append(f"Erwartungswert {m['expectancy_r']:.2f} R < 0.10 R nach Kosten")
        if m2["expectancy_r"] <= 0: reasons.append("bei doppelten Kosten nicht mehr profitabel")
        if nb < 0.5: reasons.append(f"nur {nb:.0%} der Nachbar-Parameter profitabel (Overfitting-Verdacht)")
        if m["max_dd_pct"] > 8.0: reasons.append(f"OOS-Drawdown {m['max_dd_pct']:.1f}% > 8%-Kontolimit")
        sig_ok = (lo is not None and lo > 0) and (m["t_stat"] or 0) >= need
        hard_fail = [r for r in reasons if "Erwartungswert" in r or "doppelten" in r or "Nachbar" in r or "Drawdown" in r]
        if not reasons and sig_ok:
            verdict = "PASS"
        elif not hard_fail and m["trades"] >= 15:
            verdict = "FORWARD_TEST"   # vielversprechend, aber (noch) nicht bewiesen -> nur Demo, klein
        else:
            verdict = "FAIL" if m["trades"] >= 15 else "INSUFFICIENT_DATA"
        if not sig_ok and verdict == "PASS":
            verdict = "FORWARD_TEST"
        rep.update(verdict=verdict, reasons=reasons, oos=m, oos_cost2=m2, bootstrap_ci=[lo, hi], neighbours_positive=nb,
                   required_t=need, sig_ok=sig_ok, folds=folds, trials=trials + trials2 + len(grid_points(nm)),
                   trials_total=total_trials)
        reports.append(rep)
    return reports, total_trials
