"""Bridge to the migration toolkit (``aap-bridge``).

* :func:`run` executes an ``aap-bridge`` sub-command inside the workspace with
  the toolkit configuration and the ``.env`` from the ``aap-bridge-env`` Secret.
* :class:`StateDB` reads the toolkit state database (SQLite) read-only:
  per-resource migration status and error messages are the ground truth used
  by the agent, the dataset builder and the assistant.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from migration_ai.config import Settings

DB_RELATIVE = "database/migration_state.db"


def load_env_file(path: str) -> dict[str, str]:
    """Parse a dotenv file (KEY=VALUE, comments, optional quotes)."""
    env: dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return env
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:]
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        env[key.strip()] = val
    return env


def toolkit_env(settings: Settings, work_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update(load_env_file(settings.toolkit_env_file))
    env["MIGRATION_STATE_DB_PATH"] = f"sqlite:///{(work_dir / DB_RELATIVE).resolve()}"
    env.setdefault("AAP_BRIDGE_CONFIG", settings.toolkit_config)
    env["PYTHONUNBUFFERED"] = "1"
    env["TERM"] = "dumb"
    env["COLUMNS"] = "160"
    return env


@dataclass
class RunResult:
    command: list[str]
    returncode: int
    seconds: float
    output_tail: str
    log_file: str


def run(settings: Settings, work_dir: Path, args: list[str], log_name: str,
        timeout: int | None = None, binary: str = "aap-bridge") -> RunResult:
    """Run ``aap-bridge <args>`` in ``work_dir``; full output goes to logs/agent/<log_name>.log."""
    for folder in ("logs/agent", "database", "reports"):
        (work_dir / folder).mkdir(parents=True, exist_ok=True)
    cmd = [binary, "--config", settings.toolkit_config, "--log-file", "logs/migration.log", *args]
    log_path = work_dir / "logs" / "agent" / f"{log_name}.log"
    start = time.time()
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write("$ " + " ".join(cmd) + "\n")
        fh.flush()
        try:
            proc = subprocess.run(
                cmd, cwd=work_dir, env=toolkit_env(settings, work_dir), stdin=subprocess.DEVNULL,
                stdout=fh, stderr=subprocess.STDOUT, timeout=timeout, check=False,
            )
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            fh.write(f"\n[migration-ai] timeout after {timeout}s\n")
            rc = 124
        except FileNotFoundError:
            fh.write(f"\n[migration-ai] command not found: {binary}\n")
            rc = 127
    text = log_path.read_text(encoding="utf-8", errors="replace")
    tail = "\n".join(text.splitlines()[-40:])
    return RunResult(cmd, rc, round(time.time() - start, 1), tail[-4000:], str(log_path))


class StateDB:
    """Read-only access to the toolkit's ``migration_progress`` table."""

    def __init__(self, path: Path):
        self.path = path

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def _rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        if not self.exists:
            return []
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            return con.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            con.close()

    def import_rows(self) -> list[dict]:
        return [dict(r) for r in self._rows(
            "SELECT resource_type, source_id, source_name, target_id, status, error_message "
            "FROM migration_progress WHERE phase = 'import'"
        )]

    def status_counts(self, phase: str = "import") -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for r in self._rows(
            "SELECT resource_type, status, COUNT(*) AS n FROM migration_progress WHERE phase = ? "
            "GROUP BY resource_type, status", (phase,)
        ):
            out.setdefault(r["resource_type"], {})[r["status"]] = r["n"]
        return out

    def totals(self, phase: str = "import") -> dict[str, int]:
        c: Counter = Counter()
        for statuses in self.status_counts(phase).values():
            c.update(statuses)
        return dict(c)

    def top_errors(self, limit: int = 15) -> list[dict]:
        from aap_migration.cli.commands.migration_report_v2 import normalize_error_key

        c: Counter = Counter()
        examples: dict[str, dict] = {}
        for r in self.import_rows():
            if r["status"] != "failed":
                continue
            key = normalize_error_key(r["error_message"] or "") or "Unknown error"
            c[(r["resource_type"], key)] += 1
            examples.setdefault((r["resource_type"], key), r)
        return [
            {"resource_type": rt, "error_key": key, "count": n,
             "example": f"{examples[(rt, key)]['source_name']} (#{examples[(rt, key)]['source_id']})"}
            for (rt, key), n in c.most_common(limit)
        ]

    def find(self, text: str, limit: int = 20) -> list[dict]:
        like = f"%{text}%"
        return [dict(r) for r in self._rows(
            "SELECT resource_type, source_id, source_name, target_id, status, error_message, phase "
            "FROM migration_progress WHERE source_name LIKE ? OR CAST(source_id AS TEXT) = ? "
            "ORDER BY phase LIMIT ?", (like, text, limit)
        )]
