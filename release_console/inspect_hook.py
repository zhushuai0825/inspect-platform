"""Run the existing inspect suite after a publish reaches a terminal state.

This calls core.execute. It does not add, edit, or replace builtin checks.
"""

from __future__ import annotations

import re


def run_existing_suite(run_id: str) -> dict[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", run_id):
        raise ValueError("invalid run id")
    import core

    report = core.execute(run_id=run_id, trigger="jenkins")
    return {"id": str(report.get("id") or run_id), "status": str(report.get("status") or "")}
