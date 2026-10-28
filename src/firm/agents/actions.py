"""Was Agenten bewirken duerfen: eine kleine, geschlossene Liste von Aktionen.

Jede Aktion wird hier validiert und normalisiert, bevor der Orchestrator sie ausfuehrt. Es gibt bewusst keine
Aktion, die Risiko erhoeht, eine Order sendet, Limits aendert oder eine Strategie freischaltet.

  tighten_risk     Risiko pro Trade fuer einen Markt (oder "*") befristet senken (Faktor 0..1, max 48 h)
  block_entries    Neue Einstiege befristet sperren (z. B. vor einem Termin). Braucht eine Quelle.
  request_research Forschungslauf anstossen (der Orchestrator begrenzt die Haeufigkeit)
  flag             Hinweis/Alarm an CEO und Dashboard
  propose_playbook Eigene Arbeitsanleitung aendern (Regel hinzufuegen/streichen); wirksam erst nach deiner Freigabe
  propose_hire     (nur CEO) Einstellungsantrag fuer eine vorhandene Rollen-Vorlage; wirksam erst nach Freigabe durch dich
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

ACTION_TYPES = ("tighten_risk", "block_entries", "request_research", "flag", "propose_hire", "propose_playbook")
EPIC_RE = re.compile(r"^[A-Z0-9_.]{2,20}$|^\*$")


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """Erstes vollstaendiges JSON-Objekt aus einer Modellantwort (auch in ```json-Bloecken)."""
    if not text:
        return None
    t = text.strip()
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        pass
    start = t.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for j in range(start, len(t)):
            ch = t[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(t[start:j + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        pass
                    break
        start = t.find("{", start + 1)
    return None


def validate_action(action: Any, allowed: List[str], whitelist: List[str]) -> Tuple[bool, Any]:
    """-> (ok, normalisierte_aktion | fehlertext)"""
    if not isinstance(action, dict):
        return False, "Aktion ist kein Objekt"
    kind = str(action.get("type") or "").strip()
    if kind not in ACTION_TYPES:
        return False, f"unbekannte Aktion '{kind}'"
    if kind not in allowed:
        return False, f"Aktion '{kind}' gehoert nicht zu den Rechten dieser Rolle"
    reason = " ".join(str(action.get("reason") or action.get("message") or "").split())[:300]
    if len(reason) < 10:
        return False, f"{kind}: Begruendung fehlt oder ist zu kurz"

    if kind in ("tighten_risk", "block_entries"):
        epic = str(action.get("epic") or "").strip().upper()
        if not EPIC_RE.match(epic) or (epic != "*" and epic not in whitelist):
            return False, f"{kind}: Markt '{epic}' ist nicht in der Whitelist"
        try:
            hours = float(action.get("hours", 4))
        except (TypeError, ValueError):
            return False, f"{kind}: 'hours' ist keine Zahl"
        if not 0 < hours <= 48:
            return False, f"{kind}: Dauer muss zwischen 0 und 48 Stunden liegen"
        if kind == "block_entries":
            source = " ".join(str(action.get("source") or "").split())[:200]
            if len(source) < 4:
                return False, "block_entries: ohne Quelle (Werkzeug-Ergebnis oder Termin) keine Sperre"
            return True, {"type": kind, "epic": epic, "factor": 0.0, "hours": hours, "reason": reason, "source": source}
        try:
            factor = float(action.get("factor"))
        except (TypeError, ValueError):
            return False, "tighten_risk: 'factor' fehlt"
        if not 0 <= factor < 1:
            return False, "tighten_risk: Faktor muss in [0, 1) liegen (Risiko kann nur sinken)"
        return True, {"type": kind, "epic": epic, "factor": factor, "hours": hours, "reason": reason}

    if kind == "request_research":
        return True, {"type": kind, "reason": reason}

    if kind == "propose_playbook":
        add = " ".join(str(action.get("add_rule") or "").split())[:300]
        remove = " ".join(str(action.get("remove_rule") or "").split())[:300]
        if not add and not remove:
            return False, "propose_playbook: 'add_rule' oder 'remove_rule' fehlt"
        if add and len(add) < 15:
            return False, "propose_playbook: Regel zu kurz"
        return True, {"type": kind, "add_rule": add or None, "remove_rule": remove or None, "reason": reason}

    if kind == "propose_hire":
        role = str(action.get("role") or "").strip()
        if not re.match(r"^[A-Za-z]{3,40}$", role):
            return False, "propose_hire: 'role' muss der Name einer Rollen-Vorlage sein"
        return True, {"type": kind, "role": role, "reason": reason}

    severity = str(action.get("severity") or "info").lower()
    if severity not in ("info", "warn", "critical"):
        severity = "info"
    return True, {"type": "flag", "severity": severity, "message": reason}


# ------------------------------------------------------------------ lessons
_GENERIC = re.compile(
    r"\b(i|we) (should|could|would|need to|must) (have|always|be|make sure|continue|ensure)\b|"
    r"\b(be more careful|double[- ]check|stay vigilant|keep monitoring|continue to monitor)\b",
    re.IGNORECASE,
)


def testable_lesson(text: Optional[str], whitelist: List[str]) -> Optional[str]:
    """Nur pruefbare Aussagen: konkreter Bezug (Markt, Strategie, Zahl) und kein Allgemeinplatz."""
    if not text or not isinstance(text, str):
        return None
    t = " ".join(text.split()).strip(" -*\"'")
    if not 25 <= len(t) <= 300 or t.upper() in ("NONE", "NULL", "N/A"):
        return None
    if _GENERIC.search(t):
        return None
    concrete = (any(e in t.upper() for e in whitelist)
                or re.search(r"\d", t) is not None
                or re.search(r"donchian|momentum|rsi|ema|ml_logit|atr|spread|stop", t, re.IGNORECASE) is not None)
    return t if concrete else None
