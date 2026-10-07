"""The application's own sentences, in Arabic (docs/arabic-replies-plan.md).

Keyed by the English sentence, as a message catalog is: the English tables in
`response_wording` and `next_step` stay the single source of what each sentence
says, and this holds only how it reads in Arabic. A test proves the catalog is
total - every English sentence has its Arabic, and no Arabic entry has lost its
English - so editing a sentence without translating it fails the build, never a
customer.

The English rules hold here too: no product fact, no figure and no digit, and a
question only where the English asks one. The customer is addressed in the
masculine form, as the reply writer is told to (CLAUDE.md 10.2).

Reviewed by a native speaker before this reaches production (plan 3.4).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from app.core.logging import get_logger
from app.schemas.language import ReplyLanguage

logger = get_logger(__name__)

ARABIC: Mapping[str, str] = MappingProxyType(
    {
        # ── a turn that could not be done ──────────────────────────────────
        "I wasn't able to run that search just now. Please try again in a moment.": (
            "لم أتمكن من إجراء هذا البحث الآن. يُرجى المحاولة مرة أخرى بعد قليل."
        ),
        "I wasn't able to pull up that product just now.": "لم أتمكن من عرض هذا المنتج الآن.",
        "I wasn't able to put those side by side just now.": (
            "لم أتمكن من مقارنة هذه المنتجات الآن."
        ),
        "I wasn't able to work out which product you meant.": (
            "لم أتمكن من تحديد المنتج الذي تقصده."
        ),
        "Those questions have moved on. Tell me what you're looking for and I'll find it.": (
            "تلك الأسئلة لم تعد مطروحة. أخبرني بما تبحث عنه وسأبحث لك عنه."
        ),
        "I wasn't able to put a reply together just now.": "لم أتمكن من إعداد رد الآن.",
        "One of the pieces you asked me to keep isn't available any more, so I wasn't able "
        "to plan the room around it.": (
            "إحدى القطع التي طلبت الاحتفاظ بها لم تعد متوفرة، لذلك لم أتمكن من تخطيط الغرفة حولها."
        ),
        "I couldn't check everything currently in your room, so I wasn't sure which piece "
        "you meant.": (
            "لم أتمكن من التحقق من كل ما في غرفتك حاليًا، لذلك لم أكن متأكدًا من القطعة التي تقصدها."
        ),
        "I couldn't find another one of those to offer you, so I've left your current choice "
        "as it is.": "لم أجد بديلًا آخر من هذا النوع أعرضه عليك، لذلك أبقيت اختيارك الحالي كما هو.",
        "I found alternatives, but none of them works alongside the rest of the room, so I've "
        "left your current choice as it is.": (
            "وجدت بدائل، لكن لا يصلح أيٌّ منها مع بقية الغرفة، لذلك أبقيت اختيارك الحالي كما هو."
        ),
        "You haven't picked anything out yet, so there's nothing to show you here.": (
            "لم تختر أي شيء بعد، لذلك لا يوجد ما أعرضه عليك هنا."
        ),
        "Sorry, I didn't quite catch that. Try saying it a little differently - for example, "
        "the kind of piece you're after, or what you'd like to change about what you're "
        "looking at.": (
            "عذرًا، لم أفهم ذلك تمامًا. جرّب أن تصيغه بطريقة أخرى - مثلًا نوع القطعة"
            " التي تبحث عنها، أو ما تودّ تغييره فيما تراه أمامك."
        ),
        "I wasn't able to answer that one just now. Please try again in a moment.": (
            "لم أتمكن من الإجابة عن ذلك الآن. يُرجى المحاولة مرة أخرى بعد قليل."
        ),
        "I wasn't able to put a room plan together just now. Please try again in a moment.": (
            "لم أتمكن من إعداد مخطط للغرفة الآن. يُرجى المحاولة مرة أخرى بعد قليل."
        ),
        # ── a room that could not be worked out ────────────────────────────
        "I can work with a maximum you'd like to stay under, but not with the budget as "
        "you've described it, so I wasn't able to put the room together yet.": (
            "أستطيع التخطيط بناءً على حدٍّ أقصى لا تودّ تجاوزه، لكن ليس على أساس الميزانية بالصيغة"
            " التي وصفتها، لذلك لم أتمكن من تجهيز الغرفة بعد."
        ),
        "One of the pieces you asked me to keep is priced differently from the budget you "
        "gave me, so I can't compare the two reliably enough to build the package.": (
            "إحدى القطع التي طلبت الاحتفاظ بها مسعّرة على أساس مختلف عن الميزانية التي حددتها،"
            " لذلك لا أستطيع المقارنة بينهما بدقة كافية لتجهيز المجموعة."
        ),
        "I couldn't confirm the current price of one of the pieces you asked me to keep, so "
        "I wasn't able to work out what the room would come to.": (
            "لم أتمكن من التأكد من السعر الحالي لإحدى القطع التي طلبت الاحتفاظ بها، لذلك لم"
            " أستطع حساب تكلفة الغرفة."
        ),
        "I wasn't able to work the room package out just now.": (
            "لم أتمكن من إعداد مجموعة الغرفة الآن."
        ),
        # ── a change to a room ─────────────────────────────────────────────
        "That piece will stay in the room.": "ستبقى هذه القطعة في الغرفة.",
        "That piece can change in later refinements.": (
            "أصبح بالإمكان تغيير هذه القطعة في أي تعديل لاحق."
        ),
        "I'll treat that as something you already have, so it won't count towards what you "
        "spend.": "سأعتبرها قطعة تملكها بالفعل، لذلك لن تُحسب ضمن ما ستنفقه.",
        "I'll count that as something you still need to buy.": (
            "سأحسبها ضمن القطع التي ما زلت تحتاج إلى شرائها."
        ),
        "I've made that change. I wasn't able to work the rest of the room out again just "
        "now, so what you can see may be out of date.": (
            "أجريت هذا التغيير. لم أتمكن من إعادة حساب بقية الغرفة الآن، لذلك قد لا يكون ما"
            " تراه محدّثًا."
        ),
        "I wasn't able to put that together just now.": "لم أتمكن من تجهيز ذلك الآن.",
        # ── a side effect that did not happen ──────────────────────────────
        "I wasn't able to save that selection.": "لم أتمكن من حفظ هذا الاختيار.",
        "I wasn't able to remove that selection.": "لم أتمكن من إزالة هذا الاختيار.",
        "I wasn't able to switch to that product.": "لم أتمكن من الانتقال إلى هذا المنتج.",
        # ── when a reply could not be written ──────────────────────────────
        "I'm not able to answer that just now.": "لا أستطيع الإجابة عن ذلك الآن.",
        "Here's what I found.": "إليك ما وجدته.",
        "I couldn't find anything that matches all of that together. Tell me which part "
        "matters least - the budget, the size or the colour - and I'll widen that one.": (
            "لم أجد ما يطابق كل ذلك معًا. أخبرني أيّ جزء هو الأقل أهمية لك - الميزانية أو"
            " المقاس أو اللون - وسأوسّع البحث فيه."
        ),
        "Here's what you've picked out so far.": "إليك ما اخترته حتى الآن.",
        "Here are the details for that one.": "إليك تفاصيل هذا المنتج.",
        "Here's how those compare.": "إليك المقارنة بينها.",
        "That choice looks lovely in the room - it does bring the total a little over your "
        "budget. Shall we stretch to fit it, or keep within budget?": (
            "يبدو هذا الاختيار جميلًا في الغرفة - لكنه يرفع الإجمالي قليلًا فوق ميزانيتك. هل"
            " نتجاوز الميزانية قليلًا لنضيفه، أم نبقى ضمنها؟"
        ),
        "Good choice - it's saved in your picks. Would you like anything to go with it?": (
            "اختيار موفّق - حفظته في اختياراتك. هل تودّ شيئًا يتناسب معه؟"
        ),
        "Let's find the right one for you. Tap whatever matters below - or skip straight to "
        "the results.": (
            "لنجد القطعة المناسبة لك. اختر ما يهمك من الخيارات أدناه - أو انتقل مباشرة إلى النتائج."
        ),
        "I wasn't able to put that answer into words just now.": (
            "لم أتمكن من صياغة تلك الإجابة الآن."
        ),
        "Could you tell me a little more about what you're after?": (
            "هل يمكنك أن تخبرني بالمزيد عمّا تبحث عنه؟"
        ),
        "Here's a room package covering everything it needs.": (
            "إليك مجموعة متكاملة تغطي كل احتياجات الغرفة."
        ),
        "Here's a partial room package - some of the pieces it needs are still missing.": (
            "إليك مجموعة جزئية للغرفة - ما زالت تنقصها بعض القطع التي تحتاجها الغرفة."
        ),
        "The pieces you asked me to keep don't fit within the budget you gave me, so I "
        "couldn't put a package together around them.": (
            "القطع التي طلبت الاحتفاظ بها لا تتسع لها الميزانية التي حددتها، لذلك لم أتمكن من"
            " تجهيز مجموعة حولها."
        ),
        # ── seating that takes more than one piece ─────────────────────────
        "No single piece seats that many, so I've put together a few combinations that do - "
        "have a look.": (
            "لا توجد قطعة واحدة تتسع لهذا العدد، لذلك جهّزت لك بعض التشكيلات التي تتسع له -"
            " ألقِ نظرة."
        ),
        "No single piece seats that many, but a combination will. Would you like separate "
        "sofas arranged together, or a sofa with a few armchairs?": (
            "لا توجد قطعة واحدة تتسع لهذا العدد، لكن تشكيلة من عدة قطع ستفي بالغرض. هل تفضّل"
            " كنبات منفصلة مرتبة معًا، أم كنبة مع بعض الكراسي المنفردة؟"
        ),
        "Those are all the combinations of that kind I can put together. Would you like to "
        "see a different arrangement instead?": (
            "هذه كل التشكيلات من هذا النوع التي أستطيع تجهيزها. هل تودّ رؤية ترتيب مختلف بدلًا منها؟"
        ),
        "I couldn't reach that many seats within your budget, even by combining pieces. "
        "Tell me which matters more and I'll take it from there.": (
            "لم أتمكن من توفير هذا العدد من المقاعد ضمن ميزانيتك، حتى مع الجمع بين عدة قطع."
            " أخبرني أيّهما أهم لك: عدد المقاعد أم الميزانية، وسأتابع على هذا الأساس."
        ),
        # ── a room's own questions ─────────────────────────────────────────
        "Let's design your room. What would you like to spend on it overall?": (
            "لنصمّم غرفتك. كم تودّ أن تنفق عليها إجمالًا؟"
        ),
        "Here are the pieces I'd put in the room - untick anything you don't need, add "
        "anything you'd like, or tell me to choose for you.": (
            "هذه القطع التي أقترحها للغرفة - ألغِ تحديد ما لا تحتاجه، وأضف ما تريده، أو اطلب"
            " مني أن أختار لك."
        ),
        "How many people will usually be sitting in the room?": ("كم شخصًا سيجلس في الغرفة عادةً؟"),
        "Which colours are you drawn to for the room - or shall I choose for you?": (
            "ما الألوان التي تميل إليها للغرفة - أم تفضّل أن أختار لك؟"
        ),
        # ── the next step a reply ends on ──────────────────────────────────
        "What type of furniture are you looking for?": "ما نوع الأثاث الذي تبحث عنه؟",
        "Which room would you like to design?": "أي غرفة تودّ تصميمها؟",
        "Are you looking for a particular piece, or would you like help designing a whole "
        "room?": "هل تبحث عن قطعة معيّنة، أم تودّ المساعدة في تصميم غرفة كاملة؟",
        "Shall I find what goes with your picks, or design a room around them?": (
            "هل أبحث لك عمّا يتناسب مع اختياراتك، أم أصمّم غرفة حولها؟"
        ),
        "Shall I design a room around your picks, or would you like to keep browsing?": (
            "هل أصمّم غرفة حول اختياراتك، أم تودّ مواصلة التصفح؟"
        ),
        "Would you like to add it to your picks, or see what goes with it?": (
            "هل تودّ إضافته إلى اختياراتك، أم رؤية ما يتناسب معه؟"
        ),
        "Which one are you leaning towards?": "إلى أيّها تميل أكثر؟",
        "Would you like to swap any piece, or add a finishing touch?": (
            "هل تودّ تبديل أي قطعة، أم إضافة لمسة أخيرة؟"
        ),
        "Would you like to narrow these down, or see more options?": (
            "هل تودّ تضييق هذه الخيارات، أم رؤية المزيد؟"
        ),
        "What would you like to do next?": "ماذا تودّ أن نفعل بعد ذلك؟",
    }
)


def in_language(sentence: str, language: ReplyLanguage) -> str:
    """`sentence` as the customer should read it.

    English is returned as it is. A sentence with no Arabic - which the
    catalog's test makes a build failure - is still sent, in English, rather
    than leaving the customer with nothing; it is logged by its length, never
    its text.
    """
    if language is ReplyLanguage.EN:
        return sentence
    arabic = ARABIC.get(sentence)
    if arabic is None:
        logger.warning("fixed_sentence_untranslated", language=str(language), chars=len(sentence))
        return sentence
    return arabic
