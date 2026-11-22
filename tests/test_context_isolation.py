"""The model's context must not manufacture a request the user never made.

The bug these pin: the relevance floor only filtered when something scored, so an
utterance that matched nothing — every greeting — received the entire memory page.
With `last_plumber_provider` and `preferred_area` in view, saying "hi" started a
plumber booking.
"""
from __future__ import annotations

from errand.clock import FakeClock
from errand.context import ContextAssembler
from errand.policy import State
from evals.demo_data import ANCHOR, build_world


def _world():
    clock = FakeClock(ANCHOR.replace(hour=8))
    return build_world(clock, user_id="u_aldrin"), clock


def test_a_greeting_sends_no_memory_at_all():
    """Not "some irrelevant memory" — none. An empty block is the honest context."""
    world, _ = _world()
    try:
        prompt, manifest = ContextAssembler().assemble(
            state=State.NEW, approval_kind=None, history=world.history,
            memory=world.memory, user_id="u_aldrin", intent="hi",
            tool_result=None, latest_user="hi",
        )
        rendered = ContextAssembler().render_text(prompt)
        for leak in ("last_plumber_provider", "prov_06", "preferred_area"):
            assert leak not in rendered, f"'hi' still leaks {leak}"
        assert not manifest.included, manifest.included
    finally:
        world.close()


def test_an_unmatched_utterance_never_sends_the_whole_page():
    """The old code sent everything once nothing matched. That is the bug."""
    world, _ = _world()
    try:
        total = len(world.memory.admissible("u_aldrin"))
        assert total > 1, "the demo world should carry several memories"
        prompt, manifest = ContextAssembler().assemble(
            state=State.NEW, approval_kind=None, history=world.history,
            memory=world.memory, user_id="u_aldrin", intent="zzzz nonsense",
            tool_result=None, latest_user="zzzz nonsense",
        )
        assert len(manifest.included) < total, (
            f"sent {len(manifest.included)} of {total} memories for an unmatched intent"
        )
    finally:
        world.close()


def test_a_real_request_still_receives_the_relevant_preference():
    """The floor must not be so strict that useful context disappears."""
    world, _ = _world()
    try:
        prompt, manifest = ContextAssembler().assemble(
            state=State.NEW, approval_kind=None, history=world.history,
            memory=world.memory, user_id="u_aldrin",
            intent="Find a plumber in Koramangala",
            tool_result=None, latest_user="Find a plumber in Koramangala",
        )
        rendered = ContextAssembler().render_text(prompt)
        assert "preferred_area" in rendered
    finally:
        world.close()


def test_every_included_memory_scored_above_zero():
    """No zero-score row may enter the prompt by any route."""
    world, _ = _world()
    try:
        for intent in ("hi", "thanks", "zzzz", "Find a plumber in Koramangala"):
            _, manifest = ContextAssembler().assemble(
                state=State.NEW, approval_kind=None, history=world.history,
                memory=world.memory, user_id="u_aldrin", intent=intent,
                tool_result=None, latest_user=intent,
            )
            for entry in manifest.included:
                assert entry["score"] > 0, (intent, entry)
    finally:
        world.close()


def test_the_prior_conversation_can_be_disabled():
    """A clean slate must really be clean, or "hi" continues an old thread."""
    world, _ = _world()
    try:
        assert len(world.history.turns) > 0
    finally:
        world.close()

    clock = FakeClock(ANCHOR.replace(hour=8))
    fresh = build_world(clock, user_id="u_aldrin", prior_conversation=False)
    try:
        assert fresh.history.turns == [], (
            f"--fresh still seeded {len(fresh.history.turns)} turns"
        )
    finally:
        fresh.close()
