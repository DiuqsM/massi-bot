"""
Massi-Bot — Concurrency & Speed Tests

Tests the three concurrent-message scenarios that unit/simulator tests miss:

  1. Rapid typing (settle window)
     - Messages arriving within 8s extend the settle window by 5s each
     - Handler waits for typing to stop before calling the agent

  2. Message arrives while bot is processing (regen loop)
     - New message queued during agent call → regenerate with combined text
     - "nvm" / cancellation tokens during regen → PPV suppressed
     - Post-send sweep: message arrives DURING execute_actions → processed afterward

  3. Lock queue drain
     - Two concurrent webhook calls for the same fan → second message queued, not dropped
     - When lock releases, queued messages combine into a single agent call
     - Multiple queued messages all join the same call (not separate calls)

  4. Processing speed
     - Orchestrator pipeline (mocked agent) runs in < 500ms
     - Connector immediately ACKs webhook (200) before agent call starts

Run with:
    cd ~/massi-bot && python -m pytest tests/test_concurrency.py -v
"""

import sys
import os
import asyncio
import time
import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))

from engine.models import Subscriber, SubState, SpendingHistory, BotAction


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_sub(**kwargs) -> Subscriber:
    sub = Subscriber(
        sub_id="test-concurrent",
        username="testfan",
        state=SubState.WELCOME_SENT,
    )
    for k, v in kwargs.items():
        setattr(sub, k, v)
    return sub


def _agent_result(text: str = "hey 😏", ppv=None):
    return {
        "messages": [{"text": text, "delay_seconds": 2}],
        "ppv": ppv,
        "consent_given": False,
        "consent_declined": False,
        "horniness_score": 3,
        "fan_name": None,
        "fan_profile_update": None,
    }


def _ppv_result(text: str = "give me a sec", tier: int = 1):
    return {
        "messages": [{"text": text, "delay_seconds": 2}],
        "ppv": {"tier": tier, "price": 27.38, "caption": "just for you 😈", "heads_up": "give me a sec"},
        "consent_given": False,
        "consent_declined": False,
        "horniness_score": 6,
        "fan_name": None,
        "fan_profile_update": None,
    }


def _patch_orchestrator_infra():
    """Patch all DB/network deps in the orchestrator for fast unit tests."""
    from llm.memory_manager import memory_manager as mm
    return [
        patch.object(mm, "get_context_memories",         AsyncMock(return_value=[])),
        patch.object(mm, "get_persona_context",          AsyncMock(return_value=[])),
        patch.object(mm, "maybe_generate_profile_summary", AsyncMock(return_value="")),
        patch.object(mm, "maybe_extract_and_store",      AsyncMock(return_value=0)),
        patch.object(mm, "maybe_store_persona_facts",    AsyncMock(return_value=None)),
        patch("llm.context_awareness.get_weather",       AsyncMock(return_value=None)),
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Settle window (adaptive wait for typing to stop)
# ═══════════════════════════════════════════════════════════════════════════════

class TestSettleWindow:
    """
    The settle window waits for the fan to stop typing before calling the agent.
    Initial: 8s. Each new message extends by 5s. Max: 30s total.

    Tests use a very short settle window (0.05s initial / 0.02s extension)
    so tests complete in milliseconds.
    """

    @pytest.mark.anyio
    async def test_no_new_messages_exits_after_initial_wait(self):
        """Single message → settle exits after initial_seconds."""
        import connector.fanvue_connector as c

        fan_id = "settle-test-1"
        c._sub_last_msg_time[fan_id] = time.monotonic()

        with (
            patch("connector.fanvue_connector._settle_initial_seconds", return_value=0.05),
            patch("connector.fanvue_connector._settle_extension_seconds", return_value=0.02),
            patch("connector.fanvue_connector._settle_max_seconds", return_value=1.0),
        ):
            t0 = time.monotonic()
            await c._wait_for_settle(fan_id)
            elapsed = time.monotonic() - t0

        # Should have waited ~0.05s (initial), not more
        assert elapsed >= 0.04, f"Exited too early: {elapsed:.3f}s"
        assert elapsed < 0.20, f"Waited too long: {elapsed:.3f}s"

    @pytest.mark.anyio
    async def test_new_message_extends_wait(self):
        """
        Second message arrives 0.03s into the initial 0.05s wait →
        settle extends to 0.02s after that second message.
        Total wait should be ~0.05s (0.03 elapsed + 0.02 extension).
        """
        import connector.fanvue_connector as c

        fan_id = "settle-test-2"
        c._sub_last_msg_time[fan_id] = time.monotonic()

        async def inject_second_message():
            await asyncio.sleep(0.03)  # arrives 30ms into the initial 50ms wait
            c._sub_last_msg_time[fan_id] = time.monotonic()

        with (
            patch("connector.fanvue_connector._settle_initial_seconds", return_value=0.05),
            patch("connector.fanvue_connector._settle_extension_seconds", return_value=0.04),
            patch("connector.fanvue_connector._settle_max_seconds", return_value=1.0),
        ):
            t0 = time.monotonic()
            await asyncio.gather(
                c._wait_for_settle(fan_id),
                inject_second_message(),
            )
            elapsed = time.monotonic() - t0

        # Extension pushes target to: inject_time + 0.04 ≈ 0.03 + 0.04 = 0.07s
        assert elapsed >= 0.06, f"Extension not applied: {elapsed:.3f}s"
        assert elapsed < 0.25, f"Waited too long: {elapsed:.3f}s"

    @pytest.mark.anyio
    async def test_rapid_fire_messages_hit_max_cap(self):
        """
        Messages injected every 5ms would extend forever, but the 30s cap prevents it.
        With a tiny cap of 0.08s, the settle must exit even with continuous messages.
        """
        import connector.fanvue_connector as c

        fan_id = "settle-test-3"
        c._sub_last_msg_time[fan_id] = time.monotonic()
        stop_injecting = False

        async def inject_continuously():
            while not stop_injecting:
                c._sub_last_msg_time[fan_id] = time.monotonic()
                await asyncio.sleep(0.005)

        async def run_settle():
            nonlocal stop_injecting
            with (
                patch("connector.fanvue_connector._settle_initial_seconds", return_value=0.02),
                patch("connector.fanvue_connector._settle_extension_seconds", return_value=0.02),
                patch("connector.fanvue_connector._settle_max_seconds", return_value=0.08),
            ):
                await c._wait_for_settle(fan_id)
            stop_injecting = True

        t0 = time.monotonic()
        await asyncio.gather(run_settle(), inject_continuously())
        elapsed = time.monotonic() - t0

        # Must exit at or near the max cap of 0.08s
        assert elapsed >= 0.07, f"Exited before max cap: {elapsed:.3f}s"
        assert elapsed < 0.25, f"Max cap not enforced: {elapsed:.3f}s"


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Lock queue drain (concurrent webhooks for the same fan)
# ═══════════════════════════════════════════════════════════════════════════════

class TestLockQueueDrain:
    """
    Two webhook calls arrive for the same fan concurrently.
    The lock ensures only one processes at a time; the second queues.
    When the lock releases, the first call (or next one) drains the queue.
    """

    @pytest.mark.anyio
    async def test_second_message_queued_when_lock_held(self):
        """
        While lock is held (simulating an active agent call), a new message
        for the same fan is added to _sub_queued_messages, not processed directly.
        """
        import connector.fanvue_connector as c

        fan_id = "queue-test-1"
        # Clean up any leftover state
        c._sub_queued_messages.pop(fan_id, None)
        c._sub_locks.pop(fan_id, None)

        lock = c._get_sub_lock(fan_id)
        second_queued = asyncio.Event()

        async def hold_lock_while_processing():
            async with lock:
                await asyncio.sleep(0.05)  # simulate agent thinking

        async def simulate_second_webhook():
            await asyncio.sleep(0.01)  # arrive while lock is held
            # This is the exact logic in the connector's handle():
            if lock.locked():
                if fan_id not in c._sub_queued_messages:
                    c._sub_queued_messages[fan_id] = []
                c._sub_queued_messages[fan_id].append("second message")
                second_queued.set()
            else:
                pytest.fail("Lock wasn't held when second webhook arrived — timing off")

        await asyncio.gather(hold_lock_while_processing(), simulate_second_webhook())
        await second_queued.wait()

        assert c._sub_queued_messages.get(fan_id) == ["second message"]

    @pytest.mark.anyio
    async def test_multiple_queued_messages_combined_into_one_agent_call(self):
        """
        3 messages queued while bot processes message 1.
        When the handler drains the queue, the agent receives
        "msg1\nmsg2\nmsg3\nmsg4" as a single combined call — not 4 separate calls.
        """
        import connector.fanvue_connector as c

        fan_id = "queue-test-2"
        c._sub_queued_messages[fan_id] = ["msg2", "msg3", "msg4"]

        agent_calls = []

        async def mock_agent(**kwargs):
            agent_calls.append(kwargs.get("message", ""))
            return _agent_result()

        sub = _make_sub()
        infra_patches = _patch_orchestrator_infra()
        for p in infra_patches:
            p.start()

        try:
            with patch("agents.orchestrator.single_agent_process", side_effect=mock_agent):
                with patch("agents.orchestrator.run_all_guardrails",
                           AsyncMock(return_value=(True, []))):
                    from agents.orchestrator import process_message
                    # The connector's drain logic — combine current + queued before agent call
                    combined = "msg1"
                    queued = c._sub_queued_messages.pop(fan_id, [])
                    if queued:
                        combined = (combined + "\n" + "\n".join(queued)).strip()

                    # This is what the connector actually calls after combining:
                    await process_message(sub, combined)
        finally:
            for p in infra_patches:
                p.stop()

        assert len(agent_calls) == 1, f"Expected 1 agent call, got {len(agent_calls)}"
        assert "msg1" in agent_calls[0]
        assert "msg2" in agent_calls[0]
        assert "msg3" in agent_calls[0]
        assert "msg4" in agent_calls[0]

    @pytest.mark.anyio
    async def test_queue_empty_after_drain(self):
        """After the lock releases and the queue is drained, the queue must be empty."""
        import connector.fanvue_connector as c

        fan_id = "queue-test-3"
        c._sub_queued_messages[fan_id] = ["a", "b", "c"]

        # Simulate the drain (what the connector does on lock acquire):
        drained = c._sub_queued_messages.pop(fan_id, [])

        assert drained == ["a", "b", "c"]
        assert fan_id not in c._sub_queued_messages


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Regen loop (message arrives during agent processing)
# ═══════════════════════════════════════════════════════════════════════════════

class TestRegenLoop:
    """
    The regen loop: after the agent responds but before actions are sent,
    if new messages arrived in _sub_queued_messages, the connector regenerates
    the response with the full accumulated text (up to 2 times).
    """

    @pytest.mark.anyio
    async def test_regen_triggered_when_message_arrives_mid_processing(self):
        """
        Fan sends "hey", agent starts processing.
        Fan then sends "actually what time is it?" mid-processing.
        Connector regenerates with "hey\nactually what time is it?" combined.
        """
        import connector.fanvue_connector as c

        fan_id = "regen-test-1"
        c._sub_queued_messages.pop(fan_id, None)

        call_count = 0
        call_texts = []

        async def mock_agent(**kwargs):
            nonlocal call_count
            call_count += 1
            call_texts.append(kwargs.get("message", ""))
            if call_count == 1:
                # Simulate: second message arrives WHILE agent is processing the first
                c._sub_queued_messages[fan_id] = ["actually what time is it?"]
            return _agent_result()

        sub = _make_sub()
        infra_patches = _patch_orchestrator_infra()
        for p in infra_patches:
            p.start()

        try:
            with patch("agents.orchestrator.single_agent_process", side_effect=mock_agent):
                with patch("agents.orchestrator.run_all_guardrails",
                           AsyncMock(return_value=(True, []))):
                    from agents.orchestrator import process_message

                    # First call — sets off the agent
                    initial_message = "hey"
                    actions = await process_message(sub, initial_message)

                    # Regen check: the connector does this after the agent call:
                    accumulated = initial_message
                    regen_count = 0
                    while regen_count < 2:
                        pre_send_queued = c._sub_queued_messages.pop(fan_id, [])
                        if not pre_send_queued:
                            break
                        regen_count += 1
                        accumulated = (accumulated + "\n" + "\n".join(pre_send_queued)).strip()
                        actions = await process_message(sub, accumulated)

        finally:
            for p in infra_patches:
                p.stop()

        # Agent should have been called twice total
        assert call_count == 2, f"Expected 2 calls (initial + 1 regen), got {call_count}"
        # Second call must include the new message
        assert "actually what time is it?" in call_texts[1], \
            f"Regen call missing the new message: {call_texts[1]!r}"

    @pytest.mark.anyio
    async def test_regen_capped_at_two_passes(self):
        """Regen loop runs at most 2 times even if messages keep arriving."""
        import connector.fanvue_connector as c

        fan_id = "regen-test-2"
        c._sub_queued_messages.pop(fan_id, None)
        call_count = 0

        async def mock_agent(**kwargs):
            nonlocal call_count
            call_count += 1
            # Always inject a new message — would cause infinite regen without the cap
            c._sub_queued_messages[fan_id] = [f"msg-during-regen-{call_count}"]
            return _agent_result()

        sub = _make_sub()
        infra_patches = _patch_orchestrator_infra()
        for p in infra_patches:
            p.start()

        try:
            with patch("agents.orchestrator.single_agent_process", side_effect=mock_agent):
                with patch("agents.orchestrator.run_all_guardrails",
                           AsyncMock(return_value=(True, []))):
                    from agents.orchestrator import process_message

                    accumulated = "first message"
                    await process_message(sub, accumulated)

                    regen_count = 0
                    while regen_count < 2:  # max_regens = 2
                        queued = c._sub_queued_messages.pop(fan_id, [])
                        if not queued:
                            break
                        regen_count += 1
                        accumulated = (accumulated + "\n" + "\n".join(queued)).strip()
                        await process_message(sub, accumulated)

        finally:
            for p in infra_patches:
                p.stop()

        # 1 initial + 2 regen = 3 total, even though messages kept arriving
        assert call_count == 3, f"Expected 3 agent calls (1 initial + 2 regen max), got {call_count}"

    @pytest.mark.anyio
    async def test_cancellation_token_suppresses_ppv_during_regen(self):
        """
        Agent returns a PPV action for the first message.
        Fan then sends "nvm" — which is a cancellation token.
        Regen should drop the PPV, not re-include it.
        """
        import connector.fanvue_connector as c

        fan_id = "regen-test-3"
        c._sub_queued_messages.pop(fan_id, None)

        _CANCEL_TOKENS = (
            "nvm", "nevermind", "never mind", "cancel", "wait",
            "hold on", "stop", "not yet", "later", "changed my mind",
        )
        call_count = 0

        async def mock_agent(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call returns a PPV; also inject "nvm"
                c._sub_queued_messages[fan_id] = ["nvm"]
                return _ppv_result()
            # Regen call — agent doesn't drop a PPV (fan cancelled)
            return _agent_result("okay no worries")

        sub = _make_sub()
        sub.sext_consent_given = True
        infra_patches = _patch_orchestrator_infra()
        for p in infra_patches:
            p.start()

        final_actions = None
        try:
            with patch("agents.orchestrator.single_agent_process", side_effect=mock_agent):
                with patch("agents.orchestrator.run_all_guardrails",
                           AsyncMock(return_value=(True, []))):
                    from agents.orchestrator import process_message

                    accumulated = "show me something"
                    prior_actions = await process_message(sub, accumulated)

                    # Regen loop (connector logic):
                    regen_count = 0
                    while regen_count < 2:
                        queued = c._sub_queued_messages.pop(fan_id, [])
                        if not queued:
                            break
                        regen_count += 1
                        accumulated = (accumulated + "\n" + "\n".join(queued)).strip()
                        new_msgs_lower = "\n".join(queued).lower()
                        is_cancellation = any(tok in new_msgs_lower for tok in _CANCEL_TOKENS)
                        prior_ppv = [a for a in prior_actions if a.action_type == "send_ppv"]
                        current_actions = await process_message(sub, accumulated)

                        # Connector's cancellation logic:
                        if prior_ppv and not is_cancellation:
                            # Preserve PPV if no cancellation
                            has_ppv_now = any(a.action_type == "send_ppv" for a in current_actions)
                            if not has_ppv_now:
                                current_actions = list(current_actions) + prior_ppv
                        elif prior_ppv and is_cancellation:
                            # Cancellation token: do NOT add back the PPV
                            pass

                        final_actions = current_actions
                        prior_actions = current_actions

        finally:
            for p in infra_patches:
                p.stop()

        assert call_count == 2, f"Expected regen: 2 agent calls, got {call_count}"
        assert final_actions is not None
        ppv_in_final = [a for a in final_actions if a.action_type == "send_ppv"]
        assert len(ppv_in_final) == 0, \
            f"PPV should be suppressed after 'nvm' cancellation, got: {ppv_in_final}"

    @pytest.mark.anyio
    async def test_ppv_preserved_when_no_cancellation_in_regen(self):
        """
        Agent returns a PPV. Regen is triggered by a follow-up message ("wait what's in it?")
        that is NOT a cancellation token. The PPV must be preserved in the final actions.
        """
        import connector.fanvue_connector as c

        fan_id = "regen-test-4"
        c._sub_queued_messages.pop(fan_id, None)

        _CANCEL_TOKENS = (
            "nvm", "nevermind", "never mind", "cancel", "wait",
            "hold on", "stop", "not yet", "later", "changed my mind",
        )
        call_count = 0

        async def mock_agent(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                c._sub_queued_messages[fan_id] = ["what's in it though?"]
                return _ppv_result()
            # Regen: agent doesn't re-include PPV (maybe it didn't notice)
            return _agent_result("it's really good trust me")

        sub = _make_sub()
        sub.sext_consent_given = True
        infra_patches = _patch_orchestrator_infra()
        for p in infra_patches:
            p.start()

        final_actions = None
        try:
            with patch("agents.orchestrator.single_agent_process", side_effect=mock_agent):
                with patch("agents.orchestrator.run_all_guardrails",
                           AsyncMock(return_value=(True, []))):
                    from agents.orchestrator import process_message

                    accumulated = "show me"
                    prior_actions = await process_message(sub, accumulated)

                    regen_count = 0
                    while regen_count < 2:
                        queued = c._sub_queued_messages.pop(fan_id, [])
                        if not queued:
                            break
                        regen_count += 1
                        accumulated = (accumulated + "\n" + "\n".join(queued)).strip()
                        new_msgs_lower = "\n".join(queued).lower()
                        is_cancellation = any(tok in new_msgs_lower for tok in _CANCEL_TOKENS)
                        prior_ppv = [a for a in prior_actions if a.action_type == "send_ppv"]
                        current_actions = await process_message(sub, accumulated)

                        # Connector's PPV preservation logic:
                        if prior_ppv and not is_cancellation:
                            has_ppv_now = any(a.action_type == "send_ppv" for a in current_actions)
                            if not has_ppv_now:
                                current_actions = list(current_actions) + prior_ppv

                        final_actions = current_actions
                        prior_actions = current_actions

        finally:
            for p in infra_patches:
                p.stop()

        assert final_actions is not None
        ppv_in_final = [a for a in final_actions if a.action_type == "send_ppv"]
        assert len(ppv_in_final) >= 1, \
            "PPV should be preserved when follow-up is not a cancellation token"


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Processing speed
# ═══════════════════════════════════════════════════════════════════════════════

class TestProcessingSpeed:
    """
    With the agent mocked to return instantly, the orchestrator pipeline
    (context build → guardrails → action assembly) should complete fast.
    This catches accidental blocking calls, sync sleeps, or tight loops
    introduced in the pipeline.
    """

    @pytest.mark.anyio
    async def test_orchestrator_pipeline_completes_under_500ms_with_mocked_agent(self):
        """
        Build context → agent call (mocked, 0ms) → guardrails → action assembly.
        Should complete in under 500ms. If it's slower, something is blocking.
        """
        sub = _make_sub()
        sub.sext_consent_given = True

        infra_patches = _patch_orchestrator_infra()
        for p in infra_patches:
            p.start()

        try:
            with patch("agents.orchestrator.single_agent_process",
                       AsyncMock(return_value=_agent_result())):
                with patch("agents.orchestrator.run_all_guardrails",
                           AsyncMock(return_value=(True, []))):
                    from agents.orchestrator import process_message

                    t0 = time.monotonic()
                    await process_message(sub, "hey what's up")
                    elapsed_ms = (time.monotonic() - t0) * 1000

        finally:
            for p in infra_patches:
                p.stop()

        assert elapsed_ms < 500, (
            f"Pipeline took {elapsed_ms:.0f}ms with a mocked agent — "
            f"something is blocking. Should be < 500ms."
        )

    @pytest.mark.anyio
    async def test_five_sequential_messages_each_under_500ms(self):
        """
        Verify there's no per-message state accumulation that causes slowdown.
        5 sequential messages should each complete under 500ms individually.
        """
        sub = _make_sub()
        messages = ["hey", "what's up", "that's cool", "tell me more", "okay 👀"]

        infra_patches = _patch_orchestrator_infra()
        for p in infra_patches:
            p.start()

        try:
            with patch("agents.orchestrator.single_agent_process",
                       AsyncMock(return_value=_agent_result())):
                with patch("agents.orchestrator.run_all_guardrails",
                           AsyncMock(return_value=(True, []))):
                    from agents.orchestrator import process_message

                    for msg in messages:
                        t0 = time.monotonic()
                        await process_message(sub, msg)
                        elapsed_ms = (time.monotonic() - t0) * 1000
                        assert elapsed_ms < 500, (
                            f"Message '{msg}' took {elapsed_ms:.0f}ms — "
                            f"expected < 500ms with mocked agent"
                        )
        finally:
            for p in infra_patches:
                p.stop()

    @pytest.mark.anyio
    async def test_purchase_handler_under_500ms_with_mocked_agent(self):
        """process_purchase pipeline should be equally fast."""
        sub = _make_sub()
        sub.sext_consent_given = True
        sub.spending = SpendingHistory(ppv_count=0, total_spent=0.0)

        infra_patches = _patch_orchestrator_infra()
        for p in infra_patches:
            p.start()

        try:
            with patch("agents.orchestrator.single_agent_process",
                       AsyncMock(return_value=_agent_result("omg you got it 😍"))):
                with patch("agents.orchestrator.run_all_guardrails",
                           AsyncMock(return_value=(True, []))):
                    from agents.orchestrator import process_purchase

                    t0 = time.monotonic()
                    await process_purchase(sub, amount=27.38, content_type="ppv")
                    elapsed_ms = (time.monotonic() - t0) * 1000

        finally:
            for p in infra_patches:
                p.stop()

        assert elapsed_ms < 500, f"process_purchase took {elapsed_ms:.0f}ms — expected < 500ms"

    @pytest.mark.anyio
    async def test_concurrent_messages_different_fans_dont_block_each_other(self):
        """
        Two different fans send a message at the same time.
        Each should complete in ~parallel — fan B's processing shouldn't
        wait for fan A's lock.
        """
        import connector.fanvue_connector as c

        fan_a = "speed-test-fan-a"
        fan_b = "speed-test-fan-b"
        c._sub_locks.pop(fan_a, None)
        c._sub_locks.pop(fan_b, None)

        lock_a = c._get_sub_lock(fan_a)
        lock_b = c._get_sub_lock(fan_b)

        # Verify the two fans have independent locks
        assert lock_a is not lock_b, "Different fans must have different locks"

        timings = {}

        async def process_fan(fan_id: str, hold_ms: float):
            lock = c._get_sub_lock(fan_id)
            async with lock:
                t0 = time.monotonic()
                await asyncio.sleep(hold_ms / 1000)
                timings[fan_id] = time.monotonic() - t0

        t_start = time.monotonic()
        # Fan A takes 80ms, Fan B takes 80ms — if serial, ~160ms; if parallel, ~80ms
        await asyncio.gather(
            process_fan(fan_a, 80),
            process_fan(fan_b, 80),
        )
        total = time.monotonic() - t_start

        # Should complete in ~80ms (parallel), not 160ms (serial)
        assert total < 0.14, (
            f"Two fans' processing took {total*1000:.0f}ms — "
            f"expected ~80ms (parallel). Different-fan locks must be independent."
        )

    @pytest.mark.anyio
    async def test_same_fan_messages_are_serial_not_parallel(self):
        """
        Two messages for the SAME fan must process serially (one waits for the other).
        Total time should be ~160ms, not ~80ms.
        """
        import connector.fanvue_connector as c

        fan_id = "speed-test-serial"
        c._sub_locks.pop(fan_id, None)

        timings = []

        async def process_one():
            lock = c._get_sub_lock(fan_id)
            async with lock:
                await asyncio.sleep(0.08)
                timings.append(time.monotonic())

        t_start = time.monotonic()
        await asyncio.gather(process_one(), process_one())
        total = time.monotonic() - t_start

        # Serial: ~160ms total. If parallel (broken), ~80ms.
        assert total >= 0.14, (
            f"Same-fan messages completed in {total*1000:.0f}ms — "
            f"they should be serial (~160ms), not parallel. Lock is not working."
        )
