"""Gedaechtnis ueberlebt Updates: Laufzeitdaten liegen in einem festen Datenordner ausserhalb des Programmordners,
alte Daten im Programmordner werden beim ersten Start uebernommen, ein alter Entpack-Ordner laesst sich importieren."""
import os
import sqlite3

from src.firm import paths
from src.firm.agents.memory import LessonStore
from src.firm.bus.event_store import EventStore

KEYS = ("FIRM_HOME", "FIRM_DB_PATH", "FIRM_STATE_PATH", "FIRM_HALT_FILE", "FIRM_LIVE_TOKEN_FILE", "FIRM_ARCHIVE_DIR",
        "FIRM_REPORT_DIR", "FIRM_BACKUP_DIR", "FIRM_STUDIO_DIR", "FIRM_DATA_DIR", "NOTIFY_CURSOR_FILE")


def clean_env(monkeypatch):
    for k in KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("FIRM_NO_DOTENV", "1")


def old_install(root):
    """Ein Programmordner mit Gedaechtnis wie bisher (alles relativ im Ordner)."""
    root.mkdir(parents=True, exist_ok=True)
    LessonStore(str(root / "firm_events.db")).add("RiskOfficer", "GOLD orb lost 5 trades in a row at spread 0.2 ATR",
                                                    evidence_task_id="t1")
    EventStore(str(root / "firm_events.db")).append("X", "TRADE_RESULT", {"epic": "GOLD", "r": -1.0})
    (root / "firm_state.json").write_text('{"peak": 200}')
    (root / "data").mkdir()
    (root / "data" / "strategy_verdicts.json").write_text('{"verdicts": {}}')
    (root / "archiv").mkdir()
    (root / "archiv" / "trade.json").write_text("{}")


def test_home_paths_are_set_and_explicit_values_win(tmp_path, monkeypatch):
    clean_env(monkeypatch)
    monkeypatch.setenv("FIRM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FIRM_REPORT_DIR", "/custom/reports")
    monkeypatch.setenv("FIRM_BACKUP_DIR", "data/backups")          # relativ wie in alten .env-Dateien
    home = paths.apply_home(project_root=tmp_path / "code")
    assert home == tmp_path / "home"
    assert os.environ["FIRM_DB_PATH"] == str(home / "firm_events.db")
    assert os.environ["FIRM_DATA_DIR"] == str(home / "data")
    assert os.environ["FIRM_REPORT_DIR"] == "/custom/reports"
    assert os.environ["FIRM_BACKUP_DIR"] == str(home / "data" / "backups")
    assert paths.data_dir() == home / "data"


def test_first_start_takes_over_memory_from_project_folder(tmp_path, monkeypatch):
    clean_env(monkeypatch)
    code = tmp_path / "code"
    old_install(code)
    monkeypatch.setenv("FIRM_HOME", str(tmp_path / "home"))
    home = paths.apply_home(project_root=code)
    assert LessonStore(str(home / "firm_events.db")).count() == 1
    assert (home / "data" / "strategy_verdicts.json").exists() and (home / "archiv" / "trade.json").exists()
    assert (home / "firm_state.json").read_text() == '{"peak": 200}'
    assert (code / "firm_events.db").exists()                 # Original bleibt (nur kopiert)
    # zweiter Start: nichts wird ueberschrieben
    LessonStore(str(home / "firm_events.db")).add("CEO", "EURUSD momentum had 0.3 R expectancy over 40 trades",
                                                   evidence_task_id="t2")
    paths.apply_home(project_root=code)
    assert LessonStore(str(home / "firm_events.db")).count() == 2


def test_import_from_old_unzipped_folder_keeps_a_backup(tmp_path, monkeypatch):
    clean_env(monkeypatch)
    monkeypatch.setenv("FIRM_HOME", str(tmp_path / "home"))
    home = paths.apply_home(project_root=tmp_path / "fresh")     # neue Version, leeres Gedaechtnis
    EventStore(str(home / "firm_events.db")).append("X", "BOOT", {})
    old = tmp_path / "trading-firm-old"
    old_install(old)
    report = paths.import_memory(old, home)
    assert report["lessons"] == 1 and report["backup"] and os.path.exists(report["backup"])
    assert LessonStore(str(home / "firm_events.db")).count() == 1
    assert (home / "data" / "strategy_verdicts.json").exists()


def test_import_refuses_folder_without_memory(tmp_path):
    (tmp_path / "empty").mkdir()
    try:
        paths.import_memory(tmp_path / "empty", tmp_path / "home")
        assert False, "should refuse"
    except FileNotFoundError:
        pass


def test_project_mode_keeps_old_behaviour(tmp_path, monkeypatch):
    clean_env(monkeypatch)
    monkeypatch.setenv("FIRM_HOME", "project")
    assert paths.apply_home(project_root=tmp_path) is None
    assert "FIRM_DB_PATH" not in os.environ


def test_old_env_with_relative_db_path_still_moves_memory_out(tmp_path, monkeypatch):
    clean_env(monkeypatch)
    code = tmp_path / "code"
    old_install(code)
    monkeypatch.setenv("FIRM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("FIRM_DB_PATH", "firm_events.db")
    home = paths.apply_home(project_root=code)
    assert os.environ["FIRM_DB_PATH"] == str(home / "firm_events.db")
    assert LessonStore(os.environ["FIRM_DB_PATH"]).count() == 1
