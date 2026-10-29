import asyncio
import sys

from src.firm.envload import use_os_trust_store

use_os_trust_store()          # before any HTTPS client is created

from src.firm.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
