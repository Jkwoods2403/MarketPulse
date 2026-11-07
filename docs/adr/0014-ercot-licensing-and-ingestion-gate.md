# ADR-0014: ERCOT licensing classification and the pending-clarification ingestion gate

- **Status:** Proposed
- **Date:** 2026-11-07

## Context

ADR-0008 requires every source's terms to be verified when its connector is built, and licensing
to be tracked per source so a public deployment can exclude data that may not be redistributed.
ERCOT data is public, but public availability does not establish storage, commercial or
redistribution rights.

ERCOT's terms were reviewed on 2026-11-07 (full findings and quotes in
[`docs/data-sources.md`](../data-sources.md#ercot-electric-reliability-council-of-texas)). In
short:

- **Confirmed:** ERCOT expects downloaded reports to be stored on the user's own hardware. API
  Terms, Data Access Portal terms, "Limitations on Downloads": "Reports should be downloaded and
  stored on the users' own hardware to avoid downloading from the site multiple times during a
  yearly time period." This is an instruction, not a license grant.
- **Confirmed for public content of ercot.com only:** the website Terms of Use (last updated
  07/20/2023), Sec. 5, allow use, reproduction and redistribution of public content, and of
  "raw data provided in public portions of this website ... in compilations, charts, and
  analyses".
- **Unresolved:** whether Sec. 5 covers data obtained through the API. The API Terms contain no
  use, redistribution, commercial-use or attribution terms, and do not say whether they include
  or replace the website terms. Nothing addresses commercial use or how long data may be kept.

Silence is not treated as permission. The question was put to ERCOT through the ERCOT Public
Portal, the channel API Terms Sec. H names for questions about the policy:

| Ticket | Submitted | Initial state | Priority | Status |
|---|---|---|---|---|
| SS0007218 | 2026-11-07 | New | 4 - Low | Pending |

## Decision

### Classification (conservative and internal, not legal conclusions)

- **License:** `no_redistribution`. A future public deployment excludes ERCOT data automatically
  (ADR-0008).
- **Access requirement:** `subscription_required`, recorded in a new source attribute
  `access_requirement` (`open`, `registration_required`, `subscription_required`). Access and
  license are separate questions: how data may be fetched says nothing about what may be done
  with it.
- These classifications do **not** establish that private storage, personal research or
  commercial use is permitted.

### The pending-clarification gate

- **No ERCOT ingestion of any kind, staging included, until both:** (1) ERCOT's written answer
  to SS0007218 is received and recorded in `docs/data-sources.md`, and (2) the project owner's
  explicit decision that the answer permits ingestion is recorded there too. The completed terms
  review does not satisfy this gate.
- **Redistribution and public deployment** of ERCOT data or anything derived from it stay
  blocked until ERCOT's written clarification permits them.
- **Enforcement in code (proposed; implemented in Step 2, not here):** a source-level ingestion
  block, so every ingestion job refuses a blocked source before any HTTP request, run, raw
  payload or file. The ERCOT source is created in the blocked state. Lifting the block is a
  reviewed catalog change that cites the recorded answer and decision. The existing EIA guard
  for `personal_use_only` series is not changed by this ADR.
- The `no_redistribution` value alone does not stop ingestion (the EIA guard blocks only
  `personal_use_only`); the gate above does.

### Access, probing and ingestion are different activities

| Activity | What it does | What it stores | Allowed while SS0007218 is pending |
|---|---|---|---|
| **Account and credentials** | The project owner registers in their own name and subscribes to the Public API product | Credentials in the local `.env` only | Yes, by the owner only. API Terms Sec. A: no bot accounts and no sharing credentials. |
| **Authentication check** | One token request to ERCOT's sign-in service; no data endpoint | Nothing | Yes, with the owner's explicit approval |
| **Read-only probe** (`marketpulse probe ercot`) | A fixed, pre-approved list of data requests; responses held in memory only | **Metadata only:** field names and types, units, row counts, flags, timestamps, page sizes, response sizes. No price, load or forecast values; no raw payload files; no database writes. | **The owner's decision.** It is API access under the API Terms, not ingestion, but it does download reports, which may count toward the download limit below. |
| **Ingestion** | Any persistence of ERCOT data values or raw responses: database rows, raw payload files, caches, exports, or test fixtures made from real data | ERCOT data | **No** (gate above) |
| **Display and redistribution** | Dashboards, charts, analyses or exports shown to anyone else, and any public deployment | Not applicable | **No.** Requires ERCOT's written clarification. |

Real ERCOT data is never committed to this public repository; test fixtures are synthetic and
labeled SYNTHETIC.

### Download limit: an internal safeguard, not an interpretation

API Terms, "Limitations on Downloads": "You may not download the same report from the Data
Access Portal more than three times within a 12-month period."

- **Unresolved:** whether this clause applies to Public API requests (the Data Access Portal is
  described as providing access "through the use of APIs", but the clause names only the
  portal), and what "the same report" means for an API query: a report product such as
  NP6-905-CD, one posted file or posting, or one query.
- **Market Pulse does not claim to comply by any particular counting method.** Under a
  product-level reading, even routine daily requests to one product could exceed three
  downloads a year, so routine polling cannot be designed until ERCOT answers.
- **Conservative internal safeguard, until ERCOT answers:** never send the same request (same
  endpoint and parameters) twice; keep the probe's request list fixed and approved in advance;
  and request no more than the planned rows. This reduces repeated downloads but **does not
  establish that ERCOT's restriction is satisfied.**
- **Follow-up question for ticket SS0007218** (drafted for the owner to add; not submitted):

  > For API users: does the limit of downloading "the same report ... more than three times
  > within a 12-month period" in the Data Access Portal terms apply to requests made through the
  > Public API? If so, how does ERCOT define "the same report" for API requests (for example, a
  > report product such as NP6-905-CD, a single posting or file, or a single query with specific
  > parameters), and how are API downloads counted toward the limit? Would regular automated
  > retrieval of newly posted data from the same product (for example, once per day) be
  > consistent with this limit?

- No circumvention of ERCOT's limits (API Terms Sec. D and Data Access Portal General
  Limitations).

### Request rate

The client throttles itself (planned: 25 requests per minute) and backs off on HTTP 429. The
figure of 30 requests per minute comes from ERCOT developer documentation read during the
research; it is not in any terms document and was not re-checked. It is confirmed in the
logged-in API Explorer before the probe.

### Terms can change

Website Terms Sec. 10: "ERCOT reserves the right to change the terms of use contained within
this Agreement at any time without notice." The terms are re-read before each ERCOT phase gate,
and the review date is recorded in `docs/data-sources.md`.

### Attribution and trademarks

When ERCOT data is shown, the source is cited in text ("Source: ERCOT"), as the project's data
rules require for every value. The ERCOT logo and the name as a trademark are not used: website
Terms Sec. 9 requires ERCOT's prior written permission. The final attribution text waits for
ERCOT's answer.

## Open decisions

| Item | Status | Settled by |
|---|---|---|
| Whether website Terms Sec. 5 covers API data | Unresolved | ERCOT (SS0007218) |
| Private storage of API data, and how long raw responses may be kept | Unresolved | ERCOT, then a retention ADR (ADR-0012) |
| Commercial use, and whether a separate agreement is required | Unresolved | ERCOT |
| Redistribution of derived charts, statistics and analyses based on API data | Unresolved | ERCOT |
| Attribution required for API data | Unresolved | ERCOT |
| Whether the download limit applies to API requests, and what "the same report" means | Unresolved; follow-up question drafted, not yet submitted | ERCOT (follow-up on SS0007218) |
| Whether to run the read-only probe before ERCOT answers | Open decision | Project owner |
| 30 requests per minute | ERCOT developer documentation read during the research ("The API allows up to 30 requests per minute"); in no terms document; not re-checked | Logged-in API Explorer documentation |
| U.S.-only access | Stated in the first research revision; its source could not be re-identified; in no terms document | Logged-in API Explorer documentation |

## Consequences

- No ERCOT data is stored until ERCOT answers and the owner records a decision; design, code and
  tests that need no ERCOT data can proceed.
- If ERCOT's answer changes the classification, a new ADR supersedes the relevant part of this
  one.
- Routine polling and the history backfill cannot be planned in detail until ERCOT explains how
  downloads are counted.
- How long raw payloads are kept is not decided here; it depends on ERCOT's answer (ADR-0012).
