"""Which code produced a run.

A run artifact that cannot be tied back to a commit is not reproducible: the
same run_id can be regenerated from different code and nothing in the output
says so. This records the commit at run time — collect time is too late,
because collection can happen days and several commits later.

``dirty`` is recorded rather than suppressed. A run from an uncommitted tree
is still a real run; pretending it came from the commit alone is the failure
mode this is here to prevent.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TIMEOUT_S = 5


def _git(*args: str, cwd: Path) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def describe_code_version(repo_root: Optional[Path] = None) -> Dict[str, Any]:
    """Return {commit, dirty, branch} for the working tree, or {} if unavailable."""
    root = Path(repo_root) if repo_root is not None else _REPO_ROOT
    commit = _git("rev-parse", "HEAD", cwd=root)
    if commit is None:
        return {}
    status = _git("status", "--porcelain", cwd=root)
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=root)
    version: Dict[str, Any] = {
        "commit": commit,
        # None (git failed) is not the same as clean, and must not read as clean.
        "dirty": None if status is None else bool(status),
    }
    if branch:
        version["branch"] = branch
    return version
