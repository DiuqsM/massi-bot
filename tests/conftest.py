"""
Shared pytest configuration for all Massi-Bot tests.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, patch

# ── Test result collection for log files ─────────────────────────────────────

_test_records: list[dict] = []
_session_start: float = 0.0


def pytest_sessionstart(session):
    global _session_start
    _session_start = time.time()


def pytest_runtest_logreport(report):
    """Capture per-test timing and outcome after each phase."""
    if report.when != "call":
        return

    # Extract duration from the report (seconds)
    duration_ms = round(report.duration * 1000) if hasattr(report, "duration") else None

    record = {
        "nodeid": report.nodeid,
        "passed": report.passed,
        "failed": report.failed,
        "skipped": report.skipped,
        "duration_ms": duration_ms,
        "longrepr": None,
    }

    if report.failed and report.longrepr:
        record["longrepr"] = str(report.longrepr)

    _test_records.append(record)


def pytest_sessionfinish(session, exitstatus):
    """Write JSON log at end of session, only when concurrency tests are included."""
    concurrency_records = [
        r for r in _test_records if "test_concurrency" in r["nodeid"]
    ]
    if not concurrency_records:
        return

    run_at = datetime.now(timezone.utc)
    total_elapsed = round(time.time() - _session_start, 2)

    passed = sum(1 for r in concurrency_records if r["passed"])
    failed = sum(1 for r in concurrency_records if r["failed"])
    skipped = sum(1 for r in concurrency_records if r["skipped"])

    out_dir = Path(__file__).parent.parent / "docs" / "test_results" / run_at.strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = run_at.strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"concurrency_{stamp}.json"

    data = {
        "run_at": run_at.isoformat(),
        "elapsed_seconds": total_elapsed,
        "summary": {
            "total": len(concurrency_records),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
        },
        "tests": concurrency_records,
    }
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nConcurrency log: {json_path}")


@pytest.fixture
def anyio_backend():
    """Lock all anyio tests to asyncio — trio is not installed."""
    return "asyncio"


@pytest.fixture(autouse=True)
def mock_memory_manager():
    """
    Patch memory_manager globally to prevent real Supabase/sentence-transformers
    calls during tests. Without this, anyio event-loop teardown fails because
    httpx transports can't close after the loop is shut down.
    """
    try:
        from llm import memory_manager as mm_module
        with patch.object(mm_module.memory_manager, "get_context_memories",
                          new=AsyncMock(return_value=[])), \
             patch.object(mm_module.memory_manager, "maybe_extract_and_store",
                          new=AsyncMock(return_value=None)):
            yield
    except Exception:
        # Module not importable in this test context — skip patching
        yield
