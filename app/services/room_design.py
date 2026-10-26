"""Nora's room design, checked against the vocabulary and the shelf.

Nora decides the room: its pieces, which types may fill each, how many, what
each should look like, how big, where it goes and how much of the budget it
takes. This is the deterministic half - every rule a design must keep before a
single product is searched (CLAUDE.md 3.3, 14.3):

* every type is an approved one, and a piece's types share one family - a
  refusal names the family the registry files it under;
* a type the store does not stock is left out, and a piece left with none is
  reported as not stocked, never filled with something near it;
* colours and styles are cut to the ones that piece's types are stocked in;
* a seat count goes only on a type that seats several - a chair seats one by
  its nature, and a chaise lounge or recliner never counts as a seat;
* shares of the budget add up to at most the whole of it;
* prose states no figure.

What a room needs is never checked here: there is no room table to check it
against (`room_pieces_v1.yaml` is a guide Nora reads, not a rule).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.schemas.catalog_overview import CatalogOverview
from app.schemas.design import (
    DesignCategoryNeed,
    DesignPriority,
    PieceDesign,
)
from app.schemas.discovery import SeatingCapacityConstraint
from app.services.design_checks import figure_free, stocked_values
from app.taxonomy.attributes import AttributeFamily, CatalogAttributes
from app.taxonomy.registry import CommerceTaxonomy
from app.taxonomy.seating import SeatingSemantics
from app.taxonomy.words import customer_words

MAX_ROOM_PIECES = 14
MAX_PIECE_QUANTITY = 6
MAX_SEATS_EACH = 12


@dataclass(frozen=True, slots=True)
class PieceSpec:
    """One piece as Nora wrote it - every value still unchecked."""

    role: str
    types: tuple[str, ...]
    quantity: int = 1
    must: bool = True
    colours: tuple[str, ...] = ()
    styles: tuple[str, ...] = ()
    avoid_colours: tuple[str, ...] = ()
    avoid_styles: tuple[str, ...] = ()
    feel: str | None = None
    longer_min_cm: int | None = None
    longer_max_cm: int | None = None
    shorter_min_cm: int | None = None
    shorter_max_cm: int | None = None
    share_percent: int | None = None
    seats_each: int | None = None
    wall: str | None = None
    beside: str | None = None
    from_pick: int | None = None


@dataclass(frozen=True, slots=True)
class RoomDesignProblem:
    """A rule the design broke - corrected by Nora, never guessed by code."""

    code: str
    piece: int | None = None
    detail: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CheckedRoomDesign:
    needs: tuple[DesignCategoryNeed, ...]
    """The pieces the store can supply, in her order."""
    picks: dict[int, int]
    """Need position -> the cart place of the pick that fills it."""
    facts: dict[str, object]
    """What was left out and why, and the seats the design gives."""


class RoomDesignChecker:
    def __init__(
        self,
        taxonomy: CommerceTaxonomy,
        attributes: CatalogAttributes,
        seating: SeatingSemantics | None,
    ) -> None:
        self._taxonomy = taxonomy
        self._attributes = attributes
        self._seating = seating

    def check(
        self,
        pieces: Sequence[PieceSpec],
        overview: CatalogOverview,
        *,
        head_count: int | None,
        walls: Sequence[str],
        texts: Sequence[str | None] = (),
    ) -> CheckedRoomDesign | RoomDesignProblem:
        """The design as needs the room engine can build, or the first rule it
        breaks. `walls` are the names of the walls on record; `texts` any
        other prose of hers (her why) that must state no figure."""
        if not pieces or len(pieces) > MAX_ROOM_PIECES:
            return RoomDesignProblem(
                code="pieces_out_of_range", detail={"at_most": MAX_ROOM_PIECES}
            )
        if not figure_free(*texts):
            return RoomDesignProblem(code="why_states_a_figure")
        shares = sum(p.share_percent or 0 for p in pieces)
        if shares > 100:
            return RoomDesignProblem(code="shares_over_whole_budget", detail={"shares": shares})
        named_walls = {w.casefold(): w for w in walls}
        roles = {p.role.casefold() for p in pieces}

        needs: list[DesignCategoryNeed] = []
        picks: dict[int, int] = {}
        not_stocked: list[str] = []
        types_dropped: list[dict[str, object]] = []
        looks_dropped: list[str] = []
        walls_unknown: list[str] = []
        for number, piece in enumerate(pieces, start=1):
            problem = self._piece_problem(number, piece, named_walls)
            if problem is not None:
                return problem
            family = self._taxonomy.category_of(piece.types[0])
            assert family is not None, "checked approved above"
            stocked = tuple(t for t in dict.fromkeys(piece.types) if overview.stocks(family, t))
            unstocked = [t for t in piece.types if t not in stocked]
            if unstocked:
                types_dropped.append(
                    {"piece": number, "types": [customer_words(t) for t in unstocked]}
                )
            if not stocked and piece.from_pick is None:
                not_stocked.append(piece.role)
                continue
            kinds = stocked or piece.types[:1]
            shelves = [s for s in overview.shelves_in(family) if s.commerce_subcategory in kinds]
            colours_here = self._canonical(
                AttributeFamily.COLOR, [c for s in shelves for c in s.colours]
            )
            styles_here = self._canonical(
                AttributeFamily.STYLE, [t for s in shelves for t in s.styles]
            )
            colours = stocked_values(piece.colours, colours_here)
            styles = stocked_values(piece.styles, styles_here)
            if len(colours) < len(piece.colours) or len(styles) < len(piece.styles):
                looks_dropped.append(piece.role)
            wall = named_walls.get((piece.wall or "").casefold()) if piece.wall else None
            if piece.wall and wall is None:
                walls_unknown.append(piece.role)
            beside = piece.beside if piece.beside and piece.beside.casefold() in roles else None
            if piece.from_pick is not None:
                picks[len(needs)] = piece.from_pick
            needs.append(
                DesignCategoryNeed(
                    commerce_category=family,
                    commerce_subcategory=kinds[0],
                    priority=DesignPriority.REQUIRED if piece.must else DesignPriority.OPTIONAL,
                    quantity=piece.quantity,
                    semantic_intent=piece.feel,
                    seating_capacity=(
                        SeatingCapacityConstraint(
                            min_capacity=piece.seats_each, max_capacity=piece.seats_each
                        )
                        if piece.seats_each is not None
                        else None
                    ),
                    design=PieceDesign(
                        role=piece.role,
                        alternatives=tuple(kinds[1:]),
                        colours=colours,
                        styles=styles,
                        avoid_colours=stocked_values(piece.avoid_colours, colours_here),
                        avoid_styles=stocked_values(piece.avoid_styles, styles_here),
                        longer_min_cm=piece.longer_min_cm,
                        longer_max_cm=piece.longer_max_cm,
                        shorter_min_cm=piece.shorter_min_cm,
                        shorter_max_cm=piece.shorter_max_cm,
                        share_percent=piece.share_percent,
                        wall=wall,
                        beside=beside,
                    ),
                )
            )
        facts: dict[str, object] = {}
        if not_stocked:
            facts["not_stocked"] = not_stocked
        if types_dropped:
            facts["types_not_stocked"] = types_dropped
        if looks_dropped:
            facts["looks_left_out"] = {
                "pieces": looks_dropped,
                "why": "colours or styles those types are not stocked in",
            }
        if walls_unknown:
            facts["walls_not_on_record"] = walls_unknown
        facts.update(self._seat_facts(needs, head_count))
        return CheckedRoomDesign(needs=tuple(needs), picks=picks, facts=facts)

    def _piece_problem(
        self, number: int, piece: PieceSpec, walls: dict[str, str]
    ) -> RoomDesignProblem | None:
        if not piece.types:
            return RoomDesignProblem(code="piece_needs_a_type", piece=number)
        if (
            piece.longer_min_cm is not None
            and piece.longer_max_cm is not None
            and piece.longer_min_cm > piece.longer_max_cm
        ) or (
            piece.shorter_min_cm is not None
            and piece.shorter_max_cm is not None
            and piece.shorter_min_cm > piece.shorter_max_cm
        ):
            return RoomDesignProblem(code="size_range_out_of_order", piece=number)
        families: dict[str, str] = {}
        for kind in piece.types:
            family = self._taxonomy.category_of(kind)
            if family is None:
                return RoomDesignProblem(
                    code="type_not_approved", piece=number, detail={"type": kind}
                )
            families[kind] = family
        if len(set(families.values())) > 1:
            # One piece is one family: a side table or a stool are two pieces.
            return RoomDesignProblem(
                code="types_span_families",
                piece=number,
                detail={"families": {customer_words(k): f for k, f in families.items()}},
            )
        # A wall is named as they named it ("wall 2"); any other words are hers.
        wall_words = None if (piece.wall or "").casefold() in walls else piece.wall
        if not figure_free(piece.role, piece.feel, wall_words, piece.beside):
            return RoomDesignProblem(code="words_state_a_figure", piece=number)
        if piece.seats_each is not None:
            seating = self._seating
            if seating is None or not all(seating.seats_several(t) for t in piece.types):
                # Only a type that seats several has a seat count to choose;
                # one-seat types seat one, and some types never count.
                return RoomDesignProblem(
                    code="seats_each_only_on_multi_seat_types",
                    piece=number,
                    detail={
                        "one_seat_types": [customer_words(t) for t in seating.one_seat_types()]
                        if seating
                        else [],
                        "never_counted": [customer_words(t) for t in seating.never_counted_types()]
                        if seating
                        else [],
                    },
                )
        return None

    def _seat_facts(
        self, needs: Sequence[DesignCategoryNeed], head_count: int | None
    ) -> dict[str, object]:
        """The seats the design gives, from her seat counts and the reviewed
        one-seat types - a fact for her, never a refusal."""
        seating = self._seating
        if seating is None:
            return {}
        seats = 0
        unknown: list[str] = []
        for need in needs:
            kind = need.commerce_subcategory
            if need.commerce_category != "seating" or not seating.counts_as_seats(kind):
                continue
            if need.seating_capacity is not None and need.seating_capacity.min_capacity:
                seats += need.seating_capacity.min_capacity * need.quantity
            elif seating.seats_one(kind):
                seats += need.quantity
            else:
                unknown.append(need.design.role if need.design else customer_words(kind or ""))
        facts: dict[str, object] = {"seats_planned": seats}
        if unknown:
            facts["seats_unknown_for"] = unknown
        if head_count is not None:
            facts["head_count"] = head_count
            facts["seats_short"] = max(head_count - seats, 0)
        return facts

    def _canonical(self, family: AttributeFamily, stored: Sequence[str]) -> tuple[str, ...]:
        names = (self._attributes.canonical(family, value) for value in stored)
        return tuple(dict.fromkeys(n for n in names if n is not None))
