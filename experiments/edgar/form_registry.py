"""Form handler protocol and dispatch registry.

The registry owns dispatch by normalized form family.  New form handlers
register here; the retrieval and commit layers consult it instead of
hardcoded ``if form_upper.startswith(...)`` branches.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Protocol

from domain import (
    FilingDiscovery,
    SubmissionHeader,
    get_logger,
)


logger = get_logger(__name__)


class FormHandler(Protocol):
    """Minimal contract for form-family-specific processing."""

    def supports(self, form_type: str) -> bool:
        """Return True if this handler handles *form_type*."""
        ...

    def parse(
        self,
        *,
        accession_number: str,
        header: SubmissionHeader,
        primary_bytes: bytes | None,
        discovery: FilingDiscovery,
    ) -> Any | None:
        """Extract structured data from the primary document.

        Returns a handler-specific parsed object, or None on failure.
        """
        ...

    def persist(
        self,
        conn: sqlite3.Connection,
        accession_number: str,
        parsed: Any,
        now_iso: str,
    ) -> None:
        """Write handler-specific rows inside an existing transaction."""
        ...

    def build_events(
        self,
        accession_number: str,
        parsed: Any,
        **kwargs: Any,
    ) -> list[Any]:
        """Build domain event envelopes for the parsed data."""
        ...


class FormRegistry:
    """Central dispatch for registered form handlers.

    Handlers are checked in registration order; the **first** handler
    whose ``supports()`` returns True wins.
    """

    def __init__(self) -> None:
        self._handlers: list[FormHandler] = []

    def register(self, handler: FormHandler) -> None:
        self._handlers.append(handler)
        logger.info("registered form handler: %s", type(handler).__name__)

    def get_handler(self, form_type: str) -> FormHandler | None:
        """Return the first handler that supports *form_type*, or None."""
        for handler in self._handlers:
            if handler.supports(form_type):
                return handler
        return None

    @property
    def handlers(self) -> list[FormHandler]:
        return list(self._handlers)
