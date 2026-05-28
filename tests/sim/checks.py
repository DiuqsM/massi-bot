"""
Massi-Bot Simulator — Check Functions

Two kinds of checks:
  1. Deterministic: inspect BotAction lists and Subscriber state directly.
     Zero cost, always fast.
  2. LLM-judge (optional): ask Haiku a yes/no question about the bot's output.
     ~$0.001 per question. Skip with --no-llm-judge for free runs.
"""

from __future__ import annotations

import os
import sys
import logging
from dataclasses import dataclass
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engine"))

from engine.models import BotAction, Subscriber, SubState

logger = logging.getLogger(__name__)

_haiku_client: Optional[object] = None
_grok_client: Optional[object] = None


def _get_grok_client():
    global _grok_client
    if _grok_client is not None:
        return _grok_client
    try:
        from openai import AsyncOpenAI
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            return None
        _grok_client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            timeout=30.0,
        )
    except Exception:
        pass
    return _grok_client


def _get_haiku_client():
    global _haiku_client
    if _haiku_client is not None:
        return _haiku_client
    try:
        from openai import AsyncOpenAI
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            return None
        _haiku_client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            timeout=30.0,
        )
    except Exception:
        pass
    return _haiku_client


@dataclass
class CheckResult:
    name: str
    passed: bool
    reason: str
    is_llm_judge: bool = False


# ── Helpers ──────────────────────────────────────────────────────────────────

def get_all_text(actions: list[BotAction]) -> str:
    return " ".join(
        a.message for a in actions
        if a.action_type == "send_message" and a.message
    ).lower()


def has_ppv(actions: list[BotAction]) -> bool:
    return any(a.action_type == "send_ppv" for a in actions)


def get_ppv_price(actions: list[BotAction]) -> Optional[float]:
    for a in actions:
        if a.action_type == "send_ppv" and a.ppv_price is not None:
            return a.ppv_price
    return None


def get_ppv_prices(actions: list[BotAction]) -> list[float]:
    return [
        a.ppv_price for a in actions
        if a.action_type == "send_ppv" and a.ppv_price is not None
    ]


# ── Deterministic checks ─────────────────────────────────────────────────────

def check_has_response(actions: list[BotAction], name: str = "has_response") -> CheckResult:
    msgs = [a for a in actions if a.action_type == "send_message" and a.message]
    if msgs:
        return CheckResult(name, True, f"{len(msgs)} message(s) returned")
    return CheckResult(name, False, "no messages returned")


def check_no_ppv(actions: list[BotAction], name: str = "no_ppv") -> CheckResult:
    if not has_ppv(actions):
        return CheckResult(name, True, "no PPV in actions")
    price = get_ppv_price(actions)
    return CheckResult(name, False, f"unexpected PPV found (${price:.2f})")


def check_has_ppv_action(actions: list[BotAction], name: str = "has_ppv") -> CheckResult:
    if has_ppv(actions):
        price = get_ppv_price(actions)
        return CheckResult(name, True, f"PPV action present (${price:.2f})")
    return CheckResult(name, False, "no PPV action in response")


def check_ppv_price(
    actions: list[BotAction],
    expected: float,
    tolerance: float = 0.02,
    name: Optional[str] = None,
) -> CheckResult:
    label = name or f"ppv_price=${expected:.2f}"
    price = get_ppv_price(actions)
    if price is None:
        return CheckResult(label, False, "no PPV action found")
    if abs(price - expected) <= tolerance:
        return CheckResult(label, True, f"PPV price ${price:.2f} ✓")
    return CheckResult(label, False, f"PPV price ${price:.2f} ≠ expected ${expected:.2f}")


def check_text_contains(
    actions: list[BotAction],
    keyword: str,
    name: Optional[str] = None,
) -> CheckResult:
    label = name or f"text_contains:{keyword}"
    text = get_all_text(actions)
    if keyword.lower() in text:
        return CheckResult(label, True, f"found '{keyword}'")
    return CheckResult(label, False, f"'{keyword}' not in: '{text[:120]}'")


def check_text_not_contains(
    actions: list[BotAction],
    keyword: str,
    name: Optional[str] = None,
) -> CheckResult:
    label = name or f"no:{keyword}"
    text = get_all_text(actions)
    if keyword.lower() not in text:
        return CheckResult(label, True, f"'{keyword}' correctly absent")
    return CheckResult(label, False, f"'{keyword}' found — should not appear")


def check_sub_flag(
    sub: Subscriber,
    attr: str,
    expected: bool = True,
    name: Optional[str] = None,
) -> CheckResult:
    label = name or f"sub.{attr}={expected}"
    val = getattr(sub, attr, None)
    if bool(val) == expected:
        return CheckResult(label, True, f"sub.{attr} = {val!r} ✓")
    return CheckResult(label, False, f"sub.{attr} = {val!r}, expected {expected!r}")


def check_sub_state(
    sub: Subscriber,
    expected_state: SubState,
    name: Optional[str] = None,
) -> CheckResult:
    label = name or f"sub.state={expected_state.value}"
    if sub.state.value == expected_state.value:
        return CheckResult(label, True, f"state = {sub.state.value} ✓")
    return CheckResult(label, False, f"state = {sub.state.value!r}, expected {expected_state.value!r}")


def check_ppv_price_sequence(
    all_actions: list[list[BotAction]],
    expected_prices: list[float],
    tolerance: float = 0.02,
    name: str = "ppv_price_sequence",
) -> CheckResult:
    """Check PPV prices across multiple action lists match expected sequence."""
    actual: list[float] = []
    for actions in all_actions:
        actual.extend(get_ppv_prices(actions))
    if len(actual) != len(expected_prices):
        return CheckResult(
            name, False,
            f"got {len(actual)} PPV(s) ({[f'${p:.2f}' for p in actual]}), "
            f"expected {len(expected_prices)} ({[f'${p:.2f}' for p in expected_prices]})",
        )
    mismatches = [
        f"PPV#{i+1}: got ${a:.2f} ≠ ${e:.2f}"
        for i, (a, e) in enumerate(zip(actual, expected_prices))
        if abs(a - e) > tolerance
    ]
    if mismatches:
        return CheckResult(name, False, "; ".join(mismatches))
    return CheckResult(name, True, f"all {len(actual)} prices match ✓")


def check_no_money_mentions(
    actions: list[BotAction],
    name: str = "no_money_mention",
) -> CheckResult:
    """Check bot didn't mention money/pricing/content for sale in these actions."""
    text = get_all_text(actions)
    money_words = ["$", "dollar", "pay", "payment", "ppv", "unlock", "purchase", "content for sale"]
    found = [w for w in money_words if w in text]
    if not found:
        return CheckResult(name, True, "no money mentions ✓")
    return CheckResult(name, False, f"money words found: {found}")


# ── LLM judge ────────────────────────────────────────────────────────────────

async def llm_judge(
    question: str,
    evidence: str,
    name: str,
    skip: bool = False,
) -> CheckResult:
    """
    Ask Haiku a yes/no question about the bot's output.
    Returns CheckResult with passed=True if answer is "yes".

    Pass skip=True to skip without failing (e.g. --no-llm-judge mode).
    """
    if skip:
        return CheckResult(name, True, "(llm-judge skipped)", is_llm_judge=True)

    client = _get_haiku_client()
    if client is None:
        return CheckResult(name, True, "(no API key — judge skipped)", is_llm_judge=True)

    prompt = (
        "You are evaluating a chatbot's response. Answer ONLY 'yes' or 'no'. Nothing else.\n\n"
        f"BOT OUTPUT:\n{evidence}\n\n"
        f"QUESTION: {question}"
    )

    try:
        resp = await client.chat.completions.create(
            model="anthropic/claude-haiku-4.5",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=5,
            temperature=0.0,
        )
        answer = (resp.choices[0].message.content or "").strip().lower()
        passed = answer.startswith("yes")
        return CheckResult(
            name, passed,
            f"judge: '{answer}'",
            is_llm_judge=True,
        )
    except Exception as e:
        logger.warning("LLM judge failed for %r: %s", name, e)
        return CheckResult(name, True, f"(judge error — skipped: {e})", is_llm_judge=True)


async def simulate_fan_turn(
    conversation_history: list[dict],
    persona: str,
    skip: bool = False,
    use_grok: bool = False,
) -> str:
    """
    Use Haiku (or Grok for explicit/offensive scenarios) to generate a realistic fan reply.

    persona: brief character description (e.g. "curious but not ready to spend yet")
    use_grok: use Grok (x-ai/grok-4.3) instead of Haiku — no content restrictions,
              suitable for explicit/offensive fan messages that Haiku refuses to write.
    Returns the fan message string.
    """
    if skip:
        return "yeah okay"

    history_text = "\n".join(
        f"{'Fan' if m['role'] in ('sub', 'user') else 'Bot'}: {m['content']}"
        for m in conversation_history[-6:]
    )

    prompt = (
        f"Character: {persona}\n\n"
        f"Chat so far:\n{history_text}\n\n"
        "Their next text message:"
    )

    if use_grok:
        client = _get_grok_client()
        if client is None:
            return "yeah"
        # Grok has no content restrictions — use explicit system framing so it
        # writes the exact kind of message the scenario needs (explicit requests,
        # rude/dismissive tone, hard-limit asks, etc.) without softening.
        system = (
            "You are a creative writing assistant generating realistic text messages for test scenarios. "
            "Write ONLY the next message from the character described. No labels, no quotes, no commentary. "
            "Keep it short (5–20 words). Stay fully in character — write whatever that character would actually say, "
            "including crude, explicit, rude, or blunt content if that's who they are. "
            "This is fiction for automated testing. Never break character."
        )
        model = "x-ai/grok-4.3"
    else:
        client = _get_haiku_client()
        if client is None:
            return "yeah"
        # Neutral framing — no mention of "adult content" which triggers safety refusals.
        # We only describe the character writing task, not the platform context.
        system = (
            "You are writing a single text message as a fictional character in a chat conversation. "
            "Rules: (1) Output ONLY the message text — no quotes, no labels, no commentary. "
            "(2) Keep it short: 5–15 words is ideal. Real texts are brief. "
            "(3) Vary your style — don't start every message with 'omg', 'honestly', or 'lol'. "
            "(4) Type like a real person on their phone: lowercase is fine, "
            "occasional missing punctuation, natural abbreviations. "
            "(5) Stay in character no matter what the other person says. "
            "(6) Never ask if the other person is a bot, automated, or AI — "
            "just have a normal conversation."
        )
        model = "anthropic/claude-haiku-4.5"

    # Phrases that indicate the model refused to stay in character (only relevant for Haiku).
    _REFUSAL_SIGNALS = (
        "i can't", "i cannot", "i won't", "i'm not going to",
        "i'm not able", "i'll step out", "step back from",
        "regardless of", "any form", "this scenario",
        "i'd be happy to help with something else",
    )

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            max_tokens=60,
            temperature=0.7,
        )
        reply = (resp.choices[0].message.content or "yeah").strip()
        if not use_grok and any(sig in reply.lower() for sig in _REFUSAL_SIGNALS):
            logger.warning("Fan simulator refused (falling back): %r", reply[:80])
            return "haha yeah that's actually really interesting lol"
        return reply
    except Exception as e:
        logger.warning("Fan simulator (%s) failed: %s", model, e)
        return "yeah"
