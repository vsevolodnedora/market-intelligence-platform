# EDGAR ingestion + durable outbox

This project ingests selected SEC EDGAR filings for a watchlist, normalizes the portions most useful to downstream systems, persists the results in SQLite, and emits durable outbox events.

For downstream consumers there are two primary interfaces:

- **Historical / research / EDA / train-backtest-data interface:** the SQLite database.
- **Live / latency-sensitive interface:** the outbox stream, exposed by the default launcher as UTC date-partitioned JSONL files and designed to be replaceable with another publisher callback.

## Scope

This codebase is an ingestion and publication layer.

What it does:

1. Discover relevant EDGAR filings.
2. Resolve issuer relevance for direct and ambiguous forms.
3. Retrieve the full submission and, when possible, the selected primary document.
4. Parse and normalize selected Form 4 and 8-K content.
5. Persist filing state, normalized rows, and outbox events transactionally.
6. Expose optional in-process metrics and health endpoints.
7. Optionally archive cold artifacts and prior-UTC-day JSONL event files.
8. Optionally supervise the daemon with a shell wrapper that monitors hot storage and memory.

## Validated file set

This README is validated against the following supplied files:

- `edgar_core.py`
- `edgar_daemon.py`
- `event_outbox.py`
- `metrics.py`
- `metrics_http.py`
- `edgar_archiver.py`
- `edgar_master.sh`

## File ownership

- `edgar_core.py` owns shared contracts and implementations: models, SEC parsing, watchlist loading, rate limiting, HTTP client behavior, 8-K / Form 4 extraction helpers, and SQLite schema/storage.
- `edgar_daemon.py` owns orchestration: polling, prioritization, live vs historical work lanes, retries, startup replay, and the outbox publisher loop.
- `event_outbox.py` owns the transactional outbox, event envelopes, artifact commit helpers, and the default JSONL publishers.
- `metrics.py` owns the in-memory metrics registry and Prometheus-style exposition.
- `metrics_http.py` owns the lightweight `/metrics` and `/healthz` HTTP server.
- `edgar_archiver.py` owns cold-artifact archival and archival of prior-UTC-day JSONL event files.
- `edgar_master.sh` owns optional process supervision, config validation, hot working-set monitoring, and archiver invocation under resource pressure.

## What downstream consumers should assume

- **SQLite plus outbox is the durable truth layer.**
- **JSONL is a transport/export surface, not the truth layer.**
- **Delivery is at least once.** Consumers must deduplicate by `event_id`.
- **Live ordering is by `commit_seq`.** Do not infer ordering from file mtimes or wall-clock timestamps.
- **JSONL files are partitioned by UTC day.** Daily cutoffs should be interpreted in UTC, not host local time.
- **Programmatic embeddings can disable publishing.** The default daemon CLI auto-derives `publish_dir`, but if no publisher is configured, outbox rows remain in SQLite and are not exported.
- **Artifact paths are mutable over time.** Cold artifacts may be archived and their DB paths rewritten; always resolve artifact locations from SQLite instead of deriving them heuristically from CIK and accession.
- **Live Form 4 event payloads are intentionally compact summaries, not full historical extracts.** Use SQLite tables for complete research datasets.
- **`edgar.feed.gap_detected` is not tied to a specific accession.** Its envelope uses `accession_number = "N/A"`.

## Supported filing families

Default direct forms:

- `8-K`, `8-K/A`
- `10-K`, `10-K/A`
- `10-Q`, `10-Q/A`
- `6-K`, `6-K/A`
- `20-F`, `20-F/A`
- `40-F`, `40-F/A`

Default ambiguous forms:

- `3`, `3/A`, `4`, `4/A`, `5`, `5/A`
- `SC 13D`, `SC 13D/A`, `SC 13G`, `SC 13G/A`

For ambiguous forms, issuer resolution is conservative:

- ownership forms prefer `issuer`, then fall back to `filer`
- activist forms prefer `subject_company`
- other ambiguous forms fall back to `filer`

Downstream consumers should prefer `issuer_cik` and `issuer_name` when present. `archive_cik` is the SEC archive-path anchor, not always the canonical economic issuer.

## Runtime inputs and defaults (daemon)

The daemon requires a SEC-compliant `--user-agent` and accepts the main runtime settings below.

- `--db-path` default: `./data/edgar.db`
- `--raw-dir` default: `./data/raw`
- `--watchlist-file` optional
- `--max-rps` default: `5.0`, must be `> 0` and `<= 10`
- `--latest-poll-seconds` default: `5`
- `--watchlist-audit-seconds` default: `21600`
- `--backfill-lookback-days` default: `365`
- `--reconcile-poll-seconds` default: `3600`
- `--weekly-repair-cron` default: `0 11 * * 6`
- `--http-timeout-seconds` default: `20.0`
- `--max-retries` default: `3`
- `--retry-base-seconds` default: `2.0`
- `--retry-failed-poll-seconds` default: `300`
- `--publish-dir` optional on the CLI, but if omitted the daemon auto-derives it as `<db_path.parent>/events`, so the default CLI behavior enables JSONL publishing
- `--live-workers` default: `3`
- `--live-rps-share` default: `0.6`, must be between `0.1` and `0.9`
- `--metrics-enabled` default: disabled
- `--metrics-host` default: `127.0.0.1`
- `--metrics-port` default: `9108`

Notes:

- `weekly_repair_cron` supports only `minute hour * * dow` format. It does not support ranges, lists, or step syntax.
- The `Settings` type also contains `out_form4_transactions_cap=20` and `out_form4_owners_cap=10`, which bound the size of the compact live `edgar.form4.parsed` payload. In the supplied daemon, those caps are **not exposed as CLI flags**.
- The daemon splits SEC request capacity between live and historical lanes using `live_rps_share`; the historical lane gets the remainder.

Minimal example:

```bash
python edgar_daemon.py \
  --user-agent "Firm Name ops@firm.com" \
  --db-path ./data/edgar.db \
  --raw-dir ./data/raw \
  --watchlist-file ./watchlist.yaml
```

## Optional operational entrypoints

### Archiver

`edgar_archiver.py` is a standalone archival process. It archives:

- cold filing artifacts for filings with `retrieval_status = 'retrieved'` older than a retention threshold
- closed prior-UTC-day JSONL event files

Important defaults and behavior:

- `--retention-days` default: `14`
- `--batch-size` default: `100`
- `--publish-dir` defaults to `<db_path.parent>/events` only if that directory exists
- `--raw-dir` defaults to `<db_path.parent>/raw` only if that directory exists
- archival copies are byte-identical and verified before DB path rewrites
- DB rewrites touch only artifact location fields; they do **not** change `retrieval_status`, retry metadata, normalized Form 4 / 8-K tables, checkpoints, or outbox rows
- current UTC-day JSONL files are never archived

Example:

```bash
python edgar_archiver.py \
  --db-path ./data/edgar.db \
  --archive-dir ./data/archive \
  --retention-days 14
```

### Optional supervisor

`edgar_master.sh` is an optional shell supervisor. It validates configuration, launches the daemon, monitors the hot working set (`raw` + `events` + DB by default), invokes the archiver when storage limits are breached, and can gracefully stop the daemon if limits remain exceeded.

Important operational note: the monitored hot-path set should exclude `ARCHIVE_DIR`. If archive storage is nested inside the monitored tree, archiving may not reduce measured bytes materially.

## Watchlist schema

The watchlist YAML must contain a top-level `companies` list. Each valid entry must include:

- `cik`
- `ticker`
- `name`

Optional:

- `aliases`
- any additional metadata fields, which are preserved

Entries with missing fields, invalid CIKs, missing tickers, or duplicate CIKs are skipped. If no valid entries remain, loading fails.

Example:

```yaml
companies:
  - cik: "0000320193"
    ticker: "AAPL"
    name: "Apple Inc."
    aliases: ["APPLE", "APPLE INC"]
```

## Inputs consumed from SEC

The system uses multiple SEC sources depending on stage:

- the latest filings Atom feed for live discovery
- `hdr.sgml` for low-cost header resolution of ambiguous forms
- the filing complete `.txt` submission for full retrieval
- filing index HTML pages for primary-document fallback selection
- company submissions JSON and daily company index sources for reconciliation and backfill paths

## Lifecycle

1. **Discovery**: create or merge a `FilingDiscovery`.
2. **Relevance classification**:
   - direct forms can be resolved from the discovered archive CIK
   - ambiguous forms use header resolution to promote a canonical issuer when possible
3. **Retrieval**:
   - fetch the full `.txt`
   - parse the submission header
   - extract or fetch the primary document when possible
   - parse Form 4 / 8-K structured content when applicable
4. **Single DB transaction**:
   - persist header metadata
   - persist parties and artifact records
   - refresh normalized Form 4 / 8-K rows for the accession
   - update retrieval state
   - insert outbox events in the same transaction
5. **Publication**:
   - lease pending outbox rows in `commit_seq` order
   - publish via the configured callback
   - mark published or schedule retry with backoff
6. **Optional archival**:
   - archive cold artifacts
   - rewrite DB artifact paths transactionally
   - archive prior-UTC-day JSONL files at file level only

### State model

Relevance states:

- `unknown`
- `direct_match`
- `direct_unmatched`
- `hdr_pending`
- `hdr_match`
- `irrelevant`
- `hdr_failed`
- `hdr_transient_fail`
- `unresolved`

Retrieval states:

- `discovered`
- `queued`
- `in_progress`
- `retrieved_partial`
- `retrieved`
- `retrieval_failed`

`retrieved` is terminal. `retrieved_partial` and `retrieval_failed` are retryable.

## Retrieval behavior that matters downstream

The retriever is text-first:

1. fetch the complete `.txt` submission
2. parse SGML header metadata
3. choose the primary document from header metadata if possible
4. extract the primary document directly from the SGML container when possible
5. otherwise fetch the filing index and retrieve the selected primary document directly

Primary-document selection is form-aware:

- ownership forms prefer XML
- otherwise non-exhibit HTML is preferred, then XML, then TXT
- exhibit and graphic documents are excluded from primary selection

Artifacts are written atomically using temp-file write, fsync, rename, and directory fsync. Blocking filesystem work is offloaded to worker threads so the event loop is not stalled.

A full retrieval can still end in `retrieved_partial` when the submission `.txt` is persisted successfully but no selected primary document is available locally after extraction/fallback.

## Structured extracts

### Form 4

The parser emits issuer information, reporting owners, transactions, holdings, and footnotes.

Important downstream detail: for multi-owner filings, the implementation emits one transaction row per reporting owner, which makes ownership analytics and backtests less ambiguous.

Main normalized fields in `form4_transactions` include:

- issuer identity: `issuer_cik`, `issuer_name`, `issuer_ticker`
- owner identity: `reporting_owner_cik`, `reporting_owner_name`
- owner roles: `is_director`, `is_officer`, `officer_title`, `is_ten_pct_owner`
- transaction details: `security_title`, `transaction_date`, `transaction_code`, `shares`, `price_per_share`, `acquired_disposed`
- post-transaction state: `shares_owned_after`, `direct_indirect`, `is_derivative`

The live `edgar.form4.parsed` event is a compact summary. By default it includes at most 20 transaction summaries and at most 10 owner summaries even when the SQLite tables contain more complete detail. Use SQLite, not the live event payload, to build full historical research or training datasets.

### 8-K

The parser first uses SGML `ITEM INFORMATION`, then falls back to pattern matching in the primary document body. Duplicate item detections are deduplicated.

Normalized fields in `eight_k_events`:

- `accession_number`
- `item_number`
- `item_description`
- `filing_date`
- `company_name`
- `cik`

Important downstream detail: `filing_date` is **not format-stable across all code paths**. It can be ISO `YYYY-MM-DD`, compact `YYYYMMDD`, or `null`, depending on whether it came from the discovery object or SGML header fallback. Normalize it explicitly in downstream pipelines.

## SQLite schema for downstream use

Main tables:

- `filings`
- `filing_parties`
- `filing_documents`
- `checkpoints`
- `form4_transactions`
- `form4_holdings`
- `eight_k_events`
- `outbox_events`

### `filings`

This is the master filing table.

Key columns:

- identity: `accession_number`
- source identity: `archive_cik`, `company_name`, `form_type`, `source`
- timing: `filing_date`, `accepted_at`, `discovered_at`, `first_seen_at`, `last_seen_at`, `updated_at`
- SEC URLs: `filing_href`, `filing_index_url`, `complete_txt_url`, `hdr_sgml_url`, `primary_document_url`
- lifecycle: `relevance_state`, `retrieval_status`, `attempt_count`, `last_attempt_at`, `next_retry_at`, `inactive_reason`
- canonical issuer: `issuer_cik`, `issuer_name`, `issuer_name_normalized`
- metadata blobs: `discovery_metadata_json`, `header_metadata_json`
- artifact paths and hashes currently used by the retrieval commit path: `raw_txt_path`, `primary_doc_path`, `txt_sha256`, `primary_sha256`, `primary_document_url`
- additional nullable schema fields present but not populated by the current retrieval/commit path in the supplied code: `raw_index_path`, `index_sha256`

Treat `raw_index_path` and `index_sha256` as optional schema surface, not as guaranteed populated data.

### `filing_parties`

One row per filing-party role. Main fields: `accession_number`, `role`, `cik`, `name`, `name_normalized`.

### `filing_documents`

One row per persisted artifact record. Main fields: `artifact_type`, `source_url`, `local_path`, `sha256`, `content_type`, `metadata_json`.

In the supplied commit path, this table is populated for the selected primary document artifact when one is available. The raw submission `.txt` path is tracked on `filings.raw_txt_path` rather than inserted here as a `filing_documents` row.

### `form4_transactions`, `form4_holdings`, `eight_k_events`

These tables are refreshed per accession on reparse. Treat them as the latest normalized state for that filing accession.

### `outbox_events`

This is the durable live-event queue. Main fields include:

- `commit_seq`
- `event_id`
- `accession_number`
- `subject`
- `payload_json`
- `status`
- `publish_attempts`
- `next_attempt_at`
- `last_error`
- `created_at`
- `published_at`
- `leased_at`
- `lease_token`

`commit_seq` is the ordering key consumers should preserve for live replay. For migrated databases, older rows may have `commit_seq` backfilled rather than originally auto-assigned, but consumer ordering semantics remain `commit_seq`-first.

## Live event contract

Subjects:

- `edgar.filing.retrieved`
- `edgar.filing.retrieved_partial`
- `edgar.filing.failed`
- `edgar.form4.parsed`
- `edgar.8k.item_detected`
- `edgar.feed.gap_detected`

Envelope written to JSONL:

```json
{
  "event_id": "string",
  "subject": "string",
  "accession_number": "string",
  "payload": {"...": "..."},
  "created_at": "ISO-8601",
  "commit_seq": 123
}
```

Notes:

- JSONL files are written to `{publish_dir}/events-YYYY-MM-DD.jsonl` using the **current UTC date**.
- The default daemon launcher enables a **batch JSONL publisher** when `publish_dir` is configured; it appends multiple leased events with one fsync for the batch.
- Archiving of old JSONL files is file-level only and does not change outbox rows in SQLite.

### Delivery and ordering semantics

This is the most important contract for live consumers:

- event creation is transactional with filing-state persistence
- publication is asynchronous and separate from the DB commit
- the publisher leases rows, retries failures with exponential backoff, and resets stale leases on restart or expiry
- delivery is therefore **at least once**, not exactly once
- `event_id` is the idempotency key
- `commit_seq` is the monotonic ordering key
- consumers should be prepared for redelivery after publish failures or lease recovery

### Payloads

`edgar.filing.retrieved` and `edgar.filing.retrieved_partial`:

```json
{
  "archive_cik": "0000000000",
  "form_type": "8-K",
  "company_name": "Issuer Name",
  "issuer_cik": "0000000000",
  "issuer_name": "Issuer Name",
  "status": "retrieved|retrieved_partial",
  "txt_sha256": "hex|null",
  "primary_sha256": "hex|null",
  "primary_document_url": "url|null",
  "acceptance_datetime": "YYYYMMDDHHMMSS|null",
  "filing_date": "YYYYMMDD|null",
  "header_form_type": "string|null"
}
```

Notes:

- `filing_date` in this payload comes from `header.filed_as_of_date`, so the documented shape is compact `YYYYMMDD` or `null`.
- `header_form_type` can differ from the discovery-stage `form_type` when downstream consumers want to inspect the parsed header contract explicitly.

`edgar.form4.parsed`:

```json
{
  "issuer_cik": "...",
  "issuer_name": "...",
  "issuer_ticker": "...",
  "transaction_count": 0,
  "holding_count": 0,
  "owner_count": 0,
  "transactions": ["up to 20 compact summaries by default"],
  "reporting_owners": ["up to 10 compact owner summaries by default"]
}
```

`edgar.8k.item_detected`:

```json
{
  "item_number": "1.01",
  "item_description": "...",
  "company_name": "...",
  "cik": "...",
  "filing_date": "YYYY-MM-DD|YYYYMMDD|null"
}
```

`edgar.filing.failed`:

```json
{
  "archive_cik": "...",
  "form_type": "...",
  "error": "truncated to 500 chars",
  "attempt_no": 1
}
```

`edgar.feed.gap_detected`:

```json
{
  "watermark_ts": "ISO-8601",
  "pages_checked": 3
}
```

Note: this event uses envelope-level `accession_number = "N/A"`.

## Filesystem outputs and archival behavior

The daemon writes raw artifacts under the raw directory by archive CIK and accession-without-dashes, including the full `.txt` submission and, when available, the selected primary document.

The default publisher writes UTC date-partitioned JSONL files:

```text
{publish_dir}/events-YYYY-MM-DD.jsonl
```

The archiver may later:

- move cold filing artifacts under `archive_dir` while rewriting their DB locations transactionally
- move prior-UTC-day JSONL files under `archive_dir/events/`
- leave the current UTC-day JSONL file untouched

Do not reconstruct raw paths heuristically. Always resolve artifact paths from the database.

## Observability

When `--metrics-enabled` is set, the daemon exposes an in-process HTTP server with:

- `/metrics` for Prometheus-style text exposition
- `/healthz` for a JSON health summary

Relevant metric families include SEC HTTP request timing, discovery and retrieval counts, queue depths, end-to-end discovery-to-event latency, event-to-publish latency, and archival counters/hooks. Metrics are optional and in-memory only; they do not change the persistence contract.

## Guidance for research, EDA, dataset creation, and backtests

Use SQLite as the historical source.

Suggested joins and filters:

- filing master dataset: `filings`
- issuer/entity normalization: `filings` + `filing_parties`
- artifact lookup: `filings` + `filing_documents`
- insider datasets: `form4_transactions`, `form4_holdings`
- 8-K event datasets: `eight_k_events`
- live/historical join key: `accession_number`

Recommended downstream rules:

- prefer `issuer_cik` over `archive_cik` when populated
- for strict completeness, filter to `retrieval_status = 'retrieved'`
- for faster but less complete studies, include `retrieved_partial` and tolerate missing primary-document-derived features
- normalize 8-K `filing_date` before modeling or joins
- use `accepted_at` for event timing and `filing_date` for filing-day grouping
- exclude clearly irrelevant filings with `relevance_state` filters
- use `attempt_count` and `next_retry_at` to understand recovery behavior in production-like simulations
- treat `outbox_events` and JSONL as replay surfaces for live semantics, not as substitutes for the normalized tables
- for archived datasets, always rely on DB-resolved artifact paths rather than assuming files still live under the hot `raw_dir`

Replay guidance:

- for historical backtests based on final persisted state, replay from SQLite ordered by `accepted_at`
- for production-like event simulation, replay the outbox or JSONL stream ordered by `commit_seq`, deduplicating on `event_id`

## Guidance for latency-sensitive inference pipelines

This project can feed a latency-sensitive pipeline, but it is not a hard-real-time message bus.

Important caveats:

- SQLite uses WAL mode with many writers serialized by SQLite itself, so p99 latency can couple to WAL checkpoint pressure under concurrent writes
- JSONL publishing performs flush and fsync work for durability, although blocking file I/O is offloaded to worker threads so the event loop is not stalled
- the live lane is protected with a dedicated share of SEC request capacity, but retrieval and persistence are still bounded by SEC response time, filesystem latency, and SQLite contention
- the live Form 4 event is a bounded summary payload; if inference needs full ownership detail, the consumer must read SQLite or artifact content

Practical design guidance:

1. consume live signals from the outbox or JSONL layer, not from raw file mtimes
2. make downstream consumers idempotent on `event_id`
3. preserve ordering by `commit_seq`
4. treat `retrieved_partial` as a usable but incomplete signal
5. keep strategy logic, market-data joins, feature stores, and execution outside this codebase

## Restart and failure behavior

The daemon is designed to resume work after interruptions.

It has replay and recovery paths for:

- retry candidates
- pending header-gate work
- transient header failures
- stranded queued or in-progress retrievals
- discoveries persisted before a crash but not yet classified
- stale outbox leases from a prior run

This improves operational continuity, but it also reinforces the need for downstream idempotency.
