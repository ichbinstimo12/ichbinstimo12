"""Oeffnet das Dashboard automatisch im Browser, sobald der Server antwortet (inkl. Token, damit es sich sofort verbindet)."""
import asyncio
import logging
import os
import urllib.request
import webbrowser
from urllib.parse import quote

logger = logging.getLogger("Launcher")


def dashboard_url(host: str, port: int, token: str = "") -> str:
    host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    token = (token or "").strip()
    if token in ("", "HIER_EINFUEGEN"):
        return f"http://{host}:{port}/"
    return f"http://{host}:{port}/?token={quote(token)}"


def _healthy(host: str, port: int) -> bool:
    host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


async def open_when_ready(host: str, port: int, token: str = "", timeout: float = 60.0, interval: float = 0.5) -> bool:
    """Wartet auf /health und oeffnet dann den Browser. Gibt True zurueck, wenn geoeffnet wurde. Bricht nie den Start ab."""
    loop = asyncio.get_running_loop()
    waited = 0.0
    while waited < timeout:
        if await loop.run_in_executor(None, _healthy, host, port):
            url = dashboard_url(host, port, token)
            try:
                opened = webbrowser.open(url)
            except Exception as e:  # noqa
                logger.warning("Browser konnte nicht geoeffnet werden: %s", e)
                return False
            if not opened:
                logger.info("Kein Browser gefunden. Oeffne selbst: http://%s:%s/", host, port)
            return bool(opened)
        await asyncio.sleep(interval)
        waited += interval
    logger.warning("Dashboard antwortete nicht innerhalb von %.0f s; Browser nicht geoeffnet.", timeout)
    return False
