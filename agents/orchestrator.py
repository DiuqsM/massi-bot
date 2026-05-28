"""
Massi-Bot — Single-Agent Orchestrator

Thin wrapper around agents.single_agent for the connectors.
One Opus 4.7 call per fan message; code-level post-processing handles
guardrails, PPV injection, state advancement, and memory extraction.

Public entry points (called by connectors):
  - process_message(sub, message, avatar, model_profile) -> list[BotAction]
  - process_purchase(sub, amount, avatar, content_type, model_profile) -> list[BotAction]
  - process_new_subscriber(sub, avatar, model_profile) -> list[BotAction]
  - process_resub(sub, avatar, model_profile) -> list[BotAction]
  - process_new_follower(sub, avatar, model_profile) -> list[BotAction]
"""

import os
import sys
import random
import logging
import uuid
from typing import Optional
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'engine'))

from models import Subscriber, SubState, BotAction
from engine.bandit_recorder import record_bot_message_sent
from engine.high_value_memory import HVCategory, append_utterance
from engine.session_control import SessionController, classify_objection, BROKEY_COOLING_WARMTH
import random as _random

from agents.context_builder import build_context
from agents.single_agent import process_message as single_agent_process
from agents.parallel_guardrails import run_all_guardrails, build_corrective_hint

from llm.memory_extractor import update_callback_references
from llm.memory_manager import memory_manager

logger = logging.getLogger(__name__)

# Default tier pricing — override via WILLS_AND_WONTS.md or model profile.
# These are the defaults Massi-Bot ships with; Claude Code will ask the
# operator whether they want to keep or change them during setup.
_DEFAULT_TIER_PRICES = {
    1: 27.38, 2: 36.56, 3: 77.35,
    4: 92.46, 5: 127.45, 6: 200.00,
}

_GFE_CONTINUATION_PRICE = 20.00

# Cobalt-Strike jitter for PPV realness (heads-up -> PPV drop).
_PPV_JITTER_MIN_SECONDS = 10
_PPV_JITTER_MAX_SECONDS = 15


def _tier_prices(model_profile=None) -> dict:
    """Return the active tier price table. Per-model overrides via model_profile."""
    if model_profile and getattr(model_profile, "tier_prices", None):
        try:
            return {int(k): float(v) for k, v in model_profile.tier_prices.items()}
        except Exception:
            pass
    return dict(_DEFAULT_TIER_PRICES)


async def process_message(
    sub: Subscriber,
    message: str,
    avatar=None,
    model_profile=None,
    active_tier_count: Optional[int] = None,
    recovery_context: Optional[dict] = None,
) -> list[BotAction]:
    """
    Route an incoming fan message through the single agent.

    One Opus call with optional tool use (Grok uncensor, custom classifier,
    memory lookup, admin alert). Code-level post-processing enforces
    guardrails and injects PPV heads-up / jitter.
    """
    if not message or not message.strip():
        return []

    req_id = str(uuid.uuid4())[:8]
    logger.info("[%s] Processing message for sub %s: %s", req_id, sub.sub_id, message[:60])

    sub.add_message("sub", message)
    sub.message_count += 1
    sub.gfe_message_count += 1

    # ── Session lock: skip Opus entirely, return template response immediately ──
    # Fan has completed all tiers and is in the cooldown window. No LLM needed —
    # the answer is always "not tonight, come back tomorrow." Bypassing Opus saves
    # ~15s latency and ~$0.015 per message, and guarantees no explicit content leak.
    if getattr(sub, "session_locked_until", None) and sub.session_locked_until > datetime.now():
        from llm.llm_router import _calculate_reply_delay
        push = getattr(sub, "session_lock_push_count", 0)
        sub.session_lock_push_count = push + 1
        lock_response = SessionController.get_session_lock_response(sub, push_count=push)
        sub.add_message("bot", lock_response)
        logger.info(
            "[%s] Session lock active (push #%d) — template response, no Opus call for sub %s",
            req_id, push + 1, sub.sub_id[:8],
        )
        return [BotAction(
            action_type="send_message",
            message=lock_response,
            delay_seconds=_calculate_reply_delay(lock_response),
            metadata={"source": "session_lock_template", "push_count": push},
        )]

    try:
        # ── Objection detection: track state, inject directive into context ──
        # Only active when a PPV is actually live (pending_ppv set) or was sent
        # in the last 30 minutes (fan may reply "no" after the heads-up but before
        # the PPV drop arrives). Never fires during normal post-consent chat.
        _pending_ppv = getattr(sub, "pending_ppv", None)
        _last_pitch = getattr(sub, "last_pitch_at", None)
        _pitch_recent = (
            _last_pitch is not None
            and (datetime.now() - _last_pitch) < timedelta(minutes=30)
        )
        in_selling_mode = _pending_ppv is not None or _pitch_recent
        objection_context: Optional[dict] = None
        if in_selling_mode:
            if SessionController.is_in_brokey_cooldown(sub):
                # Fan hit 2 nos — warmth-only, no PPV drops
                objection_context = {"brokey_cooldown": True}
                logger.info("[%s] Brokey cooldown active for sub %s", req_id, sub.sub_id[:8])
            else:
                objection_type = classify_objection(message)
                if objection_type:
                    ppv_count = sub.spending.ppv_count if sub.spending else 0
                    _, next_action = SessionController.handle_tier_objection(sub, avatar, objection_type)
                    objection_context = {
                        "objection_type": objection_type,
                        "no_count": sub.tier_no_count,
                        "is_brokey": next_action == "brokey",
                        "has_purchased_before": ppv_count > 0,
                    }
                    logger.info("[%s] Objection %s no#%d brokey=%s for sub %s",
                                req_id, objection_type, sub.tier_no_count,
                                next_action == "brokey", sub.sub_id[:8])

        context = await build_context(sub, message, avatar, model_profile=model_profile)
        context["request_id"] = req_id

        if objection_context:
            context["objection_context"] = objection_context

        # Tell the agent the continuation gate is ready — it decides the timing.
        tiers_bought = sub.spending.ppv_count if sub.spending else 0
        threshold = sub.continuation_threshold_jitter or random.randint(40, 50)
        if (
            sub.gfe_message_count >= threshold
            and not getattr(sub, "gfe_continuation_pending", False)
            and tiers_bought == 0
        ):
            context["gfe_continuation_ready"] = True

        if recovery_context:
            # Fix 11: tell the agent it's coming back from silence so it can
            # respond naturally to fan messages sent during the outage.
            context["recovery_context"] = recovery_context

        tier_count = active_tier_count
        if tier_count is None:
            tier_count = 6
            if model_profile and getattr(model_profile, "active_tier_count", None):
                tier_count = int(model_profile.active_tier_count)

        result = await single_agent_process(
            message=message,
            avatar=avatar,
            sub=sub,
            context=context,
            active_tier_count=tier_count,
        )

        messages_list = result.get("messages", []) or []
        ppv_info = result.get("ppv")
        consent_given = result.get("consent_given", False)

        # Don't pitch a PPV the moment the server comes back from an outage.
        # The agent handles the comeback message; next fan message resumes normal selling.
        if recovery_context and ppv_info:
            logger.info("[%s] Recovery mode — suppressing PPV for sub %s", req_id, sub.sub_id[:8])
            ppv_info = None

        if consent_given and not getattr(sub, "sext_consent_given", False):
            sub.sext_consent_given = True
            logger.info("[%s] Consent given by sub %s", req_id, sub.sub_id)

        # Extract fan name if the agent just learned it
        learned_name = (result.get("fan_name") or "").strip()
        if learned_name and learned_name != getattr(sub, "fan_name", ""):
            sub.fan_name = learned_name
            logger.info("[%s] Fan name learned: %r for sub %s", req_id, learned_name, sub.sub_id[:8])

        # Merge fan profile update — lists dedupe-append, strings overwrite
        profile_update = result.get("fan_profile_update") or {}
        if profile_update and isinstance(profile_update, dict):
            if not hasattr(sub, "fan_profile") or not sub.fan_profile:
                sub.fan_profile = {"personality": "", "interests": [], "kinks": [], "notes": ""}
            fp = sub.fan_profile
            if profile_update.get("personality"):
                fp["personality"] = str(profile_update["personality"]).strip()
            if profile_update.get("notes"):
                fp["notes"] = str(profile_update["notes"]).strip()
            for list_field in ("interests", "kinks"):
                new_items = profile_update.get(list_field) or []
                if isinstance(new_items, str):
                    new_items = [new_items]
                existing = fp.get(list_field) or []
                existing_lower = {i.lower() for i in existing}
                for item in new_items:
                    item = str(item).strip()
                    if item and item.lower() not in existing_lower:
                        existing.append(item)
                        existing_lower.add(item.lower())
                fp[list_field] = existing
            sub.fan_profile = fp
            logger.info("[%s] Fan profile updated for sub %s: %s", req_id, sub.sub_id[:8],
                        {k: v for k, v in profile_update.items() if v})

        # Update horniness score — Opus base + code-level keyword boost
        new_score = result.get("horniness_score")
        opus_score = max(0, min(10, int(new_score))) if isinstance(new_score, (int, float)) else getattr(sub, "horniness_score", 0)

        # Keyword detector: fan's actual words override Opus's conservative judgment
        msg_lower = (message or "").lower()
        keyword_boost = 0
        _EXPLICIT_9 = {"cum", "cumming", "orgasm", "cock", "dick", "pussy", "clit", "ass", "tits", "naked", "nude", "fingering", "masturbat", "jerk", "stroke", "fuck", "fucking", "sex", "horny", "hard", "wet", "dripping", "moan"}
        _EXPLICIT_6 = {"hot", "sexy", "turn me on", "turned on", "aroused", "naughty", "dirty", "kinky", "want you", "need you", "body", "touch", "feel you", "show me", "send me", "pic", "video", "content"}
        _COOLDOWN = {"bye", "later", "gtg", "gotta go", "not now", "busy", "nvm", "nevermind", "stop", "no thanks"}

        words = set(msg_lower.split())
        if any(k in msg_lower for k in _EXPLICIT_9):
            keyword_boost = 9
        elif any(k in msg_lower for k in _EXPLICIT_6):
            keyword_boost = 6
        elif any(k in msg_lower for k in _COOLDOWN):
            keyword_boost = -3  # fan cooling off

        # Take whichever is higher between Opus and keyword detector, apply cooldown
        if keyword_boost < 0:
            final_score = max(0, opus_score + keyword_boost)
        else:
            final_score = max(opus_score, keyword_boost)

        old_score = getattr(sub, "horniness_score", 0)
        sub.horniness_score = final_score
        if final_score != old_score:
            logger.info("[%s] Horniness score: %d → %d (opus=%d keyword_boost=%d) for sub %s",
                        req_id, old_score, final_score, opus_score, keyword_boost, sub.sub_id[:8])

        # ── Mechanical consent detection ──
        # Mirrors the horniness keyword boost pattern. If the fan's message contains an
        # unambiguous proactive spend signal, grant consent in code regardless of what
        # Opus put in its JSON output. All phrases are multi-word to avoid false positives.
        _CONSENT_PHRASES = {
            "open to spending", "willing to spend", "down to spend", "ready to spend",
            "i'll pay", "ill pay", "i will pay", "i'd pay", "id pay",
            "i'll buy", "ill buy", "i will buy", "i'd buy", "id buy",
            "happy to pay", "happy to spend",
            "don't mind spending", "dont mind spending",
            "don't mind paying", "dont mind paying",
            "ready to buy", "down to buy",
        }
        if not getattr(sub, "sext_consent_given", False):
            if any(phrase in msg_lower for phrase in _CONSENT_PHRASES):
                sub.sext_consent_given = True
                consent_given = True
                logger.info("[%s] Consent granted mechanically (keyword match) for sub %s",
                            req_id, sub.sub_id[:8])

        # ── Parallel guardrails (8 concurrent checks, Cresta pattern) ──
        tiers_purchased = sub.spending.ppv_count if sub.spending else 0
        sext_consent = getattr(sub, "sext_consent_given", False) or getattr(sub, "horniness_score", 0) > 5
        all_passed, reports = await run_all_guardrails(
            messages=messages_list,
            ppv_intent=ppv_info,
            sub=sub,
            avatar=avatar,
            tiers_purchased=tiers_purchased,
            sext_consent_given=sext_consent,
        )

        guardrail_passed: list[dict] = []
        if all_passed:
            for msg in messages_list:
                text = (msg.get("text") or "").strip()
                if text:
                    guardrail_passed.append({
                        "text": text,
                        "delay_seconds": msg.get("delay_seconds", random.randint(5, 12)),
                    })
        else:
            hint = build_corrective_hint([r for r in reports if not r.passed])
            logger.info("[%s] Guardrail rejection: %s", req_id, hint[:120])
            # Strip PPV intent if guardrails failed (safer to skip than to retry).
            ppv_info = None

        if not guardrail_passed and not ppv_info:
            guardrail_passed = [{"text": "hmm 😏 what were you saying?", "delay_seconds": 8}]

        # ── Build BotActions ──
        actions: list[BotAction] = []
        for msg in guardrail_passed:
            actions.append(BotAction(
                action_type="send_message",
                message=msg["text"],
                delay_seconds=msg["delay_seconds"],
                metadata={"source": "single_agent"},
            ))

        # ── PPV injection with heads-up + Cobalt jitter ──
        # Hard block: never drop a new tier PPV while the session cooldown is active.
        # The prompt already instructs the agent not to pitch, but Opus can still emit
        # a ppv block — strip it mechanically so the lock is guaranteed.
        _session_locked = bool(
            getattr(sub, "session_locked_until", None)
            and sub.session_locked_until > datetime.now()
        )
        if _session_locked and ppv_info and str(ppv_info.get("tier", "")).lower() not in ("continuation", "custom"):
            logger.info(
                "Session lock active until %s — discarding tier PPV for sub %s",
                sub.session_locked_until.isoformat(), sub.sub_id[:8],
            )
            ppv_info = None

        if ppv_info and ppv_info.get("tier"):
            prices = _tier_prices(model_profile)
            ppv_tier = ppv_info.get("tier")
            is_custom = str(ppv_tier).lower() == "custom"
            is_continuation = str(ppv_tier).lower() == "continuation"

            if is_continuation:
                pass  # handled in the GFE continuation block below
            elif is_custom:
                # Fix 13 Bug A: the tool-authoritative price lives on
                # sub.pending_custom_order (written by classify_custom_request),
                # not the agent's JSON echo. The agent has been known to anchor
                # to previously-paid custom prices in conversation history, so
                # the tool result wins. Agent's ppv.price is advisory only —
                # logged at WARNING when the two diverge so we can track drift.
                custom_order = getattr(sub, "pending_custom_order", None) or {}
                tool_price = custom_order.get("quoted_price")
                agent_price = ppv_info.get("price")
                if tool_price:
                    price = float(tool_price)
                    if agent_price and abs(float(agent_price) - price) > 0.01:
                        logger.warning(
                            "Custom PPV price mismatch: agent said $%.2f, tool said $%.2f — using tool",
                            float(agent_price), price,
                        )
                else:
                    price = float(agent_price) if agent_price else 127.38
                caption = ppv_info.get("caption", "just for you")
                actions.append(BotAction(
                    action_type="send_ppv",
                    ppv_price=price,
                    ppv_caption=caption,
                    message="",
                    delay_seconds=random.randint(5, 15),
                    metadata={"tier": "custom", "source": "single_agent"},
                ))
            elif not is_continuation:
                tier_num = int(ppv_tier)
                price = prices.get(tier_num, prices.get(1, 27.38))
                caption = ppv_info.get("caption", "just for you 😈")
                heads_up = ppv_info.get("heads_up", "")

                if heads_up:
                    actions.append(BotAction(
                        action_type="send_message",
                        message=heads_up,
                        delay_seconds=random.randint(4, 9),
                        metadata={"source": "single_agent", "ppv_heads_up": True},
                    ))
                    append_utterance(sub, HVCategory.PPV_HEADS_UP, heads_up)

                # Cobalt-Strike jitter between heads-up and PPV drop.
                ppv_delay = random.randint(_PPV_JITTER_MIN_SECONDS, _PPV_JITTER_MAX_SECONDS)
                actions.append(BotAction(
                    action_type="send_ppv",
                    ppv_price=price,
                    ppv_caption=caption,
                    message="",
                    delay_seconds=ppv_delay,
                    metadata={"tier": f"tier_{tier_num}", "source": "single_agent"},
                ))
                sub.last_pitch_at = datetime.now()

        # ── GFE continuation paywall ──
        # Agent fires it if it recognises the moment; orchestrator forces it if the agent
        # skips (Opus tends to self-exempt when the conversation feels emotionally warm).
        _gfe_ready = context.get("gfe_continuation_ready", False)
        _agent_fired_continuation = bool(
            ppv_info and str(ppv_info.get("tier", "")).lower() == "continuation"
        )
        if _agent_fired_continuation or _gfe_ready:
            sub.gfe_continuation_pending = True
            sub.continuation_threshold_jitter = random.randint(40, 50)
            actions.append(BotAction(
                action_type="send_ppv",
                ppv_price=_GFE_CONTINUATION_PRICE,
                ppv_caption="just for you",
                message="",
                delay_seconds=random.randint(5, 15),
                metadata={"tier": "continuation", "source": "single_agent"},
            ))
            logger.info(
                "GFE continuation paywall fired (%s) for sub %s (msg %d)",
                "agent" if _agent_fired_continuation else "mechanical",
                sub.sub_id[:8], sub.gfe_message_count,
            )

        # ── Track bot messages + bandit record ──
        fan_name_lower = (getattr(sub, "fan_name", "") or "").strip().lower()
        for action in actions:
            if action.message and action.action_type == "send_message":
                sub.add_message("bot", action.message)
                # Track when bot last said the fan's name so the prompt can enforce the 30-msg cooldown
                if fan_name_lower and fan_name_lower in action.message.lower():
                    sub.fan_name_last_used_at_msg = sub.message_count
                    logger.debug("Fan name '%s' used at msg %d", fan_name_lower, sub.message_count)
                try:
                    await record_bot_message_sent(sub, action.message)
                except Exception:
                    logger.debug("bandit record failed", exc_info=True)

        # ── Memory extraction (background-ish) ──
        try:
            update_callback_references(sub, message)
            await memory_manager.maybe_extract_and_store(sub, message)
            for action in actions:
                if action.message and action.action_type == "send_message":
                    await memory_manager.maybe_store_persona_facts(action.message)
        except Exception as exc:
            logger.debug("Memory extraction error: %s", exc)

        return actions

    except Exception as exc:
        logger.exception("Orchestrator error for sub %s: %s", sub.sub_id, exc)
        return [BotAction(
            action_type="send_message",
            message="hmm I got distracted for a sec 😂 what were you saying?",
            delay_seconds=random.randint(5, 12),
        )]


async def process_purchase(
    sub: Subscriber,
    amount: float,
    avatar=None,
    content_type: str = "ppv",
    model_profile=None,
    active_tier_count: Optional[int] = None,
) -> list[BotAction]:
    """
    Process a confirmed purchase event. Agent generates the post-purchase
    reaction + (if appropriate) next-tier lead-in. PPV injection is disabled
    here so the agent can't auto-pitch a new tier during payment confirmation.
    """
    sub.record_purchase(amount, content_type)

    # ANY purchase (tier PPV, custom, continuation, tip) resets the GFE
    # message counter so a fan who just spent money doesn't immediately hit
    # the continuation paywall again.
    sub.gfe_message_count = 0

    if content_type == "gfe_continuation" or (
        getattr(sub, "gfe_continuation_pending", False) and 15.0 <= amount <= 25.0
    ):
        sub.gfe_continuation_pending = False
        # Re-randomize the continuation threshold so the next cycle fires at
        # a different point (prevents deterministic "exactly 30 msgs" pattern).
        sub.continuation_threshold_jitter = random.randint(40, 50)

    try:
        context = await build_context(sub, "paid", avatar, model_profile=model_profile)
        tier_count = active_tier_count
        if tier_count is None:
            tier_count = 6
            if model_profile and getattr(model_profile, "active_tier_count", None):
                tier_count = int(model_profile.active_tier_count)

        tiers_bought_so_far = sub.spending.ppv_count if sub.spending else 0
        context["event_hint"] = (
            f"[PURCHASE CONFIRMED] The fan just unlocked a PPV — tier {tiers_bought_so_far}. "
            f"React with genuine excitement (short, 1 sentence max). "
            f"Then, if there are more tiers left, tease what's coming next — build anticipation. "
            f"Do NOT mention money, prices, or 'tier'. Keep it natural and in-character."
        )

        result = await single_agent_process(
            message="paid",
            avatar=avatar,
            sub=sub,
            context=context,
            active_tier_count=tier_count,
        )

        actions: list[BotAction] = []
        ppv_info = result.get("ppv")

        for msg in (result.get("messages") or []):
            text = (msg.get("text") or "").strip()
            if text:
                actions.append(BotAction(
                    action_type="send_message",
                    message=text,
                    delay_seconds=msg.get("delay_seconds", random.randint(5, 12)),
                    metadata={"source": "single_agent", "context": "post_purchase"},
                ))

        # ── PPV injection: ALWAYS schedule next tier after a confirmed PPV purchase ──
        # The agent's post-purchase reaction is the TEXT. The next tier drop is the
        # orchestrator's job — it knows exactly what tier comes next from ppv_count.
        # Agent's ppv output (if any) is used for caption/heads_up copy; if absent,
        # we use defaults. The tier number is ALWAYS derived from ppv_count.
        if content_type == "ppv":
            tiers_bought = sub.spending.ppv_count if sub.spending else 0  # post-purchase count
            tier_ceiling = tier_count or 6
            if 0 < tiers_bought < tier_ceiling:
                next_tier_num = tiers_bought + 1
                prices = _tier_prices(model_profile)
                price = prices.get(next_tier_num, prices.get(1, 27.38))

                # Use agent's caption/heads_up if it provided them
                agent_ppv = ppv_info or {}
                caption = agent_ppv.get("caption") or "just for you 😈"
                heads_up = agent_ppv.get("heads_up") or ""

                if heads_up:
                    actions.append(BotAction(
                        action_type="send_message",
                        message=heads_up,
                        delay_seconds=random.randint(4, 9),
                        metadata={"source": "single_agent", "ppv_heads_up": True},
                    ))
                    append_utterance(sub, HVCategory.PPV_HEADS_UP, heads_up)

                ppv_delay = random.randint(_PPV_JITTER_MIN_SECONDS, _PPV_JITTER_MAX_SECONDS)
                actions.append(BotAction(
                    action_type="send_ppv",
                    ppv_price=price,
                    ppv_caption=caption,
                    message="",
                    delay_seconds=ppv_delay,
                    metadata={"tier": f"tier_{next_tier_num}", "source": "purchase_auto_inject"},
                ))
                sub.last_pitch_at = datetime.now()
                logger.info(
                    "Post-purchase PPV drop: tier %d $%.2f (delay %ds) for sub %s",
                    next_tier_num, price, ppv_delay, sub.sub_id[:8],
                )

        for action in actions:
            if action.message:
                sub.add_message("bot", action.message)

        return actions

    except Exception as exc:
        logger.exception("Purchase orchestrator error for sub %s: %s", sub.sub_id, exc)
        return [BotAction(
            action_type="send_message",
            message="omg you actually opened it 😍",
            delay_seconds=random.randint(5, 12),
        )]


async def process_new_subscriber(
    sub: Subscriber,
    avatar=None,
    model_profile=None,
    active_tier_count: Optional[int] = None,
) -> list[BotAction]:
    """Welcome a brand-new subscriber. One short opener from the agent."""
    try:
        context = await build_context(sub, "", avatar, model_profile=model_profile)
        tier_count = active_tier_count
        if tier_count is None:
            tier_count = int(getattr(model_profile, "active_tier_count", 6) or 6)
        result = await single_agent_process(
            message="",
            avatar=avatar,
            sub=sub,
            context=context,
            active_tier_count=tier_count,
        )

        actions: list[BotAction] = []
        for msg in (result.get("messages") or []):
            text = (msg.get("text") or "").strip()
            if text:
                actions.append(BotAction(
                    action_type="send_message",
                    message=text,
                    delay_seconds=msg.get("delay_seconds", random.randint(5, 12)),
                    metadata={"source": "single_agent", "context": "welcome"},
                ))

        sub.state = SubState.WELCOME_SENT
        for action in actions:
            if action.message:
                sub.add_message("bot", action.message)
        return actions

    except Exception as exc:
        logger.exception("Welcome orchestrator error for sub %s: %s", sub.sub_id, exc)
        return [BotAction(
            action_type="send_message",
            message="hey 😏 what caught your eye?",
            delay_seconds=random.randint(5, 12),
        )]


async def process_resub(
    sub: Subscriber,
    avatar=None,
    model_profile=None,
    active_tier_count: Optional[int] = None,
) -> list[BotAction]:
    """Welcome back a fan who lapsed and resubscribed."""
    try:
        context = await build_context(sub, "", avatar, model_profile=model_profile)
        tier_count = active_tier_count
        if tier_count is None:
            tier_count = int(getattr(model_profile, "active_tier_count", 6) or 6)

        ppv_count = sub.spending.ppv_count if sub.spending else 0
        total_spent = sub.spending.total_spent if sub.spending else 0.0
        history_note = (
            f"He previously spent ${total_spent:.2f} across {ppv_count} PPV purchase(s)."
            if ppv_count > 0 else "He hasn't purchased anything yet."
        )
        fan_name_note = f" His name is {sub.fan_name}." if sub.fan_name else ""

        context["event_hint"] = (
            f"[RESUB] This fan just resubscribed after their subscription lapsed.{fan_name_note} {history_note} "
            f"Send a warm, genuine 'you came back' message — short, personal, in your voice. "
            f"If you know something specific about him (from fan profile or memories), reference it naturally. "
            f"Don't be dramatic. Don't mention money or content yet. Just welcome him back like you mean it."
        )

        result = await single_agent_process(
            message="",
            avatar=avatar,
            sub=sub,
            context=context,
            active_tier_count=tier_count,
        )

        # Resub is rapport-only — never pitch content on the welcome-back message.
        if result.get("ppv"):
            logger.info("Resub flow: discarding PPV from agent result for sub %s", sub.sub_id[:8])
            result["ppv"] = None

        actions: list[BotAction] = []
        for msg in (result.get("messages") or []):
            text = (msg.get("text") or "").strip()
            if text:
                actions.append(BotAction(
                    action_type="send_message",
                    message=text,
                    delay_seconds=msg.get("delay_seconds", random.randint(5, 12)),
                    metadata={"source": "single_agent", "context": "resub"},
                ))

        sub.state = SubState.WELCOME_SENT
        for action in actions:
            if action.message:
                sub.add_message("bot", action.message)
        return actions

    except Exception as exc:
        logger.exception("Resub orchestrator error for sub %s: %s", sub.sub_id, exc)
        return [BotAction(
            action_type="send_message",
            message="you came back 🥺 I missed you",
            delay_seconds=random.randint(5, 12),
        )]


async def process_new_follower(
    sub: Subscriber,
    avatar=None,
    model_profile=None,
    active_tier_count: Optional[int] = None,
) -> list[BotAction]:
    """Nudge a new follower to subscribe — conversion opportunity."""
    try:
        context = await build_context(sub, "", avatar, model_profile=model_profile)
        tier_count = active_tier_count
        if tier_count is None:
            tier_count = int(getattr(model_profile, "active_tier_count", 6) or 6)

        context["event_hint"] = (
            "[NEW FOLLOWER] This person just followed you but has NOT subscribed yet — they haven't paid. "
            "Send one short, curious, flirty message that makes them want to subscribe. "
            "Tease what's behind the paywall without saying 'subscribe' or 'paywall' directly. "
            "Make it feel like a private moment, like you noticed them specifically. "
            "One or two sentences max. No hard sell. Leave ppv null."
        )

        result = await single_agent_process(
            message="",
            avatar=avatar,
            sub=sub,
            context=context,
            active_tier_count=tier_count,
        )

        actions: list[BotAction] = []
        for msg in (result.get("messages") or []):
            text = (msg.get("text") or "").strip()
            if text:
                actions.append(BotAction(
                    action_type="send_message",
                    message=text,
                    delay_seconds=msg.get("delay_seconds", random.randint(8, 20)),
                    metadata={"source": "single_agent", "context": "new_follower"},
                ))

        for action in actions:
            if action.message:
                sub.add_message("bot", action.message)
        return actions

    except Exception as exc:
        logger.exception("New follower orchestrator error for sub %s: %s", sub.sub_id, exc)
        return [BotAction(
            action_type="send_message",
            message="noticed you 👀 you should come closer...",
            delay_seconds=random.randint(8, 20),
        )]
