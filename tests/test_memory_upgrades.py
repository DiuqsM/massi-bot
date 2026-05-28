"""
Tests for memory system upgrades:
  - U5: Memory deduplication (cosine similarity > 0.85 skips insert)
  - U6: Emotional valence auto-scoring

Run with: pytest tests/test_memory_upgrades.py -v
"""

import pytest
import sys
import os
from unittest.mock import MagicMock, AsyncMock, patch
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'engine'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from models import Subscriber, SubState


# Override the autouse mock_memory_manager fixture from conftest.py
# so our memory-specific tests can exercise the real get_context_memories method.
@pytest.fixture(autouse=True)
def mock_memory_manager():
    """No-op override: let memory tests control their own mocking."""
    yield


# ─────────────────────────────────────────────
# U6: Emotional Valence Auto-Scoring
# ─────────────────────────────────────────────

class TestEmotionalValence:

    def test_strong_negative_keywords(self):
        from llm.memory_store import _estimate_emotional_valence
        assert _estimate_emotional_valence("my dog died last week", "event") == -0.8
        assert _estimate_emotional_valence("going through a divorce", "relationship") == -0.8
        assert _estimate_emotional_valence("just got fired from my job", "event") == -0.8
        assert _estimate_emotional_valence("in the hospital right now", "event") == -0.8
        assert _estimate_emotional_valence("dealing with cancer treatment", "event") == -0.8

    def test_moderate_negative_keywords(self):
        from llm.memory_store import _estimate_emotional_valence
        assert _estimate_emotional_valence("feeling really stressed", "emotion") == -0.5
        assert _estimate_emotional_valence("i've been so lonely", "emotion") == -0.5
        assert _estimate_emotional_valence("had a rough day at work", "emotion") == -0.5
        assert _estimate_emotional_valence("i'm overwhelmed with everything", "emotion") == -0.5
        assert _estimate_emotional_valence("been anxious about stuff", "emotion") == -0.5

    def test_moderate_positive_keywords(self):
        from llm.memory_store import _estimate_emotional_valence
        assert _estimate_emotional_valence("just got promoted!", "event") == 0.6
        assert _estimate_emotional_valence("started a new job", "job") == 0.6
        assert _estimate_emotional_valence("i'm so excited about this", "emotion") == 0.6
        assert _estimate_emotional_valence("feeling happy today", "emotion") == 0.6
        assert _estimate_emotional_valence("i'm grateful for everything", "emotion") == 0.6

    def test_strong_positive_keywords(self):
        from llm.memory_store import _estimate_emotional_valence
        assert _estimate_emotional_valence("just got engaged!", "event") == 0.8
        assert _estimate_emotional_valence("we're having a baby", "event") == 0.8
        assert _estimate_emotional_valence("graduated from college", "event") == 0.8
        assert _estimate_emotional_valence("this is the best day ever", "event") == 0.8
        assert _estimate_emotional_valence("landed my dream job", "job") == 0.8

    def test_emotion_category_default_negative(self):
        from llm.memory_store import _estimate_emotional_valence
        # Generic emotion without specific keywords defaults to -0.3
        assert _estimate_emotional_valence("feeling some kind of way", "emotion") == -0.3

    def test_neutral_default(self):
        from llm.memory_store import _estimate_emotional_valence
        # No keywords, non-emotion category → 0.0
        assert _estimate_emotional_valence("works as a plumber", "job") == 0.0
        assert _estimate_emotional_valence("from Texas", "location") == 0.0
        assert _estimate_emotional_valence("into fishing", "hobby") == 0.0

    def test_strong_negative_takes_priority_over_moderate(self):
        from llm.memory_store import _estimate_emotional_valence
        # "died" is strong negative, should hit first even if other words match
        assert _estimate_emotional_valence("my dad died and I'm stressed", "event") == -0.8

    def test_case_insensitive(self):
        from llm.memory_store import _estimate_emotional_valence
        assert _estimate_emotional_valence("MY DOG DIED", "event") == -0.8
        assert _estimate_emotional_valence("Just Got Promoted", "event") == 0.6


# ─────────────────────────────────────────────
# U5: Memory Deduplication
# ─────────────────────────────────────────────

class TestMemoryDedup:

    @pytest.mark.anyio
    async def test_dedup_refreshes_existing_memory(self):
        """When a near-duplicate exists (similarity > 0.85), update last_accessed instead of inserting."""
        from llm.memory_store import store_memory

        mock_sb = MagicMock()
        # Dedup RPC returns a matching row
        mock_rpc_result = MagicMock()
        mock_rpc_result.data = [{"id": "existing-uuid-123", "fact": "works as a nurse", "similarity": 0.92}]
        mock_sb.rpc.return_value.execute.return_value = mock_rpc_result

        # Update call
        mock_update_result = MagicMock()
        mock_sb.table.return_value.update.return_value.eq.return_value.execute.return_value = mock_update_result

        mock_embed = MagicMock(return_value=[0.1] * 384)

        with patch("llm.memory_store._get_supabase", return_value=mock_sb), \
             patch("llm.memory_store._embed", mock_embed):
            result = await store_memory(
                sub_id="test-sub",
                fact="works as a nurse",
                category="job",
            )

        assert result is True
        # Should have called rpc for dedup check
        mock_sb.rpc.assert_called_once_with("match_subscriber_memory", {
            "p_sub_id": "test-sub",
            "p_query_emb": [0.1] * 384,
            "p_limit": 3,
            "p_threshold": 0.60,
        })
        # Should have updated last_accessed, NOT inserted
        mock_sb.table.return_value.update.assert_called_once()
        update_arg = mock_sb.table.return_value.update.call_args[0][0]
        assert "last_accessed" in update_arg
        # Should NOT have called insert
        mock_sb.table.return_value.insert.assert_not_called()

    @pytest.mark.anyio
    async def test_no_dedup_match_proceeds_with_insert(self):
        """When no near-duplicate exists, proceed with normal insert."""
        from llm.memory_store import store_memory

        mock_sb = MagicMock()
        # Dedup RPC returns no matches
        mock_rpc_result = MagicMock()
        mock_rpc_result.data = []
        mock_sb.rpc.return_value.execute.return_value = mock_rpc_result

        # Insert call
        mock_insert_result = MagicMock()
        mock_sb.table.return_value.insert.return_value.execute.return_value = mock_insert_result

        mock_embed = MagicMock(return_value=[0.1] * 384)

        with patch("llm.memory_store._get_supabase", return_value=mock_sb), \
             patch("llm.memory_store._embed", mock_embed):
            result = await store_memory(
                sub_id="test-sub",
                fact="works as a nurse",
                category="job",
            )

        assert result is True
        # Should have called insert
        mock_sb.table.return_value.insert.assert_called_once()
        insert_arg = mock_sb.table.return_value.insert.call_args[0][0]
        assert insert_arg["fact"] == "works as a nurse"
        assert insert_arg["category"] == "job"

    @pytest.mark.anyio
    async def test_dedup_check_failure_falls_through_to_insert(self):
        """If the dedup RPC call fails, still proceed with insert."""
        from llm.memory_store import store_memory

        mock_sb = MagicMock()
        # Dedup RPC raises exception
        mock_sb.rpc.return_value.execute.side_effect = Exception("RPC failed")

        # Insert call succeeds
        mock_insert_result = MagicMock()
        mock_sb.table.return_value.insert.return_value.execute.return_value = mock_insert_result

        mock_embed = MagicMock(return_value=[0.1] * 384)

        with patch("llm.memory_store._get_supabase", return_value=mock_sb), \
             patch("llm.memory_store._embed", mock_embed):
            result = await store_memory(
                sub_id="test-sub",
                fact="from Texas",
                category="location",
            )

        assert result is True
        mock_sb.table.return_value.insert.assert_called_once()


# ─────────────────────────────────────────────
# U6: Emotional valence integration in store_memory
# ─────────────────────────────────────────────

class TestEmotionalValenceInStore:

    @pytest.mark.anyio
    async def test_auto_scores_when_emotional_val_is_zero(self):
        """store_memory auto-calculates emotional_val when passed as 0.0."""
        from llm.memory_store import store_memory

        mock_sb = MagicMock()
        # No dedup match
        mock_rpc_result = MagicMock()
        mock_rpc_result.data = []
        mock_sb.rpc.return_value.execute.return_value = mock_rpc_result

        mock_insert_result = MagicMock()
        mock_sb.table.return_value.insert.return_value.execute.return_value = mock_insert_result

        mock_embed = MagicMock(return_value=[0.1] * 384)

        with patch("llm.memory_store._get_supabase", return_value=mock_sb), \
             patch("llm.memory_store._embed", mock_embed):
            await store_memory(
                sub_id="test-sub",
                fact="my dog died last week",
                category="event",
                emotional_val=0.0,  # Should be auto-scored
            )

        insert_arg = mock_sb.table.return_value.insert.call_args[0][0]
        assert insert_arg["emotional_val"] == -0.8

    @pytest.mark.anyio
    async def test_preserves_explicit_emotional_val(self):
        """store_memory preserves explicitly provided non-zero emotional_val."""
        from llm.memory_store import store_memory

        mock_sb = MagicMock()
        mock_rpc_result = MagicMock()
        mock_rpc_result.data = []
        mock_sb.rpc.return_value.execute.return_value = mock_rpc_result

        mock_insert_result = MagicMock()
        mock_sb.table.return_value.insert.return_value.execute.return_value = mock_insert_result

        mock_embed = MagicMock(return_value=[0.1] * 384)

        with patch("llm.memory_store._get_supabase", return_value=mock_sb), \
             patch("llm.memory_store._embed", mock_embed):
            await store_memory(
                sub_id="test-sub",
                fact="just got promoted!",
                category="event",
                emotional_val=0.9,  # Explicitly set — should NOT be overwritten
            )

        insert_arg = mock_sb.table.return_value.insert.call_args[0][0]
        assert insert_arg["emotional_val"] == 0.9


# ─────────────────────────────────────────────
# U6: Emotional context in memory_manager
# ─────────────────────────────────────────────

class TestMemoryManagerEmotionalContext:

    @pytest.mark.anyio
    async def test_negative_emotional_context(self):
        """When retrieved memories have avg valence <= -0.5, adds nurturing note."""
        import llm.memory_manager as mm

        mock_rows = [
            {"fact": "my dog died", "emotional_val": -0.8, "id": "1"},
            {"fact": "feeling stressed", "emotional_val": -0.5, "id": "2"},
        ]

        sub = MagicMock()
        sub.sub_id = "test-sub"

        with patch.object(mm, "retrieve_memories_with_metadata",
                          new_callable=AsyncMock, return_value=mock_rows):
            memories = await mm.memory_manager.get_context_memories(sub, "how are you")

        assert memories == ["my dog died", "feeling stressed"]
        assert "rough time" in mm.memory_manager._last_emotional_context

    @pytest.mark.anyio
    async def test_positive_emotional_context(self):
        """When retrieved memories have avg valence >= 0.5, adds playful note."""
        import llm.memory_manager as mm

        mock_rows = [
            {"fact": "just got promoted", "emotional_val": 0.6, "id": "1"},
            {"fact": "having a baby", "emotional_val": 0.8, "id": "2"},
        ]

        sub = MagicMock()
        sub.sub_id = "test-sub"

        with patch.object(mm, "retrieve_memories_with_metadata",
                          new_callable=AsyncMock, return_value=mock_rows):
            memories = await mm.memory_manager.get_context_memories(sub, "what's new")

        assert memories == ["just got promoted", "having a baby"]
        assert "good place" in mm.memory_manager._last_emotional_context

    @pytest.mark.anyio
    async def test_neutral_emotional_context(self):
        """When memories have neutral valence, no emotional context is added."""
        import llm.memory_manager as mm

        mock_rows = [
            {"fact": "works as a plumber", "emotional_val": 0.0, "id": "1"},
            {"fact": "from Texas", "emotional_val": 0.0, "id": "2"},
        ]

        sub = MagicMock()
        sub.sub_id = "test-sub"

        with patch.object(mm, "retrieve_memories_with_metadata",
                          new_callable=AsyncMock, return_value=mock_rows):
            await mm.memory_manager.get_context_memories(sub, "hey there")

        assert mm.memory_manager._last_emotional_context == ""

    @pytest.mark.anyio
    async def test_empty_memories_no_emotional_context(self):
        """When no memories retrieved, emotional context is empty."""
        import llm.memory_manager as mm

        sub = MagicMock()
        sub.sub_id = "test-sub"

        with patch.object(mm, "retrieve_memories_with_metadata",
                          new_callable=AsyncMock, return_value=[]):
            memories = await mm.memory_manager.get_context_memories(sub, "hello")

        assert memories == []
        assert mm.memory_manager._last_emotional_context == ""

    def test_format_for_prompt_with_emotional_context(self):
        """format_for_prompt includes emotional note when provided."""
        from llm.memory_manager import memory_manager

        result = memory_manager.format_for_prompt(
            ["works as a nurse", "from Texas"],
            emotional_context="He's been going through a rough time recently."
        )
        assert "Long-term memories" in result
        assert "works as a nurse" in result
        assert "Emotional note:" in result
        assert "rough time" in result

    def test_format_for_prompt_uses_auto_computed_context(self):
        """format_for_prompt uses _last_emotional_context when no explicit context given."""
        from llm.memory_manager import memory_manager

        memory_manager._last_emotional_context = "He's in a good place. Match his energy, be playful."
        result = memory_manager.format_for_prompt(["just got promoted"])
        assert "Emotional note:" in result
        assert "good place" in result

    def test_format_for_prompt_no_emotional_context(self):
        """format_for_prompt omits emotional note when none available."""
        from llm.memory_manager import memory_manager

        memory_manager._last_emotional_context = ""
        result = memory_manager.format_for_prompt(["works as a plumber"])
        assert "Emotional note:" not in result

    def test_format_for_prompt_empty_memories(self):
        """format_for_prompt returns empty string for empty memories."""
        from llm.memory_manager import memory_manager
        assert memory_manager.format_for_prompt([]) == ""

    def test_format_for_prompt_explicit_overrides_auto(self):
        """Explicit emotional_context parameter overrides auto-computed one."""
        from llm.memory_manager import memory_manager

        memory_manager._last_emotional_context = "auto context"
        result = memory_manager.format_for_prompt(
            ["some fact"],
            emotional_context="explicit context"
        )
        assert "explicit context" in result
        assert "auto context" not in result
