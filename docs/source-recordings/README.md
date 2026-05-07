# Source recordings

Verbatim transcripts of the voice-note review sessions that the
distilled feedback / code-analysis docs are based on. Kept here so
the synthesis is always auditable against the original.

| Date       | File                                  | Topics                                                                        |
|------------|---------------------------------------|-------------------------------------------------------------------------------|
| 2026-05-03 | `2026-05-03-part-1.whisperx.txt`      | `main.py`, `worktree.py`, `fs_cleanup.py`, `attention.py`, `agents/artifacts.py` |
| 2026-05-03 | `2026-05-03-part-2.whisperx.txt`      | `agents/parsing.py`, `agents/prompts.py`                                       |
| 2026-05-06 | `google_recorder_litehive_3.*`        | Docstrings, artifact services, execution trace, `AgentManager`, role defaults  |
| 2026-05-06 | `google_recorder_litehive_4.*`        | Parsing/domain model, sandbox support, subagent sessions and artifacts         |
| 2026-05-06 | `google_recorder_litehive_5.*`        | Workspace paths, registry, runtime settings, workspace loading/creation        |
| 2026-05-06 | `google_recorder_litehive_6.*`        | Agent/common domain types, outcome kind/reason/verdict relationships           |

Format: older `2026-05-03-*` files are WhisperX transcripts with
`[MM:SS]` timestamps. `google_recorder_litehive_3.*` through
`google_recorder_litehive_6.*` include the original `.m4a`,
Recorder-provided `.transcript.txt`, raw `.transcription.jsonpb`,
`.words.json` timings, and OpenAI Whisper transcripts in
`.openai-whisper-1.txt` / `.openai-whisper-1.json`. Russian speech,
occasionally code-mixed with English identifiers
(transcribed phonetically: e.g. `Workspace Content` →
`воркспейс контент`, `PipelineState` → `байплейнстейт`,
`fast_status` → `фастстатус`). Treat phonetic transcriptions as
hints, not literal identifiers — the surrounding context names
the actual file/symbol.

The Recorder-provided transcripts for the 2026-05-06 batch are noisy
and sometimes mis-detect technical terms as unrelated English or
Spanish words. Use the `.openai-whisper-1.txt` files as the working
transcripts for this batch, and verify unclear items against the
audio before changing behavior.

Synthesis lives in:

- `docs/feedback-2026-05-03.md` — the structured rules (R1–R12,
  per-file findings, process notes).
- `docs/code-analysis-2026-05-03.md` — cross-cutting pattern
  audit and the sequencing plan.
- `docs/action-steps-2026-05-06.md` — concrete follow-up tasks from
  the `google_recorder_litehive_3.*` through
  `google_recorder_litehive_6.*` recordings.
- `docs/voice-instructions-2026-05-06.md` — detailed verification
  checklist extracted from the same recordings, intended to be worked
  through item by item.

If a rule in the synthesis seems to under-capture what was said,
grep these transcripts for the timestamp and quote the original
verbatim into the rule. Loss of texture is a bug.
