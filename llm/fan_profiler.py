"""
Background fan profile extraction using Haiku 4.5.

Runs every 10 fan messages as a non-blocking background task.
Reads the last 20 messages and extracts personality, interests, kinks, and notes.
Results are merged (dedupe-append for lists, overwrite for strings) into the subscriber's
fan_profile and saved back to Supabase.
"""

import os
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

_HAIKU_MODEL = "anthropic/claude-haiku-4-5-20251001"

_SYSTEM = """You extract fan profile data from a chat log between a content creator and a fan.

Read the conversation and identify exactly what was revealed about the fan:

personality  — how he communicates and behaves (one short sentence, e.g. "shy but opens up quickly",
               "very direct and confident", "emotionally open, shares a lot about his life")
interests    — hobbies, topics, things he mentions that are NOT sexual content
               (e.g. ["basketball", "gaming", "gym", "cooking"])
kinks        — sexual turn-ons, fetishes, things that clearly excite him based on what he said
               (e.g. ["feet", "being dominated", "JOI", "dirty talk"])
notes        — anything else worth remembering: job, relationship status, living situation, schedule
               (e.g. "works night shifts", "single for 2 years", "lives alone")

Rules:
- Only include what is CLEARLY EVIDENT from the conversation. Never guess or infer beyond what was said.
- Leave a field as "" or [] if nothing was learned about it this conversation.
- For lists: include only specific, distinct items. Not vague words like "sex" or "content".
- Output ONLY valid JSON. No explanation, no preamble.

Output:
{"personality": "...", "interests": [...], "kinks": [...], "notes": "..."}"""


async def extract_fan_profile(recent_messages: list) -> Optional[dict]:
    """
    Run a Haiku pass over recent_messages and return a partial fan_profile dict.
    Returns None if nothing was learned or the call fails.
    """
    if not recent_messages:
        return None

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return None

    lines = []
    for msg in recent_messages[-20:]:
        role = msg.get("role", "")
        content = (msg.get("content") or "").strip()
        if not content:
            continue
        if role in ("sub", "user"):
            lines.append(f"Fan: {content}")
        elif role in ("bot", "assistant"):
            lines.append(f"Bot: {content}")

    if len(lines) < 3:
        return None

    conversation = "\n".join(lines)

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1",
            timeout=20.0,
        )
        comp = await client.chat.completions.create(
            model=_HAIKU_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": conversation},
            ],
            max_tokens=300,
            temperature=0.1,
        )
        raw = (comp.choices[0].message.content or "").strip()

        # Strip markdown fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        result = json.loads(raw)
        if not isinstance(result, dict):
            return None

        extracted = {
            "personality": str(result.get("personality", "")).strip(),
            "interests": [str(i).strip() for i in (result.get("interests") or []) if str(i).strip()],
            "kinks": [str(k).strip() for k in (result.get("kinks") or []) if str(k).strip()],
            "notes": str(result.get("notes", "")).strip(),
        }

        # Only return if at least one field has content
        if any([extracted["personality"], extracted["interests"], extracted["kinks"], extracted["notes"]]):
            return extracted
        return None

    except Exception as e:
        logger.debug("Fan profile extraction error: %s", e)
        return None
