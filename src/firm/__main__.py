import asyncio
import sys

from src.firm.cli import main

if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
