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
        return bool(self.root)

    def __getitem__(self, key: str) -> FailureDiagnosticValue:
        return self.root[key]

    def get(self, key: str, default: FailureDiagnosticValue = None) -> FailureDiagnosticValue:
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
