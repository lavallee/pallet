"""Errors carry the exit code the CLI returns, so callers can branch on it."""

from __future__ import annotations

from typing import Any


class PalletError(Exception):
    """Base error. ``kind`` and ``exit_code`` are part of the CLI contract."""

    exit_code = 1
    kind = "error"

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class SpecError(PalletError):
    """A spec, contract, issue, or shaping rule is invalid, or data does not conform."""

    exit_code = 3
    kind = "invalid"


class FetchError(PalletError):
    """A source could not be retrieved, or custody for it is missing."""

    exit_code = 4
    kind = "fetch"


class VerifyError(PalletError):
    """Bytes on disk do not match the hashes that describe them."""

    exit_code = 5
    kind = "verify"


class RefusedError(PalletError):
    """The dataset's rights do not allow the requested action."""

    exit_code = 6
    kind = "refused"


class PreconditionError(PalletError):
    """The store or the dataset's lifecycle is not ready for this step."""

    exit_code = 7
    kind = "precondition"


class NotFoundError(PalletError):
    """A dataset, release, or file does not exist."""

    exit_code = 8
    kind = "not_found"


EXIT_CODES = {
    0: "ok",
    1: "unexpected error",
    2: "usage error (bad arguments)",
    SpecError.exit_code: "invalid spec, contract, issue, or non-conforming data",
    FetchError.exit_code: "fetch failed or custody missing",
    VerifyError.exit_code: "verification failed (hash, size, or structure mismatch)",
    RefusedError.exit_code: "refused by the dataset's rights",
    PreconditionError.exit_code: "precondition not met (store or lifecycle)",
    NotFoundError.exit_code: "not found",
}
