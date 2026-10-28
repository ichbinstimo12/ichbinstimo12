import random
from datetime import datetime, timezone


def make(n, seed=1, regime=False, mu=0.0006, switch=0.01, vol=0.003, start=100.0, spread_pct=0.0003, t0=1_700_000_000, step=3600):
    rnd, px, drift, out = random.Random(seed), start, 0.0, []
    for i in range(n):
        if regime and rnd.random() < switch:
            drift = rnd.choice((-mu, 0.0, mu))
        o = px
        r = drift + rnd.gauss(0, vol)
        c = o * (1 + r)
        hi = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
        lo = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
        out.append({"ts": t0 + i * step, "o": o, "h": hi, "l": lo, "c": c, "spread": c * spread_pct, "v": 1})
        px = c
    return out
