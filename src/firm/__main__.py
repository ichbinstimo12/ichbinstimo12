import asyncio
import os
import subprocess
import sys
from pathlib import Path


def _venv_python() -> Path | None:
    root = Path(__file__).resolve().parents[2]
    for cand in (root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"):
        if cand.exists():
            return cand
    return None


def _ensure_packages() -> None:
    """`python -m src.firm ...` with the system Python (without packages): hand over to the project's .venv
    instead of crashing with ModuleNotFoundError."""
    try:
        import httpx  # noqa: F401
        return
    except ImportError:
        pass
    venv = _venv_python()
    if venv is not None and os.environ.get("FIRM_VENV_HANDOVER") != "1":
        env = dict(os.environ, FIRM_VENV_HANDOVER="1")      # never loops: the .venv run does not hand over again
        sys.exit(subprocess.call([str(venv), "-m", "src.firm", *sys.argv[1:]], env=env))
    print("Die Python-Pakete der Firma fehlen. Einmal start.bat (oder firm.bat) ausfuehren - das legt .venv an "
          "und installiert alles. Danach z. B.:  .\\firm eval", file=sys.stderr)
    sys.exit(1)


_ensure_packages()

from src.firm.envload import use_os_trust_store  # noqa: E402

use_os_trust_store()          # before any HTTPS client is created

from src.firm.paths import apply_home  # noqa: E402

apply_home()                  # memory outside the code folder: before modules read their path defaults

from src.firm.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
