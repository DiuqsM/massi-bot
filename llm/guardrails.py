"""
LLM Guardrails — post-processing filters for LLM output.

Two modes:
  post_process()          — universal filters (all states)
  post_process_stateful() — state-specific filters (GuardrailMode)
"""

import re
import random
import logging
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)

# ── Universal patterns ─────────────────────────────────────────────────────────

_AI_PATTERNS = re.compile(
    r"as an AI\b|I'?m an AI\b|I am an AI\b|"
    r"I cannot (send|do|discuss|help with|provide)\b|"
    r"I'?m not able to\b|"
    r"I (am|was) (made|built|created|trained|designed|developed) by\b|"
    r"\bOpenAI\b|\bAnthropic\b|\blanguage model\b",
    re.IGNORECASE,
)

_CONTACT_PATTERNS = re.compile(
    r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b|"          # phone
    r"\b[\w.+]+@[\w.]+\.\w{2,}\b|"                # email
    r"\b(add|find|follow|DM|message)\s+me\s+on\b", # off-platform redirect
    re.IGNORECASE,
)

_AI_VOCAB = re.compile(
    r"\bdelve\b|\bnuanced\b|\bcertainly\b|\bundoubtedly\b|"
    r"\bcomprehensive\b|\bfacilitate\b|\boptimize\b|\bsynthesize\b",
    re.IGNORECASE,
)

_SYCOPHANTIC = re.compile(
    r"^(great question[!,]?|absolutely[!,]?|of course[!,]?|sure thing[!,]?|certainly[!,]?)",
    re.IGNORECASE,
)

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")

_EMOJI_RE = re.compile(
    "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF\U0001F900-\U0001FAFF"
    "\U00002702-\U000027B0❤️\U0001F495]+"
)

_DEFAULT_EMOJIS = ["😏", "💕", "🔥", "😈", "😍", "💋"]


def post_process(text: str, avatar_emojis: list[str] | None = None) -> Optional[str]:
    """
    Universal guardrails. Returns None if any check fails; otherwise cleaned text.
    Checks (in order): empty, AI self-reference, contact info, AI vocab, sycophancy,
    truncates to 3 sentences, ensures punctuation, appends emoji if missing.
    """
    if not text or not text.strip():
        return None

    text = text.strip()

    if _AI_PATTERNS.search(text):
        return None

    if _CONTACT_PATTERNS.search(text):
        return None

    if _AI_VOCAB.search(text):
        return None

    if _SYCOPHANTIC.match(text):
        return None

    # Truncate to 3 sentences
    sentences = _SENTENCE_END.split(text)
    if len(sentences) > 3:
        # Keep up to first 3 complete sentences
        rebuilt = " ".join(sentences[:3])
        # Preserve the punctuation of the 3rd sentence
        match = re.search(r"[.!?]", rebuilt[::-1])
        if match:
            text = rebuilt
        else:
            text = rebuilt + "."
    else:
        text = text

    # Append emoji before final punctuation (keeps the emoji inside the last sentence)
    if not _EMOJI_RE.search(text):
        pool = avatar_emojis if avatar_emojis else _DEFAULT_EMOJIS
        emoji = random.choice(pool)
        # Strip trailing punctuation, insert emoji, re-add punctuation
        base = text.rstrip(".!?")
        punct = text[len(base):] or "."
        text = base + " " + emoji + punct

    # Ensure ends with punctuation (handles pre-existing emoji with no trailing punct)
    if text and text[-1] not in ".!?":
        text += "."

    return text


# ── Stateful guardrails ────────────────────────────────────────────────────────

class GuardrailMode(Enum):
    QUALIFYING = "qualifying"
    SELLING    = "selling"
    OBJECTION  = "objection"
    STANDARD   = "standard"


_STATE_TO_MODE: dict[str, GuardrailMode] = {
    "new":             GuardrailMode.QUALIFYING,
    "welcome_sent":    GuardrailMode.QUALIFYING,
    "qualifying":      GuardrailMode.QUALIFYING,
    "classified":      GuardrailMode.QUALIFYING,
    "warming":         GuardrailMode.SELLING,
    "tension_build":   GuardrailMode.SELLING,
    "first_ppv_ready": GuardrailMode.SELLING,
    "first_ppv_sent":  GuardrailMode.OBJECTION,
    "looping":         GuardrailMode.SELLING,
    "custom_pitch":    GuardrailMode.SELLING,
    "post_session":    GuardrailMode.STANDARD,
    "gfe_active":      GuardrailMode.STANDARD,
    "retention":       GuardrailMode.STANDARD,
    "re_engagement":   GuardrailMode.STANDARD,
}

_EXPLICIT_RE = re.compile(
    r"\b(cock|pussy|fuck(?:ing)?|cum|suck|dick|ass\b|naked|nude|orgasm|"
    r"masturbat|vibrat|dildo|horny|tits|boobs|nipple)\b",
    re.IGNORECASE,
)

# In SELLING mode the bot should never say the price — that's the PPV unlock's job
_PRICE_RE = re.compile(r"\$\s*\d+|\b\d+\s*dollars?\b", re.IGNORECASE)

_SOFT_LANGUAGE_RE = re.compile(
    r"\bdon'?t worry\b|\bno worries\b|\bit'?s okay\b|\bthat'?s (fine|ok|okay)\b|"
    r"\bnot a problem\b",
    re.IGNORECASE,
)

_OTHER_FANS_RE = re.compile(
    r"\bother (fans?|guys?|subscribers?|men)\b|"
    r"\bmy (other|previous|past) fans?\b",
    re.IGNORECASE,
)


def post_process_stateful(text: str, mode: GuardrailMode) -> Optional[str]:
    """Apply mode-specific guardrails. Returns None if text is rejected."""
    if not text or not text.strip():
        return None

    # Universal: AI self-reference always blocked
    if _AI_PATTERNS.search(text) or _AI_VOCAB.search(text) or _SYCOPHANTIC.match(text):
        return None

    if mode == GuardrailMode.QUALIFYING:
        if _EXPLICIT_RE.search(text):
            return None

    elif mode == GuardrailMode.SELLING:
        # Price mentions forbidden — the PPV unlock shows the price
        if _PRICE_RE.search(text):
            return None
        # Allow up to 4 sentences in selling mode (more leeway for tension build)
        sentences = _SENTENCE_END.split(text)
        if len(sentences) > 4:
            text = " ".join(sentences[:4])
            if text and text[-1] not in ".!?":
                text += "."

    elif mode == GuardrailMode.OBJECTION:
        if _SOFT_LANGUAGE_RE.search(text):
            return None
        if _OTHER_FANS_RE.search(text):
            return None

    return text


def get_mode_for_state(state_str: str) -> GuardrailMode:
    return _STATE_TO_MODE.get(state_str, GuardrailMode.STANDARD)
