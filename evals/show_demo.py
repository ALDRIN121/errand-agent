"""Print the seeded demo world. `python -m evals.demo_data` to eyeball the fixtures.

This is a development aid, not part of the agent: it exists so a human can check
the mock environment before recording a demo, without launching the chat loop.
"""
from __future__ import annotations

import sys

from errand.clock import FakeClock
from evals.demo_data import ANCHOR, build_world, load_fixture


def main(argv: list[str]) -> int:
    user_id = argv[1] if len(argv) > 1 else "u_aldrin"
    clock = FakeClock(ANCHOR.replace(hour=8))
    world = build_world(clock, user_id=user_id)
    data = load_fixture()

    print(f"=== demo world: {user_id} @ {clock.now():%a %d %b %Y %H:%M %Z} ===\n")

    print("users")
    for u in data["users"]:
        print(f"  {u['id']:10s} {u['name']:8s} {u['area']:12s} budget ₹{u['default_budget_paise']//100:>5}")
    print()

    print(f"prior bookings for {user_id}")
    for b in world.bookings_for(user_id):
        print(f"  {b.booking_id}  {b.provider_id}  {b.service:12s} {b.slot.start:%a %d %b %H:%M}  {b.status}")
    print()

    print("stored preferences (admissible)")
    for m in world.memory.admissible(user_id):
        print(f"  {m.key:28s} {m.value}")
    print()

    print("excluded memory rows (must never reach the model)")
    for row in world.memory.all_rows(user_id):
        if row["reason"]:
            print(f"  {row['key']:28s} excluded: {row['reason']}")
    print()

    providers = world.server.search_providers("plumber", world.users[user_id]["area"])
    print(f"plumbers available in {world.users[user_id]['area']}")
    for p in providers:
        flag = "  [review carries an injection]" if any(
            r.trust == "untrusted" and ("IGNORE" in r.text or "SYSTEM:" in r.text)
            for r in world.provider_reviews(p.id)
        ) else ""
        print(f"  {p.id}  ₹{p.price_paise//100:>5}  ★{p.rating}  {len(p.slots)} slots{flag}")
    print()

    print(f"scripted demo dialogues ({len(world.dialogues)})")
    for d in world.dialogues:
        ex = d["expected"]
        verb = ex.get("outcome_verb", "")
        print(f"  {d['id']:24s} -> {ex['outcome']:32s} {verb}")

    world.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
