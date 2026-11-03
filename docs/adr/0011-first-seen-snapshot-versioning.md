# ADR-0011: First-seen snapshot versioning for providers without vintages

- **Status:** Accepted
- **Date:** 2026-11-02

## Context

ADR-0007 stores every revision of a value as a separate version with a validity period
(`vintage_start`, `vintage_end`), so point-in-time queries can return what was known at any
instant. FRED/ALFRED publishes real-time vintages, so FRED versions use the provider's own dates.

EIA publishes **no vintages**: the API returns only the current values. EIA does revise data
(weekly petroleum and natural gas estimates, monthly natural gas balances for about two years),
and periods can be renumbered or withdrawn. Inventing past vintages (for example dating a value
at its period end or an assumed release time) would fabricate history and break point-in-time
research. Version history must therefore come from what Market Pulse itself observed.

## Decision

### Vintage basis

Every series carries an immutable `vintage_basis` (`ref.series`, copied from its source in the
catalog):

- `source_vintage`: version times are the provider's vintages (FRED).
- `first_seen`: version times are **when Market Pulse first retrieved the value** (EIA). They are
  not publication times. No version is dated before Market Pulse first saw it.

Writers enforce the label: the FRED writer refuses `first_seen` series and the snapshot writer
refuses `source_vintage` series. Point-in-time results always return the vintage basis.

### Snapshots

Each complete, validated EIA response for one series is a **snapshot** taken at one instant,
`snapshot_at` (timezone-aware UTC), captured only after every page has been retrieved,
validated, and mapped to its raw payload. Compared with the stored current versions in the
snapshot's scope:

- new period: insert a version starting at `snapshot_at`;
- same value and status (exact `Decimal` equality, so `1.0` equals `1.00`): confirm only
  (`last_confirmed_at`, `last_confirmed_run_id`);
- different value or status (including value to or from null): close the current version at
  `snapshot_at` and insert the new one; a replaced value is not marked confirmed;
- stored period missing from the snapshot (**vanished**): keep the stored version unchanged and
  unconfirmed, and track one open `vanished_period` warning per series and period (occurrences
  and last detection updated; resolved when the period is reported again; a later
  disappearance opens a new issue).

History is append-only: rows are never deleted and values never rewritten; only `vintage_end`
(once) and the confirmation fields change.

### Rejection (all-or-nothing per series)

A snapshot is rejected, with **no observation or vanished-issue change**, when:

- more than **5** periods, or more than **1%** of the stored periods in scope, vanish at once
  (more likely a source problem than real revisions). There is no minimum scope size;
- it is not strictly newer than every stored version start, end, and confirmation of the series
  (stale, replayed, duplicate, or out-of-order snapshots);
- it has duplicate periods, periods outside the requested scope, period-format mismatches, a
  changed period end or period label, or missing provenance.

A rejected snapshot records only its issues and marks the run `partial`. Any exception rolls the
whole data transaction back and marks the run `failed`.

### Unfinalized runs

The observation transaction and the final update of the run row (`ops.ingestion_runs`) are
separate transactions, so the snapshot can commit successfully and the run-status update can then
fail on its own (for example, the connection drops). Such a run is **UNFINALIZED**:

- committed observation changes are kept and are never reported as rolled back;
- the CLI reports `UNFINALIZED`, exits with code 1, and states explicitly whether the snapshot's
  observations were committed;
- if the run body had already failed, its original error is reported, not the update error;
- the run row may remain `running` until operational reconciliation of stuck runs is implemented
  (backlog). A retry is a new run; the stale-snapshot guard still applies.

### Provenance

Every new EIA version records `source_period` (the period exactly as EIA reported it) and
`raw_payload_id`: the raw response **page** that contained it, matched by redacted URL and the
SHA-256 of the body. Each page must belong to the same run and source, have been retrieved no
later than `snapshot_at`, and have a URL that is exactly the request made for this series: the
EIA host, the path `/v2/<route>/data`, and the series facet, frequency, sort, page length,
offset, and window `start` (present only for incremental runs), with no other parameters such as
`end`. Every successful raw response is stored before parsing, in its own transaction, so it
survives any later failure; HTTP error responses are not stored (backlog). There is one run per
series.

### Modes and scope

- **Full** (first run, or `--full`): the whole history; the scope is unbounded.
- **Incremental**: W = the latest stored period minus 90 days (daily), 26 weeks (weekly), or 26
  months (monthly). The request starts at the period immediately before W and rows before W are
  dropped, so correctness does not depend on whether EIA's `start` parameter is inclusive. The
  scope starts at W. Incremental windows narrow the scope; they never change the threshold, so
  in a small window a single vanished period rejects the snapshot.

### Concurrency and time guards

Writers for one series serialize on a transaction-scoped advisory lock shared with FRED; the
plan is made from data read after the lock. `snapshot_at` must not precede the run's start or
any cited page's retrieval, and may be at most 5 minutes ahead of the database clock. A retry is
a new run with a fresh fetch and a fresh `snapshot_at`.

## Consequences

- EIA point-in-time history **starts at Market Pulse's first ingestion** of each series. Earlier
  "as of" queries return nothing for EIA series; revisions EIA made before that are unknown.
- Revisions are only as fine-grained as ingestion runs: a value revised twice between runs
  appears as one revision, dated when Market Pulse saw it.
- A period that EIA removes permanently stays stored and counts as vanished in every snapshot
  that covers it; if that exceeds the threshold, those snapshots are rejected until an operator
  acts. The open issue and the rejection are the alert. **Backlog:** an audited operator
  decision to retire a period.
- The strict rules favor rejecting a suspicious snapshot over storing it; rejected runs need
  review but never damage history.
- Raw pages make every stored EIA value reproducible from the exact bytes received.
