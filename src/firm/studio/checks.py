"""Deterministische Qualitaets- und Rechtspruefung eines Produkt-Entwurfs (vor der KI-Pruefung und vor dir).

Ein Entwurf, der hier durchfaellt, kommt nie zur Freigabe. Geprueft wird, was ein Programm sicher erkennen kann:
Umfang, Struktur, Preisrahmen, verbotene Versprechen und Themen, persoenliche Daten, Platzhalter, fremde Marken.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

# Versprechen und Themen, die bei digitalen Produkten zu Sperren, Rueckerstattungen oder Haftung fuehren.
FORBIDDEN_CLAIMS = [
    r"garantiert", r"guaranteed", r"100\s*%\s*(sicher|erfolg|success)", r"reich werden", r"get rich",
    r"passives? einkommen ohne", r"risikofrei", r"risk[- ]free", r"heilt", r"cures?\b",
    r"ohne arzt", r"anlageempfehlung", r"investment advice", r"steuertipp", r"tax advice",
    r"kauf(e|en)? (diese|die) aktie", r"buy this (stock|coin)",
]
# Marken/Namen, die ohne Erlaubnis nicht im Produkttitel stehen duerfen (Auszug, erweiterbar).
TRADEMARKS = ["notion", "canva", "excel", "microsoft", "apple", "google", "chatgpt", "openai", "claude",
              "disney", "nike", "adidas", "harry potter", "pokemon", "swisscom", "migros", "coop", "ubs"]
PLACEHOLDERS = [r"\[(insert|your|dein|ihr)[^\]]*\]", r"lorem ipsum", r"\bTODO\b", r"\bXXX\b", r"<placeholder>"]
PII = [r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", r"\+?\d[\d \-/]{8,}\d", r"\bCH\d{2}[ ]?\d{4}"]
MIN_HEADINGS = 3


def word_count(text: str) -> int:
    return len(re.findall(r"\w+", text or ""))


def check_draft(title: str, body: str, price_usd: float, limits: Dict[str, Any],
                min_words: int = 1200) -> List[str]:
    """-> Liste der Befunde (leer = bestanden)."""
    issues: List[str] = []
    low_all = f"{title}\n{body}".lower()
    n = word_count(body)
    if n < min_words:
        issues.append(f"zu kurz: {n} Woerter (mind. {min_words})")
    headings = len(re.findall(r"^#{1,3} \S", body or "", flags=re.M))
    if headings < MIN_HEADINGS:
        issues.append(f"zu wenig Struktur: {headings} Ueberschriften (mind. {MIN_HEADINGS})")
    if not (limits["min_price_usd"] <= float(price_usd) <= limits["max_price_usd"]):
        issues.append(f"Preis {price_usd} ausserhalb {limits['min_price_usd']}-{limits['max_price_usd']} USD")
    for pat in FORBIDDEN_CLAIMS:
        m = re.search(pat, low_all)
        if m:
            issues.append(f"verbotenes Versprechen/Thema: '{m.group(0)}'")
    for name in TRADEMARKS:
        if re.search(rf"\b{re.escape(name)}\b", title.lower()):
            issues.append(f"fremde Marke im Titel: '{name}'")
    for pat in PLACEHOLDERS:
        m = re.search(pat, body or "", flags=re.I)
        if m:
            issues.append(f"Platzhalter im Text: '{m.group(0)}'")
    for pat in PII:
        m = re.search(pat, body or "")
        if m:
            issues.append(f"moegliche persoenliche Daten: '{m.group(0)[:30]}'")
    if re.search(r"https?://", body or ""):
        issues.append("Links im Produkt (nicht pruefbar, koennen veralten oder schaden) - entfernen")
    return issues
