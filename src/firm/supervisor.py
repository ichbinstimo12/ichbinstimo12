"""Supervisor: startet die Firma als getrennte Prozesse und ueberwacht sie.

  money  Broker, Desk, RiskGate, Reconciler, Kill-Switch, Kursstream, Forschung, Berichte
         -> einziger Prozess mit den Capital.com-Schluesseln, ohne LLM-Schluessel
  mcp    capitalcom-mcp als lokaler HTTP-Dienst (127.0.0.1), Demo-Schluessel, Trading serverseitig AUS
  brain  Agenten, Scheduler, LLM -> OHNE Broker-Schluessel (auch .env wird nicht gelesen)
  api    Dashboard -> ohne Broker- und LLM-Schluessel
  notify Handy-Benachrichtigungen + Totmannschalter-Ping (nur wenn NOTIFY_*/HEALTHCHECK_PING_URL gesetzt)
         -> ohne Broker- und LLM-Schluessel; die Benachrichtigungs-Tokens kennt nur dieser Prozess

Faellt ein Prozess aus, wird er mit wachsender Pause neu gestartet. Stuerzt der money-Prozess zu oft ab
(5x in 10 Minuten), wird das Halt-Flag gesetzt: lieber stehen als unkontrolliert handeln.
Halt-Flag (CLI/Dashboard/Limitbruch): der money-Prozess stellt glatt und beendet sich, danach stoppt alles.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("Supervisor")

BROKER_KEYS = ("CAPITAL_", "CAP_")
LLM_KEYS = ("ANTHROPIC_",)
NOTIFY_KEYS = ("NOTIFY_", "HEALTHCHECK_")
RETIRED_KEYS = ("SOLANA_", "X_BEARER_TOKEN", "BIRDEYE_", "MEMEDESK_")   # removed memecoin desk: no zone sees these
ALWAYS_DROP = ("FIRM_API_TOKEN",)


def filtered_env(base: Dict[str, str], drop_prefixes=(), keep_api_token: bool = False) -> Dict[str, str]:
    env = {k: v for k, v in base.items()
           if not any(k.startswith(p) for p in drop_prefixes)
           and (keep_api_token or k not in ALWAYS_DROP)}
    env["FIRM_NO_DOTENV"] = "1"   # the child must not re-read .env and get the dropped keys back
    env["PYTHONUNBUFFERED"] = "1"
    return env


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Child:
    name: str
    argv: List[str]
    env: Dict[str, str]
    cwd: Optional[str] = None
    critical: bool = False                 # money: too many crashes -> halt
    proc: Optional[asyncio.subprocess.Process] = None
    restarts: List[float] = field(default_factory=list)


class Supervisor:
    def __init__(self, mode: str, dashboard: bool, host: str, port: int, halt_file: Path,
                 env: Optional[Dict[str, str]] = None):
        self.mode, self.dashboard, self.host, self.port, self.halt_file = mode, dashboard, host, port, halt_file
        self.base_env = dict(env if env is not None else os.environ)
        self.children: List[Child] = []
        self.stopping = False
        self.terminated = False        # stopped by a signal (docker stop), not by a halt
        self._stopped: Optional[asyncio.Event] = None

    # ------------------------------------------------------------ plan
    def plan(self) -> List[Child]:
        py = [sys.executable, "-m", "src.firm"]
        mode_env = {"FIRM_MODE": self.mode}
        kids: List[Child] = [Child("money", py + ["zone", "money", "--mode", self.mode],
                                   {**filtered_env(self.base_env, LLM_KEYS + NOTIFY_KEYS + RETIRED_KEYS), **mode_env}, critical=True)]
        brain_env = {**filtered_env(self.base_env, BROKER_KEYS + NOTIFY_KEYS + RETIRED_KEYS), **mode_env}
        mcp = self._mcp_child()
        if mcp is not None:
            kids.append(mcp)
            brain_env["FIRM_MCP_URL"] = f"http://127.0.0.1:{mcp.env['_PORT']}/mcp"
        else:
            brain_env["FIRM_MCP"] = "0"
        kids.append(Child("brain", py + ["zone", "brain", "--mode", self.mode], brain_env))
        if self.dashboard:
            kids.append(Child("api", py + ["zone", "api", "--host", self.host, "--port", str(self.port)],
                              {**filtered_env(self.base_env, BROKER_KEYS + LLM_KEYS + NOTIFY_KEYS + RETIRED_KEYS, keep_api_token=True),
                               **mode_env}))
        if self.notify_configured():
            kids.append(Child("notify", py + ["zone", "notify"],
                              {**filtered_env(self.base_env, BROKER_KEYS + LLM_KEYS + RETIRED_KEYS), **mode_env}))
        return kids

    def notify_configured(self) -> bool:
        e = self.base_env
        return bool(e.get("NOTIFY_NTFY_URL") or (e.get("NOTIFY_TELEGRAM_BOT_TOKEN") and e.get("NOTIFY_TELEGRAM_CHAT_ID"))
                    or e.get("HEALTHCHECK_PING_URL"))

    def _mcp_child(self) -> Optional[Child]:
        if self.base_env.get("FIRM_MCP", "1").strip().lower() in ("0", "false", "no", "off"):
            return None
        cmd = self.base_env.get("FIRM_MCP_COMMAND", "capitalcom-mcp")
        # start.bat runs .venv/Scripts/python.exe without activating the venv: look next to the interpreter too
        exe = shutil.which(cmd) or shutil.which(cmd, path=str(Path(sys.executable).parent))
        if not exe:
            logger.warning("capitalcom-mcp not installed - agents run without broker tools.")
            return None
        from src.firm.tools.capital_mcp import CapitalMCPTools
        tools = CapitalMCPTools()
        env = tools.server_env(self.base_env)
        workdir = str(tools.workdir.resolve())
        if env is None:
            logger.warning("No Capital.com demo credentials - agents run without broker tools.")
            return None
        Path(workdir).mkdir(parents=True, exist_ok=True)
        port = free_port()
        env["_PORT"] = str(port)
        return Child("mcp", [exe, "run", "--transport", "http", "--host", "127.0.0.1", "--port", str(port)],
                     env, cwd=workdir)

    # ------------------------------------------------------------ run
    async def _spawn(self, c: Child) -> None:
        env = {k: v for k, v in c.env.items() if not k.startswith("_")}
        c.proc = await asyncio.create_subprocess_exec(*c.argv, env=env, cwd=c.cwd)
        logger.info(f"[{c.name}] started (pid {c.proc.pid})")

    async def _watch(self, c: Child) -> None:
        backoff = 2.0
        while not self.stopping:
            await self._spawn(c)
            if c.name == "mcp":
                await asyncio.sleep(2)  # give the HTTP server a moment before the brain connects
            code = await c.proc.wait()
            if self.stopping:
                return
            if self.halt_file.exists():
                # Halt: never restart anything. When the money zone is done (flattened), stop the whole firm.
                if c.name == "money":
                    logger.critical("[money] stopped after HALT - stopping the firm.")
                    asyncio.create_task(self.stop())
                else:
                    logger.warning(f"[{c.name}] stopped (HALT active) - not restarted.")
                return
            now = time.time()
            c.restarts = [t for t in c.restarts if now - t < 600] + [now]
            logger.error(f"[{c.name}] exited with code {code} ({len(c.restarts)} restart(s) in 10 min)")
            if c.critical and len(c.restarts) >= 5:
                self.halt_file.parent.mkdir(parents=True, exist_ok=True)
                self.halt_file.write_text(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} money zone crashed 5x in 10 min")
                logger.critical("money zone keeps crashing - HALT flag written, stopping the firm.")
                asyncio.create_task(self.stop())
                return
            await asyncio.sleep(backoff)
            backoff = min(60.0, backoff * 2)

    async def run(self) -> int:
        self.children = self.plan()
        from src.firm.ops.runtime_env import keep_awake
        keep_awake()
        logger.info("Starting zones: " + ", ".join(c.name for c in self.children))
        # docker stop / systemd send SIGTERM: stop the zones in order instead of dying and orphaning them.
        import signal
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._on_signal)
            except (NotImplementedError, RuntimeError, ValueError):   # Windows / not main thread
                pass
        watchers = [asyncio.create_task(self._watch(c)) for c in self.children]
        if self.dashboard and self.base_env.get("FIRM_OPEN_BROWSER", "1") != "0":
            from src.firm.api.launcher import open_when_ready
            asyncio.create_task(open_when_ready(self.host, self.port, self.base_env.get("FIRM_API_TOKEN", "")))
        try:
            while not self.stopping:
                if self.halt_file.exists() and all(c.name != "money" or c.proc is None or c.proc.returncode is not None
                                                   for c in self.children):
                    await self.stop()
                await asyncio.sleep(1)
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        await self.stop()           # also waits for a stop that a signal handler started
        for w in watchers:          # watchers may sit in a restart back-off; the processes are already stopped
            w.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)
        return 0

    def _on_signal(self) -> None:
        self.terminated = True
        self._signal_stop = asyncio.ensure_future(self.stop())     # keep a reference (exceptions stay visible)

    async def stop(self) -> None:
        if self._stopped is None:
            self._stopped = asyncio.Event()
        if self.stopping:
            await self._stopped.wait()
            return
        self.stopping = True
        try:
            # money last: it may still be flattening positions; notify after it, so the halt/flat message still goes out.
            order = {"money": 1, "notify": 2}
            others = [c for c in self.children if order.get(c.name, 0) == 0]
            for c in others:                                   # signal all non-critical zones at once
                self._terminate(c)
            await asyncio.gather(*(self._wait(c, 10) for c in others))
            for c in sorted((c for c in self.children if c.name in order), key=lambda c: order[c.name]):
                self._terminate(c)
                await self._wait(c, 60 if c.name == "money" else 10)
        finally:
            logger.info("All zones stopped.")
            self._stopped.set()

    @staticmethod
    def _terminate(c: Child) -> None:
        if c.proc is not None and c.proc.returncode is None:
            try:
                c.proc.terminate()
            except ProcessLookupError:
                pass

    @staticmethod
    async def _wait(c: Child, timeout: float) -> None:
        if c.proc is None or c.proc.returncode is not None:
            return
        try:
            await asyncio.wait_for(c.proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error(f"[{c.name}] did not stop in time - killing")
            try:
                c.proc.kill()
            except ProcessLookupError:
                pass
