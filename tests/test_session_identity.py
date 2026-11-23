"""Who a tool call is *for* is the session's fact, never the model's.

Two defects motivated this file, and both were invisible to the eval suite:

1. The prompt tells the model `list_bookings: no arguments`, but the server required
   `user_id`. A compliant model sent `{}` and the call died on a TypeError, which
   reached the user as "A service call failed" — every manage request ("move my AC
   servicing to Saturday") was unreachable in the live path.
2. The server trusted whatever `user_id` arrived, so a model that *did* supply one
   could read another customer's bookings — a cross-tenant read on a read-only tool.

The eval scenarios passed because the script handed in `{"user_id": "u_aldrin"}`
explicitly. The script satisfied the signature, so nothing exercised the contract a
real model is given. These tests assert on the contract itself.
"""
from __future__ import annotations

import pytest

from errand.clock import FakeClock
from errand.policy import State
from errand.tools.gateway import Gateway
from errand.tools.mock import MockToolServer
from evals.demo_data import ANCHOR, build_world


@pytest.fixture
def world():
    clock = FakeClock(ANCHOR.replace(hour=8))
    w = build_world(clock, seed=7, user_id="u_aldrin")
    yield w
    w.close()


def test_list_bookings_succeeds_with_no_arguments(world):
    """The exact call the system prompt describes. It must not need a user_id."""
    result = Gateway(server=world.server, user_id="u_aldrin").call(
        State.DISCOVERING, "list_bookings", {}
    )
    assert result.ok, result.error
    assert result.value, "the demo user has seeded bookings"


def test_a_model_cannot_read_another_customers_bookings(world):
    """A spoofed user_id is dropped, not honoured: the answer is still this session's."""
    result = Gateway(server=world.server, user_id="u_aldrin").call(
        State.DISCOVERING, "list_bookings", {"user_id": "u_priya"}
    )
    assert result.ok, result.error
    owners = {b.user_id for b in result.value}
    assert owners == {"u_aldrin"}, f"leaked bookings for {owners - {'u_aldrin'}}"


def test_an_unknown_argument_is_still_refused(world):
    """Dropping bound keys must not become a blanket 'accept anything'."""
    result = Gateway(server=world.server, user_id="u_aldrin").call(
        State.DISCOVERING, "list_bookings", {"limit": 5}
    )
    assert not result.ok
    assert result.error.error_class == "validation"


def test_a_booking_belongs_to_the_user_who_made_it(world):
    """A booking recorded under a constant id is invisible to its own owner.

    That is the second half of the manage bug: even after `list_bookings` stopped
    crashing, a booking made in this session could not be found by the person who
    made it, so a follow-up "move that booking" had nothing to resolve.
    """
    provider = world.server.search_providers("plumber", "Koramangala")[0]
    hold = world.server.hold_slot(provider.id, provider.slots[0], user_id="u_aldrin")
    booking = world.server.book(hold.hold_id, "identity-key")

    assert booking.user_id == "u_aldrin"
    visible = [b.booking_id for b in world.server.list_bookings("u_aldrin")]
    assert booking.booking_id in visible
    assert booking.booking_id not in [
        b.booking_id for b in world.server.list_bookings("u_priya")
    ]


def test_holds_are_scoped_to_their_owner(world):
    """Two customers holding the same slot must not receive each other's quote."""
    provider = world.server.search_providers("plumber", "Koramangala")[0]
    slot = provider.slots[0]
    mine = world.server.hold_slot(provider.id, slot, user_id="u_aldrin")
    theirs = world.server.hold_slot(provider.id, slot, user_id="u_priya")

    assert mine.hold_id != theirs.hold_id
    assert theirs.user_id == "u_priya"


def test_the_prompt_and_the_contract_agree_on_list_bookings():
    """The schema text is what the model is told; it must not name arguments."""
    from errand.llm import _SCHEMA_TEXT

    line = next(ln for ln in _SCHEMA_TEXT.splitlines() if ln.startswith("- list_bookings"))
    assert "no arguments" in line
    assert "user_id" not in line
