"""The words a client draws around products, before any reply has carried them.

Every chat reply carries its `labels`; a store's widget opening in Arabic asks
here for them before the customer has said anything (CLAUDE.md 25).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import ResourcesDep
from app.schemas.card_labels import CardLabels
from app.schemas.language import ReplyLanguage
from app.services.card_labels import card_labels

router = APIRouter(tags=["labels"])


@router.get("/labels", response_model=CardLabels)
async def labels(
    app_resources: ResourcesDep,
    language: Annotated[ReplyLanguage | None, Query()] = None,
) -> CardLabels:
    """The card, tray and catalogue words in `language` - English while Arabic
    replies are off, as every reply is."""
    arabic = app_resources.settings.customer_agent.arabic_replies
    return card_labels(language if arabic else None)
