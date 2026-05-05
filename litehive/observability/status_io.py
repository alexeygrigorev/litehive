"""
File and parse helpers for the ``litehive status`` loaders.

Lets the loaders degrade gracefully on corrupt or unreadable
YAML/JSON: each helper returns a ``(value, issue)`` pair so the
caller can record a structured diagnostic and keep going,
instead of taking down the whole status surface for one bad
file.
"""

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any, Mapping

import yaml
from pydantic import ValidationError

from litehive.observability.status_types import StatusIssue


def _safe_yaml_mapping(
    path: Path,
    key: str,
    remediation: str,
) -> tuple[dict[str, Any] | None, StatusIssue | None]:
    """
    Read a YAML file expected to contain a top-level mapping.

    On parse error or wrong shape returns a :class:`StatusIssue`
    instead of raising, so the config loader can keep merging
    the layers it *could* read; one corrupt file should not
    blank out an otherwise-readable workspace config.
    """
    data, issue = _safe_yaml_document(path, key=key, remediation=remediation)
    if issue is not None:
        return None, issue
    if data is None:
        return {}, None
    if not isinstance(data, Mapping):
        return None, StatusIssue(
            key=key,
            severity="ERROR",
            message=f"INVALID at {path} (expected YAML mapping) — {remediation}",
        )
    return dict(data), None


def _safe_yaml_document(
    path: Path,
    key: str,
    remediation: str,
) -> tuple[object | None, StatusIssue | None]:
    """
    Read a YAML file, converting parse and IO errors into status issues.

    Each error becomes one diagnostic line carrying the file
    location info, so corrupt config surfaces as one issue
    instead of crashing ``status``/``health``. Distinct from
    :func:`_safe_yaml_mapping` because some YAML callers accept
    non-mapping documents (lists, scalars).
    """
    if not path.exists():
        return None, None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")), None
    except yaml.YAMLError as exc:
        return None, StatusIssue(
            key=key,
            severity="ERROR",
            message=f"CORRUPT at {path} ({_yaml_location_label(exc)}) — {remediation}",
        )
    except OSError as exc:
        detail = exc.strerror or str(exc)
        return None, StatusIssue(
            key=key,
            severity="ERROR",
            message=f"UNREADABLE at {path} ({detail}) — {remediation}",
        )


def _safe_json_mapping(
    path: Path,
    key: str,
    remediation: str,
) -> tuple[dict[str, Any] | None, StatusIssue | None]:
    """
    Read a JSON file expected to contain a top-level object.

    Missing or blank files return ``({}, None)`` so the caller
    can treat absence as "default state", while parse and shape
    errors return a structured status issue. Used by the
    runner-state loader so a corrupt status file degrades to a
    visible diagnostic rather than a missed report.
    """
    if not path.exists():
        return None, None
    try:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            return {}, None
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, StatusIssue(
            key=key,
            severity="ERROR",
            message=f"CORRUPT at {path} (line {exc.lineno}) — {remediation}",
        )
    except OSError as exc:
        detail = exc.strerror or str(exc)
        return None, StatusIssue(
            key=key,
            severity="ERROR",
            message=f"UNREADABLE at {path} ({detail}) — {remediation}",
        )
    if data is None:
        return {}, None
    if not isinstance(data, Mapping):
        return None, StatusIssue(
            key=key,
            severity="ERROR",
            message=f"INVALID at {path} (expected JSON object) — {remediation}",
        )
    return dict(data), None


def _heartbeat_age_seconds(heartbeat_at: str | None) -> int | None:
    """
    Compute seconds elapsed since the runner's last heartbeat.

    Used by the runner-state probe to decide whether the runner
    is wedged. Returns ``None`` for missing or unparseable
    timestamps so the probe can short-circuit on "no signal"
    rather than treating a parse failure as zero seconds.
    """
    if not heartbeat_at:
        return None
    try:
        timestamp = datetime.fromisoformat(heartbeat_at)
    except ValueError:
        return None
    return max(0, int((datetime.now(UTC) - timestamp).total_seconds()))


def _validation_error_label(exc: Exception) -> str:
    """
    Format the first validation error as a one-line ``path: msg`` label.

    Keeps config/runner status messages scannable instead of
    dumping pydantic's full multi-line traceback into the
    operator's terminal. Falls back to ``str(exc)`` for non-
    pydantic exceptions so the helper can be applied uniformly.
    """
    if isinstance(exc, ValidationError):
        if exc.errors():
            error = exc.errors()[0]
        else:
            error = {}
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = error.get("msg") or "validation error"
        if location:
            return f"{location}: {message}"
        return str(message)
    return str(exc).strip() or type(exc).__name__


def _yaml_location_label(exc: yaml.YAMLError | None) -> str:
    """
    Render the line number from a YAMLError as a short label.

    Returns ``"line unknown"`` when the exception lacks a
    ``problem_mark`` so corrupt-YAML status messages always
    carry a location field even when PyYAML cannot give a
    precise line. Lets diagnostics include "line N" inline
    without conditional formatting at every call site.
    """
    if exc is None:
        return "line unknown"
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return "line unknown"
    return f"line {mark.line + 1}"
