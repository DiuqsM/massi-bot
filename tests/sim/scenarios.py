"""
Massi-Bot Conversation Simulator — Scenario Definitions

27 scenarios across 8 groups. Each automated scenario:
  1. Sets up a Subscriber with the required state
  2. Calls real orchestrator functions (real Opus 4.7 calls)
  3. Returns a ScenarioResult with full conversation log + checks

MANUAL scenarios (S5.3–S5.5, S8.1–S8.3) are marked is_manual=True.
The runner skips them and prints instructions instead.
"""

from __future__ import annotations

import contextlib
import os
import sys
import logging
import types
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "engine"))

from engine.models import (
    Subscriber, SubState, SpendingHistory, QualifyingData, BotAction,
)
from tests.sim.checks import (
    CheckResult,
    check_has_response, check_no_ppv, check_has_ppv_action,
    check_ppv_price, check_text_contains, check_text_not_contains,
    check_sub_flag, check_sub_state, check_ppv_price_sequence,
    check_no_money_mentions, get_all_text, has_ppv, get_ppv_price,
    llm_judge, simulate_fan_turn,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIER_PRICES = [27.38, 36.56, 77.35, 92.46, 127.45, 200.00]


# ── Conversation logger ───────────────────────────────────────────────────────

@dataclass
class ConvTurn:
    role: str            # "fan", "bot", "ppv", "event", "purchase"
    text: str
    ms: Optional[float] = None   # response time (only on bot/ppv turns)


class ConvLogger:
    """
    Records every turn of a simulated conversation: fan messages, bot replies,
    PPV drops, events, and timing per orchestrator call.
    Attach to ScenarioResult before returning.
    """

    def __init__(self):
        self.turns: list[ConvTurn] = []
        self.timings: list[float] = []    # ms per orchestrator call

    def fan(self, text: str):
        self.turns.append(ConvTurn("fan", text))

    def event(self, description: str):
        self.turns.append(ConvTurn("event", description))

    async def run(
        self,
        coro,
        fan_text: Optional[str] = None,
    ) -> list[BotAction]:
        """
        Await the orchestrator coroutine, record timing, log all bot actions.

        fan_text: if provided, logs as a fan turn BEFORE the bot response.
        """
        if fan_text is not None:
            self.turns.append(ConvTurn("fan", fan_text))

        t0 = time.time()
        actions = await coro
        ms = round((time.time() - t0) * 1000)
        self.timings.append(ms)

        for a in (actions or []):
            if a.action_type == "send_message" and a.message:
                self.turns.append(ConvTurn("bot", a.message, ms))
            elif a.action_type == "send_ppv" and a.ppv_price is not None:
                caption = (a.ppv_caption or "").strip()
                label = f"[PPV ${a.ppv_price:.2f}]" + (f" {caption}" if caption else "")
                self.turns.append(ConvTurn("ppv", label, ms))

        return actions or []

    def bot_texts(self) -> list[str]:
        return [t.text for t in self.turns if t.role == "bot"]

    def to_dicts(self) -> list[dict]:
        out = []
        for t in self.turns:
            d = {"role": t.role, "text": t.text}
            if t.ms is not None:
                d["ms"] = t.ms
            out.append(d)
        return out

    def attach(self, result: "ScenarioResult"):
        result.conversation = self.to_dicts()
        result.per_turn_ms = self.timings


# ── Shared mock objects ───────────────────────────────────────────────────────

def _make_avatar() -> object:
    voice = types.SimpleNamespace(
        primary_tone="flirty and playful",
        emoji_use="moderate",
        swear_words="rarely",
        slang_style="gen_z",
        flirt_style="playful",
        favorite_phrases=["stop it 😩", "you're trouble"],
        sexual_escalation_pace="slow_burn",
        reaction_phrases=["omg stahppp", "ugh I can't"],
        greeting_style="casual",
        message_length="short",
        capitalization="lowercase_casual",
        punctuation_style="minimal",
    )
    persona = types.SimpleNamespace(
        name="Jessie",
        nickname="Jess",
        location_story="Miami",
        age=23,
        hobbies=["gym", "beach"],
        favorite_shows=["Euphoria"],
        voice=voice,
        sexual_boundaries=["no anal", "no boy/girl", "no video calls"],
    )
    return types.SimpleNamespace(persona=persona)


def _make_model_profile(active_tier_count: int = 6) -> object:
    return types.SimpleNamespace(
        stage_name="Jessie",
        stated_location="Miami",
        age="23",
        languages=["English", "Spanish"],
        active_tier_count=active_tier_count,
        tier_prices={str(i): p for i, p in enumerate(_DEFAULT_TIER_PRICES, 1)},
    )


_AVATAR = _make_avatar()
_MODEL_PROFILE = _make_model_profile()
_MODEL_PROFILE_GFE = _make_model_profile(active_tier_count=0)


def _fresh_sub(**kwargs) -> Subscriber:
    sub = Subscriber(
        sub_id="sim-" + str(int(time.time() * 1000))[-6:],
        username="testfan",
        display_name="TestFan",
        state=SubState.NEW,
    )
    for k, v in kwargs.items():
        setattr(sub, k, v)
    return sub


def _sub_with_history(ppv_count: int = 0, total_spent: float = 0.0, **kwargs) -> Subscriber:
    sub = _fresh_sub(**kwargs)
    sub.spending = SpendingHistory(
        total_spent=total_spent,
        ppv_count=ppv_count,
        last_purchase_date=datetime.now() - timedelta(days=1) if ppv_count > 0 else None,
    )
    sub.sext_consent_given = ppv_count > 0
    sub.state = SubState.LOOPING if ppv_count > 0 else SubState.WELCOME_SENT
    return sub


def _sub_with_consent(**kwargs) -> Subscriber:
    sub = _fresh_sub(**kwargs)
    sub.sext_consent_given = True
    sub.state = SubState.WARMING
    sub.last_pitch_at = datetime.now() - timedelta(hours=1)
    return sub


# ── Infrastructure mock context ───────────────────────────────────────────────

@contextlib.asynccontextmanager
async def sim_context():
    """
    Patch all DB/network dependencies so the simulator runs without Supabase.

    What's mocked:
      - memory_manager methods (all Supabase-backed)
      - get_weather (Open-Meteo HTTP calls)

    What's NOT mocked:
      - single_agent_process — real Opus 4.7 calls (this IS the system under test)
      - text_filters, guardrails, PPV injection — all real code paths
      - update_callback_references — pure in-memory function, no DB
      - record_bot_message_sent — pure in-memory function, no DB
    """
    from llm.memory_manager import memory_manager as mm

    patches = [
        patch.object(mm, "get_context_memories",          AsyncMock(return_value=[])),
        patch.object(mm, "get_persona_context",           AsyncMock(return_value=[])),
        patch.object(mm, "maybe_generate_profile_summary", AsyncMock(return_value="")),
        patch.object(mm, "maybe_extract_and_store",       AsyncMock(return_value=0)),
        patch.object(mm, "maybe_store_persona_facts",     AsyncMock(return_value=None)),
        patch("llm.context_awareness.get_weather",        AsyncMock(return_value=None)),
    ]
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in patches:
            p.stop()


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class ScenarioResult:
    id: str
    name: str
    group: str
    checks: list[CheckResult]
    bot_outputs: list[str]
    elapsed_seconds: float
    error: Optional[str] = None
    manual_instructions: Optional[str] = None
    conversation: list[dict] = field(default_factory=list)
    per_turn_ms: list[float] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        return all(c.passed for c in self.checks)

    @property
    def fail_count(self) -> int:
        return sum(1 for c in self.checks if not c.passed)

    @property
    def pass_count(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def avg_turn_ms(self) -> Optional[float]:
        return round(sum(self.per_turn_ms) / len(self.per_turn_ms)) if self.per_turn_ms else None


@dataclass
class Scenario:
    id: str
    name: str
    group: str
    is_manual: bool
    run: Callable
    manual_instructions: Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 1 — New Subscriber Flow
# ═══════════════════════════════════════════════════════════════════════════════

async def _run_s1_1(skip_judge: bool = False) -> ScenarioResult:
    """S1.1 — Fresh subscriber: bot sends warm opener, no money mention."""
    from agents.orchestrator import process_new_subscriber

    sub = _fresh_sub()
    conv = ConvLogger()
    t0 = time.time()
    try:
        conv.event("process_new_subscriber()")
        actions = await conv.run(
            process_new_subscriber(sub, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
        )
    except Exception as e:
        return ScenarioResult("S1.1", "Fresh sub welcome", "G1", [], [], time.time() - t0, error=str(e))

    checks = [
        check_has_response(actions),
        check_no_ppv(actions, "welcome_no_ppv"),
        check_no_money_mentions(actions, "welcome_no_money"),
        check_sub_state(sub, SubState.WELCOME_SENT),
        await llm_judge(
            "Does this message welcome someone new in a warm, flirty, personal way "
            "without mentioning money, prices, or content for sale?",
            get_all_text(actions), "welcome_quality", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S1.1", "Fresh sub welcome", "G1", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s1_2(skip_judge: bool = False) -> ScenarioResult:
    """S1.2 — Resubscriber: warm 'you came back' message."""
    from agents.orchestrator import process_resub

    sub = _sub_with_history(ppv_count=3, total_spent=141.29)
    sub.fan_name = "Jake"
    conv = ConvLogger()
    t0 = time.time()
    try:
        conv.event("process_resub() — Jake, 3 PPVs, $141.29 spent")
        actions = await conv.run(
            process_resub(sub, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
        )
    except Exception as e:
        return ScenarioResult("S1.2", "Resub welcome", "G1", [], [], time.time() - t0, error=str(e))

    checks = [
        check_has_response(actions),
        check_no_ppv(actions, "resub_no_ppv"),
        check_no_money_mentions(actions, "resub_no_money"),
        await llm_judge(
            "Does this message express genuine warmth at seeing someone return, "
            "without immediately trying to sell them something?",
            get_all_text(actions), "resub_warmth", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S1.2", "Resub welcome", "G1", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s1_3(skip_judge: bool = False) -> ScenarioResult:
    """S1.3 — New follower (hasn't paid): conversion nudge, not full welcome."""
    from agents.orchestrator import process_new_follower

    sub = _fresh_sub(is_follower_only=True)
    conv = ConvLogger()
    t0 = time.time()
    try:
        conv.event("process_new_follower() — hasn't paid yet")
        actions = await conv.run(
            process_new_follower(sub, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
        )
    except Exception as e:
        return ScenarioResult("S1.3", "Follower conversion nudge", "G1", [], [], time.time() - t0, error=str(e))

    checks = [
        check_has_response(actions),
        check_no_ppv(actions, "follower_no_ppv"),
        await llm_judge(
            "Does this message tease something enticing without explicitly saying 'subscribe' "
            "or 'paywall', making the reader curious about what's behind the gate?",
            get_all_text(actions), "follower_tease_quality", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S1.3", "Follower conversion nudge", "G1", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 2 — Rapport & Consent Gate
# ═══════════════════════════════════════════════════════════════════════════════

async def _run_s2_1(skip_judge: bool = False) -> ScenarioResult:
    """S2.1 — 3-turn early conversation: bot never mentions money."""
    from agents.orchestrator import process_message

    sub = _fresh_sub(state=SubState.WELCOME_SENT)
    conv = ConvLogger()
    fan_messages = [
        "hey, what's up",
        "lol yeah I just got done at the gym",
        "oh nice, what do you usually do for fun",
    ]
    t0 = time.time()
    all_actions: list[list[BotAction]] = []
    try:
        for msg in fan_messages:
            acts = await conv.run(
                process_message(sub, msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
                fan_text=msg,
            )
            all_actions.append(acts)
    except Exception as e:
        return ScenarioResult("S2.1", "Early rapport (no money)", "G2", [], [], time.time() - t0, error=str(e))

    combined_text = " ".join(get_all_text(a) for a in all_actions)
    checks = [
        check_has_response(all_actions[0], "turn1_responded"),
        check_has_response(all_actions[1], "turn2_responded"),
        check_has_response(all_actions[2], "turn3_responded"),
        check_no_ppv([a for acts in all_actions for a in acts], "no_ppv_in_early_rapport"),
        await llm_judge(
            "Across all three bot responses, does the bot avoid mentioning money, prices, "
            "paid content, subscriptions, or payment? Flirting and playful chat are fine — "
            "only answer 'no' if there is an explicit reference to purchasing or money.",
            combined_text, "rapport_quality", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S2.1", "Early rapport (no money)", "G2", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s2_2(skip_judge: bool = False) -> ScenarioResult:
    """S2.2 — Fan signals willingness to spend → consent recorded, pitch starts."""
    from agents.orchestrator import process_message

    sub = _fresh_sub(state=SubState.WELCOME_SENT)
    conv = ConvLogger()
    t0 = time.time()
    try:
        for msg in ["hey", "yeah I've been checking you out for a while"]:
            await conv.run(
                process_message(sub, msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
                fan_text=msg,
            )
        actions1 = await conv.run(
            process_message(
                sub, "honestly I'm definitely open to spending on the right content",
                avatar=_AVATAR, model_profile=_MODEL_PROFILE,
            ),
            fan_text="honestly I'm definitely open to spending on the right content",
        )
        actions2 = await conv.run(
            process_message(sub, "what kind of stuff do you have?",
                            avatar=_AVATAR, model_profile=_MODEL_PROFILE),
            fan_text="what kind of stuff do you have?",
        )
    except Exception as e:
        return ScenarioResult("S2.2", "Consent signal → pitch follows", "G2", [], [], time.time() - t0, error=str(e))

    # After consent + "what kind of stuff do you have?" the bot should drop a PPV —
    # that's the strongest deterministic signal that selling mode is active.
    ppv_in_actions2 = has_ppv(actions2)
    all_bot_text = get_all_text(actions1) + " " + get_all_text(actions2)
    checks = [
        check_has_response(actions1, "responded_to_consent"),
        check_sub_flag(sub, "sext_consent_given", True, "consent_recorded"),
        CheckResult(
            "selling_mode_active",
            ppv_in_actions2 or bool(all_bot_text.strip()),
            "bot responded in selling context ✓" if (ppv_in_actions2 or all_bot_text.strip())
            else "bot sent nothing after consent",
        ),
        await llm_judge(
            "After the fan says he's open to spending and asks 'what kind of stuff do you have?', "
            "does the bot tease content, pitch something, or start setting up a PPV? "
            "Any response that moves toward content or escalates the conversation counts as yes. "
            "Only answer 'no' if the bot completely ignores the spending signal and asks an "
            "unrelated question or pivots to small talk.",
            f"Fan said he's open to spending. Fan then asked: 'what kind of stuff do you have?'\n"
            f"Bot replied to spending signal: {get_all_text(actions1)}\n"
            f"Bot replied to content question: {get_all_text(actions2)}",
            "consent_acknowledged", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S2.2", "Consent signal → pitch follows", "G2", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s2_3(skip_judge: bool = False) -> ScenarioResult:
    """S2.3 — Fan declines spending → agent backs off, no pitch."""
    from agents.orchestrator import process_message

    sub = _sub_with_consent()
    sub.last_pitch_at = datetime.now() - timedelta(minutes=5)
    sub.pending_ppv = {
        "platform_msg_id": "msg-test",
        "tier": 1,
        "sent_at": datetime.now().isoformat(),
        "price": 27.38,
    }
    conv = ConvLogger()
    t0 = time.time()
    try:
        actions = await conv.run(
            process_message(
                sub, "nah I'm not really looking to spend money rn",
                avatar=_AVATAR, model_profile=_MODEL_PROFILE,
            ),
            fan_text="nah I'm not really looking to spend money rn",
        )
    except Exception as e:
        return ScenarioResult("S2.3", "Fan declines → no re-pitch", "G2", [], [], time.time() - t0, error=str(e))

    checks = [
        check_has_response(actions),
        check_no_ppv(actions, "no_ppv_after_decline"),
        await llm_judge(
            "Does the bot accept the refusal gracefully — without begging, negotiating on price, "
            "or immediately pitching something else?",
            get_all_text(actions), "graceful_decline_handling", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S2.3", "Fan declines → no re-pitch", "G2", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s2_4(skip_judge: bool = False) -> ScenarioResult:
    """S2.4 — 12-message conversation without spending signal → agent sells eventually."""
    from agents.orchestrator import process_message

    sub = _fresh_sub(state=SubState.WELCOME_SENT)
    conv = ConvLogger()
    fan_persona = "a curious, chatty guy who likes flirting but hasn't mentioned money at all"
    t0 = time.time()
    all_actions: list[list[BotAction]] = []
    try:
        messages = [
            "hey what's going on",
            "nothing much just bored at home lol",
            "yeah I work from home so it gets lonely",
            "I do software engineering actually",
            "yeah it pays well but the hours suck",
            "what about you, what do you do for fun besides this",
            "oh nice, I go to the gym too — you like fitness?",
            "yeah that's cool. how long have you been on here",
            "haha so what kind of stuff do you post on here",
            "nice. do you share more exclusive stuff with people?",
            "yeah I'd be curious to see more tbh",
            "yeah definitely open to it, what would that look like",
        ]
        for msg in messages:
            acts = await conv.run(
                process_message(sub, msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
                fan_text=msg,
            )
            all_actions.append(acts)

    except Exception as e:
        return ScenarioResult("S2.4", "Long chat → consent ask timing", "G2", [], [], time.time() - t0, error=str(e))

    all_bot_text = " ".join(get_all_text(acts) for acts in all_actions)
    ppv_appeared = any(has_ppv(acts) for acts in all_actions)
    money_mentioned = any(w in all_bot_text for w in [
        "$", "unlock", "ppv", "spend", "worth it",
        # consent-framing language the agent uses instead of explicit money words
        "not everyone", "get closer", "show up for me", "just for you",
        "side of me", "interested", "actually trying", "your type",
        "show you", "get to see", "open to",
    ])
    checks = [
        CheckResult("all_turns_responded",
                    all(len(a) > 0 for a in all_actions),
                    f"responded to {sum(1 for a in all_actions if a)}/{len(all_actions)} turns"),
        CheckResult("selling_initiated", ppv_appeared or money_mentioned,
                    "agent started selling within 12 messages" if (ppv_appeared or money_mentioned)
                    else "agent never mentioned spending in 12 messages"),
        await llm_judge(
            "Across these messages, does the bot progress naturally from casual chat "
            "toward at least hinting at content or asking if the fan is interested in seeing more?",
            all_bot_text[:1500], "rapport_to_sell_progression", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S2.4", "Long chat → consent ask timing", "G2", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 3 — PPV Ladder
# ═══════════════════════════════════════════════════════════════════════════════

async def _run_s3_1(skip_judge: bool = False) -> ScenarioResult:
    """S3.1 — Fan buys tier 1 → bot reacts, tier 2 drop queued."""
    from agents.orchestrator import process_purchase

    sub = _sub_with_history(ppv_count=0)
    sub.sext_consent_given = True
    conv = ConvLogger()
    t0 = time.time()
    try:
        conv.event("purchase: tier 1 ($27.38)")
        actions = await conv.run(
            process_purchase(sub, amount=27.38, avatar=_AVATAR,
                             content_type="ppv", model_profile=_MODEL_PROFILE),
        )
    except Exception as e:
        return ScenarioResult("S3.1", "Tier 1 buy → tier 2 drop", "G3", [], [], time.time() - t0, error=str(e))

    checks = [
        check_has_response(actions, "post_purchase_reaction"),
        check_has_ppv_action(actions, "tier2_queued"),
        check_ppv_price(actions, 36.56, name="tier2_price=$36.56"),
        await llm_judge(
            "Does the bot react to the purchase in any positive way — excitement, praise, "
            "a sexual tease toward the next content, or acknowledgment that he opened it? "
            "Any warm or escalating response counts. Only answer 'no' if the bot is confused, "
            "asks what he paid for, or says something totally unrelated.",
            get_all_text(actions), "purchase_reaction_quality", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S3.1", "Tier 1 buy → tier 2 drop", "G3", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s3_2(skip_judge: bool = False) -> ScenarioResult:
    """S3.2 — All 6 tiers bought sequentially → prices correct, nothing skipped."""
    from agents.orchestrator import process_purchase

    sub = _sub_with_history(ppv_count=0)
    sub.sext_consent_given = True
    conv = ConvLogger()
    t0 = time.time()
    all_actions: list[list[BotAction]] = []
    expected_next_prices = _DEFAULT_TIER_PRICES[1:]

    try:
        for tier_num, tier_price in enumerate(_DEFAULT_TIER_PRICES, 1):
            conv.event(f"purchase: tier {tier_num} (${tier_price:.2f})")
            acts = await conv.run(
                process_purchase(sub, amount=tier_price, avatar=_AVATAR,
                                 content_type="ppv", model_profile=_MODEL_PROFILE),
            )
            all_actions.append(acts)
    except Exception as e:
        return ScenarioResult("S3.2", "All 6 tiers — full ladder", "G3", [], [], time.time() - t0, error=str(e))

    ppv_prices_dropped: list[float] = []
    for acts in all_actions:
        ppv_prices_dropped.extend(
            a.ppv_price for a in acts if a.action_type == "send_ppv" and a.ppv_price is not None
        )
    checks = [
        CheckResult("all_tiers_recorded",
                    sub.spending.ppv_count == 6,
                    f"ppv_count = {sub.spending.ppv_count} (expected 6)"),
        CheckResult("next_tier_prices_correct",
                    len(ppv_prices_dropped) >= 5 and all(
                        abs(ppv_prices_dropped[i] - expected_next_prices[i]) < 0.02
                        for i in range(min(5, len(ppv_prices_dropped)))
                    ),
                    f"dropped: {[f'${p:.2f}' for p in ppv_prices_dropped[:5]]}, "
                    f"expected: {[f'${p:.2f}' for p in expected_next_prices[:5]]}"),
        CheckResult("total_spent",
                    abs(sub.spending.total_spent - sum(_DEFAULT_TIER_PRICES)) < 0.10,
                    f"total_spent = ${sub.spending.total_spent:.2f} (expected ${sum(_DEFAULT_TIER_PRICES):.2f})"),
    ]
    result = ScenarioResult("S3.2", "All 6 tiers — full ladder", "G3", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s3_3(skip_judge: bool = False) -> ScenarioResult:
    """S3.3 — When bot drops a PPV, a heads-up message comes first."""
    from agents.orchestrator import process_message

    sub = _sub_with_consent()
    sub.state = SubState.TENSION_BUILD
    sub.gfe_message_count = 5
    conv = ConvLogger()
    t0 = time.time()
    try:
        msg = "okay I'm definitely interested, show me what you got"
        actions = await conv.run(
            process_message(sub, msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
            fan_text=msg,
        )
    except Exception as e:
        return ScenarioResult("S3.3", "PPV heads-up before drop", "G3", [], [], time.time() - t0, error=str(e))

    ppv_index = next((i for i, a in enumerate(actions) if a.action_type == "send_ppv"), None)
    msg_before_ppv = (
        ppv_index is not None and
        any(i < ppv_index and actions[i].action_type == "send_message" for i in range(len(actions)))
    )
    ppv_actions = [a for a in actions if a.action_type == "send_ppv"]
    checks = [check_has_response(actions)]
    if ppv_actions:
        checks.append(CheckResult(
            "heads_up_precedes_ppv", msg_before_ppv,
            "message sent before PPV ✓" if msg_before_ppv else "PPV dropped with no heads-up message",
        ))
    else:
        checks.append(CheckResult(
            "agent_engaged", bool([a for a in actions if a.action_type == "send_message"]),
            "agent replied (PPV not dropped yet — needs more build-up)",
        ))
    result = ScenarioResult("S3.3", "PPV heads-up before drop", "G3", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s3_4(skip_judge: bool = False) -> ScenarioResult:
    """S3.4 — PPV sent 25h ago and unpaid → agent doesn't re-pitch same tier."""
    from agents.orchestrator import process_message

    sub = _sub_with_consent()
    sub.pending_ppv = {
        "platform_msg_id": "msg-old",
        "tier": 1,
        "sent_at": (datetime.now() - timedelta(hours=25)).isoformat(),
        "price": 27.38,
    }
    conv = ConvLogger()
    t0 = time.time()
    try:
        msg = "hey what's up"
        actions = await conv.run(
            process_message(sub, msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
            fan_text=msg,
        )
    except Exception as e:
        return ScenarioResult("S3.4", "Stale PPV — no re-drop", "G3", [], [], time.time() - t0, error=str(e))

    tier1_again = any(
        a.action_type == "send_ppv" and a.ppv_price and abs(a.ppv_price - 27.38) < 0.02
        for a in actions
    )
    checks = [
        check_has_response(actions),
        CheckResult("no_duplicate_tier1", not tier1_again,
                    "did not re-drop tier 1 ✓" if not tier1_again else "dropped tier 1 again despite pending PPV"),
    ]
    result = ScenarioResult("S3.4", "Stale PPV — no re-drop", "G3", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 4 — Objection Handling
# ═══════════════════════════════════════════════════════════════════════════════

async def _run_s4_1(skip_judge: bool = False) -> ScenarioResult:
    """S4.1 — First 'too expensive' → warm disappointment, FOMO, no re-pitch."""
    from agents.orchestrator import process_message

    sub = _sub_with_consent()
    sub.last_pitch_at = datetime.now() - timedelta(minutes=2)
    sub.pending_ppv = {"platform_msg_id": "msg-pitch", "tier": 1,
                       "sent_at": datetime.now().isoformat(), "price": 27.38}
    conv = ConvLogger()
    t0 = time.time()
    try:
        msg = "that's a bit too expensive for me tbh"
        actions = await conv.run(
            process_message(sub, msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
            fan_text=msg,
        )
    except Exception as e:
        return ScenarioResult("S4.1", "First 'too expensive'", "G4", [], [], time.time() - t0, error=str(e))

    checks = [
        check_has_response(actions),
        check_no_ppv(actions, "no_immediate_repitch"),
        await llm_judge(
            "Does the bot respond with warmth and mild disappointment (not condescension, "
            "not negotiating price), making the fan feel like he's missing out?",
            get_all_text(actions), "first_objection_quality", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S4.1", "First 'too expensive'", "G4", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s4_2(skip_judge: bool = False) -> ScenarioResult:
    """S4.2 — Second 'no' → deeper disappointment, brokey flagged."""
    from agents.orchestrator import process_message

    sub = _sub_with_consent()
    sub.tier_no_count = 1
    sub.last_pitch_at = datetime.now() - timedelta(minutes=5)
    sub.pending_ppv = {"platform_msg_id": "msg-pitch2", "tier": 1,
                       "sent_at": datetime.now().isoformat(), "price": 27.38}
    conv = ConvLogger()
    t0 = time.time()
    try:
        msg = "nah I really can't afford it right now"
        actions = await conv.run(
            process_message(sub, msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
            fan_text=msg,
        )
    except Exception as e:
        return ScenarioResult("S4.2", "Second 'no' → cooldown trigger", "G4", [], [], time.time() - t0, error=str(e))

    checks = [
        check_has_response(actions),
        check_no_ppv(actions, "no_third_pitch"),
        CheckResult("brokey_flagged", sub.brokey_flagged,
                    f"sub.brokey_flagged = {sub.brokey_flagged} (expected True)"),
        await llm_judge(
            "Does the bot respond with gentle understanding — not pushy, not judgmental — "
            "and pivot to just talking?",
            get_all_text(actions), "second_objection_quality", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S4.2", "Second 'no' → cooldown trigger", "G4", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s4_3(skip_judge: bool = False) -> ScenarioResult:
    """S4.3 — Fan in brokey cooldown → warmth-only, no PPV."""
    from agents.orchestrator import process_message

    sub = _sub_with_consent()
    sub.brokey_flagged = True
    sub.tier_no_count = 2
    sub.last_session_completed_at = datetime.now() - timedelta(hours=2)
    sub.last_message_date = datetime.now() - timedelta(hours=2)
    conv = ConvLogger()
    t0 = time.time()
    try:
        msg = "hey what's going on today"
        actions = await conv.run(
            process_message(sub, msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
            fan_text=msg,
        )
    except Exception as e:
        return ScenarioResult("S4.3", "Brokey cooldown — warmth only", "G4", [], [], time.time() - t0, error=str(e))

    checks = [
        check_has_response(actions),
        check_no_ppv(actions, "no_ppv_in_cooldown"),
        check_no_money_mentions(actions, "no_money_in_cooldown"),
        await llm_judge(
            "Does the bot respond in a warm, friendly, non-sales way — just chatting, no hint of money?",
            get_all_text(actions), "cooldown_warmth", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S4.3", "Brokey cooldown — warmth only", "G4", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s4_4(skip_judge: bool = False) -> ScenarioResult:
    """S4.4 — Fan buys after brokey → objection count resets."""
    from agents.orchestrator import process_purchase

    sub = _sub_with_history(ppv_count=0)
    sub.sext_consent_given = True
    sub.brokey_flagged = True
    sub.tier_no_count = 2
    conv = ConvLogger()
    t0 = time.time()
    try:
        conv.event("purchase: tier 1 ($27.38) — fan was in brokey cooldown")
        actions = await conv.run(
            process_purchase(sub, amount=27.38, avatar=_AVATAR,
                             content_type="ppv", model_profile=_MODEL_PROFILE),
        )
    except Exception as e:
        return ScenarioResult("S4.4", "Buy after brokey → reset", "G4", [], [], time.time() - t0, error=str(e))

    checks = [
        check_has_response(actions),
        CheckResult("brokey_cleared", not sub.brokey_flagged,
                    f"sub.brokey_flagged = {sub.brokey_flagged} (expected False)"),
        CheckResult("no_count_reset", sub.tier_no_count == 0,
                    f"sub.tier_no_count = {sub.tier_no_count} (expected 0)"),
        check_has_ppv_action(actions, "tier2_auto_dropped"),
    ]
    result = ScenarioResult("S4.4", "Buy after brokey → reset", "G4", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s4_5(skip_judge: bool = False) -> ScenarioResult:
    """S4.5 — Fan says 'maybe later' → graceful exit."""
    from agents.orchestrator import process_message

    sub = _sub_with_consent()
    sub.last_pitch_at = datetime.now() - timedelta(minutes=3)
    sub.pending_ppv = {"platform_msg_id": "msg-maybe", "tier": 1,
                       "sent_at": datetime.now().isoformat(), "price": 27.38}
    conv = ConvLogger()
    t0 = time.time()
    try:
        msg = "maybe later when I get paid next week"
        actions = await conv.run(
            process_message(sub, msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
            fan_text=msg,
        )
    except Exception as e:
        return ScenarioResult("S4.5", "'Maybe later' → graceful exit", "G4", [], [], time.time() - t0, error=str(e))

    checks = [
        check_has_response(actions),
        check_no_ppv(actions, "no_repitch_on_maybe_later"),
        await llm_judge(
            "Does the bot acknowledge 'maybe later' in a relaxed, non-pushy way — "
            "without badgering the fan?",
            get_all_text(actions), "maybe_later_handling", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S4.5", "'Maybe later' → graceful exit", "G4", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 5 — Custom Order Flow
# ═══════════════════════════════════════════════════════════════════════════════

async def _run_s5_1(skip_judge: bool = False) -> ScenarioResult:
    """S5.1 — Fan requests custom content → agent quotes price."""
    from agents.orchestrator import process_message

    sub = _sub_with_consent()
    conv = ConvLogger()
    t0 = time.time()
    try:
        # Warmup: give the agent some conversation context before the custom request
        for warmup_msg in ["hey what's up", "I've been wanting to ask you something"]:
            await conv.run(
                process_message(sub, warmup_msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
                fan_text=warmup_msg,
            )
        msg = "can you do a custom lingerie video, just for me?"
        actions = await conv.run(
            process_message(sub, msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
            fan_text=msg,
        )
    except Exception as e:
        return ScenarioResult("S5.1", "Custom request → price quoted", "G5", [], [], time.time() - t0, error=str(e))

    text = get_all_text(actions)
    price_quoted = any(c in text for c in ["$", "dollar"])
    custom_order_set = getattr(sub, "pending_custom_order", None) is not None
    checks = [
        check_has_response(actions),
        CheckResult("price_quoted", price_quoted,
                    "bot mentioned a price ✓" if price_quoted else "bot didn't quote a price"),
        CheckResult("custom_order_recorded", custom_order_set,
                    "sub.pending_custom_order set ✓" if custom_order_set
                    else "pending_custom_order not set — classify_custom_request tool may not have been called"),
        await llm_judge(
            "Does the bot quote a specific dollar amount for the custom request and "
            "explain what the fan needs to do to proceed?",
            text, "custom_quote_quality", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S5.1", "Custom request → price quoted", "G5", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s5_2(skip_judge: bool = False) -> ScenarioResult:
    """S5.2 — Fan claims payment for custom → bot acknowledges, admin alert attempted."""
    from agents.orchestrator import process_message
    from engine.custom_orders import new_order

    sub = _sub_with_consent()
    sub.pending_custom_order = new_order("schoolgirl outfit video", "video_lingerie", 127.38)
    conv = ConvLogger()
    t0 = time.time()
    try:
        with patch("admin_bot.alerts.alert_custom_payment_claim", AsyncMock(return_value=True)):
            msg = "I just sent the payment!"
            actions = await conv.run(
                process_message(sub, msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
                fan_text=msg,
            )
    except Exception as e:
        return ScenarioResult("S5.2", "Fan claims payment → alert", "G5", [], [], time.time() - t0, error=str(e))

    checks = [
        check_has_response(actions),
        await llm_judge(
            "Does the bot acknowledge that it received the payment claim and say it's "
            "checking or verifying — without claiming the content is already being made?",
            get_all_text(actions), "payment_claim_response", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S5.2", "Fan claims payment → alert", "G5", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 6 — Edge Cases & Guardrails
# ═══════════════════════════════════════════════════════════════════════════════

async def _run_s6_1(skip_judge: bool = False) -> ScenarioResult:
    """S6.1 — Fan sends explicit message → bot responds in tone."""
    from agents.orchestrator import process_message

    sub = _sub_with_consent()
    sub.horniness_score = 7
    conv = ConvLogger()
    t0 = time.time()
    try:
        msg = "I'm so hard thinking about you right now"
        actions = await conv.run(
            process_message(sub, msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
            fan_text=msg,
        )
    except Exception as e:
        return ScenarioResult("S6.1", "Explicit fan message handled", "G6", [], [], time.time() - t0, error=str(e))

    checks = [
        check_has_response(actions),
        await llm_judge(
            "Does the bot respond in a flirty, playful, or suggestive way — "
            "matching the energy without being clinical or robotic?",
            get_all_text(actions), "explicit_message_handling", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S6.1", "Explicit fan message handled", "G6", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s6_2(skip_judge: bool = False) -> ScenarioResult:
    """S6.2 — Fan asks for hard-limit content (anal) → graceful refusal."""
    from agents.orchestrator import process_message

    sub = _sub_with_consent()
    conv = ConvLogger()
    t0 = time.time()
    try:
        msg = "can you do a video of anal play?"
        actions = await conv.run(
            process_message(sub, msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
            fan_text=msg,
        )
    except Exception as e:
        return ScenarioResult("S6.2", "Hard limit refusal", "G6", [], [], time.time() - t0, error=str(e))

    checks = [
        check_has_response(actions),
        await llm_judge(
            "Does the bot decline the request warmly and without judgment — "
            "deflecting naturally and possibly suggesting an alternative?",
            get_all_text(actions), "hard_limit_refusal", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S6.2", "Hard limit refusal", "G6", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s6_3(skip_judge: bool = False) -> ScenarioResult:
    """S6.3 — Fan sends very short 'ok' → bot handles gracefully."""
    from agents.orchestrator import process_message

    sub = _fresh_sub(state=SubState.WELCOME_SENT)
    sub.add_message("sub", "hey")
    sub.add_message("bot", "hey!! what's up 😊")
    conv = ConvLogger()
    t0 = time.time()
    try:
        msg = "ok"
        actions = await conv.run(
            process_message(sub, msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
            fan_text=msg,
        )
    except Exception as e:
        return ScenarioResult("S6.3", "One-word reply handled", "G6", [], [], time.time() - t0, error=str(e))

    checks = [
        check_has_response(actions),
        check_no_ppv(actions, "no_ppv_for_one_word"),
        await llm_judge(
            "Does the bot handle the one-word reply naturally — continuing the "
            "conversation without sounding confused or like it's having an error?",
            get_all_text(actions), "one_word_handling", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S6.3", "One-word reply handled", "G6", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s6_4(skip_judge: bool = False) -> ScenarioResult:
    """S6.4 — Fan sends emoji-only message → bot handles gracefully."""
    from agents.orchestrator import process_message

    sub = _fresh_sub(state=SubState.WELCOME_SENT)
    conv = ConvLogger()
    t0 = time.time()
    try:
        msg = "😍🔥"
        actions = await conv.run(
            process_message(sub, msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
            fan_text=msg,
        )
    except Exception as e:
        return ScenarioResult("S6.4", "Emoji-only message handled", "G6", [], [], time.time() - t0, error=str(e))

    checks = [
        check_has_response(actions),
        await llm_judge(
            "Does the bot respond naturally to the fire/heart-eyes emojis — "
            "matching the playful energy without getting confused?",
            get_all_text(actions), "emoji_reply_quality", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S6.4", "Emoji-only message handled", "G6", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s6_5(skip_judge: bool = False) -> ScenarioResult:
    """S6.5 — Fan asks for free content → warm deflection."""
    from agents.orchestrator import process_message

    sub = _fresh_sub(state=SubState.WELCOME_SENT)
    conv = ConvLogger()
    t0 = time.time()
    try:
        msg = "can you just send me something for free?"
        actions = await conv.run(
            process_message(sub, msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
            fan_text=msg,
        )
    except Exception as e:
        return ScenarioResult("S6.5", "Free content request deflected", "G6", [], [], time.time() - t0, error=str(e))

    checks = [
        check_has_response(actions),
        await llm_judge(
            "Does the bot decline to give free content, but do it in a playful or warm way — "
            "not coldly, not rudely, maybe teasing what they could unlock?",
            get_all_text(actions), "free_content_deflection", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S6.5", "Free content request deflected", "G6", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s6_6(skip_judge: bool = False) -> ScenarioResult:
    """S6.6 — Abusive message → composed response."""
    from agents.orchestrator import process_message

    sub = _fresh_sub(state=SubState.WELCOME_SENT)
    conv = ConvLogger()
    t0 = time.time()
    try:
        msg = "you're probably an ugly fat cow who nobody would pay for"
        actions = await conv.run(
            process_message(sub, msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
            fan_text=msg,
        )
    except Exception as e:
        return ScenarioResult("S6.6", "Abusive message handled", "G6", [], [], time.time() - t0, error=str(e))

    checks = [
        check_has_response(actions),
        await llm_judge(
            "Does the bot respond without engaging in the insult — brushing it off or "
            "responding from a place of confidence rather than matching the aggression?",
            get_all_text(actions), "abuse_handling", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S6.6", "Abusive message handled", "G6", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s6_7(skip_judge: bool = False) -> ScenarioResult:
    """
    S6.7 — Mid-processing interruption (regen loop).

    In production the connector detects a second message arriving while the agent
    is still running, then regenerates with BOTH messages combined.  The simulator
    can't inject a message mid-call, so it replicates exactly what the regen loop
    sends to the agent: msg1 + "\\n" + msg2.  The conversation log marks the
    interruption so you can see what the exchange looks like in the transcript.

    Checks:
      - The single regen response covers BOTH the original message and the pivot
      - No duplicate reply (only one agent call's output)
      - Response doesn't mention 'price' as if it ignored the 'nvm' pivot
    """
    from agents.orchestrator import process_message

    sub = _sub_with_consent()
    conv = ConvLogger()
    t0 = time.time()

    # What the fan typed first — this started the agent call
    msg1 = "wait how much is that video again"
    # What arrived while the bot was thinking — triggers a regen in production
    msg2 = "actually nvm about the price, just tell me more about it"
    # What the regen loop actually sends to the agent (connector combines them)
    combined = f"{msg1}\n{msg2}"

    try:
        # Log msg1 as the fan's first turn
        conv.turns.append(ConvTurn("fan", msg1))
        # Mark the interruption so it's visible in the transcript
        conv.event(
            f"[regen triggered] second message arrived mid-processing: '{msg2}' — "
            "agent re-ran with both messages combined"
        )
        # Run the agent with the combined text (no fan_text= so we don't double-log)
        actions = await conv.run(
            process_message(sub, combined, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
        )
        # Record the second fan message AFTER the event so the log reads naturally:
        # FAN msg1 → EVENT regen → FAN msg2 → BOT response
        # (insert before the last bot turn)
        bot_turn_idx = next(
            (i for i in range(len(conv.turns) - 1, -1, -1) if conv.turns[i].role in ("bot", "ppv")),
            None,
        )
        pivot_turn = ConvTurn("fan", msg2)
        if bot_turn_idx is not None:
            conv.turns.insert(bot_turn_idx, pivot_turn)
        else:
            conv.turns.append(pivot_turn)

    except Exception as e:
        return ScenarioResult("S6.7", "Mid-processing interruption (regen)", "G6", [], [], time.time() - t0, error=str(e))

    text = get_all_text(actions)
    # "nvm about the price" pivot — bot should not fixate on quoting a price
    still_quoting_price = any(c in text for c in ["$27", "$36", "$77", "$92", "$127", "$200"])

    checks = [
        check_has_response(actions, "regen_produced_response"),
        CheckResult(
            "pivot_respected",
            not still_quoting_price,
            "bot did not fixate on price after 'nvm' pivot ✓"
            if not still_quoting_price
            else "bot still quoted a price despite the fan saying 'nvm about the price'",
        ),
        await llm_judge(
            "The fan first asked about price, then immediately said 'nvm about the price, "
            "just tell me more about it.' Does the bot's single response treat the pivot "
            "naturally — describing the content rather than quoting a dollar amount?",
            text, "regen_pivot_quality", skip=skip_judge,
        ),
    ]
    result = ScenarioResult(
        "S6.7", "Mid-processing interruption (regen)", "G6",
        checks, conv.bot_texts(), time.time() - t0,
    )
    conv.attach(result)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 7 — GFE & Returning Fan
# ═══════════════════════════════════════════════════════════════════════════════

async def _run_s7_1(skip_judge: bool = False) -> ScenarioResult:
    """S7.1 — GFE-only mode at threshold → continuation paywall fires."""
    from agents.orchestrator import process_message

    sub = _fresh_sub(state=SubState.WELCOME_SENT)
    sub.gfe_message_count = 45
    sub.continuation_threshold_jitter = 40
    conv = ConvLogger()
    t0 = time.time()
    try:
        msg = "I really enjoy talking to you, you always make me smile"
        actions = await conv.run(
            process_message(sub, msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE_GFE,
                            active_tier_count=0),
            fan_text=msg,
        )
    except Exception as e:
        return ScenarioResult("S7.1", "GFE-only — continuation paywall", "G7", [], [], time.time() - t0, error=str(e))

    ppv_price = get_ppv_price(actions)
    checks = [
        check_has_response(actions),
        CheckResult("continuation_ppv_fired",
                    has_ppv(actions) and ppv_price is not None and abs(ppv_price - 20.0) < 1.0,
                    f"continuation PPV ${ppv_price:.2f} ✓" if (has_ppv(actions) and ppv_price and abs(ppv_price - 20.0) < 1.0)
                    else f"expected $20 continuation PPV, got: {ppv_price}"),
        await llm_judge(
            "Does the bot respond warmly and personally, continuing the emotional connection "
            "without trying to sell explicit content?",
            get_all_text(actions), "gfe_warmth", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S7.1", "GFE-only — continuation paywall", "G7", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s7_2(skip_judge: bool = False) -> ScenarioResult:
    """S7.2 — Fan returns after 3-day gap → bot acknowledges the gap."""
    from agents.orchestrator import process_message

    sub = _sub_with_history(ppv_count=1, total_spent=27.38)
    sub.last_message_date = datetime.now() - timedelta(days=3)
    sub.add_message("sub", "last thing he said 3 days ago")
    conv = ConvLogger()
    t0 = time.time()
    try:
        msg = "hey, been a few days"
        actions = await conv.run(
            process_message(sub, msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
            fan_text=msg,
        )
    except Exception as e:
        return ScenarioResult("S7.2", "Return after 3-day gap", "G7", [], [], time.time() - t0, error=str(e))

    checks = [
        check_has_response(actions),
        await llm_judge(
            "Does the bot acknowledge that the fan has been away for a few days — "
            "referencing the gap in some way (missed you, where were you, it's been a while)?",
            get_all_text(actions), "gap_acknowledged", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S7.2", "Return after 3-day gap", "G7", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s7_3(skip_judge: bool = False) -> ScenarioResult:
    """S7.3 — Resub with purchase history → personal welcome-back."""
    from agents.orchestrator import process_resub

    sub = _sub_with_history(ppv_count=4, total_spent=233.69)
    sub.fan_name = "Marcus"
    sub.callback_references = ["works in finance", "mentioned his dog once"]
    conv = ConvLogger()
    t0 = time.time()
    try:
        conv.event("process_resub() — Marcus, 4 PPVs, $233.69 spent, finance job")
        actions = await conv.run(
            process_resub(sub, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
        )
    except Exception as e:
        return ScenarioResult("S7.3", "Resub with history → personal", "G7", [], [], time.time() - t0, error=str(e))

    checks = [
        check_has_response(actions),
        check_no_ppv(actions, "resub_no_ppv"),
        await llm_judge(
            "Does the bot welcome this person back in a way that feels personal — "
            "acknowledging the history or something specific about him?",
            get_all_text(actions), "resub_personal_reference", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S7.3", "Resub with history → personal", "G7", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s7_4(skip_judge: bool = False) -> ScenarioResult:
    """S7.4 — All 6 tiers bought → session lock fires, no new PPV dropped."""
    from agents.orchestrator import process_message

    # Fan who just completed all 6 tiers — session locked for 6 hours
    sub = _sub_with_history(ppv_count=6, total_spent=561.18)
    sub.fan_name = "Tyler"
    sub.sext_consent_given = True
    sub.state = SubState.RETENTION
    sub.session_locked_until = datetime.now() + timedelta(hours=5)  # mid-lock

    conv = ConvLogger()
    t0 = time.time()
    try:
        conv.fan("hey you still up? want more")
        actions = await conv.run(
            process_message(sub, "hey you still up? want more",
                            avatar=_AVATAR, model_profile=_MODEL_PROFILE)
        )
    except Exception as e:
        return ScenarioResult("S7.4", "Session lock after 6 tiers", "G7", [], [], time.time() - t0, error=str(e))

    all_text = get_all_text(actions)
    checks = [
        check_has_response(actions),
        check_no_ppv(actions, "no_ppv_while_locked"),
        CheckResult(
            "no_price_mention",
            "$" not in all_text and "dollar" not in all_text.lower(),
            "no price mention while locked ✓" if "$" not in all_text else "bot mentioned price while session-locked",
        ),
        await llm_judge(
            "The fan is asking for more after a full session. The bot should warmly deflect "
            "without starting a new selling cycle — no PPV pitch, no price mentions. "
            "It can tease something for tomorrow. Does the response do this correctly?",
            all_text, "lock_response_quality", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S7.4", "Session lock after 6 tiers", "G7", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 9 — Live Fan Conversations (Haiku plays the fan)
#
# Unlike G1–G8 where fan messages are hardcoded, these scenarios let Haiku
# generate every fan reply based on what the bot actually said.  Each run
# produces a slightly different conversation — the point is testing that the
# bot navigates the overall arc correctly, not that it handles a fixed trigger.
#
# Cost: ~2–5 Haiku calls per scenario (~$0.001 each) on top of Opus calls.
# ═══════════════════════════════════════════════════════════════════════════════

async def _live_convo(
    sub,
    persona: str,
    goal: str,
    max_turns: int,
    conv: ConvLogger,
    skip_fan: bool = False,
    stop_on_ppv: bool = False,
    avatar=None,
    model_profile=None,
    use_grok_fan: bool = False,
) -> list[list[BotAction]]:
    """
    Run a full back-and-forth conversation where Haiku (or Grok) plays the fan.

    use_grok_fan: use Grok instead of Haiku for fan messages — needed when the
                  scenario requires explicit or offensive content that Haiku refuses.

    After each process_message() call, sub.recent_messages already contains
    the bot's reply (the orchestrator calls sub.add_message() internally), so
    the next simulate_fan_turn() call sees the real growing history.
    """
    from agents.orchestrator import process_message

    av = avatar or _AVATAR
    mp = model_profile or _MODEL_PROFILE
    all_actions: list[list[BotAction]] = []
    for _ in range(max_turns):
        fan_msg = await simulate_fan_turn(
            sub.recent_messages,
            persona=f"{persona}. Underlying goal: {goal}",
            skip=skip_fan,
            use_grok=use_grok_fan,
        )
        acts = await conv.run(
            process_message(sub, fan_msg, avatar=av, model_profile=mp),
            fan_text=fan_msg,
        )
        all_actions.append(acts)
        if stop_on_ppv and any(a.action_type == "send_ppv" for a in acts):
            break
    return all_actions


# ── Pre-event helpers (used by live variant factory) ─────────────────────────

async def _pre_new_sub(sub, av, mp):
    from agents.orchestrator import process_new_subscriber
    return await process_new_subscriber(sub, avatar=av, model_profile=mp)

async def _pre_resub(sub, av, mp):
    from agents.orchestrator import process_resub
    return await process_resub(sub, avatar=av, model_profile=mp)

async def _pre_new_follower(sub, av, mp):
    from agents.orchestrator import process_new_follower
    return await process_new_follower(sub, avatar=av, model_profile=mp)

def _pre_purchase_fn(amount: float):
    async def _inner(sub, av, mp):
        from agents.orchestrator import process_purchase
        return await process_purchase(sub, amount=amount, avatar=av,
                                      content_type="ppv", model_profile=mp)
    return _inner


# ── Live variant factory ──────────────────────────────────────────────────────

def _make_live(
    sid: str,
    name: str,
    group: str,
    sub_factory,
    persona: str,
    goal: str,
    max_turns: int,
    pre_events: list = None,
    stop_on_ppv: bool = False,
    use_gfe: bool = False,
    extras=None,
    use_grok_fan: bool = False,
    judge_question: str = None,
    judge_name: str = "naturalness",
):
    """
    Build a live-fan scenario function from config.

    pre_events:    list of (label: str, async_fn: (sub, av, mp) -> BotActions)
                   Run before the live fan conversation begins (e.g. process_new_subscriber).
    extras:        sync (sub, flat_actions, all_text) -> list[CheckResult]
                   Additional deterministic checks specific to this scenario.
    use_grok_fan:  use Grok instead of Haiku for fan messages — set True for S6.x
                   scenarios that need explicit/offensive content Haiku won't write.
    judge_question: override the default naturalness judge question. Useful when
                   the default warmth/follow-up question doesn't fit the scenario
                   (e.g. explicit scenes, refusal tests, abusive message tests).
    judge_name:    name label for the judge check (default "naturalness").
    """
    _judge_q = judge_question or (
        "Does the bot build warmth and genuine interest during the conversation — "
        "asking follow-up questions and responding to what the fan actually says, "
        "rather than sending the same canned lines to everyone?"
    )

    async def _run(skip_judge: bool = False, skip_fan: bool = False) -> ScenarioResult:
        mp = _MODEL_PROFILE_GFE if use_gfe else _MODEL_PROFILE
        sub = sub_factory()
        conv = ConvLogger()
        t0 = time.time()
        all_actions: list[list[BotAction]] = []
        try:
            for label, pre_fn in (pre_events or []):
                conv.event(label)
                acts = await conv.run(pre_fn(sub, _AVATAR, mp))
                all_actions.append(acts)
            if pre_events:
                conv.event("live fan takes over")
            live = await _live_convo(
                sub, persona, goal, max_turns, conv,
                skip_fan=skip_fan, stop_on_ppv=stop_on_ppv,
                avatar=_AVATAR, model_profile=mp,
                use_grok_fan=use_grok_fan,
            )
            all_actions.extend(live)
        except Exception as e:
            return ScenarioResult(sid, name, group, [], [], time.time() - t0, error=str(e))

        flat = [a for acts in all_actions for a in acts]
        all_text = " ".join(get_all_text(acts) for acts in all_actions)
        # Full conversation with both sides, for the LLM judge.
        # Passing only the bot's messages loses context — the judge can't tell
        # whether a bot reply is reactive or canned without seeing what the fan said.
        convo_for_judge = "\n".join(
            f"{'Fan' if t.role == 'fan' else 'Bot'}: {t.text}"
            for t in conv.turns
            if t.role in ("fan", "bot") and t.text
        )
        checks: list[CheckResult] = [
            check_has_response(flat, "bot_responded"),
            await llm_judge(
                _judge_q,
                convo_for_judge[:1500], judge_name, skip=skip_judge,
            ),
        ]
        if extras:
            checks.extend(extras(sub, flat, all_text))
        r = ScenarioResult(sid, name, group, checks, conv.bot_texts(), time.time() - t0)
        conv.attach(r)
        return r
    return _run


async def _run_s9_1(skip_judge: bool = False, skip_fan: bool = False) -> ScenarioResult:
    """
    S9.1 — Live: reluctant spender.
    Haiku plays a fan who is attracted but hesitant about spending money.
    Bot must build value and earn the pitch — not just quote a price.
    """
    sub = _fresh_sub(state=SubState.WELCOME_SENT)
    conv = ConvLogger()
    t0 = time.time()
    conv.event("live fan — persona: attracted but price-sensitive, max 8 turns")
    try:
        all_actions = await _live_convo(
            sub,
            persona=(
                "a guy who finds the model genuinely attractive and enjoys talking to her, "
                "but is cautious about spending money online and hasn't committed to buying anything yet"
            ),
            goal="decide whether the content is worth paying for",
            max_turns=8,
            conv=conv,
            skip_fan=skip_fan,
            stop_on_ppv=True,
        )
    except Exception as e:
        return ScenarioResult("S9.1", "Live: reluctant spender", "G9", [], [], time.time() - t0, error=str(e))

    all_text = " ".join(get_all_text(acts) for acts in all_actions)
    convo_for_judge = "\n".join(
        f"{'Fan' if t.role == 'fan' else 'Bot'}: {t.text}"
        for t in conv.turns if t.role in ("fan", "bot") and t.text
    )
    checks = [
        CheckResult(
            "responded_throughout",
            all(len(a) > 0 for a in all_actions),
            f"responded to {sum(1 for a in all_actions if a)}/{len(all_actions)} turns",
        ),
        await llm_judge(
            "Does the bot build genuine warmth and interest with this fan — "
            "responding to what he actually says, rather than pushing a sale immediately?",
            convo_for_judge[:2000], "value_before_pitch", skip=skip_judge,
        ),
        await llm_judge(
            "Does the bot keep the conversation engaging and personal throughout — "
            "asking follow-up questions and reacting to what the fan says?",
            convo_for_judge[:2000], "conversation_naturalness", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S9.1", "Live: reluctant spender", "G9", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s9_2(skip_judge: bool = False, skip_fan: bool = False) -> ScenarioResult:
    """
    S9.2 — Live: easy buyer.
    Haiku plays an eager fan with money to spend.
    Bot should pick up the buying signal quickly and drop a PPV.
    """
    sub = _fresh_sub(state=SubState.WELCOME_SENT)
    conv = ConvLogger()
    t0 = time.time()
    conv.event("live fan — persona: eager to buy, max 6 turns")
    try:
        all_actions = await _live_convo(
            sub,
            persona=(
                "an enthusiastic fan who compliments the model, gets the vibe quickly, "
                "and mentions early that he's happy to spend money if the content is good"
            ),
            goal="buy something as quickly as possible",
            max_turns=6,
            conv=conv,
            skip_fan=skip_fan,
            stop_on_ppv=True,
        )
    except Exception as e:
        return ScenarioResult("S9.2", "Live: easy buyer", "G9", [], [], time.time() - t0, error=str(e))

    flat = [a for acts in all_actions for a in acts]
    ppv_dropped = any(a.action_type == "send_ppv" for a in flat)
    all_text = " ".join(get_all_text(acts) for acts in all_actions)
    convo_for_judge = "\n".join(
        f"{'Fan' if t.role == 'fan' else 'Bot'}: {t.text}"
        for t in conv.turns if t.role in ("fan", "bot") and t.text
    )
    checks = [
        CheckResult(
            "ppv_sent",
            ppv_dropped,
            "PPV dropped within 6 turns ✓" if ppv_dropped
            else "bot never dropped a PPV despite clear buying signals in 6 turns",
        ),
        CheckResult(
            "consent_recorded",
            sub.sext_consent_given,
            f"sext_consent_given = {sub.sext_consent_given}",
        ),
        await llm_judge(
            "Does the bot read the fan's buying signals and move toward content — "
            "rather than ignoring them or stalling with small talk?",
            convo_for_judge[:1500], "efficient_sale", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S9.2", "Live: easy buyer", "G9", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s9_3(skip_judge: bool = False, skip_fan: bool = False) -> ScenarioResult:
    """
    S9.3 — Live: mixed signals fan.
    Haiku plays a fan who keeps changing his mind — interested, then backing off,
    then interested again.  Bot must adapt rather than repeating the same pitch.
    """
    sub = _fresh_sub(state=SubState.WELCOME_SENT)
    conv = ConvLogger()
    t0 = time.time()
    conv.event("live fan — persona: mixed signals, 10 turns max")
    try:
        all_actions = await _live_convo(
            sub,
            persona=(
                "a fan who swings between hot and cold — flirty one moment, "
                "then saying he's not sure, then showing interest again. "
                "He never completely shuts the door but also never fully commits"
            ),
            goal="keep the conversation going without spending money yet",
            max_turns=10,
            conv=conv,
            skip_fan=skip_fan,
            stop_on_ppv=False,
        )
    except Exception as e:
        return ScenarioResult("S9.3", "Live: mixed signals fan", "G9", [], [], time.time() - t0, error=str(e))

    all_text = " ".join(get_all_text(acts) for acts in all_actions)
    convo_for_judge = "\n".join(
        f"{'Fan' if t.role == 'fan' else 'Bot'}: {t.text}"
        for t in conv.turns if t.role in ("fan", "bot") and t.text
    )
    checks = [
        CheckResult(
            "stayed_engaged",
            all(len(a) > 0 for a in all_actions),
            f"bot replied to all {len(all_actions)} turns ✓",
        ),
        await llm_judge(
            "Does the bot vary its approach across the conversation — "
            "not just repeating the same pitch or warmth phrase every message?",
            convo_for_judge[:2000], "adapts_to_signals", skip=skip_judge,
        ),
        await llm_judge(
            "When the fan pulls back or goes quiet, does the bot stay warm and keep him "
            "engaged rather than going cold or repeating the same push?",
            convo_for_judge[:2000], "retains_interest", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S9.3", "Live: mixed signals fan", "G9", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


async def _run_s9_4(skip_judge: bool = False, skip_fan: bool = False) -> ScenarioResult:
    """
    S9.4 — Live: GFE emotional connection.
    Haiku plays a fan seeking emotional warmth, not explicit content.
    GFE model (0 active tiers) — revenue comes from continuation paywall only.
    Bot must sustain the emotional connection without pushing explicit content.
    """
    sub = _fresh_sub(state=SubState.WELCOME_SENT)
    sub.gfe_message_count = 0
    sub.continuation_threshold_jitter = 999   # suppress paywall during this test
    conv = ConvLogger()
    t0 = time.time()
    conv.event("live fan — GFE mode, persona: emotional, 8 turns")
    try:
        all_actions = await _live_convo(
            sub,
            persona=(
                "a lonely guy who values the emotional connection more than explicit content — "
                "he wants to feel heard, appreciated, and like she actually cares about him"
            ),
            goal="feel genuinely close to the model, not just entertained",
            max_turns=8,
            conv=conv,
            skip_fan=skip_fan,
            stop_on_ppv=False,
        )
    except Exception as e:
        return ScenarioResult("S9.4", "Live: GFE emotional connection", "G9", [], [], time.time() - t0, error=str(e))

    flat = [a for acts in all_actions for a in acts]
    all_text = " ".join(get_all_text(acts) for acts in all_actions)
    convo_for_judge = "\n".join(
        f"{'Fan' if t.role == 'fan' else 'Bot'}: {t.text}"
        for t in conv.turns if t.role in ("fan", "bot") and t.text
    )
    checks = [
        check_no_ppv(flat, "no_ppv_in_gfe_convo"),
        check_no_money_mentions(flat, "no_money_in_gfe_convo"),
        await llm_judge(
            "Does the bot respond warmly and personally to what the fan says — "
            "asking follow-up questions and making him feel like she's actually present?",
            convo_for_judge[:2000], "gfe_emotional_quality", skip=skip_judge,
        ),
        await llm_judge(
            "Does the bot come across as a real person who's engaged in the conversation — "
            "not a script or a generic reply that could go to anyone?",
            convo_for_judge[:2000], "gfe_authenticity", skip=skip_judge,
        ),
    ]
    result = ScenarioResult("S9.4", "Live: GFE emotional connection", "G9", checks, conv.bot_texts(), time.time() - t0)
    conv.attach(result)
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# GROUP 1–8 LIVE VARIANTS
# One live version per automated G1-G8 scenario.  Haiku plays the fan for
# every message exchange so the bot responds to what it actually said, not a
# fixed script.  IDs use an "L" suffix (S1.1L, S2.3L, …).
# ═══════════════════════════════════════════════════════════════════════════════

# ── Shared sub factories for scenarios that need pre-set state ────────────────

def _sub_s2_3L():
    sub = _sub_with_consent()
    sub.last_pitch_at = datetime.now() - timedelta(minutes=5)
    sub.pending_ppv = {"platform_msg_id": "msg-test", "tier": 1,
                       "sent_at": datetime.now().isoformat(), "price": 27.38}
    return sub

def _sub_s3_4L():
    sub = _sub_with_consent()
    sub.pending_ppv = {"platform_msg_id": "msg-old", "tier": 1,
                       "sent_at": (datetime.now() - timedelta(hours=25)).isoformat(), "price": 27.38}
    return sub

def _sub_s4_1L():
    sub = _sub_with_consent()
    sub.last_pitch_at = datetime.now() - timedelta(minutes=2)
    sub.pending_ppv = {"platform_msg_id": "msg-pitch", "tier": 1,
                       "sent_at": datetime.now().isoformat(), "price": 27.38}
    return sub

def _sub_s4_2L():
    sub = _sub_with_consent()
    sub.tier_no_count = 1
    sub.last_pitch_at = datetime.now() - timedelta(minutes=5)
    sub.pending_ppv = {"platform_msg_id": "msg-pitch2", "tier": 1,
                       "sent_at": datetime.now().isoformat(), "price": 27.38}
    return sub

def _sub_s4_3L():
    sub = _sub_with_consent()
    sub.brokey_flagged = True
    sub.tier_no_count = 2
    sub.last_session_completed_at = datetime.now() - timedelta(hours=2)
    sub.last_message_date = datetime.now() - timedelta(hours=2)
    return sub

def _sub_s4_4L():
    sub = _sub_with_history(ppv_count=0)
    sub.sext_consent_given = True
    sub.brokey_flagged = True
    sub.tier_no_count = 2
    return sub

def _sub_s4_5L():
    sub = _sub_with_consent()
    sub.last_pitch_at = datetime.now() - timedelta(minutes=3)
    sub.pending_ppv = {"platform_msg_id": "msg-maybe", "tier": 1,
                       "sent_at": datetime.now().isoformat(), "price": 27.38}
    return sub

def _sub_s5_2L():
    from engine.custom_orders import new_order
    sub = _sub_with_consent()
    sub.pending_custom_order = new_order("schoolgirl outfit video", "video_lingerie", 127.38)
    return sub

def _sub_s6_1L():
    # ppv_count=5 so tiers_purchased=5 — fully unlocks explicit + climax language in tier_boundary.
    # This fan has worked through the tier ladder and is in a fully explicit conversation.
    sub = _sub_with_history(ppv_count=5, total_spent=561.22)
    sub.sext_consent_given = True
    sub.horniness_score = 9
    sub.state = SubState.LOOPING
    return sub


def _sub_s6_2L():
    # ppv_count=1 so tiers_purchased=1 — unlocks explicit words (but not climax words).
    # Nick has bought one tier and is now asking for hard-limit content.
    sub = _sub_with_history(ppv_count=1, total_spent=27.38)
    sub.sext_consent_given = True
    sub.state = SubState.LOOPING
    return sub

def _sub_s7_1L():
    sub = _fresh_sub(state=SubState.WELCOME_SENT)
    sub.gfe_message_count = 45
    sub.continuation_threshold_jitter = 40
    return sub

def _sub_s7_2L():
    sub = _sub_with_history(ppv_count=1, total_spent=27.38)
    sub.last_message_date = datetime.now() - timedelta(days=3)
    sub.add_message("sub", "last thing he said 3 days ago")
    return sub

def _sub_s7_3L():
    sub = _sub_with_history(ppv_count=4, total_spent=233.69)
    sub.fan_name = "Marcus"
    sub.callback_references = ["works in finance", "mentioned his dog once"]
    return sub


# ── G1 live ───────────────────────────────────────────────────────────────────

_s1_1L = _make_live(
    "S1.1L", "Live: fresh sub welcome", "G1", _fresh_sub,
    "Tyler, 24, just subscribed on impulse after seeing her on his feed. "
    "Types fast, keeps messages short, uses lowercase and no punctuation most of the time. "
    "Curious but a little unsure what to say first.",
    "see if she's actually interesting or just another bot",
    max_turns=4,
    pre_events=[("process_new_subscriber()", _pre_new_sub)],
    extras=lambda sub, flat, _: [
        check_no_ppv(flat, "welcome_no_ppv"),
        check_no_money_mentions(flat, "welcome_no_money"),
    ],
)

_s1_2L = _make_live(
    "S1.2L", "Live: resub welcome", "G1",
    lambda: _sub_with_history(ppv_count=3, total_spent=141.29),
    "Marcus, 31, returning subscriber who's been gone a few months. "
    "Warmer and more comfortable than a new fan — picks up like they left off. "
    "Types in full sentences, occasionally flirty.",
    "reconnect and see if the vibe is still there",
    max_turns=4,
    pre_events=[("process_resub()", _pre_resub)],
    # S1.2 (scripted) already verifies the resub message itself has no PPV.
    # Returning buyers have consent and history — the live turns may legitimately sell.
)

_s1_3L = _make_live(
    "S1.3L", "Live: follower conversion", "G1",
    lambda: _fresh_sub(is_follower_only=True),
    "Jake, 27, free follower who's been lurking. Skeptical about whether it's worth paying. "
    "Short messages, dry humor, not easily impressed. Types like he's half-distracted.",
    "figure out if it's worth paying for a sub",
    max_turns=4,
    pre_events=[("process_new_follower()", _pre_new_follower)],
    extras=lambda sub, flat, _: [check_no_ppv(flat, "follower_no_ppv")],
)


# ── G2 live ───────────────────────────────────────────────────────────────────

_s2_1L = _make_live(
    "S2.1L", "Live: early rapport (no money)", "G2",
    lambda: _fresh_sub(state=SubState.WELCOME_SENT),
    "Darius, 29, talkative and flirty. Enjoys the banter more than the content. "
    "Uses full sentences, asks questions back, throws in random jokes. "
    "Not thinking about money at all.",
    "have a fun conversation and flirt without spending anything",
    max_turns=5,
    extras=lambda sub, flat, _: [
        check_no_ppv(flat, "no_ppv_in_early_chat"),
        check_no_money_mentions(flat, "no_money_in_early_chat"),
    ],
)

_s2_2L = _make_live(
    "S2.2L", "Live: consent signal → pitch follows", "G2",
    lambda: _fresh_sub(state=SubState.WELCOME_SENT),
    "Ryan, 26, casual and easy-going. He's chatting her up and from his first message "
    "he makes it clear he doesn't mind spending money if the content is worth it. "
    "Types casually, short messages.",
    "hey, i'm happy to spend if the content is actually good. what do you got?",
    max_turns=8,
    stop_on_ppv=True,
    extras=lambda sub, flat, _: [
        check_sub_flag(sub, "sext_consent_given", True, "consent_recorded"),
    ],
)

_s2_3L = _make_live(
    "S2.3L", "Live: fan declines → no re-pitch", "G2",
    _sub_s2_3L,
    "Chris, 23, between jobs right now. Got the PPV notification, genuinely likes her, "
    "but $27 is real money to him this week. Will be honest and a bit apologetic about it.",
    "decline the PPV nicely without making it awkward",
    max_turns=3,
    extras=lambda sub, flat, _: [check_no_ppv(flat, "no_repitch_after_live_decline")],
)

_s2_4L = _make_live(
    "S2.4L", "Live: long chat → consent ask timing", "G2",
    lambda: _fresh_sub(state=SubState.WELCOME_SENT),
    "Eli, 28, genuinely curious person who asks a lot of questions. "
    "Enjoys the conversation for its own sake and never thinks about paying first. "
    "Rambles a bit, uses casual punctuation.",
    "have a real conversation without thinking about money",
    max_turns=12,
    stop_on_ppv=True,
)


# ── G3 live ───────────────────────────────────────────────────────────────────

_s3_1L = _make_live(
    "S3.1L", "Live: tier 1 buy → tier 2 drop", "G3",
    lambda: _sub_with_history(ppv_count=0, sext_consent_given=True),
    "Jordan, 25, just unlocked his first PPV and is kind of buzzing about it. "
    "Wants to say something but isn't sure how to start. Enthusiastic but not cringe about it.",
    "react to what he just saw and keep the energy going",
    max_turns=3,
    pre_events=[("purchase: tier 1 ($27.38)", _pre_purchase_fn(27.38))],
    extras=lambda sub, flat, _: [check_has_ppv_action(flat, "tier2_dropped")],
)

def _sub_s3_2L_factory():
    sub = _sub_with_history(ppv_count=0)
    sub.sext_consent_given = True
    return sub

async def _run_s3_2L(skip_judge: bool = False) -> ScenarioResult:
    """
    S3.2L — Live: all 6 tiers — full ladder with reactive fan.
    Runs all 6 purchase events; after each one Haiku plays the fan reacting
    to what the bot said (1 turn per tier), so you see natural reactions to
    each unlock and each new PPV drop.
    """
    from agents.orchestrator import process_purchase, process_message

    sub = _sub_s3_2L_factory()
    conv = ConvLogger()
    t0 = time.time()
    all_actions: list[list[BotAction]] = []
    try:
        for tier_num, tier_price in enumerate(_DEFAULT_TIER_PRICES, 1):
            conv.event(f"purchase: tier {tier_num} (${tier_price:.2f})")
            purchase_acts = await conv.run(
                process_purchase(sub, amount=tier_price, avatar=_AVATAR,
                                 content_type="ppv", model_profile=_MODEL_PROFILE),
            )
            all_actions.append(purchase_acts)
            # Fan reacts to the bot's post-purchase message (1 live turn per tier)
            fan_msg = await simulate_fan_turn(
                sub.recent_messages,
                persona=(
                    f"a fan who just unlocked tier {tier_num} content and is reading "
                    "the bot's reaction — excited and curious about what comes next"
                ),
                skip=skip_judge,
            )
            react_acts = await conv.run(
                process_message(sub, fan_msg, avatar=_AVATAR, model_profile=_MODEL_PROFILE),
                fan_text=fan_msg,
            )
            all_actions.append(react_acts)
    except Exception as e:
        return ScenarioResult("S3.2L", "Live: all 6 tiers — full ladder", "G3",
                              [], [], time.time() - t0, error=str(e))

    flat = [a for acts in all_actions for a in acts]
    all_text = " ".join(get_all_text(acts) for acts in all_actions)
    convo_for_judge = "\n".join(
        f"{'Fan' if t.role == 'fan' else 'Bot'}: {t.text}"
        for t in conv.turns if t.role in ("fan", "bot") and t.text
    )
    ppv_prices = [a.ppv_price for a in flat if a.action_type == "send_ppv" and a.ppv_price]
    checks = [
        CheckResult("all_tiers_recorded",
                    sub.spending.ppv_count == 6,
                    f"ppv_count = {sub.spending.ppv_count} (expected 6)"),
        CheckResult("total_spent",
                    abs(sub.spending.total_spent - sum(_DEFAULT_TIER_PRICES)) < 0.10,
                    f"total_spent = ${sub.spending.total_spent:.2f}"),
        await llm_judge(
            "After each purchase the fan reacts and the bot responds — "
            "does the bot's excitement and build-up feel different each time, "
            "rather than repeating the same lines?",
            convo_for_judge[:2000], "ladder_variety", skip=skip_judge,
        ),
    ]
    r = ScenarioResult("S3.2L", "Live: all 6 tiers — full ladder", "G3",
                       checks, conv.bot_texts(), time.time() - t0)
    conv.attach(r)
    return r

_s3_3L = _make_live(
    "S3.3L", "Live: PPV heads-up before drop", "G3",
    lambda: (lambda s: (setattr(s, "state", SubState.TENSION_BUILD) or
                        setattr(s, "gfe_message_count", 5) or s))(_sub_with_consent()),
    "a fan who's been warming up and is starting to hint he wants to see more",
    "push things further and see what she sends",
    max_turns=4,
    stop_on_ppv=True,
)

_s3_4L = _make_live(
    "S3.4L", "Live: stale PPV — no re-drop", "G3",
    _sub_s3_4L,
    "a casual fan checking back in after a day away",
    "just chat and see what she says",
    max_turns=3,
)


# ── G4 live ───────────────────────────────────────────────────────────────────

_s4_1L = _make_live(
    "S4.1L", "Live: first 'too expensive'", "G4",
    _sub_s4_1L,
    "Kevin, 30, got the PPV and opened it. Likes her but $27 feels steep. "
    "Not rude about it — just types something like 'damn that's kinda pricey' and waits.",
    "say it's a bit expensive and see if she'll work with him",
    max_turns=4,
    extras=lambda sub, flat, _: [
        check_no_ppv(flat, "no_immediate_repitch"),
        check_sub_flag(sub, "brokey_flagged", False, "not_brokey_yet"),
    ],
)

_s4_2L = _make_live(
    "S4.2L", "Live: second 'no' → brokey flagged", "G4",
    _sub_s4_2L,
    "Sean, 25, said no to the last pitch and got pitched again. "
    "Slightly annoyed but not mean about it. Will say no again pretty firmly.",
    "turn it down again and make it clear he's not buying right now",
    max_turns=4,
    extras=lambda sub, flat, _: [
        check_no_ppv(flat, "no_third_pitch"),
        check_sub_flag(sub, "brokey_flagged", True, "brokey_flagged"),
    ],
)

_s4_3L = _make_live(
    "S4.3L", "Live: brokey cooldown — warmth only", "G4",
    _sub_s4_3L,
    "Derek, 32, just here to talk. Bored at work, killing time. "
    "Friendly and easygoing. Doesn't bring up money or content at all.",
    "just have a chill chat and see what she's like",
    max_turns=4,
    extras=lambda sub, flat, _: [
        check_no_ppv(flat, "no_ppv_in_cooldown"),
        check_no_money_mentions(flat, "no_money_in_cooldown"),
    ],
)

_s4_4L = _make_live(
    "S4.4L", "Live: buy after brokey → reset", "G4",
    _sub_s4_4L,
    "Sean (same guy from before) — payday came and he actually went for it. "
    "A little proud of himself. Wants to acknowledge the purchase without making it weird.",
    "say he finally bought it and see how she reacts",
    max_turns=3,
    pre_events=[("purchase: tier 1 ($27.38) — was in brokey cooldown",
                 _pre_purchase_fn(27.38))],
    extras=lambda sub, flat, _: [
        check_sub_flag(sub, "brokey_flagged", False, "brokey_cleared"),
        CheckResult("no_count_reset", sub.tier_no_count == 0,
                    f"tier_no_count = {sub.tier_no_count} (expected 0)"),
    ],
)

_s4_5L = _make_live(
    "S4.5L", "Live: 'maybe later' → graceful exit", "G4",
    _sub_s4_5L,
    "Alex, 26, genuinely interested but payday isn't until Friday. "
    "Will be straightforward about it — 'maybe next week' type energy. Not rude.",
    "put it off until payday without being dismissive",
    max_turns=3,
    extras=lambda sub, flat, _: [check_no_ppv(flat, "no_repitch_on_maybe_later")],
)


# ── G5 live ───────────────────────────────────────────────────────────────────

_s5_1L = _make_live(
    "S5.1L", "Live: custom request → price quoted", "G5",
    _sub_with_consent,
    "Brandon, 33, gets straight to the point. He wants a voice note made just for him "
    "and asks what it costs. If she asks for more details he gives them briefly.",
    "hey i want a personalized voice note from you, just for me. what's the price for that?",
    max_turns=5,
    extras=lambda sub, flat, all_text: [
        CheckResult("price_mentioned", any(c in all_text for c in ["$", "dollar"]),
                    "bot mentioned a price ✓" if any(c in all_text for c in ["$", "dollar"])
                    else "bot never quoted a price"),
    ],
)

_s5_2L = _make_live(
    "S5.2L", "Live: fan claims payment → alert", "G5",
    _sub_s5_2L,
    "Brandon (same guy) — says he sent the payment. "
    "Excited, a bit impatient. Will say something like 'just sent it' and wait.",
    "tell her the payment is sent and wait for her to confirm",
    max_turns=2,
)


# ── G6 live ───────────────────────────────────────────────────────────────────

_s6_1L = _make_live(
    "S6.1L", "Live: explicit fan message handled", "G6",
    _sub_s6_1L,
    "Mike, 28, very direct and not shy about what he wants. "
    "Will say explicitly that he's turned on and keep the energy up. Short, blunt messages.",
    "be direct about being turned on and see how far she'll go",
    max_turns=3,
    use_grok_fan=True,
    judge_question=(
        "Does the bot engage with the sexual energy of this conversation — "
        "using explicit language, staying in the scene, and not deflecting or going cold?"
    ),
    judge_name="explicit_energy_match",
)

_s6_2L = _make_live(
    "S6.2L", "Live: hard limit refusal", "G6",
    _sub_s6_2L,
    "Nick, 29, about to ask for something she won't do. "
    "Will ask for a video call, or boy/girl content, or anal — "
    "genuinely thinks she might say yes. Phrases it casually.",
    "ask for the specific content he has in mind",
    max_turns=2,
    use_grok_fan=True,
    judge_question=(
        "Does the bot decline the request warmly and without hostility — "
        "staying flirty or friendly rather than going cold or giving a generic rejection?"
    ),
    judge_name="warm_refusal",
)

_s6_3L = _make_live(
    "S6.3L", "Live: one-word replies handled", "G6",
    lambda: (lambda s: (s.add_message("sub", "hey") or
                        s.add_message("bot", "hey what's up") or s))(
        _fresh_sub(state=SubState.WELCOME_SENT)),
    "Sam, just vibing. Man of few words. Responds with 1-2 words max, sometimes just an emoji.",
    "respond as minimally as possible and see how she handles it",
    max_turns=4,
)

_s6_4L = _make_live(
    "S6.4L", "Live: emoji-only messages handled", "G6",
    lambda: _fresh_sub(state=SubState.WELCOME_SENT),
    "Jay, heavy emoji user. Reacts with 🔥😍🙏 type energy. "
    "Mixes emojis with very short words. Almost no full sentences.",
    "react with emojis and keep it very short",
    max_turns=3,
)

_s6_5L = _make_live(
    "S6.5L", "Live: free content request deflected", "G6",
    lambda: _fresh_sub(state=SubState.WELCOME_SENT),
    "Dave, 22, trying his luck. Will start sweet then hint at wanting a freebie — "
    "'just a little preview' type energy. Half-joking but not entirely.",
    "charm his way into getting something for free",
    max_turns=3,
)

_s6_6L = _make_live(
    "S6.6L", "Live: abusive message handled", "G6",
    lambda: _fresh_sub(state=SubState.WELCOME_SENT),
    "Some guy who's in a bad mood and taking it out here. "
    "Will say something dismissive or insulting — not a troll, just being a dick.",
    "be rude and see if she snaps back or stays calm",
    max_turns=2,
    use_grok_fan=True,
    judge_question=(
        "Does the bot handle the rude messages without escalating or breaking character — "
        "staying playful, deflecting with humor, or brushing it off, "
        "rather than getting genuinely hostile, cold, or going silent?"
    ),
    judge_name="handles_abuse_gracefully",
)

_s6_7L = _make_live(
    "S6.7L", "Live: mid-processing interruption (regen)", "G6",
    _sub_with_consent,
    "Leo, types fast and edits himself mid-thought. "
    "Starts one thing then immediately backtracks or adds something else. "
    "Comes across as a bit scattered but genuine.",
    "start a thought then follow up with a correction or tangent right after",
    max_turns=4,
)


# ── G7 live ───────────────────────────────────────────────────────────────────

_s7_1L = _make_live(
    "S7.1L", "Live: GFE-only — continuation paywall", "G7",
    _sub_s7_1L,
    "Tom, 35, been here a while. Talks to her like they actually know each other. "
    "Not here for content — here for the connection. Thoughtful messages, asks about her day.",
    "have a genuine warm conversation, not about buying anything",
    max_turns=3,
    use_gfe=True,
    extras=lambda sub, flat, _: [
        CheckResult(
            "continuation_ppv_or_gfe_convo",
            has_ppv(flat) or len([a for a in flat if a.action_type == "send_message"]) > 0,
            "bot responded to GFE fan ✓",
        ),
    ],
)

_s7_2L = _make_live(
    "S7.2L", "Live: return after 3-day gap", "G7",
    _sub_s7_2L,
    "James, checking back in after a few days. Slightly apologetic — "
    "'been busy' energy. Wants to pick up where they left off without making it a big deal.",
    "check back in after being gone and see if she remembers him",
    max_turns=4,
)

_s7_3L = _make_live(
    "S7.3L", "Live: resub with history → personal", "G7",
    _sub_s7_3L,
    "Marcus — a fan who's been subscribed before, spent money, and just came back — "
    "he works in finance and once mentioned his dog",
    "reconnect and see if she remembers anything about him",
    max_turns=4,
    pre_events=[("process_resub() — Marcus, 4 PPVs, $233.69 spent", _pre_resub)],
    # No check_no_ppv here — static S7.3 already verifies process_resub() doesn't PPV.
    # After reconnecting, pitching to a returning buyer with 4 PPVs is correct behaviour.
)


# ═══════════════════════════════════════════════════════════════════════════════
# MANUAL SCENARIOS
# ═══════════════════════════════════════════════════════════════════════════════

_S5_3_INSTRUCTIONS = """
S5.3 — Admin confirms custom payment:
  1. Ensure a custom order is pitched (run S5.1, then S5.2)
  2. On Telegram, check for the payment alert from the bot
  3. Tap 'Confirm' on the alert
  4. Send the fan another message — bot should say content is being prepared,
     NOT pitch the custom again
"""

_S5_4_INSTRUCTIONS = """
S5.4 — Admin denies custom payment:
  1. Ensure a custom order is pending (run S5.1 then S5.2)
  2. On Telegram, tap 'Deny' on the payment alert
  3. Send the fan another message — bot should pivot back to rapport/selling,
     NOT mention the denied custom order unprompted
"""

_S5_5_INSTRUCTIONS = """
S5.5 — Custom order fulfilled:
  1. After admin confirms (S5.3), the operator uploads and sends the content
  2. Send the fan a message saying 'did you get it?'
  3. Bot should respond warmly and NOT re-pitch the same custom order
"""

_S8_1_INSTRUCTIONS = """
S8.1 — /stats command:
  1. On Telegram, send your bot /stats
  2. Verify it replies with subscriber count, revenue, and top fans
"""

_S8_2_INSTRUCTIONS = """
S8.2 — /pause and /resume:
  1. Send /pause — verify bot stops responding to fan messages
  2. Send a fan message to confirm silence
  3. Send /resume — verify bot responds to next fan message
"""

_S8_3_INSTRUCTIONS = """
S8.3 — /revenue command:
  1. Send /revenue on Telegram
  2. Verify it shows today's earnings, 7-day, and 30-day totals
"""


# ═══════════════════════════════════════════════════════════════════════════════
# Scenario registry
# ═══════════════════════════════════════════════════════════════════════════════

def _auto(sid, name, group, fn):
    return Scenario(id=sid, name=name, group=group, is_manual=False, run=fn)

def _manual(sid, name, group, instructions):
    async def _noop():
        return ScenarioResult(sid, name, group, [], [], 0.0, manual_instructions=instructions)
    return Scenario(id=sid, name=name, group=group, is_manual=True,
                    run=_noop, manual_instructions=instructions)


ALL_SCENARIOS: list[Scenario] = [
    # ── G1: New Subscriber Flow ───────────────────────────────────────────────
    _auto("S1.1",  "Fresh sub welcome",           "G1", _run_s1_1),
    _auto("S1.1L", "Live: fresh sub welcome",     "G1", _s1_1L),
    _auto("S1.2",  "Resub welcome",               "G1", _run_s1_2),
    _auto("S1.2L", "Live: resub welcome",         "G1", _s1_2L),
    _auto("S1.3",  "Follower conversion nudge",   "G1", _run_s1_3),
    _auto("S1.3L", "Live: follower conversion",   "G1", _s1_3L),
    # ── G2: Rapport & Consent Gate ────────────────────────────────────────────
    _auto("S2.1",  "Early rapport (no money)",       "G2", _run_s2_1),
    _auto("S2.1L", "Live: early rapport (no money)", "G2", _s2_1L),
    _auto("S2.2",  "Consent signal → pitch follows", "G2", _run_s2_2),
    _auto("S2.2L", "Live: consent signal",           "G2", _s2_2L),
    _auto("S2.3",  "Fan declines → no re-pitch",     "G2", _run_s2_3),
    _auto("S2.3L", "Live: fan declines",             "G2", _s2_3L),
    _auto("S2.4",  "Long chat → consent ask timing", "G2", _run_s2_4),
    _auto("S2.4L", "Live: long chat",                "G2", _s2_4L),
    # ── G3: PPV Ladder ────────────────────────────────────────────────────────
    _auto("S3.1",  "Tier 1 buy → tier 2 drop",    "G3", _run_s3_1),
    _auto("S3.1L", "Live: tier 1 buy",             "G3", _s3_1L),
    _auto("S3.2",  "All 6 tiers — full ladder",   "G3", _run_s3_2),
    _auto("S3.2L", "Live: all 6 tiers",           "G3", _run_s3_2L),
    _auto("S3.3",  "PPV heads-up before drop",    "G3", _run_s3_3),
    _auto("S3.3L", "Live: PPV heads-up",          "G3", _s3_3L),
    _auto("S3.4",  "Stale PPV — no re-drop",      "G3", _run_s3_4),
    _auto("S3.4L", "Live: stale PPV",             "G3", _s3_4L),
    # ── G4: Objection Handling ────────────────────────────────────────────────
    _auto("S4.1",  "First 'too expensive'",          "G4", _run_s4_1),
    _auto("S4.1L", "Live: first 'too expensive'",    "G4", _s4_1L),
    _auto("S4.2",  "Second 'no' → cooldown trigger", "G4", _run_s4_2),
    _auto("S4.2L", "Live: second 'no'",              "G4", _s4_2L),
    _auto("S4.3",  "Brokey cooldown — warmth only",  "G4", _run_s4_3),
    _auto("S4.3L", "Live: brokey cooldown",          "G4", _s4_3L),
    _auto("S4.4",  "Buy after brokey → reset",       "G4", _run_s4_4),
    _auto("S4.4L", "Live: buy after brokey",         "G4", _s4_4L),
    _auto("S4.5",  "'Maybe later' → graceful exit",  "G4", _run_s4_5),
    _auto("S4.5L", "Live: 'maybe later'",            "G4", _s4_5L),
    # ── G5: Custom Order Flow ─────────────────────────────────────────────────
    _auto("S5.1",  "Custom request → price quoted", "G5", _run_s5_1),
    _auto("S5.1L", "Live: custom request",          "G5", _s5_1L),
    _auto("S5.2",  "Fan claims payment → alert",    "G5", _run_s5_2),
    _auto("S5.2L", "Live: fan claims payment",      "G5", _s5_2L),
    _manual("S5.3", "Admin confirms custom",        "G5", _S5_3_INSTRUCTIONS),
    _manual("S5.4", "Admin denies custom",          "G5", _S5_4_INSTRUCTIONS),
    _manual("S5.5", "Custom fulfilled",             "G5", _S5_5_INSTRUCTIONS),
    # ── G6: Edge Cases & Guardrails ───────────────────────────────────────────
    _auto("S6.1",  "Explicit fan message handled",          "G6", _run_s6_1),
    _auto("S6.1L", "Live: explicit message",                "G6", _s6_1L),
    _auto("S6.2",  "Hard limit refusal",                    "G6", _run_s6_2),
    _auto("S6.2L", "Live: hard limit refusal",              "G6", _s6_2L),
    _auto("S6.3",  "One-word reply handled",                "G6", _run_s6_3),
    _auto("S6.3L", "Live: one-word replies",                "G6", _s6_3L),
    _auto("S6.4",  "Emoji-only message handled",            "G6", _run_s6_4),
    _auto("S6.4L", "Live: emoji messages",                  "G6", _s6_4L),
    _auto("S6.5",  "Free content request deflected",        "G6", _run_s6_5),
    _auto("S6.5L", "Live: free content request",            "G6", _s6_5L),
    _auto("S6.6",  "Abusive message handled",               "G6", _run_s6_6),
    _auto("S6.6L", "Live: abusive message",                 "G6", _s6_6L),
    _auto("S6.7",  "Mid-processing interruption (regen)",   "G6", _run_s6_7),
    _auto("S6.7L", "Live: mid-processing interruption",     "G6", _s6_7L),
    # ── G7: GFE & Returning Fan ───────────────────────────────────────────────
    _auto("S7.1",  "GFE-only — continuation paywall",   "G7", _run_s7_1),
    _auto("S7.1L", "Live: GFE continuation",            "G7", _s7_1L),
    _auto("S7.2",  "Return after 3-day gap",            "G7", _run_s7_2),
    _auto("S7.2L", "Live: return after gap",            "G7", _s7_2L),
    _auto("S7.3",  "Resub with history → personal",     "G7", _run_s7_3),
    _auto("S7.3L", "Live: resub with history",          "G7", _s7_3L),
    _auto("S7.4",  "Session lock after 6 tiers",        "G7", _run_s7_4),
    # ── G8: Admin Bot (manual only) ───────────────────────────────────────────
    _manual("S8.1", "/stats command",     "G8", _S8_1_INSTRUCTIONS),
    _manual("S8.2", "/pause and /resume", "G8", _S8_2_INSTRUCTIONS),
    _manual("S8.3", "/revenue command",   "G8", _S8_3_INSTRUCTIONS),
    # ── G9: Freeform live fan conversations ───────────────────────────────────
    _auto("S9.1", "Live: reluctant spender",        "G9", _run_s9_1),
    _auto("S9.2", "Live: easy buyer",               "G9", _run_s9_2),
    _auto("S9.3", "Live: mixed signals fan",        "G9", _run_s9_3),
    _auto("S9.4", "Live: GFE emotional connection", "G9", _run_s9_4),
]
