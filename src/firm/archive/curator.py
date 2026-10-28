"""
The cleaning agent's deterministic half.

Policy, in plain words:

* **Trades are never deleted.** Older days are gzipped into
  ``trades/compressed/<YYYY-MM>.jsonl.gz`` and the loose day folders go away.
* **Tasks keep only the latest N per agent.** Older task files are gzipped into
  ``agents/<Agent>/tasks/compressed/<YYYY-MM>.jsonl.gz``.
* **Nothing is removed before it is safely written.** Every gzip append is
  verified, and the curator is idempotent (it skips rows already compressed).

The LLM agent called "Archivist" reads this report and writes its own note, but
it cannot delete anything: the policy lives here, in code.
"""

from __future__ import annotations

import gzip
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from src.firm.archive.store import Archive, _utcnow

logger = logging.getLogger("ArchiveCurator")

DEFAULT_KEEP_TASKS = 40
DEFAULT_KEEP_TRADE_DAYS = 3


class ArchiveCurator:
    def __init__(
        self,
        archive: Archive,
        keep_tasks: int = DEFAULT_KEEP_TASKS,
        keep_trade_days: int = DEFAULT_KEEP_TRADE_DAYS,
    ):
        self.archive = archive
        self.keep_tasks = max(1, int(keep_tasks))
        self.keep_trade_days = max(0, int(keep_trade_days))

    # ------------------------------------------------------------------ run
    def run(self) -> Dict[str, Any]:
        self.archive.ensure()
        report: Dict[str, Any] = {
            "ran_at": _utcnow().isoformat(),
            "tasks_compressed": 0,
            "task_files_kept": 0,
            "trade_events_compressed": 0,
            "trade_days_removed": 0,
            "errors": [],
        }

        for agent_dir in sorted(self.archive.agents_dir.glob("*")):
            if not agent_dir.is_dir():
                continue
            try:
                compressed, kept = self._curate_agent(agent_dir)
                report["tasks_compressed"] += compressed
                report["task_files_kept"] += kept
            except Exception as e:
                logger.error(f"Could not curate {agent_dir.name}: {e}")
                report["errors"].append(f"{agent_dir.name}: {e}")

        try:
            compressed, removed = self._curate_trades()
            report["trade_events_compressed"] = compressed
            report["trade_days_removed"] = removed
        except Exception as e:
            logger.error(f"Could not curate trades: {e}")
            report["errors"].append(f"trades: {e}")

        try:
            self._write_index(report)
        except Exception as e:
            logger.warning(f"Could not write archive index: {e}")

        logger.info(
            "Archive curated: %s task file(s) compressed, %s trade event(s) compressed, "
            "%s old day folder(s) removed.",
            report["tasks_compressed"], report["trade_events_compressed"], report["trade_days_removed"],
        )
        return report

    # --------------------------------------------------------------- agents
    def _curate_agent(self, agent_dir: Path) -> Tuple[int, int]:
        tasks_dir = agent_dir / "tasks"
        if not tasks_dir.exists():
            return 0, 0

        files = self._sorted_task_files(tasks_dir)
        if len(files) <= self.keep_tasks:
            self._write_summary(agent_dir, files)
            return 0, len(files)

        old = files[: len(files) - self.keep_tasks]
        keep = files[len(files) - self.keep_tasks:]

        # Group by month so each month has one compressed file.
        by_month: Dict[str, List[Tuple[Path, Dict[str, Any]]]] = {}
        for path in old:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"Skipping unreadable task file {path}: {e}")
                continue
            month = str(record.get("recorded_at") or "")[:7] or "unknown"
            by_month.setdefault(month, []).append((path, record))

        written = 0
        for month, items in by_month.items():
            target = tasks_dir / "compressed" / f"{month}.jsonl.gz"
            known = self._known_task_ids(target)
            fresh = [(p, r) for (p, r) in items if str(r.get("task_id") or "") not in known]
            if fresh:
                before = target.stat().st_size if target.exists() else 0
                self.archive.append_gzip_jsonl(target, [r for _, r in fresh])
                if not target.exists() or target.stat().st_size <= before:
                    raise RuntimeError(f"gzip append to {target} produced no data")
                written += len(fresh)
            # Only now is it safe to remove the loose files.
            for path, _ in items:
                try:
                    path.unlink()
                except OSError as e:
                    logger.warning(f"Could not remove {path}: {e}")

        self._write_summary(agent_dir, keep)
        return written, len(keep)

    @staticmethod
    def _sorted_task_files(tasks_dir: Path) -> List[Path]:
        """Oldest first, by the recorded time inside the file (not the name:
        many tasks can share a second, and ids are hashed)."""
        def key(path: Path):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                stamp = str(record.get("recorded_at") or "")
            except Exception:
                stamp = ""
            return (stamp, path.name)

        return sorted(tasks_dir.glob("*.json"), key=key)

    @staticmethod
    def _known_task_ids(path: Path) -> Set[str]:
        ids: Set[str] = set()
        if not path.exists():
            return ids
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                for line in f:
                    try:
                        ids.add(str(json.loads(line).get("task_id") or ""))
                    except json.JSONDecodeError:
                        continue
        except Exception:
            return ids
        return ids

    def _write_summary(self, agent_dir: Path, files: List[Path]) -> None:
        rows: List[Dict[str, Any]] = []
        for path in files[-10:]:
            try:
                rows.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
        lines = [f"# {agent_dir.name} - latest tasks", ""]
        if not rows:
            lines.append("_No tasks archived yet._")
        for r in rows:
            deliverable = str((r.get("result") or {}).get("deliverable") or "").replace("\n", " ")
            lessons = (r.get("result") or {}).get("lessons_learned") or []
            lines.append(f"## {str(r.get('recorded_at') or '')[:16]} - {r.get('title')}")
            lines.append(f"- status: `{r.get('status')}`")
            if deliverable:
                lines.append(f"- outcome: {deliverable[:400]}")
            if lessons:
                lines.append("- learned:")
                lines.extend(f"  - {l}" for l in lessons)
            lines.append("")
        try:
            (agent_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Could not write summary for {agent_dir.name}: {e}")

    # --------------------------------------------------------------- trades
    def _curate_trades(self) -> Tuple[int, int]:
        cutoff = date.today() - timedelta(days=self.keep_trade_days)
        compressed = 0
        removed = 0

        for day_dir in sorted(self.archive.trades_dir.iterdir()):
            if not day_dir.is_dir() or day_dir.name == "compressed":
                continue
            try:
                day = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day >= cutoff:
                continue

            files = sorted(day_dir.glob("*.json"))
            if not files:
                try:
                    day_dir.rmdir()
                    removed += 1
                except OSError:
                    pass
                continue

            by_month: Dict[str, List[Tuple[Path, Dict[str, Any]]]] = {}
            for path in files:
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                month = day_dir.name[:7]
                by_month.setdefault(month, []).append((path, record))

            for month, items in by_month.items():
                target = self.archive.compressed_dir / f"{month}.jsonl.gz"
                known = self._known_trade_keys(target)
                fresh = [(p, r) for (p, r) in items if self._trade_key(r) not in known]
                if fresh:
                    before = target.stat().st_size if target.exists() else 0
                    self.archive.append_gzip_jsonl(target, [r for _, r in fresh])
                    if not target.exists() or target.stat().st_size <= before:
                        raise RuntimeError(f"gzip append to {target} produced no data")
                    compressed += len(fresh)
                for path, _ in items:
                    try:
                        path.unlink()
                    except OSError as e:
                        logger.warning(f"Could not remove {path}: {e}")

            try:
                day_dir.rmdir()
                removed += 1
            except OSError:
                pass

        return compressed, removed

    @staticmethod
    def _trade_key(record: Dict[str, Any]) -> str:
        return "|".join(str(record.get(k) or "") for k in ("recorded_at", "kind", "epic", "deal_id"))

    def _known_trade_keys(self, path: Path) -> Set[str]:
        keys: Set[str] = set()
        if not path.exists():
            return keys
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                for line in f:
                    try:
                        keys.add(self._trade_key(json.loads(line)))
                    except json.JSONDecodeError:
                        continue
        except Exception:
            return keys
        return keys

    # ---------------------------------------------------------------- index
    def _write_index(self, report: Dict[str, Any]) -> None:
        stats = self.archive.stats()
        lines = [
            "# Archive index",
            "",
            f"Last curation: `{report['ran_at'][:19]}` UTC",
            "",
            "## Trades",
            "",
            f"- fills recorded: **{stats['trades_total']}** ({stats['trades_closed']} closed)",
            "- realized profit booked in archive: "
            + (
                f"**{stats['realized_profit']:+.2f}**"
                if stats.get("trades_with_profit")
                else "**—** (no broker-confirmed profit yet)"
            ),
            f"- compressed month files: {stats['compressed_files']}",
            "",
            "## Agents",
            "",
        ]
        for agent, count in sorted(stats["tasks_per_agent"].items()):
            lines.append(f"- `{agent}`: {count} task file(s) kept, older ones gzipped")
        lines += [
            "",
            "## Policy",
            "",
            f"- keep the latest {self.keep_tasks} task files per agent",
            f"- keep trade day folders for {self.keep_trade_days} day(s), older days are gzipped",
            "- nothing is deleted before it is written to a .jsonl.gz and verified",
            "",
        ]
        (self.archive.root / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")
