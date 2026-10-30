"""Betriebshilfen fuer den Dauerlauf: Rechner wach halten, Windows-Rauschen filtern, Fehler drosseln, Ollama starten.

Nichts hier beruehrt Geld oder Limits. Alles laesst sich per Umgebungsvariable abschalten.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger("Ops")

# Windows: SetThreadExecutionState-Flags (winbase.h)
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def keep_awake(platform: str = sys.platform, kernel32: Any = None) -> bool:
    """Verhindert unter Windows den Energiesparmodus, solange der Prozess laeuft (Bildschirm darf aus).
    Abschalten mit FIRM_KEEP_AWAKE=0. -> True, wenn aktiv."""
    if os.getenv("FIRM_KEEP_AWAKE", "1") == "0" or not platform.startswith("win"):
        return False
    try:
        if kernel32 is None:
            import ctypes
            kernel32 = ctypes.windll.kernel32          # type: ignore[attr-defined]
        ok = bool(kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED))
    except Exception as e:
        logger.warning(f"KeepAwake not available: {e}")
        return False
    if ok:
        logger.info("KeepAwake active: Windows will not go to sleep while the firm runs (FIRM_KEEP_AWAKE=0 disables).")
    return ok


def is_benign_disconnect(context: Dict[str, Any]) -> bool:
    """Windows-Proactor meldet abgebrochene Verbindungen der Gegenseite (WinError 10054) als 'unbehandelte
    Ausnahme', obwohl httpx/websockets sie bereits behandeln. Das ist Rauschen, kein Fehler der Firma."""
    exc = context.get("exception")
    if isinstance(exc, ConnectionResetError):
        return True
    return isinstance(exc, OSError) and getattr(exc, "winerror", None) in (10053, 10054)


def install_loop_noise_filter(loop: Any) -> None:
    default = loop.get_exception_handler()

    def handler(lp, context):
        if is_benign_disconnect(context):
            logger.debug(f"Ignored remote disconnect: {context.get('exception')}")
            return
        if default is not None:
            default(lp, context)
        else:
            lp.default_exception_handler(context)

    loop.set_exception_handler(handler)


class ErrorThrottle:
    """Gleiche Fehler (z. B. waehrend eines Internet-Ausfalls fuer jeden Markt) nur einmal pro Fenster loggen,
    danach eine Zusammenfassung mit der Anzahl unterdrueckter Meldungen."""

    def __init__(self, window_s: float = 60.0, now_fn: Callable[[], float] = time.monotonic):
        self.window, self.now = window_s, now_fn
        self._state: Dict[str, list] = {}       # key -> [window_start, suppressed]

    def should_log(self, key: str) -> Optional[int]:
        """-> None = unterdruecken, sonst Anzahl der seit dem letzten Log unterdrueckten Meldungen."""
        t = self.now()
        st = self._state.get(key)
        if st is None or t - st[0] >= self.window:
            suppressed = st[1] if st else 0
            self._state[key] = [t, 0]
            return suppressed
        st[1] += 1
        return None


def error_kind(e: BaseException) -> str:
    """Fehlerart ohne marktspezifische Details: alle 'getaddrinfo failed' zaehlen als ein Fehler."""
    return f"{type(e).__name__}:{str(e)[:60]}"


_ollama_started_at: Optional[float] = None


def try_start_ollama(base_url: str, which: Callable[[str], Optional[str]] = shutil.which,
                     popen: Callable[..., Any] = subprocess.Popen, now_fn: Callable[[], float] = time.monotonic) -> bool:
    """Ollama laeuft nicht? Bei lokaler Adresse und installiertem `ollama` einmal `ollama serve` im Hintergrund
    starten (hoechstens alle 10 Minuten). Abschalten mit FIRM_OLLAMA_AUTOSTART=0."""
    global _ollama_started_at
    if os.getenv("FIRM_OLLAMA_AUTOSTART", "1") == "0":
        return False
    if (urlparse(base_url).hostname or "") not in ("localhost", "127.0.0.1", "::1"):
        return False
    if _ollama_started_at is not None and now_fn() - _ollama_started_at < 600:
        return False
    exe = which("ollama")
    if not exe:
        return False
    kwargs: Dict[str, Any] = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    try:
        popen([exe, "serve"], **kwargs)
    except Exception as e:
        logger.warning(f"Could not start Ollama: {e}")
        return False
    _ollama_started_at = now_fn()
    logger.info("Ollama was not running - started 'ollama serve' in the background.")
    return True
