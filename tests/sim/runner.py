"""
Massi-Bot Conversation Simulator — Runner

Makes real Opus 4.7 calls to verify the bot behaves correctly across
26 predefined scenarios. Use this after code changes to catch regressions
before they hit live subscribers.

──────────────────────────────────────────────────────────────────────────────
COST ESTIMATE
──────────────────────────────────────────────────────────────────────────────

  Each automated scenario calls Opus 4.7 via OpenRouter (1–6 calls per scenario).
  Prompt caching helps significantly — the large system prompt is usually cached.

  Per scenario:
    Worst case (6 Opus calls, uncached):   ~$0.60–$0.90
    Typical (2–3 Opus calls, cached input): ~$0.10–$0.25
    Best case (1 call, cached):             ~$0.04–$0.08

  Full run (26 scenarios, 19 automated):
    Conservative estimate:  ~$3–6
    Typical run:            ~$2–4
    With --no-llm-judge:    same (Haiku judge calls are ~$0.001 each, negligible)

  High-cost scenarios:
    S2.4 (12 Opus turns):  ~$0.50–$1.20
    S3.2 (6 purchases):    ~$0.30–$0.60

  To minimize cost:
    - Run a single scenario:  python -m tests.sim.runner --scenario S1.1
    - Run a group:            python -m tests.sim.runner --group G1
    - Skip LLM judge:         python -m tests.sim.runner --no-llm-judge
    - Dry run (list only):    python -m tests.sim.runner --dry-run

──────────────────────────────────────────────────────────────────────────────
USAGE
──────────────────────────────────────────────────────────────────────────────

  cd ~/massi-bot

  # Full automated suite
  python -m tests.sim.runner

  # Single scenario
  python -m tests.sim.runner --scenario S3.1

  # Entire group
  python -m tests.sim.runner --group G4

  # Skip LLM judge (deterministic checks only, much cheaper)
  python -m tests.sim.runner --no-llm-judge

  # List all scenarios without running
  python -m tests.sim.runner --dry-run

  # Verbose mode (show bot outputs)
  python -m tests.sim.runner --verbose

  Requires: OPENROUTER_API_KEY set in environment or .env
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engine"))


# ── ANSI colours ─────────────────────────────────────────────────────────────

_NO_COLOR = not sys.stdout.isatty()

def _green(s: str) -> str:
    return s if _NO_COLOR else f"\033[32m{s}\033[0m"

def _red(s: str) -> str:
    return s if _NO_COLOR else f"\033[31m{s}\033[0m"

def _yellow(s: str) -> str:
    return s if _NO_COLOR else f"\033[33m{s}\033[0m"

def _bold(s: str) -> str:
    return s if _NO_COLOR else f"\033[1m{s}\033[0m"

def _dim(s: str) -> str:
    return s if _NO_COLOR else f"\033[2m{s}\033[0m"


# ── Load .env ─────────────────────────────────────────────────────────────────

def _load_env():
    env_path = os.path.join(os.path.dirname(__file__), "..", "..", ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v


# ── Printing ──────────────────────────────────────────────────────────────────

def _print_result(result, verbose: bool = False):
    from tests.sim.scenarios import ScenarioResult

    status = _green("PASS") if result.passed else _red("FAIL")
    elapsed = f"{result.elapsed_seconds:.1f}s"

    if result.error:
        print(f"  {_red('ERROR')} [{result.id}] {result.name} ({elapsed})")
        print(f"         {_red(result.error)}")
        return

    print(f"  {status} [{result.id}] {result.name} ({elapsed})")

    for check in result.checks:
        icon = _green("✓") if check.passed else _red("✗")
        judge_tag = _dim(" [llm-judge]") if check.is_llm_judge else ""
        print(f"        {icon} {check.name}: {_dim(check.reason)}{judge_tag}")

    if verbose and result.bot_outputs:
        print(f"       {_dim('Bot said:')}")
        for text in result.bot_outputs[:3]:
            short = text[:120].replace("\n", " ")
            print(f"         {_dim(repr(short))}")
        if len(result.bot_outputs) > 3:
            print(f"         {_dim(f'... and {len(result.bot_outputs) - 3} more')}")


def _print_manual(scenario):
    print(f"  {_yellow('MANUAL')} [{scenario.id}] {scenario.name}")
    if scenario.manual_instructions:
        for line in scenario.manual_instructions.strip().split("\n"):
            print(f"         {_dim(line)}")


def _print_summary(results: list, total_time: float, manual_skipped: int):
    from tests.sim.scenarios import ScenarioResult

    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed
    errors = sum(1 for r in results if r.error)

    print()
    print(_bold("═" * 60))
    print(_bold("SUMMARY"))
    print(_bold("═" * 60))
    print(f"  Scenarios run:    {total}")
    print(f"  Passed:           {_green(str(passed))}")
    if failed:
        print(f"  Failed:           {_red(str(failed))}")
    if errors:
        print(f"  Errors:           {_red(str(errors))}")
    if manual_skipped:
        print(f"  Manual (skipped): {_yellow(str(manual_skipped))}")
    print(f"  Total time:       {total_time:.1f}s")

    if failed:
        print()
        print(_bold("FAILURES:"))
        for r in results:
            if not r.passed and not r.error:
                for c in r.checks:
                    if not c.passed:
                        print(f"  {_red('✗')} [{r.id}] {c.name}: {c.reason}")
    print()


def _print_dry_run(scenarios):
    print(_bold("Scenarios (dry run — not executing):"))
    print()
    current_group = None
    for s in scenarios:
        if s.group != current_group:
            current_group = s.group
            group_names = {
                "G1": "Group 1 — New Subscriber Flow",
                "G2": "Group 2 — Rapport & Consent Gate",
                "G3": "Group 3 — PPV Ladder",
                "G4": "Group 4 — Objection Handling",
                "G5": "Group 5 — Custom Order Flow",
                "G6": "Group 6 — Edge Cases & Guardrails",
                "G7": "Group 7 — GFE & Returning Fan",
                "G8": "Group 8 — Admin Bot",
                "G9": "Group 9 — Live Fan Conversations",
            }
            print(f"  {_bold(group_names.get(s.group, s.group))}")
        tag = _yellow("MANUAL") if s.is_manual else _dim("auto  ")
        print(f"    [{tag}] {s.id}  {s.name}")
    print()


# ── Log writer ───────────────────────────────────────────────────────────────

def _format_dm_transcript(conversation: list[dict]) -> list[str]:
    """
    Render a conversation as a DM-style thread.

    Each line is prefixed with a relative timestamp (+Xs) that accumulates as
    the bot's response latency elapses.  Fan messages are right-aligned with
    '>>' and bot/ppv messages are left-aligned with '<<'.  Events appear as
    centred separator lines.

    Example output:

        +0.0s   Fan  >>  hey how much is that video again
                         ── regen: second message arrived mid-processing ──
        +0.0s   Fan  >>  actually nvm the price, just tell me more
        +1.3s   Bot  <<  oh honestly it's more about the vibe lol, not just a
                         price tag. what kind of stuff are you into?
                         (1340ms to respond)
    """
    _WRAP = 58          # chars before wrapping message text
    _TS_W = 6           # "+12.3s"
    _ROLE_W = 7         # "Fan    " / "Bot    " / "PPV    "
    _ARROW_W = 4        # " >> " / " << " / "    "
    _INDENT = " " * (_TS_W + 1 + _ROLE_W + _ARROW_W)

    lines: list[str] = []
    t = 0.0   # accumulated seconds elapsed since scenario start

    for turn in conversation:
        role = turn["role"]
        text = turn["text"]
        ms = turn.get("ms")

        # ── Event / system line ───────────────────────────────────────────────
        if role == "event":
            sep = f"── {text} ──"
            lines.append(_INDENT + sep)
            continue

        # ── Compute display timestamp ─────────────────────────────────────────
        ts = f"+{t:.1f}s".ljust(_TS_W)

        # ── Role label and arrow ──────────────────────────────────────────────
        if role == "fan":
            label = "Fan".ljust(_ROLE_W)
            arrow = " >> "
        elif role == "bot":
            label = "Bot".ljust(_ROLE_W)
            arrow = " << "
        elif role == "ppv":
            label = "PPV".ljust(_ROLE_W)
            arrow = " << "
        else:
            label = role[:_ROLE_W].ljust(_ROLE_W)
            arrow = "    "

        # ── Wrap long message text ────────────────────────────────────────────
        words = text.split()
        wrapped: list[str] = []
        current = ""
        for word in words:
            if current and len(current) + 1 + len(word) > _WRAP:
                wrapped.append(current)
                current = word
            else:
                current = (current + " " + word).strip()
        if current:
            wrapped.append(current)

        # First line
        lines.append(f"{ts} {label}{arrow}{wrapped[0] if wrapped else ''}")
        # Continuation lines (no ts/role prefix)
        for chunk in wrapped[1:]:
            lines.append(f"{_INDENT}{chunk}")

        # Latency annotation for bot/ppv turns
        if role in ("bot", "ppv") and ms is not None:
            lines.append(f"{_INDENT}({ms:,}ms to respond)")
            t += ms / 1000   # advance clock after bot speaks

    return lines


def _update_index(out_dir: Path, run_at: datetime, results: list, total_time: float) -> None:
    """Append a row for this run to docs/test_results/INDEX.md."""
    import json as _json
    index_path = out_dir / "INDEX.md"
    date_str = run_at.strftime("%Y-%m-%d")
    time_str = run_at.strftime("%H:%M:%S")
    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed
    scenario_ids = ", ".join(r.id for r in results)
    fail_mark = f"{failed} ❌" if failed else "0 ✅"
    new_row = f"| {len(results)} | {time_str} | {scenario_ids} | {passed} | {fail_mark} | {int(total_time)}s |"

    if index_path.exists():
        content = index_path.read_text(encoding="utf-8")
    else:
        content = "# Sim Test Results Index\n\nOrganized by date. Each row = one attempt.\n"

    date_header = f"## {date_str}"
    sim_header = "### Sim runs"
    table_header = "| # | Time (UTC) | Scenario(s) | Passed | Failed | Duration |"
    table_sep =    "|---|-----------|-------------|--------|--------|----------|"

    if date_header not in content:
        content = content.rstrip() + f"\n\n{date_header}\n\n{sim_header}\n\n{table_header}\n{table_sep}\n{new_row}\n"
    elif sim_header not in content[content.index(date_header):]:
        insert_at = content.index(date_header) + len(date_header)
        content = content[:insert_at] + f"\n\n{sim_header}\n\n{table_header}\n{table_sep}\n{new_row}" + content[insert_at:]
    else:
        # Find the last row in this date's sim table and append after it
        sim_section_start = content.index(date_header)
        sim_table_start = content.index(table_sep, sim_section_start) + len(table_sep)
        # Find the end of the table (blank line or next ##)
        after_table = content[sim_table_start:]
        rows = []
        for line in after_table.splitlines():
            if line.startswith("|"):
                rows.append(line)
            else:
                break
        row_num = len(rows) + 1
        new_row = f"| {row_num} | {time_str} | {scenario_ids} | {passed} | {fail_mark} | {int(total_time)}s |"
        insert_pos = sim_table_start + sum(len(r) + 1 for r in rows)
        content = content[:insert_pos] + new_row + "\n" + content[insert_pos:]

    index_path.write_text(content, encoding="utf-8")


def _write_logs(
    results: list,
    total_time: float,
    run_at: datetime,
    out_dir: Path,
    skip_judge: bool,
) -> tuple[Path, Path]:
    """Write human-readable .txt and machine-readable .json to a YYYY-MM-DD subdir."""
    date_dir = out_dir / run_at.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    stamp = run_at.strftime("%Y%m%d_%H%M%S")
    txt_path = date_dir / f"sim_{stamp}.txt"
    json_path = date_dir / f"sim_{stamp}.json"

    passed = sum(1 for r in results if r.passed)
    failed = len(results) - passed

    # ── Human-readable ────────────────────────────────────────────────────────
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("Massi-Bot Simulator Run")
    lines.append(f"  Date/time : {run_at.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(f"  LLM judge : {'disabled' if skip_judge else 'enabled'}")
    lines.append(f"  Scenarios : {len(results)}")
    lines.append(f"  Passed    : {passed}")
    lines.append(f"  Failed    : {failed}")
    lines.append(f"  Duration  : {total_time:.1f}s")
    lines.append("=" * 70)
    lines.append("")

    _GROUP_NAMES = {
        "G1": "Group 1 — New Subscriber Flow",
        "G2": "Group 2 — Rapport & Consent Gate",
        "G3": "Group 3 — PPV Ladder",
        "G4": "Group 4 — Objection Handling",
        "G5": "Group 5 — Custom Order Flow",
        "G6": "Group 6 — Edge Cases & Guardrails",
        "G7": "Group 7 — GFE & Returning Fan",
        "G8": "Group 8 — Admin Bot",
        "G9": "Group 9 — Live Fan Conversations",
    }

    current_group = None
    for r in results:
        if r.group != current_group:
            current_group = r.group
            lines.append(f"── {_GROUP_NAMES.get(r.group, r.group)} ──")
            lines.append("")

        status = "PASS" if r.passed else "FAIL"
        avg_ms = f"  avg {r.avg_turn_ms}ms/turn" if r.avg_turn_ms is not None else ""
        lines.append(f"[{status}] {r.id} — {r.name}  ({r.elapsed_seconds:.1f}s{avg_ms})")

        if r.error:
            lines.append(f"  ERROR: {r.error}")
        else:
            for c in r.checks:
                icon = "✓" if c.passed else "✗"
                judge = " [llm-judge]" if c.is_llm_judge else ""
                lines.append(f"  {icon} {c.name}: {c.reason}{judge}")

        # DM-style conversation transcript
        if r.conversation:
            lines.append("")
            lines.append("  Chat transcript:")
            lines.append("  " + "-" * 68)
            for dm_line in _format_dm_transcript(r.conversation):
                lines.append("  " + dm_line)
            lines.append("  " + "-" * 68)

        lines.append("")

    lines.append("=" * 70)
    lines.append("")
    txt_path.write_text("\n".join(lines), encoding="utf-8")

    # ── JSON ─────────────────────────────────────────────────────────────────
    json_data = {
        "run_at": run_at.isoformat(),
        "llm_judge": not skip_judge,
        "summary": {
            "total": len(results),
            "passed": passed,
            "failed": failed,
            "elapsed_seconds": round(total_time, 2),
        },
        "scenarios": [
            {
                "id": r.id,
                "name": r.name,
                "group": r.group,
                "passed": r.passed,
                "elapsed_seconds": round(r.elapsed_seconds, 2),
                "avg_turn_ms": r.avg_turn_ms,
                "per_turn_ms": r.per_turn_ms,
                "error": r.error,
                "checks": [
                    {
                        "name": c.name,
                        "passed": c.passed,
                        "reason": c.reason,
                        "llm_judge": c.is_llm_judge,
                    }
                    for c in r.checks
                ],
                "conversation": r.conversation,
            }
            for r in results
        ],
    }
    json_path.write_text(json.dumps(json_data, indent=2, ensure_ascii=False), encoding="utf-8")

    return txt_path, json_path


# ── Main ──────────────────────────────────────────────────────────────────────

async def _run_all(
    scenarios,
    skip_judge: bool,
    skip_fan: bool,
    verbose: bool,
    fail_fast: bool = False,
) -> tuple[list, int]:
    from tests.sim.scenarios import sim_context

    results = []
    manual_skipped = 0
    current_group = None
    auto_scenarios = [s for s in scenarios if not s.is_manual]
    total_auto = len(auto_scenarios)
    completed = 0

    async with sim_context():
        for scenario in scenarios:
            if scenario.group != current_group:
                current_group = scenario.group
                group_names = {
                    "G1": "Group 1 — New Subscriber Flow",
                    "G2": "Group 2 — Rapport & Consent Gate",
                    "G3": "Group 3 — PPV Ladder",
                    "G4": "Group 4 — Objection Handling",
                    "G5": "Group 5 — Custom Order Flow",
                    "G6": "Group 6 — Edge Cases & Guardrails",
                    "G7": "Group 7 — GFE & Returning Fan",
                    "G8": "Group 8 — Admin Bot",
                    "G9": "Group 9 — Live Fan Conversations",
                }
                print()
                print(_bold(f"── {group_names.get(scenario.group, scenario.group)} ──"))

            if scenario.is_manual:
                _print_manual(scenario)
                manual_skipped += 1
                continue

            completed += 1
            print(f"  {_dim(f'[{completed}/{total_auto}]')} running {scenario.id} — {scenario.name} …", end="", flush=True)
            print()

            import inspect
            sig = inspect.signature(scenario.run)
            kwargs = {}
            if "skip_judge" in sig.parameters:
                kwargs["skip_judge"] = skip_judge
            if "skip_fan" in sig.parameters:
                kwargs["skip_fan"] = skip_fan
            result = await scenario.run(**kwargs)

            results.append(result)
            _print_result(result, verbose=verbose)

            if fail_fast and not result.passed:
                print()
                print(_red(f"✗ Stopping after first failure: [{result.id}] {result.name}"))
                break

    return results, manual_skipped


def main():
    _load_env()

    parser = argparse.ArgumentParser(
        description="Massi-Bot Conversation Simulator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--scenario", "-s",
        metavar="ID",
        help="Run a single scenario by ID (e.g. S1.1, S3.2)",
    )
    parser.add_argument(
        "--group", "-g",
        metavar="GROUP",
        help="Run all scenarios in a group (e.g. G1, G4)",
    )
    parser.add_argument(
        "--no-llm-judge",
        action="store_true",
        help="Skip LLM-judge quality checks (deterministic checks only)",
    )
    parser.add_argument(
        "--no-live-fan",
        action="store_true",
        help="Replace Haiku fan simulator with 'yeah okay' (keeps LLM judge, cuts live scenario cost ~50%%)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show bot output text for each scenario",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List scenarios without running them",
    )
    parser.add_argument(
        "--list-manual",
        action="store_true",
        help="Print manual test instructions and exit",
    )
    parser.add_argument(
        "--log-dir",
        metavar="DIR",
        default=None,
        help="Directory for log files (default: docs/test_results)",
    )
    parser.add_argument(
        "--no-log",
        action="store_true",
        help="Skip writing log files",
    )
    parser.add_argument(
        "--live-only",
        action="store_true",
        help="Run only live-fan scenarios (IDs ending in L, plus G9)",
    )
    parser.add_argument(
        "--scripted-only",
        action="store_true",
        help="Run only scripted scenarios (no L suffix, no G9)",
    )
    parser.add_argument(
        "--fail-fast", "-x",
        action="store_true",
        help="Stop after the first failing scenario",
    )
    args = parser.parse_args()

    from tests.sim.scenarios import ALL_SCENARIOS

    # Filter
    scenarios = ALL_SCENARIOS
    if args.scenario:
        scenarios = [s for s in scenarios if s.id.upper() == args.scenario.upper()]
        if not scenarios:
            print(_red(f"No scenario found with ID '{args.scenario}'"))
            sys.exit(1)
    elif args.group:
        scenarios = [s for s in scenarios if s.group.upper() == args.group.upper()]
        if not scenarios:
            print(_red(f"No scenarios found in group '{args.group}'"))
            sys.exit(1)

    if args.live_only:
        scenarios = [s for s in scenarios if s.id.endswith("L") or s.group == "G9"]
    elif args.scripted_only:
        scenarios = [s for s in scenarios if not s.id.endswith("L") and s.group != "G9"]

    if args.dry_run:
        _print_dry_run(scenarios)
        return

    if args.list_manual:
        for s in scenarios:
            if s.is_manual and s.manual_instructions:
                print(_bold(f"[{s.id}] {s.name}"))
                print(s.manual_instructions.strip())
                print()
        return

    # Check API key
    if not os.environ.get("OPENROUTER_API_KEY"):
        print(_red("ERROR: OPENROUTER_API_KEY not set. Check your .env file."))
        sys.exit(1)

    auto_count = sum(1 for s in scenarios if not s.is_manual)
    manual_count = sum(1 for s in scenarios if s.is_manual)

    flags = []
    if args.no_llm_judge:
        flags.append("no llm-judge")
    if args.no_live_fan:
        flags.append("no live fan")
    if args.fail_fast:
        flags.append("fail-fast")
    flag_str = f"  [{', '.join(flags)}]" if flags else ""

    print(_bold("Massi-Bot Conversation Simulator"))
    print(f"Running {auto_count} automated scenario(s)"
          + (f", {manual_count} manual (skipped)" if manual_count else "")
          + flag_str)
    print()

    run_at = datetime.now(timezone.utc)
    t0 = time.time()
    results, manual_skipped = asyncio.run(
        _run_all(scenarios, skip_judge=args.no_llm_judge,
                 skip_fan=args.no_live_fan, verbose=args.verbose,
                 fail_fast=args.fail_fast)
    )
    total_time = time.time() - t0

    _print_summary(results, total_time, manual_skipped)

    if results and not args.no_log:
        log_dir = Path(args.log_dir) if args.log_dir else (
            Path(__file__).parent.parent.parent / "docs" / "test_results"
        )
        txt_path, json_path = _write_logs(
            results, total_time, run_at, log_dir, skip_judge=args.no_llm_judge
        )
        _update_index(log_dir, run_at, results, total_time)
        print(f"  Logs written:")
        print(f"    {txt_path}")
        print(f"    {json_path}")
        print()

    # Exit with error code if any scenario failed
    if any(not r.passed for r in results):
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    main()
