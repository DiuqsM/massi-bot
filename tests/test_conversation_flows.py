"""
Massi-Bot — Conversation Flow Tests

Scripted multi-turn flows that verify the orchestrator advances state,
fires PPVs, handles objections, and manages special events correctly —
without making real LLM or Supabase calls.

Each test controls what the "agent" returns at each turn via
side_effect iterators on single_agent_process, then asserts on
the resulting BotActions and subscriber state.

Run with:
    cd ~/massi-bot && python -m pytest tests/test_conversation_flows.py -v
"""

import sys
import os
import asyncio
import pytest
import random
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))

from engine.models import Subscriber, SpendingHistory, SubState, BotAction


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def make_sub(**kwargs) -> Subscriber:
    sub = Subscriber(sub_id="flow-test-001", username="test_fan")
    sub.spending = SpendingHistory()
    for k, v in kwargs.items():
        setattr(sub, k, v)
    return sub


def make_avatar():
    av = MagicMock()
    av.persona.name = "Jasmine"
    av.persona.ig_account_tag = "test_model"
    av.persona.voice.primary_tone = "flirty"
    av.persona.voice.flirt_style = "playful"
    av.persona.voice.capitalization = "lowercase_casual"
    av.persona.voice.emoji_use = "light"
    av.persona.voice.punctuation_style = "minimal"
    return av


def agent_says(text="hey 😏", ppv=None, consent=False, horniness=3):
    """Build a single-agent result dict."""
    return {
        "messages": [{"text": text, "delay_seconds": 8}],
        "ppv": ppv,
        "consent_given": consent,
        "consent_declined": False,
        "horniness_score": horniness,
        "fan_name": None,
        "fan_profile_update": None,
    }


def agent_says_ppv(tier: int, heads_up: str = "give me a sec babe", caption: str = "just for you 😈"):
    return agent_says(
        text="omg ok wait",
        ppv={"tier": tier, "heads_up": heads_up, "caption": caption},
        horniness=7,
    )


_BASE_CONTEXT = {
    "relationship_summary": "", "session_arc": "", "open_threads": [],
    "tier_content_awareness": "", "time_since_last_fan_message": "2 min",
    "goodbye_state": {}, "memories": [], "callback_refs": [], "persona_facts": [],
    "live_context": "", "model_profile": None,
}


def _patch_orchestrator(agent_results):
    """
    Context manager that patches all external I/O in agents.orchestrator.
    agent_results: a list of dicts returned by single_agent_process in order,
                   or a single dict returned for every call.
    """
    if isinstance(agent_results, list):
        side_effect = iter(agent_results)
        agent_mock = AsyncMock(side_effect=lambda **_kw: next(side_effect))
    else:
        agent_mock = AsyncMock(return_value=agent_results)

    return patch.multiple(
        "agents.orchestrator",
        build_context=AsyncMock(return_value=_BASE_CONTEXT.copy()),
        single_agent_process=agent_mock,
        run_all_guardrails=AsyncMock(return_value=(True, [])),
        record_bot_message_sent=AsyncMock(),
        update_callback_references=MagicMock(return_value=None),
        memory_manager=MagicMock(
            maybe_extract_and_store=AsyncMock(),
            maybe_store_persona_facts=AsyncMock(),
        ),
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. New Subscriber Welcome Flow
# ══════════════════════════════════════════════════════════════════════════════

class TestNewSubscriberFlow:

    @pytest.mark.anyio
    async def test_welcome_returns_send_message_action(self):
        from agents.orchestrator import process_new_subscriber
        sub = make_sub()
        with _patch_orchestrator(agent_says("hey 😏 what caught your eye?")):
            actions = await process_new_subscriber(sub, make_avatar())
        assert len(actions) > 0
        assert actions[0].action_type == "send_message"
        assert actions[0].message == "hey 😏 what caught your eye?"

    @pytest.mark.anyio
    async def test_welcome_advances_state_to_welcome_sent(self):
        from agents.orchestrator import process_new_subscriber
        sub = make_sub()
        with _patch_orchestrator(agent_says("hey")):
            await process_new_subscriber(sub, make_avatar())
        assert sub.state.value == "welcome_sent"

    @pytest.mark.anyio
    async def test_welcome_message_recorded_in_history(self):
        from agents.orchestrator import process_new_subscriber
        sub = make_sub()
        with _patch_orchestrator(agent_says("omg hi 😍")):
            await process_new_subscriber(sub, make_avatar())
        bot_msgs = [m["content"] for m in sub.recent_messages if m["role"] == "bot"]
        assert "omg hi 😍" in bot_msgs

    @pytest.mark.anyio
    async def test_welcome_fallback_on_agent_error(self):
        from agents.orchestrator import process_new_subscriber
        sub = make_sub()
        with patch("agents.orchestrator.build_context", side_effect=RuntimeError("boom")):
            actions = await process_new_subscriber(sub, make_avatar())
        assert len(actions) == 1
        assert actions[0].action_type == "send_message"

    @pytest.mark.anyio
    async def test_first_fan_message_after_welcome(self):
        from agents.orchestrator import process_message
        sub = make_sub(state=SubState.WELCOME_SENT)
        with _patch_orchestrator(agent_says("tell me more about yourself 😏")):
            actions = await process_message(sub, "hey what's up", make_avatar())
        assert len(actions) > 0
        # add_message("sub",...) increments by 1, then orchestrator does += 1 → 2 per turn
        assert sub.message_count == 2


# ══════════════════════════════════════════════════════════════════════════════
# 2. PPV Tier Ladder Flow
# ══════════════════════════════════════════════════════════════════════════════

class TestTierLadderFlow:

    @pytest.mark.anyio
    async def test_agent_ppv_intent_produces_send_ppv_action(self):
        from agents.orchestrator import process_message
        sub = make_sub(sext_consent_given=True, horniness_score=7)
        with _patch_orchestrator(agent_says_ppv(tier=1)):
            actions = await process_message(sub, "show me more", make_avatar())
        ppv_actions = [a for a in actions if a.action_type == "send_ppv"]
        assert len(ppv_actions) == 1
        assert ppv_actions[0].ppv_price == 27.38

    @pytest.mark.anyio
    async def test_heads_up_message_sent_before_ppv(self):
        from agents.orchestrator import process_message
        sub = make_sub(sext_consent_given=True, horniness_score=7)
        with _patch_orchestrator(agent_says_ppv(tier=1, heads_up="give me a sec babe")):
            actions = await process_message(sub, "omg yes", make_avatar())
        types = [a.action_type for a in actions]
        # heads-up message must come before the PPV
        assert "send_message" in types
        assert "send_ppv" in types
        assert types.index("send_message") < types.index("send_ppv")

    @pytest.mark.anyio
    async def test_purchase_increments_ppv_count(self):
        from agents.orchestrator import process_purchase
        sub = make_sub()
        with _patch_orchestrator(agent_says("omg you opened it 😍")):
            await process_purchase(sub, 27.38, make_avatar(), content_type="ppv")
        assert sub.spending.ppv_count == 1
        assert sub.spending.total_spent == 27.38

    @pytest.mark.anyio
    async def test_purchase_resets_gfe_message_count(self):
        from agents.orchestrator import process_purchase
        sub = make_sub(gfe_message_count=35)
        with _patch_orchestrator(agent_says("yesss 🥰")):
            await process_purchase(sub, 27.38, make_avatar(), content_type="ppv")
        assert sub.gfe_message_count == 0

    @pytest.mark.anyio
    async def test_full_tier_ladder_ppv_count(self):
        """Simulate purchases for all 6 tiers — ppv_count reaches 6."""
        from agents.orchestrator import process_purchase
        sub = make_sub()
        prices = [27.38, 36.56, 77.35, 92.46, 127.45, 200.00]
        for price in prices:
            with _patch_orchestrator(agent_says("you're incredible 😍")):
                await process_purchase(sub, price, make_avatar(), content_type="ppv")
        assert sub.spending.ppv_count == 6
        assert abs(sub.spending.total_spent - sum(prices)) < 0.01

    @pytest.mark.anyio
    async def test_tier_price_from_default_table(self):
        """Tier price injected into PPV action matches default table."""
        from agents.orchestrator import process_message
        sub = make_sub(sext_consent_given=True, horniness_score=7)
        tier_prices = {1: 27.38, 2: 36.56, 3: 77.35, 4: 92.46, 5: 127.45, 6: 200.00}
        for tier, expected_price in tier_prices.items():
            with _patch_orchestrator(agent_says_ppv(tier=tier)):
                actions = await process_message(sub, "yes please", make_avatar())
            ppv_action = next(a for a in actions if a.action_type == "send_ppv")
            assert ppv_action.ppv_price == expected_price, f"tier {tier}: expected ${expected_price}"

    @pytest.mark.anyio
    async def test_last_pitch_at_set_on_ppv_intent(self):
        from agents.orchestrator import process_message
        sub = make_sub(sext_consent_given=True, horniness_score=7)
        assert sub.last_pitch_at is None
        with _patch_orchestrator(agent_says_ppv(tier=1)):
            await process_message(sub, "show me", make_avatar())
        assert sub.last_pitch_at is not None
        assert (datetime.now() - sub.last_pitch_at).seconds < 5


# ══════════════════════════════════════════════════════════════════════════════
# 3. Objection & Brokey Flow
# ══════════════════════════════════════════════════════════════════════════════

class TestObjectionFlow:

    @pytest.mark.anyio
    async def test_first_no_increments_tier_no_count(self):
        from agents.orchestrator import process_message
        sub = make_sub(pending_ppv={"tier": 1, "sent_at": datetime.now().isoformat()})
        sub.last_pitch_at = datetime.now()
        with _patch_orchestrator(agent_says("aww come on 🥺")):
            await process_message(sub, "I can't afford that", make_avatar())
        assert sub.tier_no_count == 1
        assert not sub.brokey_flagged

    @pytest.mark.anyio
    async def test_second_no_triggers_brokey(self):
        from agents.orchestrator import process_message
        sub = make_sub(
            pending_ppv={"tier": 1, "sent_at": datetime.now().isoformat()},
            tier_no_count=1,
        )
        sub.last_pitch_at = datetime.now()
        with _patch_orchestrator(agent_says("ok I get it 💔")):
            await process_message(sub, "still too expensive", make_avatar())
        assert sub.brokey_flagged is True
        assert sub.tier_no_count == 2

    @pytest.mark.anyio
    async def test_brokey_cooldown_injects_warmth_only_context(self):
        """After brokey_flagged, the context passed to the agent must include brokey_cooldown."""
        from agents.orchestrator import process_message
        # brokey context only fires when in_selling_mode=True (pending PPV or recent pitch)
        sub = make_sub(brokey_flagged=True, last_pitch_at=datetime.now())

        captured_context = {}

        async def capture(**kw):
            captured_context.update(kw.get("context", {}))
            return agent_says("hey how's your day going")

        with patch("agents.orchestrator.build_context",
                   new=AsyncMock(return_value=_BASE_CONTEXT.copy())), \
             patch("agents.orchestrator.single_agent_process",
                   new=AsyncMock(side_effect=capture)), \
             patch("agents.orchestrator.run_all_guardrails",
                   new=AsyncMock(return_value=(True, []))), \
             patch("agents.orchestrator.record_bot_message_sent", new=AsyncMock()), \
             patch("agents.orchestrator.update_callback_references", return_value=None), \
             patch("agents.orchestrator.memory_manager",
                   MagicMock(maybe_extract_and_store=AsyncMock(),
                             maybe_store_persona_facts=AsyncMock())):
            await process_message(sub, "hey", make_avatar())

        obj_ctx = captured_context.get("objection_context", {})
        assert obj_ctx.get("brokey_cooldown") is True

    @pytest.mark.anyio
    async def test_objection_not_detected_outside_selling_mode(self):
        """A fan saying 'can't afford' during normal chat must not trigger objection."""
        from agents.orchestrator import process_message
        sub = make_sub()  # no pending_ppv, no recent pitch
        with _patch_orchestrator(agent_says("aww what's going on?")):
            await process_message(sub, "I can't afford anything rn", make_avatar())
        assert sub.tier_no_count == 0
        assert not sub.brokey_flagged

    @pytest.mark.anyio
    async def test_purchase_resets_brokey_flag(self):
        from agents.orchestrator import process_purchase
        sub = make_sub(brokey_flagged=True, tier_no_count=2)
        with _patch_orchestrator(agent_says("you're back 😍")):
            await process_purchase(sub, 27.38, make_avatar(), content_type="ppv")
        assert sub.brokey_flagged is False
        assert sub.tier_no_count == 0

    @pytest.mark.anyio
    async def test_returning_buyer_objection_notes_purchase_history(self):
        """has_purchased_before flag is True when fan already has ppv_count > 0."""
        from agents.orchestrator import process_message
        sub = make_sub(
            pending_ppv={"tier": 2, "sent_at": datetime.now().isoformat()},
        )
        sub.last_pitch_at = datetime.now()
        sub.spending.ppv_count = 1
        sub.spending.total_spent = 27.38

        captured_context = {}

        async def capture(**kw):
            captured_context.update(kw.get("context", {}))
            return agent_says("but babe you loved the last one")

        with patch("agents.orchestrator.build_context",
                   new=AsyncMock(return_value=_BASE_CONTEXT.copy())), \
             patch("agents.orchestrator.single_agent_process",
                   new=AsyncMock(side_effect=capture)), \
             patch("agents.orchestrator.run_all_guardrails",
                   new=AsyncMock(return_value=(True, []))), \
             patch("agents.orchestrator.record_bot_message_sent", new=AsyncMock()), \
             patch("agents.orchestrator.update_callback_references", return_value=None), \
             patch("agents.orchestrator.memory_manager",
                   MagicMock(maybe_extract_and_store=AsyncMock(),
                             maybe_store_persona_facts=AsyncMock())):
            await process_message(sub, "too expensive", make_avatar())

        obj_ctx = captured_context.get("objection_context", {})
        assert obj_ctx.get("has_purchased_before") is True


# ══════════════════════════════════════════════════════════════════════════════
# 4. GFE Continuation Paywall Flow
# ══════════════════════════════════════════════════════════════════════════════

class TestGFEContinuationFlow:

    @pytest.mark.anyio
    async def test_continuation_flag_set_in_context_when_threshold_reached(self):
        """When gfe_message_count >= threshold, agent context gets gfe_continuation_ready=True."""
        from agents.orchestrator import process_message
        sub = make_sub(gfe_message_count=42, continuation_threshold_jitter=40)

        captured_context = {}

        async def capture(**kw):
            captured_context.update(kw.get("context", {}))
            return agent_says("lol you're so cute")

        with patch("agents.orchestrator.build_context",
                   new=AsyncMock(return_value=_BASE_CONTEXT.copy())), \
             patch("agents.orchestrator.single_agent_process",
                   new=AsyncMock(side_effect=capture)), \
             patch("agents.orchestrator.run_all_guardrails",
                   new=AsyncMock(return_value=(True, []))), \
             patch("agents.orchestrator.record_bot_message_sent", new=AsyncMock()), \
             patch("agents.orchestrator.update_callback_references", return_value=None), \
             patch("agents.orchestrator.memory_manager",
                   MagicMock(maybe_extract_and_store=AsyncMock(),
                             maybe_store_persona_facts=AsyncMock())):
            await process_message(sub, "haha yeah", make_avatar())

        assert captured_context.get("gfe_continuation_ready") is True

    @pytest.mark.anyio
    async def test_continuation_flag_not_set_below_threshold(self):
        from agents.orchestrator import process_message
        sub = make_sub(gfe_message_count=20, continuation_threshold_jitter=40)

        captured_context = {}

        async def capture(**kw):
            captured_context.update(kw.get("context", {}))
            return agent_says("you're sweet")

        with patch("agents.orchestrator.build_context",
                   new=AsyncMock(return_value=_BASE_CONTEXT.copy())), \
             patch("agents.orchestrator.single_agent_process",
                   new=AsyncMock(side_effect=capture)), \
             patch("agents.orchestrator.run_all_guardrails",
                   new=AsyncMock(return_value=(True, []))), \
             patch("agents.orchestrator.record_bot_message_sent", new=AsyncMock()), \
             patch("agents.orchestrator.update_callback_references", return_value=None), \
             patch("agents.orchestrator.memory_manager",
                   MagicMock(maybe_extract_and_store=AsyncMock(),
                             maybe_store_persona_facts=AsyncMock())):
            await process_message(sub, "hey", make_avatar())

        assert not captured_context.get("gfe_continuation_ready")

    @pytest.mark.anyio
    async def test_continuation_flag_not_set_when_already_pending(self):
        """If fan hasn't paid the previous continuation, don't offer another."""
        from agents.orchestrator import process_message
        sub = make_sub(
            gfe_message_count=50,
            continuation_threshold_jitter=40,
            gfe_continuation_pending=True,
        )
        captured_context = {}

        async def capture(**kw):
            captured_context.update(kw.get("context", {}))
            return agent_says("hey")

        with patch("agents.orchestrator.build_context",
                   new=AsyncMock(return_value=_BASE_CONTEXT.copy())), \
             patch("agents.orchestrator.single_agent_process",
                   new=AsyncMock(side_effect=capture)), \
             patch("agents.orchestrator.run_all_guardrails",
                   new=AsyncMock(return_value=(True, []))), \
             patch("agents.orchestrator.record_bot_message_sent", new=AsyncMock()), \
             patch("agents.orchestrator.update_callback_references", return_value=None), \
             patch("agents.orchestrator.memory_manager",
                   MagicMock(maybe_extract_and_store=AsyncMock(),
                             maybe_store_persona_facts=AsyncMock())):
            await process_message(sub, "hey", make_avatar())

        assert not captured_context.get("gfe_continuation_ready")

    @pytest.mark.anyio
    async def test_continuation_suppressed_when_fan_has_purchased_tier(self):
        """Fans who bought a tier are in the selling funnel — no GFE paywall."""
        from agents.orchestrator import process_message
        sub = make_sub(gfe_message_count=50, continuation_threshold_jitter=40)
        sub.spending.ppv_count = 1

        captured_context = {}

        async def capture(**kw):
            captured_context.update(kw.get("context", {}))
            return agent_says("hey")

        with patch("agents.orchestrator.build_context",
                   new=AsyncMock(return_value=_BASE_CONTEXT.copy())), \
             patch("agents.orchestrator.single_agent_process",
                   new=AsyncMock(side_effect=capture)), \
             patch("agents.orchestrator.run_all_guardrails",
                   new=AsyncMock(return_value=(True, []))), \
             patch("agents.orchestrator.record_bot_message_sent", new=AsyncMock()), \
             patch("agents.orchestrator.update_callback_references", return_value=None), \
             patch("agents.orchestrator.memory_manager",
                   MagicMock(maybe_extract_and_store=AsyncMock(),
                             maybe_store_persona_facts=AsyncMock())):
            await process_message(sub, "hey", make_avatar())

        assert not captured_context.get("gfe_continuation_ready")

    @pytest.mark.anyio
    async def test_agent_triggered_continuation_sets_pending_and_ppv_action(self):
        """When agent returns tier=continuation, pending flag set + send_ppv action created."""
        from agents.orchestrator import process_message
        sub = make_sub(gfe_message_count=50, continuation_threshold_jitter=40)
        continuation_result = agent_says(
            "I feel like we vibe so well 🥰",
            ppv={"tier": "continuation"},
        )
        with _patch_orchestrator(continuation_result):
            actions = await process_message(sub, "you're amazing", make_avatar())

        assert sub.gfe_continuation_pending is True
        ppv_actions = [a for a in actions if a.action_type == "send_ppv"]
        assert len(ppv_actions) == 1
        assert ppv_actions[0].ppv_price == 20.00

    @pytest.mark.anyio
    async def test_continuation_payment_resets_counter(self):
        from agents.orchestrator import process_purchase
        sub = make_sub(gfe_continuation_pending=True, gfe_message_count=50)
        with _patch_orchestrator(agent_says("you're back 🥰")):
            await process_purchase(sub, 20.00, make_avatar(), content_type="gfe_continuation")
        assert sub.gfe_continuation_pending is False
        assert sub.gfe_message_count == 0
        # gfe_continuations_paid is incremented by the connector's purchase handler, not process_purchase


# ══════════════════════════════════════════════════════════════════════════════
# 5. Resub Flow
# ══════════════════════════════════════════════════════════════════════════════

class TestResubFlow:

    @pytest.mark.anyio
    async def test_resub_returns_send_message_action(self):
        from agents.orchestrator import process_resub
        sub = make_sub()
        sub.spending.ppv_count = 2
        sub.spending.total_spent = 63.94
        with _patch_orchestrator(agent_says("you came back 🥺 I missed you")):
            actions = await process_resub(sub, make_avatar())
        assert len(actions) > 0
        assert actions[0].action_type == "send_message"

    @pytest.mark.anyio
    async def test_resub_advances_state_to_welcome_sent(self):
        from agents.orchestrator import process_resub
        sub = make_sub(state=SubState.COOLED_OFF)
        with _patch_orchestrator(agent_says("omg you're back 😍")):
            await process_resub(sub, make_avatar())
        assert sub.state.value == "welcome_sent"

    @pytest.mark.anyio
    async def test_resub_event_hint_includes_spend_history(self):
        """The event_hint injected into context must reference prior spend."""
        from agents.orchestrator import process_resub
        sub = make_sub()
        sub.spending.ppv_count = 3
        sub.spending.total_spent = 141.29

        captured_context = {}

        async def capture(**kw):
            captured_context.update(kw.get("context", {}))
            return agent_says("hey stranger 😏")

        with patch("agents.orchestrator.build_context",
                   new=AsyncMock(return_value=_BASE_CONTEXT.copy())), \
             patch("agents.orchestrator.single_agent_process",
                   new=AsyncMock(side_effect=capture)):
            await process_resub(sub, make_avatar())

        hint = captured_context.get("event_hint", "")
        assert "141.29" in hint or "3" in hint
        assert "RESUB" in hint

    @pytest.mark.anyio
    async def test_resub_event_hint_includes_fan_name_when_known(self):
        from agents.orchestrator import process_resub
        sub = make_sub(fan_name="Jake")
        captured_context = {}

        async def capture(**kw):
            captured_context.update(kw.get("context", {}))
            return agent_says("Jake 😭 you came back")

        with patch("agents.orchestrator.build_context",
                   new=AsyncMock(return_value=_BASE_CONTEXT.copy())), \
             patch("agents.orchestrator.single_agent_process",
                   new=AsyncMock(side_effect=capture)):
            await process_resub(sub, make_avatar())

        hint = captured_context.get("event_hint", "")
        assert "Jake" in hint

    @pytest.mark.anyio
    async def test_resub_fallback_on_error(self):
        from agents.orchestrator import process_resub
        sub = make_sub()
        with patch("agents.orchestrator.build_context", side_effect=RuntimeError("timeout")):
            actions = await process_resub(sub, make_avatar())
        assert len(actions) == 1
        assert actions[0].action_type == "send_message"


# ══════════════════════════════════════════════════════════════════════════════
# 6. New Follower Conversion Flow
# ══════════════════════════════════════════════════════════════════════════════

class TestNewFollowerFlow:

    @pytest.mark.anyio
    async def test_new_follower_returns_send_message_action(self):
        from agents.orchestrator import process_new_follower
        sub = make_sub(is_follower_only=True)
        with _patch_orchestrator(agent_says("noticed you 👀 you should come closer...")):
            actions = await process_new_follower(sub, make_avatar())
        assert len(actions) > 0
        assert actions[0].action_type == "send_message"

    @pytest.mark.anyio
    async def test_new_follower_event_hint_signals_not_subscribed(self):
        from agents.orchestrator import process_new_follower
        sub = make_sub(is_follower_only=True)
        captured_context = {}

        async def capture(**kw):
            captured_context.update(kw.get("context", {}))
            return agent_says("hey 😏")

        with patch("agents.orchestrator.build_context",
                   new=AsyncMock(return_value=_BASE_CONTEXT.copy())), \
             patch("agents.orchestrator.single_agent_process",
                   new=AsyncMock(side_effect=capture)):
            await process_new_follower(sub, make_avatar())

        hint = captured_context.get("event_hint", "")
        assert "NEW FOLLOWER" in hint
        assert "NOT subscribed" in hint or "hasn't paid" in hint or "NOT yet" in hint.upper()

    @pytest.mark.anyio
    async def test_new_follower_fallback_on_error(self):
        from agents.orchestrator import process_new_follower
        sub = make_sub(is_follower_only=True)
        with patch("agents.orchestrator.build_context", side_effect=RuntimeError("timeout")):
            actions = await process_new_follower(sub, make_avatar())
        assert len(actions) == 1
        assert actions[0].action_type == "send_message"


# ══════════════════════════════════════════════════════════════════════════════
# 7. Horniness Score Flow
# ══════════════════════════════════════════════════════════════════════════════

class TestHorninessScoreFlow:

    @pytest.mark.anyio
    async def test_explicit_keyword_boosts_score_to_9(self):
        from agents.orchestrator import process_message
        sub = make_sub()
        with _patch_orchestrator(agent_says(horniness=3)):
            await process_message(sub, "I'm so horny rn", make_avatar())
        assert sub.horniness_score == 9

    @pytest.mark.anyio
    async def test_sexual_interest_keyword_boosts_to_6(self):
        from agents.orchestrator import process_message
        sub = make_sub()
        # "sexy" contains "sex" which is in _EXPLICIT_9 — use "hot" (in _EXPLICIT_6 only)
        with _patch_orchestrator(agent_says(horniness=2)):
            await process_message(sub, "you're so hot", make_avatar())
        assert sub.horniness_score == 6

    @pytest.mark.anyio
    async def test_cooldown_keyword_reduces_score(self):
        from agents.orchestrator import process_message
        sub = make_sub(horniness_score=7)
        with _patch_orchestrator(agent_says(horniness=7)):
            await process_message(sub, "gotta go bye", make_avatar())
        assert sub.horniness_score < 7

    @pytest.mark.anyio
    async def test_opus_score_wins_when_higher_than_keyword(self):
        from agents.orchestrator import process_message
        sub = make_sub()
        # Opus returns 8, no explicit keywords → keyword_boost=0, final=8
        with _patch_orchestrator(agent_says("...", horniness=8)):
            await process_message(sub, "you're amazing", make_avatar())
        assert sub.horniness_score == 8

    @pytest.mark.anyio
    async def test_score_stored_on_subscriber(self):
        from agents.orchestrator import process_message
        sub = make_sub()
        with _patch_orchestrator(agent_says(horniness=5)):
            await process_message(sub, "that's hot", make_avatar())
        assert sub.horniness_score >= 5


# ══════════════════════════════════════════════════════════════════════════════
# 8. Fan Name Rate Limit Flow
# ══════════════════════════════════════════════════════════════════════════════

class TestFanNameRateLimit:

    @pytest.mark.anyio
    async def test_fan_name_learned_from_agent_result(self):
        from agents.orchestrator import process_message
        sub = make_sub()

        result = agent_says("nice to meet you Jake 😏")
        result["fan_name"] = "Jake"

        with _patch_orchestrator(result):
            await process_message(sub, "I'm Jake btw", make_avatar())

        assert sub.fan_name == "Jake"

    @pytest.mark.anyio
    async def test_fan_name_last_used_tracked_when_name_in_message(self):
        from agents.orchestrator import process_message
        sub = make_sub(fan_name="Jake", message_count=5)

        with _patch_orchestrator(agent_says("Jake you're so sweet 😍")):
            await process_message(sub, "hey", make_avatar())

        assert sub.fan_name_last_used_at_msg == sub.message_count

    @pytest.mark.anyio
    async def test_fan_name_not_tracked_when_absent_from_message(self):
        from agents.orchestrator import process_message
        sub = make_sub(fan_name="Jake", fan_name_last_used_at_msg=0)

        with _patch_orchestrator(agent_says("you're so sweet 😍")):
            await process_message(sub, "hey", make_avatar())

        assert sub.fan_name_last_used_at_msg == 0


# ══════════════════════════════════════════════════════════════════════════════
# 9. Multi-turn Sequence: Rapport → Consent → PPV → Purchase → Next Tier
# ══════════════════════════════════════════════════════════════════════════════

class TestFullConversationSequence:

    @pytest.mark.anyio
    async def test_rapport_to_first_purchase_sequence(self):
        """
        5-turn scripted sequence:
        fan: hey → bot: rapport
        fan: ur hot → bot: tease (horniness rises)
        fan: show me → bot: ppv drop (tier 1)
        [simulate purchase]
        fan: omg → bot: reaction + next tier lead
        """
        from agents.orchestrator import process_message, process_purchase

        sub = make_sub(state=SubState.WELCOME_SENT)
        avatar = make_avatar()

        # Turn 1 — rapport
        with _patch_orchestrator(agent_says("aww tell me more 😊", horniness=2)):
            actions1 = await process_message(sub, "hey", avatar)
        assert sub.message_count == 2  # add_message + explicit += 1 per turn
        assert not any(a.action_type == "send_ppv" for a in actions1)

        # Turn 2 — escalation, horniness rises
        with _patch_orchestrator(agent_says("you're making me blush 😏", horniness=5)):
            await process_message(sub, "ur so hot", avatar)
        assert sub.horniness_score >= 5

        # Turn 3 — agent drops tier 1 PPV
        with _patch_orchestrator(agent_says_ppv(tier=1)):
            actions3 = await process_message(sub, "show me more", avatar)
        assert any(a.action_type == "send_ppv" for a in actions3)
        assert sub.last_pitch_at is not None

        # Simulate fan purchasing (connector would call this after Fanvue webhook)
        sub.pending_ppv = None  # cleared after purchase
        with _patch_orchestrator(agent_says("omg you actually opened it 😍 ok wait...", horniness=8)):
            purchase_actions = await process_purchase(sub, 27.38, avatar, content_type="ppv")
        assert sub.spending.ppv_count == 1
        assert any(a.action_type == "send_message" for a in purchase_actions)

    @pytest.mark.anyio
    async def test_message_count_increments_each_turn(self):
        from agents.orchestrator import process_message
        sub = make_sub()
        for i in range(5):
            with _patch_orchestrator(agent_says(f"message {i}")):
                await process_message(sub, f"fan msg {i}", make_avatar())
        assert sub.message_count == 10  # add_message + explicit += 1 = 2 per turn

    @pytest.mark.anyio
    async def test_gfe_message_count_increments_each_turn(self):
        from agents.orchestrator import process_message
        sub = make_sub(gfe_message_count=0)
        for _ in range(3):
            with _patch_orchestrator(agent_says("hey")):
                await process_message(sub, "hey", make_avatar())
        assert sub.gfe_message_count == 3

    @pytest.mark.anyio
    async def test_conversation_history_grows_with_turns(self):
        from agents.orchestrator import process_message
        sub = make_sub()
        replies = ["first reply", "second reply", "third reply"]
        for i, reply in enumerate(replies):
            with _patch_orchestrator(agent_says(reply)):
                await process_message(sub, f"fan msg {i}", make_avatar())
        bot_msgs = [m["content"] for m in sub.recent_messages if m["role"] == "bot"]
        for reply in replies:
            assert reply in bot_msgs

    @pytest.mark.anyio
    async def test_ppv_suppressed_in_recovery_mode(self):
        """During server recovery, the orchestrator must not drop a PPV."""
        from agents.orchestrator import process_message
        sub = make_sub()
        recovery_ctx = {"bot_gap_str": "~2 hours", "msg_count": 3}
        with _patch_orchestrator(agent_says_ppv(tier=1)):
            actions = await process_message(
                sub, "hey", make_avatar(), recovery_context=recovery_ctx
            )
        assert not any(a.action_type == "send_ppv" for a in actions)
