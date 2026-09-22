"""What one conversation keeps between requests.

An **outer runtime envelope** around the agent state, deliberately not a change
to it. `AgentStateV1` is domain truth - what the customer wants, what they were
shown, what is in their room - and it is versioned by its own schema. Session
revision, TTL and history bounds are facts about the transport, and a field for
one of them inside the agent state would let a runtime concern reach code that
reasons about furniture (CLAUDE.md 19).

The three pieces travel together because they are one snapshot. Persisting the
state without the history it was produced beside would leave a later turn
resolving "the second one" against products from one turn and language from
another.

Two versions, and they are not the same version:

* ``AGENT_STATE_VERSION`` is the agent state's, and the agent state owns it.
* ``SESSION_ENVELOPE_VERSION`` is this wrapper's. It moves when the *envelope*
  gains or loses a field, which can happen without the state changing at all.

Both fail closed. An envelope written by a newer deployment is refused rather
than read partially, because a field this version cannot see is a fact it would
silently drop.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.agent_state import AgentStateV1
from app.schemas.conversation import ConversationContext

SESSION_ENVELOPE_VERSION = "session_v1"


class SessionEnvelope(BaseModel):
    """One conversation's runtime snapshot, exactly as stored.

    Carries no `store_id` and no `session_id`. Both are in the key this was
    read from, and a copy inside the value could disagree with it - at which
    point one retailer's session could be served under another's scope. The key
    is the single answer (M13 5).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    envelope_version: str
    """Required, with no default, so a truncated payload cannot be read as
    current. A record that does not say what wrote it is one this version
    cannot claim to understand."""

    session_revision: int = Field(default=0, ge=0)
    """How many turns have been persisted for this session.

    Runtime concurrency metadata, and **not** `bundle_revision`, not a search
    revision and not an agent-state revision. Those describe what happened to a
    room or a search; this one describes what happened to the session record,
    and it is the only one a client ever sees (M13 15).

    Zero means nothing has been persisted yet - a session that exists only in
    this request.
    """

    state: AgentStateV1
    conversation: ConversationContext
    """Both required, and deliberately not defaulted.

    A defaulted `state` is the silent reset this contract exists to prevent: a
    stored payload that lost the field would validate into an *empty room*, and
    the next turn would resolve "the second one" against nothing while looking
    for all the world like a valid session. Absent means unreadable, and
    unreadable is refused (M13 7).
    """

    @model_validator(mode="after")
    def _the_envelope_version_is_one_we_understand(self) -> Self:
        """Refused rather than read partially.

        A newer deployment may have written fields this one has no model for.
        `extra="forbid"` would already reject them, but the version check says
        *why* - and says it before a field-level error makes a version skew
        look like corruption.
        """
        if self.envelope_version != SESSION_ENVELOPE_VERSION:
            raise ValueError(f"unsupported session envelope version: {self.envelope_version!r}")
        return self

    def advanced(
        self, *, state: AgentStateV1, conversation: ConversationContext
    ) -> SessionEnvelope:
        """The next snapshot, one revision on.

        The only way the revision moves, so it cannot be set to an arbitrary
        number by a caller assembling an envelope by hand.
        """
        return SessionEnvelope(
            envelope_version=SESSION_ENVELOPE_VERSION,
            session_revision=self.session_revision + 1,
            state=state,
            conversation=conversation,
        )


def new_session() -> SessionEnvelope:
    """A conversation nobody has had yet.

    Revision zero, empty state, no history. Deliberately a function rather than
    a module-level constant: `AgentStateV1` is frozen, but a shared default
    instance would make "the new session" a single object that every test and
    request compares identity against.
    """
    return SessionEnvelope(
        envelope_version=SESSION_ENVELOPE_VERSION,
        state=AgentStateV1(),
        conversation=ConversationContext(),
    )
