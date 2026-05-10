"""Typed report-local failure diagnostics."""

from typing import TypeAlias

from pydantic import Field, RootModel


FailureDiagnosticValue: TypeAlias = str | int | bool | None | list[str]


class FailureDiagnostics(RootModel[dict[str, FailureDiagnosticValue]]):
    """
    Typed report-local failure evidence.

    The persisted shape remains a JSON object so existing reports,
    runtime state, and status renderers keep working, but callers pass
    a named domain value instead of anonymous dictionaries.
    """

    root: dict[str, FailureDiagnosticValue] = Field(default_factory=dict)

    def __bool__(self) -> bool:
        """
        Truthy when at least one diagnostic entry is present.

        Lets callers guard rendering with ``if diagnostics:`` instead of
        checking ``len(diagnostics.root)`` or ``as_dict()``.
        """
        return bool(self.root)

    def __getitem__(self, key: str) -> FailureDiagnosticValue:
        """
        Retrieve a single diagnostic value by key.

        Delegates to the underlying dict so callers can use subscript
        syntax (``diagnostics["exit_code"]``) instead of unpacking via
        ``as_dict()`` first.
        """
        return self.root[key]

    def get(self, key: str, default: FailureDiagnosticValue = None) -> FailureDiagnosticValue:
        """
        Retrieve a diagnostic value with a fallback.

        Mirrors ``dict.get`` semantics so callers that probe for optional
        keys (e.g. ``"reason_code"``) get ``None`` instead of
        ``KeyError`` when the key was never recorded.
        """
        return self.root.get(key, default)

    def as_dict(self) -> dict[str, FailureDiagnosticValue]:
        """
        Return a mutable dictionary copy for legacy boundaries.
        """
        return dict(self.root)


def empty_failure_diagnostics() -> FailureDiagnostics:
    """
    Return an empty diagnostics value for pydantic default factories.
    """
    return FailureDiagnostics({})
