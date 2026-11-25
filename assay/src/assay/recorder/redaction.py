"""Sensitive data redaction engine for HTTP headers and payloads."""

from __future__ import annotations

import json
from typing import Any, List, Optional, Sequence
from urllib.parse import parse_qsl, urlencode

from assay.models.recording import HTTPHeader

REDACTED_VALUE = "[REDACTED]"

DEFAULT_SENSITIVE_HEADERS: tuple[str, ...] = (
    "Authorization",
    "Cookie",
    "Set-Cookie",
    "X-Api-Key",
)

DEFAULT_SENSITIVE_BODY_FIELDS: tuple[str, ...] = (
    "password",
    "token",
    "secret",
    "credit_card",
    "cvv",
)


class Redactor:
    """Redacts sensitive headers and body fields before persistence."""

    def __init__(
        self,
        header_names: Optional[Sequence[str]] = None,
        body_fields: Optional[Sequence[str]] = None,
    ) -> None:
        """Initialize a Redactor with case-insensitive header and field sets.

        Args:
            header_names: Header names whose values should be redacted.
            body_fields: JSON/form field keys whose values should be redacted.
        """
        headers = (
            DEFAULT_SENSITIVE_HEADERS if header_names is None else header_names
        )
        fields = (
            DEFAULT_SENSITIVE_BODY_FIELDS if body_fields is None else body_fields
        )
        self.header_names: set[str] = {name.lower() for name in headers}
        self.body_fields: set[str] = {field.lower() for field in fields}

    @classmethod
    def from_field_list(cls, fields: Sequence[str]) -> Redactor:
        """Build a Redactor from a unified list of sensitive field/header names.

        Args:
            fields: Combined list of header names and body keys to redact.

        Returns:
            Configured ``Redactor`` instance applying the list to both headers
            and body payloads.
        """
        return cls(header_names=fields, body_fields=fields)

    def redact_headers(self, headers: List[HTTPHeader]) -> List[HTTPHeader]:
        """Return a copy of headers with sensitive values replaced.

        Args:
            headers: List of captured ``HTTPHeader`` items.

        Returns:
            New list of ``HTTPHeader`` objects with matching names redacted.
        """
        redacted: List[HTTPHeader] = []
        for header in headers:
            if header.name.lower() in self.header_names:
                redacted.append(HTTPHeader(name=header.name, value=REDACTED_VALUE))
            else:
                redacted.append(HTTPHeader(name=header.name, value=header.value))
        return redacted

    def _redact_json_value(self, value: Any) -> Any:
        """Recursively redact matching keys in parsed JSON structures."""
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                if str(key).lower() in self.body_fields:
                    result[key] = REDACTED_VALUE
                else:
                    result[key] = self._redact_json_value(item)
            return result
        if isinstance(value, list):
            return [self._redact_json_value(element) for element in value]
        return value

    def redact_body(self, body_content: str, content_type: Optional[str]) -> str:
        """Redact sensitive fields from JSON or form-urlencoded body content.

        Args:
            body_content: Raw decoded body string.
            content_type: HTTP ``Content-Type`` header value, if present.

        Returns:
            Redacted body string, or the original ``body_content`` if the
            content type is not structured or parsing fails.
        """
        if not body_content:
            return body_content

        media_type = (content_type or "").split(";")[0].strip().lower()

        if media_type == "application/json" or media_type.endswith("+json"):
            try:
                parsed = json.loads(body_content)
            except (json.JSONDecodeError, TypeError, ValueError):
                return body_content
            redacted = self._redact_json_value(parsed)
            return json.dumps(redacted)

        if media_type == "application/x-www-form-urlencoded":
            try:
                pairs = parse_qsl(body_content, keep_blank_values=True)
            except (TypeError, ValueError):
                return body_content
            if not pairs:
                return body_content
            redacted_pairs = [
                (
                    key,
                    REDACTED_VALUE if key.lower() in self.body_fields else val,
                )
                for key, val in pairs
            ]
            return urlencode(redacted_pairs)

        return body_content
