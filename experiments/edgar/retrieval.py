"""Filing retrieval pipeline — text-first approach with form dispatch."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from domain import (
    EightKEvent,
    FilingArtifact,
    FilingDiscovery,
    Form4Filing,
    RetrievedFilingBundle,
    SubmissionHeader,
    _OWNERSHIP_FORM_RE,
    _13F_FORM_RE,
    accession_nodashes,
    derive_archive_base,
    derive_complete_txt_url,
    derive_hdr_sgml_url,
    derive_index_url,
    filename_from_url,
    get_logger,
    guess_content_type_from_filename,
    is_textual_primary_filename,
    safe_filename,
    sha256_hex,
    utcnow,
)
from sec_io import (
    SECClient,
    SECHTTPError,
    choose_primary_document,
    choose_primary_document_from_header,
    extract_primary_document_bytes,
    normalized_header_metadata,
    parse_index_document_rows,
    parse_submission_text,
)
from storage import SQLiteStorage
from form_registry import FormRegistry
from event_outbox import ArtifactWriter, FilingCommitService

from metrics import METRICS


logger = get_logger(__name__)

class FilingRetriever:
    """Downloads filing artifacts using the text-first approach."""

    def __init__(self, client: SECClient, storage: SQLiteStorage, raw_dir: Path,
                 retry_base_seconds: float = 2.0,
                 commit_service: FilingCommitService | None = None,
                 out_form4_transactions_cap: int = 20,
                 out_form4_owners_cap: int = 10,
                 form_registry: FormRegistry | None = None,
    ) -> None:
        self.client = client
        self.storage = storage
        self.raw_dir = raw_dir
        self.retry_base_seconds = retry_base_seconds
        self.commit_service = commit_service
        self.out_form4_transactions_cap = out_form4_transactions_cap
        self.out_form4_owners_cap = out_form4_owners_cap
        self.form_registry = form_registry or FormRegistry()
        self._artifact_writer = ArtifactWriter()

    async def fetch_header_only(self, discovery: FilingDiscovery) -> SubmissionHeader:
        """Fetch and parse hdr.sgml for header-gate resolution.
        Falls back to complete .txt if hdr.sgml is missing or malformed.
        """
        hdr_url = discovery.hdr_sgml_url or derive_hdr_sgml_url(
            discovery.archive_cik, discovery.accession_number,
        )
        try:
            text = await self.client.get_text(hdr_url)
            header = parse_submission_text(text)
            if header.parties:
                return header
            logger.warning(
                "hdr.sgml parsed but no parties found for %s — falling back to .txt",
                discovery.accession_number,
            )
        except SECHTTPError as exc:
            if exc.status == 404:
                logger.info("hdr.sgml not found for %s — falling back to .txt", discovery.accession_number)
            else:
                logger.warning("hdr.sgml fetch failed for %s (HTTP %d)", discovery.accession_number, exc.status)
        except Exception:
            logger.exception("hdr.sgml parse failed for %s", discovery.accession_number)

        txt_url = discovery.complete_txt_url or derive_complete_txt_url(
            discovery.archive_cik, discovery.accession_number,
        )
        text = await self.client.get_text(txt_url)
        return parse_submission_text(text)

    # 13F information table resolution helpers

    @staticmethod
    def _find_13f_infotable(
        header: SubmissionHeader,
        txt_bytes: bytes,
        primary_filename: str | None,
    ) -> bytes | None:
        """Search the SGML container for the 13F information table XML.

        For 13F filings, the primary document selector often picks the
        cover HTML (doc_type "13F-HR") instead of the separate
        INFORMATION TABLE XML exhibit.  This method looks through the
        header's document list for the infotable and extracts it from
        the SGML container.
        """
        _INFOTABLE_PATTERNS = (
            "information table", "infotable", "info_table",
        )

        for doc in header.documents:
            doc_type = (doc.doc_type or "").upper().strip()
            filename = (doc.filename or "").lower()
            desc = (doc.description or "").lower()

            # Skip the primary document — we already have that
            if doc.filename and primary_filename and doc.filename.lower() == primary_filename.lower():
                continue

            # Match by doc_type or description or filename
            is_infotable = (
                "INFORMATION TABLE" in doc_type
                or any(p in desc for p in _INFOTABLE_PATTERNS)
                or any(p in filename for p in _INFOTABLE_PATTERNS)
            )

            if is_infotable and doc.filename:
                extracted = extract_primary_document_bytes(txt_bytes, doc.filename)
                if extracted is not None:
                    return extracted

        return None

    async def _fetch_13f_infotable_from_index(
        self,
        cik: str,
        acc: str,
        form_type: str | None,
    ) -> bytes | None:
        """Fetch the 13F information table via index page lookup.

        Falls back to HTTP fetch when the infotable is not in the SGML
        container (which happens for many real-world 13F filings).
        """
        _INFOTABLE_PATTERNS = (
            "information table", "infotable", "info_table",
        )

        index_url = derive_index_url(cik, acc)
        try:
            index_bytes, _ = await self.client.get_bytes(index_url)
            index_html = index_bytes.decode("utf-8", errors="replace")
            doc_rows = parse_index_document_rows(index_html)

            for row in doc_rows:
                doc_type = (row.doc_type or "").upper().strip()
                filename = (row.filename or "").lower()
                desc = (row.description or "").lower()

                is_infotable = (
                    "INFORMATION TABLE" in doc_type
                    or any(p in desc for p in _INFOTABLE_PATTERNS)
                    or any(p in filename for p in _INFOTABLE_PATTERNS)
                )

                if is_infotable and (row.href or row.filename):
                    from urllib.parse import urljoin
                    import html as _html
                    if row.href:
                        fetch_url = urljoin(index_url, _html.unescape(row.href))
                    else:
                        fetch_url = f"{derive_archive_base(cik, acc)}/{row.filename}"
                    try:
                        data, _ = await self.client.get_bytes(fetch_url)
                        return data
                    except Exception:
                        logger.warning(
                            "failed to fetch 13F infotable from %s", fetch_url,
                        )

        except Exception:
            logger.warning("13F index lookup failed for %s", acc)

        return None

    async def retrieve_full(self, discovery: FilingDiscovery) -> bool:
        """Full retrieval: text-first, extract primary doc, parse structured forms.

        When a ``commit_service`` is available, all DB writes (header metadata,
        filing parties, artifact records, Form 4 rows, 8-K rows, retrieval
        status, **and** outbox events) are committed in a single SQLite
        transaction.  Filesystem writes use atomic temp-fsync-rename.

        **All blocking filesystem (fsync/rename) and SQLite work is offloaded
        to a thread pool** so the event loop is never stalled by disk flushes
        or WAL checkpoint pressure.

        Falls back to the original per-call storage pattern when no commit
        service is configured (backward compatibility).
        """
        acc = discovery.accession_number
        cik = discovery.archive_cik
        acc_dir = self.raw_dir / cik / accession_nodashes(acc)
        await asyncio.to_thread(acc_dir.mkdir, parents=True, exist_ok=True)
        txt_url = discovery.complete_txt_url or derive_complete_txt_url(cik, acc)

        try:
            # 1. Fetch complete submission text (.txt)
            logger.info("retrieving %s: fetching complete .txt", acc)
            txt_bytes, _ = await self.client.get_bytes(txt_url)
            txt_path = acc_dir / f"{acc}.txt"
            # Atomic write offloaded to thread: temp → fsync → rename
            txt_hash = await self._artifact_writer.write_atomic_async(txt_path, txt_bytes)

            # 2. Parse SGML header
            txt_decoded = txt_bytes.decode("utf-8", errors="replace")
            header = parse_submission_text(txt_decoded)

            canonical = header.canonical_issuer()

            # 3. Extract primary document from .txt
            preferred_filename = filename_from_url(discovery.primary_document_url)
            chosen_doc = choose_primary_document_from_header(header, preferred_filename)
            target_filename = (
                chosen_doc.filename if chosen_doc and chosen_doc.filename else preferred_filename
            )

            pdoc_path_str: str | None = None
            pdoc_hash: str | None = None
            primary_url: str | None = None
            extracted_bytes: bytes | None = None
            artifact: FilingArtifact | None = None

            # When header parsing produces no documents and
            # discovery.primary_document_url is absent, target_filename is
            # None.  Go directly to index lookup before accepting a partial
            # retrieval.
            if target_filename is None:
                logger.info(
                    "no primary document from header for %s — consulting filing index",
                    acc,
                )
                index_url = derive_index_url(cik, acc)
                try:
                    index_bytes, _ = await self.client.get_bytes(index_url)
                    alt = choose_primary_document(
                        index_bytes.decode("utf-8", errors="replace"),
                        index_url, form_type=header.form_type,
                    )
                    if alt:
                        target_filename = alt.rsplit("/", 1)[-1] if "/" in alt else alt
                        primary_url = alt if alt.startswith("http") else f"{derive_archive_base(cik, acc)}/{target_filename}"
                except Exception:
                    logger.warning("index lookup for missing primary doc failed for %s", acc)

            if target_filename:
                if primary_url is None:
                    primary_url = f"{derive_archive_base(cik, acc)}/{target_filename}"
                extracted_bytes = extract_primary_document_bytes(txt_bytes, target_filename)
                if extracted_bytes is not None:
                    pdoc_name = safe_filename(target_filename)
                    pdoc_path = acc_dir / pdoc_name
                    pdoc_hash = await self._artifact_writer.write_atomic_async(pdoc_path, extracted_bytes)
                    pdoc_path_str = str(pdoc_path)
                    artifact = FilingArtifact(
                        accession_number=acc, artifact_type="primary_document",
                        source_url=primary_url, local_path=pdoc_path,
                        sha256=pdoc_hash,
                        content_type=guess_content_type_from_filename(target_filename),
                        metadata={"extraction_method": "sgml_txt_container"},
                    )
                else:
                    # Fallback: fetch index + HTTP
                    logger.warning(
                        "primary doc %s not in SGML container for %s — trying fallback",
                        target_filename, acc,
                    )
                    index_url = derive_index_url(cik, acc)
                    try:
                        index_bytes, _ = await self.client.get_bytes(index_url)
                        alt = choose_primary_document(
                            index_bytes.decode("utf-8", errors="replace"),
                            index_url, form_type=header.form_type,
                        )
                        if alt:
                            target_filename = alt.rsplit("/", 1)[-1] if "/" in alt else alt
                            primary_url = alt if alt.startswith("http") else f"{derive_archive_base(cik, acc)}/{target_filename}"
                    except Exception:
                        logger.warning("index fallback fetch failed for %s", acc)

                    if target_filename and primary_url:
                        try:
                            pdoc_bytes, ct = await self.client.get_bytes(primary_url)
                            pdoc_name = safe_filename(target_filename)
                            pdoc_path = acc_dir / pdoc_name
                            pdoc_hash = await self._artifact_writer.write_atomic_async(pdoc_path, pdoc_bytes)
                            pdoc_path_str = str(pdoc_path)
                            extracted_bytes = pdoc_bytes
                            artifact = FilingArtifact(
                                accession_number=acc, artifact_type="primary_document",
                                source_url=primary_url, local_path=pdoc_path,
                                sha256=pdoc_hash,
                                content_type=ct or guess_content_type_from_filename(target_filename),
                                metadata={"extraction_method": "http_direct_fallback"},
                            )
                        except Exception:
                            logger.exception("fallback HTTP fetch failed for %s", acc)

            # For 13F filings, the primary document is often
            # the cover HTML, not the information-table XML.  Detect and
            # fetch the information table explicitly so the handler
            # receives the correct document.
            form_upper = (header.form_type or "").upper().strip()
            infotable_bytes: bytes | None = None

            if _13F_FORM_RE.fullmatch(form_upper):
                infotable_bytes = self._find_13f_infotable(
                    header, txt_bytes, target_filename,
                )
                if infotable_bytes is None:
                    # Try index-based lookup for the infotable document
                    infotable_bytes = await self._fetch_13f_infotable_from_index(
                        cik, acc, header.form_type,
                    )
                if infotable_bytes is not None:
                    logger.info(
                        "13F info table resolved for %s (%d bytes)",
                        acc, len(infotable_bytes),
                    )
                else:
                    logger.warning(
                        "13F info table not found for %s — handler will receive "
                        "primary document (may be cover HTML)",
                        acc,
                    )

            # 4. Structured extraction via form handler registry
            #    Use get_handler() for first-match semantics per the
            #    FormRegistry contract.
            form_results: dict[str, Any] = {}

            handler = self.form_registry.get_handler(form_upper)
            if handler is not None:
                handler_name = type(handler).__name__
                # For 13F, pass the information table bytes instead of
                # the primary document when available
                handler_bytes = extracted_bytes
                if infotable_bytes is not None and handler_name == "ThirteenFHandler":
                    handler_bytes = infotable_bytes
                parsed = handler.parse(
                    accession_number=acc,
                    header=header,
                    primary_bytes=handler_bytes,
                    discovery=discovery,
                )
                if parsed is not None:
                    form_results[handler_name] = parsed

            # 5. Build the bundle
            bundle = RetrievedFilingBundle(
                accession_number=acc,
                archive_cik=cik,
                form_type=discovery.form_type,
                company_name=discovery.company_name,
                header=header,
                canonical_cik=canonical.cik if canonical else None,
                canonical_name=canonical.name if canonical else None,
                canonical_name_normalized=canonical.name_normalized if canonical else None,
                txt_path=str(txt_path),
                txt_sha256=txt_hash,
                primary_doc_path=pdoc_path_str,
                primary_sha256=pdoc_hash,
                primary_document_url=primary_url,
                artifact=artifact,
                form_results=form_results,
            )

            # 6. Commit — offloaded to thread to avoid blocking the event loop
            #    with SQLite transactions and WAL checkpoint pressure.
            if self.commit_service is not None:
                await asyncio.to_thread(
                    self.commit_service.commit_retrieved_filing,
                    bundle=bundle,
                    form_registry=self.form_registry,
                    retry_base_seconds=self.retry_base_seconds,
                    out_form4_transactions_cap=self.out_form4_transactions_cap,
                    out_form4_owners_cap=self.out_form4_owners_cap,
                )
            else:
                # Legacy path: separate storage calls (no outbox) — also offloaded
                def _legacy_commit() -> None:
                    header_meta = normalized_header_metadata(header)
                    self.storage.save_header_metadata(acc, header_meta)
                    if header.parties:
                        self.storage.save_filing_parties(acc, header.parties)
                    if canonical:
                        self.storage.promote_canonical_issuer(
                            acc, canonical.cik, canonical.name, canonical.name_normalized,
                        )
                    if artifact:
                        self.storage.attach_artifact(artifact)
                    # Delegate form-specific persistence to handlers
                    # Use a proper context-managed connection so the
                    # handler writes are transactional and the connection
                    # is always closed
                    for handler in self.form_registry.handlers:
                        handler_name = type(handler).__name__
                        if handler_name in bundle.form_results:
                            with self.storage._conn() as conn:
                                handler.persist(
                                    conn,
                                    acc, bundle.form_results[handler_name],
                                    utcnow().isoformat(),
                                )
                                conn.commit()
                    final_status_inner = "retrieved" if pdoc_path_str else "retrieved_partial"
                    self.storage.update_retrieval_status(
                        acc, final_status_inner,
                        retry_base_seconds=self.retry_base_seconds,
                        raw_txt_path=str(txt_path),
                        primary_doc_path=pdoc_path_str,
                        txt_sha256=txt_hash,
                        primary_sha256=pdoc_hash,
                        primary_document_url=primary_url,
                    )
                await asyncio.to_thread(_legacy_commit)

            final_status = "retrieved" if pdoc_path_str else "retrieved_partial"
            if final_status == "retrieved":
                METRICS.inc("edgar_filings_retrieved_total")
            else:
                METRICS.inc("edgar_filings_partial_total")
            logger.info("retrieval complete for %s (status=%s)", acc, final_status)
            return True

        except Exception as exc:
            METRICS.inc("edgar_filings_failed_total")
            logger.exception("retrieval failed for %s", acc)
            if self.commit_service is not None:
                await asyncio.to_thread(
                    self.commit_service.commit_failed_filing,
                    accession_number=acc,
                    archive_cik=cik,
                    form_type=discovery.form_type,
                    error=str(exc),
                    retry_base_seconds=self.retry_base_seconds,
                )
            else:
                await asyncio.to_thread(
                    self.storage.update_retrieval_status,
                    acc, "retrieval_failed",
                    retry_base_seconds=self.retry_base_seconds,
                )
            return False
