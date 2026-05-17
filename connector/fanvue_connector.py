"""
Massi-Bot - Fanvue Connector

FastAPI app (port 8000) that:
  1. Receives 6 Fanvue webhook event types
  2. Verifies HMAC-SHA256 signatures (X-Fanvue-Signature: t=...,v0=...)
  3. Loads/saves subscriber state from Supabase
  4. Feeds events into the single-agent orchestrator
  5. Executes BotActions (send_message, send_ppv) with mandatory delays
  6. Handles OAuth 2.0 PKCE callback for initial token setup

Run with: uvicorn connector.fanvue_connector:app --port 8000
"""

import os
import sys
import hmac
import time
import json
import base64
import random
import hashlib
import logging
import asyncio
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Dict

import redis as _redis_lib
import httpx
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'engine'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.models import Subscriber, BotAction, SubState
from engine.avatars import AvatarConfig
from connector.token_manager import token_manager
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
from connector.media_handler import process_media, MediaAnalysis
from agents.media_reactor import react_to_media
from agents.context_builder import build_context

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
        logger.info("Sentry initialized for fanvue_connector")
    except Exception as _sentry_err:
        logger.warning("Sentry init failed: %s", _sentry_err)

# ─────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────

API_BASE = "https://api.fanvue.com"
API_VERSION = "2025-06-26"
PLATFORM = "fanvue"
SIGNATURE_MAX_AGE = 300   # 5 minutes replay window

# ─────────────────────────────────────────────
# Module state (replaces BotController)
# ─────────────────────────────────────────────

@dataclass
class ModelContext:
    """Per-model state loaded at startup, keyed by creator_uuid."""
    model_id: str
    creator_uuid: str
    model_profile: Optional[object] = None  # ModelProfile
    attribution: Optional[object] = None    # AttributionEngine
    default_avatar: str = "luxury_baddie"   # Fallback avatar for this model
    stage_name: str = ""                    # For logging
    active_tier_count: int = 6             # Number of active tiers (3 = tease only, 6 = full pipeline)


_avatars: Dict[str, AvatarConfig] = {}   # Shared across all models
_model_contexts: Dict[str, ModelContext] = {}  # creator_uuid -> ModelContext

# Legacy fallback -- used when recipientUuid is missing from payload
_fallback_model_id: Optional[str] = None

# Per-subscriber lock: prevents concurrent processing of messages from the same fan.
# When a message is being processed for a subscriber, new messages are queued and
# processed after the current one completes (with rapid-fire combining).
_sub_locks: Dict[str, asyncio.Lock] = {}
_sub_queued_messages: Dict[str, list] = {}  # fan_uuid → [queued message texts]
_processed_message_uuids: Dict[str, bool] = {}  # messageUuid → True (dedup, max 500)

# Adaptive settle window — wait for fan to stop typing before starting pipeline
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


async def _wait_for_settle(platform_user_id: str) -> None:
    """
    Wait for the fan to finish typing before starting the pipeline.
    Initial: 8s. Extends 5s per new message. Max 30s total.
    Fanvue doesn't have typing indicators but the orchestrator will fire one when it starts.
    """
    import time as _time
    initial = _settle_initial_seconds()
    extension = _settle_extension_seconds()
    max_total = _settle_max_seconds()

    start = _time.monotonic()
    first_msg_time = _sub_last_msg_time.get(platform_user_id, start)
    target_wake = first_msg_time + initial

    while True:
        now = _time.monotonic()
        elapsed = now - start
        if elapsed >= max_total:
            logger.debug("Settle hit max cap %.1fs for %s", max_total, platform_user_id[:8])
            return
        sleep_for = target_wake - now
        if sleep_for <= 0:
            latest_msg = _sub_last_msg_time.get(platform_user_id, first_msg_time)
            if latest_msg > first_msg_time:
                first_msg_time = latest_msg
                target_wake = latest_msg + extension
                continue
            return
        await asyncio.sleep(min(sleep_for, 1.0))
        latest_msg = _sub_last_msg_time.get(platform_user_id, first_msg_time)
        if latest_msg > first_msg_time:
            first_msg_time = latest_msg
            target_wake = latest_msg + extension


def _get_sub_lock(fan_uuid: str) -> asyncio.Lock:
    """Get or create an asyncio.Lock for a specific subscriber."""
    if fan_uuid not in _sub_locks:
        _sub_locks[fan_uuid] = asyncio.Lock()
    return _sub_locks[fan_uuid]


def _is_engine_paused() -> bool:
    """Check Redis flag set by admin bot /pause command."""
    try:
        r = _redis_lib.from_url(os.environ["REDIS_URL"], decode_responses=True)
        return bool(r.get("engine:paused"))
    except Exception:
        return False


def _get_model_context(payload: dict) -> ModelContext:
    """
    Extract recipientUuid from webhook payload and look up ModelContext.
    Falls back to FANVUE_MODEL_ID env var for backwards compatibility.
    Auto-registers unknown creator UUIDs when FANVUE_MODEL_ID is set.
    """
    recipient_uuid = payload.get("recipientUuid", "")

    if recipient_uuid and recipient_uuid in _model_contexts:
        return _model_contexts[recipient_uuid]

    # Unknown creator UUID -- try to auto-register if we can identify the model
    if recipient_uuid and _fallback_model_id:
        # Check if this UUID belongs to a model we know about in Supabase
        try:
            db = get_supabase()
            result = db.table("models").select("id, stage_name").eq(
                "fanvue_creator_uuid", recipient_uuid
            ).limit(1).execute()
            if result.data:
                row = result.data[0]
                ctx = _build_model_context(row["id"], recipient_uuid)
                _model_contexts[recipient_uuid] = ctx
                logger.info("Auto-registered model context: %s (%s)",
                           ctx.stage_name, recipient_uuid[:8])
                return ctx
        except Exception as e:
            logger.warning("Auto-register lookup failed: %s", e)

        # Last resort: use fallback model_id (backwards compat)
        if _fallback_model_id not in [ctx.model_id for ctx in _model_contexts.values()]:
            logger.warning("Unknown recipientUuid %s -- using fallback model %s",
                          recipient_uuid[:8], _fallback_model_id[:8])
        # Find context by fallback model_id
        for ctx in _model_contexts.values():
            if ctx.model_id == _fallback_model_id:
                # Update creator_uuid mapping for future lookups
                ctx_copy = ModelContext(
                    model_id=ctx.model_id,
                    creator_uuid=recipient_uuid,
                    model_profile=ctx.model_profile,
                    attribution=ctx.attribution,
                    default_avatar=ctx.default_avatar,
                    stage_name=ctx.stage_name,
                )
                _model_contexts[recipient_uuid] = ctx_copy
                # Do NOT overwrite Supabase fanvue_creator_uuid here — that value is the
                # source of truth set during setup. Auto-detecting from webhooks would
                # oscillate between the manager UUID (test button) and model UUID (real messages).
                logger.info("Mapped unknown recipientUuid %s to model %s (in-memory only)",
                           recipient_uuid[:8], ctx.model_id[:8])
                return ctx_copy

    raise HTTPException(404, f"Unknown model -- recipientUuid not mapped: {recipient_uuid[:16]}")


def _build_model_context(model_id: str, creator_uuid: str) -> ModelContext:
    """Build a ModelContext for a single model."""
    profile = load_model_profile(model_id)

    # Load profile_json once for all per-model config
    pj = {}
    if profile:
        try:
            db = get_supabase()
            result = db.table("models").select("profile_json").eq("id", model_id).limit(1).execute()
            if result.data:
                pj = result.data[0].get("profile_json") or {}
        except Exception:
            pass

    # Per-model IG attribution
    ig_map_json = "{}"
    ig_map_raw = pj.get("ig_map", {})
    if ig_map_raw:
        ig_map_json = json.dumps(ig_map_raw)
    if ig_map_json == "{}":
        ig_map_json = os.environ.get("FANVUE_IG_MAP", "{}")
    attribution = build_attribution(ig_map_json, _avatars)

    # Per-model default avatar
    default_avatar = pj.get("default_avatar", "luxury_baddie")

    # Per-model active tier count (default 6, set to 3 for models with limited content)
    active_tier_count = pj.get("active_tier_count", 6)

    return ModelContext(
        model_id=model_id,
        creator_uuid=creator_uuid,
        model_profile=profile,
        attribution=attribution,
        default_avatar=default_avatar,
        stage_name=profile.stage_name if profile else model_id[:8],
        active_tier_count=active_tier_count,
    )


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
            platform=platform,
            username=sub.username,
            sub_id=sub.sub_id,
            whale_score=score,
            total_spent=total,
            highest_purchase=highest,
            trigger=trigger,
        )


# ─────────────────────────────────────────────
# FastAPI app
# ─────────────────────────────────────────────

app = FastAPI(title="Massi-Bot Fanvue Connector", version="2.0.0")


@app.on_event("startup")
async def startup():
    global _avatars, _model_contexts, _fallback_model_id

    # Shared: load all avatars (used by all models)
    _avatars = load_avatars()

    # Load creator_uuid -> model_id mappings from Supabase
    from persistence.model_profile import load_creator_model_map
    creator_map = load_creator_model_map()

    # Build ModelContext for each mapped model
    for creator_uuid, model_id in creator_map.items():
        ctx = _build_model_context(model_id, creator_uuid)
        _model_contexts[creator_uuid] = ctx
        logger.info("Loaded model context: %s (%s -> %s)",
                    ctx.stage_name, creator_uuid[:8], model_id[:8])

    # Fallback: FANVUE_MODEL_ID env var for backwards compatibility
    _fallback_model_id = os.environ.get("FANVUE_MODEL_ID", "")
    if _fallback_model_id and not any(
        ctx.model_id == _fallback_model_id for ctx in _model_contexts.values()
    ):
        # Model exists in env but has no creator_uuid mapping yet
        # Create a placeholder context -- creator_uuid will be set from first webhook
        logger.info("Fallback model %s has no creator_uuid -- will auto-detect from webhooks",
                    _fallback_model_id[:8])

    # Start token refresh
    await token_manager.ensure_started()

    # Pre-warm sentence-transformer encoder (avoids 3s cold-start on first fan message)
    try:
        from llm.memory_store import prewarm_encoder
        prewarm_encoder()
    except Exception:
        pass

    # Start PPV auto-cleanup sweep (6h default abandonment, configurable via PPV_ABANDONMENT_HOURS)
    try:
        from connector.ppv_cleanup import start_sweep_loop
        asyncio.create_task(start_sweep_loop(PLATFORM, delete_fanvue_message))
    except Exception as e:
        logger.warning("PPV cleanup sweep failed to start: %s", e)

    # Error-recovery sweep: startup pass + 60s background loop (multi-model aware)
    try:
        from connector.recovery import run_recovery_sweep, recovery_loop
        from admin_bot.error_alerts import alert_bot_error, alert_bot_error_resolved

        def _fanvue_model_ctx(model_id_lookup: str):
            # Find the ctx whose model_id matches; return (creator_uuid, model_profile).
            for creator_uuid, ctx in _model_contexts.items():
                if ctx.model_id == model_id_lookup:
                    return (creator_uuid, ctx.model_profile)
            return ("", None)

        async def _fanvue_sweep():
            # Each model can have its own active_tier_count; do a per-model pass.
            for creator_uuid, ctx in list(_model_contexts.items()):
                await run_recovery_sweep(
                    platform=PLATFORM,
                    model_context_lookup=lambda _mid, cu=creator_uuid, ctx=ctx: (cu, ctx.model_profile),
                    orchestrator_process_message=orchestrator_process_message,
                    get_avatar_fn=get_avatar,
                    avatars_registry=_avatars,
                    active_tier_count=ctx.active_tier_count,
                    execute_actions_fn=execute_actions,
                    sub_lock_factory=_get_sub_lock,
                    send_alert_bot_error_resolved=alert_bot_error_resolved,
                    send_alert_bot_error=alert_bot_error,
                )

        asyncio.create_task(_fanvue_sweep())
        asyncio.create_task(recovery_loop(_fanvue_sweep, interval_seconds=60))
        logger.info("Recovery sweep wired on Fanvue (startup + 60s loop)")
    except Exception as e:
        logger.warning("Recovery sweep failed to start: %s", e)

    # Inbox polling fallback: Fanvue webhook delivery is unreliable.
    # Poll GET /chats every 30s and inject any unread fan messages that didn't arrive via webhook.
    async def _inbox_poll_loop():
        import time as _time
        await asyncio.sleep(10)  # brief startup delay
        while True:
            try:
                # Deduplicate by model_id — _model_contexts accumulates UUID aliases over time
                # (each unknown recipientUuid gets a new entry), so poll once per real model.
                seen_model_ids: set[str] = set()
                unique_entries: list[tuple[str, object]] = []
                for creator_uuid, ctx in list(_model_contexts.items()):
                    if ctx.model_id not in seen_model_ids:
                        seen_model_ids.add(ctx.model_id)
                        unique_entries.append((creator_uuid, ctx))
                for creator_uuid, ctx in unique_entries:
                    try:
                        headers = await _get_headers(creator_uuid)
                        async with httpx.AsyncClient(timeout=15) as client:
                            resp = await client.get(
                                f"{API_BASE}/chats",
                                headers=headers,
                                params={"size": 100},
                            )
                        if resp.status_code != 200:
                            continue
                        chats = resp.json().get("data") or []
                        for chat in chats:
                            last_msg = chat.get("lastMessage") or {}
                            last_msg_uuid = last_msg.get("uuid", "")
                            last_sender = last_msg.get("senderUuid", "")
                            # Skip if last message was sent by the model (already replied)
                            if last_sender == creator_uuid:
                                continue
                            # Skip if already processed
                            if last_msg_uuid and last_msg_uuid in _processed_message_uuids:
                                continue
                            # Skip if no unread messages from the fan
                            if chat.get("isRead", True) and not chat.get("unreadMessagesCount", 0):
                                continue
                            fan = chat.get("user") or {}
                            fan_uuid = fan.get("uuid", "")
                            if not fan_uuid:
                                continue
                            # Skip if currently processing this fan
                            if _get_sub_lock(fan_uuid).locked():
                                continue
                            # Inject as a signed internal webhook
                            payload_dict = {
                                "recipientUuid": creator_uuid,
                                "sender": {
                                    "uuid": fan_uuid,
                                    "handle": fan.get("handle", ""),
                                    "displayName": fan.get("displayName", ""),
                                },
                                "timestamp": last_msg.get("sentAt", ""),
                            }
                            body_bytes = json.dumps(payload_dict).encode()
                            ts_str = str(int(_time.time()))
                            signed_str = f"{ts_str}.{body_bytes.decode()}"
                            secret = os.environ.get("FANVUE_WEBHOOK_SECRET", "")
                            sig = hmac.new(secret.encode(), signed_str.encode(), hashlib.sha256).hexdigest()
                            logger.info("Inbox poll: injecting unread message from fan %s", fan_uuid[:8])
                            async with httpx.AsyncClient(timeout=10) as client:
                                await client.post(
                                    "http://localhost:8000/webhook/fanvue/",
                                    content=body_bytes,
                                    headers={
                                        "Content-Type": "application/json",
                                        "X-Fanvue-Signature": f"t={ts_str},v0={sig}",
                                    },
                                )
                    except Exception as e:
                        logger.warning("Inbox poll error for creator %s: %s", creator_uuid[:8], e)
            except Exception as e:
                logger.warning("Inbox poll loop error: %s", e)
            await asyncio.sleep(30)

    asyncio.create_task(_inbox_poll_loop())
    logger.info("Inbox polling loop started (30s interval)")

    logger.info("Fanvue connector started (%d models loaded, %d avatars)",
                len(_model_contexts), len(_avatars))


@app.on_event("shutdown")
async def shutdown():
    await token_manager.stop()


# ─────────────────────────────────────────────
# Signature verification
# ─────────────────────────────────────────────

def verify_signature(body: bytes, sig_header: str) -> None:
    """
    Verify Fanvue HMAC-SHA256 webhook signature.
    Header format: "t=<unix_timestamp>,v0=<hex_signature>"
    """
    if not sig_header:
        logger.warning("SIGNATURE FAIL: missing header")
        raise HTTPException(status_code=403, detail="Missing signature header")

    try:
        parts = dict(part.split("=", 1) for part in sig_header.split(","))
        timestamp_str = parts["t"]
        signature = parts["v0"]
    except (KeyError, ValueError):
        logger.warning("SIGNATURE FAIL: malformed header=%s", sig_header[:100])
        raise HTTPException(status_code=403, detail="Malformed signature header")

    try:
        ts = int(timestamp_str)
    except ValueError:
        logger.warning("SIGNATURE FAIL: invalid timestamp=%s", timestamp_str)
        raise HTTPException(status_code=403, detail="Invalid timestamp")
    age = abs(time.time() - ts)
    if age > SIGNATURE_MAX_AGE:
        logger.warning("SIGNATURE FAIL: timestamp too old age=%.0fs header=%s", age, sig_header[:80])
        raise HTTPException(status_code=403, detail="Webhook timestamp too old")

    secret = os.environ["FANVUE_WEBHOOK_SECRET"]
    signed_payload = f"{timestamp_str}.{body.decode('utf-8')}"
    expected = hmac.new(
        secret.encode(),
        signed_payload.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(signature, expected):
        logger.warning("SIGNATURE FAIL: hmac mismatch. header=%s secret_prefix=%s body_prefix=%s",
                       sig_header[:80], secret[:4], body[:50])
        raise HTTPException(status_code=403, detail="Invalid signature")


# ─────────────────────────────────────────────
# Outbound Fanvue API calls
# ─────────────────────────────────────────────

async def _get_headers(creator_uuid: str = "") -> dict:
    try:
        token = await token_manager.get_access_token(creator_uuid)
    except Exception:
        # Webhook recipientUuid may differ from OAuth JWT sub — fall back to any stored token
        token = await token_manager.get_access_token("")
    return {
        "Authorization": f"Bearer {token}",
        "X-Fanvue-API-Version": API_VERSION,
        "Content-Type": "application/json",
    }


async def send_fanvue_message(creator_uuid: str, user_uuid: str, text: str) -> None:
    """Send a plain text message to a subscriber. Token is per-creator.
    1-retry on network timeout + catches all exceptions so callers never crash."""
    if not creator_uuid:
        logger.error("Cannot send message -- no creator_uuid")
        return
    headers = await _get_headers(creator_uuid)
    url = f"{API_BASE}/chats/{user_uuid}/message"
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, json={"text": text}, headers=headers)
            if resp.status_code not in (200, 201):
                logger.error("send_message failed %d: %s | text_sent=%s", resp.status_code, resp.text[:200], text[:200])
                try:
                    from admin_bot.alerts import _send
                    if resp.status_code == 429:
                        retry_after = resp.headers.get("Retry-After", "unknown")
                        asyncio.create_task(_send(
                            f"🚦 <b>Fanvue Rate Limited (429)</b>\n"
                            f"User: <code>{user_uuid[:12]}</code>\n"
                            f"Retry-After: {retry_after}s\n\n"
                            f"Too many messages sent too fast. Bot is being throttled."
                        ))
                    else:
                        asyncio.create_task(_send(
                            f"⚠️ <b>Fanvue Message Rejected</b>\n"
                            f"User: <code>{user_uuid[:12]}</code>\n"
                            f"Status: {resp.status_code}\n"
                            f"Text: <i>{text[:150]}</i>\n"
                            f"Error: {resp.text[:150]}"
                        ))
                except Exception:
                    pass
            else:
                logger.debug("Sent message to %s", user_uuid)
            return
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            if attempt == 0:
                logger.warning("Fanvue send_message timeout/network err (attempt 1), retrying in 2s: %s", e)
                await asyncio.sleep(2)
                continue
            logger.error("Fanvue send_message failed after retry: %s", e)
            try:
                from admin_bot.alerts import _send
                asyncio.create_task(_send(
                    f"⚠️ <b>Fanvue Message Dropped</b>\n"
                    f"User: <code>{user_uuid[:12]}</code>\n"
                    f"Reason: network/timeout after retry\n"
                    f"Error: {str(e)[:200]}\n\n"
                    f"Fan did not receive a reply."
                ))
            except Exception:
                pass
            return
        except Exception as e:
            logger.error("Fanvue send_message unexpected error: %s", e)
            return


async def send_fanvue_ppv(
    creator_uuid: str,
    user_uuid: str,
    caption: str,
    fanvue_media_uuids: list[str] | str,
    price_cents: int,
) -> Optional[str]:
    """
    Send a PPV message to a single subscriber.
    Prices must be in CENTS (e.g. $27.38 -> 2738).
    Accepts a single UUID string or a list of UUIDs for multi-media bundles.

    Returns the Fanvue message UUID on success (for later deletion), None on failure.
    """
    if not creator_uuid:
        logger.error("Cannot send PPV -- no creator_uuid")
        return None
    # Normalize to list
    if isinstance(fanvue_media_uuids, str):
        fanvue_media_uuids = [fanvue_media_uuids]
    headers = await _get_headers(creator_uuid)
    url = f"{API_BASE}/chats/{user_uuid}/message"
    payload = {
        "text": caption,
        "mediaUuids": fanvue_media_uuids,
        "price": price_cents,
    }
    # PPV sends get aggressive retry — a failed PPV is a lost sale.
    # Retry on: timeouts, network errors, AND server errors (500/502/503/504).
    # Backoff: 2s, 10s, 30s (3 retries total).
    _PPV_RETRY_DELAYS = [2, 10, 30]
    resp = None
    for attempt in range(len(_PPV_RETRY_DELAYS) + 1):
        try:
            async with httpx.AsyncClient(timeout=40.0) as client:
                resp = await client.post(url, json=payload, headers=headers)
            if resp.status_code in (200, 201):
                break
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After", "unknown")
                logger.warning("Fanvue send_ppv rate limited 429 (attempt %d), Retry-After=%s", attempt + 1, retry_after)
                try:
                    from admin_bot.alerts import _send
                    asyncio.create_task(_send(
                        f"🚦 <b>Fanvue PPV Rate Limited (429)</b>\n"
                        f"User: <code>{user_uuid[:12]}</code>\n"
                        f"Price: {price_cents} cents\n"
                        f"Retry-After: {retry_after}s\n\n"
                        f"PPV delivery is being throttled."
                    ))
                except Exception:
                    pass
                if attempt < len(_PPV_RETRY_DELAYS):
                    delay = _PPV_RETRY_DELAYS[attempt]
                    await asyncio.sleep(delay)
                    continue
                break
            if resp.status_code >= 500 and attempt < len(_PPV_RETRY_DELAYS):
                delay = _PPV_RETRY_DELAYS[attempt]
                logger.warning("Fanvue send_ppv server error %d (attempt %d), retrying in %ds: %s",
                               resp.status_code, attempt + 1, delay, resp.text[:100])
                await asyncio.sleep(delay)
                continue
            break
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            if attempt < len(_PPV_RETRY_DELAYS):
                delay = _PPV_RETRY_DELAYS[attempt]
                logger.warning("Fanvue send_ppv timeout/network err (attempt %d), retrying in %ds: %s",
                               attempt + 1, delay, e)
                await asyncio.sleep(delay)
                continue
            logger.error("Fanvue send_ppv failed after %d retries for user %s: %s", attempt + 1, user_uuid, e)
            return None
        except Exception as e:
            logger.error("Fanvue send_ppv unexpected error for user %s: %s", user_uuid, e)
            return None
    if resp is None:
        return None
    if resp.status_code not in (200, 201):
        logger.error(
            "send_ppv failed %d for user %s after all retries: %s",
            resp.status_code, user_uuid, resp.text[:200],
        )
        try:
            from admin_bot.alerts import _send
            asyncio.create_task(_send(
                f"🚨 <b>Fanvue PPV Send FAILED</b>\n"
                f"User: <code>{user_uuid[:12]}</code>\n"
                f"Price: {price_cents} cents\n"
                f"Status: {resp.status_code}\n"
                f"Error: {resp.text[:150]}\n\n"
                f"PPV was NOT delivered. Fan may be waiting."
            ))
        except Exception:
            pass
        return None
    logger.info("Sent PPV %d cents (%d media) to %s", price_cents, len(fanvue_media_uuids), user_uuid)
    try:
        data = resp.json()
        # Fanvue returns the created message; extract its uuid
        return data.get("uuid") or data.get("data", {}).get("uuid") or data.get("messageUuid")
    except Exception:
        return None


async def delete_fanvue_message(
    creator_uuid: str,
    user_uuid: str,
    message_uuid: str,
) -> bool:
    """Delete a previously-sent message from the chat. Returns True on success."""
    if not creator_uuid or not message_uuid:
        return False
    try:
        headers = await _get_headers(creator_uuid)
        url = f"{API_BASE}/chats/{user_uuid}/messages/{message_uuid}"
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.delete(url, headers=headers)
        if resp.status_code in (200, 204):
            logger.info("Fanvue delete OK: msg=%s user=%s", message_uuid, user_uuid)
            return True
        logger.warning("Fanvue delete failed %d: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        logger.warning("Fanvue delete error: %s", e)
        return False


# ─────────────────────────────────────────────
# Media retrieval (REST API — fills gaps from webhook)
# ─────────────────────────────────────────────

async def fetch_message_media_info(
    creator_uuid: str,
    sender_uuid: str,
    message_uuid: str,
) -> Optional[dict]:
    """
    Fetch full message details from REST API to get mediaType + pricing.

    The webhook only gives us hasMedia + mediaUuids. We need the REST API
    to determine: (a) media type (image/video/audio), (b) whether the
    media is locked/paywalled (pricing field set = creator-sent PPV).

    Returns dict with keys: mediaType, pricing, purchasedAt, mediaUuids
    or None on failure.
    """
    if not creator_uuid:
        return None
    headers = await _get_headers(creator_uuid)
    url = f"{API_BASE}/chats/{sender_uuid}/messages"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                url,
                headers=headers,
                params={"size": 5, "markAsRead": "false"},
            )
        if resp.status_code != 200:
            logger.warning("fetch_message_media_info failed %d: %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        messages = data.get("results") or data.get("data") or []
        if isinstance(data, list):
            messages = data
        for msg in messages:
            if msg.get("uuid") == message_uuid:
                result = {
                    "mediaType": msg.get("mediaType"),
                    "pricing": msg.get("pricing"),
                    "purchasedAt": msg.get("purchasedAt"),
                    "mediaUuids": msg.get("mediaUuids") or [],
                }
                logger.info(">>> Message details: mediaType=%s pricing=%s purchasedAt=%s",
                            result["mediaType"], result["pricing"], result["purchasedAt"])
                return result
        logger.debug("Message %s not found in recent %d messages", message_uuid, len(messages))
    except Exception as e:
        logger.warning("fetch_message_media_info error: %s", str(e)[:100])
    return None


_MEDIA_ACCESS_DENIED = "__ACCESS_DENIED__"


async def resolve_media_url(
    creator_uuid: str,
    sender_uuid: str,
    message_uuid: str,
    media_uuid: str,
) -> Optional[str]:
    """
    Get a download URL for a specific media UUID via the message media endpoint.

    Returns:
        - Media URL string on success
        - _MEDIA_ACCESS_DENIED if 403 (creator-locked media)
        - None on other failures
    """
    if not creator_uuid:
        return None
    headers = await _get_headers(creator_uuid)
    url = f"{API_BASE}/chats/{sender_uuid}/messages/{message_uuid}/media"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                url,
                headers=headers,
                params={"mediaUuids": media_uuid, "variants": "main"},
            )
        if resp.status_code == 403:
            logger.info(">>> CREATOR-LOCKED MEDIA: 403 access denied for %s", media_uuid)
            return _MEDIA_ACCESS_DENIED
        if resp.status_code != 200:
            logger.warning("resolve_media_url failed %d: %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        results = data.get("results") or {}
        media_obj = results.get(media_uuid)
        if not media_obj:
            return None
        variants = media_obj.get("variants") or []
        for v in variants:
            if v.get("variantType") == "main" and v.get("url"):
                return v["url"]
        # Fallback: return first variant URL
        if variants and variants[0].get("url"):
            return variants[0]["url"]
    except Exception as e:
        logger.warning("resolve_media_url error: %s", str(e)[:100])
    return None


# ─────────────────────────────────────────────
# Action executor (honors delay_seconds)
# ─────────────────────────────────────────────

async def execute_actions(
    actions: list[BotAction],
    platform_user_id: str,
    model_id: str,
    sub: Subscriber,
    creator_uuid: str = "",
) -> None:
    """
    Execute a list of BotActions against the Fanvue API.
    Honors each action's delay_seconds -- mandatory per engine design.

    Also handles GFE-kick force-delete: if sub._force_delete_pending_ppv is set
    (orchestrator flagged a PPV for immediate deletion when kicking to GFE), we DELETE it
    before running the action list.
    """
    # GFE-kick force-delete: happens BEFORE sending actions so the chat is cleaned up first
    pending_to_delete = getattr(sub, "_force_delete_pending_ppv", None)
    if pending_to_delete:
        msg_uuid = pending_to_delete.get("platform_msg_id")
        del_creator = pending_to_delete.get("creator_uuid") or creator_uuid
        if msg_uuid and del_creator:
            try:
                ok = await delete_fanvue_message(del_creator, platform_user_id, msg_uuid)
                logger.info("GFE-kick force-delete: msg=%s user=%s success=%s",
                            msg_uuid, platform_user_id, ok)
            except Exception as e:
                logger.warning("GFE-kick force-delete failed: %s", e)
        sub._force_delete_pending_ppv = None

    def _mark_sent():
        """Stamp recovery-tracking timestamp after any successful fan-visible send."""
        sub.last_successful_bot_message_at = datetime.now().isoformat(timespec="seconds")

    for action in actions:
      # Per-action try/except: one action failing must NOT kill subsequent actions.
      try:
        # Belt-and-suspenders: continuation PPVs should NEVER have a multi-minute jitter.
        # If a continuation bundle somehow has >60s delay, cap it (pre-taken content, no realness needed).
        if action.action_type == "send_ppv" and action.metadata:
            tier_meta = action.metadata.get("tier", "")
            if tier_meta == "continuation" and action.delay_seconds > 60:
                logger.warning("Continuation PPV had %ds delay — capping to 10s",
                               action.delay_seconds)
                action.delay_seconds = random.randint(5, 10)

        if action.delay_seconds > 0:
            await asyncio.sleep(action.delay_seconds)

        if action.action_type == "send_message" and action.message:
            await send_fanvue_message(creator_uuid, platform_user_id, action.message)
            _mark_sent()

        elif action.action_type == "send_ppv":
            # Custom payment PPV — use a continuation photo as placeholder
            if (action.metadata or {}).get("use_continuation_placeholder") or (action.metadata or {}).get("tier") == "custom":
                try:
                    db = get_supabase()
                    r = db.table("content_catalog").select("fanvue_media_uuid").eq("model_id", model_id).eq("tier", 0).eq("media_type", "photo").limit(1).execute()
                    placeholder_uuid = (r.data[0]["fanvue_media_uuid"] if r.data else None)
                    if placeholder_uuid:
                        price_cents = round((action.ppv_price or 0) * 100)
                        sent_msg_uuid = await send_fanvue_ppv(
                            creator_uuid, platform_user_id,
                            action.ppv_caption or "custom order payment -- unlock to confirm",
                            [placeholder_uuid], price_cents,
                        )
                        logger.info("Custom payment PPV sent: $%.2f to %s (placeholder=%s)",
                                    action.ppv_price, platform_user_id, placeholder_uuid[:12])
                        _mark_sent()
                    else:
                        logger.warning("No continuation photo for custom PPV placeholder — sending caption only")
                        if action.ppv_caption:
                            await send_fanvue_message(creator_uuid, platform_user_id, action.ppv_caption)
                            _mark_sent()
                except Exception as e:
                    logger.warning("Custom PPV send failed: %s", e)
                continue

            bundle_info = None
            if action.content_id:
                bundle_info = get_bundle_by_id(action.content_id, model_id)

            # Fallback: look up by tier if no content_id was set
            if not bundle_info and action.metadata:
                tier_str = action.metadata.get("tier", "")  # e.g. "tier_1" or "continuation"
                if tier_str == "continuation":
                    # GFE continuation paywall — pick a random continuation image
                    # Falls back to tier 1 if no dedicated continuation content exists
                    try:
                        db = get_supabase()
                        # First try dedicated continuation content (tier=0)
                        r = db.table("content_catalog").select("*").eq("model_id", model_id).eq("tier", 0).execute()
                        candidates = r.data or []
                        if not candidates:
                            # Fallback: use tier 1 content
                            r = db.table("content_catalog").select("*").eq("model_id", model_id).eq("tier", 1).execute()
                            candidates = r.data or []
                        if candidates:
                            # Pick randomly, avoid repeating the last one sent
                            sent_ids = getattr(sub, 'sent_captions', []) or []
                            unsent = [c for c in candidates if c.get("bundle_id") not in sent_ids]
                            if not unsent:
                                unsent = candidates  # All sent, reset pool
                            bundle_info = random.choice(unsent)
                            logger.info("Continuation image picked: %s (from %d candidates)",
                                        bundle_info.get("bundle_id"), len(candidates))
                    except Exception as e:
                        logger.warning("Continuation content lookup failed: %s", e)
                else:
                    tier_num = int(tier_str.split("_")[-1]) if tier_str.startswith("tier_") else 0
                    if tier_num:
                        try:
                            # Session-scoped lookup — stay in the fan's current session until completion
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
                                # Collect ALL media UUIDs for this bundle
                                bundle_id = bundle_info.get("bundle_id")
                                all_media = [row["fanvue_media_uuid"] for row in r.data
                                             if row.get("fanvue_media_uuid") and row.get("bundle_id") == bundle_id]
                                bundle_info["_all_media_uuids"] = all_media
                                logger.info("Session %d tier %d bundle found: %s (%d media)",
                                            current_session, tier_num, bundle_id, len(all_media))
                            else:
                                logger.warning("No content for session %d tier %d — session gap for sub %s",
                                               current_session, tier_num, platform_user_id)
                        except Exception as e:
                            logger.warning("Tier lookup failed: %s", e)

            # Collect media UUIDs — prefer full bundle, fall back to single
            fanvue_media_uuids = (bundle_info or {}).get("_all_media_uuids") or []
            if not fanvue_media_uuids:
                single_uuid = (bundle_info or {}).get("fanvue_media_uuid")
                if single_uuid:
                    fanvue_media_uuids = [single_uuid]

            if not fanvue_media_uuids:
                logger.warning(
                    "No Fanvue media UUID for bundle %s -- sending caption only",
                    action.content_id,
                )
                if action.ppv_caption:
                    await send_fanvue_message(creator_uuid, platform_user_id, action.ppv_caption)
                    _mark_sent()
            else:
                price_cents = round((action.ppv_price or 0) * 100)
                sent_msg_uuid = await send_fanvue_ppv(
                    creator_uuid,
                    platform_user_id,
                    action.ppv_caption,
                    fanvue_media_uuids,
                    price_cents,
                )
                _mark_sent()
                # Track pending PPV for 6h auto-delete (only selling tiers 1-6, not continuation)
                tier_meta = (action.metadata or {}).get("tier", "")
                if sent_msg_uuid and tier_meta.startswith("tier_"):
                    try:
                        tier_int = int(tier_meta.split("_")[-1])
                    except ValueError:
                        tier_int = 0
                    if tier_int > 0:
                        sub.pending_ppv = {
                            "platform_msg_id": sent_msg_uuid,
                            "tier": tier_int,
                            "sent_at": datetime.now().isoformat(),
                            "bundle_id": (bundle_info or {}).get("bundle_id", ""),
                            "price_cents": price_cents,
                            "platform": PLATFORM,
                            "platform_user_id": platform_user_id,
                            "creator_uuid": creator_uuid,
                            "model_id": model_id,
                        }
                        # Persist immediately — any queued-message drain that reloads from DB
                        # must see the pending_ppv flag, or the no-re-drop rule will break.
                        try:
                            save_subscriber(sub, PLATFORM, platform_user_id, model_id)
                        except Exception as e:
                            logger.warning("save_subscriber after PPV send failed: %s", e)
                        logger.info("Pending PPV tracked: tier=%d msg=%s for sub %s",
                                    tier_int, sent_msg_uuid, platform_user_id)

        elif action.action_type == "send_free" and action.message:
            await send_fanvue_message(creator_uuid, platform_user_id, action.message)
            _mark_sent()

        elif action.action_type == "flag":
            logger.info("FLAG action for %s: %s", platform_user_id, action.metadata)
      except Exception as exc:
        logger.exception("Action failed (%s) — continuing with remaining actions: %s",
                         action.action_type, exc)


# ─────────────────────────────────────────────
# Media processing helpers
# ─────────────────────────────────────────────

async def _process_incoming_media(
    creator_uuid: str,
    sender_uuid: str,
    message_uuid: str,
    media_uuids: list[str],
) -> Optional[MediaAnalysis]:
    """
    Fetch message details from REST API, check if media is locked,
    download + analyze if free.

    Returns MediaAnalysis or None (if locked, failed, or disabled).
    """
    # Step 1: Get mediaType + pricing from REST API
    msg_info = await fetch_message_media_info(creator_uuid, sender_uuid, message_uuid)

    if msg_info and msg_info.get("pricing"):
        # Media is locked (creator-sent PPV) — cannot view without purchasing
        logger.info(">>> LOCKED MEDIA from %s (pricing=%s) — skipping download",
                     sender_uuid, msg_info["pricing"])
        return None

    media_type_raw = (msg_info or {}).get("mediaType") or "image"
    # Map Fanvue mediaType enum to our internal types
    media_type_map = {
        "image": "image",
        "video": "video",
        "audio": "voice",
        "document": "image",  # fallback
    }
    media_type = media_type_map.get(media_type_raw, "image")

    # Step 2: Resolve download URL for the first media UUID
    first_uuid = media_uuids[0] if media_uuids else None
    if not first_uuid:
        return None

    media_url = await resolve_media_url(creator_uuid, sender_uuid, message_uuid, first_uuid)

    if media_url == _MEDIA_ACCESS_DENIED:
        # Creator-locked media (403) — we can't see it.
        # Return a synthetic MediaAnalysis so the reactor can respond naturally.
        logger.info(">>> Creator-locked media from %s — returning blind analysis", sender_uuid)
        return MediaAnalysis(
            media_type=media_type,
            description=f"Fan sent a {media_type} but it is locked/inaccessible (creator-to-creator restriction)",
            transcript=None,
            is_explicit=False,
            is_selfie=False,
            mood="unknown",
            raw_vision_output="LOCKED",
        )

    if not media_url:
        logger.warning("Could not resolve media URL for %s", first_uuid)
        return None

    # Step 3: Download + analyze via media_handler
    logger.info(">>> Processing %s media from %s (uuid=%s)", media_type, sender_uuid, first_uuid)
    access_token = await token_manager.get_access_token()
    analysis = await process_media(media_url, media_type, "fanvue", access_token=access_token)

    if analysis:
        logger.info(">>> Media analysis: type=%s explicit=%s selfie=%s mood=%s desc=%s",
                     analysis.media_type, analysis.is_explicit, analysis.is_selfie,
                     analysis.mood, analysis.description[:80])
    return analysis


async def _media_react(
    sub: Subscriber,
    avatar,
    media_analysis: MediaAnalysis,
    fan_text: str,
    model_profile=None,
) -> list[BotAction]:
    """
    Generate a reaction to fan-sent media via the Media Reactor agent.

    Returns list of BotActions (send_message only, never PPV).
    """
    try:
        from agents.emotion_analyzer import analyze_emotion
        context = await build_context(sub, fan_text or "", avatar, model_profile=model_profile)
        emotion = await analyze_emotion(
            message=fan_text or f"[sent {media_analysis.media_type}]",
            conversation_history=context.get("conversation_history", ""),
            subscriber_summary=context.get("subscriber_summary", ""),
        )
        result = await react_to_media(
            media_analysis=media_analysis,
            fan_text=fan_text,
            avatar=avatar,
            sub=sub,
            context=context,
            emotion_analysis=emotion,
            model_profile=model_profile,
        )
        messages = result.get("messages", [])
        actions = []
        for msg in messages:
            # Media reactions: 10-20 second delay (quick glance + short reply)
            delay = random.randint(10, 20)
            actions.append(BotAction(
                action_type="send_message",
                message=msg.get("text", ""),
                delay_seconds=delay,
            ))
        logger.info(">>> Media reaction: %d actions", len(actions))
        return actions
    except Exception as exc:
        logger.exception("Media reaction error: %s", exc)
        try:
            from admin_bot.error_alerts import alert_bot_error
            await alert_bot_error("media_reactor", exc, platform=PLATFORM, model=model_id)
        except Exception:
            pass
        return []


# ─────────────────────────────────────────────
# Subscriber lifecycle helpers
# ─────────────────────────────────────────────

def _get_or_load_subscriber(
    platform_user_id: str,
    model_id: str,
    username: str = "",
    display_name: str = "",
) -> tuple[Subscriber, bool]:
    """Load subscriber from Supabase. Returns (subscriber, is_new)."""
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
# Webhook handlers
# ─────────────────────────────────────────────

@app.post("/webhook/fanvue/")
@app.post("/webhook/fanvue")
async def webhook_dispatcher(request: Request, background_tasks: BackgroundTasks):
    """
    Fanvue sends all events to a single registered URL with the event type in the payload body.
    This dispatcher reads the event type and routes to the appropriate handler.
    Starlette caches request.body() so sub-handlers can read it again safely.
    """
    body = await request.body()
    verify_signature(body, request.headers.get("X-Fanvue-Signature", ""))
    payload = json.loads(body)

    event_type = (
        payload.get("event") or
        payload.get("type") or
        payload.get("eventType") or
        ""
    ).lower().replace("_", "-")

    # Infer from payload structure when event type field is absent
    if not event_type:
        if payload.get("buyerUuid") or payload.get("buyer"):
            event_type = "purchase-received"
        elif payload.get("tipperUuid"):
            event_type = "tip-received"
        elif payload.get("followerUuid"):
            event_type = "new-follower"
        elif payload.get("subscriberUuid") or (payload.get("subscriber") and "message" not in payload):
            event_type = "new-subscriber"
        else:
            event_type = "message-received"

    logger.info("Fanvue dispatcher: event=%s keys=%s", event_type, sorted(payload.keys()))

    if event_type in ("message-received", "message.received"):
        return await webhook_message_received(request, background_tasks)
    elif event_type in ("new-subscriber", "subscriber.new", "subscription.new"):
        return await webhook_new_subscriber(request, background_tasks)
    elif event_type in ("purchase-received", "purchase.received", "ppv.purchased"):
        return await webhook_purchase_received(request, background_tasks)
    elif event_type in ("tip-received", "tip.received"):
        return await webhook_tip_received(request, background_tasks)
    elif event_type in ("message-read", "message.read"):
        return await webhook_message_read(request)
    elif event_type in ("new-follower", "follower.new"):
        return await webhook_new_follower(request)
    else:
        logger.warning("Fanvue dispatcher: unknown event '%s' -- routing as message-received", event_type)
        return await webhook_message_received(request, background_tasks)


@app.post("/webhook/fanvue/message-received")
async def webhook_message_received(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    verify_signature(body, request.headers.get("X-Fanvue-Signature", ""))

    payload = json.loads(body)

    # ── MEDIA/CREATOR DISCOVERY LOGGING ──
    # Log full payload when media is present or when sender fields are unusual.
    # This helps discover: hasMedia, mediaType, mediaUuids, pricing, purchasedAt,
    # and any isCreator/senderType fields Fanvue may include.
    # TODO: Remove this logging block once field discovery is complete.
    message_obj_raw = payload.get("message") or {}
    has_media = message_obj_raw.get("hasMedia")
    if has_media:
        logger.info(">>> MEDIA PAYLOAD DISCOVERY: %s", json.dumps(payload, default=str))
    sender_raw = payload.get("sender") or {}
    sender_keys = sorted(sender_raw.keys())
    if sender_keys != ["displayName", "handle", "uuid"] and sender_keys != ["displayName", "username", "uuid"] and sender_keys != ["displayName", "uuid"]:
        logger.info(">>> SENDER FIELD DISCOVERY (unexpected keys %s): %s", sender_keys, json.dumps(sender_raw, default=str))
    # Log message object keys + pricing/purchasedAt for first encounters
    msg_keys = sorted(message_obj_raw.keys())
    pricing = message_obj_raw.get("pricing")
    if pricing is not None:
        logger.info(">>> PRICING FIELD FOUND on message: %s (full msg obj: %s)",
                     json.dumps(pricing, default=str), json.dumps(message_obj_raw, default=str))
    # Log top-level payload keys once to discover any unexpected fields
    payload_keys = sorted(payload.keys())
    logger.debug(">>> PAYLOAD KEYS: %s | MSG KEYS: %s | SENDER KEYS: %s",
                 payload_keys, msg_keys, sender_keys)

    # Fanvue nests sender info and message text in sub-objects
    sender = payload.get("sender") or {}
    message_obj = payload.get("message") or {}
    platform_user_id: str = sender.get("uuid", "") or payload.get("senderUuid", "")
    message_text: str = (message_obj.get("text", "") or payload.get("text", "")).strip()
    username: str = sender.get("username", "")
    display_name: str = sender.get("displayName", username)

    # MULTI-MODEL: resolve model context from recipientUuid
    ctx = _get_model_context(payload)
    model_id = ctx.model_id
    creator_uuid = ctx.creator_uuid

    if not platform_user_id:
        return JSONResponse({"status": "ignored"})
    # Media-only messages (no text) are valid -- process them if media handling is enabled
    media_uuids = message_obj.get("mediaUuids") or []
    if not message_text and not has_media:
        # Fanvue webhooks are notification-only — fetch actual text from REST API
        if platform_user_id:
            try:
                from connector.recovery import fetch_fan_messages_since_fanvue
                fetched = await fetch_fan_messages_since_fanvue(
                    creator_uuid, platform_user_id, since_iso=None, limit=5
                )
                if fetched:
                    message_text = fetched[-1].get("text", "").strip()
                    if message_text:
                        logger.info("Fetched message text from API for %s: %s",
                                    platform_user_id[:8], message_text[:80])
            except Exception as e:
                logger.warning("Failed to fetch message text from API: %s", e)
        if not message_text:
            return JSONResponse({"status": "ignored"})

    # Dedup: skip if we've already processed this exact message UUID
    msg_uuid = payload.get("messageUuid") or message_obj.get("uuid", "")
    if msg_uuid and msg_uuid in _processed_message_uuids:
        logger.info(">>> DEDUP: skipping already-processed message %s", msg_uuid)
        return JSONResponse({"status": "duplicate"})
    if msg_uuid:
        _processed_message_uuids[msg_uuid] = True
        # Cap the dedup cache at 500 entries to prevent unbounded growth
        if len(_processed_message_uuids) > 500:
            oldest = list(_processed_message_uuids.keys())[:250]
            for k in oldest:
                del _processed_message_uuids[k]

    async def handle():
        # Record message arrival time for adaptive settle window tracking
        import time as _time
        _sub_last_msg_time[platform_user_id] = _time.monotonic()

        lock = _get_sub_lock(platform_user_id)

        # If lock is held, queue this message and return — the active handler will pick it up
        if lock.locked():
            if platform_user_id not in _sub_queued_messages:
                _sub_queued_messages[platform_user_id] = []
            if message_text:
                _sub_queued_messages[platform_user_id].append(message_text)
            logger.info(">>> QUEUED message for %s (lock held): %s", platform_user_id, (message_text or "[media]")[:50])
            return

        async with lock:
            # Adaptive settle window INSIDE the lock — prevents concurrent handlers
            # from racing through their own settle windows and processing separately.
            await _wait_for_settle(platform_user_id)

            logger.info(">>> HANDLE START: user=%s msg=%s media=%s", platform_user_id, (message_text or "[none]")[:50], bool(has_media))
            if _is_engine_paused():
                logger.info("Engine paused — dropping message from %s", platform_user_id)
                return
            try:
                sub, is_new = _get_or_load_subscriber(
                    platform_user_id, model_id,
                    username=username, display_name=display_name,
                )

                # ── MEDIA PROCESSING ──
                # If has_media, fetch full message details from REST API to get
                # mediaType + pricing. If pricing is set → locked (creator PPV) → skip.
                # If pricing is null → free media → download, analyze, react.
                media_analysis: Optional[MediaAnalysis] = None
                if has_media and media_uuids and os.environ.get("MEDIA_PROCESSING_ENABLED", "").lower() == "true":
                    media_analysis = await _process_incoming_media(
                        creator_uuid, platform_user_id, msg_uuid, media_uuids,
                    )

                if is_new:
                    if ctx.attribution and message_text:
                        attr_result = ctx.attribution.detect(messages=[message_text])
                        if attr_result.detected:
                            sub.persona_id = attr_result.persona_id or ""
                            sub.source_ig_account = attr_result.ig_handle or ""
                            sub.source_detected = True
                    if not sub.persona_id:
                        sub.persona_id = ctx.default_avatar
                    avatar = get_avatar(_avatars, sub.persona_id)

                    if message_text:
                        actions = await orchestrator_process_message(sub, message_text, avatar, model_profile=ctx.model_profile, active_tier_count=ctx.active_tier_count)
                    elif media_analysis:
                        # Media-only first message -- react to media
                        actions = await _media_react(sub, avatar, media_analysis, "", model_profile=ctx.model_profile, active_tier_count=ctx.active_tier_count)
                    else:
                        actions = []
                else:
                    if not sub.persona_id:
                        sub.persona_id = ctx.default_avatar

                    # Combine with any queued messages that arrived during processing
                    combined_text = message_text or ""
                    queued = _sub_queued_messages.pop(platform_user_id, [])
                    if queued:
                        combined_text = (combined_text + "\n" + "\n".join(queued)).strip()
                        logger.info(">>> Combined %d queued messages for %s", len(queued), platform_user_id)

                    avatar = get_avatar(_avatars, sub.persona_id)

                    if combined_text and media_analysis:
                        # Text + media: process text through orchestrator, then append media reaction
                        actions = await orchestrator_process_message(sub, combined_text, avatar, model_profile=ctx.model_profile, active_tier_count=ctx.active_tier_count)
                        media_actions = await _media_react(sub, avatar, media_analysis, combined_text, model_profile=ctx.model_profile, active_tier_count=ctx.active_tier_count)
                        actions.extend(media_actions)
                    elif combined_text:
                        actions = await orchestrator_process_message(sub, combined_text, avatar, model_profile=ctx.model_profile, active_tier_count=ctx.active_tier_count)
                    elif media_analysis:
                        # Media-only (no text) -- react via Media Reactor
                        actions = await _media_react(sub, avatar, media_analysis, "", model_profile=ctx.model_profile, active_tier_count=ctx.active_tier_count)
                    else:
                        actions = []

                # Before sending, check if more messages arrived during orchestrator processing.
                # Regenerate up to 2 times combining all queued messages — prevents "fan answered
                # but I responded as if they hadn't" race conditions.
                accumulated_text = message_text or ""
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
                    if not accumulated_text:
                        break
                    logger.info(">>> REGENERATING (pass %d/%d): %d new msgs arrived -- recomposing for %s",
                                regen_count, max_regens, len(pre_send_queued), platform_user_id)
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
                    actions = await orchestrator_process_message(sub, accumulated_text, avatar, model_profile=ctx.model_profile, active_tier_count=ctx.active_tier_count)
                    if prior_ppv_actions and not is_cancellation:
                        has_ppv_now = any(a.action_type == "send_ppv" for a in (actions or []))
                        if not has_ppv_now:
                            logger.warning(
                                ">>> Regen pass %d dropped PPV — preserving prior PPV action(s) for %s",
                                regen_count, platform_user_id,
                            )
                            actions = list(actions or []) + prior_ppv_actions

                logger.info(">>> Got %d actions from orchestrator", len(actions) if actions else 0)
                save_subscriber(sub, PLATFORM, platform_user_id, model_id)
                await execute_actions(actions, platform_user_id, model_id, sub, creator_uuid=creator_uuid)

                # After executing (with delays), check for EVEN MORE messages.
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
                    actions = await orchestrator_process_message(sub, all_follow_up, avatar, model_profile=ctx.model_profile, active_tier_count=ctx.active_tier_count)
                    save_subscriber(sub, PLATFORM, platform_user_id, model_id)
                    await execute_actions(actions, platform_user_id, model_id, sub, creator_uuid=creator_uuid)

                logger.info(">>> HANDLE COMPLETE for %s", platform_user_id)

            except Exception as exc:
                logger.exception("Error handling message from %s: %s", platform_user_id, exc)
                # Drain queue into stuck state BEFORE losing it
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

    background_tasks.add_task(handle)
    return JSONResponse({"status": "ok"})


@app.post("/webhook/fanvue/new-subscriber")
async def webhook_new_subscriber(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    verify_signature(body, request.headers.get("X-Fanvue-Signature", ""))

    payload = json.loads(body)
    logger.info(">>> NEW-SUBSCRIBER PAYLOAD: %s", json.dumps(payload)[:500])
    # Try nested structure first, fall back to flat
    subscriber = payload.get("subscriber") or payload.get("sender") or {}
    platform_user_id: str = subscriber.get("uuid", "") or payload.get("subscriberUuid", payload.get("userUuid", ""))
    username: str = subscriber.get("username", "") or payload.get("username", "")
    display_name: str = subscriber.get("displayName", username) or payload.get("displayName", username)
    tracking_tag: Optional[str] = payload.get("trackingTag")
    promo_code: Optional[str] = payload.get("promoCode")

    if not platform_user_id:
        return JSONResponse({"status": "ignored"})

    # MULTI-MODEL: resolve model context from recipientUuid
    ctx = _get_model_context(payload)
    model_id = ctx.model_id
    creator_uuid = ctx.creator_uuid

    async def handle():
        try:
            sub = create_subscriber(
                PLATFORM, platform_user_id, model_id,
                username=username, display_name=display_name,
            )

            # Run attribution before new-subscriber processing
            if (tracking_tag or promo_code) and ctx.attribution:
                result = ctx.attribution.detect(
                    tracking_tag=tracking_tag, promo_code=promo_code
                )
                if result.detected:
                    sub.source_ig_account = result.ig_handle or ""
                    sub.persona_id = result.persona_id or ""
                    sub.source_detected = True

            avatar = get_avatar(_avatars, sub.persona_id)
            actions = await orchestrator_process_new_subscriber(sub, avatar)
            save_subscriber(sub, PLATFORM, platform_user_id, model_id)
            await execute_actions(actions, platform_user_id, model_id, sub, creator_uuid=creator_uuid)

        except Exception as exc:
            logger.exception("Error handling new subscriber %s: %s", platform_user_id, exc)
            try:
                from admin_bot.error_alerts import alert_bot_error
                await alert_bot_error(
                    "handle_new_subscriber", exc, platform=PLATFORM, model=model_id,
                    extra_context={"user_uuid": platform_user_id},
                )
            except Exception:
                pass

    background_tasks.add_task(handle)
    return JSONResponse({"status": "ok"})


@app.post("/webhook/fanvue/purchase-received")
async def webhook_purchase_received(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    verify_signature(body, request.headers.get("X-Fanvue-Signature", ""))

    payload = json.loads(body)
    logger.info(">>> PURCHASE PAYLOAD: %s", json.dumps(payload)[:500])
    # Try nested structure first, fall back to flat
    buyer = payload.get("buyer") or payload.get("sender") or {}
    platform_user_id: str = buyer.get("uuid", "") or payload.get("buyerUuid", payload.get("userUuid", ""))
    amount_cents: int = payload.get("amount", 0)
    amount_dollars: float = amount_cents / 100.0
    content_ref: Optional[str] = payload.get("contentId") or payload.get("bundleId")

    if not platform_user_id:
        return JSONResponse({"status": "ignored"})

    # MULTI-MODEL: resolve model context from recipientUuid
    ctx = _get_model_context(payload)
    model_id = ctx.model_id
    creator_uuid = ctx.creator_uuid

    async def handle():
        lock = _get_sub_lock(platform_user_id)
        async with lock:
            try:
                logger.info(">>> PURCHASE START: user=%s amount=$%.2f", platform_user_id, amount_dollars)
                sub, _ = _get_or_load_subscriber(platform_user_id, model_id)
                avatar = get_avatar(_avatars, sub.persona_id)

                # ── GFE Continuation Payment Detection ──
                # If the subscriber has a pending continuation gate and pays,
                # reset the gate and let the conversation resume.
                if sub.gfe_continuation_pending:
                    sub.gfe_continuation_pending = False
                    sub.gfe_continuations_paid += 1
                    sub.gfe_message_count = 0
                    import random as _rnd
                    sub.continuation_threshold_jitter = _rnd.randint(25, 35)
                    logger.info(">>> GFE CONTINUATION PAID by %s ($%.2f) -- counter reset, next threshold=%s, continuations=%d",
                                platform_user_id, amount_dollars, sub.continuation_threshold_jitter, sub.gfe_continuations_paid)
                    # Send a warm "welcome back" instead of normal purchase flow
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
                    record_transaction(
                        sub.sub_id, model_id, "gfe_continuation", amount_dollars,
                        PLATFORM, content_ref,
                    )
                    await alert_purchase(PLATFORM, sub.username, amount_dollars)
                    await execute_actions(actions, platform_user_id, model_id, sub, creator_uuid=creator_uuid)
                    # Process any queued messages now that the gate is cleared
                    queued = _sub_queued_messages.pop(platform_user_id, [])
                    if queued:
                        combined = "\n".join(queued)
                        msg_actions = await orchestrator_process_message(sub, combined, avatar, model_profile=ctx.model_profile, active_tier_count=ctx.active_tier_count)
                        save_subscriber(sub, PLATFORM, platform_user_id, model_id)
                        await execute_actions(msg_actions, platform_user_id, model_id, sub, creator_uuid=creator_uuid)
                    return

                # Custom payment detection — if pending_custom_order exists and this payment
                # roughly matches the quoted price, it's a custom payment, not a tier purchase.
                custom_order = getattr(sub, "pending_custom_order", None)
                if custom_order and custom_order.get("status") in ("pitched", "awaiting_admin_confirm"):
                    quoted = custom_order.get("quoted_price", 0)
                    if abs(amount_dollars - quoted) < 5.0:  # within $5 tolerance
                        logger.info("Custom payment detected: $%.2f matches quoted $%.2f for %s",
                                    amount_dollars, quoted, platform_user_id)
                        from engine.custom_orders import mark_fan_paid
                        sub.pending_custom_order = mark_fan_paid(custom_order)
                        sub.gfe_message_count = 0  # Reset — fan is spending, don't paywall them
                        sub.pending_ppv = None  # Custom-payment PPV was the placeholder; fan paid, clear it
                        sub.custom_request_streak = 0
                        save_subscriber(sub, PLATFORM, platform_user_id, model_id)
                        record_transaction(sub.sub_id, model_id, "custom", amount_dollars, PLATFORM, content_ref)
                        try:
                            from admin_bot.alerts import alert_custom_payment_claim
                            await alert_custom_payment_claim(sub, sub.pending_custom_order)
                        except Exception as e:
                            logger.warning("Custom payment alert failed: %s", e)
                        # Send fan confirmation that we're verifying
                        await send_fanvue_message(creator_uuid, platform_user_id,
                            "got it baby, let me verify real quick and then I'll start working on your custom for you")
                        return

                # Fan paid — clear pending_ppv + reset custom streak
                if sub.pending_ppv:
                    logger.info("Clearing pending_ppv (tier=%s) after purchase by %s",
                                sub.pending_ppv.get("tier"), platform_user_id)
                    sub.pending_ppv = None
                sub.custom_request_streak = 0

                actions = await orchestrator_process_purchase(sub, amount_dollars, avatar, model_profile=ctx.model_profile, active_tier_count=ctx.active_tier_count)

                save_subscriber(sub, PLATFORM, platform_user_id, model_id)
                record_transaction(
                    sub.sub_id, model_id, "ppv", amount_dollars,
                    PLATFORM, content_ref,
                )
                logger.info(
                    "Purchase: %s paid $%.2f for %s",
                    platform_user_id, amount_dollars, content_ref,
                )
                await alert_purchase(PLATFORM, sub.username, amount_dollars)
                await _check_whale_escalation(sub, PLATFORM)

                if actions:
                    await execute_actions(actions, platform_user_id, model_id, sub, creator_uuid=creator_uuid)
                    save_subscriber(sub, PLATFORM, platform_user_id, model_id)

                # Process any fan messages that queued while we held the lock
                queued = _sub_queued_messages.pop(platform_user_id, [])
                if queued:
                    logger.info(">>> Processing %d queued messages after purchase for %s", len(queued), platform_user_id)
                    sub = load_subscriber(PLATFORM, platform_user_id, model_id) or sub
                    avatar = get_avatar(_avatars, sub.persona_id)
                    combined = "\n".join(queued)
                    msg_actions = await orchestrator_process_message(sub, combined, avatar, model_profile=ctx.model_profile, active_tier_count=ctx.active_tier_count)
                    save_subscriber(sub, PLATFORM, platform_user_id, model_id)
                    await execute_actions(msg_actions, platform_user_id, model_id, sub, creator_uuid=creator_uuid)

                logger.info(">>> PURCHASE COMPLETE: ppv_count=%d total=$%.2f", sub.spending.ppv_count, sub.spending.total_spent)
            except Exception as exc:
                logger.exception("Error handling purchase from %s: %s", platform_user_id, exc)
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
                        mark_stuck(stuck_sub, "handle_purchase", exc, manual_only=True)
                        try:
                            save_subscriber(stuck_sub, PLATFORM, platform_user_id, model_id)
                        except Exception:
                            pass
                    await alert_bot_error(
                        "handle_purchase", exc, sub=stuck_sub,
                        platform=PLATFORM, model=model_id,
                        extra_context={"manual_confirm_required": True},
                    )
                except Exception:
                    pass

    background_tasks.add_task(handle)
    return JSONResponse({"status": "ok"})


@app.post("/webhook/fanvue/tip-received")
async def webhook_tip_received(request: Request, background_tasks: BackgroundTasks):
    body = await request.body()
    verify_signature(body, request.headers.get("X-Fanvue-Signature", ""))

    payload = json.loads(body)
    platform_user_id: str = payload.get("tipperUuid", payload.get("userUuid", ""))
    amount_cents: int = payload.get("amount", 0)
    amount_dollars: float = amount_cents / 100.0

    if not platform_user_id:
        return JSONResponse({"status": "ignored"})

    # MULTI-MODEL: resolve model context from recipientUuid
    ctx = _get_model_context(payload)
    model_id = ctx.model_id

    async def handle():
        try:
            sub, _ = _get_or_load_subscriber(platform_user_id, model_id)
            sub.record_purchase(amount_dollars, "tip")
            save_subscriber(sub, PLATFORM, platform_user_id, model_id)
            record_transaction(sub.sub_id, model_id, "tip", amount_dollars, PLATFORM)
            logger.info("Tip: %s sent $%.2f", platform_user_id, amount_dollars)
        except Exception as exc:
            logger.exception("Error handling tip from %s: %s", platform_user_id, exc)
            try:
                from admin_bot.error_alerts import alert_bot_error
                await alert_bot_error(
                    "handle_tip", exc, platform=PLATFORM, model=model_id,
                    extra_context={"user_uuid": platform_user_id, "amount_dollars": amount_dollars},
                )
            except Exception:
                pass

    background_tasks.add_task(handle)
    return JSONResponse({"status": "ok"})


@app.post("/webhook/fanvue/message-read")
async def webhook_message_read(request: Request):
    body = await request.body()
    verify_signature(body, request.headers.get("X-Fanvue-Signature", ""))
    return JSONResponse({"status": "ok"})


@app.post("/webhook/fanvue/new-follower")
async def webhook_new_follower(request: Request):
    body = await request.body()
    verify_signature(body, request.headers.get("X-Fanvue-Signature", ""))
    payload = json.loads(body)
    logger.info("New follower: %s", payload.get("followerUuid", "unknown"))
    return JSONResponse({"status": "ok"})


# ─────────────────────────────────────────────
# Test endpoint (simulate purchase — no signature required)
# ─────────────────────────────────────────────

@app.post("/test/simulate-purchase")
async def test_simulate_purchase(request: Request, background_tasks: BackgroundTasks):
    """Simulate a purchase event for testing. No HMAC verification."""
    payload = await request.json()
    fan_uuid: str = payload.get("fan_uuid", "")
    amount_dollars: float = payload.get("amount", 0.0)
    tier: int = payload.get("tier", 1)

    if not fan_uuid:
        return JSONResponse({"status": "error", "detail": "fan_uuid required"})

    # MULTI-MODEL: use fallback model for test endpoint
    # Try to get context from payload (may have recipientUuid), else use fallback
    fallback_uuid = os.environ.get("FANVUE_CREATOR_UUID", "")
    try:
        ctx = _get_model_context(payload)
    except HTTPException:
        # No recipientUuid in test payload -- build a fallback context
        model_id = _fallback_model_id or os.environ.get("FANVUE_MODEL_ID", "")
        if not model_id:
            return JSONResponse({"status": "error", "detail": "No model configured"})
        if fallback_uuid not in _model_contexts:
            _model_contexts[fallback_uuid] = _build_model_context(model_id, fallback_uuid)
        ctx = _model_contexts[fallback_uuid]

    model_id = ctx.model_id
    creator_uuid = ctx.creator_uuid

    async def handle():
        lock = _get_sub_lock(fan_uuid)
        async with lock:
            try:
                sub, _ = _get_or_load_subscriber(fan_uuid, model_id)
                avatar = get_avatar(_avatars, sub.persona_id)
                logger.info(">>> TEST PURCHASE: fan=%s amount=$%.2f tier=%d", fan_uuid, amount_dollars, tier)

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
                        save_subscriber(sub, PLATFORM, fan_uuid, model_id)
                        record_transaction(sub.sub_id, model_id, "custom", amount_dollars, PLATFORM)
                        try:
                            from admin_bot.alerts import alert_custom_payment_claim
                            await alert_custom_payment_claim(sub, sub.pending_custom_order)
                        except Exception as e:
                            logger.warning("Custom payment alert failed: %s", e)
                        await send_fanvue_message(creator_uuid, fan_uuid, "got it baby, let me verify real quick and then I'll start working on your custom for you")
                        return

                # Fan paid — clear pending_ppv + reset custom streak
                if sub.pending_ppv:
                    sub.pending_ppv = None
                sub.custom_request_streak = 0

                actions = await orchestrator_process_purchase(sub, amount_dollars, avatar, model_profile=ctx.model_profile, active_tier_count=ctx.active_tier_count)

                save_subscriber(sub, PLATFORM, fan_uuid, model_id)
                record_transaction(sub.sub_id, model_id, "ppv", amount_dollars, PLATFORM)
                logger.info(">>> TEST PURCHASE: got %d actions", len(actions) if actions else 0)

                if actions:
                    await execute_actions(actions, fan_uuid, model_id, sub, creator_uuid=creator_uuid)
                    save_subscriber(sub, PLATFORM, fan_uuid, model_id)

                # Process any fan messages that queued while we held the lock
                queued = _sub_queued_messages.pop(fan_uuid, [])
                if queued:
                    logger.info(">>> Processing %d queued messages after test purchase for %s", len(queued), fan_uuid)
                    sub = load_subscriber(PLATFORM, fan_uuid, model_id) or sub
                    avatar = get_avatar(_avatars, sub.persona_id)
                    combined = "\n".join(queued)
                    msg_actions = await orchestrator_process_message(sub, combined, avatar, model_profile=ctx.model_profile, active_tier_count=ctx.active_tier_count)
                    save_subscriber(sub, PLATFORM, fan_uuid, model_id)
                    await execute_actions(msg_actions, fan_uuid, model_id, sub, creator_uuid=creator_uuid)

                logger.info(">>> TEST PURCHASE COMPLETE: ppv_count=%d total=$%.2f",
                            sub.spending.ppv_count, sub.spending.total_spent)
            except Exception as exc:
                logger.exception("Test purchase error: %s", exc)
                try:
                    from admin_bot.error_alerts import alert_bot_error
                    await alert_bot_error(
                        "test_simulate_purchase", exc, platform=PLATFORM, model=model_id,
                        extra_context={"fan_uuid": fan_uuid, "amount": amount_dollars},
                    )
                except Exception:
                    pass

    background_tasks.add_task(handle)
    return JSONResponse({"status": "ok", "amount": amount_dollars, "tier": tier})


# ─────────────────────────────────────────────
# OAuth 2.0 PKCE flow
# ─────────────────────────────────────────────

_pkce_store: dict[str, str] = {}


@app.get("/oauth/start")
async def oauth_start():
    """Generate a PKCE authorization URL and redirect to Fanvue's auth page."""
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    state = secrets.token_hex(16)

    _pkce_store[state] = code_verifier

    domain = os.environ["DOMAIN"]
    client_id = os.environ["FANVUE_CLIENT_ID"]
    redirect_uri = f"https://{domain}/oauth/callback"

    scopes = (
        "openid offline_access "
        "read:chat write:chat read:fan read:self read:insights "
        "read:media write:media read:creator write:creator "
        "read:agency write:agency read:tracking_links write:tracking_links "
        "read:post write:post"
    )

    auth_url = (
        "https://auth.fanvue.com/oauth2/auth"
        f"?client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&response_type=code"
        f"&scope={scopes.replace(' ', '+')}"
        f"&state={state}"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
    )

    return HTMLResponse(
        f"<html><body>"
        f"<p>Click to authorize Massi-Bot with Fanvue:</p>"
        f"<a href='{auth_url}'>Authorize</a>"
        f"</body></html>"
    )


@app.get("/oauth/callback")
async def oauth_callback(code: str, state: str):
    """Handle the OAuth authorization code callback from Fanvue."""
    code_verifier = _pkce_store.pop(state, None)
    if not code_verifier:
        raise HTTPException(400, "Invalid or expired OAuth state parameter")

    try:
        await token_manager.exchange_code(code, code_verifier)
    except Exception as exc:
        logger.exception("OAuth exchange failed: %s", exc)
        raise HTTPException(500, f"Token exchange failed: {exc}")

    return HTMLResponse(
        "<html><body>"
        "<h2>Authorization successful!</h2>"
        "<p>Fanvue tokens stored. The bot is now operational.</p>"
        "</body></html>"
    )


# ─────────────────────────────────────────────
# Health + status
# ─────────────────────────────────────────────

@app.get("/health")
async def health():
    has_tokens = await token_manager.has_tokens()
    models = {ctx.stage_name: ctx.model_id[:8] for ctx in _model_contexts.values()}
    return JSONResponse({
        "status": "ok",
        "platform": "fanvue",
        "engine_ready": bool(_avatars),
        "tokens_present": has_tokens,
        "models_loaded": len(_model_contexts),
        "models": models,
    })


@app.get("/")
async def root():
    return JSONResponse({"service": "Massi-Bot Fanvue Connector", "version": "2.0.0"})
