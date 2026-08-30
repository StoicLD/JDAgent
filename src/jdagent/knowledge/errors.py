"""Stable knowledge-module errors that do not leak secrets."""

from enum import StrEnum


class KnowledgeErrorCode(StrEnum):
    """Stable failures at the knowledge application seam."""

    CATALOG_CORRUPT = "catalog_corrupt"
    NAME_CONFLICT = "name_conflict"
    NOT_FOUND = "not_found"
    STILL_REFERENCED = "still_referenced"
    CONFIRMATION_REQUIRED = "confirmation_required"
    INVALID_CREDENTIAL_REF = "invalid_credential_ref"
    INVALID_ARGUMENT = "invalid_argument"
    KNOWLEDGE_BUSY = "knowledge_busy"
    DEPENDENCY_MISSING = "dependency_missing"
    SOURCE_TOO_LARGE = "source_too_large"
    BATCH_TOO_LARGE = "batch_too_large"
    CSV_FIELD_TOO_LARGE = "csv_field_too_large"
    ENCODING_AMBIGUOUS = "encoding_ambiguous"
    ENCODING_FAILED = "encoding_failed"
    PARSE_FAILED = "parse_failed"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    SOURCE_CORRUPT = "source_corrupt"
    ACCESS_DENIED = "access_denied"


class KnowledgeError(RuntimeError):
    """A classified knowledge-module failure."""

    def __init__(self, code: KnowledgeErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
