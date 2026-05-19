"""
Massi-Bot - OnlyFans Connector (OnlyFansAPI.com v2 webhook format)

FastAPI app (port 8001) that:
  1. Receives all OnlyFansAPI.com webhook events on a single endpoint
  2. Verifies HMAC-SHA256 signatures (Signature: <hex>)
  3. Dispatches on the "event" field in the payload
  4. Loads/saves subscriber state from Supabase
  5. Feeds events into the single-agent orchestrator
  6. Executes BotActions with mandatory delays

Key difference from Fanvue:
  - Auth is a static API key (no OAuth flow needed)
  - Prices are in DOLLARS — pass through unchanged from engine output
  - Signature header is just "Signature: <hex>" (no timestamp prefix)
  - Single unified webhook endpoint, not per-event routes

Run with: uvicorn connector.of_connector:app --port 8001
"""

import os
import re
import sys
import hmac
import json
import random
import hashlib
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Optional, Dict

import redis as _redis_lib
import httpx
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'engine'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.models import Subscriber, BotAction, SubState
from engine.avatars import AvatarConfig
from connector.init_helpers import load_avatars, build_attribution, get_avatar
from persistence.subscriber_store import (
    load_subscriber, create_subscriber, save_subscriber, record_transaction,
)
from persistence.content_store import get_bundle_by_id
from persistence.supabase_client import get_client as get_supabase
from persistence.model_profile import load_model_profile
from agents.orchestrator import process_message as orchestrator_process_message
from agents.orchestrator import process_purchase as orchestrator_process_purchase
from agents.orchestrator import process_new_subscriber as orchestrator_process_new_subscriber
from admin_bot.alerts import alert_purchase, alert_whale_escalation

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ─────────────────────────────────────────────
# Sentry (optional — only initializes if SENTRY_DSN is set)
# ─────────────────────────────────────────────

_SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if _SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            traces_sample_rate=0.1,
            environment=os.environ.get("ENVIRONMENT", "production"),
        )
        logger.info("Sentry initialized for of_connector")
    except Exception as _sentry_err:
        logger.warning("Sentry init failed: %s", _sentry_err)

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

PLATFORM = "onlyfans"

# ─────────────────────────────────────────────
# Module state (replaces BotController)
# ─────────────────────────────────────────────

_avatars: Dict[str, AvatarConfig] = {}
_attribution = None  # Optional[AttributionEngine]
_model_id: Optional[str] = None
_model_profile = None  # Loaded from Supabase models table
_active_tier_count = 6  # Per-model active tier count (from profile_json)
_default_avatar = "luxury_baddie"  # Fallback avatar when persona_id is empty

# Per-subscriber lock + message queue (same pattern as Fanvue connector)
_sub_locks: Dict[str, asyncio.Lock] = {}
_sub_queued_messages: Dict[str, list] = {}
_processed_message_ids: Dict[str, bool] = {}  # message dedup cache (max 500)

# Adaptive settle window — wait for fan to stop typing before starting pipeline.
# Tracks the timestamp of the most recent message per subscriber so we can extend
# the wait whenever a new message arrives during the settle window.
_sub_last_msg_time: Dict[str, float] = {}


def _settle_initial_seconds() -> float:
    try:
        return float(os.environ.get("MESSAGE_SETTLE_INITIAL_SECONDS", "8"))
    except ValueError:
        return 8.0


def _settle_extension_seconds() -> float:
    try:
        return float(os.environ.get("MESSAGE_SETTLE_EXTENSION_SECONDS", "5"))
    except ValueError:
        return 5.0


def _settle_max_seconds() -> float:
    try:
        return float(os.environ.get("MESSAGE_SETTLE_MAX_SECONDS", "30"))
    except ValueError:
        return 30.0


async def _wait_for_settle(platform_user_id: str, chat_id: str) -> None:
    """
    Wait for the fan to finish typing before we start the pipeline.

    Initial wait: 8s (configurable). If a new message arrives during the wait,
    extend the timer by 5s (configurable) from that new message's arrival time.
    Hard cap: 30s total (configurable) regardless of how many messages arrive.

    Typing indicator is sent during this window so the fan sees "typing..." and
    the delay feels like the model reading + composing a thoughtful reply.
    """
    import time as _time
    initial = _settle_initial_seconds()
    extension = _settle_extension_seconds()
    max_total = _settle_max_seconds()

    start = _time.monotonic()
    first_msg_time = _sub_last_msg_time.get(platform_user_id, start)
    # Target wake time = first message + initial settle
    target_wake = first_msg_time + initial

    # Typing indicator fires throughout settle window
    typing_task = asyncio.create_task(maintain_typing(chat_id, max_total + 5))

    try:
        while True:
            now = _time.monotonic()
            elapsed_since_start = now - start
            if elapsed_since_start >= max_total:
                logger.debug("Settle window hit max cap (%.1fs) for %s", max_total, platform_user_id)
                return
            sleep_for = target_wake - now
            if sleep_for <= 0:
                # Check if a new message arrived since we last computed target
                latest_msg = _sub_last_msg_time.get(platform_user_id, first_msg_time)
                if latest_msg > first_msg_time:
                    # New message arrived during the wait — extend
                    first_msg_time = latest_msg
                    target_wake = latest_msg + extension
                    continue
                # No new messages, settle complete
                return
            await asyncio.sleep(min(sleep_for, 1.0))  # Sleep in 1s chunks so we can react to new msgs
            # After sleep, check if new message bumped target
            latest_msg = _sub_last_msg_time.get(platform_user_id, first_msg_time)
            if latest_msg > first_msg_time:
                first_msg_time = latest_msg
                target_wake = latest_msg + extension
    finally:
        typing_task.cancel()


def _get_sub_lock(fan_id: str) -> asyncio.Lock:
    if fan_id not in _sub_locks:
        _sub_locks[fan_id] = asyncio.Lock()
    return _sub_locks[fan_id]


def _is_engine_paused() -> bool:
    """Check Redis flag set by admin bot /pause command."""
    try:
        r = _redis_lib.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"), decode_responses=True)
        return bool(r.get("engine:paused"))
    except Exception:
        return False


def _get_model_id() -> str:
    model_id = os.environ.get("OF_MODEL_ID", "")
    if not model_id:
        raise RuntimeError("OF_MODEL_ID environment variable not set")
    return model_id


async def _check_whale_escalation(sub, platform: str) -> None:
    """U10: Fire tiered whale escalation alert if thresholds are crossed."""
    score = sub.whale_score
    total = sub.spending.total_spent
    highest = sub.spending.highest_single_purchase
    trigger = None
    if score >= 50:
        trigger = "score"
    elif highest >= 50:
        trigger = "single_purchase"
    elif total >= 150:
        trigger = "total_spent"
    if trigger:
        await alert_whale_escalation(
            platform=platform, username=sub.username, sub_id=sub.sub_id,
            whale_score=score, total_spent=total, highest_purchase=highest,
            trigger=trigger,
        )


# ─────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────

app = FastAPI(title="Massi-Bot OnlyFans Connector", version="2.0.0", redirect_slashes=False)


@app.on_event("startup")
async def startup():
    global _model_id, _model_profile, _avatars, _attribution, _active_tier_count, _default_avatar
    _model_id = os.environ.get("OF_MODEL_ID", "")
    _avatars = load_avatars()
    _attribution = build_attribution(
        os.environ.get("OF_IG_MAP", "{}"), _avatars
    )
    if _model_id:
        _model_profile = load_model_profile(_model_id)
        try:
            from persistence.supabase_client import get_client as get_supabase
            db = get_supabase()
            result = db.table("models").select("profile_json").eq("id", _model_id).limit(1).execute()
            if result.data:
                pj = result.data[0].get("profile_json") or {}
                _active_tier_count = pj.get("active_tier_count", 6)
                _default_avatar = pj.get("default_avatar", "luxury_baddie")
        except Exception:
            pass
    # Pre-warm sentence-transformer encoder (avoids 3s cold-start on first fan message)
    try:
        from llm.memory_store import prewarm_encoder
        prewarm_encoder()
    except Exception:
        pass

    # Start PPV auto-cleanup sweep (6h default abandonment, configurable via PPV_ABANDONMENT_HOURS)
    try:
        from connector.ppv_cleanup import start_sweep_loop
        asyncio.create_task(start_sweep_loop(PLATFORM, delete_of_message))
    except Exception as e:
        logger.warning("PPV cleanup sweep failed to start: %s", e)

    # Error-recovery sweep: startup pass + 60s background loop
    try:
        from connector.recovery import run_recovery_sweep, recovery_loop
        from admin_bot.error_alerts import alert_bot_error, alert_bot_error_resolved

        def _model_ctx(_mid):
            return ("", _model_profile)

        async def _of_sweep():
            await run_recovery_sweep(
                platform=PLATFORM,
                model_context_lookup=_model_ctx,
                orchestrator_process_message=orchestrator_process_message,
                get_avatar_fn=get_avatar,
                avatars_registry=_avatars,
                active_tier_count=_active_tier_count,
                execute_actions_fn=execute_actions,
                sub_lock_factory=_get_sub_lock,
                send_alert_bot_error_resolved=alert_bot_error_resolved,
                send_alert_bot_error=alert_bot_error,
            )

        # Startup pass
        asyncio.create_task(_of_sweep())
        # Periodic loop
        asyncio.create_task(recovery_loop(_of_sweep, interval_seconds=60))
        logger.info("Recovery sweep wired on OF (startup + 60s loop)")
    except Exception as e:
        logger.warning("Recovery sweep failed to start: %s", e)

    logger.info("OnlyFans connector started (model_id=%s, profile=%s, avatars=%d)",
                _model_id, _model_profile.stage_name if _model_profile else "none", len(_avatars))


# ─────────────────────────────────────────────
# Signature verification
# ─────────────────────────────────────────────

def verify_signature(body: bytes, sig_header: str) -> None:
    """
    Verify OnlyFansAPI.com HMAC-SHA256 signature.
    Header: Signature: <hex-encoded-hmac-sha256-of-body>
    """
    if not sig_header:
        raise HTTPException(status_code=403, detail="Missing Signature header")

    secret = os.environ["OFAPI_WEBHOOK_SECRET"]
    expected = hmac.new(
        secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(sig_header, expected):
        raise HTTPException(status_code=403, detail="Invalid signature")


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    """Strip HTML tags from OF message text."""
    return _HTML_TAG_RE.sub("", text).strip()


def _get_or_load_subscriber(
    platform_user_id: str,
    model_id: str,
    username: str = "",
    display_name: str = "",
) -> tuple[Subscriber, bool]:
    sub = load_subscriber(PLATFORM, platform_user_id, model_id)
    if sub is None:
        sub = create_subscriber(
            PLATFORM, platform_user_id, model_id,
            username=username, display_name=display_name,
        )
        sub._platform = PLATFORM
        return sub, True
    sub._platform = PLATFORM
    return sub, False


# ─────────────────────────────────────────────
# Outbound OnlyFans API calls
# ─────────────────────────────────────────────

def _of_api_base() -> str:
    account_id = os.environ["OFAPI_ACCOUNT_ID"]
    base = os.environ.get("OFAPI_BASE", "https://app.onlyfansapi.com")
    return f"{base}/api/{account_id}"


def _of_headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['OFAPI_KEY']}",
        "Content-Type": "application/json",
    }


async def send_typing_indicator(chat_id: str) -> None:
    """Send 'typing...' indicator to a fan's chat. Lasts ~4 seconds per call. Free."""
    try:
        url = f"{_of_api_base()}/chats/{chat_id}/typing"
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, headers=_of_headers())
    except Exception:
        pass  # Best-effort — never crash on typing indicator failure


async def maintain_typing(chat_id: str, duration_seconds: float) -> None:
    """Keep typing indicator alive for a duration by re-sending every 3 seconds."""
    import time
    start = time.monotonic()
    while (time.monotonic() - start) < duration_seconds:
        await send_typing_indicator(chat_id)
        await asyncio.sleep(3)


async def send_of_message(chat_id: str, text: str) -> None:
    """Send a plain text message via OnlyFansAPI.com.
    Includes 1-retry on network timeout + catches all exceptions so callers never crash."""
    url = f"{_of_api_base()}/chats/{chat_id}/messages"
    last_err = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json={"text": text}, headers=_of_headers())
            if resp.status_code not in (200, 201):
                logger.error("OF send_message failed %d: %s | text_sent=%s", resp.status_code, resp.text[:200], text[:200])
                # Alert admin on platform rejection so they can manually intervene
                try:
                    from admin_bot.alerts import _send
                    asyncio.create_task(_send(
                        f"⚠️ <b>OF Message Rejected</b>\n"
                        f"Chat: <code>{chat_id}</code>\n"
                        f"Status: {resp.status_code}\n"
                        f"Text: <i>{text[:150]}</i>\n"
                        f"Error: {resp.text[:150]}"
                    ))
                except Exception:
                    pass
            else:
                logger.debug("Sent OF message to chat %s", chat_id)
            return
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last_err = e
            if attempt == 0:
                logger.warning("OF send_message timeout/network err (attempt 1), retrying in 2s: %s", e)
                await asyncio.sleep(2)
                continue
            logger.error("OF send_message failed after retry: %s", e)
            return
        except Exception as e:
            logger.error("OF send_message unexpected error: %s", e)
            return


async def send_of_ppv(
    chat_id: str,
    caption: str,
    of_media_ids: list[str] | str,
    price_dollars: float,
) -> Optional[str]:
    """
    Send a PPV message via OnlyFansAPI.com.
    Price is in DOLLARS — do NOT multiply by 100 (opposite of Fanvue).
    Accepts a single ID string or a list of IDs for multi-media bundles.

    Returns the OnlyFans message ID on success (for later deletion), None on failure.
    """
    if isinstance(of_media_ids, str):
        of_media_ids = [of_media_ids]
    url = f"{_of_api_base()}/chats/{chat_id}/messages"
    payload = {
        "text": caption,
        "price": price_dollars,
        "mediaFiles": of_media_ids,
    }
    # PPV sends get aggressive retry — a failed PPV is a lost sale.
    # Retry on: timeouts, network errors, AND server errors (500/502/503/504).
    # Backoff: 2s, 10s, 30s (3 retries total).
    _PPV_RETRY_DELAYS = [2, 10, 30]
    resp = None
    for attempt in range(len(_PPV_RETRY_DELAYS) + 1):
        try:
            async with httpx.AsyncClient(timeout=40.0) as client:
                resp = await client.post(url, json=payload, headers=_of_headers())
            if resp.status_code in (200, 201):
                break  # Success
            if resp.status_code >= 500 and attempt < len(_PPV_RETRY_DELAYS):
                delay = _PPV_RETRY_DELAYS[attempt]
                logger.warning("OF send_ppv server error %d (attempt %d), retrying in %ds: %s",
                               resp.status_code, attempt + 1, delay, resp.text[:100])
                await asyncio.sleep(delay)
                continue
            break  # 4xx or exhausted retries
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            if attempt < len(_PPV_RETRY_DELAYS):
                delay = _PPV_RETRY_DELAYS[attempt]
                logger.warning("OF send_ppv timeout/network err (attempt %d), retrying in %ds: %s",
                               attempt + 1, delay, e)
                await asyncio.sleep(delay)
                continue
            logger.error("OF send_ppv failed after %d retries for chat %s: %s", attempt + 1, chat_id, e)
            return None
        except Exception as e:
            logger.error("OF send_ppv unexpected error for chat %s: %s", chat_id, e)
            return None
    if resp is None:
        return None
    if resp.status_code not in (200, 201):
        logger.error(
            "OF send_ppv failed %d for chat %s after all retries: %s",
            resp.status_code, chat_id, resp.text[:200],
        )
        try:
            from admin_bot.alerts import _send
            asyncio.create_task(_send(
                f"🚨 <b>OF PPV Send FAILED</b>\n"
                f"Chat: <code>{chat_id}</code>\n"
                f"Price: ${price_dollars:.2f}\n"
                f"Status: {resp.status_code}\n"
                f"Error: {resp.text[:150]}\n\n"
                f"PPV was NOT delivered. Fan may be waiting."
            ))
        except Exception:
            pass
        return None
    logger.info("Sent OF PPV $%.2f (%d media) to chat %s", price_dollars, len(of_media_ids), chat_id)
    try:
        data = resp.json()
        # OnlyFansAPI returns the created message — extract its ID
        msg = data.get("data") or data
        return str(msg.get("id") or msg.get("message_id") or "")
    except Exception:
        return None


async def delete_of_message(chat_id: str, message_id: str) -> bool:
    """
    Delete an OnlyFans message via OnlyFansAPI.com.
    Subject to OnlyFans 24-hour hard time limit — cannot delete messages older than 24h.
    """
    if not chat_id or not message_id:
        return False
    try:
        url = f"{_of_api_base()}/chats/{chat_id}/messages/{message_id}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.delete(url, headers=_of_headers())
        if resp.status_code in (200, 204):
            logger.info("OF delete OK: msg=%s chat=%s", message_id, chat_id)
            return True
        logger.warning("OF delete failed %d: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        logger.warning("OF delete error: %s", e)
        return False


# ─────────────────────────────────────────────
# Action executor
# ─────────────────────────────────────────────

async def execute_actions(
    actions: list[BotAction],
    chat_id: str,
    model_id: str,
    sub: Subscriber,
) -> None:
    """Execute BotActions against the OnlyFans API. Price in dollars (pass-through).

    Also handles the GFE-kick force-delete flag: if sub._force_delete_pending_ppv is set
    (orchestrator flagged a PPV for immediate deletion when kicking to GFE), we DELETE it
    before running the action list so the fan experiences a clean re-entry.
    """
    # GFE-kick force-delete: happens BEFORE sending actions so the chat is cleaned up first
    pending_to_delete = getattr(sub, "_force_delete_pending_ppv", None)
    if pending_to_delete:
        msg_id = pending_to_delete.get("platform_msg_id")
        if msg_id:
            try:
                ok = await delete_of_message(chat_id, msg_id)
                logger.info("GFE-kick force-delete: msg=%s sub=%s success=%s",
                            msg_id, chat_id, ok)
            except Exception as e:
                logger.warning("GFE-kick force-delete failed: %s", e)
        sub._force_delete_pending_ppv = None

    def _mark_sent():
        """Stamp recovery-tracking timestamp after any successful fan-visible send."""
        sub.last_successful_bot_message_at = datetime.now().isoformat(timespec="seconds")

    for action in actions:
      # Per-action try/except: one action failing must NOT kill subsequent actions.
      # A timed-out heads-up shouldn't prevent the PPV drop that follows.
      try:
        # Belt-and-suspenders: continuation PPVs should NEVER have a multi-minute jitter.
        # If a continuation bundle somehow has >60s delay, cap it.
        if action.action_type == "send_ppv" and action.metadata:
            tier_meta = action.metadata.get("tier", "")
            if tier_meta == "continuation" and action.delay_seconds > 60:
                logger.warning("Continuation PPV had %ds delay — capping to 10s (pre-taken content, no realness needed)",
                               action.delay_seconds)
                action.delay_seconds = random.randint(5, 10)

        if action.delay_seconds > 0:
            # Show "typing..." during the mandatory delay
            if action.delay_seconds > 2:
                typing_task = asyncio.create_task(
                    maintain_typing(chat_id, action.delay_seconds - 1)
                )
                await asyncio.sleep(action.delay_seconds)
                typing_task.cancel()
            else:
                await asyncio.sleep(action.delay_seconds)

        if action.action_type == "send_message" and action.message:
            await send_of_message(chat_id, action.message)
            _mark_sent()

        elif action.action_type == "send_ppv":
            # Custom payment PPV — use a continuation photo as placeholder
            if (action.metadata or {}).get("use_continuation_placeholder") or (action.metadata or {}).get("tier") == "custom":
                try:
                    db = get_supabase()
                    r = db.table("content_catalog").select("of_media_id").eq("model_id", model_id).eq("tier", 0).eq("media_type", "photo").limit(1).execute()
                    placeholder_id = (r.data[0]["of_media_id"] if r.data else None)
                    if placeholder_id:
                        sent_msg_id = await send_of_ppv(
                            chat_id,
                            action.ppv_caption or "custom order payment -- unlock to confirm",
                            [placeholder_id], action.ppv_price or 0,
                        )
                        logger.info("Custom payment PPV sent: $%.2f to %s", action.ppv_price, chat_id)
                        _mark_sent()
                    else:
                        logger.warning("No continuation photo for custom PPV placeholder — sending caption only")
                        if action.ppv_caption:
                            await send_of_message(chat_id, action.ppv_caption)
                            _mark_sent()
                except Exception as e:
                    logger.warning("Custom PPV send failed: %s", e)
                continue

            bundle_info = None
            if action.content_id:
                bundle_info = get_bundle_by_id(action.content_id, model_id)

            # Fallback: look up by tier if no content_id was set
            if not bundle_info and action.metadata:
                tier_str = action.metadata.get("tier", "")
                if tier_str == "continuation":
                    try:
                        db = get_supabase()
                        r = db.table("content_catalog").select("*").eq("model_id", model_id).eq("tier", 0).execute()
                        candidates = r.data or []
                        if not candidates:
                            r = db.table("content_catalog").select("*").eq("model_id", model_id).eq("tier", 1).execute()
                            candidates = r.data or []
                        if candidates:
                            sent_ids = getattr(sub, 'sent_captions', []) or []
                            unsent = [c for c in candidates if c.get("bundle_id") not in sent_ids]
                            if not unsent:
                                unsent = candidates
                            bundle_info = random.choice(unsent)
                            logger.info("Continuation image picked: %s (from %d candidates)",
                                        bundle_info.get("bundle_id"), len(candidates))
                    except Exception as e:
                        logger.warning("Continuation content lookup failed: %s", e)
                else:
                    tier_num = int(tier_str.split("_")[-1]) if tier_str.startswith("tier_") else 0
                    if tier_num:
                        try:
                            # Session-scoped lookup — stay in fan's current session until completion
                            current_session = getattr(sub, "current_session_number", 1) or 1
                            db = get_supabase()
                            r = (db.table("content_catalog")
                                   .select("*")
                                   .eq("model_id", model_id)
                                   .eq("tier", tier_num)
                                   .eq("session_number", current_session)
                                   .execute())
                            if r.data:
                                bundle_info = r.data[0]
                                bundle_id = bundle_info.get("bundle_id")
                                all_media = [row["of_media_id"] for row in r.data
                                             if row.get("of_media_id") and row.get("bundle_id") == bundle_id]
                                bundle_info["_all_of_media_ids"] = all_media
                                logger.info("Session %d tier %d bundle found: %s (%d media)",
                                            current_session, tier_num, bundle_id, len(all_media))
                            else:
                                logger.warning("No content for session %d tier %d — session gap for sub %s",
                                               current_session, tier_num, chat_id)
                        except Exception as e:
                            logger.warning("Tier lookup failed: %s", e)

            # Collect OF media IDs — prefer full bundle, fall back to single
            of_media_ids = (bundle_info or {}).get("_all_of_media_ids") or []
            if not of_media_ids:
                single_id = (bundle_info or {}).get("of_media_id") or (bundle_info or {}).get("b2_key")
                if single_id:
                    of_media_ids = [single_id]

            if not of_media_ids:
                logger.warning(
                    "No OF media ID for bundle %s — sending caption only",
                    action.content_id,
                )
                if action.ppv_caption:
                    await send_of_message(chat_id, action.ppv_caption)
                    _mark_sent()
            else:
                price_dollars = action.ppv_price or 0.0
                sent_msg_id = await send_of_ppv(chat_id, action.ppv_caption, of_media_ids, price_dollars)
                _mark_sent()
                # Track pending PPV for 6h auto-delete (only selling tiers 1-6, not continuation)
                tier_meta = (action.metadata or {}).get("tier", "")
                if sent_msg_id and tier_meta.startswith("tier_"):
                    try:
                        tier_int = int(tier_meta.split("_")[-1])
                    except ValueError:
                        tier_int = 0
                    if tier_int > 0:
                        sub.pending_ppv = {
                            "platform_msg_id": sent_msg_id,
                            "tier": tier_int,
                            "sent_at": datetime.now().isoformat(),
                            "bundle_id": (bundle_info or {}).get("bundle_id", ""),
                            "price_dollars": price_dollars,
                            "platform": PLATFORM,
                            "platform_user_id": chat_id,
                            "model_id": model_id,
                        }
                        # Persist immediately — any queued-message drain that reloads from DB
                        # must see the pending_ppv flag, or the no-re-drop rule will break.
                        try:
                            save_subscriber(sub, PLATFORM, chat_id, model_id)
                        except Exception as e:
                            logger.warning("save_subscriber after PPV send failed: %s", e)
                        logger.info("Pending PPV tracked: tier=%d msg=%s for sub %s",
                                    tier_int, sent_msg_id, chat_id)

        elif action.action_type == "send_free" and action.message:
            await send_of_message(chat_id, action.message)
            _mark_sent()

        elif action.action_type == "flag":
            logger.info("FLAG action for chat %s: %s", chat_id, action.metadata)
      except Exception as exc:
        logger.exception("Action failed (%s) — continuing with remaining actions: %s",
                         action.action_type, exc)


# ─────────────────────────────────────────────
# Event handlers (called as background tasks)
# ─────────────────────────────────────────────

async def _handle_message(payload: dict, model_id: str) -> None:
    """Handle messages.received — new inbound message from a fan.
    Uses per-subscriber locking + message queuing (same pattern as Fanvue connector)."""
    from_user = payload.get("fromUser") or {}
    platform_user_id: str = str(from_user.get("id") or payload.get("user_id", ""))
    username: str = from_user.get("username", "")
    display_name: str = from_user.get("name", username)
    chat_id: str = platform_user_id
    raw_text: str = payload.get("text", "")
    message_text: str = strip_html(raw_text).strip()

    if not platform_user_id or not message_text:
        logger.debug("messages.received ignored: missing user_id or text")
        return

    # Dedup: skip if we've already processed this exact message
    msg_id = str(payload.get("id", ""))
    if msg_id and msg_id in _processed_message_ids:
        logger.info(">>> DEDUP: skipping already-processed message %s", msg_id)
        return
    if msg_id:
        _processed_message_ids[msg_id] = True
        if len(_processed_message_ids) > 500:
            oldest = list(_processed_message_ids.keys())[:250]
            for k in oldest:
                del _processed_message_ids[k]

    # Record message arrival time for adaptive settle window tracking
    import time as _time
    _sub_last_msg_time[platform_user_id] = _time.monotonic()

    lock = _get_sub_lock(platform_user_id)

    # If lock is held, queue this message and return — the active handler will pick it up
    if lock.locked():
        if platform_user_id not in _sub_queued_messages:
            _sub_queued_messages[platform_user_id] = []
        _sub_queued_messages[platform_user_id].append(message_text)
        logger.info(">>> QUEUED message for %s (lock held): %s", platform_user_id, message_text[:50])
        return

    async with lock:
        # Adaptive settle window INSIDE the lock — otherwise concurrent handlers
        # each start their own settle and race to acquire, processing separately.
        # Holding the lock during settle forces new messages to queue.
        await _wait_for_settle(platform_user_id, chat_id)

        try:
            if _is_engine_paused():
                logger.info("Engine paused — dropping message from %s", platform_user_id)
                return

            sub, is_new = _get_or_load_subscriber(
                platform_user_id, model_id,
                username=username, display_name=display_name,
            )

            # Start typing indicator while agents process (settle already had its own typing)
            typing_task = asyncio.create_task(maintain_typing(chat_id, 20))

            try:
                if is_new:
                    if _attribution and message_text:
                        attr_result = _attribution.detect(messages=[message_text])
                        if attr_result.detected:
                            sub.persona_id = attr_result.persona_id or ""
                            sub.source_ig_account = attr_result.ig_handle or ""
                            sub.source_detected = True
                            logger.info(
                                "Keyword attribution for new sub %s: persona=%s",
                                platform_user_id, sub.persona_id,
                            )
                    if not sub.persona_id:
                        sub.persona_id = _default_avatar
                    avatar = get_avatar(_avatars, sub.persona_id)
                    actions = await orchestrator_process_new_subscriber(sub, avatar, model_profile=_model_profile, active_tier_count=_active_tier_count)
                else:
                    if not sub.persona_id:
                        sub.persona_id = _default_avatar

                    # Combine with any queued messages that arrived while we acquired the lock
                    combined_text = message_text
                    queued = _sub_queued_messages.pop(platform_user_id, [])
                    if queued:
                        combined_text = (combined_text + "\n" + "\n".join(queued)).strip()
                        logger.info(">>> Combined %d queued messages for %s", len(queued), platform_user_id)

                    avatar = get_avatar(_avatars, sub.persona_id)
                    actions = await orchestrator_process_message(sub, combined_text, avatar, model_profile=_model_profile, active_tier_count=_active_tier_count)
            finally:
                typing_task.cancel()  # Stop typing when agents are done

            # Before sending, check if more messages arrived during orchestrator processing.
            # Regenerate up to 2 times combining all queued messages. This prevents "the fan
            # answered my question but I responded as if they hadn't" race conditions.
            accumulated_text = combined_text if not is_new else ""
            regen_count = 0
            max_regens = 2
            # Cancellation tokens — if fan says any of these mid-gen, don't preserve the PPV
            _CANCEL_TOKENS = ("nvm", "nevermind", "never mind", "cancel", "wait", "hold on", "stop", "not yet", "later", "changed my mind")
            while not is_new and regen_count < max_regens:
                pre_send_queued = _sub_queued_messages.pop(platform_user_id, [])
                if not pre_send_queued:
                    break
                regen_count += 1
                accumulated_text = (accumulated_text + "\n" + "\n".join(pre_send_queued)).strip()
                logger.info(">>> REGENERATING (pass %d/%d): %d new msgs arrived -- recomposing for %s",
                            regen_count, max_regens, len(pre_send_queued), platform_user_id)
                # Capture PPV from prior output BEFORE regenerating
                prior_ppv_actions = [a for a in (actions or []) if a.action_type == "send_ppv"]
                new_msgs_lower = "\n".join(pre_send_queued).lower()
                is_cancellation = any(tok in new_msgs_lower for tok in _CANCEL_TOKENS)
                # Save in-memory tool side-effects (e.g. pending_custom_order written by
                # classify_custom_request) BEFORE reloading from DB, or the reload wipes them.
                try:
                    save_subscriber(sub, PLATFORM, platform_user_id, model_id)
                except Exception as se:
                    logger.warning("save before regen reload failed: %s", se)
                sub = load_subscriber(PLATFORM, platform_user_id, model_id) or sub
                avatar = get_avatar(_avatars, sub.persona_id)
                actions = await orchestrator_process_message(sub, accumulated_text, avatar, model_profile=_model_profile, active_tier_count=_active_tier_count)
                # If regen dropped a PPV that the prior pass produced, and the new msgs aren't a
                # cancellation, preserve the PPV. Fan often says things like "send the payment link"
                # after agreeing — the regen sees "fan is ready" and accidentally drops the PPV thinking
                # it's already sent.
                if prior_ppv_actions and not is_cancellation:
                    has_ppv_now = any(a.action_type == "send_ppv" for a in (actions or []))
                    if not has_ppv_now:
                        logger.warning(
                            ">>> Regen pass %d dropped PPV — preserving prior PPV action(s) for %s",
                            regen_count, platform_user_id,
                        )
                        actions = list(actions or []) + prior_ppv_actions

            save_subscriber(sub, PLATFORM, platform_user_id, model_id)
            await execute_actions(actions, chat_id, model_id, sub)

            # After executing (with delays), check for messages that arrived during send.
            # Post-gen sweep: fan often replies DURING our generation ("I appreciate that"
            # landing while agent is drafting next message). Wait 5s + resweep to merge
            # trickles into one pipeline run instead of firing back-to-back near-dup replies.
            post_send_queued = _sub_queued_messages.pop(platform_user_id, [])
            if post_send_queued:
                await asyncio.sleep(5)
                more = _sub_queued_messages.pop(platform_user_id, [])
                if more:
                    post_send_queued.extend(more)
                    logger.info(">>> Post-gen sweep merged %d extra messages for %s", len(more), platform_user_id)
                all_follow_up = "\n".join(post_send_queued)
                logger.info(">>> Processing %d post-send queued messages for %s",
                            len(post_send_queued), platform_user_id)
                # Save tool side-effects before reloading so reload doesn't wipe them
                try:
                    save_subscriber(sub, PLATFORM, platform_user_id, model_id)
                except Exception as se:
                    logger.warning("save before post-send reload failed: %s", se)
                sub = load_subscriber(PLATFORM, platform_user_id, model_id) or sub
                avatar = get_avatar(_avatars, sub.persona_id)
                actions = await orchestrator_process_message(sub, all_follow_up, avatar, model_profile=_model_profile, active_tier_count=_active_tier_count)
                save_subscriber(sub, PLATFORM, platform_user_id, model_id)
                await execute_actions(actions, chat_id, model_id, sub)

        except Exception as exc:
            logger.exception("Error handling messages.received from %s: %s", platform_user_id, exc)
            # Drain any queued messages into stuck-state BEFORE we lose them
            leftover = _sub_queued_messages.pop(platform_user_id, [])
            try:
                from connector.recovery import mark_stuck
                from admin_bot.error_alerts import alert_bot_error
                stuck_sub = None
                try:
                    stuck_sub = load_subscriber(PLATFORM, platform_user_id, model_id)
                except Exception:
                    pass
                if stuck_sub is not None:
                    stuck_sub._platform = PLATFORM
                    mark_stuck(stuck_sub, "handle_messages_received", exc, inbound_text=message_text)
                    for extra in leftover:
                        stuck_sub.unrecovered_inbound.append({"text": extra[:2000], "received_at": datetime.now().isoformat(timespec="seconds")})
                    try:
                        save_subscriber(stuck_sub, PLATFORM, platform_user_id, model_id)
                    except Exception as se:
                        logger.warning("Could not persist stuck state: %s", se)
                await alert_bot_error(
                    "handle_messages_received", exc,
                    sub=stuck_sub, platform=PLATFORM, model=model_id,
                    inbound_snippet=message_text[:300],
                    extra_context={"queued_messages": len(leftover)},
                )
            except Exception as alert_exc:
                logger.warning("Error alerting failed (non-fatal): %s", alert_exc)


async def _handle_new_subscriber(payload: dict, model_id: str) -> None:
    """Handle subscriptions.new — a fan just subscribed."""
    platform_user_id: str = str(payload.get("user_id", ""))
    replace_pairs: dict = payload.get("replacePairs", {})
    link_html: str = replace_pairs.get("{SUBSCRIBER_LINK}", "")
    username_match = re.search(r'onlyfans\.com/([^"\'>/]+)', link_html)
    username: str = username_match.group(1) if username_match else ""
    chat_id: str = platform_user_id

    if not platform_user_id:
        logger.warning("subscriptions.new: missing user_id in payload")
        return

    try:
        sub = create_subscriber(
            PLATFORM, platform_user_id, model_id,
            username=username, display_name=username,
        )
        avatar = get_avatar(_avatars, sub.persona_id)
        actions = await orchestrator_process_new_subscriber(sub, avatar, model_profile=_model_profile, active_tier_count=_active_tier_count)
        save_subscriber(sub, PLATFORM, platform_user_id, model_id)
        await execute_actions(actions, chat_id, model_id, sub)
    except Exception as exc:
        logger.exception("Error handling subscriptions.new %s: %s", platform_user_id, exc)
        try:
            from admin_bot.error_alerts import alert_bot_error
            await alert_bot_error(
                "handle_new_subscriber", exc, platform=PLATFORM, model=model_id,
                extra_context={"user_id": platform_user_id},
            )
        except Exception:
            pass


async def _handle_renewed(payload: dict, model_id: str) -> None:
    """Handle subscriptions.renewed — subscriber renewed."""
    platform_user_id: str = str(payload.get("user_id", ""))
    replace_pairs: dict = payload.get("replacePairs", {})
    price_str: str = replace_pairs.get("{PRICE}", "0").replace("$", "").replace(",", "")
    try:
        amount = float(price_str)
    except ValueError:
        amount = 0.0

    if not platform_user_id:
        return

    try:
        sub, _ = _get_or_load_subscriber(platform_user_id, model_id)
        record_transaction(sub.sub_id, model_id, "subscription", amount, PLATFORM)
        logger.info("OF renewal: %s $%.2f", platform_user_id, amount)
    except Exception as exc:
        logger.exception("Error handling subscriptions.renewed %s: %s", platform_user_id, exc)
        try:
            from admin_bot.error_alerts import alert_bot_error
            await alert_bot_error(
                "handle_renewed", exc, platform=PLATFORM, model=model_id,
                extra_context={"user_id": platform_user_id, "amount": amount},
            )
        except Exception:
            pass


async def _handle_ppv_unlocked(payload: dict, model_id: str) -> None:
    """Handle messages.ppv.unlocked — fan purchased a PPV message.
    Uses per-sub locking + GFE continuation detection + queued message drain."""
    platform_user_id: str = str(payload.get("user_id", ""))
    replace_pairs: dict = payload.get("replacePairs", {})
    amount_str: str = replace_pairs.get("{AMOUNT}", "0").replace("$", "").replace(",", "")
    try:
        amount_dollars = float(amount_str)
    except ValueError:
        amount_dollars = 0.0

    if not platform_user_id:
        return

    lock = _get_sub_lock(platform_user_id)
    async with lock:
        try:
            logger.info(">>> PURCHASE START: user=%s amount=$%.2f", platform_user_id, amount_dollars)
            sub, _ = _get_or_load_subscriber(platform_user_id, model_id)
            if not sub.persona_id:
                sub.persona_id = _default_avatar
            avatar = get_avatar(_avatars, sub.persona_id)

            # ── GFE Continuation Payment Detection ──
            if sub.gfe_continuation_pending:
                sub.gfe_continuation_pending = False
                sub.gfe_continuations_paid += 1
                logger.info(">>> GFE CONTINUATION PAID by %s ($%.2f) -- gate reset, continuations=%d",
                            platform_user_id, amount_dollars, sub.gfe_continuations_paid)
                actions = [BotAction(
                    action_type="send_message",
                    message=random.choice([
                        "you're back 🥰 ok where were we...",
                        "I knew you weren't gonna leave me 😏 now come here...",
                        "see?? that's what I like about you... you show up 💕 now let's keep going",
                        "ok NOW we're talking 😘 I missed you already lol",
                    ]),
                    delay_seconds=random.randint(8, 15),
                )]
                save_subscriber(sub, PLATFORM, platform_user_id, model_id)
                record_transaction(sub.sub_id, model_id, "gfe_continuation", amount_dollars, PLATFORM)
                await alert_purchase(PLATFORM, sub.username, amount_dollars)
                await execute_actions(actions, platform_user_id, model_id, sub)
                # Process any queued messages now that the gate is cleared
                queued = _sub_queued_messages.pop(platform_user_id, [])
                if queued:
                    combined = "\n".join(queued)
                    msg_actions = await orchestrator_process_message(sub, combined, avatar, model_profile=_model_profile, active_tier_count=_active_tier_count)
                    save_subscriber(sub, PLATFORM, platform_user_id, model_id)
                    await execute_actions(msg_actions, platform_user_id, model_id, sub)
                return

            # Custom payment detection — if pending_custom_order exists and amount roughly matches
            custom_order = getattr(sub, "pending_custom_order", None)
            if custom_order and custom_order.get("status") in ("pitched", "awaiting_admin_confirm"):
                quoted = custom_order.get("quoted_price", 0)
                if abs(amount_dollars - quoted) < 5.0:
                    logger.info("Custom payment detected: $%.2f matches quoted $%.2f for %s",
                                amount_dollars, quoted, platform_user_id)
                    from engine.custom_orders import mark_fan_paid
                    sub.pending_custom_order = mark_fan_paid(custom_order)
                    sub.gfe_message_count = 0  # Reset — fan is spending, don't paywall them
                    sub.pending_ppv = None  # Custom-payment PPV was the placeholder; fan paid, clear it
                    sub.custom_request_streak = 0
                    save_subscriber(sub, PLATFORM, platform_user_id, model_id)
                    record_transaction(sub.sub_id, model_id, "custom", amount_dollars, PLATFORM)
                    try:
                        from admin_bot.alerts import alert_custom_payment_claim
                        await alert_custom_payment_claim(sub, sub.pending_custom_order)
                    except Exception as e:
                        logger.warning("Custom payment alert failed: %s", e)
                    await send_of_message(platform_user_id,
                        "got it baby, let me verify real quick and then I'll start working on your custom for you")
                    return

            # Fan paid — clear pending_ppv so the auto-delete sweep leaves it alone
            if sub.pending_ppv:
                logger.info("Clearing pending_ppv (tier=%s) after purchase by %s",
                            sub.pending_ppv.get("tier"), platform_user_id)
                sub.pending_ppv = None
            # Reset custom request streak — fan is buying, they're engaged
            sub.custom_request_streak = 0

            actions = await orchestrator_process_purchase(sub, amount_dollars, avatar, model_profile=_model_profile, active_tier_count=_active_tier_count)

            save_subscriber(sub, PLATFORM, platform_user_id, model_id)
            record_transaction(sub.sub_id, model_id, "ppv", amount_dollars, PLATFORM)
            logger.info("OF PPV unlocked: %s $%.2f", platform_user_id, amount_dollars)
            await alert_purchase(PLATFORM, sub.username, amount_dollars)
            await _check_whale_escalation(sub, PLATFORM)

            if actions:
                await execute_actions(actions, platform_user_id, model_id, sub)
                save_subscriber(sub, PLATFORM, platform_user_id, model_id)

            # Process any fan messages that queued while we held the lock
            queued = _sub_queued_messages.pop(platform_user_id, [])
            if queued:
                logger.info(">>> Processing %d queued messages after purchase for %s", len(queued), platform_user_id)
                sub = load_subscriber(PLATFORM, platform_user_id, model_id) or sub
                avatar = get_avatar(_avatars, sub.persona_id)
                combined = "\n".join(queued)
                msg_actions = await orchestrator_process_message(sub, combined, avatar, model_profile=_model_profile, active_tier_count=_active_tier_count)
                save_subscriber(sub, PLATFORM, platform_user_id, model_id)
                await execute_actions(msg_actions, platform_user_id, model_id, sub)

            logger.info(">>> PURCHASE COMPLETE: ppv_count=%d total=$%.2f", sub.spending.ppv_count, sub.spending.total_spent)
        except Exception as exc:
            logger.exception("Error handling messages.ppv.unlocked %s: %s", platform_user_id, exc)
            # Purchase errors: mark manual_only so the auto-sweep doesn't re-run them
            try:
                from connector.recovery import mark_stuck
                from admin_bot.error_alerts import alert_bot_error
                stuck_sub = None
                try:
                    stuck_sub = load_subscriber(PLATFORM, platform_user_id, model_id)
                except Exception:
                    pass
                if stuck_sub is not None:
                    stuck_sub._platform = PLATFORM
                    mark_stuck(stuck_sub, "handle_ppv_unlocked", exc, manual_only=True)
                    try:
                        save_subscriber(stuck_sub, PLATFORM, platform_user_id, model_id)
                    except Exception:
                        pass
                await alert_bot_error(
                    "handle_ppv_unlocked", exc, sub=stuck_sub,
                    platform=PLATFORM, model=model_id,
                    extra_context={"amount_dollars": amount_dollars, "manual_confirm_required": True},
                )
            except Exception:
                pass


async def _handle_transaction(payload: dict, model_id: str) -> None:
    """Handle transactions.new — any new revenue transaction."""
    tx_type: str = payload.get("type", "other")
    amount: float = float(payload.get("amount", 0.0))
    logger.info("OF transaction: type=%s amount=%.2f", tx_type, amount)


async def _handle_tip(payload: dict, model_id: str) -> None:
    """Handle tips.received — fan sent a tip."""
    platform_user_id: str = str(payload.get("user_id", ""))
    amount_gross: float = float(payload.get("amountGross", 0.0))

    if not platform_user_id:
        return

    try:
        sub, _ = _get_or_load_subscriber(platform_user_id, model_id)
        record_transaction(sub.sub_id, model_id, "tip", amount_gross, PLATFORM)
        logger.info("OF tip: %s $%.2f gross", platform_user_id, amount_gross)
    except Exception as exc:
        logger.exception("Error handling tips.received %s: %s", platform_user_id, exc)
        try:
            from admin_bot.error_alerts import alert_bot_error
            await alert_bot_error(
                "handle_tip", exc, platform=PLATFORM, model=model_id,
                extra_context={"user_id": platform_user_id, "amount_gross": amount_gross},
            )
        except Exception:
            pass


# ─────────────────────────────────────────────
# Unified webhook endpoint
# ─────────────────────────────────────────────

@app.get("/webhook/of")
@app.get("/webhook/of/")
async def webhook_of_ping():
    """Endpoint reachability check."""
    return JSONResponse({"status": "ok"})


@app.post("/webhook/of")
@app.post("/webhook/of/")
async def webhook_of(request: Request, background_tasks: BackgroundTasks):
    """Single unified webhook endpoint for all OnlyFansAPI.com events."""
    body = await request.body()
    verify_signature(body, request.headers.get("Signature", ""))

    data = json.loads(body)
    event: str = data.get("event", "")
    account_id: str = data.get("account_id", "")
    payload: dict = data.get("payload", {})

    try:
        model_id = _get_model_id()
    except RuntimeError as exc:
        logger.error("Cannot dispatch event %s: %s", event, exc)
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)

    if event == "messages.received":
        background_tasks.add_task(_handle_message, payload, model_id)
    elif event == "messages.ppv.unlocked":
        background_tasks.add_task(_handle_ppv_unlocked, payload, model_id)
    elif event == "subscriptions.new":
        background_tasks.add_task(_handle_new_subscriber, payload, model_id)
    elif event == "subscriptions.renewed":
        background_tasks.add_task(_handle_renewed, payload, model_id)
    elif event == "transactions.new":
        background_tasks.add_task(_handle_transaction, payload, model_id)
    elif event == "tips.received":
        background_tasks.add_task(_handle_tip, payload, model_id)
    elif event.startswith("accounts."):
        logger.info("OF account event: %s (account_id=%s)", event, account_id)
    else:
        logger.debug("Unhandled OF event: %s", event)

    return JSONResponse({"status": "ok"})


# ─────────────────────────────────────────────
# Test endpoint (simulate purchase — no signature required)
# ─────────────────────────────────────────────

@app.post("/test/simulate-purchase")
async def test_simulate_purchase(request: Request, background_tasks: BackgroundTasks):
    """Simulate a purchase event for testing. No HMAC verification."""
    payload = await request.json()
    fan_id: str = str(payload.get("fan_id", payload.get("fan_uuid", "")))
    amount_dollars: float = payload.get("amount", 0.0)
    tier: int = payload.get("tier", 1)

    if not fan_id:
        return JSONResponse({"status": "error", "detail": "fan_id required"})

    model_id = _model_id or ""
    if not model_id:
        return JSONResponse({"status": "error", "detail": "No model configured"})

    async def handle():
        lock = _get_sub_lock(fan_id)
        async with lock:
            try:
                sub, _ = _get_or_load_subscriber(fan_id, model_id)
                if not sub.persona_id:
                    sub.persona_id = _default_avatar
                avatar = get_avatar(_avatars, sub.persona_id)
                logger.info(">>> TEST PURCHASE: fan=%s amount=$%.2f tier=%d", fan_id, amount_dollars, tier)

                # GFE continuation detection
                if sub.gfe_continuation_pending:
                    sub.gfe_continuation_pending = False
                    sub.gfe_continuations_paid += 1
                    sub.gfe_message_count = 0
                    # Re-randomize continuation threshold for next cycle
                    import random as _rnd
                    sub.continuation_threshold_jitter = _rnd.randint(40, 50)
                    logger.info(">>> GFE CONTINUATION PAID (test) by %s — counter reset, next threshold=%s",
                                fan_id, sub.continuation_threshold_jitter)
                    actions = [BotAction(
                        action_type="send_message",
                        message=random.choice([
                            "you're back 🥰 ok where were we...",
                            "I knew you weren't gonna leave me 😏 now come here...",
                            "see?? that's what I like about you... you show up 💕 now let's keep going",
                            "ok NOW we're talking 😘 I missed you already lol",
                        ]),
                        delay_seconds=random.randint(8, 15),
                    )]
                    save_subscriber(sub, PLATFORM, fan_id, model_id)
                    record_transaction(sub.sub_id, model_id, "gfe_continuation", amount_dollars, PLATFORM)
                    await execute_actions(actions, fan_id, model_id, sub)
                    queued = _sub_queued_messages.pop(fan_id, [])
                    if queued:
                        combined = "\n".join(queued)
                        msg_actions = await orchestrator_process_message(sub, combined, avatar, model_profile=_model_profile, active_tier_count=_active_tier_count)
                        save_subscriber(sub, PLATFORM, fan_id, model_id)
                        await execute_actions(msg_actions, fan_id, model_id, sub)
                    return

                # Custom payment detection — same as real webhook
                custom_order = getattr(sub, "pending_custom_order", None)
                if custom_order and custom_order.get("status") in ("pitched", "awaiting_admin_confirm"):
                    quoted = custom_order.get("quoted_price", 0)
                    if abs(amount_dollars - quoted) < 5.0:
                        logger.info(">>> TEST PURCHASE: custom payment detected $%.2f matches quoted $%.2f", amount_dollars, quoted)
                        from engine.custom_orders import mark_fan_paid
                        sub.pending_custom_order = mark_fan_paid(custom_order)
                        sub.gfe_message_count = 0  # Reset — fan is spending
                        sub.pending_ppv = None  # Custom-payment PPV was the placeholder; fan paid, clear it
                        sub.custom_request_streak = 0
                        save_subscriber(sub, PLATFORM, fan_id, model_id)
                        record_transaction(sub.sub_id, model_id, "custom", amount_dollars, PLATFORM)
                        try:
                            from admin_bot.alerts import alert_custom_payment_claim
                            await alert_custom_payment_claim(sub, sub.pending_custom_order)
                        except Exception as e:
                            logger.warning("Custom payment alert failed: %s", e)
                        await send_of_message(fan_id, "got it baby, let me verify real quick and then I'll start working on your custom for you")
                        return

                # Fan paid — clear pending_ppv + reset custom streak
                if sub.pending_ppv:
                    sub.pending_ppv = None
                sub.custom_request_streak = 0

                actions = await orchestrator_process_purchase(sub, amount_dollars, avatar, model_profile=_model_profile, active_tier_count=_active_tier_count)

                save_subscriber(sub, PLATFORM, fan_id, model_id)
                record_transaction(sub.sub_id, model_id, "ppv", amount_dollars, PLATFORM)
                logger.info(">>> TEST PURCHASE: got %d actions", len(actions) if actions else 0)

                if actions:
                    await execute_actions(actions, fan_id, model_id, sub)
                    save_subscriber(sub, PLATFORM, fan_id, model_id)

                # Process any fan messages that queued while we held the lock
                queued = _sub_queued_messages.pop(fan_id, [])
                if queued:
                    logger.info(">>> Processing %d queued messages after test purchase for %s", len(queued), fan_id)
                    sub = load_subscriber(PLATFORM, fan_id, model_id) or sub
                    avatar = get_avatar(_avatars, sub.persona_id)
                    combined = "\n".join(queued)
                    msg_actions = await orchestrator_process_message(sub, combined, avatar, model_profile=_model_profile, active_tier_count=_active_tier_count)
                    save_subscriber(sub, PLATFORM, fan_id, model_id)
                    await execute_actions(msg_actions, fan_id, model_id, sub)

                logger.info(">>> TEST PURCHASE COMPLETE: ppv_count=%d total=$%.2f",
                            sub.spending.ppv_count, sub.spending.total_spent)
            except Exception as exc:
                logger.exception("Test purchase error: %s", exc)
                try:
                    from admin_bot.error_alerts import alert_bot_error
                    await alert_bot_error(
                        "test_simulate_purchase", exc, platform=PLATFORM, model=model_id,
                        extra_context={"fan_id": fan_id, "amount": amount_dollars},
                    )
                except Exception:
                    pass

    background_tasks.add_task(handle)
    return JSONResponse({"status": "ok", "amount": amount_dollars, "tier": tier})


# ─────────────────────────────────────────────
# Shutdown
# ─────────────────────────────────────────────

@app.on_event("shutdown")
async def shutdown():
    logger.info("OnlyFans connector shutting down")


# ─────────────────────────────────────────────
# Health + status
# ─────────────────────────────────────────────

@app.get("/health")
async def health():
    return JSONResponse({
        "status": "ok",
        "platform": "onlyfans",
        "engine_ready": bool(_avatars),
        "model_id": _model_id or "not_set",
    })


@app.get("/")
async def root():
    return JSONResponse({"service": "Massi-Bot OnlyFans Connector", "version": "2.0.0"})
