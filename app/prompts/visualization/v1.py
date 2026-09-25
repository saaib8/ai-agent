"""Room-render instructions, version 1.

A versioned application asset (CLAUDE.md 28). Deterministic: the same package,
room and view always produce the same prompt, and nothing in it is written by a
language model. Every product named comes from the catalog read.

What the wording is for, learned from rendering real packages:

* **"Exactly these products and nothing else."** Image models furnish rooms
  generously - bedding, plants, books on a nightstand. Every addition is a
  product the customer cannot buy here.
* **The photo decides appearance.** Catalog colours are sometimes wrong (a
  sage-green lamp recorded as "ivory"); a model that trusts the text draws the
  wrong lamp. Text identifies a piece and gives its size; the photo shows it.
* **The closing check is the last line**, because a trailing instruction
  weighs more than the same words buried mid-prompt.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.schemas.visualization import RenderView

VERSION = "visualization/v1"

VIEWS: dict[RenderView, tuple[str, str]] = {
    RenderView.CORNER: (
        "Corner",
        "wide-angle interior photograph taken from one corner of the room at chest "
        "height (about 120 cm), the two opposite walls visible with natural one-point "
        "perspective, the camera framing the entire floor so every listed product is "
        "fully visible.",
    ),
    RenderView.EYE_LEVEL: (
        "Eye-level",
        "eye-level interior photograph from standing height (about 160 cm), wide-angle "
        "lens facing the main furniture arrangement straight on, every listed product "
        "visible in the frame.",
    ),
    RenderView.ISOMETRIC: (
        "Isometric",
        "isometric three-quarter aerial view from an upper corner of the room, camera "
        "pitched about 45 degrees downward so the whole floor layout reads clearly, two "
        "walls visible, every listed product fully visible with none hidden behind "
        "another.",
    ),
    RenderView.TOP_DOWN: (
        "Top-down",
        "strict top-down orthographic view, like a photorealistic floor plan - camera "
        "directly overhead pointing straight down, the entire floor visible, every "
        "listed product fully visible and none cropped or occluded, walls shown as a "
        "thin outline.",
    ),
}


@dataclass(frozen=True, slots=True)
class RenderPiece:
    """One package line as the prompt describes it."""

    reference: int | None
    """The 1-based number of its photo among the images sent, or None when no
    photo could be fetched and the piece must be drawn from its description."""

    name: str
    kind: str
    size_cm: tuple[float, ...]
    quantity: int


@dataclass(frozen=True, slots=True)
class RenderRoom:
    room_label: str
    style: str | None
    length_m: float | None
    width_m: float | None


def view_label(view: RenderView) -> str:
    return VIEWS[view][0]


def build_prompt(room: RenderRoom, pieces: tuple[RenderPiece, ...], view: RenderView) -> str:
    units = sum(piece.quantity for piece in pieces)
    style = f"{room.style} " if room.style else ""
    size = (
        f", approximately {room.length_m:.1f} m by {room.width_m:.1f} m"
        if room.length_m and room.width_m
        else ""
    )
    lines = [
        f"Create a single photorealistic render of a {style}{room.room_label}{size}.",
        "",
        f"Furnish the room with EXACTLY these catalog products and NOTHING else "
        f"({units} pieces in total). Do not add any furniture, decor or accessories "
        "that are not listed - no bedding, extra rugs, curtains, plants, books, "
        "ornaments, wall art, lamps, TVs, cushions or objects of any kind.",
        "Every listed product must be visible, each the stated number of times. None "
        "may be omitted, merged, hidden behind another piece or cropped out of frame. "
        "If the room looks tight, widen the camera - never drop a piece.",
        "Apart from the listed products the room is a bare architectural shell: plain "
        "walls, plain flooring, one window, one door, empty floor everywhere else.",
        "Reproduce each product faithfully from its reference image - same shape, "
        "proportions, material and colour. The image is the authority on how a product "
        "looks; the text below only identifies it and gives its approximate size.",
        *(_piece_line(piece) for piece in pieces),
        "",
        "Arrange the pieces the way an interior designer would for this room, with "
        "realistic walkways and clearances.",
        "Keep walls and flooring in neutral tones that harmonise with the furniture.",
        "",
        f"Photography: {VIEWS[view][1]} Soft natural daylight, realistic scale, no "
        "people, no text, no watermarks, no brand logos.",
        "",
        f"Before finishing, check that all {units} listed pieces are present and "
        "identifiable, and that nothing unlisted was added.",
    ]
    return "\n".join(lines)


def reference_caption(piece: RenderPiece) -> str:
    """The label that travels with a piece's photo, for models that take one."""
    return f"Image {piece.reference}: {piece.name} ({piece.kind})"


def _piece_line(piece: RenderPiece) -> str:
    details = [piece.kind]
    if piece.size_cm:
        details.append(" x ".join(f"{v:.0f}" for v in piece.size_cm) + " cm")
    count = f" - {piece.quantity} of them" if piece.quantity > 1 else ""
    described = f"{piece.name} ({', '.join(details)}){count}"
    if piece.reference is None:
        return f"- (no photo) {described}: draw it faithfully from this description."
    return f"- image {piece.reference}: {described}"
