"""Live-Freigabe (CLAUDE.md Prinzip 4): zeitlich begrenzt, an Live-Konto und Limits gebunden, nicht faelschbar.

Das Token wird von `firm approve-live` (Operator, mit .env) signiert: HMAC-SHA256 ueber Ablaufzeit, Live-Konto und
den Hash von config/limits.toml. Der Schluessel wird aus den Live-Zugangsdaten abgeleitet; nur der Operator und der
money-Prozess kennen sie. Agenten-, MCP-, API- und Benachrichtigungs-Prozess koennen deshalb kein gueltiges Token
schreiben, auch wenn sie die Datei im gemeinsamen Datenordner ueberschreiben koennten.
Aendern sich die Limits nach der Freigabe, ist das Token ungueltig (neue Freigabe noetig).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Mapping, Optional, Tuple

FIELDS = ("expires_at", "account", "limits_sha256")


def _key(env: Mapping[str, str]) -> Optional[bytes]:
    api, pw = env.get("CAPITAL_LIVE_API_KEY") or "", env.get("CAPITAL_LIVE_PASSWORD") or ""
    if not api or not pw:
        return None
    return hashlib.sha256(f"firm-live-approval|{api}|{pw}".encode()).digest()


def _account(env: Mapping[str, str]) -> str:
    ident = env.get("CAPITAL_LIVE_IDENTIFIER") or ""
    return hashlib.sha256(ident.encode()).hexdigest()[:16]        # bound to the account, identifier not stored


def limits_sha256(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sig(key: bytes, token: dict) -> str:
    msg = json.dumps({k: token.get(k) for k in FIELDS}, sort_keys=True).encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def issue(duration_s: float, env: Mapping[str, str], limits_path: str | Path, now: Optional[float] = None) -> dict:
    key = _key(env)
    if key is None:
        raise RuntimeError("CAPITAL_LIVE_API_KEY / CAPITAL_LIVE_PASSWORD fehlen - ohne Live-Schluessel keine Freigabe")
    now = time.time() if now is None else now
    token = {"created_at": now, "expires_at": now + float(duration_s), "duration_seconds": float(duration_s),
             "account": _account(env), "limits_sha256": limits_sha256(limits_path)}
    token["sig"] = _sig(key, token)
    return token


def verify(token: dict, env: Mapping[str, str], limits_path: str | Path, now: Optional[float] = None) -> Tuple[bool, str]:
    key = _key(env)
    if key is None:
        return False, "Live-Schluessel fehlen in diesem Prozess"
    if not isinstance(token, dict) or not token.get("sig"):
        return False, "Token unsigniert"
    if not hmac.compare_digest(str(token["sig"]), _sig(key, token)):
        return False, "Signatur ungueltig"
    if token.get("account") != _account(env):
        return False, "Token gehoert zu einem anderen Live-Konto"
    try:
        if token.get("limits_sha256") != limits_sha256(limits_path):
            return False, "Limits wurden nach der Freigabe geaendert - neue Freigabe noetig"
    except OSError:
        return False, "limits.toml nicht lesbar"
    if float(token.get("expires_at") or 0) <= (time.time() if now is None else now):
        return False, "Token abgelaufen"
    return True, "ok"
