"""Exception types mapped to CLI exit codes (D12)."""

from __future__ import annotations

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_USAGE = 2


class AuditError(Exception):
    """Fatal runtime error: the audit could not be completed (exit code 1)."""

    exit_code = EXIT_FATAL


class InvalidInputError(AuditError):
    """Invalid user input such as a malformed repository URL (exit code 2)."""

    exit_code = EXIT_USAGE
