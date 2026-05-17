"""
Rigorous tests for _split_into_chunks.

Organised into:
  - Properties every output must satisfy (invariants)
  - Edge / degenerate inputs
  - Casual texting patterns (real bot voice)
  - Ellipsis-heavy AI output
  - Punctuation edge cases
  - Emoji handling
  - Content integrity (nothing lost, nothing duplicated, order preserved)
  - PPV / teaser messages that must not be split
  - Regression tests for previously found bugs
"""

import sys
import os
import re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from connector.fanvue_connector import _split_into_chunks


# ─────────────────────────────────────────────
# Shared invariant helpers — applied to EVERY test
# ─────────────────────────────────────────────

def assert_invariants(original: str, chunks: list[str]):
    """Properties that must hold for every call regardless of input."""
    # 1. Return type
    assert isinstance(chunks, list)

    # 2. No empty chunks
    for i, c in enumerate(chunks):
        assert c.strip(), f"Chunk {i} is empty or whitespace: {chunks!r}"

    # 3. At least one chunk for non-empty input
    if original.strip():
        assert len(chunks) >= 1, f"Got 0 chunks for non-empty input: {original!r}"

    # 4. All words preserved (nothing silently dropped)
    original_words = re.findall(r"\w+", original.lower())
    chunks_words = re.findall(r"\w+", " ".join(chunks).lower())
    assert sorted(original_words) == sorted(chunks_words), (
        f"Words lost or gained during splitting.\n"
        f"  Original: {original_words}\n"
        f"  Chunks:   {chunks_words}"
    )

    # 5. Order preserved — each chunk's content appears in the original
    #    in the same left-to-right sequence
    pos = 0
    for chunk in chunks:
        first_word = re.search(r"\w+", chunk)
        if first_word:
            idx = original.lower().find(first_word.group().lower(), pos)
            assert idx != -1, (
                f"Chunk appears out of order or not found in original.\n"
                f"  Chunk: {chunk!r}\n"
                f"  Original: {original!r}"
            )
            pos = idx

    # 6. No chunk appears more times in output than it occurs in the original text.
    #    (Repeated inputs like "yes. yes. yes." legitimately produce identical chunks.)
    from collections import Counter
    for chunk_text, count in Counter(chunks).items():
        needle = chunk_text.lower().strip()
        found, start = 0, 0
        while True:
            idx = original.lower().find(needle, start)
            if idx == -1:
                break
            found += 1
            start = idx + 1
        assert found >= count, (
            f"Chunk {chunk_text!r} appears {count}x in output but only {found}x in original.\n"
            f"  Chunks: {chunks!r}"
        )


def run(text: str) -> list[str]:
    result = _split_into_chunks(text)
    assert_invariants(text, result)
    return result


# ─────────────────────────────────────────────
# Edge / degenerate inputs
# ─────────────────────────────────────────────

def test_empty_string():
    assert _split_into_chunks("") == []

def test_whitespace_only():
    assert _split_into_chunks("   ") == []

def test_single_word():
    assert run("hey") == ["hey"]

def test_single_word_with_emoji():
    assert run("hey 😏") == ["hey 😏"]

def test_only_ellipsis():
    result = _split_into_chunks("...")
    assert isinstance(result, list)
    # Should not crash; if it returns content it must be non-empty strings
    for c in result:
        assert c.strip()

def test_only_punctuation_marks():
    result = _split_into_chunks("!?!")
    assert isinstance(result, list)
    for c in result:
        assert c.strip()

def test_double_newline():
    result = run("first thought\n\nsecond thought")
    assert len(result) == 2
    assert "first thought" in result[0]
    assert "second thought" in result[1]

def test_trailing_newline():
    result = run("first thought\n")
    assert len(result) == 1
    assert result[0] == "first thought"

def test_leading_spaces():
    result = run("   hey there. what's up")
    assert len(result) == 2


# ─────────────────────────────────────────────
# Single-sentence (must NOT split)
# ─────────────────────────────────────────────

def test_no_punct_no_split():
    text = "you tryna actually see what im working with or just talk"
    assert run(text) == [text]

def test_sentence_no_punct_with_emoji():
    text = "come here and show me what you got 😏"
    assert run(text) == [text]

def test_price_not_split():
    # Dollar amounts with dots must not create false split points
    # (prices only appear in captions, but let's be safe)
    text = "that'll be $27.38 for this one"
    result = run(text)
    # The dot in 27.38 is NOT followed by a space so it should not split
    assert len(result) == 1

def test_abbreviation_not_split():
    # "lol." at end — only one thought
    text = "you're so funny lol."
    result = run(text)
    assert len(result) == 1


# ─────────────────────────────────────────────
# Two-part splits
# ─────────────────────────────────────────────

def test_two_sentences_period():
    result = run("i like it slow. you want to find out")
    assert len(result) == 2
    assert result[0] == "i like it slow."
    assert result[1] == "you want to find out"

def test_two_sentences_exclamation():
    result = run("oh wow! you actually came back")
    assert len(result) == 2
    assert "oh wow!" in result[0]

def test_two_sentences_question():
    result = run("you still there? i was waiting")
    assert len(result) == 2
    assert "you still there?" in result[0]

def test_ellipsis_split():
    result = run("mmm someone's getting bold... i like it slow and deep first")
    assert len(result) == 2
    assert "bold..." in result[0]
    assert "slow and deep first" in result[1]

def test_newline_split():
    result = run("hey\nwhat you up to")
    assert len(result) == 2
    assert result[0] == "hey"
    assert result[1] == "what you up to"

def test_period_then_no_punct():
    result = run("wait. i need to think about that for a second")
    assert len(result) == 2
    assert result[0] == "wait."


# ─────────────────────────────────────────────
# Three-part splits — real AI multi-idea output
# ─────────────────────────────────────────────

def test_real_example_three_thoughts():
    text = (
        "mmm someone's getting bold... "
        "i like it slow and deep first, then rough when im already a mess. "
        "you tryna actually see what im working with or just talk"
    )
    result = run(text)
    assert len(result) == 3
    assert "bold..." in result[0]
    assert "mess." in result[1]
    assert "just talk" in result[2]

def test_three_sentences_all_punct():
    result = run("stop. you're making this hard. i can't concentrate when you talk like that")
    assert len(result) == 3

def test_three_thoughts_mixed_punct():
    result = run("you really want to play this game? fine. let me show you how it ends")
    assert len(result) == 3
    assert "?" in result[0]
    assert "fine." in result[1]
    assert "ends" in result[2]

def test_ellipsis_then_period():
    result = run("i was thinking about you all day... couldn't stop. you do something to me")
    assert len(result) == 3

def test_multiple_exclamation_then_period():
    result = run("omg!! you're so bad. stop making me feel like this")
    assert len(result) == 3
    assert "omg!!" in result[0]


# ─────────────────────────────────────────────
# Longer AI outputs — 4+ chunks
# ─────────────────────────────────────────────

def test_four_chunk_output():
    text = (
        "you think i don't notice when you talk like that. "
        "i do. "
        "every single time. "
        "and it makes me want to show you something"
    )
    result = run(text)
    assert len(result) == 4

def test_many_ellipses():
    text = "slow... then slower... then you can't take it anymore... then we speed up"
    result = run(text)
    assert len(result) == 4
    for chunk in result:
        assert len(chunk) > 0

def test_long_sexting_message():
    text = (
        "mmm i like that you asked nicely. "
        "i've been thinking about this all day. "
        "you want me to start slow? "
        "good. "
        "because i was going to anyway"
    )
    result = run(text)
    assert len(result) == 5

def test_consecutive_short_sentences():
    result = run("yes. yes. yes. come on.")
    assert len(result) == 4
    assert all("yes" in c or "come on" in c for c in result)


# ─────────────────────────────────────────────
# Emoji handling
# ─────────────────────────────────────────────

def test_emoji_mid_chunk_stays_together():
    # Emoji between two sentences — stays with the sentence it follows
    text = "you think you can handle me? 😏 i don't think you're ready. prove me wrong"
    result = run(text)
    assert len(result) == 3
    assert "😏" in result[1]   # emoji follows the ? so leads chunk 2

def test_emoji_at_end_of_chunk():
    result = run("i'm watching you 😈. don't think i haven't noticed")
    assert len(result) == 2
    assert "😈." in result[0]

def test_multiple_emoji_no_punct():
    text = "😏😏😏 you really went there"
    result = run(text)
    assert len(result) == 1
    assert "😏😏😏" in result[0]

def test_emoji_only_message():
    result = run("😏")
    assert result == ["😏"]

def test_emoji_between_ellipsis_chunks():
    text = "come here... 🔥 i want to show you something"
    result = run(text)
    assert len(result) == 2
    assert "🔥" in result[1]


# ─────────────────────────────────────────────
# PPV teasers and vague lead-ins — must NOT be split or mutated
# ─────────────────────────────────────────────

def test_ppv_teaser_short():
    text = "give me a few minutes 😏"
    assert run(text) == [text]

def test_ppv_teaser_vague():
    text = "don't judge me ok"
    assert run(text) == [text]

def test_ppv_lead_in_generic():
    text = "i have something for you"
    assert run(text) == [text]

def test_ppv_heads_up_with_emoji():
    text = "give me like 10 min, getting something ready for you 🔥"
    assert run(text) == [text]

def test_ppv_caption_unchanged():
    # Captions are intentionally vague — must not be mangled
    text = "something i made just for you"
    assert run(text) == [text]


# ─────────────────────────────────────────────
# Punctuation edge cases
# ─────────────────────────────────────────────

def test_double_question_mark():
    result = run("wait what?? you actually said that. ok then")
    assert len(result) == 3
    assert "??" in result[0]

def test_interrobang_style():
    result = run("are you serious?! i can't believe that. wow")
    assert len(result) == 3

def test_period_no_following_space_no_split():
    # Dot not followed by space — decimal, abbreviation, end of string
    text = "that costs $9.99 and you know it"
    result = run(text)
    assert len(result) == 1

def test_ellipsis_at_very_end():
    result = run("come here...")
    assert len(result) == 1
    assert result[0] == "come here..."

def test_sentence_ending_at_end_of_string():
    result = run("i like it rough.")
    assert len(result) == 1
    assert result[0] == "i like it rough."

def test_question_then_ellipsis():
    result = run("you want to see? ... then come get it")
    assert len(result) >= 2

def test_mixed_ellipsis_and_period():
    result = run("slow at first... then faster. then you beg me to stop")
    assert len(result) == 3

def test_ok_period_is_valid_standalone():
    result = run("ok. i'll show you what you want")
    assert len(result) == 2
    assert result[0] == "ok."


# ─────────────────────────────────────────────
# Content integrity — nothing lost, nothing added, order kept
# ─────────────────────────────────────────────

def test_no_words_lost_long_message():
    text = (
        "you think i don't notice when you stare like that. "
        "i do. every single time. "
        "and it makes me want to give you something to really stare at... "
        "but first you have to ask nicely"
    )
    result = run(text)
    assert len(result) >= 3

def test_no_duplicate_content():
    text = "slow at first. then faster. then you beg me to stop"
    result = run(text)
    joined = " ".join(result)
    for word in text.split():
        assert joined.lower().count(word.lower()) <= text.lower().count(word.lower()), \
            f"Word '{word}' duplicated in output"

def test_chunks_appear_in_original_order():
    text = "alpha. beta. gamma. delta"
    result = run(text)
    assert len(result) == 4
    assert result[0] == "alpha."
    assert result[1] == "beta."
    assert result[2] == "gamma."
    assert result[3] == "delta"

def test_contractions_survive():
    text = "i don't think you're ready. don't say i didn't warn you"
    result = run(text)
    assert len(result) == 2
    assert "don't think you're ready." in result[0]
    assert "didn't warn you" in result[1]

def test_apostrophe_in_word_not_split():
    # Apostrophes should never trigger a split
    text = "it's fine. you'll see"
    result = run(text)
    assert len(result) == 2
    assert "it's fine." in result[0]
    assert "you'll see" in result[1]


# ─────────────────────────────────────────────
# Regression: bugs found in previous test run
# ─────────────────────────────────────────────

def test_regression_exclamation_not_swallowed():
    # Previously: "oh wow! you actually came back" returned 1 chunk (merge bug)
    result = run("oh wow! you actually came back")
    assert len(result) == 2
    assert "oh wow!" in result[0]

def test_regression_newline_splits():
    # Previously: newline was appended to buf instead of triggering flush
    result = run("hey\nwhat you up to")
    assert len(result) == 2
    assert result[0] == "hey"
    assert result[1] == "what you up to"

def test_regression_aggressive_merge_removed():
    # Previously: 12-char threshold merged "hey" into next sentence
    result = run("hey. come talk to me")
    assert len(result) == 2
    assert result[0] == "hey."

def test_regression_short_greeting_not_merged():
    # "hi." should remain separate from the next sentence
    result = run("hi. i've been waiting for you")
    assert len(result) == 2
    assert result[0] == "hi."


# ─────────────────────────────────────────────
# Specific AI output patterns from the bot
# ─────────────────────────────────────────────

def test_consent_flow_response():
    # "i mean..." is its own thought, so the correct split is 4 chunks.
    text = "i mean... that depends. you willing to actually spend a little? i don't tease for free"
    result = run(text)
    assert len(result) == 4
    assert "i mean..." in result[0]
    assert "that depends." in result[1]

def test_edge_control_message():
    text = "don't you dare cum yet. i'm not done with you. hold it for me"
    result = run(text)
    assert len(result) == 3

def test_post_purchase_reaction():
    text = "god i came so hard for you. did you see how i was shaking. that was all for you"
    result = run(text)
    assert len(result) == 3

def test_tier_tease_message():
    text = (
        "if you liked that... "
        "wait till you see what's underneath. "
        "i've been saving the good stuff for you"
    )
    result = run(text)
    assert len(result) == 3
    assert "liked that..." in result[0]

def test_rapport_opener():
    text = "hey you. been thinking about you lately. how's your day going"
    result = run(text)
    assert len(result) == 3

def test_gfe_continuation_message():
    text = "i really like talking to you. you always know what to say. stay a little longer?"
    result = run(text)
    assert len(result) == 3

def test_explicit_scene_narration():
    # Simulated Grok-uncensored output — multiple explicit sentences
    text = (
        "i'm touching myself thinking about you right now. "
        "you make me so wet when you talk like that. "
        "i wish you could feel how warm i am"
    )
    result = run(text)
    assert len(result) == 3
    for chunk in result:
        assert len(chunk) > 10, f"Suspiciously short explicit chunk: {chunk!r}"
