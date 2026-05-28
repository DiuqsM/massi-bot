"""
Massi-Bot - Subscriber Store

Bridge between the engine's in-memory Subscriber dataclass and Supabase.
Handles serialization, deserialization, and CRUD for subscriber records.

DB table: subscribers
Key columns with dedicated DB fields:
  id, platform, platform_user_id, username, display_name, state,
  whale_score, total_spent, persona_id, current_script_id, current_tier,
  loop_count, callback_references, recent_messages, spending_history,
  qualifying_data, last_message_at, created_at, model_id

Everything else (sub_type, ghost_count, gfe_active, session fields, etc.)
is packed into the qualifying_data JSONB field.
"""

import logging
from datetime import datetime
from typing import Optional, List, Dict, Any

from persistence.supabase_client import get_client

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'engine'))

from models import (
    Subscriber, SubState, SubType, SubTier, ScriptPhase,
    QualifyingData, SpendingHistory,
)

logger = logging.getLogger(__name__)

TABLE = "subscribers"


# ─────────────────────────────────────────────
# SERIALIZATION HELPERS
# ─────────────────────────────────────────────

def _dt_to_iso(dt: Optional[datetime]) -> Optional[str]:
    """Convert datetime to ISO 8601 string, or None."""
    return dt.isoformat() if dt else None


def _iso_to_dt(value: Optional[str]) -> Optional[datetime]:
    """Parse ISO 8601 string to datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        # Handle both '+00:00' and 'Z' suffixes
        s = value.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except (ValueError, AttributeError, TypeError):
        return None


def _subscriber_to_row(
    sub: Subscriber,
    platform: str,
    platform_user_id: str,
    model_id: str,
) -> Dict[str, Any]:
    """
    Serialize a Subscriber dataclass into a Supabase row dict.
    Dedicated columns get their own keys; everything else goes into qualifying_data.
    """
    # Build spending_history JSONB
    spending_data: Dict[str, Any] = {
        "total_spent": sub.spending.total_spent,
        "ppv_count": sub.spending.ppv_count,
        "custom_count": sub.spending.custom_count,
        "tip_count": sub.spending.tip_count,
        "last_purchase_date": _dt_to_iso(sub.spending.last_purchase_date),
        "avg_ppv_price": sub.spending.avg_ppv_price,
        "highest_single_purchase": sub.spending.highest_single_purchase,
        "rejected_ppv_count": sub.spending.rejected_ppv_count,
        "price_objection_count": sub.spending.price_objection_count,
    }

    # Build qualifying_data JSONB — merges the QualifyingData fields with
    # engine state that has no dedicated column.
    qualifying_data: Dict[str, Any] = {
        # QualifyingData fields
        "age": sub.qualifying.age,
        "location": sub.qualifying.location,
        "occupation": sub.qualifying.occupation,
        "relationship_status": sub.qualifying.relationship_status,
        "subscribe_reason": sub.qualifying.subscribe_reason,
        "interests": sub.qualifying.interests,
        "mentions_spending": sub.qualifying.mentions_spending,
        "emotional_openness": sub.qualifying.emotional_openness,
        "response_speed": sub.qualifying.response_speed,
        "message_length": sub.qualifying.message_length,
        "initiated_sexual": sub.qualifying.initiated_sexual,

        # Engine state without dedicated columns
        "sub_type": sub.sub_type.value,
        "source_ig_account": sub.source_ig_account,
        "source_detected": sub.source_detected,
        "is_follower_only": sub.is_follower_only,
        "subscribe_date": _dt_to_iso(sub.subscribe_date),
        "message_count": sub.message_count,
        "qualifying_questions_asked": sub.qualifying_questions_asked,
        "current_script_phase": sub.current_script_phase.value if sub.current_script_phase else None,
        "scripts_completed": sub.scripts_completed,
        "gfe_active": sub.gfe_active,
        "personal_details_shared": sub.personal_details_shared,
        "emotional_hooks": sub.emotional_hooks,
        "last_active_date": _dt_to_iso(sub.last_active_date),
        "ghost_count": sub.ghost_count,
        "re_engagement_attempts": sub.re_engagement_attempts,
        "asked_for_meetup": sub.asked_for_meetup,
        "asked_for_free_content": sub.asked_for_free_content,
        "one_word_reply_streak": sub.one_word_reply_streak,
        "abusive": sub.abusive,
        "tier_no_count": sub.tier_no_count,
        "last_session_completed_at": _dt_to_iso(sub.last_session_completed_at),
        "session_locked_until": _dt_to_iso(sub.session_locked_until),
        "custom_declined": sub.custom_declined,
        "brokey_flagged": sub.brokey_flagged,
        "sent_captions": sub.sent_captions,
        "gfe_message_count": sub.gfe_message_count,
        "sext_consent_given": sub.sext_consent_given,
        "horniness_score": sub.horniness_score,
        "fan_name": sub.fan_name,
        "fan_profile": sub.fan_profile,
        "tags": sub.tags,
        "gfe_continuation_pending": sub.gfe_continuation_pending,
        "gfe_continuations_paid": sub.gfe_continuations_paid,
        "ppv_heads_up_count": sub.ppv_heads_up_count,
        "ppv_threshold_jitter": sub.ppv_threshold_jitter,
        "last_consent_decline_at_msg_count": sub.last_consent_decline_at_msg_count,
        "pending_ppv": sub.pending_ppv,
        "current_session_number": sub.current_session_number,
        "custom_request_streak": sub.custom_request_streak,
        "goodbye_patterns": sub.goodbye_patterns,
        "in_flight_departure": sub.in_flight_departure,
        "continuation_threshold_jitter": sub.continuation_threshold_jitter,
        "pending_custom_order": sub.pending_custom_order,
        "high_value_utterances": sub.high_value_utterances,
        "high_value_utterances_archive": sub.high_value_utterances_archive,
        "last_crash_time": _dt_to_iso(getattr(sub, 'last_crash_time', None)),
        "last_pitch_at": _dt_to_iso(sub.last_pitch_at),
        # Error / auto-recovery state (Fix 11)
        "last_error_at": getattr(sub, "last_error_at", None),
        "last_error_context": getattr(sub, "last_error_context", None),
        "last_successful_bot_message_at": getattr(sub, "last_successful_bot_message_at", None),
        "unrecovered_inbound": getattr(sub, "unrecovered_inbound", None) or [],
        "recovery_attempts": getattr(sub, "recovery_attempts", 0),
        "recovery_next_attempt_at": getattr(sub, "recovery_next_attempt_at", None),
        "recovery_manual_only": getattr(sub, "recovery_manual_only", False),
    }

    return {
        "platform": platform,
        "platform_user_id": platform_user_id,
        "model_id": model_id,
        "username": sub.username,
        "display_name": sub.display_name,
        "state": sub.state.value,
        "whale_score": sub.whale_score,
        "total_spent": sub.spending.total_spent,
        "persona_id": sub.persona_id,
        "current_script_id": sub.current_script_id,
        "current_tier": sub.current_loop_number,   # loop_count in DB
        "loop_count": sub.current_loop_number,
        "callback_references": sub.callback_references,
        "recent_messages": sub.recent_messages,
        "spending_history": spending_data,
        "qualifying_data": qualifying_data,
        "last_message_at": _dt_to_iso(sub.last_message_date),
    }


def _decay_horniness(score: int, last_bot_message_at: Optional[str], ppv_count: int) -> int:
    """
    Time-based horniness score decay applied at load time.

    Arousal is a present-state signal — a fan who was at 8 during a sexting session
    three days ago is not still at 8 when they message again. Decaying prevents the bot
    from jumping straight to explicit/Grok mode on a cold return.

    Floors: buyers (ppv_count > 0) floor at 3 to preserve relationship warmth.
    Non-buyers floor at 0 — no prior investment, treat as fresh start.

    The orchestrator's Opus scoring + keyword detector push the score back up
    within 1-2 messages if the fan re-escalates naturally.
    """
    if score == 0:
        return 0
    if not last_bot_message_at:
        return score  # No send history yet — leave as-is

    try:
        from datetime import timezone as _tz
        last_at = datetime.fromisoformat(last_bot_message_at.replace("Z", "+00:00"))
        now = datetime.now(_tz.utc) if last_at.tzinfo else datetime.now()
        gap_hours = (now - last_at).total_seconds() / 3600
    except Exception:
        return score

    floor = 3 if ppv_count > 0 else 0

    if gap_hours < 1:
        decayed = score              # Same session — no decay
    elif gap_hours < 4:
        decayed = score - 2          # Slight cooldown
    elif gap_hours < 24:
        decayed = round(score * 0.5) # Half-day gap — significant drop
    elif gap_hours < 72:
        decayed = round(score * 0.3) # Multi-day gap
    else:
        decayed = round(score * 0.2) # 3+ days — near-reset

    result = max(floor, decayed)
    if result != score:
        logger.debug(
            "Horniness decay: %d → %d (gap=%.1fh ppv_count=%d)",
            score, result, gap_hours, ppv_count,
        )
    return result


def _row_to_subscriber(row: Dict[str, Any]) -> Subscriber:
    """
    Deserialize a Supabase row dict back into a Subscriber dataclass.
    """
    qd: Dict = row.get("qualifying_data") or {}
    sh: Dict = row.get("spending_history") or {}

    # Reconstruct QualifyingData
    qualifying = QualifyingData(
        age=qd.get("age"),
        location=qd.get("location"),
        occupation=qd.get("occupation"),
        relationship_status=qd.get("relationship_status"),
        subscribe_reason=qd.get("subscribe_reason"),
        interests=qd.get("interests") or [],
        mentions_spending=qd.get("mentions_spending", False),
        emotional_openness=qd.get("emotional_openness", 0),
        response_speed=qd.get("response_speed", "normal"),
        message_length=qd.get("message_length", "normal"),
        initiated_sexual=qd.get("initiated_sexual", False),
    )

    # Reconstruct SpendingHistory
    spending = SpendingHistory(
        total_spent=sh.get("total_spent", 0.0),
        ppv_count=sh.get("ppv_count", 0),
        custom_count=sh.get("custom_count", 0),
        tip_count=sh.get("tip_count", 0),
        last_purchase_date=_iso_to_dt(sh.get("last_purchase_date")),
        avg_ppv_price=sh.get("avg_ppv_price", 0.0),
        highest_single_purchase=sh.get("highest_single_purchase", 0.0),
        rejected_ppv_count=sh.get("rejected_ppv_count", 0),
        price_objection_count=sh.get("price_objection_count", 0),
    )

    # Parse optional script phase
    phase_raw = qd.get("current_script_phase")
    current_script_phase: Optional[ScriptPhase] = None
    if phase_raw:
        try:
            current_script_phase = ScriptPhase(phase_raw)
        except ValueError:
            logger.warning("Unknown ScriptPhase value: %s", phase_raw)

    # Parse sub_type
    try:
        sub_type = SubType(qd.get("sub_type", "unknown"))
    except ValueError:
        sub_type = SubType.UNKNOWN

    # Use DB UUID as sub_id for uniqueness
    sub = Subscriber(
        sub_id=str(row.get("id", "")),
        username=row.get("username", ""),
        display_name=row.get("display_name", ""),
        state=_safe_enum(SubState, row.get("state", "new"), SubState.NEW),
        sub_type=sub_type,
        persona_id=row.get("persona_id", ""),
        source_ig_account=qd.get("source_ig_account", ""),
        source_detected=qd.get("source_detected", False),
        is_follower_only=qd.get("is_follower_only", False),
        subscribe_date=_iso_to_dt(qd.get("subscribe_date")) or datetime.now(),
        qualifying=qualifying,
        spending=spending,
        message_count=qd.get("message_count", 0),
        qualifying_questions_asked=qd.get("qualifying_questions_asked", 0),
        current_script_id=row.get("current_script_id"),
        current_script_phase=current_script_phase,
        current_loop_number=row.get("loop_count", 0),
        scripts_completed=qd.get("scripts_completed") or [],
        gfe_active=qd.get("gfe_active", False),
        personal_details_shared=qd.get("personal_details_shared") or {},
        callback_references=row.get("callback_references") or [],
        emotional_hooks=qd.get("emotional_hooks") or [],
        last_message_date=_iso_to_dt(row.get("last_message_at")),
        last_active_date=_iso_to_dt(qd.get("last_active_date")),
        ghost_count=qd.get("ghost_count", 0),
        re_engagement_attempts=qd.get("re_engagement_attempts", 0),
        asked_for_meetup=qd.get("asked_for_meetup", False),
        asked_for_free_content=qd.get("asked_for_free_content", 0),
        one_word_reply_streak=qd.get("one_word_reply_streak", 0),
        abusive=qd.get("abusive", False),
        tier_no_count=qd.get("tier_no_count", 0),
        last_session_completed_at=_iso_to_dt(qd.get("last_session_completed_at")),
        session_locked_until=_iso_to_dt(qd.get("session_locked_until")),
        custom_declined=qd.get("custom_declined", False),
        brokey_flagged=qd.get("brokey_flagged", False),
        sent_captions=qd.get("sent_captions") or [],
        gfe_message_count=qd.get("gfe_message_count", 0),
        sext_consent_given=qd.get("sext_consent_given", False),
        fan_name=qd.get("fan_name", ""),
        fan_profile=qd.get("fan_profile") or {"personality": "", "interests": [], "kinks": [], "notes": ""},
        horniness_score=_decay_horniness(
            score=max(8, qd.get("horniness_score", 0)) if qd.get("sext_consent_given", False) else qd.get("horniness_score", 0),
            last_bot_message_at=qd.get("last_successful_bot_message_at") or row.get("last_message_at"),
            ppv_count=sh.get("ppv_count", 0),
        ),
        tags=qd.get("tags") or [],
        gfe_continuation_pending=qd.get("gfe_continuation_pending", False),
        gfe_continuations_paid=qd.get("gfe_continuations_paid", 0),
        ppv_heads_up_count=qd.get("ppv_heads_up_count", 0),
        ppv_threshold_jitter=qd.get("ppv_threshold_jitter"),
        last_consent_decline_at_msg_count=qd.get("last_consent_decline_at_msg_count"),
        pending_ppv=qd.get("pending_ppv"),
        current_session_number=qd.get("current_session_number", 1),
        custom_request_streak=qd.get("custom_request_streak", 0),
        goodbye_patterns=qd.get("goodbye_patterns") or [],
        in_flight_departure=qd.get("in_flight_departure"),
        continuation_threshold_jitter=qd.get("continuation_threshold_jitter"),
        pending_custom_order=qd.get("pending_custom_order"),
        high_value_utterances=qd.get("high_value_utterances") or {},
        high_value_utterances_archive=qd.get("high_value_utterances_archive") or {},
        last_crash_time=_iso_to_dt(qd.get("last_crash_time")),
        last_pitch_at=_iso_to_dt(qd.get("last_pitch_at")),
        # Error / auto-recovery state (Fix 11)
        last_error_at=qd.get("last_error_at"),
        last_error_context=qd.get("last_error_context"),
        last_successful_bot_message_at=qd.get("last_successful_bot_message_at"),
        unrecovered_inbound=qd.get("unrecovered_inbound") or [],
        recovery_attempts=qd.get("recovery_attempts", 0) or 0,
        recovery_next_attempt_at=qd.get("recovery_next_attempt_at"),
        recovery_manual_only=bool(qd.get("recovery_manual_only", False)),
        recent_messages=row.get("recent_messages") or [],
    )
    return sub


def _safe_enum(enum_cls, value: str, default):
    """Parse an enum value. For SubState, uses RETENTION as safe fallback instead of NEW."""
    try:
        return enum_cls(value)
    except ValueError:
        if enum_cls.__name__ == "SubState":
            logger.error(
                "CORRUPTED SubState value: '%s' — using RETENTION as safe fallback "
                "(subscriber may need manual review)", value,
            )
            return SubState.RETENTION
        logger.warning("Unknown %s value: %s — using default %s", enum_cls.__name__, value, default)
        return default


# ─────────────────────────────────────────────
# PUBLIC API
# ─────────────────────────────────────────────

def load_subscriber(
    platform: str,
    platform_user_id: str,
    model_id: str,
) -> Optional[Subscriber]:
    """
    Load a subscriber from Supabase by platform identity.
    Returns None if the subscriber doesn't exist yet.
    """
    db = get_client()
    result = (
        db.table(TABLE)
        .select("*")
        .eq("platform", platform)
        .eq("platform_user_id", platform_user_id)
        .eq("model_id", model_id)
        .limit(1)
        .execute()
    )
    if not result.data:
        return None
    return _row_to_subscriber(result.data[0])


def create_subscriber(
    platform: str,
    platform_user_id: str,
    model_id: str,
    username: str = "",
    display_name: str = "",
    persona_id: str = "",
) -> Subscriber:
    """
    Insert a new subscriber row and return the resulting Subscriber object.
    Raises an exception if the insert fails (e.g. duplicate constraint).
    """
    db = get_client()
    row = {
        "platform": platform,
        "platform_user_id": platform_user_id,
        "model_id": model_id,
        "username": username,
        "display_name": display_name,
        "state": SubState.NEW.value,
        "persona_id": persona_id,
        "whale_score": 0,
        "total_spent": 0.0,
        "current_tier": 0,
        "loop_count": 0,
        "callback_references": [],
        "recent_messages": [],
        "spending_history": {
            "total_spent": 0.0, "ppv_count": 0, "custom_count": 0,
            "tip_count": 0, "last_purchase_date": None, "avg_ppv_price": 0.0,
            "highest_single_purchase": 0.0, "rejected_ppv_count": 0,
            "price_objection_count": 0,
        },
        "qualifying_data": {
            "sub_type": SubType.UNKNOWN.value,
            "source_ig_account": "",
            "source_detected": False,
            "is_follower_only": False,
            "subscribe_date": datetime.now().isoformat(),
            "message_count": 0,
            "qualifying_questions_asked": 0,
            "interests": [],
            "mentions_spending": False,
            "emotional_openness": 0,
            "response_speed": "normal",
            "message_length": "normal",
            "initiated_sexual": False,
            "scripts_completed": [],
            "gfe_active": False,
            "personal_details_shared": {},
            "emotional_hooks": [],
            "ghost_count": 0,
            "re_engagement_attempts": 0,
            "asked_for_meetup": False,
            "asked_for_free_content": 0,
            "one_word_reply_streak": 0,
            "abusive": False,
            "tier_no_count": 0,
            "brokey_flagged": False,
            "custom_declined": False,
            "sent_captions": [],
            "gfe_message_count": 0,
            "sext_consent_given": False,
            "gfe_continuation_pending": False,
            "gfe_continuations_paid": 0,
        },
    }
    result = db.table(TABLE).insert(row).execute()
    if not result.data:
        raise RuntimeError(f"Failed to create subscriber for {platform}:{platform_user_id}")
    logger.info("Created subscriber %s on %s (model %s)", platform_user_id, platform, model_id)
    return _row_to_subscriber(result.data[0])


def save_subscriber(
    sub: Subscriber,
    platform: str,
    platform_user_id: str,
    model_id: str,
) -> None:
    """
    Upsert the full subscriber state back to Supabase.
    Uses the (platform, platform_user_id, model_id) unique constraint for conflict resolution.
    """
    db = get_client()
    row = _subscriber_to_row(sub, platform, platform_user_id, model_id)
    db.table(TABLE).upsert(row, on_conflict="platform,platform_user_id,model_id").execute()
    logger.debug("Saved subscriber %s on %s", platform_user_id, platform)


def get_subscribers_by_state(
    model_id: str,
    state: SubState,
    platform: str = "fanvue",
) -> List[Subscriber]:
    """
    Fetch all subscribers for a model in a given pipeline state.
    Useful for batch re-engagement sweeps or stats queries.
    """
    db = get_client()
    result = (
        db.table(TABLE)
        .select("*")
        .eq("model_id", model_id)
        .eq("platform", platform)
        .eq("state", state.value)
        .execute()
    )
    return [_row_to_subscriber(r) for r in (result.data or [])]


def get_top_whales(
    model_id: str,
    platform: str = "fanvue",
    limit: int = 20,
) -> List[Subscriber]:
    """
    Return the top N subscribers ordered by whale_score descending.
    """
    db = get_client()
    result = (
        db.table(TABLE)
        .select("*")
        .eq("model_id", model_id)
        .eq("platform", platform)
        .order("whale_score", desc=True)
        .limit(limit)
        .execute()
    )
    return [_row_to_subscriber(r) for r in (result.data or [])]


def get_subscriber_count(model_id: str, platform: str = "fanvue") -> int:
    """Return total subscriber count for a model on a platform."""
    db = get_client()
    result = (
        db.table(TABLE)
        .select("id", count="exact")
        .eq("model_id", model_id)
        .eq("platform", platform)
        .execute()
    )
    return result.count or 0


def record_transaction(
    subscriber_db_id: str,
    model_id: str,
    transaction_type: str,
    amount: float,
    platform: str = "fanvue",
    content_ref: Optional[str] = None,
) -> None:
    """
    Insert a transaction record (PPV purchase, tip, subscription).
    transaction_type: "ppv", "tip", "subscription", "custom"
    amount: in dollars (e.g. 27.38)
    """
    db = get_client()
    db.table("transactions").insert({
        "subscriber_id": subscriber_db_id,
        "model_id": model_id,
        "type": transaction_type,
        "amount": amount,
        "platform": platform,
        "content_ref": content_ref,
    }).execute()
    logger.info(
        "Transaction recorded: %s $%.2f for sub %s",
        transaction_type, amount, subscriber_db_id,
    )
