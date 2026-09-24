# Quant Data — Options Order Flow "Type" Column

> **Source:** Quant Data help centre, "Understanding the 'Type' Column on the Options Order Flow."
> Andrew Hiesinger, 2024-08-31. Saved 2026-09-17.
> Vendor sign-up and support copy trimmed; all type definitions reproduced verbatim.

The Type column on the Options Order Flow table records the **method of execution** used
by the buyer/seller — not the direction, size, or aggression of the trade.

---

## Definitions (verbatim)

| Type | Definition |
|---|---|
| `LATE` (aka `ADJ_LAST`) | A trade that was the result of late reporting due to sets consolidating last. |
| `AUTO` | A common occurrence, trades that are executed electronically. |
| `ISO` | A trade that is an Intermarket sweep order. ISO's are executed immediately and do not have to wait for the best bid and offer. |
| `AUCT` | A trade that was executed electronically but traded through a two-sided auction with an exposure period. |
| `ISO/AUCT` | A trade that is an Intermarket sweep order that executed electronically and traded through a two-sided auction with an exposure period. |
| `CROSS` | A trade that was executed electronically and traded through a two-sided crossing mechanism with no exposure period. |
| `CROSS/ISO` | A trade that is an Intermarket sweep order that executed electronically through a two-sided crossing mechanism with no exposure period. |
| `FLR` | A trade that is not executed electronically but rather executed on a trading floor. |
| `COB` | A multi-leg order trade that is executed electronically through a complex order book. |
| `COB/AUCT` | A multi-leg order trade that is executed electronically through a two-sided action that goes through an exposure period in a complex order book. |
| `SPRD/CROSS` | A multi-leg order trade that is executed electronically through a two-sided crossing mechanism that does not go through an exposure period. |
| `SPRD/FLR` | A multi-leg order trade that is not executed electronically but rather executed on a trading floor against other multi-leg orders executed on a trading floor. |
| `SPRD/LEG/AUTO` | A multi-leg order trade that executed electronically against single-leg orders/quotes. |
| `SPRD/COB` | A multi-leg order trade that executed electronically through a two-sided auction that goes through an exposure period in a complex order book. |
| `SPRD/LEG/AUCT` | A multi-leg order trade that executed electronically through a two-sided auction that goes through an exposure period and trades against single-leg orders/quotes. |
| `SPRD/LEG/FLR` | A multi-leg order trade that is not executed electronically but rather executed on a trading floor against single-leg orders/quotes. |
| `TIED/AUTO` | A multi-leg order trade that executed electronically through a complex order book. |
| `SPRD/CROSS/TIED` | A multi-leg order trade that was executed electronically through a two-sided crossing mechanism that does not go through an exposure period. |
| `TIED/FLR` | A multi-leg order trade that is not executed electronically but rather executed on a trading floor in a complex order book. |
| `SPRD/TIED/AUTO` | A multi-leg order trade that is executed electronically against single-order orders/quotes. |
| `SPRD/TIED/AUCT` | A multi-leg order trade that is executed electronically through a two-sided auction with an exposure period and trades against single-leg orders/quotes. |
| `SPRD/TIED/FLR` | A multi-leg order trade that is not executed electronically but rather executed on a trading floor against single-leg orders/quotes. |
| `SPRD/FLR/PP` | A multi-leg order trade proprietary product with at least 3 legs that are not executed electronically, price can be outside of the NBBO. |

---

## Derived classification

Not in the source — inferred from the definitions above for use as a flow filter.

| Class | Types | Directional read |
|---|---|---|
| **Single-leg, aggressive** | `ISO`, `ISO/AUCT`, `CROSS/ISO` | Cleanest. An ISO crosses protected quotes to get filled now — the buyer accepted worse prices for immediacy. |
| **Single-leg, ordinary** | `AUTO` | Direction readable, but no urgency signal. |
| **Single-leg, negotiated** | `AUCT`, `CROSS` | Often facilitation or pre-arranged. Weak information content. |
| **Multi-leg (spread)** | every type containing `COB`, `SPRD`, or `TIED` | **Direction is ambiguous.** The print is one leg of a structure; the other leg may invert the implied bias. |
| **Floor** | `FLR`, `SPRD/FLR`, `TIED/FLR`, `SPRD/LEG/FLR`, `SPRD/TIED/FLR`, `SPRD/FLR/PP` | Non-electronic, typically negotiated size. |
| **Reporting artefact** | `LATE` / `ADJ_LAST` | Timestamp is unreliable — do not treat as live flow. |

### Why this matters for the entry framework

The flow gate needs a "not a spread leg" filter — a single-leg-looking print that is
actually one side of a vertical carries no directional information. **The Type column is
that filter.** Excluding every type containing `COB`, `SPRD`, or `TIED` removes multi-leg
prints without needing to detect offsetting fills by timestamp.

A "golden sweep" in the strict sense is an **`ISO`** — the intermarket sweep order.
`AUTO` is just an ordinary electronic fill and should not be read as a sweep.

---

## Notes on the source

Several definitions appear to be duplicated or mistyped in the vendor's article.
Preserved verbatim above; flagged here because a parser built on them will hit the overlap:

- `TIED/AUTO` is given the same definition as `COB` ("through a complex order book"),
  despite `TIED/*` elsewhere meaning tied-to-single-leg-orders.
- `SPRD/COB` is given the same definition as `COB/AUCT` ("two-sided auction ... exposure
  period in a complex order book").
- `SPRD/TIED/AUTO` reads "against single-order orders/quotes" — apparently a typo for
  "single-leg", and duplicates `SPRD/LEG/AUTO`.
- `COB/AUCT` reads "two-sided action" — apparently a typo for "auction".

Treat the classification table above as the working interpretation, not the vendor's.

## Open item

`multi_auto_cob` does not appear as a literal value in this article. The nearest entry is
`COB`. Likely an API-field encoding (multi-leg / auto / complex order book) rather than the
display string. **Unconfirmed** — verify against a raw feed record before relying on it.

---

## Related articles (same help centre)

- What are Blocks, Splits, and Sweeps?
- What is Options Trading?
- What is the Bid-Ask Spread?
- What is Net Flow?
- Mastering the Net Drift Tool: Leveraging Order Flow Sentiment for Smarter Options Trading
