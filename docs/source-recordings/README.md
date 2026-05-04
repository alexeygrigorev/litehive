# Source recordings

Verbatim transcripts of the voice-note review sessions that the
distilled feedback / code-analysis docs are based on. Kept here so
the synthesis is always auditable against the original.

| Date       | File                                  | Topics                                                                        |
|------------|---------------------------------------|-------------------------------------------------------------------------------|
| 2026-05-03 | `2026-05-03-part-1.whisperx.txt`      | `main.py`, `worktree.py`, `fs_cleanup.py`, `attention.py`, `agents/artifacts.py` |
| 2026-05-03 | `2026-05-03-part-2.whisperx.txt`      | `agents/parsing.py`, `agents/prompts.py`                                       |

Format: WhisperX transcript with `[MM:SS]` timestamps. Russian
speech, occasionally code-mixed with English identifiers
(transcribed phonetically: e.g. `Workspace Content` →
`воркспейс контент`, `PipelineState` → `байплейнстейт`,
`fast_status` → `фастстатус`). Treat phonetic transcriptions as
hints, not literal identifiers — the surrounding context names
the actual file/symbol.

Synthesis lives in:

- `docs/feedback-2026-05-03.md` — the structured rules (R1–R12,
  per-file findings, process notes).
- `docs/code-analysis-2026-05-03.md` — cross-cutting pattern
  audit and the sequencing plan.

If a rule in the synthesis seems to under-capture what was said,
grep these transcripts for the timestamp and quote the original
verbatim into the rule. Loss of texture is a bug.
