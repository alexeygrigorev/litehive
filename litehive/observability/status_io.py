"""File/parse helpers used by the `litehive status` loaders to degrade gracefully on corrupt YAML/JSON."""

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
    """Read a YAML file expected to be a top-level mapping; on parse error or wrong shape return a status
    issue instead of raising, so the config loader can keep merging the layers it could read."""
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
    """Read a YAML file and turn YAMLError/OSError into a status issue with location info, so corrupt config
    surfaces as one diagnostic line instead of crashing `status`/`health`."""
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
    """Read a JSON file expected to be an object; on missing/blank file return empty, on parse/shape errors
    return a status issue so the runner-state loader can degrade gracefully."""
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
    """Convert the runner's last-heartbeat ISO timestamp into seconds-since-now, used by the runner-state probe
    to decide WEDGED."""
    if not heartbeat_at:
        return None
    try:
        timestamp = datetime.fromisoformat(heartbeat_at)
    except ValueError:
        return None
    return max(0, int((datetime.now(UTC) - timestamp).total_seconds()))


def _validation_error_label(exc: Exception) -> str:
    """Format the first pydantic ValidationError (or any other exception) as a one-line `path: msg` label so
    config/runner status messages stay scannable instead of dumping pydantic's full multi-line traceback."""
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
    """Render the line number from a YAMLError (or `line unknown`) so corrupt-YAML status messages point
    at the offending line."""
    if exc is None:
        return "line unknown"
    mark = getattr(exc, "problem_mark", None)
    if mark is None:
        return "line unknown"
    return f"line {mark.line + 1}"
