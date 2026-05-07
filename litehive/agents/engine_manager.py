"""
Engine adapter lookup for subagent execution.

``SubagentManager`` receives this collaborator instead of reaching
directly into heru's global registry. The default implementation still
delegates to heru at the process boundary, but tests and alternate
containers can inject a focused replacement.
"""

from typing import Any

from heru import get_engine, resume_safe_model_override


class EngineManager:
    """
    Resolve heru engine adapters and model overrides for subagent runs.
    """

    def engine_for(self, engine_name: str) -> Any:
        """
        Return the adapter registered for ``engine_name``.
        """
        return get_engine(engine_name)

    def resume_safe_model(
        self,
        engine_name: str,
        model: str | None,
        resume_session_id: str | None,
    ) -> str | None:
        """
        Return the model value safe to pass to a resumed engine run.
        """
        return resume_safe_model_override(
            engine_name,
            model,
            resume_session_id=resume_session_id,
        )
