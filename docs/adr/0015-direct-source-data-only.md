# ADR-0015: Direct-source data only; FRED retired as a data provider

- **Status:** Accepted
- **Date:** 2026-11-11

## Context

ADR-0008 listed FRED/ALFRED (Federal Reserve Bank of St. Louis) as the source of Treasury
yields, rates, and macroeconomic indicators. FRED republishes data produced by other
organizations (the Board of Governors of the Federal Reserve System, the U.S. Treasury, BLS, BEA,
and others). A FRED connector was built, and 12 series were backfilled into the production
database on 2026-11-01.

On 2026-11-10 a review of FRED's terms found that its legal page (clause (p)) and API terms
(clause (l)) prohibit storing FRED content in a database without the Bank's prior written
consent; no exception for personal use was found. New FRED ingestion was blocked pending that
consent ([`docs/data-sources.md`](../data-sources.md#fred-federal-reserve-bank-of-st-louis)).

Waiting for consent would leave the project dependent on an aggregator whose terms do not fit
how Market Pulse works: it stores every observation and revision, with provenance, in its own
database.

## Decision

1. **Market Pulse obtains data only directly from the organization that publishes it.**
   Aggregators and republishers (FRED included) are not used as data providers.
2. **FRED is retired as a data provider.** This is a project decision, not a pending licensing
   question: written consent from the Federal Reserve Bank of St. Louis would not by itself
   reactivate FRED. Reversing it requires a new ADR.
3. **Enforced in code** (PR #18): `fred` is in the source block list
   (`backend/src/marketpulse/core/source_access.py`). Ingestion (the job, the CLI command, the
   FRED client, and the fixture-recording script) refuses before any request, and the data API
   never serves a FRED series. The dashboard defines, requests, and shows no FRED series.
4. **Historical FRED data is preserved, not deleted.** The production FRED records and the raw
   FRED response files stay unchanged until a separately approved plan decides what to do with
   them. They are never served. Read-only verification (`marketpulse verify`, including
   `--replay`) may still check them; it writes nothing and contacts no provider. The FRED code
   is kept, unreachable, for the same reason.
5. **Source status:**

   | Source | Status |
   |---|---|
   | EIA | Active and approved (public domain, except third-party series such as the Refinitiv/LSEG spot prices, which are not ingested) |
   | FRED | Retired as a data provider |
   | ERCOT | On hold pending ERCOT's written permission (ADR-0014) |

6. **Future macroeconomic data comes from approved direct primary sources.** Before a source is
   integrated, its official access method and terms (including whether observations may be
   stored) are verified and recorded in `docs/data-sources.md`, and the project owner approves
   it. Likely candidates are the publishers of the series FRED republished: the U.S. Treasury,
   BLS, BEA, the Federal Reserve Board, and the Federal Reserve Bank of New York. These are
   candidates, not commitments; none is approved or integrated yet.

## Consequences

- The dashboard has no macroeconomic indicators until a direct source is integrated. The
  Macroeconomics page is marked Planned and says so; the Overview shows only EIA indicators.
- Direct-source series will need new catalog codes: catalog identity is immutable (a code never
  moves to another source), and production FRED rows still hold the old codes.
- Direct publishers generally do not provide real-time vintages through their APIs, so new
  series use first-seen versioning (ADR-0011): point-in-time history starts when Market Pulse
  first retrieves a value.
- ADR-0008 remains in force except for its FRED/ALFRED row and the FRED-specific rule, which this
  ADR replaces.
