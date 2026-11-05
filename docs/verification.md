# Verifying stored data (`marketpulse verify`)

`marketpulse verify` checks that stored observations and their lineage still satisfy the
invariants the ingestion writers guarantee. With `--replay` it also re-reads the stored raw
provider responses and compares them with the versions they produced.

```bash
# from backend/
uv run marketpulse verify                                   # SQL integrity checks, all series
uv run marketpulse verify --series ust_10y l48_gas_storage  # limit series-level checks
uv run marketpulse verify --replay                          # also replay stored raw pages
```

Code: `marketpulse.ingestion.verify` (checks and report) and `marketpulse.ingestion.replay`
(replay).

## Read-only guarantees

- **One read-only snapshot.** Every check runs in one transaction opened as `REPEATABLE READ,
  READ ONLY` (PostgreSQL refuses any write in it) and rolled back at the end.
- **Raw files are only read.** Replay reads files through `RawPayloadStore.load`, which reads,
  decompresses, and checks each file against its recorded SHA-256. Paths outside the data
  directory are refused. Nothing is created, written, or deleted, and the data directory is not
  created if it does not exist.
- **No provider calls.** Verification never contacts FRED or EIA.
- **Credentials are never printed.** The credential check reports table, row, and parameter
  *names* only, never values.
- **Tested.** The tests compare a checksum of every table that verification reads, and the size,
  modification time, and bytes of every raw file, before and after verifying. This covers
  `verify`, `verify --replay`, and runs where every outcome occurs at once.

## SQL integrity checks (always run)

| Check | Finds | Severity |
|---|---|---|
| `version_overlap` | two versions of one period overlap in time | error |
| `version_gap` | a gap between one version's end and the next one's start | error for EIA (`first_seen`); warning for FRED (`source_vintage`), because FRED can withdraw an observation and report it again later |
| `no_current_version` | a period has versions, but none is current | error for EIA; warning for FRED (legitimate ALFRED withdrawals) |
| `written_by_failed_run` / `confirmed_by_failed_run` | a version created or confirmed by a run recorded as failed | error |
| `written_by_unfinalized_run` | versions created by a run still marked `running` | warning (one per run) |
| `run_source_mismatch` | a version created by a run of another source | error |
| `missing_provenance` | an EIA version without `source_period` or `raw_payload_id` | error |
| `provenance_mismatch` | an EIA version whose cited page belongs to another run or source, or was retrieved after the version started | error |
| `first_seen_times` | an EIA version whose first retrieval differs from its start, or whose last confirmation is outside `[start, end)` | error |
| `source_vintage_midnight` | a FRED version boundary that is not midnight US Central | error |
| `vanished_issue_without_current` | an open `vanished_period` issue for a period with no current version | error |
| `unredacted_text` | a stored URL, run error, run parameter, or issue detail exposing a credential | error |

`unredacted_text` always checks the whole database. It flags a credential parameter (`api_key`,
`token`, `password`, …) only when the parameter has a real value. An empty value (`token=`) or
`REDACTED` exposes nothing and is not flagged. Configured secrets (API keys, the database
password) are flagged anywhere they appear, raw or URL-encoded.

## Replay (`--replay`)

Replay reads the raw pages under `MARKETPULSE_DATA_DIR`.

**EIA creation.** Every EIA version cites the exact raw page that contained it. That page is
re-parsed, and its row for the version's source period must match the stored series facet,
period bounds, value, and status.

**EIA closure.** The snapshot writer closes a version only when a snapshot reports a different
value or status. In the same transaction it inserts the successor, which starts exactly at the
closed version's end. Snapshot times strictly increase per series, and the triple (series,
period, version start) is unique. So the closing run is exactly the run that created the
successor, and the successor's cited page is the closing snapshot's page. A closed version is
checked against that page:
- if no successor starts at its end, the closing run cannot be identified: an error;
- the closing page must report a different value or status for the period, otherwise the
  closure is not justified: an error;
- if the closing page is missing, the version is unverified.

**EIA confirmations.** A later snapshot that reports a value unchanged confirms the version. It
moves `last_confirmed_at` and records `last_confirmed_run_id`. Replaying confirmation snapshots
is not implemented, so a version confirmed by a later run is UNSUPPORTED (see below).

**FRED creation.** FRED versions cite no page, but each records the run that created it. Each
FRED run covers one series and stores its own observation pages. Replay rebuilds the run's
output exactly as ingestion did: it parses every observation page, rejoins window-edge splits,
and converts the result to versions. Every version the run created must appear in that output
with the same period end, value, and status. The version's `first_retrieved_at` must equal the
latest retrieval time of the run's observation pages (a provenance check).

**FRED closures and confirmations.** A later FRED run can close a version or re-confirm it, but
only the time is stored, not which run did it. Those steps cannot be attributed, so such
versions are UNSUPPORTED. If the creating run's own output already had an end, that end is
compared like any other field.

### Replay states

Each replayed version is counted in **exactly one** state. The first state that applies wins:

| Precedence | State | Meaning |
|---|---|---|
| 1 | **ERROR** | A supported comparison found a mismatch, or a raw file is corrupt or fails its hash check. |
| 2 | **UNVERIFIED** | Evidence is missing or incomplete: a raw file is missing, a run has no stored pages, or a version cites no page. |
| 3 | **UNSUPPORTED** | Everything replay can check matched (at least the version's creation), but a later step was not replayed: a FRED closure or confirmation, or an EIA confirmation. |
| 4 | **VERIFIED** | The version was fully reconstructed and matched: its creation, its provenance, and any closure. |

UNVERIFIED and UNSUPPORTED are never counted as passed or failed, and they are kept apart.
UNVERIFIED means evidence is missing. UNSUPPORTED means the verification method does not cover
that step. An UNSUPPORTED version's *creation* was verified, but its full history was not.

## Output

The CLI prints one row per check with these columns:

- `status`: `FAIL`, `WARN`, `UNVERIFIED`, `UNSUPPORTED`, or `ok`;
- error and warning counts;
- the replay counts `unverified`, `unsupported`, and `verified` (`-` for SQL checks, which only
  report findings).

Below the table it prints up to five sample findings per check as "N of M finding(s) shown".
One finding can cover several versions, for example every version citing a missing page.

With `--replay`, the table adds two rows after the SQL checks, one per source:

| Row | Covers |
|---|---|
| `replay_eia` | EIA versions in scope: creation against the cited page, and closure against the closing run's page |
| `replay_fred` | FRED versions in scope: each run's output rebuilt from that run's stored pages |

In these rows, the `errors`, `unverified`, `unsupported`, and `verified` columns count
**versions**, and each version is counted in exactly one of them (see
[Replay states](#replay-states)). `warnings` is always 0. The row's `status` is `FAIL` if any
version is in ERROR, else `UNVERIFIED`, else `UNSUPPORTED`, else `ok`. Sample findings are
prefixed `error:`, `unverified:`, or `unsupported:` and listed in that order. The rows'
version counts add up to the `replay (versions)` line below.

The summary line shows totals. With `--replay` it adds:

```
replay (versions): VERIFIED n, ERROR n, UNVERIFIED n, UNSUPPORTED n
```

When anything is UNSUPPORTED, the output also explains that those versions are not counted as
verified. Without `--replay` it says `replay: not run`.

## Exit codes

The first code that applies:

| Code | Meaning |
|---|---|
| 1 | Any error: an SQL check error, or a replayed version in ERROR. Also used for an unknown `--series` code. |
| 2 | No errors, but warnings. |
| 3 | No errors or warnings, but some replayed versions are UNVERIFIED or UNSUPPORTED: not fully verified. |
| 0 | Nothing found. With `--replay`, every replayed version is VERIFIED. |

Without `--replay`, exit 0 means only that the SQL checks found nothing.

## Known limitations

- **`verify --replay` will normally exit 3 on real data.** After a second EIA snapshot
  re-confirms unchanged values, or after an incremental FRED run closes or re-confirms versions,
  those versions are UNSUPPORTED. This is intended. Scripts must not treat 3 as a failure of the
  data, and must not treat it as full verification either.
- **EIA confirmations are not replayed.** Only the latest confirming run is recorded, so earlier
  confirmations leave no trace to check.
- **FRED closures and confirmations cannot be attributed**, because FRED rows do not record the
  run that closed or confirmed them.
- **Replay shows that stored history matches what Market Pulse received**, not that the
  provider's numbers are correct.
- **EIA has no replay-level `first_retrieved_at` comparison.** The SQL checks cover it: an EIA
  version's first retrieval must equal its start, and its cited page must not have been
  retrieved after that start.
