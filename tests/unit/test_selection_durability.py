"""What the customer has chosen, and why it used to disappear.

Reconstructed from a real session. They searched sofas, compared two, chose
one, were offered rugs, chose a rug - and the session ended holding only the
sofa. Meanwhile the agent said "I've got the 5th option as your choice" and
later "you've selected a sofa and a rug", neither of which was true, and when
asked to see them answered that the cards were not shown in this turn.

Three defects, one section each:

1. a complement used the piece they settled on to ask the specialist and then
   discarded it, so the choice was never recorded;
2. the reply described selections from the conversation rather than from
   state, so it could report one that had not happened;
3. there was no way to put their choices back on screen.
"""

from __future__ import annotations

# ── 1. the piece they settled on is kept ────────────────────────────────────


# ── 2. the reply reports what was recorded, not what was said ───────────────




# ── 3. their choices can be put back on screen ──────────────────────────────


# ── the kinds must match what was actually chosen ───────────────────────────
#
# From session web-2c4aaeb983d0: a 2-seater loveseat from a comparison, then
# the first centre table. The reply said "you now have 2 sofas recorded" - the
# model had a count of two and a screen full of sofas, and no nouns of its own
# (M19 2). The same turn then offered more sofas, because the specialist was
# told about the table and nothing else (M19 1).




def test_the_kinds_are_customer_words() -> None:
    from app.taxonomy.words import customer_words

    assert customer_words("center-table") == "center table"
    assert "-" not in customer_words("single-seater-sofa")
