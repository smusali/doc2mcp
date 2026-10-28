from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    INTERNAL = 1
    USAGE = 2
    GAP = 3
    UPSTREAM = 4


class Mcp4apiError(Exception):
    exit_code: ExitCode

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UsageError(Mcp4apiError):
    exit_code = ExitCode.USAGE


class UpstreamError(Mcp4apiError):
    exit_code = ExitCode.UPSTREAM


class GapError(Mcp4apiError):
    exit_code = ExitCode.GAP

    def __init__(self, message: str, *, printed: bool) -> None:
        super().__init__(message)
        self.printed = printed
