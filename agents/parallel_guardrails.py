"""
Massi-Bot — Parallel Guardrails

Pattern from Cresta Ocean-1 production architecture: safety classifiers run CONCURRENTLY
with the voice generation, not serially after it. Concurrent execution means each
guardrail adds zero net latency — they all run in the same wall-clock time as the
voice generation itself (we're bottlenecked by the longest call).

Each guardrail is a small, focused check. Most are pure code (microseconds). Some can
optionally call an LLM for harder semantic checks (configurable per guardrail).

The orchestrator awaits all guardrails + voice in parallel via asyncio.gather.
If ANY guardrail rejects, the orchestrator can:
  1. Auto-retry voice generation with a corrective hint
  2. Fall back to a template
  3. Go silent (last resort)

This replaces the SERIAL pattern of generate → uncensor → guardrails → validate
which cost 3-4× the latency and made each step a single point of failure.
"""

import os
import sys
import logging
import asyncio
import re
from typing import Optional, Tuple, List, Dict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'engine'))

from models import Subscriber
from text_filters import (
    run_message_filters,
    run_caption_filters,
    filter_messages_list,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Guardrail result type
# ─────────────────────────────────────────────

class GuardrailResult:
    __slots__ = ("name", "passed", "reason", "fix_hint")
    def __init__(self, name: str, passed: bool, reason: Optional[str] = None, fix_hint: Optional[str] = None):
        self.name = name
        self.passed = passed
        self.reason = reason
        self.fix_hint = fix_hint
    def __repr__(self):
        return f"<GuardrailResult name={self.name} passed={self.passed} reason={self.reason}>"


# ─────────────────────────────────────────────
# Individual guardrails (each is async — even if pure code, for uniform interface)
# ─────────────────────────────────────────────

async def gr_text_filters(messages: List[Dict], caption: Optional[str] = None, allow_price: bool = False) -> GuardrailResult:
    """
    Code-level invariants: em-dash, system terminology, dollar amounts, platform names,
    AI vocabulary, reasoning dump, length. Caption gets the additional content-leak check.

    allow_price: skip filter_dollar_amounts (used when bot must quote a custom order price).
    """
    all_passed, _, reasons = filter_messages_list(messages, allow_price=allow_price)
    if not all_passed:
        return GuardrailResult(
            name="text_filters",
            passed=False,
            reason="; ".join(reasons[:3]),
            fix_hint=f"Your message violates code-level invariants: {'; '.join(reasons)}. Rewrite without these issues.",
        )
    if caption is not None:
        cap_passed, _, cap_reasons = run_caption_filters(caption)
        if not cap_passed:
            return GuardrailResult(
                name="text_filters",
                passed=False,
                reason="caption: " + "; ".join(cap_reasons[:3]),
                fix_hint=f"Your PPV caption leaks content: {'; '.join(cap_reasons)}. Captions must be vague — never reveal body parts, clothing, or actions.",
            )
    return GuardrailResult(name="text_filters", passed=True)


async def gr_tier_boundary(
    messages: List[Dict],
    tiers_purchased: int,
    sext_consent_given: bool,
) -> GuardrailResult:
    """
    Hard boundary check: if pre-consent or tier 0, no explicit language.
    If tier 1-4, no climax language. If tier 1-2, no graphic anatomy.
    """
    if not messages:
        return GuardrailResult(name="tier_boundary", passed=True)

    text_combined = " ".join(m.get("text", "").lower() for m in messages)

    # Pre-consent / tier 0 — no explicit
    if not sext_consent_given or tiers_purchased == 0:
        explicit_words = ["pussy", "cock", "cum", "fuck me", "naked", "nude", "fingering", "stroking my"]
        for w in explicit_words:
            if w in text_combined:
                return GuardrailResult(
                    name="tier_boundary",
                    passed=False,
                    reason=f"explicit word '{w}' before consent / at tier 0",
                    fix_hint="Remove explicit sexual language. Stay flirty/suggestive — fan hasn't given consent yet.",
                )

    # Tier 1-4 — no climax language
    if tiers_purchased < 5:
        climax_words = ["cum", "cumming", "orgasm", "climax", "finish me", "make me cum"]
        for w in climax_words:
            if re.search(rf"\b{re.escape(w)}\b", text_combined):
                return GuardrailResult(
                    name="tier_boundary",
                    passed=False,
                    reason=f"climax word '{w}' at tier {tiers_purchased}",
                    fix_hint=f"Climax language is reserved for tier 5-6. You're at tier {tiers_purchased}. Build heat without pushing him to climax yet.",
                )
    return GuardrailResult(name="tier_boundary", passed=True)


async def gr_no_redrop(
    ppv_intent: Optional[Dict],
    sub: Subscriber,
) -> GuardrailResult:
    """
    Hard rule: never re-drop the same tier while pending_ppv is set.
    If ppv_intent is present but sub.pending_ppv is also set → reject.
    """
    if not ppv_intent:
        return GuardrailResult(name="no_redrop", passed=True)
    pending = getattr(sub, "pending_ppv", None)
    if pending:
        return GuardrailResult(
            name="no_redrop",
            passed=False,
            reason=f"PPV already pending (tier {pending.get('tier')}) — cannot drop new one",
            fix_hint="A PPV is already pending unpaid. Do NOT include a ppv block this turn. Reference the existing PPV softly instead.",
        )
    return GuardrailResult(name="no_redrop", passed=True)


async def gr_persona_voice(
    messages: List[Dict],
    avatar,
) -> GuardrailResult:
    """
    Check for feminine endearments toward male fans + persona-specific blocked words.
    Quick code-level check.
    """
    if not messages or not avatar or not avatar.persona:
        return GuardrailResult(name="persona_voice", passed=True)

    text_combined = " ".join(m.get("text", "").lower() for m in messages)
    avatar_id = getattr(avatar.persona, "ig_account_tag", "")

    # Feminine endearments toward male fans (always blocked except goth_domme exception for "darling")
    feminine_endearments = ["mamas", "mami", "honey", "sweetie", "queen", "hun", "mamacita"]
    if avatar_id != "goth_domme":
        feminine_endearments.append("darling")

    for word in feminine_endearments:
        if re.search(rf"\b{re.escape(word)}\b", text_combined):
            return GuardrailResult(
                name="persona_voice",
                passed=False,
                reason=f"feminine endearment '{word}' toward male fan",
                fix_hint=f"Never use feminine endearments toward male fans. Remove '{word}'.",
            )

    return GuardrailResult(name="persona_voice", passed=True)


async def gr_other_fans_mention(messages: List[Dict]) -> GuardrailResult:
    """Check for mentions of other fans/subscribers/guys (always blocked)."""
    if not messages:
        return GuardrailResult(name="other_fans", passed=True)
    text = " ".join(m.get("text", "").lower() for m in messages)
    bad_phrases = [
        "other fans", "other guys", "other subscribers", "other men",
        "other people on", "my other",
    ]
    for phrase in bad_phrases:
        if phrase in text:
            return GuardrailResult(
                name="other_fans",
                passed=False,
                reason=f"mentioned '{phrase}'",
                fix_hint=f"Never mention other fans. Remove '{phrase}'. It's always just her and him.",
            )
    return GuardrailResult(name="other_fans", passed=True)


_EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001F9FF\U0001FA00-\U0001FAFF\U00002600-\U000027BF\U0001F000-\U0001F02F]"
)


async def gr_emoji_density(messages: List[Dict]) -> GuardrailResult:
    """
    Reject emoji-heavy responses. Most messages should have 0-1 emoji.
    A message with 2+ emojis is rare. Emoji overload is a bot tell.

    Rules:
      - No single message may contain more than 1 emoji
      - Across the response, average emojis per message must be <= 0.75
      - Zero emojis is the DEFAULT and often better
    """
    if not messages:
        return GuardrailResult(name="emoji_density", passed=True)

    total_emojis = 0
    for msg in messages:
        text = msg.get("text", "")
        count = len(_EMOJI_RE.findall(text))
        if count > 1:
            return GuardrailResult(
                name="emoji_density",
                passed=False,
                reason=f"single message has {count} emojis (max 1 allowed)",
                fix_hint=f"Your message has {count} emojis. Use 0 or 1. Most messages should have NONE. Pick the single best one or skip entirely.",
            )
        total_emojis += count

    # Average check only applies when there are 2+ messages.
    # A single message with 1 emoji already passed the per-message limit above (max 1),
    # so applying avg > 0.75 to a 1-message response would incorrectly reject it.
    if len(messages) > 1:
        avg = total_emojis / len(messages)
        if avg > 0.75:
            return GuardrailResult(
                name="emoji_density",
                passed=False,
                reason=f"avg {avg:.1f} emojis/msg across {len(messages)} messages (max 0.75)",
                fix_hint="Too many emojis. DEFAULT is no emoji. Use one ONLY when emotion genuinely demands it.",
            )
    return GuardrailResult(name="emoji_density", passed=True)


async def gr_passive_at_high_tier(
    messages: List[Dict],
    tiers_purchased: int,
    sext_consent_given: bool,
    register: str = "",
) -> GuardrailResult:
    """
    At sexting tiers (1+ with consent given), reject responses that ask the fan
    what HE wants instead of telling him what to do. SHE leads the scene.
    """
    if not messages or not sext_consent_given:
        return GuardrailResult(name="passive_at_high_tier", passed=True)
    # Only apply to sexting registers
    sexting_registers = (
        "vulnerable", "playful", "commanding", "raw_desire",
        "casual_confidence", "ppv_lead_in", "post_purchase_reaction",
    )
    if register and register not in sexting_registers:
        return GuardrailResult(name="passive_at_high_tier", passed=True)

    text = " ".join(m.get("text", "").lower() for m in messages)

    # Patterns that indicate she's asking him to lead instead of leading him
    passive_patterns = [
        r"what do you want",
        r"what'?s the vibe",
        r"where do you want",
        r"where to (take|go) (this|next)",
        r"tell me what you want",
        r"let me know what",
        r"your call",
        r"up to you",
        r"whatever you want",
        r"(do|want) you (want|prefer)",
        r"how should we",
        r"how do you want",
    ]
    for pattern in passive_patterns:
        if re.search(pattern, text):
            return GuardrailResult(
                name="passive_at_high_tier",
                passed=False,
                reason=f"asked fan to lead: '{pattern}'",
                fix_hint="You're asking the fan what HE wants. SHE leads. Replace the question with a COMMAND telling him what to do (stroke, edge, imagine, etc.) or a statement narrating the scene.",
            )
    return GuardrailResult(name="passive_at_high_tier", passed=True)


async def gr_fake_exclusivity(messages: List[Dict]) -> GuardrailResult:
    """Check for fake exclusivity claims."""
    if not messages:
        return GuardrailResult(name="fake_exclusivity", passed=True)
    text = " ".join(m.get("text", "").lower() for m in messages)
    bad_phrases = [
        "i've never sent this", "i've never done this", "you're the first to see",
        "i've never shown anyone", "no one else has seen",
    ]
    for phrase in bad_phrases:
        if phrase in text:
            return GuardrailResult(
                name="fake_exclusivity",
                passed=False,
                reason=f"fake exclusivity: '{phrase}'",
                fix_hint=f"Never claim fake exclusivity. He knows you're a content creator. Remove '{phrase}'.",
            )
    return GuardrailResult(name="fake_exclusivity", passed=True)


# ─────────────────────────────────────────────
# Main parallel runner
# ─────────────────────────────────────────────

async def run_all_guardrails(
    messages: List[Dict],
    ppv_intent: Optional[Dict],
    sub: Subscriber,
    avatar,
    tiers_purchased: int,
    sext_consent_given: bool,
    register: str = "",
) -> Tuple[bool, List[GuardrailResult]]:
    """
    Run all guardrails CONCURRENTLY via asyncio.gather. Returns (all_passed, results).

    Caller decides what to do with failures (regenerate, fall back, go silent).
    """
    _ppv_tier = str((ppv_intent or {}).get("tier", "")).lower()
    # Custom PPV captions reference the requested content by design — skip the leak check.
    caption = (ppv_intent or {}).get("caption") if (ppv_intent and _ppv_tier != "custom") else None
    # Allow dollar amounts in the message when a custom order is being pitched
    # (classify_custom_request tool was called this turn, so the bot must state the price)
    allow_price = bool(getattr(sub, "pending_custom_order", None))

    tasks = [
        gr_text_filters(messages, caption=caption, allow_price=allow_price),
        gr_tier_boundary(messages, tiers_purchased, sext_consent_given),
        gr_no_redrop(ppv_intent, sub),
        gr_persona_voice(messages, avatar),
        gr_other_fans_mention(messages),
        gr_fake_exclusivity(messages),
        gr_passive_at_high_tier(messages, tiers_purchased, sext_consent_given, register=register),
        gr_emoji_density(messages),
    ]

    results: List[GuardrailResult] = await asyncio.gather(*tasks, return_exceptions=False)

    failures = [r for r in results if not r.passed]
    if failures:
        logger.warning(
            "Guardrails: %d/%d FAILED — %s",
            len(failures), len(results),
            "; ".join(f"{r.name}({r.reason})" for r in failures),
        )
    return (len(failures) == 0, results)


def build_corrective_hint(failures: List[GuardrailResult]) -> str:
    """Build a concise corrective hint string for Voice Stylist regeneration."""
    if not failures:
        return ""
    hints = [f.fix_hint for f in failures if f.fix_hint]
    return "REGENERATION NEEDED — your previous output had these issues:\n" + "\n".join(f"  - {h}" for h in hints)
