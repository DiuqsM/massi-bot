"""
Massi-Bot Admin Bot

Telegram management bot in polling mode.
Commands:
  /start    — Welcome + command list
  /stats    — Subscriber + revenue overview
  /revenue  — Revenue breakdown by tier and platform
  /subs     — Recent subscribers (last 10)
  /whales   — Top whale subscribers
  /readiness — Content catalog tier readiness
  /pause    — Pause the engine (sets Redis flag)
  /resume   — Resume the engine (clears Redis flag)
  /override <user_id> <message> — Send message to specific subscriber
  /set_uuid <bundle_id> <fanvue_uuid> — Set Fanvue media UUID on a bundle
  /help     — Command list

Content intake: Send any photo/video to trigger the upload flow.

Run with: python3 -m admin_bot.bot
"""

import os
import sys
import json
import logging
import asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'engine'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# Sentry (optional — only initializes if SENTRY_DSN is set)
_SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if _SENTRY_DSN:
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=_SENTRY_DSN,
            traces_sample_rate=0.05,
            environment=os.environ.get("ENVIRONMENT", "production"),
        )
        logger = logging.getLogger(__name__)
        logging.basicConfig()
        logger.info("Sentry initialized for admin_bot")
    except Exception:
        pass

import redis
from telegram import Update, BotCommand
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    MessageHandler, filters,
)

from persistence.subscriber_store import (
    get_subscribers_by_state, get_top_whales,
    get_subscriber_count, load_subscriber,
)
from persistence.content_store import get_catalog_readiness
from persistence.supabase_client import get_client
from admin_bot.content_intake import build_content_intake_handler, cmd_register_of
from models import SubState

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)

PLATFORM = "fanvue"
ENGINE_PAUSED_KEY = "engine:paused"


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def _model_id() -> str:
    mid = os.environ.get("FANVUE_MODEL_ID", "")
    if not mid:
        raise RuntimeError("FANVUE_MODEL_ID not set")
    return mid


def _redis() -> redis.Redis:
    return redis.from_url(os.environ["REDIS_URL"], decode_responses=True)


def _is_paused() -> bool:
    try:
        return bool(_redis().get(ENGINE_PAUSED_KEY))
    except Exception:
        return False


def _get_revenue_stats(model_id: str) -> dict:
    """Pull revenue aggregates from the transactions table."""
    db = get_client()
    result = db.table("transactions").select("type,amount").eq("model_id", model_id).execute()
    rows = result.data or []

    total = 0.0
    by_type: dict[str, float] = {}
    for row in rows:
        amt = float(row.get("amount", 0))
        tx_type = row.get("type", "unknown")
        total += amt
        by_type[tx_type] = by_type.get(tx_type, 0.0) + amt

    return {"total": total, "by_type": by_type, "count": len(rows)}


def _get_sub_stats(model_id: str) -> dict:
    """Count subscribers by state."""
    db = get_client()
    result = (
        db.table("subscribers")
        .select("state,total_spent,whale_score")
        .eq("model_id", model_id)
        .execute()
    )
    rows = result.data or []
    total = len(rows)
    buyers = sum(1 for r in rows if float(r.get("total_spent", 0)) > 0)
    whales = sum(1 for r in rows if int(r.get("whale_score", 0)) >= 50)
    total_revenue = sum(float(r.get("total_spent", 0)) for r in rows)
    return {
        "total": total,
        "buyers": buyers,
        "whales": whales,
        "total_revenue": total_revenue,
        "conversion_rate": buyers / total if total else 0,
        "avg_per_sub": total_revenue / total if total else 0,
        "avg_per_buyer": total_revenue / buyers if buyers else 0,
    }


def _admin_filter():
    """Filter that only allows messages from admin chat IDs."""
    raw = os.environ.get("TELEGRAM_ADMIN_IDS", "")
    ids = [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]
    return filters.Chat(chat_id=ids) if ids else filters.ALL


# ─────────────────────────────────────────────
# Command handlers
# ─────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🤖 <b>Massi-Bot Manager Bot</b>\n\n"
        "Commands:\n"
        "/stats — Subscriber &amp; revenue overview\n"
        "/revenue — Revenue breakdown\n"
        "/subs — Recent subscribers\n"
        "/whales — Top whale subscribers\n"
        "/readiness — Content catalog status\n"
        "/pause — Pause the engine\n"
        "/resume — Resume the engine\n"
        "/override &lt;user_id&gt; &lt;message&gt; — Send message to subscriber\n"
        "/set_uuid &lt;bundle_id&gt; &lt;fanvue_uuid&gt; — Set Fanvue media UUID\n"
        "/register_of [bundle_id] — Register OF media IDs from model chat\n\n"
        "📸 Send any photo/video to upload content."
    )
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        model_id = _model_id()
        stats = _get_sub_stats(model_id)
        paused = _is_paused()
        engine_status = "⏸ PAUSED" if paused else "✅ Active"

        text = (
            f"📊 <b>Massi-Bot Stats</b>\n"
            f"{'━'*20}\n"
            f"Subscribers: <b>{stats['total']}</b>\n"
            f"Buyers: <b>{stats['buyers']}</b> ({stats['conversion_rate']:.1%})\n"
            f"Whales (score≥50): <b>{stats['whales']}</b>\n\n"
            f"Revenue: <b>${stats['total_revenue']:.2f}</b>\n"
            f"Avg/sub: ${stats['avg_per_sub']:.2f}\n"
            f"Avg/buyer: ${stats['avg_per_buyer']:.2f}\n\n"
            f"Engine: {engine_status}"
        )
    except Exception as exc:
        text = f"❌ Error fetching stats: {exc}"
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_revenue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        model_id = _model_id()
        rev = _get_revenue_stats(model_id)
        by_type = rev["by_type"]

        lines = [f"💰 <b>Revenue</b>\n{'━'*20}"]
        lines.append(f"Total transactions: {rev['count']}")
        lines.append(f"Total revenue: <b>${rev['total']:.2f}</b>\n")

        type_labels = {
            "ppv": "PPV Sales",
            "tip": "Tips",
            "subscription": "Subscriptions",
            "custom": "Custom Content",
        }
        for tx_type, amount in sorted(by_type.items(), key=lambda x: -x[1]):
            label = type_labels.get(tx_type, tx_type.title())
            lines.append(f"{label}: ${amount:.2f}")

        text = "\n".join(lines)
    except Exception as exc:
        text = f"❌ Error: {exc}"
    await update.message.reply_text(text, parse_mode="HTML")


async def cmd_subs(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        model_id = _model_id()
        db = get_client()
        result = (
            db.table("subscribers")
            .select("username,state,total_spent,whale_score,created_at")
            .eq("model_id", model_id)
            .order("created_at", desc=True)
            .limit(10)
            .execute()
        )
        rows = result.data or []
        if not rows:
            await update.message.reply_text("No subscribers yet.")
            return

        lines = [f"👥 <b>Recent Subscribers</b> (last {len(rows)})\n{'━'*20}"]
        for r in rows:
            username = r.get("username", "unknown")
            state = r.get("state", "?")
            spent = float(r.get("total_spent", 0))
            score = int(r.get("whale_score", 0))
            whale = " 🐋" if score >= 50 else ""
            lines.append(
                f"@{username} — {state} — ${spent:.2f}{whale}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"❌ Error: {exc}")


async def cmd_whales(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        model_id = _model_id()
        whales = get_top_whales(model_id, limit=10)
        if not whales:
            await update.message.reply_text("No whale subscribers yet.")
            return

        lines = [f"🐋 <b>Top Whales</b>\n{'━'*20}"]
        for sub in whales:
            lines.append(
                f"@{sub.username} — score {sub.whale_score} — "
                f"${sub.spending.total_spent:.2f} — {sub.state.value}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"❌ Error: {exc}")


async def cmd_readiness(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        model_id = _model_id()
        report = get_catalog_readiness(model_id)
        status = "✅ READY" if report["ready"] else "❌ NOT READY"

        lines = [f"📦 <b>Content Readiness: {status}</b>\n{'━'*20}"]
        for tier in report["tiers"]:
            has_uuid = "✅" if tier["has_fanvue_uuid"] else "⚠️"
            lines.append(
                f"Tier {tier['tier']} {tier['name']} — "
                f"{tier['bundle_count']} bundles {has_uuid}"
            )
        lines.append(f"\nTotal bundles: {report['total_bundles']}")
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"❌ Error: {exc}")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        _redis().set(ENGINE_PAUSED_KEY, "1")
        await update.message.reply_text("⏸ <b>Engine PAUSED.</b> No new messages will be processed.", parse_mode="HTML")
        logger.info("Engine paused by admin")
    except Exception as exc:
        await update.message.reply_text(f"❌ Error: {exc}")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        _redis().delete(ENGINE_PAUSED_KEY)
        await update.message.reply_text("▶️ <b>Engine RESUMED.</b> Processing messages normally.", parse_mode="HTML")
        logger.info("Engine resumed by admin")
    except Exception as exc:
        await update.message.reply_text(f"❌ Error: {exc}")


async def cmd_override(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /override <platform_user_id> <message text>
    Sends a message directly to a subscriber bypassing the engine.
    """
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Usage: /override &lt;platform_user_id&gt; &lt;message&gt;",
            parse_mode="HTML",
        )
        return

    platform_user_id = args[0]
    message_text = " ".join(args[1:])

    # Store the override message in Redis for the connector to pick up
    override_key = f"override:{PLATFORM}:{platform_user_id}"
    try:
        _redis().lpush(override_key, message_text)
        _redis().expire(override_key, 3600)  # TTL 1 hour
        await update.message.reply_text(
            f"✅ Override queued for <code>{platform_user_id}</code>:\n{message_text}",
            parse_mode="HTML",
        )
        logger.info("Override queued for %s: %s", platform_user_id, message_text)
    except Exception as exc:
        await update.message.reply_text(f"❌ Error: {exc}")


async def cmd_set_uuid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /set_uuid <bundle_id> <fanvue_media_uuid>
    Sets the Fanvue media UUID on a content_catalog entry.
    """
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text(
            "Usage: /set_uuid &lt;bundle_id&gt; &lt;fanvue_media_uuid&gt;",
            parse_mode="HTML",
        )
        return

    bundle_id = args[0]
    fanvue_uuid = args[1]

    try:
        from persistence.content_store import update_fanvue_uuid
        model_id = _model_id()
        update_fanvue_uuid(model_id, bundle_id, fanvue_uuid)
        await update.message.reply_text(
            f"✅ Fanvue UUID set:\n"
            f"Bundle: <code>{bundle_id}</code>\n"
            f"UUID: <code>{fanvue_uuid}</code>",
            parse_mode="HTML",
        )
    except Exception as exc:
        await update.message.reply_text(f"❌ Error: {exc}")


async def cmd_fan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /fan @username  — show full profile for a specific fan
    """
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /fan @username  or  /fan username", parse_mode="HTML")
        return

    username = args[0].lstrip("@").lower()
    try:
        model_id = _model_id()
        db = get_client()
        # Try username first, then display_name as fallback (Fanvue often stores blank username)
        result = (
            db.table("subscribers")
            .select("*")
            .eq("model_id", model_id)
            .ilike("username", username)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            # Try matching display_name (e.g. "giant-unicorn-129" → "Giant Unicorn" won't match,
            # but exact handle stored in display_name field might)
            display_search = username.replace("-", " ")
            result = (
                db.table("subscribers")
                .select("*")
                .eq("model_id", model_id)
                .ilike("display_name", f"%{display_search}%")
                .limit(1)
                .execute()
            )
            rows = result.data or []
        if not rows:
            await update.message.reply_text(f"No fan found: @{username}\nTip: try part of their display name, e.g. /fan giant")
            return

        r = rows[0]
        qd = r.get("qualifying_data") or {}

        horniness = qd.get("horniness_score", 0)
        bar_filled = "🟥" * horniness + "⬜" * (10 - horniness)

        whale = int(r.get("whale_score", 0))
        whale_bar = "🐋" * min(whale // 20, 5)

        tiers = qd.get("spending", {}).get("ppv_count", 0) if isinstance(qd.get("spending"), dict) else 0
        total_spent = float(r.get("total_spent") or 0)
        state = r.get("state", "?")
        platform_id = r.get("platform_user_id", "?")
        created = (r.get("created_at") or "")[:10]
        sext_consent = qd.get("sext_consent_given", False)
        pending_ppv = qd.get("pending_ppv")
        pending_str = f"Tier {pending_ppv.get('tier')} (${pending_ppv.get('price', 0):.2f})" if pending_ppv else "none"

        fan_name = qd.get("fan_name", "").strip()
        fp = qd.get("fan_profile") or {}
        fp_personality = (fp.get("personality") or "").strip()
        fp_interests = [i for i in (fp.get("interests") or []) if i]
        fp_kinks = [k for k in (fp.get("kinks") or []) if k]
        fp_notes = (fp.get("notes") or "").strip()

        lines = [
            f"👤 <b>@{username}</b>",
            f"Platform ID: <code>{str(platform_id)[:16]}</code>",
            f"Joined: {created}",
            f"",
            f"🌡 <b>Horniness:</b> {horniness}/10",
            f"{bar_filled}",
            f"",
            f"🐋 <b>Whale score:</b> {whale}/100 {whale_bar}",
            f"💰 <b>Total spent:</b> ${total_spent:.2f}",
            f"📦 <b>Tiers purchased:</b> {tiers}",
            f"",
            f"🔒 <b>State:</b> {state}",
            f"✅ <b>Sext consent:</b> {'yes (legacy)' if sext_consent else 'via score'}",
            f"📬 <b>Pending PPV:</b> {pending_str}",
            f"",
            f"🏷 <b>Tags:</b> {', '.join(qd.get('tags') or []) or '(none)'}",
            f"",
            f"📋 <b>Fan Profile</b>",
            f"Name: {fan_name or '(unknown)'}",
            f"Personality: {fp_personality or '(not yet observed)'}",
            f"Interests: {', '.join(fp_interests) or '(none yet)'}",
            f"Kinks: {', '.join(fp_kinks) or '(none yet)'}",
            f"Notes: {fp_notes or '(none)'}",
        ]
        await update.message.reply_text("\n".join(lines), parse_mode="HTML")
    except Exception as exc:
        await update.message.reply_text(f"❌ Error: {exc}")


# ─────────────────────────────────────────────
# Bot setup + entry point
# ─────────────────────────────────────────────

def build_application() -> Application:
    """Build and configure the bot Application."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    admin_filter = _admin_filter()

    app = Application.builder().token(token).build()

    # Content intake (bare photo/video sends go here)
    app.add_handler(build_content_intake_handler())

    # Commands (admin-only)
    for cmd, handler in [
        ("start", cmd_start),
        ("help", cmd_help),
        ("stats", cmd_stats),
        ("revenue", cmd_revenue),
        ("subs", cmd_subs),
        ("whales", cmd_whales),
        ("readiness", cmd_readiness),
        ("pause", cmd_pause),
        ("resume", cmd_resume),
        ("override", cmd_override),
        ("set_uuid", cmd_set_uuid),
        ("register_of", cmd_register_of),
        ("fan", cmd_fan),
    ]:
        app.add_handler(CommandHandler(cmd, handler, filters=admin_filter))

    # Error handler — suppress transient NetworkErrors from Sentry
    app.add_error_handler(_error_handler)

    return app


async def _error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle errors. NetworkError (Bad Gateway, etc.) are transient — log as warning only."""
    from telegram.error import NetworkError, TimedOut
    if isinstance(context.error, (NetworkError, TimedOut)):
        logger.warning("Transient Telegram network error (ignored): %s", context.error)
        return
    logger.exception("Unhandled bot error: %s", context.error, exc_info=context.error)


async def _set_bot_commands(app: Application) -> None:
    """Register command list in Telegram so it shows in the menu."""
    commands = [
        BotCommand("stats", "Subscriber & revenue overview"),
        BotCommand("revenue", "Revenue breakdown"),
        BotCommand("subs", "Recent subscribers"),
        BotCommand("whales", "Top whale subscribers"),
        BotCommand("readiness", "Content catalog status"),
        BotCommand("pause", "Pause the engine"),
        BotCommand("resume", "Resume the engine"),
        BotCommand("override", "Send message to subscriber: /override <id> <msg>"),
        BotCommand("set_uuid", "Set Fanvue media UUID: /set_uuid <bundle_id> <uuid>"),
        BotCommand("register_of", "Register OF media IDs: /register_of [bundle_id]"),
        BotCommand("help", "Command list"),
        BotCommand("gen_bundle", "Generate AI content bundle: /gen_bundle <char> <session> <tier>"),
        BotCommand("gen_video", "Generate video from image: /gen_video <char> <session> <tier>"),
        BotCommand("gen_chars", "List AI character IDs"),
        BotCommand("gen_status", "AI generation configuration status"),
        BotCommand("setface", "Set reference face (reply to photo): /setface <char>"),
        BotCommand("facelock", "Generate with face lock: /facelock <char> <prompt>"),
        BotCommand("swap", "Face swap into scene (reply to photo): /swap <char>"),
        BotCommand("undress", "Remove clothing (reply to photo): /undress <char>"),
        BotCommand("reface", "Face cleanup (reply to photo): /reface <char>"),
        BotCommand("video", "Animate image (reply to photo): /video <char> [motion]"),
        BotCommand("qwen_edit", "Qwen image edit (reply to photo): /qwen_edit <instruction>"),
        BotCommand("skin_fix", "Qwen skin enhancement (reply to photo)"),
        BotCommand("make_real", "AI-to-realism conversion (reply to photo)"),
        BotCommand("angle", "Change camera angle (reply to photo): /angle <instruction>"),
        BotCommand("train_zimage", "Z-Image LoRA training: /train_zimage <char> [--force]"),
        BotCommand("fix_face", "Fix face in image (reply to photo): /fix_face <char>"),
        BotCommand("inpaint", "Inpaint region (reply to photo): /inpaint <char> <target> <prompt>"),
        BotCommand("pose", "Pose-controlled gen (reply to photo): /pose <char> <prompt>"),
        BotCommand("depth", "Depth-controlled gen (reply to photo): /depth <char> <prompt>"),
        BotCommand("upscale", "Upscale photos/videos"),
        BotCommand("upscale_status", "Monthly upscale usage and cost"),
    ]
    await app.bot.set_my_commands(commands)
    logger.info("Bot commands registered")


def main() -> None:
    """Start the bot in polling mode."""
    from dotenv import load_dotenv
    load_dotenv()

    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )

    app = build_application()

    async def post_init(application: Application) -> None:
        await _set_bot_commands(application)

    app.post_init = post_init

    logger.info("Starting Massi-Bot admin bot in polling mode")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
