"""Photo and render acknowledgements, worded without another model call."""

from app.schemas.agent_turn import CustomerResponse
from app.schemas.language import ReplyLanguage
from app.schemas.text_choice import TextReplyChoice
from app.schemas.visualization import RenderView, RoomType

EN, AR = ReplyLanguage.EN, ReplyLanguage.AR

VIEW_PHRASES: dict[ReplyLanguage, dict[RenderView, str]] = {
    EN: {
        RenderView.CORNER: "seen from the corner",
        RenderView.EYE_LEVEL: "at eye level",
        RenderView.ISOMETRIC: "from above",
        RenderView.TOP_DOWN: "from directly overhead",
    },
    AR: {
        RenderView.CORNER: "من إحدى الزوايا",
        RenderView.EYE_LEVEL: "على مستوى النظر",
        RenderView.ISOMETRIC: "من منظور علوي مائل",
        RenderView.TOP_DOWN: "من الأعلى مباشرة",
    },
}

ARABIC_ROOMS: dict[str, str] = {
    RoomType.LIVING_ROOM: "غرفة المعيشة",
    RoomType.BEDROOM: "غرفة النوم",
    RoomType.DINING_ROOM: "غرفة الطعام",
    RoomType.HOME_OFFICE: "المكتب المنزلي",
    RoomType.KIDS_ROOM: "غرفة الأطفال",
    RoomType.MAJLIS: "المجلس",
    RoomType.ENTRYWAY: "المدخل",
}


def photo_reply(
    label: str, count: int, language: ReplyLanguage, *, comparable: bool = True
) -> CustomerResponse:
    """`comparable`: whether the first two matches are kinds that compare
    (CLAUDE.md 10.4) - otherwise "compare the first two" is not offered."""
    choices = _photo_choices(count, language, comparable=comparable)
    if language is AR:
        # Refer to the selected item rather than guessing an Arabic detection label.
        if count == 0:
            return CustomerResponse(
                message="لم أجد في هذا الكتالوج منتجات تشبه القطعة المحددة في صورتك.",
                follow_up_question="هل تودّ أن أبحث عن قطعة مشابهة بالوصف بدلًا من الصورة؟",
                choices=choices,
            )
        return CustomerResponse(
            message=f"إليك {_closest_products(count)} إلى القطعة المحددة في صورتك.",
            follow_up_question=(
                "هل تودّ معرفة المزيد عن هذا المنتج؟"
                if count == 1
                else "هل تودّ مقارنة هذه المنتجات، أو معرفة المزيد عن أحدها؟"
            ),
            choices=choices,
        )
    if count == 0:
        return CustomerResponse(
            message=(
                f"I couldn't find anything in this catalog that looks like the {label} "
                "in your photo."
            ),
            follow_up_question="Would you like me to search for one by description instead?",
            choices=choices,
        )
    return CustomerResponse(
        message=(
            f"Here is the closest match to the {label} in your photo."
            if count == 1
            else f"Here are the {count} closest matches to the {label} in your photo."
        ),
        follow_up_question=(
            "Would you like to hear more about this product?"
            if count == 1
            else "Would you like to compare any of these, or hear more about one?"
        ),
        choices=choices,
    )


def _photo_choices(
    count: int, language: ReplyLanguage, *, comparable: bool = True
) -> tuple[TextReplyChoice, ...]:
    if count == 0:
        pairs = (
            [("نعم", "نعم، ابحث بناءً على وصف"), ("لا", "لا، سأجرب صورة أخرى")]
            if language is AR
            else [("Yes", "Yes, search by description"), ("No", "No, I'll try another photo")]
        )
    else:
        pairs = (
            [("تفاصيل المنتج الأول", "أخبرني المزيد عن المنتج الأول")]
            if language is AR
            else [("More about the first one", "Tell me more about the first one")]
        )
        if count >= 2 and comparable:
            pairs.insert(
                0,
                ("قارن أول منتجين", "قارن أول منتجين")
                if language is AR
                else ("Compare the first two", "Compare the first two"),
            )
    return tuple(TextReplyChoice(label=label, value=value) for label, value in pairs)


def render_reply(
    room: str, view: RenderView, language: ReplyLanguage, *, dropped: int = 0
) -> CustomerResponse:
    phrase = VIEW_PHRASES[language][view]
    message = f"إليك تصوّر {room}، {phrase}." if language is AR else f"Here's your {room}, {phrase}."
    return CustomerResponse(message=message + _left_out(dropped, language))


def room_photo_reply(
    room: str | None, language: ReplyLanguage, *, dropped: int = 0
) -> CustomerResponse:
    """A render placed in the customer's own room photo: no view is named - the
    photo is the camera - and a room only when the pieces are a room package."""
    if language is AR:
        message = (
            f"إليك تصوّر {room} داخل صورة غرفتك."
            if room is not None
            else "إليك القطع التي اخترتها داخل صورة غرفتك."
        )
    else:
        message = (
            f"Here's your {room} package, placed in your own room."
            if room is not None
            else "Here are the pieces you picked, placed in your own room."
        )
    return CustomerResponse(message=message + _left_out(dropped, language))


def _left_out(dropped: int, language: ReplyLanguage) -> str:
    """The pieces a render could not include, said after it; empty for none."""
    if not dropped:
        return ""
    if language is AR:
        if dropped == 1:
            return " إحدى القطع التي اخترتها لم تعد متوفرة، لذا لم أدرجها في الصورة."
        # Two pieces take the dual; more, the feminine singular of a
        # non-human plural.
        reason = "لأنهما لم تعودا متوفرتين" if dropped == 2 else "لأنها لم تعد متوفرة"
        return f" لم أدرج {_pieces(dropped)} مما اخترته في الصورة {reason}."
    if dropped == 1:
        return " 1 piece you picked is no longer available, so I left it out."
    return f" {dropped} pieces you picked are no longer available, so I left them out."


def _closest_products(count: int) -> str:
    """"the closest product(s)", with Arabic's number agreement: one, two
    (the dual), three to ten (plural), eleven and more (singular)."""
    if count == 1:
        return "أقرب منتج"
    if count == 2:
        return "أقرب منتجين"
    if count <= 10:
        return f"أقرب {count} منتجات"
    return f"أقرب {count} منتجًا"


def _pieces(count: int) -> str:
    """"n pieces", with the same agreement; never called for one."""
    if count == 2:
        return "قطعتين"
    if count <= 10:
        return f"{count} قطع"
    return f"{count} قطعة"
