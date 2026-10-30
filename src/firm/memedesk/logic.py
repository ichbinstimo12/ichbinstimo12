"""Entscheidungslogik des Memecoin-Desks: reine Funktionen (deterministisch, ohne Netz, vollstaendig getestet).

Safety-Checker und Risk-Manager haben Veto-Recht: ein einziger Grund reicht, dann wird nicht gekauft.
Einstieg nur bei mindestens `min_signals` von 5 Signalen (Orangie-Stil): Narrativ, Volumen+Kaeufer, Smart Money,
Holder-Verteilung, Chartstruktur - und nie einem senkrechten Pump hinterher.
"""
from __future__ import annotations

import math
import re
import time
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DATA_MISSING = "Daten fehlen: "   # veto because a source was unreachable (retry soon), not because the token is bad
SIGNALS = ("narrative", "volume_holders", "smart_money", "distribution", "structure")


# ------------------------------------------------------------------ config
def load_config(path: str | Path = "config/memedesk.toml", override: Optional[Path] = None) -> Dict[str, Any]:
    """Konfiguration + vom Coach freigegebene, versionierte Parameter (nur [weights]/[thresholds], in [bounds])."""
    cfg = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    if override and Path(override).exists():
        import json
        params = json.loads(Path(override).read_text(encoding="utf-8"))
        for section in ("weights", "thresholds"):
            for k, v in (params.get(section) or {}).items():
                lo, hi = (cfg.get("bounds") or {}).get(k, (None, None))
                if k in cfg.get(section, {}) and lo is not None and lo <= float(v) <= hi:
                    cfg[section][k] = float(v)
    return cfg


def load_limits(path: str | Path = "config/memedesk_limits.toml") -> Dict[str, Any]:
    lim = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    rpt = float(lim["trade"]["risk_per_trade"])
    if not 0.0 < rpt <= 0.02:        # Spezifikation: max. 1-2 % pro Trade - mehr wird nicht akzeptiert
        raise ValueError(f"memedesk risk_per_trade {rpt} outside 0..2 %")
    return lim


# ------------------------------------------------------------------ safety (veto)
def safety_veto(report: Optional[Dict[str, Any]], roundtrip_loss_pct: Optional[float], pair: Dict[str, Any],
                cfg: Dict[str, Any]) -> List[str]:
    """Leere Liste = gruenes Licht. Fehlende Daten = Veto (lieber keinen Trade als einen blinden)."""
    s, reasons = cfg["safety"], []
    if report is None:
        return [DATA_MISSING + "kein Sicherheitsbericht (RugCheck nicht erreichbar)"]
    if report.get("rugged"):
        reasons.append("als Rug markiert")
    if report.get("mint_authority"):
        reasons.append("Mint-Authority aktiv (unbegrenzt neue Tokens moeglich)")
    if report.get("freeze_authority"):
        reasons.append("Freeze-Authority aktiv (Konten koennen eingefroren werden)")
    if float(report.get("lp_locked_pct") or 0) < s["min_lp_locked_pct"]:
        reasons.append(f"LP nur {float(report.get('lp_locked_pct') or 0):.0f} % gelockt/verbrannt")
    if float(report.get("top_holder_pct") or 0) > s["max_top_holder_pct"]:
        reasons.append(f"Einzel-Holder {report['top_holder_pct']:.1f} % > {s['max_top_holder_pct']} %")
    if float(report.get("top10_pct") or 0) > s["max_top10_pct"]:
        reasons.append(f"Top-10-Holder {report['top10_pct']:.1f} % > {s['max_top10_pct']} %")
    if int(report.get("graph_insiders") or 0) > 0 or int(report.get("insiders_flagged") or 0) >= 3:
        reasons.append("Insider-/Bundle-Cluster erkannt")
    if int(report.get("score") or 0) > s["max_rugcheck_score"]:
        reasons.append(f"RugCheck-Score {report['score']} > {s['max_rugcheck_score']}")
    for name, level in report.get("risks") or []:
        if level in s["block_risk_levels"]:
            reasons.append(f"RugCheck-Risiko: {name}")
    if roundtrip_loss_pct is None:
        reasons.append(DATA_MISSING + "Kauf-/Verkaufs-Quote nicht verfuegbar - kein Trade ohne Honeypot-Test")
    elif roundtrip_loss_pct > s["max_roundtrip_loss_pct"]:
        reasons.append(f"Kauf+Verkauf kostet {roundtrip_loss_pct:.1f} % (Honeypot/zu illiquide)")
    if float(pair.get("liquidity_usd") or 0) < cfg["universe"]["min_liquidity_usd"]:
        reasons.append(f"Liquiditaet {pair.get('liquidity_usd', 0):,.0f} USD zu gering")
    return reasons


# ------------------------------------------------------------------ narrative
def narrative_score(token: Dict[str, Any], pair: Dict[str, Any], recent_names: List[str],
                    x_mentions: Optional[int] = None, news_hits: int = 0, now_ms: Optional[float] = None
                    ) -> Tuple[float, List[str]]:
    """0..1: ORGANISCHE Aufmerksamkeit und Frische.

    Echte Laeufe zeigten: bezahlte DexScreener-Boosts und vorhandene Socials treffen auf praktisch jeden gelisteten
    Token zu (Narrativ war bei 100 % 'wahr'). Gewichtet wird deshalb, was man nicht kaufen kann: GeckoTerminal-
    Trending (aus echter Handelsaktivitaet), Zahl der Kaeufe, Nachrichten, X-Erwaehnungen. Boost und Socials zaehlen
    nur wenig."""
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    parts, why = [], []                       # (value, weight)
    sources = set(token.get("sources") or [])
    trending = 1.0 if "gecko_trending" in sources else 0.3 if "gecko_new" in sources else 0.0
    parts.append((trending, 1.5)); why.append("organisch Trending" if trending == 1.0 else "nicht organisch trending")
    buys = float(pair.get("buys_h1") or 0)
    parts.append((min(1.0, math.log10(1 + buys) / math.log10(1 + 5000)), 1.0)); why.append(f"{buys:.0f} Kaeufe/h")
    socials = set(pair.get("socials") or []) | set(token.get("links") or [])
    soc = len(socials & {"twitter", "telegram", "discord"}) / 3 + (0.34 if pair.get("websites") else 0)
    parts.append((min(1.0, soc), 0.5))
    boost = float(token.get("boost") or pair.get("boosts") or 0)
    parts.append((min(1.0, math.log10(1 + boost) / 3), 0.25))
    if boost:
        why.append(f"bezahlter Boost {boost:.0f} (zaehlt kaum)")
    if x_mentions is not None:
        parts.append((min(1.0, math.log10(1 + x_mentions) / 3), 1.5)); why.append(f"X-Erwaehnungen {x_mentions}")
    if news_hits:
        parts.append((min(1.0, news_hits / 3), 1.0)); why.append(f"in {news_hits} Nachrichten")
    age_h = (now_ms - float(pair.get("created_ms") or now_ms)) / 3.6e6
    fresh = 1.0 if age_h <= 6 else 0.6 if age_h <= 24 else 0.3
    name = (pair.get("symbol") or pair.get("name") or "").lower()
    stem = re.sub(r"[^a-z]", "", name)[:5]
    copies = sum(1 for n in recent_names if stem and stem in re.sub(r"[^a-z]", "", n.lower())) - 1
    if copies >= 3:
        fresh *= 0.4; why.append(f"durchgekaut: {copies} aehnliche Tokens")
    why.append(f"Alter {age_h:.1f} h")
    base = sum(v * w for v, w in parts) / sum(w for _, w in parts)
    return round(0.75 * base + 0.25 * fresh, 3), why


# ------------------------------------------------------------------ chart structure
def structure_ok(candles: List[Dict[str, float]], chase_m5_pct: float) -> Tuple[bool, bool, str]:
    """-> (gesund, senkrechter_pump, Grund). Gesund = Ausbruch nach Konsolidierung oder gehaltener Pullback."""
    if len(candles) < 8:
        return False, False, "zu wenig Kerzen"
    last, prev = candles[-1], candles[-7:-1]
    move = (last["c"] / candles[-2]["c"] - 1) * 100 if candles[-2]["c"] else 0.0
    hi, lo = max(c["h"] for c in prev), min(c["l"] for c in prev)
    rng = (hi - lo) / lo * 100 if lo else 999.0
    vertical = move > chase_m5_pct and rng > chase_m5_pct
    if vertical:
        return False, True, f"senkrechter Pump (+{move:.0f} % in einer Kerze ohne Konsolidierung)"
    if rng <= 25 and last["c"] > hi:
        return True, False, f"Ausbruch aus {rng:.0f} %-Range"
    peak = max(c["h"] for c in candles[-24:])
    pull = (1 - last["c"] / peak) * 100 if peak else 0.0
    if 15 <= pull <= 45 and last["c"] > last["o"] and last["l"] >= candles[-2]["l"]:
        return True, False, f"Pullback {pull:.0f} % gehalten (hoeheres Tief)"
    return False, False, f"keine saubere Struktur (Range {rng:.0f} %, Abstand zum Hoch {pull:.0f} %)"


# ------------------------------------------------------------------ the five signals
def evaluate_signals(pair: Dict[str, Any], safety_reasons: List[str], report: Optional[Dict[str, Any]],
                     narrative: float, holders_growth: Optional[float], smart_buyers: int,
                     candles: List[Dict[str, float]], cfg: Dict[str, Any]) -> Dict[str, Any]:
    t, w = cfg["thresholds"], cfg["weights"]
    vol = pair.get("volume") or {}
    growth = (vol.get("m5", 0) * 12) / vol["h1"] if vol.get("h1") else 0.0
    ratio = pair.get("buys_m5", 0) / max(1, pair.get("sells_m5", 0))
    holders_up = holders_growth > 0 if holders_growth is not None else ratio >= t["buy_ratio_min"]
    struct, vertical, struct_why = structure_ok(candles, t["chase_m5_pct"])
    top10 = float((report or {}).get("top10_pct") or 100)
    sig = {
        "narrative": narrative >= t["narrative_min"],
        "volume_holders": growth >= t["volume_growth_min"] and ratio >= t["buy_ratio_min"] and holders_up,
        "smart_money": smart_buyers is not None and smart_buyers >= t["smart_wallets_min"],
        "distribution": not safety_reasons and top10 <= 0.8 * cfg["safety"]["max_top10_pct"],
        "structure": struct,
    }
    score = sum(w[k] for k, v in sig.items() if v) / sum(w.values())
    count = sum(sig.values())
    chasing = vertical or float((pair.get("change") or {}).get("m5", 0)) > t["chase_m5_pct"]
    # Attention without money flowing in is how falling knives look: at least one flow signal is required.
    no_flow = bool(t.get("require_flow", True)) and not (sig["volume_holders"] or sig["smart_money"])
    enter = count >= int(t["min_signals"]) and not chasing and not safety_reasons and not no_flow
    return {"signals": sig, "count": count, "score": round(score, 3), "enter": enter, "chasing": chasing,
            "no_flow": no_flow,
            "metrics": {"narrative": narrative, "volume_growth": round(growth, 2), "buy_ratio": round(ratio, 2),
                        "holders_growth": holders_growth, "smart_buyers": smart_buyers, "top10_pct": top10,
                        "m5_change": (pair.get("change") or {}).get("m5")},
            "structure": struct_why}


# ------------------------------------------------------------------ risk manager (veto + size)
def risk_check(state: Dict[str, Any], limits: Dict[str, Any], today: str) -> Tuple[Optional[str], float]:
    """-> (Veto-Grund oder None, Einsatz in SOL). Grundlage ist das Desk-Kapital (eigenes Wallet)."""
    tr, desk = limits["trade"], limits["desk"]
    equity = float(state.get("equity") or 0)
    if state.get("halted"):
        return f"Desk gestoppt: {state['halted']}", 0.0
    if state.get("paused_day") == today:
        return f"Tagesverlust-Limit {desk['daily_loss_limit']:.0%} erreicht - Pause bis morgen", 0.0
    if len(state.get("positions") or {}) >= tr["max_open_positions"]:
        return f"max. {tr['max_open_positions']} offene Positionen", 0.0
    size = round(equity * float(tr["risk_per_trade"]), 6)
    if size <= 0:
        return "kein Desk-Kapital", 0.0
    return None, size


def update_limits(state: Dict[str, Any], limits: Dict[str, Any], today: str) -> Optional[str]:
    """Tagesverlust und Drawdown pruefen (auf Desk-Equity inkl. offener Positionen). -> Ereignis oder None."""
    desk = limits["desk"]
    eq = float(state.get("equity_mark") or state.get("equity") or 0)
    if state.get("day") != today:
        state["day"], state["day_start"] = today, eq
    state["peak"] = max(float(state.get("peak") or 0), eq)
    if not state.get("halted") and state["peak"] > 0 and (state["peak"] - eq) / state["peak"] >= desk["max_drawdown_limit"]:
        state["halted"] = f"Drawdown {desk['max_drawdown_limit']:.0%} vom Hoechststand"
        return "halt"
    if state.get("paused_day") != today and state.get("day_start") and \
            (state["day_start"] - eq) / state["day_start"] >= desk["daily_loss_limit"]:
        state["paused_day"] = today
        return "pause"
    return None
