"""What a tap on the cart or a ♡ says when it cannot be done, in the
conversation's language (CLAUDE.md 25, 36).

The widget shows the error's message as it is, so the words are the
server's, like every other label a client draws around the conversation. A
figure in one is the store's limit or how many are left, written in Western
digits like every figure on the cards.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

from app.schemas.language import ReplyLanguage

WORDS: Final = MappingProxyType(
    {
        ReplyLanguage.EN: MappingProxyType(
            {
                "gone": "That product is no longer available, so it can't be picked.",
                "gone_like": "That product is no longer available, so it can't be liked.",
                "off_screen_like": (
                    "That card isn't on screen any more, so it can't be liked from here."
                ),
                "likes_off": "Likes aren't available here.",
                "room_option": (
                    "These are options for a piece of your room - choose 'Use this' to put "
                    "one in the room."
                ),
                "cart_full": "Your cart holds up to {max} products. Remove one to add another.",
                "not_in_cart": "That product is no longer in your cart.",
                "not_in_liked": "That is no longer in your liked list.",
                "max_quantity": "You can add up to {max} of one product.",
                "only_left": "Only {count} of this product left in the store.",
                "out_of_stock": "That product is out of stock.",
                "over_limit": "The store takes up to {max} of this product per order.",
                "not_in_store": "That product can't be added to this store's cart.",
                "unconfirmed": "I couldn't check that with the store just now. Please try again.",
            }
        ),
        ReplyLanguage.AR: MappingProxyType(
            {
                "gone": "هذا المنتج لم يعد متوفرًا، لذا لا يمكن اختياره.",
                "gone_like": "هذا المنتج لم يعد متوفرًا، لذا لا يمكن الإعجاب به.",
                "off_screen_like": "هذه البطاقة لم تعد معروضة، لذا لا يمكن الإعجاب بها من هنا.",
                "likes_off": "الإعجاب غير متاح هنا.",
                "room_option": "هذه خيارات لقطعة في غرفتك - اختر «استخدم هذا» لوضعها في الغرفة.",
                "cart_full": "تتسع سلتك حتى {max} من المنتجات. أزل منتجًا لإضافة آخر.",
                "not_in_cart": "هذا المنتج لم يعد في سلتك.",
                "not_in_liked": "هذا المنتج لم يعد في قائمة إعجاباتك.",
                "max_quantity": "يمكنك إضافة حتى {max} من المنتج الواحد.",
                "only_left": "المتبقي من هذا المنتج في المتجر: {count} فقط.",
                "out_of_stock": "هذا المنتج غير متوفر حاليًا.",
                "over_limit": "يقبل المتجر حتى {max} من هذا المنتج في الطلب الواحد.",
                "not_in_store": "لا يمكن إضافة هذا المنتج إلى سلة هذا المتجر.",
                "unconfirmed": "لم أتمكن من التحقق من ذلك مع المتجر الآن. حاول مرة أخرى من فضلك.",
            }
        ),
    }
)


def say(language: ReplyLanguage, key: str, **values: object) -> str:
    """The words for `key` in `language`, with its figures filled in."""
    return WORDS[language][key].format(**values)
