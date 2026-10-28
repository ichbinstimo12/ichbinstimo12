"""Schutz gegen Prompt-Injection in externen Daten (nach ECC `llm-trading-agent-security` / `agent-security-hardening`).

Werkzeug-Ergebnisse (Capital.com/MCP), Instrumentnamen, Texte anderer Agenten und Archivinhalte sind Daten, keine
Anweisungen. Bevor sie in einen Prompt gelangen, werden sie hier geprueft:
  * unsichtbare/Steuerzeichen werden entfernt, Laenge begrenzt
  * typische Injection-Muster (Rollenwechsel, "ignore instructions", Aufforderungen zu Aktionen/Geldbewegungen,
    Chat-Steuer-Token) fuehren zur Schwaerzung des betroffenen Textes
Ein Schritt mit verdaechtigen Daten gilt als "belastet": seine Aktionen und Lektionen werden verworfen (fail closed).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, List, Tuple

REDACTED = "[ENTFERNT: moeglicher Prompt-Injection-Versuch in externen Daten]"
MAX_EXTERNAL_CHARS = 4000

PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above|earlier) (instructions|rules|messages)",
    r"disregard (all |any |the )?(previous|prior|above|system)",
    r"(new|updated|override) (task|directive|instruction|system prompt)",
    r"\bsystem prompt\b",
    r"\byou are now\b",
    r"\bact as (an? )?(admin|developer|system|root)",
    r"\b(jailbreak|developer mode)\b",
    r"<\|?(im_start|im_end|system|endoftext)\|?>",
    r"\[/?(inst|system)\]",
    r"\"type\"\s*:\s*\"(tighten_risk|block_entries|request_research|propose_hire|propose_playbook|flag)\"",
    r"\b(withdraw|transfer|send|wire)\b.{0,40}\b(funds|money|balance|chf|usd|eur|btc)\b",
    r"\b(api[_ -]?key|password|secret|private key|seed phrase|x-security-token|\bcst\b)\b.{0,30}(is|=|:|send|reveal|print)",
    r"\b(place|open|execute) (an? )?(order|trade|position)\b.{0,40}\b(now|immediately|without)\b",
    r"ignoriere (alle )?(vorherigen|bisherigen) (anweisungen|regeln)",
    r"\bneue (aufgabe|anweisung)\b",
]
_RX = [re.compile(p, re.IGNORECASE | re.DOTALL) for p in PATTERNS]
_INVISIBLE = {"Cf", "Cc", "Co", "Cs"}


def normalize(text: str) -> str:
    """Unicode normalisieren, unsichtbare und Steuerzeichen entfernen (Zeilenumbruch/Tab bleiben)."""
    t = unicodedata.normalize("NFKC", str(text))
    return "".join(ch for ch in t if ch in "\n\t" or unicodedata.category(ch) not in _INVISIBLE)


def findings(text: str) -> List[str]:
    t = normalize(text)
    return [p for p, rx in zip(PATTERNS, _RX) if rx.search(t)]


def screen(text: Any, limit: int = MAX_EXTERNAL_CHARS) -> Tuple[str, List[str]]:
    """-> (sicherer Text, gefundene Muster). Verdaechtiger Text wird komplett ersetzt."""
    t = normalize(text if isinstance(text, str) else str(text))
    hits = findings(t)
    if hits:
        return REDACTED, hits
    if len(t) > limit:
        t = t[:limit] + " ...[gekuerzt]"
    return t, []


def screen_obj(obj: Any) -> Tuple[Any, List[str]]:
    """Wie screen(), rekursiv fuer JSON-artige Strukturen (Fakten): nur betroffene Strings werden geschwaerzt."""
    hits: List[str] = []

    def walk(o):
        if isinstance(o, str):
            clean, h = screen(o, limit=2000)
            hits.extend(h)
            return clean
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in o]
        return o

    return walk(obj), hits
