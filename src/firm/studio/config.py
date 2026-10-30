"""Studio-Konfiguration und harte Grenzen.

Grenzen kommen aus config/limits.toml [studio] (nur der Operator aendert sie). Fehlt der Abschnitt, gelten die
sicheren Standardwerte unten. Werte, die lockerer sind als die absoluten Obergrenzen, werden auf diese begrenzt.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Dict

DEFAULT_LIMITS: Dict[str, Any] = {
    "enabled": True,
    "max_products_per_week": 3,      # neue Entwuerfe pro Woche
    "max_awaiting_approval": 5,      # liegen so viele bei dir zur Freigabe, macht das Studio Pause
    "max_monthly_ai_usd": 10.0,      # KI-Kosten des Studios pro Monat (Ollama zaehlt 0)
    "min_price_usd": 3.0,
    "max_price_usd": 49.0,
}
# Absolute Obergrenzen, die auch limits.toml nicht ueberschreiten kann (Schutz vor Tippfehlern).
CEILING = {"max_products_per_week": 14, "max_awaiting_approval": 20, "max_monthly_ai_usd": 200.0,
           "max_price_usd": 199.0}


def _read(path: Path) -> Dict[str, Any]:
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def load_limits(path: str | Path = "config/limits.toml") -> Dict[str, Any]:
    raw = _read(Path(path)).get("studio") or {}
    out = dict(DEFAULT_LIMITS)
    for k, default in DEFAULT_LIMITS.items():
        v = raw.get(k, default)
        out[k] = type(default)(v) if not isinstance(default, bool) else bool(v)
    for k, cap in CEILING.items():
        out[k] = min(out[k], type(out[k])(cap))
    out["min_price_usd"] = max(0.0, min(out["min_price_usd"], out["max_price_usd"]))
    return out


def load_config(path: str | Path = "config/studio.toml") -> Dict[str, Any]:
    cfg = _read(Path(path))
    cfg.setdefault("shop", {"platform": "gumroad", "currency": "usd"})
    cfg.setdefault("product", {})
    cfg["product"].setdefault("languages", ["de"])
    cfg["product"].setdefault("niches", [])
    cfg["product"].setdefault("formats", ["Checkliste"])
    cfg["product"].setdefault("min_words", 1200)
    cfg.setdefault("llm_prices", {})
    return cfg


def load_treasury(path: str | Path = "config/treasury.toml") -> Dict[str, Any]:
    p = _read(Path(path)).get("policy") or {}
    return {"tax_reserve_pct": min(max(float(p.get("tax_reserve_pct", 0.25)), 0.0), 0.9),
            "runway_months": max(float(p.get("runway_months", 3)), 0.0),
            "trading_topup_pct": min(max(float(p.get("trading_topup_pct", 0.5)), 0.0), 1.0),
            "require_pass_strategy": bool(p.get("require_pass_strategy", True)),
            "min_transfer_usd": max(float(p.get("min_transfer_usd", 50)), 0.0)}
