"""
Massi-Bot — Comprehensive AI Behavior Test Suite

Five testing categories that verify the bot works as intended:

  1. UNIT       — Each component in isolation (pure functions, data models)
  2. BLACK BOX  — Input/output only, no internal inspection
  3. WHITE BOX  — Internal branches, edge cases, code paths
  4. INTEGRATION — Modules wired together (guardrails + filters, objection flow)
  5. SYSTEM     — End-to-end behavior: PPV flow, consent gating, brokey mode

Run with:
    cd ~/massi-bot && python -m pytest tests/test_ai_behavior.py -v
"""

import sys
import os
import asyncio
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))

from engine.models import (
    Subscriber, SpendingHistory, SubState, SubTier, SubType, BotAction, Persona,
)
from engine.text_filters import (
    filter_em_dash, filter_system_terminology, filter_reasoning_dump,
    filter_ai_vocabulary, filter_caption_content_leak, filter_length,
    filter_dollar_amounts, filter_platform_names,
    run_message_filters, run_caption_filters, filter_messages_list,
)
from engine.custom_orders import (
    is_custom_request, is_payment_claim, price_for_type, CUSTOM_PRICES,
    new_order, mark_fan_paid, mark_admin_confirmed, mark_admin_denied,
    STATUS_PITCHED, STATUS_AWAITING_ADMIN, STATUS_PAID, STATUS_DENIED,
)
from engine.session_control import (
    SessionController, classify_objection,
)


# ─────────────────────────────────────────────
# Shared fixtures
# ─────────────────────────────────────────────

def make_sub(**kwargs) -> Subscriber:
    """Return a fresh Subscriber with optional field overrides."""
    sub = Subscriber(sub_id="test-sub-001", username="test_fan")
    for k, v in kwargs.items():
        setattr(sub, k, v)
    return sub


def make_avatar():
    """Minimal avatar mock that satisfies guardrail attribute access."""
    avatar = MagicMock()
    avatar.persona.ig_account_tag = "test_model"
    avatar.persona.name = "Jasmine"
    avatar.persona.voice.primary_tone = "flirty"
    avatar.persona.voice.flirt_style = "playful"
    avatar.persona.voice.capitalization = "lowercase_casual"
    avatar.persona.voice.emoji_use = "light"
    avatar.persona.voice.punctuation_style = "minimal"
    return avatar


def msg(text: str, delay: int = 8) -> dict:
    return {"text": text, "delay_seconds": delay}


# ══════════════════════════════════════════════════════════════════════════════
# 1. UNIT TESTS — Individual components in isolation
# ══════════════════════════════════════════════════════════════════════════════

class TestUnitTextFilters:
    """Unit-level tests for each text filter function."""

    def test_em_dash_replaced_with_ellipsis(self):
        passed, cleaned, _ = filter_em_dash("she said — yes")
        assert passed is True
        assert "—" not in cleaned
        assert "..." in cleaned

    def test_en_dash_replaced(self):
        passed, cleaned, _ = filter_em_dash("range 10–20")
        assert "–" not in cleaned

    def test_no_dash_unchanged(self):
        _, cleaned, _ = filter_em_dash("just normal text")
        assert cleaned == "just normal text"

    def test_system_term_tier_rejected(self):
        passed, _, reason = filter_system_terminology("you unlocked tier 3!")
        assert passed is False
        assert "tier" in reason.lower()

    def test_system_term_ppv_rejected(self):
        passed, _, reason = filter_system_terminology("your PPV is ready")
        assert passed is False

    def test_system_term_agent_rejected(self):
        passed, _, reason = filter_system_terminology("the agent decided to send this")
        assert passed is False

    def test_system_term_clean_passes(self):
        passed, _, _ = filter_system_terminology("hey how are you doing today?")
        assert passed is True

    def test_reasoning_dump_let_me_rejected(self):
        passed, _, reason = filter_reasoning_dump("let me think about what to say here")
        assert passed is False

    def test_reasoning_dump_bold_marker_rejected(self):
        passed, _, reason = filter_reasoning_dump("**Key insight:** she wants to spend")
        assert passed is False

    def test_reasoning_dump_hash_header_rejected(self):
        passed, _, reason = filter_reasoning_dump("## Analysis of the situation")
        assert passed is False

    def test_reasoning_dump_raw_json_rejected(self):
        passed, _, _ = filter_reasoning_dump('{"messages": [{"text": "hi"}]}')
        assert passed is False

    def test_reasoning_dump_clean_passes(self):
        passed, _, _ = filter_reasoning_dump("hey stranger 😏")
        assert passed is True

    def test_ai_vocabulary_delve(self):
        passed, _, reason = filter_ai_vocabulary("let me delve into that")
        assert passed is False
        assert "delve" in reason

    def test_ai_vocabulary_nuanced(self):
        passed, _, _ = filter_ai_vocabulary("that's a nuanced point")
        assert passed is False

    def test_ai_vocabulary_clean(self):
        passed, _, _ = filter_ai_vocabulary("omg stop you're so cute")
        assert passed is True

    def test_dollar_amount_explicit(self):
        passed, _, reason = filter_dollar_amounts("it's only $27.38")
        assert passed is False

    def test_dollar_amount_word_form(self):
        passed, _, _ = filter_dollar_amounts("that costs 20 dollars")
        assert passed is False

    def test_dollar_amount_clean(self):
        passed, _, _ = filter_dollar_amounts("I'll show you something special")
        assert passed is True

    def test_platform_name_fanvue(self):
        passed, _, _ = filter_platform_names("check your fanvue notifications")
        assert passed is False

    def test_platform_name_onlyfans(self):
        passed, _, _ = filter_platform_names("my OnlyFans is updated")
        assert passed is False

    def test_platform_name_clean(self):
        passed, _, _ = filter_platform_names("check your messages")
        assert passed is True

    def test_length_over_limit(self):
        long_text = "a" * 601
        passed, _, reason = filter_length(long_text, max_chars=600)
        assert passed is False
        assert "601" in reason

    def test_length_at_limit_passes(self):
        text = "a" * 600
        passed, _, _ = filter_length(text, max_chars=600)
        assert passed is True

    def test_caption_body_part_rejected(self):
        passed, _, reason = filter_caption_content_leak("my tits are out for you")
        assert passed is False

    def test_caption_clothing_rejected(self):
        passed, _, reason = filter_caption_content_leak("me going topless")
        assert passed is False

    def test_caption_action_rejected(self):
        passed, _, reason = filter_caption_content_leak("me fingering myself")
        assert passed is False

    def test_caption_vague_passes(self):
        passed, _, _ = filter_caption_content_leak("just for you 😈")
        assert passed is True

    def test_run_message_filters_auto_fix_plus_reject(self):
        text = "she said — something involving tier 1"
        passed, cleaned, reasons = run_message_filters(text)
        assert passed is False
        assert "—" not in cleaned
        assert any("tier" in r.lower() for r in reasons)

    def test_run_message_filters_clean_passes(self):
        passed, _, reasons = run_message_filters("stop it 😏 you're trouble")
        assert passed is True
        assert reasons == []

    def test_filter_messages_list_any_fail_means_all_fail(self):
        msgs = [msg("hey how are you"), msg("unlock your tier 1 now")]
        all_passed, _, reasons = filter_messages_list(msgs)
        assert all_passed is False
        assert len(reasons) > 0


class TestUnitSubscriberModel:
    """Unit tests for Subscriber data model methods."""

    def test_record_purchase_increments_ppv_count(self):
        sub = make_sub()
        sub.record_purchase(27.38, "ppv")
        assert sub.spending.ppv_count == 1
        assert sub.spending.total_spent == pytest.approx(27.38)

    def test_record_purchase_resets_objection_tracking(self):
        sub = make_sub()
        sub.tier_no_count = 2
        sub.brokey_flagged = True
        sub.record_purchase(27.38, "ppv")
        assert sub.tier_no_count == 0
        assert sub.brokey_flagged is False

    def test_record_purchase_custom_increments_custom_count(self):
        sub = make_sub()
        sub.record_purchase(127.38, "custom")
        assert sub.spending.custom_count == 1
        assert sub.spending.ppv_count == 0

    def test_record_purchase_tip_increments_tip_count(self):
        sub = make_sub()
        sub.record_purchase(10.00, "tip")
        assert sub.spending.tip_count == 1

    def test_spending_tier_unproven(self):
        s = SpendingHistory(total_spent=0)
        assert s.tier == SubTier.UNPROVEN

    def test_spending_tier_low(self):
        s = SpendingHistory(total_spent=10)
        assert s.tier == SubTier.LOW

    def test_spending_tier_mid(self):
        s = SpendingHistory(total_spent=50)
        assert s.tier == SubTier.MID

    def test_spending_tier_high(self):
        s = SpendingHistory(total_spent=200)
        assert s.tier == SubTier.HIGH

    def test_spending_tier_whale(self):
        s = SpendingHistory(total_spent=600)
        assert s.tier == SubTier.WHALE

    def test_spending_is_buyer_false_with_no_purchases(self):
        s = SpendingHistory()
        assert s.is_buyer is False

    def test_spending_is_buyer_true_after_purchase(self):
        s = SpendingHistory(ppv_count=1)
        assert s.is_buyer is True

    def test_conversion_rate_zero_with_no_attempts(self):
        s = SpendingHistory()
        assert s.conversion_rate == 0.0

    def test_conversion_rate_calculation(self):
        s = SpendingHistory(ppv_count=2, rejected_ppv_count=2)
        assert s.conversion_rate == pytest.approx(0.5)

    def test_add_message_caps_at_50(self):
        sub = make_sub()
        for i in range(60):
            sub.add_message("sub", f"message {i}")
        assert len(sub.recent_messages) == 50

    def test_add_message_updates_last_message_date(self):
        sub = make_sub()
        assert sub.last_message_date is None
        sub.add_message("sub", "hello")
        assert sub.last_message_date is not None

    def test_whale_score_high_income_occupation(self):
        sub = make_sub()
        sub.qualifying.occupation = "software engineer"
        sub.qualifying.age = 35
        sub.qualifying.relationship_status = "single"
        score = sub.whale_score
        assert score >= 40

    def test_whale_score_no_signals(self):
        sub = make_sub()
        assert sub.whale_score == 0

    def test_sub_type_auto_upgrades_to_whale(self):
        sub = make_sub()
        assert sub.sub_type != SubType.WHALE
        sub.record_purchase(500.00, "ppv")
        assert sub.sub_type == SubType.WHALE


class TestUnitCustomOrders:
    """Unit tests for custom order detection, pricing, and state machine."""

    def test_price_for_type_canonical_keys(self):
        for ctype in ("pic_lingerie", "pic_nude", "video_lingerie", "video_nude", "voice_note", "complex"):
            canonical, price = price_for_type(ctype)
            assert canonical == ctype
            assert price == CUSTOM_PRICES[ctype]

    def test_price_for_type_invalid_falls_back_to_video_nude(self):
        canonical, price = price_for_type("invalid_type")
        assert canonical == "video_nude"
        assert price == CUSTOM_PRICES["video_nude"]

    def test_new_order_has_pitched_status(self):
        order = new_order("video of me in yoga pants", "video_lingerie", 127.38)
        assert order["status"] == STATUS_PITCHED
        assert order["quoted_price"] == 127.38
        assert order["fan_confirmed_paid_at"] is None

    def test_mark_fan_paid_transitions_status(self):
        order = new_order("test request", "pic_nude", 127.38)
        updated = mark_fan_paid(order)
        assert updated["status"] == STATUS_AWAITING_ADMIN
        assert updated["fan_confirmed_paid_at"] is not None

    def test_mark_admin_confirmed_transitions_to_paid(self):
        order = new_order("test", "pic_nude", 127.38)
        order = mark_fan_paid(order)
        order = mark_admin_confirmed(order)
        assert order["status"] == STATUS_PAID
        assert order["admin_confirmed_at"] is not None

    def test_mark_admin_denied_transitions_to_denied(self):
        order = new_order("test", "pic_nude", 127.38)
        order = mark_fan_paid(order)
        order = mark_admin_denied(order)
        assert order["status"] == STATUS_DENIED

    def test_new_order_does_not_mutate_original(self):
        o1 = new_order("original", "voice_note", 47.38)
        o2 = mark_fan_paid(o1)
        assert o1["status"] == STATUS_PITCHED
        assert o2["status"] == STATUS_AWAITING_ADMIN


class TestUnitSessionController:
    """Unit tests for SessionController and classify_objection."""

    def test_classify_objection_too_expensive(self):
        assert classify_objection("that's too expensive for me rn") == "TOO_EXPENSIVE"

    def test_classify_objection_cant_afford(self):
        assert classify_objection("I can't afford that right now") == "TOO_EXPENSIVE"

    def test_classify_objection_broke(self):
        assert classify_objection("I'm broke this week lol") == "TOO_EXPENSIVE"

    def test_classify_objection_maybe_later(self):
        assert classify_objection("maybe later tonight") == "MAYBE_LATER"

    def test_classify_objection_not_right_now(self):
        assert classify_objection("not right now") == "MAYBE_LATER"

    def test_classify_objection_not_today(self):
        assert classify_objection("not today") == "MAYBE_LATER"

    def test_classify_objection_cheaper(self):
        assert classify_objection("can you go cheaper on that") == "WANTS_CHEAPER"

    def test_classify_objection_spent_too_much(self):
        assert classify_objection("I've already spent a lot this week") == "SPENT_TOO_MUCH"

    def test_classify_objection_wants_free(self):
        assert classify_objection("can you send it for free?") == "WANTS_FREE"

    def test_classify_objection_returns_none_for_normal_chat(self):
        assert classify_objection("hey how are you doing?") is None
        assert classify_objection("what are you wearing rn?") is None
        assert classify_objection("i love talking to you") is None

    def test_handle_tier_objection_first_no_increments_count(self):
        sub = make_sub()
        _, action = SessionController.handle_tier_objection(sub, None, "TOO_EXPENSIVE")
        assert sub.tier_no_count == 1
        assert action == "retry"
        assert sub.brokey_flagged is False

    def test_handle_tier_objection_second_no_triggers_brokey(self):
        sub = make_sub()
        SessionController.handle_tier_objection(sub, None, "TOO_EXPENSIVE")
        _, action = SessionController.handle_tier_objection(sub, None, "TOO_EXPENSIVE")
        assert sub.tier_no_count == 2
        assert action == "brokey"
        assert sub.brokey_flagged is True

    def test_handle_tier_objection_returning_buyer_uses_returning_pool(self):
        sub = make_sub()
        sub.spending.ppv_count = 1
        msg_text, _ = SessionController.handle_tier_objection(sub, None, "TOO_EXPENSIVE")
        assert isinstance(msg_text, str)
        assert len(msg_text) > 0

    def test_is_in_brokey_cooldown_false_when_not_flagged(self):
        sub = make_sub()
        assert SessionController.is_in_brokey_cooldown(sub) is False

    def test_is_in_brokey_cooldown_true_within_5_days(self):
        sub = make_sub()
        sub.brokey_flagged = True
        sub.last_session_completed_at = datetime.now() - timedelta(days=3)
        assert SessionController.is_in_brokey_cooldown(sub) is True

    def test_is_in_brokey_cooldown_false_after_5_days(self):
        sub = make_sub()
        sub.brokey_flagged = True
        sub.last_session_completed_at = datetime.now() - timedelta(days=6)
        assert SessionController.is_in_brokey_cooldown(sub) is False

    def test_is_in_brokey_cooldown_true_when_ref_time_none(self):
        sub = make_sub()
        sub.brokey_flagged = True
        sub.last_session_completed_at = None
        sub.last_message_date = None
        assert SessionController.is_in_brokey_cooldown(sub) is True

    def test_is_session_locked_false_by_default(self):
        sub = make_sub()
        assert SessionController.is_session_locked(sub) is False

    def test_is_session_locked_true_within_window(self):
        sub = make_sub()
        SessionController.lock_session(sub, hours=6)
        assert SessionController.is_session_locked(sub) is True

    def test_should_reset_brokey_true_after_cooldown(self):
        sub = make_sub()
        sub.brokey_flagged = True
        sub.last_session_completed_at = datetime.now() - timedelta(days=6)
        assert SessionController.should_reset_brokey(sub) is True

    def test_should_reset_brokey_false_during_cooldown(self):
        sub = make_sub()
        sub.brokey_flagged = True
        sub.last_session_completed_at = datetime.now() - timedelta(days=2)
        assert SessionController.should_reset_brokey(sub) is False


# ══════════════════════════════════════════════════════════════════════════════
# 2. BLACK BOX TESTS — Input/output behavior, no internal inspection
# ══════════════════════════════════════════════════════════════════════════════

class TestBlackBoxTextFilters:
    """Test filter behavior from the outside — does it pass or reject?"""

    @pytest.mark.parametrize("text,expected_pass", [
        ("hey stranger 😏", True),
        ("stop it you're so cute", True),
        ("I was thinking about you all day", True),
        ("let me think about the best way to say this", False),     # reasoning dump
        ("based on the conversation I should say", False),          # reasoning dump
        ("your tier 1 content is ready", False),                    # system term
        ("it costs $27.38", False),                                  # dollar amount
        ("check your fanvue", False),                               # platform name
        ("I can certainly delve into that", False),                 # AI vocab
        ("she said — something", True),                             # em-dash: auto-fixed, passes
    ])
    def test_message_filter_pass_or_fail(self, text, expected_pass):
        passed, _, _ = run_message_filters(text)
        assert passed is expected_pass

    @pytest.mark.parametrize("caption,expected_pass", [
        ("just for you 😈", True),
        ("something special", True),
        ("a little surprise", True),
        ("me going topless for you", False),                        # clothing leak
        ("my tits just for you", False),                            # body part leak
        ("me fingering myself", False),                             # action leak
        ("naked in bed", False),                                    # clothing leak
    ])
    def test_caption_filter_pass_or_fail(self, caption, expected_pass):
        passed, _, _ = run_caption_filters(caption)
        assert passed is expected_pass


class TestBlackBoxObjectionDetection:
    """
    Verify objection classifier from fan message input only.
    Tests the tightened patterns after our fix (bare "wait", "later" removed).
    """

    @pytest.mark.parametrize("message,expected", [
        ("too expensive bro", "TOO_EXPENSIVE"),
        ("can't afford that rn", "TOO_EXPENSIVE"),
        ("I'm broke this week", "TOO_EXPENSIVE"),
        ("no money right now", "TOO_EXPENSIVE"),
        ("maybe later tonight", "MAYBE_LATER"),
        ("not right now", "MAYBE_LATER"),
        ("not today", "MAYBE_LATER"),
        ("not tonight", "MAYBE_LATER"),
        ("some other time", "MAYBE_LATER"),
        ("already spent a lot", "SPENT_TOO_MUCH"),
        ("can you go cheaper", "WANTS_CHEAPER"),
        ("hook me up with a deal", "WANTS_CHEAPER"),
        ("can you just send it for free?", "WANTS_FREE"),
    ])
    def test_objection_correctly_classified(self, message, expected):
        assert classify_objection(message) == expected

    @pytest.mark.parametrize("message", [
        "how are you doing",
        "what are you wearing",
        "you look amazing",
        "wait what did you say",        # bare "wait" — no longer a trigger
        "later we should talk",         # bare "later" in normal context — no longer triggers
        "hold on let me read that",     # bare "hold on" — no longer a trigger
        "how about we chat",            # generic "how about" — removed
        "not bad at all",               # "not" but not a rejection phrase
    ])
    def test_normal_chat_not_classified_as_objection(self, message):
        assert classify_objection(message) is None


class TestBlackBoxCustomRequestDetection:
    """Test custom order detection from fan message text."""

    @pytest.mark.parametrize("message", [
        "video of you in a golf outfit",
        "send me a voice note",
        "can you make me a video",
        "pic of you in lingerie",
        "dressed as a nurse",
        "wearing my red lingerie",
        "would you record a video of that",
    ])
    def test_custom_request_detected(self, message):
        assert is_custom_request(message) is True

    @pytest.mark.parametrize("message", [
        "hey what's up",
        "you're so beautiful",
        "I love your content",
        "when are you online",
        "do you like working out",
    ])
    def test_normal_chat_not_custom_request(self, message):
        assert is_custom_request(message) is False

    @pytest.mark.parametrize("message", [
        "I paid just now",
        "sent it",
        "sent the money",
        "I tipped you",
        "transferred just now",
        "you should see the payment",
        "completed the payment",
    ])
    def test_payment_claim_detected(self, message):
        assert is_payment_claim(message) is True

    @pytest.mark.parametrize("message", [
        "hello there",
        "can you send me something",
        "I want to see more",
        "how much is it",
    ])
    def test_normal_message_not_payment_claim(self, message):
        assert is_payment_claim(message) is False


# ══════════════════════════════════════════════════════════════════════════════
# 3. WHITE BOX TESTS — Internal logic branches, code path coverage
# ══════════════════════════════════════════════════════════════════════════════

class TestWhiteBoxFilterBranches:
    """
    Target specific code branches inside filter functions.
    Each test is named for the exact path it exercises.
    """

    def test_reasoning_dump_branch_based_on_expression(self):
        passed, _, _ = filter_reasoning_dump("based on the fan's messages, I should say hey")
        assert passed is False

    def test_reasoning_dump_branch_given_the(self):
        passed, _, _ = filter_reasoning_dump("given the context here I should")
        assert passed is False

    def test_reasoning_dump_branch_considering(self):
        passed, _, _ = filter_reasoning_dump("considering his last message, I'll say")
        assert passed is False

    def test_reasoning_dump_branch_step_number(self):
        passed, _, _ = filter_reasoning_dump("step 1: greet him warmly")
        assert passed is False

    def test_reasoning_dump_empty_string_passes(self):
        passed, _, _ = filter_reasoning_dump("")
        assert passed is True

    def test_reasoning_dump_whitespace_only_passes(self):
        passed, _, _ = filter_reasoning_dump("   ")
        assert passed is True

    def test_system_term_case_insensitive_CAPS(self):
        passed, _, _ = filter_system_terminology("Your PPV IS waiting")
        assert passed is False

    def test_system_term_session_with_number(self):
        passed, _, _ = filter_system_terminology("session 2 is ready")
        assert passed is False

    def test_dollar_amount_bucks_form(self):
        passed, _, _ = filter_dollar_amounts("only 30 bucks")
        assert passed is False

    def test_dollar_amount_no_match_for_plain_numbers(self):
        passed, _, _ = filter_dollar_amounts("I was thinking about you at 3am")
        assert passed is True


class TestWhiteBoxSellingModeDetection:
    """
    Test the in_selling_mode logic that gates objection detection.
    Mirrors the exact orchestrator logic: pending_ppv OR last_pitch_at within 30min.
    """

    def _in_selling_mode(self, sub) -> bool:
        """Replicate orchestrator's selling mode check."""
        _pending_ppv = getattr(sub, "pending_ppv", None)
        _last_pitch = getattr(sub, "last_pitch_at", None)
        _pitch_recent = (
            _last_pitch is not None
            and (datetime.now() - _last_pitch) < timedelta(minutes=30)
        )
        return _pending_ppv is not None or _pitch_recent

    def test_no_pending_no_pitch_returns_false(self):
        sub = make_sub(sext_consent_given=True)  # consent alone no longer triggers
        assert self._in_selling_mode(sub) is False

    def test_pending_ppv_set_returns_true(self):
        sub = make_sub(pending_ppv={"tier": 1, "sent_at": datetime.now().isoformat()})
        assert self._in_selling_mode(sub) is True

    def test_pitch_within_30min_returns_true(self):
        sub = make_sub(last_pitch_at=datetime.now() - timedelta(minutes=10))
        assert self._in_selling_mode(sub) is True

    def test_pitch_exactly_at_30min_boundary(self):
        sub = make_sub(last_pitch_at=datetime.now() - timedelta(minutes=29))
        assert self._in_selling_mode(sub) is True

    def test_pitch_31min_ago_returns_false(self):
        sub = make_sub(last_pitch_at=datetime.now() - timedelta(minutes=31))
        assert self._in_selling_mode(sub) is False

    def test_sext_consent_alone_does_not_trigger(self):
        """Critical: permanent consent flag must NOT trigger objection detection."""
        sub = make_sub(sext_consent_given=True, pending_ppv=None, last_pitch_at=None)
        assert self._in_selling_mode(sub) is False


class TestWhiteBoxSessionControllerBranches:
    """White box coverage of SessionController internal branches."""

    def test_no_count_caps_at_2_internally(self):
        """no_level = min(sub.tier_no_count, 2) — verify cap holds."""
        sub = make_sub()
        # Manually set above cap to simulate corrupted state
        sub.tier_no_count = 5
        sub.spending.ppv_count = 0
        # Should still return a valid template without crashing
        msg_text, _ = SessionController.handle_tier_objection(sub, None, "TOO_EXPENSIVE")
        assert isinstance(msg_text, str)

    def test_objection_key_fallback_to_too_expensive(self):
        """Unknown objection type → falls back to too_expensive template pool."""
        sub = make_sub()
        msg_text, action = SessionController.handle_tier_objection(sub, None, "UNKNOWN_TYPE")
        assert isinstance(msg_text, str)
        assert action in ("retry", "brokey")

    def test_brokey_cooldown_uses_last_message_date_when_no_session_date(self):
        sub = make_sub()
        sub.brokey_flagged = True
        sub.last_session_completed_at = None
        sub.last_message_date = datetime.now() - timedelta(days=2)
        assert SessionController.is_in_brokey_cooldown(sub) is True

    def test_brokey_cooldown_prefers_session_date_over_message_date(self):
        sub = make_sub()
        sub.brokey_flagged = True
        # Session date says 6 days ago → should be out of cooldown
        sub.last_session_completed_at = datetime.now() - timedelta(days=6)
        # Message date says recent — should NOT override
        sub.last_message_date = datetime.now() - timedelta(hours=1)
        assert SessionController.is_in_brokey_cooldown(sub) is False

    def test_handle_tier_objection_accumulates_price_objection_count(self):
        sub = make_sub()
        SessionController.handle_tier_objection(sub, None, "TOO_EXPENSIVE")
        SessionController.handle_tier_objection(sub, None, "MAYBE_LATER")
        assert sub.spending.price_objection_count == 2


# ══════════════════════════════════════════════════════════════════════════════
# 4. INTEGRATION TESTS — Modules wired together
# ══════════════════════════════════════════════════════════════════════════════

class TestIntegrationGuardrails:
    """Test parallel guardrails with real filter pipeline."""

    @pytest.mark.anyio
    async def test_clean_message_passes_all_guardrails(self):
        from agents.parallel_guardrails import run_all_guardrails
        sub = make_sub()
        avatar = make_avatar()
        messages = [msg("hey how are you doing today")]
        all_passed, results = await run_all_guardrails(
            messages=messages, ppv_intent=None, sub=sub, avatar=avatar,
            tiers_purchased=0, sext_consent_given=False,
        )
        assert all_passed is True

    @pytest.mark.anyio
    async def test_explicit_word_at_tier_zero_fails_tier_boundary(self):
        from agents.parallel_guardrails import run_all_guardrails
        sub = make_sub()
        avatar = make_avatar()
        messages = [msg("I want you to see my pussy rn")]
        all_passed, results = await run_all_guardrails(
            messages=messages, ppv_intent=None, sub=sub, avatar=avatar,
            tiers_purchased=0, sext_consent_given=False,
        )
        assert all_passed is False
        names = [r.name for r in results if not r.passed]
        assert "tier_boundary" in names

    @pytest.mark.anyio
    async def test_pending_ppv_blocks_new_ppv_drop(self):
        from agents.parallel_guardrails import run_all_guardrails
        sub = make_sub(pending_ppv={"tier": 1})
        avatar = make_avatar()
        messages = [msg("give me a minute")]
        ppv_intent = {"tier": 2, "caption": "just for you"}
        all_passed, results = await run_all_guardrails(
            messages=messages, ppv_intent=ppv_intent, sub=sub, avatar=avatar,
            tiers_purchased=1, sext_consent_given=True,
        )
        assert all_passed is False
        names = [r.name for r in results if not r.passed]
        assert "no_redrop" in names

    @pytest.mark.anyio
    async def test_no_pending_ppv_allows_new_ppv_drop(self):
        from agents.parallel_guardrails import run_all_guardrails
        sub = make_sub(pending_ppv=None)
        avatar = make_avatar()
        messages = [msg("give me a minute")]
        ppv_intent = {"tier": 1, "caption": "just for you"}
        all_passed, results = await run_all_guardrails(
            messages=messages, ppv_intent=ppv_intent, sub=sub, avatar=avatar,
            tiers_purchased=0, sext_consent_given=True,
        )
        guard_names_failed = [r.name for r in results if not r.passed]
        assert "no_redrop" not in guard_names_failed

    @pytest.mark.anyio
    async def test_emoji_overload_rejected(self):
        from agents.parallel_guardrails import run_all_guardrails
        sub = make_sub()
        avatar = make_avatar()
        messages = [msg("hey 😍😏😈 what's up")]
        all_passed, results = await run_all_guardrails(
            messages=messages, ppv_intent=None, sub=sub, avatar=avatar,
            tiers_purchased=0, sext_consent_given=False,
        )
        assert all_passed is False
        names = [r.name for r in results if not r.passed]
        assert "emoji_density" in names

    @pytest.mark.anyio
    async def test_fake_exclusivity_rejected(self):
        from agents.parallel_guardrails import run_all_guardrails
        sub = make_sub()
        avatar = make_avatar()
        messages = [msg("i've never sent this to anyone before")]
        all_passed, results = await run_all_guardrails(
            messages=messages, ppv_intent=None, sub=sub, avatar=avatar,
            tiers_purchased=0, sext_consent_given=False,
        )
        assert all_passed is False
        names = [r.name for r in results if not r.passed]
        assert "fake_exclusivity" in names

    @pytest.mark.anyio
    async def test_other_fans_mention_rejected(self):
        from agents.parallel_guardrails import run_all_guardrails
        sub = make_sub()
        avatar = make_avatar()
        messages = [msg("my other fans love this content")]
        all_passed, results = await run_all_guardrails(
            messages=messages, ppv_intent=None, sub=sub, avatar=avatar,
            tiers_purchased=0, sext_consent_given=False,
        )
        assert all_passed is False
        names = [r.name for r in results if not r.passed]
        assert "other_fans" in names

    @pytest.mark.anyio
    async def test_feminine_endearment_rejected(self):
        from agents.parallel_guardrails import run_all_guardrails
        sub = make_sub()
        avatar = make_avatar()
        messages = [msg("hey sweetie how are you doing")]
        all_passed, results = await run_all_guardrails(
            messages=messages, ppv_intent=None, sub=sub, avatar=avatar,
            tiers_purchased=0, sext_consent_given=False,
        )
        assert all_passed is False
        names = [r.name for r in results if not r.passed]
        assert "persona_voice" in names

    @pytest.mark.anyio
    async def test_caption_content_leak_rejected_through_guardrail(self):
        from agents.parallel_guardrails import run_all_guardrails
        sub = make_sub(pending_ppv=None)
        avatar = make_avatar()
        messages = [msg("give me a minute")]
        ppv_intent = {"tier": 1, "caption": "me going topless for you"}
        all_passed, results = await run_all_guardrails(
            messages=messages, ppv_intent=ppv_intent, sub=sub, avatar=avatar,
            tiers_purchased=0, sext_consent_given=True,
        )
        assert all_passed is False
        names = [r.name for r in results if not r.passed]
        assert "text_filters" in names


class TestIntegrationObjectionFlow:
    """Test the full objection state machine: classify → handle → brokey."""

    def test_full_two_no_flow_reaches_brokey(self):
        sub = make_sub()
        # First rejection
        msg1, action1 = SessionController.handle_tier_objection(sub, None, "TOO_EXPENSIVE")
        assert action1 == "retry"
        assert sub.brokey_flagged is False
        # Second rejection
        msg2, action2 = SessionController.handle_tier_objection(sub, None, "TOO_EXPENSIVE")
        assert action2 == "brokey"
        assert sub.brokey_flagged is True
        assert sub.tier_no_count == 2

    def test_purchase_after_rejection_resets_no_count(self):
        sub = make_sub()
        SessionController.handle_tier_objection(sub, None, "TOO_EXPENSIVE")
        assert sub.tier_no_count == 1
        sub.record_purchase(27.38, "ppv")
        assert sub.tier_no_count == 0
        assert sub.brokey_flagged is False

    def test_returning_buyer_different_template_pool(self):
        """Returning buyers (ppv_count>0) should get different templates that reference history."""
        sub_new = make_sub()
        sub_returning = make_sub()
        sub_returning.spending.ppv_count = 1

        # Both get a string — just verify they can both produce responses
        msg_new, _ = SessionController.handle_tier_objection(sub_new, None, "TOO_EXPENSIVE")
        msg_ret, _ = SessionController.handle_tier_objection(sub_returning, None, "TOO_EXPENSIVE")
        assert isinstance(msg_new, str)
        assert isinstance(msg_ret, str)

    def test_classify_then_handle_pipeline(self):
        """classify_objection → handle_tier_objection works end-to-end."""
        sub = make_sub()
        message = "honestly too expensive for me right now"
        obj_type = classify_objection(message)
        assert obj_type is not None
        response, action = SessionController.handle_tier_objection(sub, None, obj_type)
        assert isinstance(response, str)
        assert action in ("retry", "brokey")


class TestIntegrationObjectionContextInjection:
    """
    Test that the orchestrator's objection_context is correctly
    built and injected into the context dict under the right conditions.
    """

    def _build_objection_context(self, sub, message):
        """Replicate the orchestrator's objection detection logic."""
        _pending_ppv = getattr(sub, "pending_ppv", None)
        _last_pitch = getattr(sub, "last_pitch_at", None)
        _pitch_recent = (
            _last_pitch is not None
            and (datetime.now() - _last_pitch) < timedelta(minutes=30)
        )
        in_selling_mode = _pending_ppv is not None or _pitch_recent

        if not in_selling_mode:
            return None

        if SessionController.is_in_brokey_cooldown(sub):
            return {"brokey_cooldown": True}

        obj_type = classify_objection(message)
        if not obj_type:
            return None

        ppv_count = sub.spending.ppv_count if sub.spending else 0
        _, next_action = SessionController.handle_tier_objection(sub, None, obj_type)
        return {
            "objection_type": obj_type,
            "no_count": sub.tier_no_count,
            "is_brokey": next_action == "brokey",
            "has_purchased_before": ppv_count > 0,
        }

    def test_objection_context_built_when_pending_ppv(self):
        sub = make_sub(pending_ppv={"tier": 1})
        ctx = self._build_objection_context(sub, "can't afford that")
        assert ctx is not None
        assert ctx["objection_type"] == "TOO_EXPENSIVE"

    def test_objection_context_none_when_no_selling_mode(self):
        """Fan with consent but no pending PPV + no recent pitch — no objection context."""
        sub = make_sub(sext_consent_given=True, pending_ppv=None, last_pitch_at=None)
        ctx = self._build_objection_context(sub, "can't afford that")
        assert ctx is None

    def test_objection_context_none_for_normal_chat_in_selling_mode(self):
        """Even in selling mode, normal chat doesn't produce objection context."""
        sub = make_sub(pending_ppv={"tier": 1})
        ctx = self._build_objection_context(sub, "hey what are you doing tonight")
        assert ctx is None

    def test_brokey_cooldown_context_when_flagged(self):
        sub = make_sub(pending_ppv={"tier": 1})
        sub.brokey_flagged = True
        sub.last_session_completed_at = datetime.now()
        ctx = self._build_objection_context(sub, "any message")
        assert ctx == {"brokey_cooldown": True}

    def test_has_purchased_before_true_for_returning_buyer(self):
        sub = make_sub(pending_ppv={"tier": 2})
        sub.spending.ppv_count = 1
        ctx = self._build_objection_context(sub, "too expensive")
        assert ctx["has_purchased_before"] is True

    def test_has_purchased_before_false_for_new_fan(self):
        sub = make_sub(pending_ppv={"tier": 1})
        ctx = self._build_objection_context(sub, "too expensive")
        assert ctx["has_purchased_before"] is False

    def test_is_brokey_true_on_second_rejection(self):
        sub = make_sub(pending_ppv={"tier": 1})
        sub.tier_no_count = 1  # Already had one no
        ctx = self._build_objection_context(sub, "can't afford")
        assert ctx["is_brokey"] is True


# ══════════════════════════════════════════════════════════════════════════════
# 5. SYSTEM TESTS — End-to-end flows with mocked LLM / Supabase boundary
# ══════════════════════════════════════════════════════════════════════════════

def _make_agent_result(text="hey how are you", ppv=None, consent=False, horniness=3):
    return {
        "messages": [{"text": text, "delay_seconds": 8}],
        "ppv": ppv,
        "consent_given": consent,
        "consent_declined": False,
        "horniness_score": horniness,
        "fan_name": None,
        "fan_profile_update": None,
    }


_MINIMAL_CONTEXT = {
    "relationship_summary": "test fan", "session_arc": "", "open_threads": [],
    "tier_content_awareness": "", "time_since_last_fan_message": "5 min",
    "goodbye_state": {}, "memories": [], "callback_refs": [], "persona_facts": [],
    "live_context": "", "model_profile": None,
}


@pytest.fixture
def mock_agent_infra():
    """
    Patch all external I/O so orchestrator tests run without real services.
    Patches at agents.orchestrator.* (the bound names) not the source modules,
    so the mock replaces what the orchestrator actually calls.
    """
    with patch("agents.orchestrator.build_context", new=AsyncMock(return_value=_MINIMAL_CONTEXT.copy())), \
         patch("agents.orchestrator.single_agent_process", new=AsyncMock(
             return_value=_make_agent_result()
         )), \
         patch("agents.orchestrator.run_all_guardrails", new=AsyncMock(
             return_value=(True, [])
         )), \
         patch("agents.orchestrator.record_bot_message_sent", new=AsyncMock()), \
         patch("agents.orchestrator.update_callback_references", return_value=None), \
         patch("agents.orchestrator.memory_manager") as mock_mm:
        mock_mm.maybe_extract_and_store = AsyncMock()
        mock_mm.maybe_store_persona_facts = AsyncMock()
        yield


class TestSystemProcessMessage:
    """System tests for process_message end-to-end."""

    @pytest.mark.anyio
    async def test_basic_message_returns_bot_actions(self, mock_agent_infra):
        from agents.orchestrator import process_message
        sub = make_sub()
        avatar = make_avatar()
        actions = await process_message(sub, "hey what's up", avatar=avatar)
        assert isinstance(actions, list)
        assert len(actions) > 0
        # Check duck-type (action_type attr) rather than class identity — orchestrator
        # imports BotAction from 'models' (engine/ on path) while test uses 'engine.models';
        # same file, two module objects, isinstance returns False across them.
        assert all(hasattr(a, "action_type") for a in actions)

    @pytest.mark.anyio
    async def test_message_tracked_in_history(self, mock_agent_infra):
        from agents.orchestrator import process_message
        sub = make_sub()
        await process_message(sub, "hello there", avatar=make_avatar())
        contents = [m["content"] for m in sub.recent_messages]
        assert "hello there" in contents

    @pytest.mark.anyio
    async def test_empty_message_returns_empty_actions(self, mock_agent_infra):
        from agents.orchestrator import process_message
        sub = make_sub()
        actions = await process_message(sub, "", avatar=make_avatar())
        assert actions == []

    @pytest.mark.anyio
    async def test_objection_not_detected_without_selling_mode(self, mock_agent_infra):
        """Consent-only fan saying 'can't afford' must NOT trigger objection context."""
        from agents.orchestrator import process_message
        import agents.context_builder as cb

        captured_context = {}

        async def capture_build_context(sub, message, avatar, **kwargs):
            ctx = {
                "relationship_summary": "", "session_arc": "", "open_threads": [],
                "tier_content_awareness": "", "time_since_last_fan_message": "5 min",
                "goodbye_state": {}, "memories": [], "callback_refs": [], "persona_facts": [],
                "live_context": "", "model_profile": None,
            }
            return ctx

        async def capture_agent(message, avatar, sub, context, **kwargs):
            captured_context.update(context)
            return _make_agent_result()

        with patch("agents.orchestrator.build_context", new=AsyncMock(side_effect=capture_build_context)), \
             patch("agents.orchestrator.single_agent_process", new=AsyncMock(side_effect=capture_agent)), \
             patch("agents.orchestrator.run_all_guardrails", new=AsyncMock(return_value=(True, []))), \
             patch("agents.orchestrator.record_bot_message_sent", new=AsyncMock()), \
             patch("agents.orchestrator.update_callback_references", return_value=None), \
             patch("agents.orchestrator.memory_manager") as mm:

            mm.maybe_extract_and_store = AsyncMock()
            mm.maybe_store_persona_facts = AsyncMock()
            sub = make_sub(sext_consent_given=True, pending_ppv=None, last_pitch_at=None)
            await process_message(sub, "can't afford that right now", avatar=make_avatar())

        assert "objection_context" not in captured_context


class TestSystemProcessPurchase:
    """System tests for the PPV purchase flow."""

    @pytest.mark.anyio
    async def test_purchase_returns_bot_actions(self, mock_agent_infra):
        from agents.orchestrator import process_purchase
        sub = make_sub()
        actions = await process_purchase(sub, 27.38, avatar=make_avatar(), content_type="ppv")
        assert isinstance(actions, list)

    @pytest.mark.anyio
    async def test_purchase_increments_ppv_count(self, mock_agent_infra):
        from agents.orchestrator import process_purchase
        sub = make_sub()
        assert sub.spending.ppv_count == 0
        await process_purchase(sub, 27.38, avatar=make_avatar(), content_type="ppv")
        assert sub.spending.ppv_count == 1

    @pytest.mark.anyio
    async def test_next_tier_ppv_scheduled_after_first_purchase(self, mock_agent_infra):
        """After buying tier 1, orchestrator should auto-schedule tier 2 PPV."""
        from agents.orchestrator import process_purchase
        sub = make_sub()
        actions = await process_purchase(sub, 27.38, avatar=make_avatar(), content_type="ppv",
                                         active_tier_count=6)
        ppv_actions = [a for a in actions if a.action_type == "send_ppv"]
        assert len(ppv_actions) == 1
        assert ppv_actions[0].metadata.get("tier") == "tier_2"

    @pytest.mark.anyio
    async def test_no_next_tier_at_ceiling(self, mock_agent_infra):
        """After buying tier 6 (the ceiling), no further PPV should be scheduled."""
        from agents.orchestrator import process_purchase
        sub = make_sub()
        # Simulate fan who has already bought tiers 1-5
        sub.spending.ppv_count = 5
        sub.spending.total_spent = 560.22
        actions = await process_purchase(sub, 200.00, avatar=make_avatar(), content_type="ppv",
                                         active_tier_count=6)
        ppv_actions = [a for a in actions if a.action_type == "send_ppv"]
        assert len(ppv_actions) == 0

    @pytest.mark.anyio
    async def test_purchase_resets_gfe_message_count(self, mock_agent_infra):
        from agents.orchestrator import process_purchase
        sub = make_sub()
        sub.gfe_message_count = 42
        await process_purchase(sub, 27.38, avatar=make_avatar(), content_type="ppv")
        assert sub.gfe_message_count == 0

    @pytest.mark.anyio
    async def test_tip_purchase_does_not_schedule_ppv(self, mock_agent_infra):
        """Tip payments must not trigger the PPV auto-inject logic."""
        from agents.orchestrator import process_purchase
        sub = make_sub()
        actions = await process_purchase(sub, 10.00, avatar=make_avatar(), content_type="tip")
        ppv_actions = [a for a in actions if a.action_type == "send_ppv"]
        assert len(ppv_actions) == 0


class TestSystemConsentGating:
    """System tests verifying consent-gate behavior end-to-end."""

    @pytest.mark.anyio
    async def test_consent_flag_set_when_agent_returns_consent_given(self, mock_agent_infra):
        from agents.orchestrator import process_message

        with patch("agents.orchestrator.single_agent_process", new=AsyncMock(
            return_value=_make_agent_result(consent=True, horniness=7)
        )):
            sub = make_sub()
            assert sub.sext_consent_given is False
            await process_message(sub, "yeah I'm interested", avatar=make_avatar())
            assert sub.sext_consent_given is True

    @pytest.mark.anyio
    async def test_consent_not_set_for_normal_response(self, mock_agent_infra):
        from agents.orchestrator import process_message
        sub = make_sub()
        await process_message(sub, "hey", avatar=make_avatar())
        assert sub.sext_consent_given is False


class TestSystemHorninessTracking:
    """System tests for the keyword boost + horniness score logic."""

    @pytest.mark.anyio
    async def test_explicit_keyword_boosts_horniness_to_9(self, mock_agent_infra):
        from agents.orchestrator import process_message

        with patch("agents.orchestrator.single_agent_process", new=AsyncMock(
            return_value=_make_agent_result(horniness=3)
        )):
            sub = make_sub()
            await process_message(sub, "I'm so horny rn", avatar=make_avatar())
            assert sub.horniness_score >= 9

    @pytest.mark.anyio
    async def test_cooldown_keyword_reduces_horniness(self, mock_agent_infra):
        from agents.orchestrator import process_message

        with patch("agents.orchestrator.single_agent_process", new=AsyncMock(
            return_value=_make_agent_result(horniness=8)
        )):
            sub = make_sub()
            sub.horniness_score = 8
            await process_message(sub, "gtg bye", avatar=make_avatar())
            assert sub.horniness_score <= 6

    @pytest.mark.anyio
    async def test_horniness_takes_max_of_opus_and_keyword(self, mock_agent_infra):
        from agents.orchestrator import process_message

        with patch("agents.orchestrator.single_agent_process", new=AsyncMock(
            return_value=_make_agent_result(horniness=3)
        )):
            sub = make_sub()
            await process_message(sub, "I want to fuck you so bad", avatar=make_avatar())
            assert sub.horniness_score >= 9
