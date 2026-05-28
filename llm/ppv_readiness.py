"""
PPV Readiness Checker — uses Grok to decide whether conversation is warm enough
to drop the first PPV without a template-driven state machine transition.

Returns True/False. Caller is responsible for acting on the result.
"""

import os
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_MODEL = "x-ai/grok-4.1-fast"
_MAX_MSG_LEN = 150


def _get_client():
    """Return an AsyncOpenAI client pointed at OpenRouter, or None if no key."""
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return None
    try:
        from openai import AsyncOpenAI
        return AsyncOpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            timeout=10.0,
        )
    except Exception as exc:
        logger.warning("PPV readiness client init failed: %s", exc)
        return None


def _build_prompt(messages: list[dict], state: str, message_count: int) -> str:
    lines = []
    for msg in messages:
        role = msg.get("role", "")
        content = (msg.get("content", "") or "")[:_MAX_MSG_LEN]
        if role == "bot":
            lines.append(f"Her: {content}")
        else:
            lines.append(f"Fan: {content}")

    conversation = "\n".join(lines)
    return (
        f"You are deciding whether to drop a paid photo set right now.\n"
        f"Current state: {state}\n"
        f"Messages in session: {message_count} messages\n\n"
        f"Recent conversation:\n{conversation}\n\n"
        f"Is the fan engaged and warm enough to purchase a PPV right now? "
        f"Reply with YES or NO only."
    )


async def check_ppv_readiness(
    messages: list[dict],
    state: str,
    message_count: int,
) -> bool:
    """
    Ask Grok whether the conversation is warm enough to drop a PPV.

    Returns False (safe default) when:
    - fewer than 2 messages provided
    - no API client available
    - LLM call fails

    Returns True only when the LLM explicitly says YES.
    """
    if len(messages) < 2:
        return False

    client = _get_client()
    if client is None:
        return False

    prompt = _build_prompt(messages, state, message_count)

    try:
        completion = await client.chat.completions.create(
            model=_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=5,
            temperature=0.0,
        )
        answer = completion.choices[0].message.content or ""
        return answer.strip().lower().startswith("yes")
    except Exception as exc:
        logger.warning("PPV readiness check failed: %s", exc)
        return False
