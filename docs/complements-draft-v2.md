# Draft: pairings for the 16 unpaired product types (for review)

**Status:** proposal only - **not** applied to `app/taxonomy/complements_v1.yaml`.
Written 2026-09-30 while fixing the dead-end replies.

## Why

`complements_v1.yaml` decides "what goes with" a product the customer picks.
Sixteen types store 50 stocks have no entry, so picking one of them can never
offer companions:

art-canvas, candlestick, chandelier, decorative-hanger, floor-lamp, flower,
flower-pot-and-plant, mattresses, mirror, pendant-lighting,
serving-utensil-and-tray, shelve, statue-and-antique, table-lamp, vase,
wall-clock

The dead-end fix already covers them: after such a pick the reply still closes on
a next step ("Shall I design a room around your picks…?" with chips *Design a room
around my picks* and *Keep browsing*). These pairings would add the richer "what
goes with it" offer for them too.

## Proposed pairings

Every companion is a type store 50 stocks. Order is design priority - the first
is shown as cards, the rest as chips. Please change freely; this is a first
design pass, not reviewed data.

| Picked type | Companions, in order | Reasoning |
|---|---|---|
| floor-lamp | side tables, accent chairs, rugs | A floor lamp lights a reading corner: a chair, a side table, a rug under it |
| table-lamp | nightstands, side tables, consoles | A table lamp needs a surface to stand on |
| chandelier | dining tables, center tables, rugs | Hangs over the table the room centres on |
| pendant-lighting | dining tables, consoles | Over a dining table or an entry console |
| mirror | consoles, vases, table lamps | The classic entry vignette: console, mirror, lamp, vase |
| art-canvas | consoles, sofas, vases | Art hangs over a console or a sofa |
| wall-clock | consoles, art canvases | Part of the same wall composition |
| decorative-hanger | consoles, mirrors | Entryway pieces |
| vase | consoles, center tables, flowers | Where a vase sits, and what goes in it |
| flower | vases | What holds them |
| flower-pot-and-plant | side tables, consoles | A surface for the plant |
| statue-and-antique | shelves, consoles, vases | Display surfaces and companion decor |
| candlestick | center tables, vases | Styled together on a table |
| shelve | vases, statues, candlesticks | What goes on a shelf |
| serving-utensil-and-tray | center tables, dining tables | Where a tray is used |
| mattresses | beds, nightstands | The bed it goes on, then the bedside |

## As YAML, ready to paste once approved

```yaml
  floor-lamp:
    - {category: tables, subcategory: service-table, label: side tables}
    - {category: seating, subcategory: chair, label: accent chairs}
    - {category: decor, subcategory: carpet, label: rugs}
  table-lamp:
    - {category: tables, subcategory: nightstand, label: nightstands}
    - {category: tables, subcategory: service-table, label: side tables}
    - {category: tables, subcategory: console, label: consoles}
  chandelier:
    - {category: dining, subcategory: dining-table, label: dining tables}
    - {category: tables, subcategory: center-table, label: center tables}
    - {category: decor, subcategory: carpet, label: rugs}
  pendant-lighting:
    - {category: dining, subcategory: dining-table, label: dining tables}
    - {category: tables, subcategory: console, label: consoles}
  mirror:
    - {category: tables, subcategory: console, label: consoles}
    - {category: decor, subcategory: vase, label: vases}
    - {category: lighting, subcategory: table-lamp, label: table lamps}
  art-canvas:
    - {category: tables, subcategory: console, label: consoles}
    - {category: seating, subcategory: sofa, label: sofas}
    - {category: decor, subcategory: vase, label: vases}
  wall-clock:
    - {category: tables, subcategory: console, label: consoles}
    - {category: decor, subcategory: art-canvas, label: wall art}
  decorative-hanger:
    - {category: tables, subcategory: console, label: consoles}
    - {category: decor, subcategory: mirror, label: mirrors}
  vase:
    - {category: tables, subcategory: console, label: consoles}
    - {category: tables, subcategory: center-table, label: center tables}
    - {category: decor, subcategory: flower, label: flowers}
  flower:
    - {category: decor, subcategory: vase, label: vases}
  flower-pot-and-plant:
    - {category: tables, subcategory: service-table, label: side tables}
    - {category: tables, subcategory: console, label: consoles}
  statue-and-antique:
    - {category: storage, subcategory: shelve, label: shelves}
    - {category: tables, subcategory: console, label: consoles}
    - {category: decor, subcategory: vase, label: vases}
  candlestick:
    - {category: tables, subcategory: center-table, label: center tables}
    - {category: decor, subcategory: vase, label: vases}
  shelve:
    - {category: decor, subcategory: vase, label: vases}
    - {category: decor, subcategory: statue-and-antique, label: statues}
    - {category: decor, subcategory: candlestick, label: candlesticks}
  serving-utensil-and-tray:
    - {category: tables, subcategory: center-table, label: center tables}
    - {category: dining, subcategory: dining-table, label: dining tables}
  mattresses:
    - {category: bedroom, subcategory: bed, label: beds}
    - {category: tables, subcategory: nightstand, label: nightstands}
```

## Before applying

- The loader validates every pair against the taxonomy at startup, so a typo
  fails fast.
- **Side effect to decide on:** a type that gains pairings also becomes a
  "kind" for the second-pick rule. Picking a vase, then a candlestick, would
  count as two options *only* if they share companions - with the table above
  they do not, so each would get its own "what goes with it".
