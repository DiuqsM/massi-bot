"""
Massi-Bot — Code-Level Text Invariant Enforcement

Post-generation filters that run AFTER the LLM produces output but BEFORE the message
reaches the fan. These enforce invariants that prompt-level rules have repeatedly failed
to enforce across thousands of LLM calls. The pattern is taken from production systems
like Cresta, Anthropic's verification subagents, and OpenAI's API-level enforcement.

Architectural principle (from the 2026-04-15 architecture research):
  > Move structural decisions out of LLM authority entirely. The LLM cannot violate
  > what code rejects. This is what every production system at scale does.

Each filter is:
  - PURE FUNCTION (no side effects, no I/O, no async)
  - FAST (microseconds, not milliseconds)
  - DETERMINISTIC (same input → same output)
  - COMPOSABLE (chainable in any order)
  - LOGGED (every rejection emits a structured log entry)

Filters return a tuple: (passed: bool, fixed_text: str, reason: Optional[str])
  - passed=True, fixed_text=cleaned input, reason=None  → continue
  - passed=False, fixed_text=cleaned input, reason="why" → log + may regenerate
"""

import re
import logging
from typing import Optional, Tuple, List, Dict

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Invariant patterns
# ─────────────────────────────────────────────

# Em-dash and en-dash — never in bot output (per feedback_em_dash.md)
_DASH_PATTERN = re.compile(r"[—–]")

# System terminology that must NEVER appear in user-facing text
_SYSTEM_TERMS = [
    r"\btier\s*[0-6]\b",           # "tier 1", "tier 5", etc.
    r"\bsession\s*\d+\b",          # "session 1"
    r"\bppv\b",                    # the literal acronym
    r"\bpipeline\b",
    r"\bstrategist\b",
    r"\brecommendation\b",
    r"\bemotion\s*analyzer\b",
    r"\bvalidator\b",
    r"\bdirector\b",
    r"\borchestrator\b",
    r"\bguardrail\b",
    r"\bagent\b",                  # "the agent recommends..."
    r"\bllm\b",
    r"\bgpt\b",
    r"\bclaude\b",
    r"\bopus\b",
    r"\bsonnet\b",
    r"\bhaiku\b",
    r"\banthropic\b",
    r"\bopenai\b",
    r"\bsubscriber\b",             # "the subscriber"
    r"\bbuy.?readiness\b",
    r"\bengagement\s*level\b",
    r"\bmoment.summary\b",
    r"\bconsent.given\b",
    r"\bconsent.declined\b",
]
_SYSTEM_TERMS_RE = re.compile(r"|".join(_SYSTEM_TERMS), flags=re.IGNORECASE)

# Reasoning leak markers — sentence starts that indicate chain-of-thought dump
_REASONING_MARKERS = [
    r"^let me\s",
    r"^let's\s",
    r"^looking at\s",
    r"^thinking through\s",
    r"^analyzing\s",
    r"^based on\s",
    r"^given (the|that|his)\s",
    r"^considering\s",
    r"^step\s*\d+\s*[:.]",
    r"^\*\*",                      # bold-marker reasoning ("**Key insight:**")
    r"^#{1,6}\s",                  # markdown header (# through ######)
    r"^okay so let me\s",
    r"^so first\s",
    r"^the key issue\b",
    r"^my reasoning\b",
    r"^my thought\b",
]
_REASONING_RE = re.compile(r"|".join(_REASONING_MARKERS), flags=re.IGNORECASE | re.MULTILINE)

# AI vocabulary that breaks the persona illusion (per existing rules)
_AI_VOCABULARY = [
    r"\bdelve\b",
    r"\bnuanced\b",
    r"\bfacilitate\b",
    r"\bcomprehensive\b",
    r"\bmoreover\b",
    r"\bfurthermore\b",
    r"\badditionally\b",
    r"\bcertainly\b",
    r"\bindeed\b",
    r"\butilize\b",
    r"\bleverage\b",
    r"\bmyriad\b",
    r"\btapestry\b",
    r"\brealm\b",
    r"\bnavigate\b",
]
_AI_VOCAB_RE = re.compile(r"|".join(_AI_VOCABULARY), flags=re.IGNORECASE)

# PPV caption content-description leak detectors
# Captions are flagged if they describe what's IN the bundle (body parts, clothing, actions)
_CAPTION_BODY_PARTS = [
    "tits", "boobs", "breasts", "ass", "butt", "pussy", "vagina", "nipples",
    "thighs", "legs", "stomach", "abs", "back", "feet",
]
_CAPTION_CLOTHING = [
    "lingerie", "bra", "panties", "thong", "underwear", "naked", "nude", "topless",
    "bottomless", "shirt off", "pants off", "skirt", "dress", "swimsuit", "bikini",
    "yoga pants", "leggings",
]
_CAPTION_ACTIONS = [
    "stripping", "undressing", "spreading", "fingering", "touching myself",
    "rubbing", "playing with", "grinding", "riding", "sucking",
]


# ─────────────────────────────────────────────
# Individual filters
# ─────────────────────────────────────────────

def filter_em_dash(text: str) -> Tuple[bool, str, Optional[str]]:
    """Replace em-dashes/en-dashes with spaced periods. Always passes (auto-fix)."""
    if _DASH_PATTERN.search(text):
        cleaned = _DASH_PATTERN.sub("...", text)
        logger.debug("filter_em_dash: replaced em/en dash")
        return True, cleaned, None
    return True, text, None


def filter_system_terminology(text: str) -> Tuple[bool, str, Optional[str]]:
    """
    Reject if the response contains system terminology that breaks persona illusion.
    Triggers regeneration.
    """
    match = _SYSTEM_TERMS_RE.search(text)
    if match:
        reason = f"system terminology leak: '{match.group(0)}'"
        logger.warning("filter_system_terminology: %s in text=%r", reason, text[:120])
        return False, text, reason
    return True, text, None


def filter_reasoning_dump(text: str) -> Tuple[bool, str, Optional[str]]:
    """
    Reject responses that start with chain-of-thought / reasoning markers.
    Common when the LLM dumps its reasoning into the output instead of emitting JSON.
    """
    if not text or not text.strip():
        return True, text, None
    stripped = text.strip()
    if _REASONING_RE.match(stripped):
        reason = "reasoning dump detected"
        logger.warning("filter_reasoning_dump: rejecting text starting with reasoning marker: %r",
                       stripped[:120])
        return False, text, reason
    # Also reject if text starts with { (someone forgot to extract JSON)
    if stripped.startswith("{") and stripped.endswith("}"):
        reason = "raw JSON in user-facing text"
        logger.warning("filter_reasoning_dump: raw JSON: %r", stripped[:120])
        return False, text, reason
    return True, text, None


def filter_ai_vocabulary(text: str) -> Tuple[bool, str, Optional[str]]:
    """Reject AI-vocabulary words. These break the persona illusion."""
    match = _AI_VOCAB_RE.search(text)
    if match:
        reason = f"AI vocabulary: '{match.group(0)}'"
        logger.warning("filter_ai_vocabulary: %s", reason)
        return False, text, reason
    return True, text, None


def filter_caption_content_leak(caption: str) -> Tuple[bool, str, Optional[str]]:
    """
    PPV caption content-description leak check. Captions must be vague — never reveal
    body parts, clothing states, or sexual actions. The whole point of mystery is that
    the fan pays to find out.
    """
    if not caption:
        return True, caption, None
    cap_lower = caption.lower()
    for term in _CAPTION_BODY_PARTS + _CAPTION_CLOTHING + _CAPTION_ACTIONS:
        if term in cap_lower:
            reason = f"caption content leak: '{term}'"
            logger.warning("filter_caption_content_leak: %s in caption=%r", reason, caption)
            return False, caption, reason
    return True, caption, None


def filter_length(text: str, max_chars: int = 600) -> Tuple[bool, str, Optional[str]]:
    """Reject responses that are too long (likely a reasoning dump or runaway generation)."""
    if not text:
        return True, text, None
    if len(text) > max_chars:
        reason = f"response too long ({len(text)} chars > {max_chars})"
        logger.warning("filter_length: %s", reason)
        return False, text, reason
    return True, text, None


def filter_dollar_amounts(text: str) -> Tuple[bool, str, Optional[str]]:
    """
    Reject if the response contains specific dollar amounts. Bot must NEVER state prices.
    The platform handles pricing display.
    """
    # Match $X, $X.XX, X dollars, X.XX dollars
    if re.search(r"\$\d+|\b\d+\s*dollars?\b|\b\d+\s*bucks?\b", text, flags=re.IGNORECASE):
        match = re.search(r"\$\d+(?:\.\d{2})?|\b\d+\s*(?:dollars?|bucks?)\b", text, flags=re.IGNORECASE)
        reason = f"dollar amount leak: '{match.group(0) if match else '?'}'"
        logger.warning("filter_dollar_amounts: %s", reason)
        return False, text, reason
    return True, text, None


def filter_platform_names(text: str) -> Tuple[bool, str, Optional[str]]:
    """
    Reject platform name mentions: OnlyFans, Fanvue, Instagram, Twitter, X.
    She doesn't acknowledge the platform exists.
    """
    if re.search(r"\b(onlyfans|fanvue|instagram|twitter|tiktok|snapchat)\b", text, flags=re.IGNORECASE):
        match = re.search(r"\b(onlyfans|fanvue|instagram|twitter|tiktok|snapchat)\b", text, flags=re.IGNORECASE)
        reason = f"platform name leak: '{match.group(0) if match else '?'}'"
        logger.warning("filter_platform_names: %s", reason)
        return False, text, reason
    return True, text, None


# ─────────────────────────────────────────────
# Pipeline orchestration
# ─────────────────────────────────────────────

# Filters that auto-fix (always pass after fixing)
_AUTO_FIX_FILTERS = [
    filter_em_dash,
]

# Filters that reject and trigger regeneration
_REJECT_FILTERS = [
    filter_reasoning_dump,
    filter_system_terminology,
    filter_dollar_amounts,
    filter_platform_names,
    filter_ai_vocabulary,
    filter_length,
]


def run_message_filters(text: str, allow_price: bool = False) -> Tuple[bool, str, List[str]]:
    """
    Run all message-level filters on a user-facing text.

    Returns:
      (passed, cleaned_text, rejection_reasons)
        - passed=True if no rejections (auto-fix filters always pass)
        - cleaned_text is the text after all auto-fixes
        - rejection_reasons is a list of reasons if passed=False (one per failing reject filter)

    allow_price: when True, skip filter_dollar_amounts (used for custom order quotes where
    the bot must state the price explicitly).
    """
    cleaned = text
    reasons: List[str] = []

    # Auto-fixes first (they may clean up content the reject filters would otherwise reject)
    for fn in _AUTO_FIX_FILTERS:
        ok, cleaned, _ = fn(cleaned)

    # Reject filters
    for fn in _REJECT_FILTERS:
        if allow_price and fn is filter_dollar_amounts:
            continue
        ok, cleaned, reason = fn(cleaned)
        if not ok and reason:
            reasons.append(reason)

    return (len(reasons) == 0, cleaned, reasons)


def run_caption_filters(caption: str) -> Tuple[bool, str, List[str]]:
    """
    Run filters specific to PPV captions (stricter — no content description allowed).
    """
    cleaned = caption
    reasons: List[str] = []

    # Auto-fixes
    for fn in _AUTO_FIX_FILTERS:
        ok, cleaned, _ = fn(cleaned)

    # Reject filters — captions get the standard set + content leak check
    caption_filters = _REJECT_FILTERS + [filter_caption_content_leak]
    for fn in caption_filters:
        ok, cleaned, reason = fn(cleaned)
        if not ok and reason:
            reasons.append(reason)

    return (len(reasons) == 0, cleaned, reasons)


def filter_message_dict(msg: Dict, allow_price: bool = False) -> Tuple[bool, Dict, List[str]]:
    """
    Filter a message dict {"text": str, "delay_seconds": int}.
    Returns (passed, cleaned_msg_dict, rejection_reasons).
    """
    text = msg.get("text", "")
    passed, cleaned, reasons = run_message_filters(text, allow_price=allow_price)
    new_msg = dict(msg)
    new_msg["text"] = cleaned
    return passed, new_msg, reasons


def filter_messages_list(messages: List[Dict], allow_price: bool = False) -> Tuple[bool, List[Dict], List[str]]:
    """
    Filter a list of message dicts. Returns (all_passed, cleaned_list, all_reasons).
    A failure on ANY message means the whole batch needs regeneration.
    """
    cleaned_list = []
    all_reasons: List[str] = []
    all_passed = True
    for msg in messages:
        passed, cleaned, reasons = filter_message_dict(msg, allow_price=allow_price)
        cleaned_list.append(cleaned)
        if not passed:
            all_passed = False
            all_reasons.extend(reasons)
    return all_passed, cleaned_list, all_reasons
