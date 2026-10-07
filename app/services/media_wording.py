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
        RenderView.CORNER: "من زاوية الغرفة",
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


def photo_reply(label: str, count: int, language: ReplyLanguage) -> CustomerResponse:
    choices = _photo_choices(count, language)
    if language is AR:
        # Refer to the selected item rather than guessing an Arabic detection label.
        if count == 0:
            return CustomerResponse(
                message="لم أجد في هذا الكتالوج منتجات تشبه العنصر المحدد في صورتك.",
                follow_up_question="هل تودّ أن أبحث عنه بناءً على وصف بدلًا من الصورة؟",
                choices=choices,
            )
        return CustomerResponse(
            message=(
                "إليك أقرب منتج للعنصر المحدد في صورتك."
                if count == 1
                else f"وجدت {count} من المنتجات الأقرب للعنصر المحدد في صورتك."
            ),
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


def _photo_choices(count: int, language: ReplyLanguage) -> tuple[TextReplyChoice, ...]:
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
        if count >= 2:
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
    if language is AR:
        message = f"إليك تصوّر {room}، {phrase}."
        if dropped == 1:
            message += " إحدى القطع التي اخترتها لم تعد متوفرة، لذا لم أدرجها في الصورة."
        elif dropped:
            message += f" لم أدرج {dropped} من القطع التي اخترتها في الصورة لأنها لم تعد متوفرة."
    else:
        message = f"Here's your {room}, {phrase}."
        if dropped == 1:
            message += " 1 piece you picked is no longer available, so I left it out."
        elif dropped:
            message += f" {dropped} pieces you picked are no longer available, so I left them out."
    return CustomerResponse(message=message)
