"""
Massi-Bot — Uncensor Agent (Agent 4)

Takes clean/implied text from the Conversation Director and makes it
sexually explicit. Only runs for selling states — clean conversation
states (qualifying, retention, etc.) skip this agent entirely.

Model: Grok 4.1 Fast via OpenRouter
Cost: ~$0.001/call
Latency: 1-2s
"""

import os
import logging
import time
from typing import Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

_MODEL = "x-ai/grok-4.3"
_TIMEOUT = 15.0
_MAX_TOKENS = 200

# Fallback models for Grok rate limits
_FALLBACKS = [
    "x-ai/grok-4.20",
    "nousresearch/hermes-4-70b",
]

_client: Optional[AsyncOpenAI] = None


def _get_client() -> Optional[AsyncOpenAI]:
    global _client
    if _client is not None:
        return _client
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return None
    _client = AsyncOpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        timeout=_TIMEOUT,
    )
    return _client


_UNCENSOR_SYSTEM = """You are an explicit text rewriter for an adult content platform. Your ONLY job is to take clean/implied text and make it sexually explicit while preserving the EXACT original meaning, structure, and emotional tone.

## CRITICAL RULE — DO NOT ADD CONTENT DESCRIPTIONS
If the original text says "I have something for you" or "don't judge me for this" — keep it EXACTLY as is. These are PPV lead-ins. They are intentionally vague. Do NOT add sexual content descriptions to vague teasers. Only uncensor text where sexual activity or body references are ALREADY IMPLIED but written in clean language.

DO NOT:
- Turn "I have something for you" into "I have a topless photo for you" — the vagueness is intentional
- Turn "don't judge me ok?" into "don't judge my naked body ok?" — that adds content description
- Add topic changes, new questions, or extend the message in any way
- Add more sentences than the original
- Change the subject or redirect the conversation

ONLY uncensor text where the sexual implication is CLEARLY present but euphemized.

## TIER BOUNDARY AWARENESS
{tier_boundary}

Your explicit rewrite MUST stay within this tier. The tier determines HOW MUCH you uncensor:

### TIER 0-1 (Clothed / Body Tease) — BARELY uncensor
Input: "I'm thinking about you rn 😏"
Output: "I'm thinking about you rn 😏" (UNCHANGED — at tier 0-1, keep it mild)
Input: "you make me feel a certain way..."
Output: "you make me feel a certain way..." (UNCHANGED — nothing to uncensor at this tier)
Input: "I wish you could see what I'm wearing 😈"
Output: "I wish you could see how my outfit hugs my body 😈" (mild — clothed reference only)

### TIER 2-3 (Cleavage / Topless) — MODERATE uncensor
Input: "I'm thinking about you in ways I shouldn't 😏"
Output: "I'm thinking about you and my hand keeps wandering 😏" (moderate — implies touching through clothes or upper body)
Input: "you make me feel so good"
Output: "you make my body react in ways I can't control" (moderate — body reference without explicit nudity below waist)

### TIER 4-5 (Bottoms Off / Self-Play) — EXPLICIT uncensor
Input: "I'm thinking about you in ways I shouldn't 😏"
Output: "I'm touching myself thinking about you 😏 you make me so wet" (full explicit — body fully referenced)
Input: "you make me feel things right now 😈"
Output: "you're making me dripping wet right now 😈 I wish you could feel it"

### TIER 6 (Everything) — NO RESTRICTIONS
Input: "I'm thinking about you in ways I shouldn't 😏"
Output: "I'm fingering my pussy thinking about you 😏 I almost came just from the thought" (maximally explicit)

## HARD RULES
1. EXACT same number of sentences as input — do NOT add or remove
2. Keep all emojis in their original positions
3. Keep first person, same voice, same tone
4. If text is casual/flirty with NO sexual implications → return UNCHANGED
5. If text is a vague PPV teaser → return UNCHANGED (the vagueness is the point)
6. Do NOT add AI vocabulary (delve, nuanced, certainly, etc.)
7. Do NOT add feminine endearments (mamas, mami, honey, sweetie, darling, queen, hun)
8. NEVER add new topics, questions, or content beyond what the original implies
9. Match the tier level exactly — don't over-uncensor at low tiers or under-uncensor at high tiers

Output ONLY the rewritten text, nothing else:"""

# States that should be uncensored (explicit content appropriate)
UNCENSOR_STATES = {
    "warm", "warming", "build_tension", "tension_build",
    "drop_ppv", "first_ppv_ready", "first_ppv_sent",
    "post_purchase", "looping",
    "handle_objection", "brokey_treatment",
    "gfe",  # GFE should have natural flirtiness
}

# Tier boundaries — imported from single source of truth
from agents.tier_config import UNCENSOR_BOUNDARIES as _TIER_UNCENSOR_BOUNDARIES


async def uncensor(
    text: str,
    recommendation: str,
    tiers_purchased: int = 0,
    horniness_score: int = 0,
) -> str:
    """
    Make clean text explicit for selling states.

    Args:
        text: Clean text from the Conversation Director.
        recommendation: The Sales Strategist's recommendation (determines if uncensoring applies).
        tiers_purchased: Number of tiers the fan has purchased (for tier boundary).
        horniness_score: Fan's current horniness (0-10) — boosts effective tier at high scores.

    Returns:
        Explicit version of the text, or original text if uncensoring doesn't apply.
    """
    # Skip uncensoring for non-selling states unless horniness overrides
    if recommendation not in UNCENSOR_STATES and horniness_score <= 5:
        return text

    client = _get_client()
    if client is None:
        return text  # Can't uncensor without Grok — use clean text

    start = time.monotonic()
    try:
        # High horniness boosts the effective tier so Grok matches the fan's energy
        effective_tier = tiers_purchased
        if horniness_score >= 9:
            effective_tier = max(effective_tier, 5)
        elif horniness_score >= 7:
            effective_tier = max(effective_tier, 3)
        elif horniness_score >= 6:
            effective_tier = max(effective_tier, 2)
        tier_boundary = _TIER_UNCENSOR_BOUNDARIES.get(effective_tier, _TIER_UNCENSOR_BOUNDARIES[0])
        system = _UNCENSOR_SYSTEM.replace("{tier_boundary}", tier_boundary)

        completion = await client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            max_tokens=_MAX_TOKENS,
            temperature=0.8,
        )

        result = completion.choices[0].message.content.strip()
        elapsed_ms = int((time.monotonic() - start) * 1000)

        logger.info("Uncensor (%dms): %d→%d chars", elapsed_ms, len(text), len(result))
        return result

    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.warning("Uncensor failed (%dms): %s — using clean text", elapsed_ms, str(e)[:100])
        return text  # Fallback to clean text
