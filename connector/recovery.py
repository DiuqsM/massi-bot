"""
Massi-Bot Connector — Error Recovery Sweep

When the bot errors out mid-pipeline it saves `sub.last_error_at` +
`sub.unrecovered_inbound` and goes silent (per feedback_crash_silence.md —
never send fake apology fallbacks). This module is responsible for the
automatic catch-up once the error is fixed:

  * Startup hook — runs once per container start, checks every sub with
    `last_error_at IS NOT NULL` and attempts recovery.
  * Periodic loop — every 60s repeats the scan so stuck subs that wait
    through a deploy-cycle still get picked up.

Recovery for a single sub:
  1. If `recovery_manual_only` → skip (e.g., purchase errors — money risk,
     never auto-retried).
  2. If `recovery_next_attempt_at > now` → skip (backoff window).
  3. Pull chat history from platform API (source of truth) — filter to
     fan-sent messages with created_at > last_successful_bot_message_at.
  4. Union with any `unrecovered_inbound` cached at crash time (catches
     webhooks that never wrote to our state).
  5. Acquire per-sub lock (same lock normal webhook handlers use).
  6. Run normal orchestrator pipeline with `recovery_context` injected into
     context dict — the agent sees its own silence duration + N messages
     during silence but receives NO behavioral rule (no forced apology).
  7. On success: clear all error fields, send `alert_bot_error_resolved`.
     On failure: bump retry counter, set backoff, re-alert with `[retry #N]`.

Backoff schedule: 60s → 5m → 30m → hold (no further auto-retry, but the
next fan inbound naturally triggers normal pipeline).
"""

from __future__ import annotations

import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from persistence.supabase_client import get_client
from persistence.subscriber_store import load_subscriber, save_subscriber

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Error → stuck-state marker (called from connector exception handlers)
# ─────────────────────────────────────────────

def mark_stuck(
    sub: Any,
    operation: str,
    error: BaseException,
    *,
    inbound_text: str = "",
    manual_only: bool = False,
) -> None:
    """
    Mark a subscriber as stuck on error. Caller is still responsible for
    firing the Telegram alert and for save_subscriber().

    Args:
        sub: Subscriber object (mutated in-place)
        operation: Short pipeline-op name (handle_messages_received, etc.)
        error: Caught exception
        inbound_text: Fan-visible text that triggered the crash
        manual_only: True for purchase-path errors — blocks auto-retry
    """
    import traceback
    now_iso = datetime.now().isoformat(timespec="seconds")
    try:
        sub.last_error_at = now_iso
        sub.last_error_context = {
            "operation": operation,
            "error_type": type(error).__name__,
            "error_msg": str(error)[:500],
            "tb_snippet": "\n".join(traceback.format_exc().strip().split("\n")[-10:])[:2000],
        }
        if inbound_text:
            if sub.unrecovered_inbound is None:
                sub.unrecovered_inbound = []
            sub.unrecovered_inbound.append({
                "text": inbound_text[:2000],
                "received_at": now_iso,
            })
        if manual_only:
            sub.recovery_manual_only = True
    except Exception as e:
        logger.warning("mark_stuck() internal failure: %s", e)


def clear_stuck(sub: Any) -> None:
    """Clear stuck state after successful recovery or normal successful turn."""
    try:
        sub.last_error_at = None
        sub.last_error_context = None
        sub.unrecovered_inbound = []
        sub.recovery_attempts = 0
        sub.recovery_next_attempt_at = None
        sub.recovery_manual_only = False
    except Exception as e:
        logger.warning("clear_stuck() internal failure: %s", e)


# ─────────────────────────────────────────────
# Platform API fetch — source of truth
# ─────────────────────────────────────────────

def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        return dt
    except Exception:
        return None


async def fetch_fan_messages_since_of(
    chat_id: str,
    since_iso: Optional[str],
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    Fetch fan-sent messages from OF chat since given timestamp.
    Returns [{text, msg_id, sent_at}] sorted oldest → newest.
    """
    account_id = os.environ.get("OFAPI_ACCOUNT_ID", "")
    api_key = os.environ.get("OFAPI_KEY", "")
    base = os.environ.get("OFAPI_BASE", "https://app.onlyfansapi.com")
    if not account_id or not api_key:
        return []
    url = f"{base}/api/{account_id}/chats/{chat_id}/messages"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {api_key}"},
                params={"limit": limit},
            )
        if resp.status_code != 200:
            logger.warning("OF fetch_fan_messages_since %d: %s", resp.status_code, resp.text[:200])
            return []
        body = resp.json()
    except Exception as e:
        logger.warning("OF fetch_fan_messages_since exception: %s", e)
        return []

    # Response shape: {data: {messages: [...]}} or similar — handle common variants
    messages = []
    if isinstance(body, dict):
        data = body.get("data") or body
        if isinstance(data, dict):
            messages = data.get("messages") or data.get("list") or []
        elif isinstance(data, list):
            messages = data
    elif isinstance(body, list):
        messages = body

    since_dt = _parse_iso(since_iso)
    out = []
    for m in messages:
        # Identify fan-sent: fromUser.id != creator_id. The webhook payload we
        # receive already filters to fan messages, so we rely on "isFromUser"
        # flags when present, or falsy fromBot flags.
        is_from_bot = bool(m.get("isFromBot")) or bool((m.get("fromUser") or {}).get("isCreator"))
        if is_from_bot:
            continue
        created_at = m.get("createdAt") or m.get("created_at") or ""
        created_dt = _parse_iso(created_at)
        if since_dt and created_dt and created_dt <= since_dt:
            continue
        text = (m.get("text") or m.get("body") or "").strip()
        if not text:
            continue
        out.append({
            "text": text,
            "msg_id": str(m.get("id") or m.get("uuid") or ""),
            "sent_at": created_at,
        })
    # Sort oldest → newest
    out.sort(key=lambda x: x.get("sent_at", ""))
    return out


async def fetch_fan_messages_since_fanvue(
    creator_uuid: str,
    sender_uuid: str,
    since_iso: Optional[str],
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Fetch fan-sent messages from Fanvue chat since given timestamp."""
    if not creator_uuid or not sender_uuid:
        return []
    # Resolve OAuth token via token_manager; fall back to any stored token if UUID mismatches
    try:
        from connector.token_manager import token_manager as _tm
        try:
            token = await _tm.get_access_token(creator_uuid)
        except Exception:
            token = await _tm.get_access_token("")
    except Exception as e:
        logger.warning("Fanvue token fetch failed: %s", e)
        return []
    if not token:
        return []

    api_base = os.environ.get("FANVUE_API_BASE", "https://api.fanvue.com")
    api_version = os.environ.get("FANVUE_API_VERSION", "2025-06-26")
    url = f"{api_base}/chats/{sender_uuid}/messages"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Fanvue-API-Version": api_version,
                },
                params={"size": limit, "markAsRead": "false"},
            )
        if resp.status_code != 200:
            logger.warning("Fanvue fetch_fan_messages_since %d: %s", resp.status_code, resp.text[:200])
            return []
        body = resp.json()
    except Exception as e:
        logger.warning("Fanvue fetch_fan_messages_since exception: %s", e)
        return []

    messages = body.get("results") or body.get("data") or (body if isinstance(body, list) else [])
    since_dt = _parse_iso(since_iso)
    out = []
    for m in messages:
        sender = m.get("sender") or {}
        sender_id = sender.get("uuid") or m.get("senderUuid") or ""
        # Fan-sent = sender.uuid == the chat's "sender_uuid" (the fan's uuid)
        if sender_id and sender_id != sender_uuid:
            continue
        created_at = m.get("createdAt") or m.get("created_at") or ""
        created_dt = _parse_iso(created_at)
        if since_dt and created_dt and created_dt <= since_dt:
            continue
        text = (m.get("text") or m.get("body") or "").strip()
        if not text:
            continue
        out.append({
            "text": text,
            "msg_id": str(m.get("uuid") or m.get("id") or ""),
            "sent_at": created_at,
        })
    out.sort(key=lambda x: x.get("sent_at", ""))
    return out


# ─────────────────────────────────────────────
# Backoff schedule
# ─────────────────────────────────────────────

_BACKOFF_SECONDS = [60, 300, 1800]  # 60s, 5m, 30m — beyond that: hold


def _next_backoff_iso(attempt_count: int) -> Optional[str]:
    """Returns ISO timestamp for next retry, or None if past cap."""
    if attempt_count >= len(_BACKOFF_SECONDS):
        return None  # hold — no more auto-retry
    delta = _BACKOFF_SECONDS[attempt_count]
    return (datetime.now() + timedelta(seconds=delta)).isoformat(timespec="seconds")


# ─────────────────────────────────────────────
# Sweep — platform-agnostic; caller provides platform-specific helpers
# ─────────────────────────────────────────────

async def run_recovery_sweep(
    platform: str,
    model_context_lookup: Callable[[str], Tuple[str, Any]],
    # ↑ Given a model_id, returns (creator_uuid_or_empty, model_profile).
    #   For OF, creator_uuid is unused (empty string OK).
    orchestrator_process_message: Callable,
    get_avatar_fn: Callable,
    avatars_registry: Any,
    active_tier_count: int,
    execute_actions_fn: Callable,
    sub_lock_factory: Callable[[str], "asyncio.Lock"],
    send_alert_bot_error_resolved: Optional[Callable] = None,
    send_alert_bot_error: Optional[Callable] = None,
) -> int:
    """
    Scan all subscribers on this platform with `last_error_at IS NOT NULL`
    and attempt recovery for eligible ones.

    Returns: count of recoveries successfully run this pass.
    """
    try:
        db = get_client()
        result = db.table("subscribers").select(
            "platform, platform_user_id, model_id, qualifying_data, display_name, username"
        ).eq("platform", platform).execute()
    except Exception as e:
        logger.warning("recovery_sweep: failed to scan subscribers: %s", e)
        return 0

    rows = result.data or []
    now = datetime.now()
    recovered = 0

    for row in rows:
        qd = row.get("qualifying_data") or {}
        last_error_at = qd.get("last_error_at")
        if not last_error_at:
            continue
        manual_only = bool(qd.get("recovery_manual_only"))
        if manual_only:
            continue
        next_attempt_at = qd.get("recovery_next_attempt_at")
        next_dt = _parse_iso(next_attempt_at)
        if next_dt and next_dt > now:
            continue  # still in backoff window

        platform_user_id = row.get("platform_user_id", "")
        model_id = row.get("model_id", "")
        if not platform_user_id or not model_id:
            continue

        try:
            await _recover_single_sub(
                platform=platform,
                platform_user_id=platform_user_id,
                model_id=model_id,
                model_context_lookup=model_context_lookup,
                orchestrator_process_message=orchestrator_process_message,
                get_avatar_fn=get_avatar_fn,
                avatars_registry=avatars_registry,
                active_tier_count=active_tier_count,
                execute_actions_fn=execute_actions_fn,
                sub_lock_factory=sub_lock_factory,
                send_alert_bot_error_resolved=send_alert_bot_error_resolved,
                send_alert_bot_error=send_alert_bot_error,
            )
            recovered += 1
        except Exception as e:
            logger.exception("recovery_sweep: sub %s failed: %s", platform_user_id, e)

    if recovered:
        logger.info("Recovery sweep (%s): %d sub(s) attempted", platform, recovered)
    return recovered


async def _recover_single_sub(
    *,
    platform: str,
    platform_user_id: str,
    model_id: str,
    model_context_lookup: Callable,
    orchestrator_process_message: Callable,
    get_avatar_fn: Callable,
    avatars_registry: Any,
    active_tier_count: int,
    execute_actions_fn: Callable,
    sub_lock_factory: Callable,
    send_alert_bot_error_resolved: Optional[Callable],
    send_alert_bot_error: Optional[Callable],
) -> None:
    lock = sub_lock_factory(platform_user_id)
    async with lock:
        sub = load_subscriber(platform, platform_user_id, model_id)
        if not sub:
            return
        if not sub.last_error_at:
            return  # cleared between scan and recovery
        if sub.recovery_manual_only:
            return

        sub._platform = platform  # set platform marker for downstream
        creator_uuid, model_profile = model_context_lookup(model_id)

        # Fetch fan messages since last successful send.
        since = sub.last_successful_bot_message_at or sub.last_error_at
        fetched: List[Dict[str, Any]] = []
        if platform == "onlyfans":
            fetched = await fetch_fan_messages_since_of(platform_user_id, since)
        elif platform == "fanvue":
            fetched = await fetch_fan_messages_since_fanvue(creator_uuid, platform_user_id, since)

        # Union with cached unrecovered inbound (captured at crash site)
        cached = sub.unrecovered_inbound or []
        all_texts = []
        seen_msg_ids = set()
        # Platform-fetched first (source of truth), then cached only if not dup
        for m in fetched:
            mid = m.get("msg_id", "")
            if mid:
                seen_msg_ids.add(mid)
            all_texts.append(m.get("text", ""))
        for m in cached:
            # Cached entries don't have msg_id, can't dedupe against platform — include them
            # unless the text already appears verbatim
            t = (m.get("text") or "").strip()
            if t and t not in all_texts:
                all_texts.append(t)

        if not all_texts:
            # No messages to recover — fan hasn't said anything since silence
            # Clear the stuck flag without running the pipeline (nothing to say)
            clear_stuck(sub)
            save_subscriber(sub, platform, platform_user_id, model_id)
            logger.info("Recovery: sub %s had no unrecovered messages; cleared flags", platform_user_id)
            return

        # Compute silence duration for alert message + recovery context hint
        silence_gap_str = _format_gap(since)
        combined = "\n".join(all_texts)

        # Inject recovery context into the pipeline
        recovery_context = {
            "bot_gap_str": silence_gap_str,
            "msg_count": len(all_texts),
        }

        prior_attempts = sub.recovery_attempts or 0
        try:
            actions = await orchestrator_process_message(
                sub,
                combined,
                get_avatar_fn(avatars_registry, sub.persona_id),
                model_profile=model_profile,
                active_tier_count=active_tier_count,
                recovery_context=recovery_context,
            )
            # Success: run actions, clear state
            save_subscriber(sub, platform, platform_user_id, model_id)
            if actions:
                if platform == "fanvue":
                    await execute_actions_fn(
                        actions, platform_user_id, model_id, sub, creator_uuid=creator_uuid
                    )
                else:
                    await execute_actions_fn(actions, platform_user_id, model_id, sub)

            clear_stuck(sub)
            save_subscriber(sub, platform, platform_user_id, model_id)
            logger.info(
                "Recovery: sub %s recovered after %d prior attempt(s); ran %d actions",
                platform_user_id, prior_attempts, len(actions) if actions else 0,
            )
            if send_alert_bot_error_resolved:
                try:
                    await send_alert_bot_error_resolved(
                        operation="recovery_sweep",
                        sub=sub,
                        platform=platform,
                        retries=prior_attempts,
                        silence_duration_str=silence_gap_str,
                    )
                except Exception:
                    pass

        except Exception as e:
            # Recovery itself crashed — bump counter, set backoff, re-alert
            sub.recovery_attempts = prior_attempts + 1
            sub.recovery_next_attempt_at = _next_backoff_iso(sub.recovery_attempts)
            save_subscriber(sub, platform, platform_user_id, model_id)
            logger.exception(
                "Recovery attempt %d failed for sub %s: %s",
                sub.recovery_attempts, platform_user_id, e,
            )
            if send_alert_bot_error:
                try:
                    await send_alert_bot_error(
                        operation=f"recovery_retry#{sub.recovery_attempts}",
                        error=e,
                        sub=sub,
                        platform=platform,
                        inbound_snippet=combined[:200],
                        extra_context={
                            "silence_duration": silence_gap_str,
                            "next_attempt_at": sub.recovery_next_attempt_at or "HOLD (no further auto-retry)",
                        },
                    )
                except Exception:
                    pass


def _format_gap(since_iso: Optional[str]) -> str:
    if not since_iso:
        return "unknown"
    dt = _parse_iso(since_iso)
    if not dt:
        return "unknown"
    delta = datetime.now() - dt
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins / 60
    if hours < 24:
        return f"{hours:.1f}h"
    days = hours / 24
    return f"{days:.1f}d"


# ─────────────────────────────────────────────
# Background loop starter (called from connector startup hooks)
# ─────────────────────────────────────────────

async def recovery_loop(
    sweep_callable: Callable,
    interval_seconds: int = 60,
) -> None:
    """Run the platform-specific sweep_callable every `interval_seconds`."""
    logger.info("Recovery loop started (interval %ds)", interval_seconds)
    # Initial delay to let startup complete
    await asyncio.sleep(5)
    while True:
        try:
            await sweep_callable()
        except Exception as e:
            logger.exception("Recovery loop iteration error: %s", e)
        await asyncio.sleep(interval_seconds)
