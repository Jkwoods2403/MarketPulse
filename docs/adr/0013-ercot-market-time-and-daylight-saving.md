# ADR-0013: ERCOT market time and daylight saving

- **Status:** Proposed
- **Date:** 2026-11-07

## Context

ERCOT labels time in local market time, by operating day and hour **ending**, and Austin, Texas
observes daylight saving time. One calendar day a year has 23 hours and one has 25. On the
25-hour day one local clock hour occurs twice, so a local label alone cannot identify it. A wrong
rule here would silently shift values by an hour, which is the largest technical risk in the
ERCOT connector.

ADR-0007 requires UTC storage with local conventions kept alongside; ADR-0012 makes the UTC
interval the identity of every ERCOT value. This ADR fixes how ERCOT labels become UTC, and
separates what is verified from what is assumed.

## Verified facts

| Fact | Source |
|---|---|
| Central Prevailing Time (CPT) is "Either Central Standard Time or Central Daylight Time, in effect in Austin, Texas." | ERCOT Nodal Protocols, Section 2 (definitions), from [ERCOT's current Nodal Protocols](https://www.ercot.com/mktrules/nprotocols/current) |
| An Operating Day is "The day, including hours ending 0100 to 2400, during which energy flows." Hours are labeled by when they **end**. | Same |
| Hourly API products carry a `DSTFlag`; SCED products carry `repeatHourFlag`. Both are booleans ("Format - true \| false.") in the API. | ERCOT Public API OpenAPI specification ([`pubapi-apim-api.json`](https://raw.githubusercontent.com/ercot/api-specs/main/pubapi/pubapi-apim-api.json), version 1.0; the copy reviewed was last committed 2024-02-21) |
| Load-forecast posting times (`postedDatetime`) carry no flag. | Same |
| RTC+B went live for operating day 2025-12-05. | ERCOT market notice [M-F110525-01](https://www.ercot.com/services/comm/mkt_notices/M-F110525-01) |

Calendar facts, independent of ERCOT: in `America/Chicago` the spring-forward day skips local
02:00–03:00 and the fall-back day repeats local 01:00–02:00.

## Decision

### Time zone

CPT is implemented as the IANA zone `America/Chicago`, the zone that covers Austin, Texas. All
conversions use Python's `zoneinfo`, never fixed UTC offsets.

### Label to UTC

For a row with operating date `D`, hour ending `HE` (1–24), interval `i` (1-based, for
sub-hourly data) and interval length `L`:

1. local start = `D` 00:00 + (`HE` − 1) hours + (`i` − 1) × `L`, as a wall-clock time in
   `America/Chicago`;
2. `interval_start` = that local start converted to UTC. If the local start does not exist
   (spring-forward gap), the row is rejected. If it is ambiguous (fall-back repeat), it is
   resolved only by the repeated-hour rule below;
3. `interval_end` = `interval_start` + `L`, **computed in UTC**.

The end is never computed by adding `L` to a local time and converting that, because the
result can itself be ambiguous or non-existent even when the interval is valid. For example, on a
spring-forward day the hour starting at local 01:00 CST ends at local 03:00 CDT; adding one
hour on the local clock would give 02:00, which does not exist.

Hour ending 24 therefore ends at 00:00 local on the next calendar day.

### Daylight-saving days

| Day type | Elapsed hours (calendar fact) | Hourly rows, 15-minute rows (assumption) | Handling |
|---|---|---|---|
| Normal | 24 | 24, 96 | Direct conversion |
| Spring-forward (second Sunday of March) | 23 | 23, 92 | A row whose local start does not exist **rejects the snapshot** |
| Fall-back (first Sunday of November) | 25 | 25, 100 | A row whose local start is in the repeated hour is resolved only by the repeated-hour rule |

**How ERCOT labels these days is an assumption, not verified:** that spring-forward days omit
hour ending 03, and that fall-back days repeat hour ending 02 (once with the flag set, once
without). The row counts above follow from that assumption. The probe checks it.

### The repeated hour

- **The flag rule is unresolved.** No official ERCOT text read so far says whether
  `DSTFlag = true` (or `repeatHourFlag = true`) marks the first or the second occurrence of the
  repeated hour. A third-party implementation (gridstatus pull request #989) is not
  authoritative and is not relied on.
- **Until the rule is settled, the converter refuses every row in the repeated hour.** It raises
  an error naming the row's labels, and the snapshot is rejected. It never picks an occurrence by
  default.
- **How it will be settled:** the read-only probe (ADR-0014) reads the repeated hour on
  2026-11-01 and 2025-11-02 and compares those rows with SCED-interval data around them. That
  comparison is only valid if SCED timestamps are unambiguous, which is itself an assumption the
  probe checks. The result is recorded in a new ADR before any ingestion.
- Once settled, the rule is mapped to Python's `fold` (0 = first occurrence, 1 = second). This
  ADR does not say which flag value maps to which.

### Posting times without a flag

A `postedDatetime` inside the repeated local hour is ambiguous. It is stored as received with
`posted_at_ambiguous = true` (ADR-0012), and its UTC value is resolved by its order relative to
neighbouring postings of the same product. If order cannot resolve it, the posting is rejected.
Analyses can exclude ambiguous postings.

### Formats and labels

- The OpenAPI specification documents query parameters as dates "Format - yyyy-MM-dd." and
  timestamps "Format - yyyy-MM-ddTH24:mm:ss." (verbatim). Whether "H24" only means a 24-hour
  clock, or whether hour 24 can appear in timestamps, is unresolved.
- ERCOT labels are stored exactly as received (ADR-0012), so any conversion can be audited and
  re-run.

### Tests required before any ERCOT data is stored

- Every transition from 2023 to 2027: spring-forward 2023-03-12, 2024-03-10, 2025-03-09,
  2026-03-08, 2027-03-14; fall-back 2023-11-05, 2024-11-03, 2025-11-02, 2026-11-01, 2027-11-07.
- For each: elapsed hours, monotonically increasing and gap-free UTC intervals, `interval_end`
  computed in UTC (including the hour before the spring gap and the hours around the fall-back
  repeat), hour ending 24 crossing midnight, a non-existent local start rejected, the repeated
  hour refused while the rule is open, and both occurrences distinct once it is set.
- Fixtures are synthetic and labeled SYNTHETIC; no real ERCOT data is committed (ADR-0014).

## Open decisions and assumptions

| Item | Status | Settled by |
|---|---|---|
| Which occurrence of the repeated hour the flag marks | **Unresolved; blocks storing fall-back days** | Probe, then a new ADR |
| Fall-back days repeat hour ending 02; spring-forward days omit hour ending 03 | Assumption | Probe |
| Report files show the flags as Y/N | Assumption from a third party (gridstatus pull request #989); an ERCOT file field list was cited in the research but not re-read | Probe, if files are ever used |
| `interval_number` 1–4 counts 15-minute intervals within the hour ending | Assumption | Probe |
| SCED timestamps are the SCED run time and are unambiguous | Assumption | Probe |
| Every API product uses CPT labels (not UTC) | Assumption (not stated on the pages read) | Probe |
| Interval length per product (15 minutes for real-time settlement prices, 60 for the hourly products) | Assumption from product names | Probe |
| Meaning of the "H24" timestamp format | Unresolved | Probe |

## Consequences

- No ERCOT value is ever stored at a guessed hour; fall-back days stay unstorable until the rule
  is verified from real data.
- Interval ends are always valid, because they are computed in UTC.
- Conversion depends only on the IANA database; future DST law changes arrive through `tzdata`
  updates.
- Keeping ERCOT's labels next to the UTC interval makes every conversion auditable and lets a
  corrected rule be re-applied from raw payloads.
