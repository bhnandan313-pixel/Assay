"""JSONPath normalization and exclusion pattern matching utilities."""

from __future__ import annotations

import re
from typing import List

_BRACKET_KEY_RE = re.compile(r"\['([^']+)'\]|\[\"([^\"]+)\"\]")
_BRACKET_INDEX_RE = re.compile(r"\[(\d+)\]")
_SEGMENT_RE = re.compile(r"[^.\[\]]+|\[\*\]|\[\d+\]")


def normalize_deepdiff_path(raw_path: str) -> str:
    """Convert DeepDiff path notation (e.g. ``root['user']['age']``) to JSONPath.

    Args:
        raw_path: Path string produced by ``deepdiff.DeepDiff`` or JSONPath.

    Returns:
        Normalized dotted JSONPath such as ``$.user.age`` or ``$.items[0].id``.
    """
    if not raw_path or raw_path == "root":
        return "$"

    path = raw_path.strip()
    if path.startswith("root"):
        path = "$" + path[4:]
    elif not path.startswith("$"):
        path = f"$.{path}"

    def _replace_key(match: re.Match[str]) -> str:
        key = match.group(1) if match.group(1) is not None else match.group(2)
        return f".{key}"

    path = _BRACKET_KEY_RE.sub(_replace_key, path)
    path = path.replace(".[", "[")
    return path


def _tokenize_jsonpath(path: str) -> List[str]:
    """Split a JSONPath or bare key expression into comparable path tokens."""
    cleaned = path.strip()
    if cleaned == "$":
        return []
    if cleaned.startswith("$."):
        cleaned = cleaned[2:]
    elif cleaned.startswith("$"):
        cleaned = cleaned[1:]
    return _SEGMENT_RE.findall(cleaned)


def _match_tokens(path_tokens: List[str], pattern_tokens: List[str]) -> bool:
    """Match normalized path tokens against pattern tokens supporting `*` and `[*]`."""
    if len(path_tokens) != len(pattern_tokens):
        return False

    for path_tok, pat_tok in zip(path_tokens, pattern_tokens):
        if pat_tok == "*":
            continue
        if pat_tok == "[*]":
            if not _BRACKET_INDEX_RE.fullmatch(path_tok):
                return False
            continue
        if path_tok != pat_tok:
            return False
    return True


def should_ignore_path(current_path: str, ignore_patterns: List[str]) -> bool:
    """Determine whether ``current_path`` matches any exclusion pattern.

    Supports:
    - Exact JSONPath matches (e.g. ``$.meta.created_at``)
    - Wildcard segments (e.g. ``$.*.updated_at``, ``$.items[*].id``)
    - Bare key names (e.g. ``id``, ``timestamp``) matching any field with that name

    Args:
        current_path: Normalized JSONPath or raw DeepDiff path.
        ignore_patterns: List of exclusion patterns.

    Returns:
        ``True`` if the path should be ignored, ``False`` otherwise.
    """
    if not ignore_patterns:
        return False

    normalized_path = normalize_deepdiff_path(current_path)
    path_tokens = _tokenize_jsonpath(normalized_path)

    for raw_pattern in ignore_patterns:
        pattern = raw_pattern.strip()
        if not pattern:
            continue

        if pattern == normalized_path:
            return True

        if not pattern.startswith("$") and "." not in pattern and "[" not in pattern:
            if pattern in path_tokens:
                return True
            continue

        pattern_tokens = _tokenize_jsonpath(pattern)
        if _match_tokens(path_tokens, pattern_tokens):
            return True

    return False
