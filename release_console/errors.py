"""Errors the admin API maps to HTTP status codes."""

from __future__ import annotations


class ReleaseError(Exception):
    pass


class ValidationError(ReleaseError):
    pass


class Conflict(ReleaseError):
    pass


class NotFound(ReleaseError):
    pass


class NotWired(ReleaseError):
    pass


class PublishRejected(ReleaseError):
    def __init__(self, record: dict[str, object]):
        message = str(record.get("result_message") or "发布未被接受")
        super().__init__(message)
        self.record = record
