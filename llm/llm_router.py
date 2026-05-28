"""
LLM Router — decides when to use the LLM instead of (or alongside) templates.

Three routing modes:
  route()        — GFE / retention / re-engagement (replaces templates entirely)
  route_bridge() — selling pipeline, explicit fan signal (one-shot injection)
  route_full()   — selling pipeline, full replacement with template fallback
"""

import re
import sys
import os
import random
import logging
from datetime import datetime
from typing import Optional, Any

# Ensure engine/ is on the path for models import (same convention as agents/orchestrator.py)
_ENGINE_DIR = os.path.join(os.path.dirname(__file__), "..", "engine")
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

from models import SubState, BotAction
from llm.llm_client import llm_client, LLMClient
from llm.guardrails import post_process, post_process_stateful, get_mode_for_state
from llm.prompts import build_system_prompt, build_messages, build_full_prompt, build_bridge_prompt
from llm.memory_manager import memory_manager
from llm.context_awareness import get_weather

logger = logging.getLogger(__name__)


# ── State sets ─────────────────────────────────────────────────────────────────

TEMPLATE_ONLY_STATES = frozenset({
    SubState.WARMING,
    SubState.TENSION_BUILD,
    SubState.FIRST_PPV_READY,
    SubState.FIRST_PPV_SENT,
    SubState.LOOPING,
    SubState.CUSTOM_PITCH,
    SubState.POST_SESSION,
})

LLM_ELIGIBLE_STATES = frozenset({
    SubState.GFE_ACTIVE,
    SubState.RETENTION,
    SubState.RE_ENGAGEMENT,
})

BRIDGE_ELIGIBLE_STATES = frozenset({
    SubState.WARMING,
    SubState.TENSION_BUILD,
    SubState.FIRST_PPV_READY,
    SubState.FIRST_PPV_SENT,
    SubState.LOOPING,
})

# States where route_full may inject a PPV if readiness check passes
_PPV_OVERRIDE_STATES = frozenset({
    SubState.WARMING,
    SubState.TENSION_BUILD,
})

# Tier 1 PPV price used for readiness-triggered drops
_TIER1_PRICE = 27.38


# ── Bridge signal detection ────────────────────────────────────────────────────

_BRIDGE_TRIGGERS = (
    r"so hard",
    r"stroking",
    r"jerking",
    r"touching myself",
    r"horny",
    r"can'?t stop thinking about you",
    r"making me (so )?(hard|wet|crazy)",
    r"about to (cum|finish)",
)

_BRIDGE_RE = re.compile("|".join(_BRIDGE_TRIGGERS), re.IGNORECASE)
_PRICE_OBJECTION_RE = re.compile(
    r"\b(expensive|afford|too much money|broke|can'?t afford)\b",
    re.IGNORECASE,
)


def _should_bridge(message: str) -> bool:
    """True if the fan's message contains an explicit arousal signal suitable for a bridge reply."""
    if not message or message.startswith("["):
        return False
    if _PRICE_OBJECTION_RE.search(message):
        return False
    words = message.split()
    if len(words) < 4:
        return False
    return bool(_BRIDGE_RE.search(message))


# ── Decision context ───────────────────────────────────────────────────────────

def _extract_decision_context(sub: Any, pre_state: SubState, actions: list) -> dict:
    """Extract structured context dict for route_full prompt and guardrails."""
    state = getattr(sub, "state", pre_state)
    state_str = state.value if state else ""
    pre_str = pre_state.value if pre_state else state_str

    # PPV info from template actions
    has_ppv = any(a.action_type == "send_ppv" for a in actions)
    ppv_price = None
    ppv_tier = None
    for a in actions:
        if a.action_type == "send_ppv":
            ppv_price = a.ppv_price
            ppv_tier = a.metadata.get("tier") if a.metadata else None
            break

    # New-state from template (post_state)
    post_state = state_str
    for a in actions:
        if getattr(a, "new_state", None):
            post_state = a.new_state.value
            break

    # Session lock
    session_locked_until = getattr(sub, "session_locked_until", None)
    session_locked = bool(session_locked_until and session_locked_until > datetime.now())

    # Objection level
    tier_no_count = getattr(sub, "tier_no_count", 0) or 0
    brokey_flagged = getattr(sub, "brokey_flagged", False)

    # Mission key
    if brokey_flagged:
        mission_key = "brokey_treatment"
    elif tier_no_count >= 3:
        mission_key = "objection_3"
    elif tier_no_count == 2:
        mission_key = "objection_2"
    elif tier_no_count == 1:
        mission_key = "objection_1"
    elif session_locked and state_str == "retention":
        mission_key = "retention_locked"
    else:
        mission_key = pre_str

    # Returning user flag
    message_count = getattr(sub, "message_count", 0) or 0
    recent_messages = getattr(sub, "recent_messages", []) or []
    is_returning = message_count > 0 or len(recent_messages) > 0

    # Days since last message
    days_silent = 0
    last_msg_at = getattr(sub, "last_message_at", None)
    if last_msg_at:
        try:
            if isinstance(last_msg_at, str):
                last_msg_at = datetime.fromisoformat(last_msg_at)
            days_silent = (datetime.now() - last_msg_at).days
        except Exception:
            pass

    return {
        "pre_state": pre_str,
        "post_state": post_state,
        "has_ppv": has_ppv,
        "ppv_price": ppv_price,
        "ppv_tier": ppv_tier,
        "mission_key": mission_key,
        "objection_level": tier_no_count,
        "is_brokey": brokey_flagged,
        "session_locked": session_locked,
        "loop_number": getattr(sub, "loop_count", 0) or 0,
        "qualifying_q_index": getattr(sub, "qualifying_questions_asked", 0) or 0,
        "days_silent": days_silent,
        "likely_bought": getattr(getattr(sub, "spending", None), "ppv_count", 0) > 0,
        "template_messages": [
            a.message for a in actions if a.action_type == "send_message"
        ],
        "content_description": {},
        "is_returning": is_returning,
        "persona_facts": [],
    }


# ── Delay calculation ──────────────────────────────────────────────────────────

def _calculate_reply_delay(text: str) -> int:
    """Return a human-feeling delay in seconds based on text length."""
    words = len(text.split())
    base = max(2, min(words, 15))
    jitter = random.randint(0, 5)
    return base + jitter


# ── Validator stub (patched in tests for integration) ─────────────────────────

async def validate_response(text: str, sub: Any) -> tuple[bool, Optional[str]]:
    """Stub validator — always passes. Real validation handled by guardrails."""
    return (True, None)


# ── Router ─────────────────────────────────────────────────────────────────────

class LLMRouter:

    def should_use_llm(self, sub: Any) -> bool:
        """True only for GFE/retention states when the LLM client is available."""
        if not llm_client.is_available:
            return False
        state = getattr(sub, "state", None)
        return state in LLM_ELIGIBLE_STATES

    def wrap_as_action(self, text: str, source: str = "llm") -> BotAction:
        """Wrap a text string into a BotAction with a human-feeling delay."""
        return BotAction(
            action_type="send_message",
            message=text,
            delay_seconds=_calculate_reply_delay(text),
            metadata={"source": source},
        )

    async def route(
        self,
        sub: Any,
        message: str,
        avatar: Any,
    ) -> Optional[list[BotAction]]:
        """
        GFE / retention routing. Returns list[BotAction] on success, None on failure.
        None triggers template fallback in the caller.
        """
        if not self.should_use_llm(sub):
            return None
        if avatar is None:
            return None

        system_prompt = build_system_prompt(avatar, sub)
        msgs = build_messages(system_prompt, sub, message)

        response = await llm_client.generate(msgs)
        if not response:
            return None

        cleaned = post_process(
            response,
            avatar_emojis=getattr(getattr(avatar, "persona", None), "voice", None)
            and getattr(avatar.persona.voice, "favorite_phrases", None),
        )
        if not cleaned:
            return None

        return [self.wrap_as_action(cleaned)]

    async def route_bridge(
        self,
        sub: Any,
        message: str,
        avatar: Any,
    ) -> Optional[list[BotAction]]:
        """
        Inject a single LLM response when fan sends an explicit signal mid-selling-pipeline.
        Returns None if state/message aren't eligible, or if LLM fails.
        """
        state = getattr(sub, "state", None)
        if state not in BRIDGE_ELIGIBLE_STATES:
            return None
        if not _should_bridge(message):
            return None
        if avatar is None:
            return None

        prompt = build_bridge_prompt(avatar, sub, message)
        msgs = [{"role": "user", "content": prompt}]

        response = await llm_client.generate(msgs)
        if not response:
            return None

        cleaned = post_process(response)
        if not cleaned:
            return None

        action = BotAction(
            action_type="send_message",
            message=cleaned,
            delay_seconds=_calculate_reply_delay(cleaned),
            metadata={"source": "llm_bridge"},
        )
        return [action]

    async def route_full(
        self,
        sub: Any,
        message: str,
        avatar: Any,
        template_actions: list[BotAction],
        pre_state: SubState,
    ) -> list[BotAction]:
        """
        Full LLM replacement for selling-pipeline states.

        - Replaces send_message text with LLM output (metadata source="llm_full").
        - Preserves send_ppv actions (price, content_id, caption, state transitions).
        - Falls back to template_actions if LLM fails or guardrails reject.
        - Optionally injects a PPV if readiness check passes (WARMING/TENSION_BUILD only).
        """
        if avatar is None:
            return template_actions

        # Check if template already has a PPV
        template_has_ppv = any(a.action_type == "send_ppv" for a in template_actions)

        # PPV readiness override (only for WARMING / TENSION_BUILD with no PPV yet)
        ppv_override: Optional[BotAction] = None
        if pre_state in _PPV_OVERRIDE_STATES and not template_has_ppv:
            from llm.ppv_readiness import check_ppv_readiness
            recent_msgs = getattr(sub, "recent_messages", []) or []
            msg_count = getattr(sub, "message_count", len(recent_msgs))
            state_str = pre_state.value
            ready = await check_ppv_readiness(recent_msgs, state_str, msg_count)
            if ready:
                ppv_override = BotAction(
                    action_type="send_ppv",
                    message="",
                    ppv_price=_TIER1_PRICE,
                    ppv_caption="something special just for you 😈",
                    delay_seconds=_calculate_reply_delay(""),
                    new_state=SubState.FIRST_PPV_SENT,
                    metadata={"tier": "tier_1_body_tease", "source": "llm_ppv_override"},
                )
                sub.state = SubState.FIRST_PPV_SENT

        # Build decision context and prompt
        ctx = _extract_decision_context(sub, pre_state, template_actions)

        # Enrich with persona facts
        try:
            model_id = getattr(sub, "model_id", None) or ""
            if model_id:
                ctx["persona_facts"] = await memory_manager.get_persona_context(model_id=model_id)
        except Exception:
            pass

        prompt = build_full_prompt(avatar, sub, ctx, message)
        msgs = [{"role": "user", "content": prompt}]

        response = await llm_client.generate(msgs)

        if not response:
            result = list(template_actions)
            if ppv_override:
                result.append(ppv_override)
            return result

        # Apply stateful guardrails
        state_str = pre_state.value
        mode = get_mode_for_state(state_str)
        cleaned = post_process_stateful(response, mode)

        if not cleaned:
            result = list(template_actions)
            if ppv_override:
                result.append(ppv_override)
            return result

        # Build result: replace send_message with LLM text, preserve send_ppv
        result: list[BotAction] = []
        msg_injected = False
        for action in template_actions:
            if action.action_type == "send_message" and not msg_injected:
                result.append(BotAction(
                    action_type="send_message",
                    message=cleaned,
                    delay_seconds=_calculate_reply_delay(cleaned),
                    new_state=action.new_state,
                    metadata={"source": "llm_full"},
                ))
                msg_injected = True
            else:
                result.append(action)

        if not msg_injected:
            result.append(BotAction(
                action_type="send_message",
                message=cleaned,
                delay_seconds=_calculate_reply_delay(cleaned),
                metadata={"source": "llm_full"},
            ))

        if ppv_override:
            result.append(ppv_override)

        return result


# Singleton
llm_router = LLMRouter()
