"""Capital.com-Werkzeuge fuer die Agenten ueber den MCP-Server `capitalcom-mcp`
(github.com/SimonTarara62/capitalcom-mcp-server, Apache-2.0, inoffiziell).

Sicherheitsmodell (drei Schichten, jede fuer sich ausreichend):
  1. Der Server wird IMMER mit CAP_ALLOW_TRADING=false und CAP_DRY_RUN=true gestartet -> der Server selbst
     verweigert jede Order, Schliessung, Aenderung.
  2. Er bekommt nur die DEMO-Zugangsdaten (CAP_ENV=demo), nie die Live-Keys.
  3. Diese Klasse ruft nur Werkzeuge aus READ_ONLY_TOOLS auf; alles andere wird vor dem Aufruf abgelehnt.
Ausgefuehrt wird weiterhin ausschliesslich in der deterministischen Money-Zone (RiskGate -> ExecutionGateway).

Der Server laeuft als eigener Prozess (stdio) in einem leeren Arbeitsordner, damit er keine fremde .env liest.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("CapitalMCP")

# Nur lesende Werkzeuge. Namen exakt wie vom Server gemeldet (capitalcom-mcp 0.3.x).
READ_ONLY_TOOLS: Dict[str, str] = {
    "cap_market_search": "Instrumente suchen: {\"search_term\": \"gold\"}",
    "cap_market_get": "Handelsregeln + aktueller Bid/Offer eines Epics: {\"epic\": \"GOLD\"}",
    "cap_market_prices": "Historische Kerzen: {\"epic\": \"GOLD\", \"resolution\": \"HOUR\", \"max\": 48}",
    "cap_market_sentiment": "Long/Short-Verteilung der Kunden: {\"market_id\": \"GOLD\"}",
    "cap_trade_positions_list": "Offene Positionen (nur lesen): {}",
    "cap_trade_orders_list": "Offene Working Orders (nur lesen): {}",
    "cap_account_history_activity": "Kontoaktivitaet (Deals, Stops): {\"last_period\": 86400}",
    "cap_account_history_transactions": "Realisierte Transaktionen (P&L, Swaps, Gebuehren): {\"last_period\": 86400}",
}

MAX_RESULT_CHARS = 4000


def _env_secret(name: str, env: Optional[Dict[str, str]] = None) -> Optional[str]:
    v = (env if env is not None else os.environ).get(name)
    if not v or v.strip() in ("", "HIER_EINFUEGEN"):
        return None
    return v


def os_ca_bundle(workdir: Path) -> Optional[str]:
    """Windows: certifi + Root/CA-Zertifikate des Windows-Speichers als PEM-Datei fuer den MCP-Prozess (der kann
    truststore nicht nutzen). Andere Systeme: None (dort gelten die System-Zertifikate ohnehin)."""
    if sys.platform != "win32":
        return None
    try:
        import ssl
        import certifi
        pems = [Path(certifi.where()).read_text(encoding="ascii", errors="ignore")]
        for store in ("ROOT", "CA"):
            for cert, encoding, trust in ssl.enum_certificates(store):
                if encoding == "x509_asn" and (trust is True or "1.3.6.1.5.5.7.3.1" in trust):
                    pems.append(ssl.DER_cert_to_PEM_cert(cert))
        path = Path(workdir) / "ca_bundle.pem"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(pems), encoding="ascii")
        return str(path.resolve())
    except Exception as e:
        logger.warning(f"Windows certificate export for MCP failed: {e}")
        return None


class CapitalMCPTools:
    """Duenner, sicherer MCP-Client. `available` ist False, wenn Paket/Server/Zugangsdaten fehlen."""

    def __init__(self, command: Optional[str] = None, workdir: str | Path = "data/mcp", call_timeout: float = 30.0):
        self.command = command or os.getenv("FIRM_MCP_COMMAND", "capitalcom-mcp")
        self.workdir = Path(workdir)
        self.call_timeout = call_timeout
        self.available = False
        self.reason = "nicht gestartet"
        self.tools: List[str] = []
        self._stack: Optional[AsyncExitStack] = None
        self._session = None
        self._lock = asyncio.Lock()

    def server_env(self, env: Optional[Dict[str, str]] = None) -> Optional[Dict[str, str]]:
        """Umgebung fuer den MCP-Server-Prozess (auch vom Supervisor fuer den HTTP-Betrieb benutzt)."""
        return self._server_env(env)

    def _server_env(self, base: Optional[Dict[str, str]] = None) -> Optional[Dict[str, str]]:
        src = base if base is not None else os.environ
        key, ident, pw = (_env_secret("CAPITAL_API_KEY", src), _env_secret("CAPITAL_IDENTIFIER", src),
                          _env_secret("CAPITAL_PASSWORD", src))
        if not (key and ident and pw):
            return None
        home = str(self.workdir.resolve())
        env = {
            "PATH": src.get("PATH", ""),
            "HOME": home,
            "USERPROFILE": home,            # Windows: Path.home() reads USERPROFILE, not HOME
            "CAP_API_KEY": key, "CAP_IDENTIFIER": ident, "CAP_API_PASSWORD": pw,
            "CAP_ENV": "demo",              # Agenten sehen nie das Live-Konto
            "CAP_ALLOW_TRADING": "false",   # Server verweigert Orders
            "CAP_DRY_RUN": "true",          # ... und blockiert zusaetzlich jede Ausfuehrung
            "CAP_ALLOWED_EPICS": "",
            "CAP_WS_ENABLED": "false",
        }
        for k in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "LANG"):  # Windows/Locale
            if src.get(k):
                env[k] = src[k]
        bundle = os_ca_bundle(self.workdir)
        if bundle:                          # same certificates as the firm (antivirus/proxy roots on Windows)
            env["SSL_CERT_FILE"] = env["REQUESTS_CA_BUNDLE"] = bundle
        return env

    async def start(self) -> bool:
        if os.getenv("FIRM_MCP", "1").strip().lower() in ("0", "false", "no", "off"):
            self.reason = "per FIRM_MCP=0 abgeschaltet"
            return False
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError:
            self.reason = "Python-Paket 'mcp' fehlt (pip install -r requirements.txt)"
            return False
        url = os.getenv("FIRM_MCP_URL")
        if url:
            # Getrennter Betrieb: Der Supervisor startet den Server mit den Demo-Schluesseln; dieser Prozess kennt sie nie.
            return await self._connect_http(url, ClientSession)
        exe = shutil.which(self.command) or shutil.which(self.command, path=str(Path(sys.executable).parent))
        if not exe:
            self.reason = f"MCP-Server '{self.command}' nicht gefunden (pip install capitalcom-mcp)"
            return False
        env = self._server_env()
        if env is None:
            self.reason = "Capital.com-Demo-Zugangsdaten fehlen"
            return False
        self.workdir.mkdir(parents=True, exist_ok=True)
        params = StdioServerParameters(command=exe, args=["run"], env=env, cwd=str(self.workdir.resolve()))
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), timeout=30)
            listed = await asyncio.wait_for(session.list_tools(), timeout=30)
        except Exception as e:
            await stack.aclose()
            self.reason = f"MCP-Start fehlgeschlagen: {e}"
            logger.warning(self.reason)
            return False
        return self._ready(stack, session, listed)

    async def _connect_http(self, url: str, ClientSession, wait_s: float = 60.0) -> bool:
        from urllib.parse import urlparse
        from mcp.client.streamable_http import streamablehttp_client
        # Wait until the server accepts connections (the supervisor starts it in parallel).
        u = urlparse(url)
        deadline = asyncio.get_running_loop().time() + wait_s
        while True:
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(u.hostname, u.port), timeout=3)
                w.close()
                break
            except (OSError, asyncio.TimeoutError):
                if asyncio.get_running_loop().time() > deadline:
                    self.reason = f"MCP-Server unter {url} antwortet nicht"
                    return False
                await asyncio.sleep(1)
        stack = AsyncExitStack()
        try:
            read, write, _ = await stack.enter_async_context(streamablehttp_client(url, timeout=30))
            session = await stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), timeout=30)
            listed = await asyncio.wait_for(session.list_tools(), timeout=30)
        except BaseException as e:  # anyio raises BaseExceptionGroup; a broken tool server must not kill the agents
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            try:
                await stack.aclose()
            except BaseException:
                pass
            self.reason = f"MCP-Server unter {url} nicht erreichbar: {type(e).__name__}"
            logger.warning(self.reason)
            return False
        return self._ready(stack, session, listed)

    def _ready(self, stack, session, listed) -> bool:
        server_tools = {t.name for t in listed.tools}
        self.tools = [t for t in READ_ONLY_TOOLS if t in server_tools]
        self._stack, self._session = stack, session
        self.available = bool(self.tools)
        self.reason = "ok" if self.available else "Server meldet keine bekannten Lese-Werkzeuge"
        logger.info(f"Capital.com MCP bereit (nur lesen): {', '.join(self.tools)}")
        return self.available

    def describe(self, allowed: Optional[List[str]] = None) -> str:
        names = [t for t in self.tools if allowed is None or t in allowed]
        return "\n".join(f"- {n}: {READ_ONLY_TOOLS[n]}" for n in names)

    async def call(self, name: str, args: Optional[Dict[str, Any]] = None,
                   allowed: Optional[List[str]] = None) -> Dict[str, Any]:
        """Ruft ein Lese-Werkzeug auf. Alles ausserhalb der Allowlist wird abgelehnt, ohne den Server zu fragen."""
        if name not in READ_ONLY_TOOLS:
            return {"ok": False, "error": f"Werkzeug '{name}' ist fuer Agenten gesperrt (nur lesende Werkzeuge)"}
        if allowed is not None and name not in allowed:
            return {"ok": False, "error": f"Werkzeug '{name}' gehoert nicht zu den Rechten dieser Rolle"}
        if not self.available or self._session is None:
            return {"ok": False, "error": f"MCP nicht verfuegbar: {self.reason}"}
        args = {k: v for k, v in (args or {}).items() if k not in ("confirm", "force")}
        async with self._lock:
            try:
                res = await asyncio.wait_for(self._session.call_tool(name, args), timeout=self.call_timeout)
            except Exception as e:
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        text = ""
        for block in getattr(res, "content", None) or []:
            text += getattr(block, "text", "") or ""
        structured = getattr(res, "structuredContent", None)
        if structured and not text:
            text = json.dumps(structured, default=str)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + " ...[gekuerzt]"
        return {"ok": not getattr(res, "isError", False), "result": text}

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception:
                pass
        self._stack, self._session, self.available = None, None, False
