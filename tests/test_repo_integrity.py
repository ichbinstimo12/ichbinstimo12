"""Jede Code-, Konfigurations- und Testdatei muss versioniert sein (eine zu breite .gitignore-Regel hatte
src/firm/data/news.py aus dem Repository und der ZIP entfernt -> Absturz beim Start)."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TRACKED_DIRS = ("src", "config", "web", "tests", "scripts", "evals", "deploy", "docs")


@pytest.mark.skipif(shutil.which("git") is None or not (ROOT / ".git").exists(), reason="no git checkout")
def test_no_source_file_is_ignored_by_git():
    files = [str(p.relative_to(ROOT)) for d in TRACKED_DIRS for p in (ROOT / d).rglob("*")
             if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    out = subprocess.run(["git", "check-ignore", "--stdin"], input="\n".join(files), capture_output=True,
                         text=True, cwd=ROOT)
    assert out.stdout.strip() == "", f"von .gitignore ausgeschlossen: {out.stdout}"
