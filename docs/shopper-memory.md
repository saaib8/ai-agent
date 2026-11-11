# Shopper memory: what they turned down and what they saw

Two sheet rows: **Remember Rejected Products** (169) and **Remember Previously
Viewed Products** (166).

## What a customer gets

- **"Not this one"** (`leave_out_product`) is remembered. Every later search
  leaves that product out - Sara's searches and their pages, "more like this",
  Salman's suggestions, and the pieces of a room Nora designs - in this chat and
  the shopper's next chats.
- **The "no" is easy to take back.** Liking the product, adding it to the cart,
  or asking to see it again ("show me that grey sofa I passed on" -
  `product_details` on a `turned_down_product`) takes it off the list. The
  reason they gave ("too dark") is kept apart, as taste (`remember_taste`), so it
  pushes similar products down without hiding them.
- **What they were shown can be found again**: "the lamp I saw before" is
  `product_details` on `seen_before`, from this chat or an earlier one. Seeing a
  product again in a new search is fine - this list never hides anything.

## How it is kept (layer 1, built)

| | Where | Cap | Lifetime |
|---|---|---|---|
| Turned down | `ProductInteractionState.turned_down_product_ids` | 30, newest kept | the chat, and the shopper record |
| Seen | `ProductInteractionState.seen_product_ids` | 50, newest kept | the chat, and the shopper record |

- Changed only through the reducers in `app/services/agent_state.py`
  (`turn_down`, `take_back`, `commit_search_results`); liking or adding takes a
  product back inside the same reducer that records the like or the add.
- Searches leave them out in one place, `SearchRunner.run`
  (`app/agents/operations/search.py`), on the search as it runs - the search
  kept on record stays the one they asked for, so "show me more" still pages it.
  Room pieces leave them out through `DesignDiscoveryService.discover(leave_out=)`
  and a piece's options.
- They travel from chat to chat in the shopper record beside the cart and likes
  (`ShopperCart`, `session.shopper_cart_ttl_s`, seven days), and follow a guest
  who signs in (`merged_cart`). An id naming no shopper (the console) keeps them
  per chat.
- Every brief names them in the order they happened (`turned_down`, and the
  latest 12 of `seen_before`), so "the second lamp I saw" is the second lamp
  as it was shown.

Bounded by design: a shopper's record never grows however long they browse, the
exclusion is a short `NOT IN` on the query that already runs, and nothing is
read that the turn did not already read.

## The event log (the bridge to layer 2)

Every change is one structured log line, `shopper_product_event`
(`app/services/shopper_memory.py`): `shopper_event` (`seen`, `turned_down`,
`taken_back`), `store_id` and the `product_ids` - references only.

## Layer 2 (not built): a durable event table

An append-only table - store, shopper, product, what happened, when - fed by the
same calls that log today. It would answer the long-term rows (remember across
sessions for good, a preference profile) and the analytics rows (most viewed,
most turned down, recommendation performance). It needs one decision first:
CLAUDE.md 4 makes Django the owner of the database schema, so the table is
either created there or the agent service is agreed to own this one table.
