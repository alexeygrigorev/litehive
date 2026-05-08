# Function And Method Ownership Inventory

Generated 2026-05-08 from production Python files under `litehive/`, excluding tests and package `__init__.py` files. This version avoids tables; inventories are grouped by module and written as bullet records.

## Summary

- Functions and methods inventoried: 1675
- Classes inventoried: 301
- Existing methods: 556
- Domain-doc cross-check classes: 102
- Domain-doc mentioned by class/boundary name: 27
- Domain-doc module-only coverage: 68
- Domain-doc missing/unclear coverage: 7
- boundary utility: 57
- domain function: 21
- domain/service candidate: 550
- domain/service method: 487
- utility: 491
- utility/protocol method: 69

## Classification Rules

- `utility`: pure/local helper, parser, formatter, path helper, or private helper that should stay free unless it grows state.
- `boundary utility`: CLI/process/container assembly code. It may stay a free function when it only converts external input and dispatches.
- `domain function`: top-level behavior in `litehive/domain`; prefer moving it onto a value object when one object owns the rule.
- `domain/service candidate`: top-level behavior that carries workspace identity, raw root paths, persistence, rendering, selection, or mutation. These are method/service candidates.
- `domain/service method`: already a method on an object. Review whether the owning class is still coherent before moving it again.

## Function Inventory

### `litehive/agents/artifacts.py`
- `ArtifactService.__init__` at `litehive/agents/artifacts.py:37`
  Classification: utility/protocol method.
  Args: `self, base`.
  Candidate owner: `ArtifactService`.
  Note: Already a method; usually special/protocol behavior.
- `ArtifactService.write_stream` at `litehive/agents/artifacts.py:40`
  Classification: domain/service method.
  Args: `self, name, content, compress`.
  Candidate owner: `ArtifactService`.
  Note: Already on an object; review class responsibility before moving.
- `ArtifactService.write_text` at `litehive/agents/artifacts.py:70`
  Classification: domain/service method.
  Args: `self, name, suffix, content, compress`.
  Candidate owner: `ArtifactService`.
  Note: Already on an object; review class responsibility before moving.
- `ArtifactService.remove_text` at `litehive/agents/artifacts.py:92`
  Classification: domain/service method.
  Args: `self, name, suffix`.
  Candidate owner: `ArtifactService`.
  Note: Already on an object; review class responsibility before moving.
- `write_stream_artifact` at `litehive/agents/artifacts.py:105`
  Classification: domain/service candidate.
  Args: `base, name, content, compress`.
  Candidate owner: `ArtifactService`.
  Note: Verb-shaped business operation; method/service candidate.
- `write_text_if_changed` at `litehive/agents/artifacts.py:116`
  Classification: domain/service candidate.
  Args: `path, content`.
  Candidate owner: `ArtifactService`.
  Note: Verb-shaped business operation; method/service candidate.
- `write_text_artifact` at `litehive/agents/artifacts.py:131`
  Classification: domain/service candidate.
  Args: `base, name, suffix, content, compress`.
  Candidate owner: `ArtifactService`.
  Note: Verb-shaped business operation; method/service candidate.
- `remove_text_artifact` at `litehive/agents/artifacts.py:148`
  Classification: domain/service candidate.
  Args: `base, name, suffix`.
  Candidate owner: `ArtifactService`.
  Note: Public module function in a domain/service package.

### `litehive/agents/callbacks.py`
- `SubagentPidRecorder.record_subagent_pid` at `litehive/agents/callbacks.py:25`
  Classification: domain/service method.
  Args: `self, task, ref, pid`.
  Candidate owner: `SubagentPidRecorder`.
  Note: Already on an object; review class responsibility before moving.
- `ProgressSnapshotWriter.write_session_progress` at `litehive/agents/callbacks.py:37`
  Classification: domain/service method.
  Args: `self, task, base, ref, prompt, execution`.
  Candidate owner: `ProgressSnapshotWriter`.
  Note: Already on an object; review class responsibility before moving.
- `CallbackWarnings.record_failure` at `litehive/agents/callbacks.py:61`
  Classification: domain/service method.
  Args: `self, ref, phase, exc`.
  Candidate owner: `CallbackWarnings`.
  Note: Already on an object; review class responsibility before moving.
- `CallbackWarnings.merged_with` at `litehive/agents/callbacks.py:78`
  Classification: domain/service method.
  Args: `self, base`.
  Candidate owner: `CallbackWarnings`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentRunCallbacks.on_started` at `litehive/agents/callbacks.py:114`
  Classification: domain/service method.
  Args: `self, pid`.
  Candidate owner: `SubagentRunCallbacks`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentRunCallbacks.on_update` at `litehive/agents/callbacks.py:124`
  Classification: domain/service method.
  Args: `self, execution`.
  Candidate owner: `SubagentRunCallbacks`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/agents/command_policy.py`
- `agent_command_is_allowed` at `litehive/agents/command_policy.py:30`
  Classification: domain/service candidate.
  Args: `role, argv`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.

### `litehive/agents/engine_callables.py`
- `resolve_cli_execution_callable` at `litehive/agents/engine_callables.py:10`
  Classification: domain/service candidate.
  Args: `engine, name`.
  Candidate owner: `resolver service for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.

### `litehive/agents/engine_manager.py`
- `EngineManager.engine_for` at `litehive/agents/engine_manager.py:20`
  Classification: domain/service method.
  Args: `self, engine_name`.
  Candidate owner: `EngineManager`.
  Note: Already on an object; review class responsibility before moving.
- `EngineManager.resume_safe_model` at `litehive/agents/engine_manager.py:26`
  Classification: domain/service method.
  Args: `self, engine_name, model, resume_session_id`.
  Candidate owner: `EngineManager`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/agents/execution_trace.py`
- `ParsedUnifiedEvents.__bool__` at `litehive/agents/execution_trace.py:45`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `ParsedUnifiedEvents`.
  Note: Already a method; usually special/protocol behavior.
- `parse_unified_events` at `litehive/agents/execution_trace.py:49`
  Classification: utility.
  Args: `stdout`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `render_event_for_execution_trace` at `litehive/agents/execution_trace.py:82`
  Classification: domain/service candidate.
  Args: `event`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `render_execution_trace_from_events` at `litehive/agents/execution_trace.py:114`
  Classification: domain/service candidate.
  Args: `events, stderr`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `render_execution_trace` at `litehive/agents/execution_trace.py:132`
  Classification: domain/service candidate.
  Args: `execution`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `recovered_timeline_from_events` at `litehive/agents/execution_trace.py:146`
  Classification: domain/service candidate.
  Args: `events, engine_name, task_id, subagent_id`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `_rehydrate_live_events` at `litehive/agents/execution_trace.py:168`
  Classification: utility.
  Args: `events`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `render_execution_trace_from_streams` at `litehive/agents/execution_trace.py:184`
  Classification: domain/service candidate.
  Args: `stdout, stderr`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `render_execution_trace_from_event_stream_payload` at `litehive/agents/execution_trace.py:202`
  Classification: domain/service candidate.
  Args: `payload, stderr`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `load_subagent_execution_trace` at `litehive/agents/execution_trace.py:225`
  Classification: domain/service candidate.
  Args: `workspace, task, ref, active, runtime_state`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_read_stream_artifact` at `litehive/agents/execution_trace.py:293`
  Classification: utility.
  Args: `base, stream, active`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/agents/manager.py`
- `_latest_report_files_changed` at `litehive/agents/manager.py:103`
  Classification: domain/service candidate.
  Args: `workspace, task, pipeline_state, source_subagent_id`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_check_engine_availability_with_retry` at `litehive/agents/manager.py:131`
  Classification: utility.
  Args: `engine, max_retries, delay`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `SubagentStartupError.__init__` at `litehive/agents/manager.py:182`
  Classification: utility/protocol method.
  Args: `self, exc`.
  Candidate owner: `SubagentStartupError`.
  Note: Already a method; usually special/protocol behavior.
- `SubagentManager.__init__` at `litehive/agents/manager.py:201`
  Classification: utility/protocol method.
  Args: `self, execution_root, workspace, config, sandbox, sessions, engines, subagent_ids`.
  Candidate owner: `SubagentManager`.
  Note: Already a method; usually special/protocol behavior.
- `SubagentManager.run` at `litehive/agents/manager.py:229`
  Classification: domain/service method.
  Args: `self, task, role, engine_name, prompt, model, max_turns, resume_session_id`.
  Candidate owner: `SubagentManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentManager._prepare_subagent_run` at `litehive/agents/manager.py:272`
  Classification: domain/service method.
  Args: `self, task, role, engine_name, prompt`.
  Candidate owner: `SubagentManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentManager._execute_subagent_engine` at `litehive/agents/manager.py:320`
  Classification: domain/service method.
  Args: `self, task, context, engine_name, role, prompt, model, max_turns, resume_session_id`.
  Candidate owner: `SubagentManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentManager._run_engine_process` at `litehive/agents/manager.py:387`
  Classification: domain/service method.
  Args: `self, task, context, engine_name, role, prompt, model, max_turns, resume_session_id`.
  Candidate owner: `SubagentManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentManager._run_live_engine_process` at `litehive/agents/manager.py:452`
  Classification: domain/service method.
  Args: `self, context, run_adapter, engine_name, prompt, task_env, effective_model, max_turns, resume_session_id`.
  Candidate owner: `SubagentManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentManager._run_single_engine_process` at `litehive/agents/manager.py:489`
  Classification: domain/service method.
  Args: `self, context, run_adapter, prompt, task_env, effective_model, max_turns, resume_session_id`.
  Candidate owner: `SubagentManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentManager._classify_completed_execution` at `litehive/agents/manager.py:521`
  Classification: domain/service method.
  Args: `self, ref, proc, transcript`.
  Candidate owner: `SubagentManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentManager._finalize_subagent_run` at `litehive/agents/manager.py:559`
  Classification: domain/service method.
  Args: `self, task, context, prompt, engine_name, outcome`.
  Candidate owner: `SubagentManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentManager._write_session_finish` at `litehive/agents/manager.py:630`
  Classification: domain/service method.
  Args: `self, task, base, ref, prompt, transcript, exit_code, execution, interruption_reason, continuation, callback_warnings`.
  Candidate owner: `SubagentManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentManager.write_session_progress` at `litehive/agents/manager.py:741`
  Classification: domain/service method.
  Args: `self, task, base, ref, prompt, execution`.
  Candidate owner: `SubagentManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentManager._parse_execution_report` at `litehive/agents/manager.py:857`
  Classification: domain/service method.
  Args: `self, task, stage, ref, execution, transcript`.
  Candidate owner: `SubagentManager`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/agents/merge_resolver.py`
- `run_worktree_merge_agent` at `litehive/agents/merge_resolver.py:32`
  Classification: domain/service candidate.
  Args: `workspace, worktree_path, task, main_head, config`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/agents/report_extraction.py`
- `MissingVerdictError.__init__` at `litehive/agents/report_extraction.py:38`
  Classification: utility/protocol method.
  Args: `self, pipeline_state, subagent_id`.
  Candidate owner: `MissingVerdictError`.
  Note: Already a method; usually special/protocol behavior.
- `stage_report_from_subagent` at `litehive/agents/report_extraction.py:46`
  Classification: domain/service candidate.
  Args: `task, stage, result, workspace`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `_stage_report_summary` at `litehive/agents/report_extraction.py:92`
  Classification: utility.
  Args: `latest, pipeline_state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_failure_diagnostics_for_activity` at `litehive/agents/report_extraction.py:101`
  Classification: utility.
  Args: `latest, failure_classification`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/agents/report_submission.py`
- `AgentReportSubmissionError.__str__` at `litehive/agents/report_submission.py:19`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `AgentReportSubmissionError`.
  Note: Already a method; usually special/protocol behavior.
- `AgentReportSubmitter.submit` at `litehive/agents/report_submission.py:60`
  Classification: domain/service method.
  Args: `self, request`.
  Candidate owner: `AgentReportSubmitter`.
  Note: Already on an object; review class responsibility before moving.
- `AgentReportSubmitter._load_task` at `litehive/agents/report_submission.py:94`
  Classification: domain/service method.
  Args: `self, task_id`.
  Candidate owner: `AgentReportSubmitter`.
  Note: Already on an object; review class responsibility before moving.
- `AgentReportSubmitter._resolve_identity` at `litehive/agents/report_submission.py:100`
  Classification: domain/service method.
  Args: `self, task`.
  Candidate owner: `AgentReportSubmitter`.
  Note: Already on an object; review class responsibility before moving.
- `AgentReportSubmitter._check_verdict` at `litehive/agents/report_submission.py:121`
  Classification: domain/service method.
  Args: `self, role, verdict, target_stage`.
  Candidate owner: `AgentReportSubmitter`.
  Note: Already on an object; review class responsibility before moving.
- `AgentReportSubmitter._resolve_follow_up_task` at `litehive/agents/report_submission.py:132`
  Classification: domain/service method.
  Args: `self, follow_up_task_id, task`.
  Candidate owner: `AgentReportSubmitter`.
  Note: Already on an object; review class responsibility before moving.
- `AgentReportSubmitter._load_pipeline_stage` at `litehive/agents/report_submission.py:142`
  Classification: domain/service method.
  Args: `self, task_id`.
  Candidate owner: `AgentReportSubmitter`.
  Note: Already on an object; review class responsibility before moving.
- `AgentReportSubmitter._resolve_stage` at `litehive/agents/report_submission.py:145`
  Classification: domain/service method.
  Args: `self, explicit_stage, task, pipeline_stage`.
  Candidate owner: `AgentReportSubmitter`.
  Note: Already on an object; review class responsibility before moving.
- `_normalized_optional` at `litehive/agents/report_submission.py:158`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/agents/session.py`
- `SubagentSessionManager.session_storage_fields` at `litehive/agents/session.py:61`
  Classification: domain/service method.
  Args: `self, ref, created_at, updated_at`.
  Candidate owner: `SubagentSessionManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentSessionManager.extract_execution_continuation` at `litehive/agents/session.py:83`
  Classification: domain/service method.
  Args: `engine_name, execution`.
  Candidate owner: `SubagentSessionManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentSessionManager.extract_execution_event_stream` at `litehive/agents/session.py:91`
  Classification: domain/service method.
  Args: `engine_name, stdout, task_id, subagent_id`.
  Candidate owner: `SubagentSessionManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentSessionManager.append_stream_delta` at `litehive/agents/session.py:105`
  Classification: domain/service method.
  Args: `self, base, ref, stream, full_content`.
  Candidate owner: `SubagentSessionManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentSessionManager.write_session_start` at `litehive/agents/session.py:117`
  Classification: domain/service method.
  Args: `self, task, base, ref, prompt`.
  Candidate owner: `SubagentSessionManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentSessionManager.write_running_session_metadata` at `litehive/agents/session.py:159`
  Classification: domain/service method.
  Args: `self, task, ref, metadata`.
  Candidate owner: `SubagentSessionManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentSessionManager.record_subagent_pid` at `litehive/agents/session.py:182`
  Classification: domain/service method.
  Args: `self, task, ref, pid`.
  Candidate owner: `SubagentSessionManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentSessionManager.subagent_inactivity_timeout_seconds` at `litehive/agents/session.py:207`
  Classification: domain/service method.
  Args: `self, engine_name`.
  Candidate owner: `SubagentSessionManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentSessionManager.completed_inactivity_timeout` at `litehive/agents/session.py:219`
  Classification: domain/service method.
  Args: `self, execution`.
  Candidate owner: `SubagentSessionManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentSessionManager.check_stdout_inactivity` at `litehive/agents/session.py:235`
  Classification: domain/service method.
  Args: `self, base, engine_name, execution`.
  Candidate owner: `SubagentSessionManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentSessionManager.terminate_stale_pid` at `litehive/agents/session.py:252`
  Classification: domain/service method.
  Args: `self, pid`.
  Candidate owner: `SubagentSessionManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentSessionManager.write_event_stream` at `litehive/agents/session.py:262`
  Classification: domain/service method.
  Args: `self, ref, task, stdout`.
  Candidate owner: `SubagentSessionManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentSessionManager.write_session_snapshot` at `litehive/agents/session.py:281`
  Classification: domain/service method.
  Args: `self, task, base, ref, snapshot`.
  Candidate owner: `SubagentSessionManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentSessionManager.write_snapshot_artifacts` at `litehive/agents/session.py:307`
  Classification: domain/service method.
  Args: `base, ref, snapshot`.
  Candidate owner: `SubagentSessionManager`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentSessionManager.session_row_for_snapshot` at `litehive/agents/session.py:320`
  Classification: domain/service method.
  Args: `self, ref, snapshot, created_at`.
  Candidate owner: `SubagentSessionManager`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/agents/session_continuation.py`
- `SubagentContinuationState.payload` at `litehive/agents/session_continuation.py:14`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SubagentContinuationState`.
  Note: Already on an object; review class responsibility before moving.
- `NoSubagentContinuation.payload` at `litehive/agents/session_continuation.py:26`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `NoSubagentContinuation`.
  Note: Already on an object; review class responsibility before moving.
- `CapturedSubagentContinuation.payload` at `litehive/agents/session_continuation.py:41`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `CapturedSubagentContinuation`.
  Note: Already on an object; review class responsibility before moving.
- `subagent_continuation_state` at `litehive/agents/session_continuation.py:48`
  Classification: domain/service candidate.
  Args: `continuation`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.

### `litehive/agents/session_events.py`
- `SubagentStartedEvent.kind` at `litehive/agents/session_events.py:28`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SubagentStartedEvent`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentStartedEvent.data` at `litehive/agents/session_events.py:32`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SubagentStartedEvent`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentPidEvent.kind` at `litehive/agents/session_events.py:59`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SubagentPidEvent`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentPidEvent.data` at `litehive/agents/session_events.py:63`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SubagentPidEvent`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentProgressEvent.kind` at `litehive/agents/session_events.py:85`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SubagentProgressEvent`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentProgressEvent.data` at `litehive/agents/session_events.py:89`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SubagentProgressEvent`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentFinishedEvent.kind` at `litehive/agents/session_events.py:115`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SubagentFinishedEvent`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentFinishedEvent.data` at `litehive/agents/session_events.py:119`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SubagentFinishedEvent`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/agents/session_inactivity.py`
- `SubagentInactivityTimeoutPolicy.live_timeout_seconds` at `litehive/agents/session_inactivity.py:38`
  Classification: domain/service method.
  Args: `self, engine_name`.
  Candidate owner: `SubagentInactivityTimeoutPolicy`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentInactivityTimeoutPolicy.completed_timeout` at `litehive/agents/session_inactivity.py:46`
  Classification: domain/service method.
  Args: `self, execution`.
  Candidate owner: `SubagentInactivityTimeoutPolicy`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentInactivityMonitor.live_timeout_seconds` at `litehive/agents/session_inactivity.py:69`
  Classification: domain/service method.
  Args: `self, engine_name`.
  Candidate owner: `SubagentInactivityMonitor`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentInactivityMonitor.completed_timeout` at `litehive/agents/session_inactivity.py:75`
  Classification: domain/service method.
  Args: `self, execution`.
  Candidate owner: `SubagentInactivityMonitor`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentInactivityMonitor.check_stdout_inactivity` at `litehive/agents/session_inactivity.py:81`
  Classification: domain/service method.
  Args: `self, base, engine_name, execution`.
  Candidate owner: `SubagentInactivityMonitor`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentInactivityMonitor.terminate_stale_pid` at `litehive/agents/session_inactivity.py:106`
  Classification: domain/service method.
  Args: `self, pid`.
  Candidate owner: `SubagentInactivityMonitor`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/agents/session_reports.py`
- `SubagentReportPayload.as_dict` at `litehive/agents/session_reports.py:32`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SubagentReportPayload`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentReportPayload.continuation_payload` at `litehive/agents/session_reports.py:47`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SubagentReportPayload`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/agents/session_snapshots.py`
- `SubagentSessionMetadata.continuation_payload` at `litehive/agents/session_snapshots.py:25`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SubagentSessionMetadata`.
  Note: Already on an object; review class responsibility before moving.
- `RunningSubagentSessionMetadata.continuation_payload` at `litehive/agents/session_snapshots.py:47`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `RunningSubagentSessionMetadata`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentSessionStorageFields.as_dict` at `litehive/agents/session_snapshots.py:70`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SubagentSessionStorageFields`.
  Note: Already on an object; review class responsibility before moving.
- `RunningSubagentSessionRow.as_dict` at `litehive/agents/session_snapshots.py:101`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `RunningSubagentSessionRow`.
  Note: Already on an object; review class responsibility before moving.
- `TerminalSubagentSessionRow.as_dict` at `litehive/agents/session_snapshots.py:129`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `TerminalSubagentSessionRow`.
  Note: Already on an object; review class responsibility before moving.
- `InterruptedSubagentSessionRow.as_dict` at `litehive/agents/session_snapshots.py:161`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `InterruptedSubagentSessionRow`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/agents/session_store.py`
- `LoadedSubagentSession.from_payload` at `litehive/agents/session_store.py:40`
  Classification: domain/service method.
  Args: `cls, payload, persisted_created_at`.
  Candidate owner: `LoadedSubagentSession`.
  Note: Already on an object; review class responsibility before moving.
- `LoadedSubagentSession.__bool__` at `litehive/agents/session_store.py:53`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `LoadedSubagentSession`.
  Note: Already a method; usually special/protocol behavior.
- `LoadedSubagentSession.subagent_id` at `litehive/agents/session_store.py:57`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `LoadedSubagentSession`.
  Note: Already on an object; review class responsibility before moving.
- `LoadedSubagentSession.role` at `litehive/agents/session_store.py:64`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `LoadedSubagentSession`.
  Note: Already on an object; review class responsibility before moving.
- `LoadedSubagentSession.updated_at` at `litehive/agents/session_store.py:68`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `LoadedSubagentSession`.
  Note: Already on an object; review class responsibility before moving.
- `LoadedSubagentSession.exit_code` at `litehive/agents/session_store.py:72`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `LoadedSubagentSession`.
  Note: Already on an object; review class responsibility before moving.
- `LoadedSubagentSession._non_empty_string` at `litehive/agents/session_store.py:78`
  Classification: domain/service method.
  Args: `self, key`.
  Candidate owner: `LoadedSubagentSession`.
  Note: Already on an object; review class responsibility before moving.
- `SerializableSubagentSession.as_dict` at `litehive/agents/session_store.py:94`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SerializableSubagentSession`.
  Note: Already on an object; review class responsibility before moving.
- `SerializableSubagentReport.as_dict` at `litehive/agents/session_store.py:103`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SerializableSubagentReport`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentArtifactPayload.as_dict` at `litehive/agents/session_store.py:114`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SubagentArtifactPayload`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentEventStreamPayload.as_dict` at `litehive/agents/session_store.py:126`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SubagentEventStreamPayload`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentArtifactStore.load_all` at `litehive/agents/session_store.py:144`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SubagentArtifactStore`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentArtifactStore.load_session_record` at `litehive/agents/session_store.py:148`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SubagentArtifactStore`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentArtifactStore.load_session` at `litehive/agents/session_store.py:152`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SubagentArtifactStore`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentArtifactStore.load_report` at `litehive/agents/session_store.py:155`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SubagentArtifactStore`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentArtifactStore.load_event_stream` at `litehive/agents/session_store.py:158`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SubagentArtifactStore`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentArtifactStore.save` at `litehive/agents/session_store.py:161`
  Classification: domain/service method.
  Args: `self, session, report, event_stream, clear_event_stream`.
  Candidate owner: `SubagentArtifactStore`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentArtifactStore._load_slice` at `litehive/agents/session_store.py:206`
  Classification: domain/service method.
  Args: `self, artifact_slice`.
  Candidate owner: `SubagentArtifactStore`.
  Note: Already on an object; review class responsibility before moving.
- `subagent_artifacts` at `litehive/agents/session_store.py:214`
  Classification: domain/service candidate.
  Args: `workspace, task_id, subagent_id`.
  Candidate owner: `SubagentSessionStore / bound subagent artifact store`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_load_subagent_payload` at `litehive/agents/session_store.py:221`
  Classification: domain/service candidate.
  Args: `workspace, task_id, subagent_id`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `load_subagent_artifacts` at `litehive/agents/session_store.py:249`
  Classification: domain/service candidate.
  Args: `workspace, task_id, subagent_id`.
  Candidate owner: `SubagentSessionStore / bound subagent artifact store`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_load_subagent_artifact_slice` at `litehive/agents/session_store.py:254`
  Classification: domain/service candidate.
  Args: `workspace, task_id, subagent_id, artifact_slice`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `load_subagent_session_record` at `litehive/agents/session_store.py:269`
  Classification: domain/service candidate.
  Args: `workspace, task_id, subagent_id`.
  Candidate owner: `SubagentSessionStore / bound subagent artifact store`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `load_subagent_session` at `litehive/agents/session_store.py:276`
  Classification: domain/service candidate.
  Args: `workspace, task_id, subagent_id`.
  Candidate owner: `SubagentSessionStore / bound subagent artifact store`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `load_subagent_report` at `litehive/agents/session_store.py:281`
  Classification: domain/service candidate.
  Args: `workspace, task_id, subagent_id`.
  Candidate owner: `SubagentSessionStore / bound subagent artifact store`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `load_subagent_event_stream` at `litehive/agents/session_store.py:286`
  Classification: domain/service candidate.
  Args: `workspace, task_id, subagent_id`.
  Candidate owner: `SubagentSessionStore / bound subagent artifact store`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/agents/session_streams.py`
- `SubagentStreamLog.ensure` at `litehive/agents/session_streams.py:22`
  Classification: domain/service method.
  Args: `self, base`.
  Candidate owner: `SubagentStreamLog`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentStreamLog.append_delta` at `litehive/agents/session_streams.py:29`
  Classification: domain/service method.
  Args: `self, base, ref, stream, full_content`.
  Candidate owner: `SubagentStreamLog`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/agents/subagent_ids.py`
- `SubagentIdRepository.__init__` at `litehive/agents/subagent_ids.py:25`
  Classification: utility/protocol method.
  Args: `self, workspace`.
  Candidate owner: `SubagentIdRepository`.
  Note: Already a method; usually special/protocol behavior.
- `SubagentIdRepository.reserve_next_id` at `litehive/agents/subagent_ids.py:29`
  Classification: domain/service method.
  Args: `self, task`.
  Candidate owner: `SubagentIdRepository`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentIdRepository._counter_next_number` at `litehive/agents/subagent_ids.py:44`
  Classification: domain/service method.
  Args: `self, connection, task_id`.
  Candidate owner: `SubagentIdRepository`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentIdRepository._session_next_number` at `litehive/agents/subagent_ids.py:60`
  Classification: domain/service method.
  Args: `self, connection, task_id`.
  Candidate owner: `SubagentIdRepository`.
  Note: Already on an object; review class responsibility before moving.
- `SubagentIdRepository._save_counter_next_number` at `litehive/agents/subagent_ids.py:77`
  Classification: domain/service method.
  Args: `self, connection, task_id, next_number`.
  Candidate owner: `SubagentIdRepository`.
  Note: Already on an object; review class responsibility before moving.
- `_next_number_after_task_refs` at `litehive/agents/subagent_ids.py:98`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_next_number_after_id` at `litehive/agents/subagent_ids.py:108`
  Classification: utility.
  Args: `subagent_id`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/agents/task_mutation.py`
- `AgentTaskMutationError.__str__` at `litehive/agents/task_mutation.py:19`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `AgentTaskMutationError`.
  Note: Already a method; usually special/protocol behavior.
- `AgentTaskUpdateRequest.has_changes` at `litehive/agents/task_mutation.py:38`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `AgentTaskUpdateRequest`.
  Note: Already on an object; review class responsibility before moving.
- `AgentTaskMutationAuthorizer.authorize` at `litehive/agents/task_mutation.py:61`
  Classification: domain/service method.
  Args: `self, requested_task_id, allowed_roles`.
  Candidate owner: `AgentTaskMutationAuthorizer`.
  Note: Already on an object; review class responsibility before moving.
- `AgentTaskMutationAuthorizer._authorized_role` at `litehive/agents/task_mutation.py:82`
  Classification: domain/service method.
  Args: `self, allowed_roles`.
  Candidate owner: `AgentTaskMutationAuthorizer`.
  Note: Already on an object; review class responsibility before moving.
- `AgentTaskMutationAuthorizer._resolve_workspace` at `litehive/agents/task_mutation.py:87`
  Classification: domain/service method.
  Args: `self, task_id`.
  Candidate owner: `AgentTaskMutationAuthorizer`.
  Note: Already on an object; review class responsibility before moving.
- `AgentTaskMutator.update` at `litehive/agents/task_mutation.py:112`
  Classification: domain/service method.
  Args: `self, request`.
  Candidate owner: `AgentTaskMutator`.
  Note: Already on an object; review class responsibility before moving.
- `AgentTaskMutator.close` at `litehive/agents/task_mutation.py:149`
  Classification: domain/service method.
  Args: `self, request`.
  Candidate owner: `AgentTaskMutator`.
  Note: Already on an object; review class responsibility before moving.
- `_normalized_optional` at `litehive/agents/task_mutation.py:160`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/attention.py`
- `OperatorNeededState.needed` at `litehive/attention.py:55`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `OperatorNeededState`.
  Note: Already on an object; review class responsibility before moving.
- `AttentionRepository.__init__` at `litehive/attention.py:94`
  Classification: utility/protocol method.
  Args: `self, workspace`.
  Candidate owner: `AttentionRepository`.
  Note: Already a method; usually special/protocol behavior.
- `AttentionRepository.append` at `litehive/attention.py:97`
  Classification: domain/service method.
  Args: `self, message`.
  Candidate owner: `AttentionRepository`.
  Note: Already on an object; review class responsibility before moving.
- `read_attention_log` at `litehive/attention.py:109`
  Classification: domain/service candidate.
  Args: `workspace, limit`.
  Candidate owner: `AttentionRepository`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `collect_operator_needed_state_for_workspace` at `litehive/attention.py:130`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `AttentionRepository`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `waiting_for_you_lines_for_workspace` at `litehive/attention.py:154`
  Classification: domain/service candidate.
  Args: `workspace, limit, reconcile`.
  Candidate owner: `AttentionRepository`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/cli/agent_cli.py`
- `_current_role` at `litehive/cli/agent_cli.py:44`
  Classification: utility.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_current_subagent_id` at `litehive/cli/agent_cli.py:55`
  Classification: utility.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `current_agent_role` at `litehive/cli/agent_cli.py:69`
  Classification: boundary utility.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `_current_stage` at `litehive/cli/agent_cli.py:80`
  Classification: utility.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_current_environment` at `litehive/cli/agent_cli.py:92`
  Classification: utility.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `block_if_agent` at `litehive/cli/agent_cli.py:96`
  Classification: boundary utility.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `_agent_unauthorized_message` at `litehive/cli/agent_cli.py:110`
  Classification: utility.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `agent_report_command` at `litehive/cli/agent_cli.py:127`
  Classification: boundary utility.
  Args: `verdict, message, message_file, stage, target_stage, task_id, workspace, files_changed, follow_up_task`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `resolve_active_agent_task_mutation_target` at `litehive/cli/agent_cli.py:222`
  Classification: boundary utility.
  Args: `requested_task_id, allowed_roles`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `_subagent_id_from_environment` at `litehive/cli/agent_cli.py:254`
  Classification: utility.
  Args: `environment`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_agent_task_mutator` at `litehive/cli/agent_cli.py:260`
  Classification: utility.
  Args: `target`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `agent_update_command` at `litehive/cli/agent_cli.py:268`
  Classification: boundary utility.
  Args: `task_id, goal, acceptance_criteria, plan, constraints, priority`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `agent_close_command` at `litehive/cli/agent_cli.py:303`
  Classification: boundary utility.
  Args: `task_id, outcome, reason`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.

### `litehive/cli/agent_dispatch.py`
- `agent_blocked_command_message` at `litehive/cli/agent_dispatch.py:26`
  Classification: boundary utility.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `requests_help` at `litehive/cli/agent_dispatch.py:43`
  Classification: boundary utility.
  Args: `argv`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `dispatch_agent_role` at `litehive/cli/agent_dispatch.py:60`
  Classification: boundary utility.
  Args: `role, argv`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.

### `litehive/cli/app.py`
- `_run_next_task` at `litehive/cli/app.py:25`
  Classification: utility.
  Args: `container`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `root` at `litehive/cli/app.py:43`
  Classification: boundary utility.
  Args: `ctx`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `main` at `litehive/cli/app.py:76`
  Classification: boundary utility.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.

### `litehive/cli/common.py`
- `make_typer` at `litehive/cli/common.py:13`
  Classification: boundary utility.
  Args: `invoke_without_command`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `choice` at `litehive/cli/common.py:32`
  Classification: boundary utility.
  Args: `values`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `require_subcommand` at `litehive/cli/common.py:43`
  Classification: boundary utility.
  Args: `ctx`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.

### `litehive/cli/daemon_cli.py`
- `daemon_group` at `litehive/cli/daemon_cli.py:25`
  Classification: boundary utility.
  Args: `ctx`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `daemon_run` at `litehive/cli/daemon_cli.py:31`
  Classification: domain/service candidate.
  Args: `workspace, foreground`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `daemon_status` at `litehive/cli/daemon_cli.py:45`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `daemon_stop` at `litehive/cli/daemon_cli.py:65`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `daemon_restart` at `litehive/cli/daemon_cli.py:78`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `daemon_worker` at `litehive/cli/daemon_cli.py:90`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/cli/display.py`
- `format_retry_on` at `litehive/cli/display.py:4`
  Classification: utility.
  Args: `config`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `task_engine_label` at `litehive/cli/display.py:17`
  Classification: utility.
  Args: `task_engine, default_engine`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `task_model_label` at `litehive/cli/display.py:29`
  Classification: utility.
  Args: `task_model`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `task_dependencies_label` at `litehive/cli/display.py:40`
  Classification: utility.
  Args: `task_id, dependencies`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `task_interruption_label` at `litehive/cli/display.py:53`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/cli/engine.py`
- `engine_command` at `litehive/cli/engine.py:27`
  Classification: boundary utility.
  Args: `action, workspace, name, until, reason, limit`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `_engine_status_command` at `litehive/cli/engine.py:67`
  Classification: domain/service candidate.
  Args: `workspace, name`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_engine_audit_command` at `litehive/cli/engine.py:77`
  Classification: domain/service candidate.
  Args: `workspace, key, limit`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_engine_default_command` at `litehive/cli/engine.py:83`
  Classification: domain/service candidate.
  Args: `workspace, name, reason`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_engine_preference_command` at `litehive/cli/engine.py:99`
  Classification: domain/service candidate.
  Args: `workspace, name, reason`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_engine_freeze_command` at `litehive/cli/engine.py:124`
  Classification: domain/service candidate.
  Args: `workspace, name, until, reason`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_engine_unfreeze_command` at `litehive/cli/engine.py:145`
  Classification: domain/service candidate.
  Args: `workspace, name, reason`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_engine_reason_context` at `litehive/cli/engine.py:160`
  Classification: utility.
  Args: `reason`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_updated_label` at `litehive/cli/engine.py:166`
  Classification: utility.
  Args: `changed`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_parse_engine_preference` at `litehive/cli/engine.py:172`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_split_engine_preference_string` at `litehive/cli/engine.py:190`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_engine_list_label` at `litehive/cli/engine.py:209`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_render_engine_audit_lines` at `litehive/cli/engine.py:224`
  Classification: domain/service candidate.
  Args: `workspace, key, limit`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_render_engine_status_lines` at `litehive/cli/engine.py:252`
  Classification: utility.
  Args: `config`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_engine_freeze_summary_line` at `litehive/cli/engine.py:301`
  Classification: utility.
  Args: `engine_freeze`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_render_quota_line` at `litehive/cli/engine.py:319`
  Classification: utility.
  Args: `_engine_name, status`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_quota_status_error` at `litehive/cli/engine.py:339`
  Classification: utility.
  Args: `status`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/cli/parse.py`
- `parse_dependency_ids` at `litehive/cli/parse.py:9`
  Classification: utility.
  Args: `raw_values, task_id, allow_clear`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `parse_acceptance_criteria` at `litehive/cli/parse.py:55`
  Classification: utility.
  Args: `raw_values, allow_clear`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `parse_text_list_option` at `litehive/cli/parse.py:81`
  Classification: utility.
  Args: `raw_values, option_name, allow_clear`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/cli/pipeline_cli.py`
- `pipeline_rules_command` at `litehive/cli/pipeline_cli.py:28`
  Classification: boundary utility.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `pipeline_set_state_command` at `litehive/cli/pipeline_cli.py:57`
  Classification: boundary utility.
  Args: `task_id, stage, workspace`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `pipeline_reset_command` at `litehive/cli/pipeline_cli.py:87`
  Classification: boundary utility.
  Args: `task_id, workspace`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `pipeline_journal_command` at `litehive/cli/pipeline_cli.py:109`
  Classification: boundary utility.
  Args: `task_id, workspace, limit`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `_print_pipeline_report_lines` at `litehive/cli/pipeline_cli.py:142`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_print_pipeline_state_lines` at `litehive/cli/pipeline_cli.py:165`
  Classification: utility.
  Args: `state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_print_recovery_trigger_line` at `litehive/cli/pipeline_cli.py:173`
  Classification: utility.
  Args: `state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_print_merge_and_commit_lines` at `litehive/cli/pipeline_cli.py:187`
  Classification: utility.
  Args: `state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_print_recovery_history_lines` at `litehive/cli/pipeline_cli.py:198`
  Classification: utility.
  Args: `state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_print_failed_run_history_lines` at `litehive/cli/pipeline_cli.py:213`
  Classification: utility.
  Args: `state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_print_failure_detail_lines` at `litehive/cli/pipeline_cli.py:229`
  Classification: utility.
  Args: `state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_print_pipeline_lifecycle_lines` at `litehive/cli/pipeline_cli.py:244`
  Classification: utility.
  Args: `lifecycle`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_print_pipeline_transition_lines` at `litehive/cli/pipeline_cli.py:252`
  Classification: utility.
  Args: `transitions, limit`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/cli/pool.py`
- `task_stage_outcomes_for_workspace` at `litehive/cli/pool.py:9`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_pool_task_report_entry_for_workspace` at `litehive/cli/pool.py:20`
  Classification: domain/service candidate.
  Args: `workspace, task_id, title, status, pipeline_status, slug, reason_code, reason, follow_up_task_id, close_reason, flag_reason`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_pending_pool_tasks_for_workspace` at `litehive/cli/pool.py:54`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_resumable_pool_tasks_for_workspace` at `litehive/cli/pool.py:74`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_closed_pool_tasks_for_workspace` at `litehive/cli/pool.py:98`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_format_pool_task_report_line` at `litehive/cli/pool.py:123`
  Classification: utility.
  Args: `label, entry`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_print_pool_summary_report` at `litehive/cli/pool.py:164`
  Classification: utility.
  Args: `report`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_pool_summary_report_data_for_workspace` at `litehive/cli/pool.py:179`
  Classification: domain/service candidate.
  Args: `workspace, completed, flagged, stop_reason, tasks_run`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_pool_summary_report_lines` at `litehive/cli/pool.py:210`
  Classification: utility.
  Args: `report`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_ensure_pool_summary_report_fields` at `litehive/cli/pool.py:281`
  Classification: utility.
  Args: `report`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_write_pool_summary_report` at `litehive/cli/pool.py:298`
  Classification: domain/service candidate.
  Args: `workspace, report`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.

### `litehive/cli/queue_cli.py`
- `queue_group` at `litehive/cli/queue_cli.py:35`
  Classification: boundary utility.
  Args: `ctx, workspace`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `move` at `litehive/cli/queue_cli.py:85`
  Classification: boundary utility.
  Args: `task_id, position, workspace`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `promote` at `litehive/cli/queue_cli.py:111`
  Classification: boundary utility.
  Args: `task_id, workspace`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `requeue` at `litehive/cli/queue_cli.py:147`
  Classification: boundary utility.
  Args: `task_id, workspace, front, force`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `resume` at `litehive/cli/queue_cli.py:186`
  Classification: boundary utility.
  Args: `task_id, workspace, front`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `stop` at `litehive/cli/queue_cli.py:219`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `prioritize` at `litehive/cli/queue_cli.py:251`
  Classification: boundary utility.
  Args: `task_ids, workspace`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `prioritize_command` at `litehive/cli/queue_cli.py:276`
  Classification: boundary utility.
  Args: `task_ids, workspace`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `recover` at `litehive/cli/queue_cli.py:292`
  Classification: boundary utility.
  Args: `task_id, workspace`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `recover_command` at `litehive/cli/queue_cli.py:323`
  Classification: boundary utility.
  Args: `task_id, workspace`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `switch` at `litehive/cli/queue_cli.py:342`
  Classification: boundary utility.
  Args: `task_id, engine, reason, workspace`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.

### `litehive/cli/runner.py`
- `register_root_commands` at `litehive/cli/runner.py:50`
  Classification: boundary utility.
  Args: `app, backup_app, db_app`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `start` at `litehive/cli/runner.py:80`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `daemon_status` at `litehive/cli/runner.py:105`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `stop` at `litehive/cli/runner.py:120`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `restart` at `litehive/cli/runner.py:142`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `daemon_worker` at `litehive/cli/runner.py:170`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `run_task` at `litehive/cli/runner.py:200`
  Classification: boundary utility.
  Args: `container, task, engine_override, model_override`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `run_once` at `litehive/cli/runner.py:222`
  Classification: boundary utility.
  Args: `container, engine, model`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `_existing_consecutive_task_failure_stop` at `litehive/cli/runner.py:295`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_emit_consecutive_task_failure_stop` at `litehive/cli/runner.py:317`
  Classification: utility.
  Args: `consecutive_task_failures`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_run_single` at `litehive/cli/runner.py:331`
  Classification: utility.
  Args: `container, engine, model`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_preview_single` at `litehive/cli/runner.py:354`
  Classification: utility.
  Args: `container, engine, model`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_workspace_has_dirty_non_litehive_changes` at `litehive/cli/runner.py:396`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_run_drain` at `litehive/cli/runner.py:412`
  Classification: utility.
  Args: `container, engine, model, stop_on_failure, max_tasks, stop_on_dirty_git`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `run_command` at `litehive/cli/runner.py:464`
  Classification: domain/service candidate.
  Args: `workspace, dry_run, drain, engine, model, stop_on_failure, max_tasks, stop_on_dirty_git`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `report_command` at `litehive/cli/runner.py:516`
  Classification: boundary utility.
  Args: `verdict, message, message_file, role, stage, task_id, workspace, files_changed`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `backup_group` at `litehive/cli/runner.py:591`
  Classification: boundary utility.
  Args: `ctx`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `backup_create` at `litehive/cli/runner.py:596`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `backup_list` at `litehive/cli/runner.py:619`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `backup_restore` at `litehive/cli/runner.py:637`
  Classification: boundary utility.
  Args: `timestamp, workspace, yes`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `db_group` at `litehive/cli/runner.py:681`
  Classification: boundary utility.
  Args: `ctx`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `db_status` at `litehive/cli/runner.py:688`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `db_migrate` at `litehive/cli/runner.py:708`
  Classification: domain/service candidate.
  Args: `workspace, dry_run`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `db_rebuild_from_events` at `litehive/cli/runner.py:746`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `db_audit` at `litehive/cli/runner.py:777`
  Classification: boundary utility.
  Args: `task_id, workspace, limit, action`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `db_settings` at `litehive/cli/runner.py:811`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `db_settings_audit` at `litehive/cli/runner.py:829`
  Classification: boundary utility.
  Args: `key, workspace, limit`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.

### `litehive/cli/task_cli.py`
- `_display_flag_reason` at `litehive/cli/task_cli.py:43`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_display_close_reason` at `litehive/cli/task_cli.py:57`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_show_dependency_label` at `litehive/cli/task_cli.py:73`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_print_creation_provenance` at `litehive/cli/task_cli.py:98`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `add` at `litehive/cli/task_cli.py:126`
  Classification: boundary utility.
  Args: `title, workspace, goal, acceptance_criteria, depends_on, priority`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `evidence` at `litehive/cli/task_cli.py:189`
  Classification: boundary utility.
  Args: `task_id, workspace`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `debug` at `litehive/cli/task_cli.py:213`
  Classification: boundary utility.
  Args: `task_id, workspace, all_, worktree`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `logs` at `litehive/cli/task_cli.py:243`
  Classification: boundary utility.
  Args: `task_id, workspace, daemon, agent, all_, follow`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `list_tasks_command` at `litehive/cli/task_cli.py:281`
  Classification: domain/service candidate.
  Args: `workspace, show_all`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `show` at `litehive/cli/task_cli.py:315`
  Classification: boundary utility.
  Args: `task_id, workspace`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `abandon` at `litehive/cli/task_cli.py:378`
  Classification: boundary utility.
  Args: `task_id, workspace`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `close` at `litehive/cli/task_cli.py:403`
  Classification: boundary utility.
  Args: `task_id, outcome, workspace, reason, follow_up_task`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `update` at `litehive/cli/task_cli.py:455`
  Classification: boundary utility.
  Args: `task_id, workspace, title, priority, goal, depends_on, acceptance_criteria, constraints, plan`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.

### `litehive/cli/task_debug_support.py`
- `render_task_evidence_for_workspace` at `litehive/cli/task_debug_support.py:24`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `debug_all_for_workspace` at `litehive/cli/task_debug_support.py:40`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `debug_latest_for_workspace` at `litehive/cli/task_debug_support.py:60`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_print_lifecycle_evidence` at `litehive/cli/task_debug_support.py:71`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_print_latest_report` at `litehive/cli/task_debug_support.py:123`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_print_latest_activity` at `litehive/cli/task_debug_support.py:144`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_print_latest_subagent` at `litehive/cli/task_debug_support.py:164`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `debug_worktree_for_workspace` at `litehive/cli/task_debug_support.py:229`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_print_worktree_evidence` at `litehive/cli/task_debug_support.py:238`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_read_exit_code` at `litehive/cli/task_debug_support.py:268`
  Classification: domain/service candidate.
  Args: `workspace, task_id, subagent_id`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_enum_value` at `litehive/cli/task_debug_support.py:281`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_first_line` at `litehive/cli/task_debug_support.py:297`
  Classification: utility.
  Args: `value, limit`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_compact_paths` at `litehive/cli/task_debug_support.py:315`
  Classification: utility.
  Args: `paths, limit`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/cli/task_logs_support.py`
- `show_latest_daemon_log_for_workspace` at `litehive/cli/task_logs_support.py:26`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `list_daemon_sessions_for_workspace` at `litehive/cli/task_logs_support.py:46`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `show_task_journal_for_workspace` at `litehive/cli/task_logs_support.py:74`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `show_latest_subagent_for_workspace` at `litehive/cli/task_logs_support.py:89`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `list_task_subagents_for_workspace` at `litehive/cli/task_logs_support.py:102`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `follow_active_subagent_for_workspace` at `litehive/cli/task_logs_support.py:134`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_latest_daemon_log_path` at `litehive/cli/task_logs_support.py:191`
  Classification: utility.
  Args: `latest_dir`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_tail_text` at `litehive/cli/task_logs_support.py:213`
  Classification: utility.
  Args: `text, lines`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_print_follow_chunk` at `litehive/cli/task_logs_support.py:228`
  Classification: utility.
  Args: `stdout_path, position`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_session_outcome` at `litehive/cli/task_logs_support.py:247`
  Classification: utility.
  Args: `directory`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_format_session_timestamp` at `litehive/cli/task_logs_support.py:275`
  Classification: utility.
  Args: `name`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_latest_subagent_ref` at `litehive/cli/task_logs_support.py:290`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_artifact_for_kind` at `litehive/cli/task_logs_support.py:312`
  Classification: utility.
  Args: `base, kind, active`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_pick_runtime_value` at `litehive/cli/task_logs_support.py:334`
  Classification: utility.
  Args: `runtime_state, *keys`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_format_duration` at `litehive/cli/task_logs_support.py:350`
  Classification: utility.
  Args: `started_at, completed_at`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `resolve_follow_task_for_workspace` at `litehive/cli/task_logs_support.py:375`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `load_task_with_runtime_for_workspace` at `litehive/cli/task_logs_support.py:394`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_coerce_datetime` at `litehive/cli/task_logs_support.py:407`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/cli/workspace.py`
- `register_root_commands` at `litehive/cli/workspace.py:56`
  Classification: boundary utility.
  Args: `app`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `print_status_issues` at `litehive/cli/workspace.py:71`
  Classification: boundary utility.
  Args: `issues`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `repair_summary_lines` at `litehive/cli/workspace.py:88`
  Classification: boundary utility.
  Args: `summary, result_label, include_empty, include_extended_fields`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `status_command` at `litehive/cli/workspace.py:135`
  Classification: domain/service candidate.
  Args: `workspace, full`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `repair_command` at `litehive/cli/workspace.py:202`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `health_command` at `litehive/cli/workspace.py:252`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `health_daemon_status_for_workspace` at `litehive/cli/workspace.py:320`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `collect_quota_health` at `litehive/cli/workspace.py:340`
  Classification: boundary utility.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `_unsupported_quota_health` at `litehive/cli/workspace.py:365`
  Classification: utility.
  Args: `engine`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `quota_health` at `litehive/cli/workspace.py:377`
  Classification: boundary utility.
  Args: `engine, status`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.

### `litehive/cli/worktree_cli.py`
- `worktree_group` at `litehive/cli/worktree_cli.py:16`
  Classification: boundary utility.
  Args: `ctx`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `ls` at `litehive/cli/worktree_cli.py:22`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `clean` at `litehive/cli/worktree_cli.py:55`
  Classification: domain/service candidate.
  Args: `workspace, dry_run`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `rescue` at `litehive/cli/worktree_cli.py:110`
  Classification: domain/service candidate.
  Args: `workspace, apply`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/config/engine_freezes.py`
- `is_engine_frozen` at `litehive/config/engine_freezes.py:17`
  Classification: domain/service candidate.
  Args: `config, engine_name`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `active_engine_freezes` at `litehive/config/engine_freezes.py:31`
  Classification: domain/service candidate.
  Args: `config`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `persist_engine_freeze_iso_for_workspace` at `litehive/config/engine_freezes.py:51`
  Classification: domain/service candidate.
  Args: `workspace, engine_name, freeze_iso, actor, source, reason`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `clear_persisted_engine_freeze_for_workspace` at `litehive/config/engine_freezes.py:82`
  Classification: domain/service candidate.
  Args: `workspace, engine_name, actor, source, reason`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/config/engine_models.py`
- `_candidate_engine_order` at `litehive/config/engine_models.py:93`
  Classification: utility.
  Args: `task, config, request`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_persist_engine_freeze` at `litehive/config/engine_models.py:112`
  Classification: domain/service candidate.
  Args: `workspace, config, engine_name, freeze_until`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_clear_engine_freeze` at `litehive/config/engine_models.py:141`
  Classification: domain/service candidate.
  Args: `workspace, config, engine_name`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `select_engine_for_workspace` at `litehive/config/engine_models.py:156`
  Classification: domain/service candidate.
  Args: `workspace, task, config, request`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `resolve_model` at `litehive/config/engine_models.py:248`
  Classification: domain/service candidate.
  Args: `task, config, engine_name, requested_model_name`.
  Candidate owner: `resolver service for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `resolve_engine_name` at `litehive/config/engine_models.py:273`
  Classification: domain/service candidate.
  Args: `task, config, engine_override`.
  Candidate owner: `resolver service for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `resolve_engine_attempt_order` at `litehive/config/engine_models.py:294`
  Classification: domain/service candidate.
  Args: `task, config, engine_override`.
  Candidate owner: `resolver service for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `_unfrozen_engine_attempt_order` at `litehive/config/engine_models.py:312`
  Classification: utility.
  Args: `engine_names, config`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `resolve_engine_plan` at `litehive/config/engine_models.py:321`
  Classification: domain/service candidate.
  Args: `task, config, engine_override`.
  Candidate owner: `resolver service for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `resolve_task_retry_policy` at `litehive/config/engine_models.py:346`
  Classification: domain/service candidate.
  Args: `task, config`.
  Candidate owner: `resolver service for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `_resolve_stage_retry_limit` at `litehive/config/engine_models.py:361`
  Classification: utility.
  Args: `task, config`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `resolve_task_rejection_loop_limit` at `litehive/config/engine_models.py:377`
  Classification: domain/service candidate.
  Args: `task, config`.
  Candidate owner: `resolver service for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.

### `litehive/config/engine_quota.py`
- `QuotaStatus.limit_reached` at `litehive/config/engine_quota.py:26`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `QuotaStatus`.
  Note: Already on an object; review class responsibility before moving.
- `QuotaStatus.short_term` at `litehive/config/engine_quota.py:29`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `QuotaStatus`.
  Note: Already on an object; review class responsibility before moving.
- `QuotaStatus.long_term` at `litehive/config/engine_quota.py:32`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `QuotaStatus`.
  Note: Already on an object; review class responsibility before moving.
- `_quota_checker` at `litehive/config/engine_quota.py:53`
  Classification: utility.
  Args: `engine_name`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_preferred_quota_reset_at` at `litehive/config/engine_quota.py:73`
  Classification: utility.
  Args: `status`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_quota_block_reason` at `litehive/config/engine_quota.py:82`
  Classification: utility.
  Args: `engine_name, status`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `engine_quota_block` at `litehive/config/engine_quota.py:101`
  Classification: domain/service candidate.
  Args: `engine_name`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `collect_engine_quota_statuses` at `litehive/config/engine_quota.py:120`
  Classification: domain/service candidate.
  Candidate owner: `collector/query service for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `_safe_quota_check` at `litehive/config/engine_quota.py:144`
  Classification: utility.
  Args: `checker`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_quota_error_label` at `litehive/config/engine_quota.py:154`
  Classification: utility.
  Args: `exc`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/config/environment.py`
- `LitehiveEnvironment.from_process` at `litehive/config/environment.py:21`
  Classification: domain/service method.
  Args: `cls`.
  Candidate owner: `LitehiveEnvironment`.
  Note: Already on an object; review class responsibility before moving.
- `LitehiveEnvironment.from_mapping` at `litehive/config/environment.py:25`
  Classification: domain/service method.
  Args: `cls, values`.
  Candidate owner: `LitehiveEnvironment`.
  Note: Already on an object; review class responsibility before moving.
- `_normalized_optional` at `litehive/config/environment.py:35`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/config/loading.py`
- `_read_config_layer` at `litehive/config/loading.py:23`
  Classification: utility.
  Args: `path`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `merge_config_layers` at `litehive/config/loading.py:41`
  Classification: domain/service candidate.
  Args: `base, overlay`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `load_effective_config_data_for_workspace` at `litehive/config/loading.py:61`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `load_config_for_workspace` at `litehive/config/loading.py:76`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `load_context_for_workspace` at `litehive/config/loading.py:94`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/config/model.py`
- `_represent_transient_failure_kind` at `litehive/config/model.py:29`
  Classification: utility.
  Args: `dumper, value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_config_list` at `litehive/config/model.py:37`
  Classification: utility.
  Args: `value, field_name`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_config_mapping` at `litehive/config/model.py:53`
  Classification: utility.
  Args: `value, field_name`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `ExternalEngineSandboxConfig.policy_for_engine` at `litehive/config/model.py:163`
  Classification: domain/service method.
  Args: `self, engine_name`.
  Candidate owner: `ExternalEngineSandboxConfig`.
  Note: Already on an object; review class responsibility before moving.
- `LitehiveConfig.__post_init__` at `litehive/config/model.py:242`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `LitehiveConfig`.
  Note: Already a method; usually special/protocol behavior.
- `LitehiveConfig.model_for_engine` at `litehive/config/model.py:279`
  Classification: domain/service method.
  Args: `self, engine_name`.
  Candidate owner: `LitehiveConfig`.
  Note: Already on an object; review class responsibility before moving.
- `LitehiveConfig.engine_attempt_order` at `litehive/config/model.py:299`
  Classification: domain/service method.
  Args: `self, initial_engine_names`.
  Candidate owner: `LitehiveConfig`.
  Note: Already on an object; review class responsibility before moving.
- `validate_config_data` at `litehive/config/model.py:315`
  Classification: domain/service candidate.
  Args: `data`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `parse_litehive_config_data` at `litehive/config/model.py:339`
  Classification: utility.
  Args: `data`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `normalize_daemon_config` at `litehive/config/model.py:361`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_positive_daemon_seconds` at `litehive/config/model.py:441`
  Classification: utility.
  Args: `value, field_name`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `normalize_engine_sequence` at `litehive/config/model.py:451`
  Classification: utility.
  Args: `engines, field_name`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `normalize_agent_startup_guidance` at `litehive/config/model.py:474`
  Classification: utility.
  Args: `guidance`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `normalize_retry_on` at `litehive/config/model.py:505`
  Classification: utility.
  Args: `retry_on, field_name`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_normalize_runner_hook` at `litehive/config/model.py:538`
  Classification: utility.
  Args: `raw_hook, field_name`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `normalize_runner_hooks` at `litehive/config/model.py:590`
  Classification: utility.
  Args: `raw_hooks`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_normalize_runner_hook_list` at `litehive/config/model.py:617`
  Classification: utility.
  Args: `point, hooks`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_normalize_sandbox_credential_input` at `litehive/config/model.py:640`
  Classification: utility.
  Args: `raw_input, field_name`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_normalize_bind_list` at `litehive/config/model.py:668`
  Classification: utility.
  Args: `raw_binds, field_name`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_normalize_sandbox_credential_inputs` at `litehive/config/model.py:689`
  Classification: utility.
  Args: `raw_inputs, field_name`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_stripped_bind_strings` at `litehive/config/model.py:712`
  Classification: utility.
  Args: `raw_binds, field_name`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_stringify_setenv_mapping` at `litehive/config/model.py:729`
  Classification: utility.
  Args: `raw_setenv, field_name`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_normalize_external_engine_sandbox_policy` at `litehive/config/model.py:746`
  Classification: utility.
  Args: `raw_policy, field_name`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_normalize_engine_policies_map` at `litehive/config/model.py:817`
  Classification: utility.
  Args: `raw_engine_policies`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `normalize_external_engine_sandbox_config` at `litehive/config/model.py:844`
  Classification: utility.
  Args: `raw_config`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/config/paths.py`
- `litehive_root` at `litehive/config/paths.py:17`
  Classification: utility.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `workspace_data_dir` at `litehive/config/paths.py:43`
  Classification: utility.
  Args: `root`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `workspace_path` at `litehive/config/paths.py:58`
  Classification: utility.
  Args: `root, *parts`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/config/profiles/loader.py`
- `available_process_profiles` at `litehive/config/profiles/loader.py:19`
  Classification: domain/service candidate.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `resolve_process_profile` at `litehive/config/profiles/loader.py:24`
  Classification: domain/service candidate.
  Args: `name`.
  Candidate owner: `resolver service for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.

### `litehive/config/profiles/rendering.py`
- `_shared_stage_text` at `litehive/config/profiles/rendering.py:7`
  Classification: utility.
  Args: `stages`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_render_process_overlay` at `litehive/config/profiles/rendering.py:18`
  Classification: utility.
  Args: `profile`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_render_project_overlay` at `litehive/config/profiles/rendering.py:41`
  Classification: utility.
  Args: `profile`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_render_scaffold_sections` at `litehive/config/profiles/rendering.py:56`
  Classification: utility.
  Args: `profile`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_render_stage_prompt_scaffolding` at `litehive/config/profiles/rendering.py:74`
  Classification: utility.
  Args: `profile`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `render_context_template` at `litehive/config/profiles/rendering.py:95`
  Classification: domain/service candidate.
  Args: `profile_name`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.

### `litehive/config/runtime_settings.py`
- `_bootstrap_config_data` at `litehive/config/runtime_settings.py:73`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_dump_runtime_value` at `litehive/config/runtime_settings.py:86`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_load_runtime_value` at `litehive/config/runtime_settings.py:103`
  Classification: utility.
  Args: `raw`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_dump_runtime_context` at `litehive/config/runtime_settings.py:120`
  Classification: utility.
  Args: `context`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_load_runtime_context` at `litehive/config/runtime_settings.py:131`
  Classification: utility.
  Args: `raw`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_sequence_value` at `litehive/config/runtime_settings.py:143`
  Classification: utility.
  Args: `raw_value, field_name`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_freeze_value` at `litehive/config/runtime_settings.py:159`
  Classification: utility.
  Args: `raw_value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_runtime_values_from_config` at `litehive/config/runtime_settings.py:176`
  Classification: utility.
  Args: `config_data`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_load_setting_rows` at `litehive/config/runtime_settings.py:198`
  Classification: utility.
  Args: `connection`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `bootstrap_runtime_settings` at `litehive/config/runtime_settings.py:217`
  Classification: domain/service candidate.
  Args: `workspace, config_data`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `load_runtime_settings` at `litehive/config/runtime_settings.py:253`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `apply_runtime_settings_to_config_data` at `litehive/config/runtime_settings.py:269`
  Classification: domain/service candidate.
  Args: `workspace, config_data`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `set_runtime_setting` at `litehive/config/runtime_settings.py:290`
  Classification: domain/service candidate.
  Args: `workspace, key, value, actor, source, context`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `set_default_engine` at `litehive/config/runtime_settings.py:359`
  Classification: domain/service candidate.
  Args: `workspace, engine_name, actor, source, context`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `set_engine_preference` at `litehive/config/runtime_settings.py:385`
  Classification: domain/service candidate.
  Args: `workspace, engines, actor, source, context`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `set_engine_freeze` at `litehive/config/runtime_settings.py:412`
  Classification: domain/service candidate.
  Args: `workspace, engine_name, freeze_iso, actor, source, context`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `clear_engine_freeze` at `litehive/config/runtime_settings.py:449`
  Classification: domain/service candidate.
  Args: `workspace, engine_name, actor, source, context`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `load_runtime_setting_audit_entries` at `litehive/config/runtime_settings.py:533`
  Classification: domain/service candidate.
  Args: `workspace, key, limit`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/config/time_parsing.py`
- `parse_utc_datetime` at `litehive/config/time_parsing.py:6`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `parse_engine_freeze_until` at `litehive/config/time_parsing.py:33`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/config/workspace.py`
- `render_workspace_gitignore` at `litehive/config/workspace.py:27`
  Classification: domain/service candidate.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `_workspace_config_template_path` at `litehive/config/workspace.py:45`
  Classification: utility.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `require_existing_workspace` at `litehive/config/workspace.py:52`
  Classification: domain/service candidate.
  Args: `root, source`.
  Candidate owner: `Workspace, WorktreeService, or GitRepository depending on module`.
  Note: Raw path identity below boundary; migrate when touched.
- `_reject_litehive_control_paths` at `litehive/config/workspace.py:69`
  Classification: utility.
  Args: `path, source`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `normalize_workspace_root` at `litehive/config/workspace.py:90`
  Classification: utility.
  Args: `root, source`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_task_exists_in_workspace` at `litehive/config/workspace.py:101`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `resolve_workspace` at `litehive/config/workspace.py:112`
  Classification: domain/service candidate.
  Args: `task_id, cwd`.
  Candidate owner: `resolver service for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `create_workspace` at `litehive/config/workspace.py:142`
  Classification: domain/service candidate.
  Args: `root, config`.
  Candidate owner: `Workspace, WorktreeService, or GitRepository depending on module`.
  Note: Raw path identity below boundary; migrate when touched.

### `litehive/config/workspace_files.py`
- `WorkspaceControlFiles.directory` at `litehive/config/workspace_files.py:28`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `WorkspaceControlFiles`.
  Note: Already on an object; review class responsibility before moving.
- `WorkspaceControlFiles.config` at `litehive/config/workspace_files.py:34`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `WorkspaceControlFiles`.
  Note: Already on an object; review class responsibility before moving.
- `WorkspaceControlFiles.context` at `litehive/config/workspace_files.py:40`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `WorkspaceControlFiles`.
  Note: Already on an object; review class responsibility before moving.
- `WorkspaceControlFiles.gitignore` at `litehive/config/workspace_files.py:46`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `WorkspaceControlFiles`.
  Note: Already on an object; review class responsibility before moving.
- `workspace_dir` at `litehive/config/workspace_files.py:53`
  Classification: utility.
  Args: `root`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `config_path` at `litehive/config/workspace_files.py:65`
  Classification: utility.
  Args: `root`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `context_path` at `litehive/config/workspace_files.py:77`
  Classification: utility.
  Args: `root`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `workspace_gitignore_path` at `litehive/config/workspace_files.py:89`
  Classification: utility.
  Args: `root`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/container.py`
- `build_container` at `litehive/container.py:56`
  Classification: domain/service candidate.
  Args: `root`.
  Candidate owner: `Workspace, WorktreeService, or GitRepository depending on module`.
  Note: Raw path identity below boundary; migrate when touched.
- `build_pipeline_container` at `litehive/container.py:72`
  Classification: domain/service candidate.
  Args: `root`.
  Candidate owner: `Workspace, WorktreeService, or GitRepository depending on module`.
  Note: Raw path identity below boundary; migrate when touched.
- `build_daemon_container` at `litehive/container.py:84`
  Classification: domain/service candidate.
  Args: `root`.
  Candidate owner: `Workspace, WorktreeService, or GitRepository depending on module`.
  Note: Raw path identity below boundary; migrate when touched.
- `build_workspace` at `litehive/container.py:96`
  Classification: domain/service candidate.
  Args: `root`.
  Candidate owner: `Workspace, WorktreeService, or GitRepository depending on module`.
  Note: Raw path identity below boundary; migrate when touched.
- `build_agent_report_submitter` at `litehive/container.py:107`
  Classification: domain/service candidate.
  Args: `workspace, env_role, env_subagent_id, env_stage`.
  Candidate owner: `DI container factory module`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `build_agent_report_submitter.load_pipeline_stage` at `litehive/container.py:118`
  Classification: utility.
  Args: `task_id`.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `build_agent_task_mutator_for_workspace` at `litehive/container.py:134`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `DI container factory module`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `build_subagent_manager_for_workspace` at `litehive/container.py:144`
  Classification: domain/service candidate.
  Args: `workspace, config, execution_root, manager_type`.
  Candidate owner: `DI container factory module`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/daemon/execution.py`
- `register_daemon` at `litehive/daemon/execution.py:73`
  Classification: domain/service candidate.
  Args: `workspace, pid, log_dir`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `unregister_daemon` at `litehive/daemon/execution.py:83`
  Classification: domain/service candidate.
  Args: `workspace, pid`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `DaemonOutput.__init__` at `litehive/daemon/execution.py:119`
  Classification: utility/protocol method.
  Args: `self, stream`.
  Candidate owner: `DaemonOutput`.
  Note: Already a method; usually special/protocol behavior.
- `DaemonOutput.line` at `litehive/daemon/execution.py:122`
  Classification: domain/service method.
  Args: `self, message`.
  Candidate owner: `DaemonOutput`.
  Note: Already on an object; review class responsibility before moving.
- `DaemonOutput.child_line` at `litehive/daemon/execution.py:130`
  Classification: domain/service method.
  Args: `self, line`.
  Candidate owner: `DaemonOutput`.
  Note: Already on an object; review class responsibility before moving.
- `DaemonOutput.runner_wait` at `litehive/daemon/execution.py:136`
  Classification: domain/service method.
  Args: `self, status`.
  Candidate owner: `DaemonOutput`.
  Note: Already on an object; review class responsibility before moving.
- `_halt_for_origin_divergence` at `litehive/daemon/execution.py:158`
  Classification: domain/service candidate.
  Args: `workspace, attention_repository`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `sleep_with_stop` at `litehive/daemon/execution.py:179`
  Classification: domain/service candidate.
  Args: `seconds, stop_requested_fn`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `_daemon_status_snapshot_for_workspace` at `litehive/daemon/execution.py:197`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_heartbeat_age_seconds` at `litehive/daemon/execution.py:212`
  Classification: utility.
  Args: `heartbeat_at`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_daemon_healthcheck_failed` at `litehive/daemon/execution.py:238`
  Classification: utility.
  Args: `entry, config`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_runner_is_live` at `litehive/daemon/execution.py:253`
  Classification: utility.
  Args: `status`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_has_work` at `litehive/daemon/execution.py:266`
  Classification: utility.
  Args: `state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_pool_stop_reason_from_state` at `litehive/daemon/execution.py:277`
  Classification: utility.
  Args: `state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_daemon_should_continue_for_stop_reason` at `litehive/daemon/execution.py:283`
  Classification: utility.
  Args: `reason`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_snapshot_exit_code` at `litehive/daemon/execution.py:297`
  Classification: utility.
  Args: `snapshot, output`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `create_workspace_venvs_ready_for_workspace` at `litehive/daemon/execution.py:313`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `maybe_run_workspace_backup` at `litehive/daemon/execution.py:331`
  Classification: domain/service candidate.
  Args: `workspace, now`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `run_logged_subprocess` at `litehive/daemon/execution.py:354`
  Classification: domain/service candidate.
  Args: `command, cwd, log_path, output, current_child`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `DaemonExecutor.run` at `litehive/daemon/execution.py:411`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `DaemonExecutor`.
  Note: Already on an object; review class responsibility before moving.
- `DaemonExecutor.run._handle_signal` at `litehive/daemon/execution.py:432`
  Classification: domain/service method.
  Args: `signum, _frame`.
  Candidate owner: `DaemonExecutor`.
  Note: Already on an object; review class responsibility before moving.
- `run_daemon_loop` at `litehive/daemon/execution.py:536`
  Classification: domain/service candidate.
  Args: `workspace, output_stream, session_dir`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_daemon_heartbeat_loop` at `litehive/daemon/execution.py:567`
  Classification: domain/service candidate.
  Args: `workspace, pid, stop_event, interval_seconds`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `start_background_daemon` at `litehive/daemon/execution.py:587`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `stop_workspace_daemon` at `litehive/daemon/execution.py:644`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `daemon_status_lines` at `litehive/daemon/execution.py:671`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `daemon_status_lines_for_workspace` at `litehive/daemon/execution.py:682`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/daemon/logs.py`
- `DaemonLogs.run_all_base` at `litehive/daemon/logs.py:37`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `DaemonLogs`.
  Note: Already on an object; review class responsibility before moving.
- `DaemonLogs.prepare_session` at `litehive/daemon/logs.py:40`
  Classification: domain/service method.
  Args: `self, session_dir`.
  Candidate owner: `DaemonLogs`.
  Note: Already on an object; review class responsibility before moving.
- `DaemonLogs.prune_sessions` at `litehive/daemon/logs.py:48`
  Classification: domain/service method.
  Args: `self, keep`.
  Candidate owner: `DaemonLogs`.
  Note: Already on an object; review class responsibility before moving.
- `DaemonLogs.latest_run_all_dir` at `litehive/daemon/logs.py:51`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `DaemonLogs`.
  Note: Already on an object; review class responsibility before moving.
- `DaemonLogs.latest_matching` at `litehive/daemon/logs.py:60`
  Classification: domain/service method.
  Args: `self, pattern`.
  Candidate owner: `DaemonLogs`.
  Note: Already on an object; review class responsibility before moving.
- `latest_run_all_log_dir_for_workspace` at `litehive/daemon/logs.py:64`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `prune_run_all_log_dirs` at `litehive/daemon/logs.py:77`
  Classification: domain/service candidate.
  Args: `log_base, keep`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `latest_matching` at `litehive/daemon/logs.py:99`
  Classification: domain/service candidate.
  Args: `log_dir, pattern`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.

### `litehive/daemon/registry.py`
- `_daemon_lock_key_for_workspace` at `litehive/daemon/registry.py:33`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `DaemonRegistryEntry.from_metadata` at `litehive/daemon/registry.py:59`
  Classification: domain/service method.
  Args: `cls, metadata, status`.
  Candidate owner: `DaemonRegistryEntry`.
  Note: Already on an object; review class responsibility before moving.
- `_optional_text_field` at `litehive/daemon/registry.py:78`
  Classification: utility.
  Args: `metadata, key`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_daemon_lock_path_for_workspace` at `litehive/daemon/registry.py:85`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_daemon_lock_is_held_in_process` at `litehive/daemon/registry.py:97`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_daemon_lock_manager_for_workspace` at `litehive/daemon/registry.py:111`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `daemon_lock_is_active_for_workspace` at `litehive/daemon/registry.py:134`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_clear_stale_daemon_metadata_for_workspace` at `litehive/daemon/registry.py:146`
  Classification: domain/service candidate.
  Args: `workspace, pid`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `daemon_metadata_for_workspace` at `litehive/daemon/registry.py:160`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `get_workspace_daemon_for_workspace` at `litehive/daemon/registry.py:181`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `register_daemon_for_workspace` at `litehive/daemon/registry.py:198`
  Classification: domain/service candidate.
  Args: `workspace, pid, log_dir`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `unregister_daemon_for_workspace` at `litehive/daemon/registry.py:248`
  Classification: domain/service candidate.
  Args: `workspace, pid`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `touch_daemon_for_workspace` at `litehive/daemon/registry.py:274`
  Classification: domain/service candidate.
  Args: `workspace, pid`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `stale_daemon_metadata_for_workspace` at `litehive/daemon/registry.py:302`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/daemon/termination.py`
- `wait_for_pid_exit` at `litehive/daemon/termination.py:22`
  Classification: domain/service candidate.
  Args: `pid, timeout_seconds, poll_interval_seconds`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `force_kill_recorded_daemon` at `litehive/daemon/termination.py:41`
  Classification: domain/service candidate.
  Args: `workspace, pid, config`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `terminate_recorded_daemon` at `litehive/daemon/termination.py:70`
  Classification: domain/service candidate.
  Args: `workspace, pid, config`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `terminate_child_process` at `litehive/daemon/termination.py:99`
  Classification: domain/service candidate.
  Args: `process`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.

### `litehive/db/migration_hooks/task_intent_columns.py`
- `task_intent_column_values` at `litehive/db/migration_hooks/task_intent_columns.py:22`
  Classification: utility.
  Args: `intent, state`.
  Candidate owner: `none`.
  Note: No obvious state/identity owner.
- `sync_task_intent_columns` at `litehive/db/migration_hooks/task_intent_columns.py:55`
  Classification: utility.
  Args: `connection`.
  Candidate owner: `none`.
  Note: No obvious state/identity owner.

### `litehive/db/schema.py`
- `MigrationApplyError.__init__` at `litehive/db/schema.py:96`
  Classification: utility/protocol method.
  Args: `self, migration, cause`.
  Candidate owner: `MigrationApplyError`.
  Note: Already a method; usually special/protocol behavior.
- `_utcnow` at `litehive/db/schema.py:110`
  Classification: utility.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_migration_resources` at `litehive/db/schema.py:122`
  Classification: utility.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `available_migrations` at `litehive/db/schema.py:134`
  Classification: utility.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_open_connection` at `litehive/db/schema.py:161`
  Classification: utility.
  Args: `db_path`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_ensure_schema_migrations_table` at `litehive/db/schema.py:184`
  Classification: utility.
  Args: `connection`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_applied_versions` at `litehive/db/schema.py:206`
  Classification: utility.
  Args: `connection`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_applied_migration_rows` at `litehive/db/schema.py:221`
  Classification: utility.
  Args: `connection`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_has_required_baseline_tables` at `litehive/db/schema.py:239`
  Classification: utility.
  Args: `connection, applied_versions`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_migration_history_matches_prefix` at `litehive/db/schema.py:262`
  Classification: utility.
  Args: `applied, available`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_database_requires_rebuild` at `litehive/db/schema.py:281`
  Classification: utility.
  Args: `db_path, migrations`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `migration_status` at `litehive/db/schema.py:306`
  Classification: utility.
  Args: `root`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `apply_pending_migrations` at `litehive/db/schema.py:336`
  Classification: utility.
  Args: `root, dry_run`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_db_fingerprint` at `litehive/db/schema.py:403`
  Classification: utility.
  Args: `db_path`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_db_cache_key` at `litehive/db/schema.py:426`
  Classification: utility.
  Args: `db_path`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `consume_rebuilt_database_marker` at `litehive/db/schema.py:439`
  Classification: utility.
  Args: `root`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `connect_workspace_db` at `litehive/db/schema.py:457`
  Classification: utility.
  Args: `root, migrate`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/domain/agent.py`
- `ExecutionTrace.from_text` at `litehive/domain/agent.py:39`
  Classification: domain/service method.
  Args: `cls, text`.
  Candidate owner: `ExecutionTrace`.
  Note: Already on an object; review class responsibility before moving.
- `ExecutionTrace.text` at `litehive/domain/agent.py:47`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `ExecutionTrace`.
  Note: Already on an object; review class responsibility before moving.
- `ExecutionTrace.__bool__` at `litehive/domain/agent.py:51`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `ExecutionTrace`.
  Note: Already a method; usually special/protocol behavior.
- `SubagentInactivityTimeout.__init__` at `litehive/domain/agent.py:106`
  Classification: utility/protocol method.
  Args: `self, execution, idle_seconds, limit_seconds`.
  Candidate owner: `SubagentInactivityTimeout`.
  Note: Already a method; usually special/protocol behavior.

### `litehive/domain/common.py`
- `StringEnum.__str__` at `litehive/domain/common.py:40`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `StringEnum`.
  Note: Already a method; usually special/protocol behavior.
- `utcnow` at `litehive/domain/common.py:51`
  Classification: utility.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `PipelineState.human_label` at `litehive/domain/common.py:122`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `PipelineState`.
  Note: Already on an object; review class responsibility before moving.
- `PipelineState.task_stage` at `litehive/domain/common.py:136`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `PipelineState`.
  Note: Already on an object; review class responsibility before moving.
- `PipelineState.pipeline_status` at `litehive/domain/common.py:172`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `PipelineState`.
  Note: Already on an object; review class responsibility before moving.
- `PipelineState.primary_stage` at `litehive/domain/common.py:206`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `PipelineState`.
  Note: Already on an object; review class responsibility before moving.
- `PipelineState.accepts_runner_hook` at `litehive/domain/common.py:232`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `PipelineState`.
  Note: Already on an object; review class responsibility before moving.
- `TaskStage.owner_role` at `litehive/domain/common.py:277`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `TaskStage`.
  Note: Already on an object; review class responsibility before moving.
- `TaskStage.retry_counter_state` at `litehive/domain/common.py:301`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `TaskStage`.
  Note: Already on an object; review class responsibility before moving.
- `runner_hook_points` at `litehive/domain/common.py:324`
  Classification: domain function.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.
- `canonical_pipeline_state` at `litehive/domain/common.py:430`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `task_stage_for_pipeline_state` at `litehive/domain/common.py:446`
  Classification: domain function.
  Args: `value`.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.
- `pipeline_stage_key` at `litehive/domain/common.py:459`
  Classification: domain function.
  Args: `name`.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.
- `pipeline_status_for_pipeline_state` at `litehive/domain/common.py:479`
  Classification: domain function.
  Args: `value`.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.
- `Verdict.stage_report_verdict` at `litehive/domain/common.py:557`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Verdict`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/domain/failure_diagnostics.py`
- `FailureDiagnostics.__bool__` at `litehive/domain/failure_diagnostics.py:22`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `FailureDiagnostics`.
  Note: Already a method; usually special/protocol behavior.
- `FailureDiagnostics.__getitem__` at `litehive/domain/failure_diagnostics.py:25`
  Classification: utility/protocol method.
  Args: `self, key`.
  Candidate owner: `FailureDiagnostics`.
  Note: Already a method; usually special/protocol behavior.
- `FailureDiagnostics.get` at `litehive/domain/failure_diagnostics.py:28`
  Classification: domain/service method.
  Args: `self, key, default`.
  Candidate owner: `FailureDiagnostics`.
  Note: Already on an object; review class responsibility before moving.
- `FailureDiagnostics.as_dict` at `litehive/domain/failure_diagnostics.py:31`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `FailureDiagnostics`.
  Note: Already on an object; review class responsibility before moving.
- `empty_failure_diagnostics` at `litehive/domain/failure_diagnostics.py:38`
  Classification: domain function.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.

### `litehive/domain/lifecycle_deltas.py`
- `_rejection_from_event` at `litehive/domain/lifecycle_deltas.py:91`
  Classification: utility.
  Args: `state, event`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_normalized_failure_text` at `litehive/domain/lifecycle_deltas.py:110`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_event_failure_shape` at `litehive/domain/lifecycle_deltas.py:124`
  Classification: utility.
  Args: `event`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_stage_retry_exhausted_record` at `litehive/domain/lifecycle_deltas.py:152`
  Classification: utility.
  Args: `state, event, failed_reason, message`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_reason_code_from_event` at `litehive/domain/lifecycle_deltas.py:197`
  Classification: utility.
  Args: `state, event`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_fingerprint_from_event` at `litehive/domain/lifecycle_deltas.py:218`
  Classification: utility.
  Args: `state, event`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `recovery_trigger_from_event` at `litehive/domain/lifecycle_deltas.py:278`
  Classification: domain function.
  Args: `state, event`.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.
- `_hook_fingerprint_from_event` at `litehive/domain/lifecycle_deltas.py:301`
  Classification: utility.
  Args: `event`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_hook_reject_loop_detected` at `litehive/domain/lifecycle_deltas.py:329`
  Classification: utility.
  Args: `state, event`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_hook_reject_delta` at `litehive/domain/lifecycle_deltas.py:345`
  Classification: utility.
  Args: `state, event, recovery_invoked`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_next_rejection_loop` at `litehive/domain/lifecycle_deltas.py:385`
  Classification: utility.
  Args: `state, event, retry_target_stage`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `rejection_loop_detected` at `litehive/domain/lifecycle_deltas.py:426`
  Classification: domain function.
  Args: `state, event, retry_target_stage`.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.
- `_rejection_loop_delta` at `litehive/domain/lifecycle_deltas.py:440`
  Classification: utility.
  Args: `state, event, retry_target_stage`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `enter_recovery` at `litehive/domain/lifecycle_deltas.py:455`
  Classification: domain function.
  Args: `state, event`.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.
- `enter_pre_exec_recovery` at `litehive/domain/lifecycle_deltas.py:487`
  Classification: domain function.
  Args: `state, event`.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.
- `_pipeline_stage_key` at `litehive/domain/lifecycle_deltas.py:501`
  Classification: utility.
  Args: `name`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_retry_counter_stage` at `litehive/domain/lifecycle_deltas.py:516`
  Classification: utility.
  Args: `origin_stage`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_hook_recovery_made_progress` at `litehive/domain/lifecycle_deltas.py:542`
  Classification: utility.
  Args: `trigger, event`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `record_recovery_success` at `litehive/domain/lifecycle_deltas.py:560`
  Classification: domain function.
  Args: `state, event`.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.
- `IncStageRetry.__call__` at `litehive/domain/lifecycle_deltas.py:621`
  Classification: utility/protocol method.
  Args: `self, state, event`.
  Candidate owner: `IncStageRetry`.
  Note: Already a method; usually special/protocol behavior.
- `RememberRejection.__call__` at `litehive/domain/lifecycle_deltas.py:645`
  Classification: utility/protocol method.
  Args: `self, state, event`.
  Candidate owner: `RememberRejection`.
  Note: Already a method; usually special/protocol behavior.
- `_rejection_tracking_delta` at `litehive/domain/lifecycle_deltas.py:664`
  Classification: utility.
  Args: `state, event, stage, increment_retry, retry_target_stage`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `FailRejectionLoop.__call__` at `litehive/domain/lifecycle_deltas.py:718`
  Classification: utility/protocol method.
  Args: `self, state, event`.
  Candidate owner: `FailRejectionLoop`.
  Note: Already a method; usually special/protocol behavior.
- `clear_completed_rejection_loop` at `litehive/domain/lifecycle_deltas.py:746`
  Classification: domain function.
  Args: `state, event`.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.
- `stash_conflict_files` at `litehive/domain/lifecycle_deltas.py:766`
  Classification: domain function.
  Args: `state, event`.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.
- `Fail.__post_init__` at `litehive/domain/lifecycle_deltas.py:803`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `Fail`.
  Note: Already a method; usually special/protocol behavior.
- `Fail.__call__` at `litehive/domain/lifecycle_deltas.py:816`
  Classification: utility/protocol method.
  Args: `self, state, event`.
  Candidate owner: `Fail`.
  Note: Already a method; usually special/protocol behavior.
- `exhaust_recovery_budget` at `litehive/domain/lifecycle_deltas.py:870`
  Classification: domain function.
  Args: `state, event`.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.
- `_recovery_verdict_for_terminal_event` at `litehive/domain/lifecycle_deltas.py:901`
  Classification: utility.
  Args: `event, reason`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_recovery_failure_explanation` at `litehive/domain/lifecycle_deltas.py:917`
  Classification: utility.
  Args: `trigger, reason, message`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/domain/outcomes.py`
- `TaskCloseReason.outcome_reason_code` at `litehive/domain/outcomes.py:61`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `TaskCloseReason`.
  Note: Already on an object; review class responsibility before moving.
- `TaskCloseReason.task_close_label` at `litehive/domain/outcomes.py:70`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `TaskCloseReason`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/domain/pool.py`
- `PoolStopReason.from_value` at `litehive/domain/pool.py:44`
  Classification: domain/service method.
  Args: `cls, value`.
  Candidate owner: `PoolStopReason`.
  Note: Already on an object; review class responsibility before moving.
- `PoolStopReason.operator_label` at `litehive/domain/pool.py:54`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `PoolStopReason`.
  Note: Already on an object; review class responsibility before moving.
- `PoolStopReason.progress_report` at `litehive/domain/pool.py:93`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `PoolStopReason`.
  Note: Already on an object; review class responsibility before moving.
- `PoolTaskReportEntry.from_mapping` at `litehive/domain/pool.py:163`
  Classification: domain/service method.
  Args: `cls, data`.
  Candidate owner: `PoolTaskReportEntry`.
  Note: Already on an object; review class responsibility before moving.
- `PoolSummaryReport.from_mapping` at `litehive/domain/pool.py:205`
  Classification: domain/service method.
  Args: `cls, data`.
  Candidate owner: `PoolSummaryReport`.
  Note: Already on an object; review class responsibility before moving.
- `PoolSummaryReport.stop_condition` at `litehive/domain/pool.py:224`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `PoolSummaryReport`.
  Note: Already on an object; review class responsibility before moving.
- `PoolSummaryReport.completed_count` at `litehive/domain/pool.py:234`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `PoolSummaryReport`.
  Note: Already on an object; review class responsibility before moving.
- `PoolSummaryReport.flagged_count` at `litehive/domain/pool.py:238`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `PoolSummaryReport`.
  Note: Already on an object; review class responsibility before moving.
- `PoolSummaryReport.resumable_count` at `litehive/domain/pool.py:242`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `PoolSummaryReport`.
  Note: Already on an object; review class responsibility before moving.
- `PoolSummaryReport.closed_count` at `litehive/domain/pool.py:246`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `PoolSummaryReport`.
  Note: Already on an object; review class responsibility before moving.
- `PoolSummaryReport.skipped_count` at `litehive/domain/pool.py:250`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `PoolSummaryReport`.
  Note: Already on an object; review class responsibility before moving.
- `PoolSummaryReport.remaining_count` at `litehive/domain/pool.py:254`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `PoolSummaryReport`.
  Note: Already on an object; review class responsibility before moving.
- `PoolSummaryReport.with_derived_progress_report` at `litehive/domain/pool.py:257`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `PoolSummaryReport`.
  Note: Already on an object; review class responsibility before moving.
- `_optional_report_string` at `litehive/domain/pool.py:274`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_int_report_value` at `litehive/domain/pool.py:283`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_report_entries_from_value` at `litehive/domain/pool.py:294`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `DirtyWorktreeOwnership.blocks_pool` at `litehive/domain/pool.py:340`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `DirtyWorktreeOwnership`.
  Note: Already on an object; review class responsibility before moving.
- `DirtyWorktreeFinding.__post_init__` at `litehive/domain/pool.py:369`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `DirtyWorktreeFinding`.
  Note: Already a method; usually special/protocol behavior.
- `DirtyWorktreeGateReport.is_clean` at `litehive/domain/pool.py:396`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `DirtyWorktreeGateReport`.
  Note: Already on an object; review class responsibility before moving.
- `DirtyWorktreeGateReport.blocks_pool` at `litehive/domain/pool.py:407`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `DirtyWorktreeGateReport`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/domain/recovery.py`
- `FailureFingerprint.budget_key` at `litehive/domain/recovery.py:91`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `FailureFingerprint`.
  Note: Already on an object; review class responsibility before moving.
- `FailureFingerprint.to_payload` at `litehive/domain/recovery.py:103`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `FailureFingerprint`.
  Note: Already on an object; review class responsibility before moving.
- `FailureFingerprint.from_payload` at `litehive/domain/recovery.py:118`
  Classification: domain/service method.
  Args: `cls, payload`.
  Candidate owner: `FailureFingerprint`.
  Note: Already on an object; review class responsibility before moving.
- `RecoveryTrigger.budget_key` at `litehive/domain/recovery.py:155`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `RecoveryTrigger`.
  Note: Already on an object; review class responsibility before moving.
- `RecoveryTrigger.to_payload` at `litehive/domain/recovery.py:167`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `RecoveryTrigger`.
  Note: Already on an object; review class responsibility before moving.
- `RecoveryTrigger.from_payload` at `litehive/domain/recovery.py:185`
  Classification: domain/service method.
  Args: `cls, payload`.
  Candidate owner: `RecoveryTrigger`.
  Note: Already on an object; review class responsibility before moving.
- `RecoveryOutcome.to_payload` at `litehive/domain/recovery.py:225`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `RecoveryOutcome`.
  Note: Already on an object; review class responsibility before moving.
- `RecoveryOutcome.from_payload` at `litehive/domain/recovery.py:243`
  Classification: domain/service method.
  Args: `cls, payload`.
  Candidate owner: `RecoveryOutcome`.
  Note: Already on an object; review class responsibility before moving.
- `blocked_on_follow_up_reason` at `litehive/domain/recovery.py:265`
  Classification: domain function.
  Args: `follow_up_task_id`.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.
- `parse_blocked_on_follow_up_reason` at `litehive/domain/recovery.py:279`
  Classification: utility.
  Args: `reason`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/domain/reports.py`
- `classify_task_activity_verdict` at `litehive/domain/reports.py:62`
  Classification: domain function.
  Args: `role, verdict`.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.
- `canonical_stage_report_verdict` at `litehive/domain/reports.py:81`
  Classification: utility.
  Args: `verdict`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `canonical_report_pipeline_state` at `litehive/domain/reports.py:114`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `TaskActivityEntry._reject_stage_report_aliases` at `litehive/domain/reports.py:294`
  Classification: domain/service method.
  Args: `cls, verdict`.
  Candidate owner: `TaskActivityEntry`.
  Note: Already on an object; review class responsibility before moving.
- `TaskActivityEntry._default_legacy_source` at `litehive/domain/reports.py:308`
  Classification: domain/service method.
  Args: `cls, data`.
  Candidate owner: `TaskActivityEntry`.
  Note: Already on an object; review class responsibility before moving.
- `TaskActivityEntry._require_agent_source_subagent_id` at `litehive/domain/reports.py:331`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `TaskActivityEntry`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/domain/roles.py`
- `AgentRole.default_stage` at `litehive/domain/roles.py:53`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `AgentRole`.
  Note: Already on an object; review class responsibility before moving.
- `AgentRole.pipeline_stages` at `litehive/domain/roles.py:77`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `AgentRole`.
  Note: Already on an object; review class responsibility before moving.
- `AgentRole.task_stages` at `litehive/domain/roles.py:101`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `AgentRole`.
  Note: Already on an object; review class responsibility before moving.
- `AgentRole.allowed_activity_verdicts` at `litehive/domain/roles.py:112`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `AgentRole`.
  Note: Already on an object; review class responsibility before moving.
- `agent_startup_guidance_keys` at `litehive/domain/roles.py:121`
  Classification: domain function.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.
- `known_agent_role` at `litehive/domain/roles.py:139`
  Classification: domain function.
  Args: `value`.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.
- `agent_activity_verdicts_for_role` at `litehive/domain/roles.py:155`
  Classification: domain function.
  Args: `role`.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.
- `agent_verdict_requires_target_stage` at `litehive/domain/roles.py:169`
  Classification: domain function.
  Args: `role, verdict`.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.
- `agent_stage_for_task` at `litehive/domain/roles.py:176`
  Classification: domain function.
  Args: `task, role`.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.
- `_reportable_stage_from_runtime` at `litehive/domain/roles.py:199`
  Classification: utility.
  Args: `current_stage`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_task_stage_from_value` at `litehive/domain/roles.py:224`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_stage_from_pipeline_status` at `litehive/domain/roles.py:234`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/domain/runtime.py`
- `_json_enum_value` at `litehive/domain/runtime.py:41`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `RuntimeStageState.model_copy` at `litehive/domain/runtime.py:86`
  Classification: domain/service method.
  Args: `self, update, deep`.
  Candidate owner: `RuntimeStageState`.
  Note: Already on an object; review class responsibility before moving.
- `TaskOutcomeState._serialize_runtime_enum_value` at `litehive/domain/runtime.py:255`
  Classification: domain/service method.
  Args: `self, value`.
  Candidate owner: `TaskOutcomeState`.
  Note: Already on an object; review class responsibility before moving.
- `PipelineRuntime._serialize_execution_status` at `litehive/domain/runtime.py:319`
  Classification: domain/service method.
  Args: `self, value`.
  Candidate owner: `PipelineRuntime`.
  Note: Already on an object; review class responsibility before moving.
- `PipelineRuntime.current_stage_name` at `litehive/domain/runtime.py:326`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `PipelineRuntime`.
  Note: Already on an object; review class responsibility before moving.
- `TaskRuntime.current_stage_name` at `litehive/domain/runtime.py:368`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `TaskRuntime`.
  Note: Already on an object; review class responsibility before moving.
- `TaskRuntime.for_storage` at `litehive/domain/runtime.py:374`
  Classification: domain/service method.
  Args: `self, commit_sha, worktree_path`.
  Candidate owner: `TaskRuntime`.
  Note: Already on an object; review class responsibility before moving.
- `RunnerStatusState._serialize_status` at `litehive/domain/runtime.py:416`
  Classification: domain/service method.
  Args: `self, value`.
  Candidate owner: `RunnerStatusState`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/domain/task.py`
- `canonicalize_task_terminal_state` at `litehive/domain/task.py:25`
  Classification: domain function.
  Args: `task`.
  Candidate owner: `existing domain value object or new domain service`.
  Note: Domain behavior; prefer method when it belongs to one value object.
- `GitSettings.to_intent_git_settings` at `litehive/domain/task.py:107`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `GitSettings`.
  Note: Already on an object; review class responsibility before moving.
- `GitSettings.to_state_git_settings` at `litehive/domain/task.py:120`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `GitSettings`.
  Note: Already on an object; review class responsibility before moving.
- `TaskStateGitSettings.to_git_updates` at `litehive/domain/task.py:165`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `TaskStateGitSettings`.
  Note: Already on an object; review class responsibility before moving.
- `TaskStateRecord.apply_to_task` at `litehive/domain/task.py:234`
  Classification: domain/service method.
  Args: `self, record`.
  Candidate owner: `TaskStateRecord`.
  Note: Already on an object; review class responsibility before moving.
- `TaskRecord.current_pipeline_stage` at `litehive/domain/task.py:302`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `TaskRecord`.
  Note: Already on an object; review class responsibility before moving.
- `TaskRecord.is_pool_pending` at `litehive/domain/task.py:313`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `TaskRecord`.
  Note: Already on an object; review class responsibility before moving.
- `TaskRecord.is_resumable` at `litehive/domain/task.py:322`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `TaskRecord`.
  Note: Already on an object; review class responsibility before moving.
- `TaskRecord.is_closed` at `litehive/domain/task.py:331`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `TaskRecord`.
  Note: Already on an object; review class responsibility before moving.
- `TaskRecord.to_intent_record` at `litehive/domain/task.py:337`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `TaskRecord`.
  Note: Already on an object; review class responsibility before moving.
- `TaskRecord.to_state_record` at `litehive/domain/task.py:361`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `TaskRecord`.
  Note: Already on an object; review class responsibility before moving.
- `TaskRecord.to_storage_state_record` at `litehive/domain/task.py:384`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `TaskRecord`.
  Note: Already on an object; review class responsibility before moving.
- `TaskRecord.from_intent_and_state` at `litehive/domain/task.py:404`
  Classification: domain/service method.
  Args: `cls, intent, state`.
  Candidate owner: `TaskRecord`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/domain/task_ops.py`
- `WorkspaceRepairSummary.repaired` at `litehive/domain/task_ops.py:91`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `WorkspaceRepairSummary`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/domain/worktree.py`
- `ManagedWorktree.cleanable` at `litehive/domain/worktree.py:39`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `ManagedWorktree`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeMergeConflict.__init__` at `litehive/domain/worktree.py:100`
  Classification: utility/protocol method.
  Args: `self, conflict_files`.
  Candidate owner: `WorktreeMergeConflict`.
  Note: Already a method; usually special/protocol behavior.

### `litehive/feedback.py`
- `cap_feedback` at `litehive/feedback.py:9`
  Classification: utility.
  Args: `text, limit`.
  Candidate owner: `none`.
  Note: No obvious state/identity owner.

### `litehive/fs_cleanup.py`
- `remove_tree_logged` at `litehive/fs_cleanup.py:16`
  Classification: utility.
  Args: `path, logger, target_label`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/git/ops.py`
- `_run_git` at `litehive/git/ops.py:39`
  Classification: utility.
  Args: `root, *args`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `is_git_repo` at `litehive/git/ops.py:58`
  Classification: utility.
  Args: `root`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `has_changes` at `litehive/git/ops.py:72`
  Classification: utility.
  Args: `root`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `status_porcelain` at `litehive/git/ops.py:85`
  Classification: utility.
  Args: `root`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `has_non_litehive_changes` at `litehive/git/ops.py:101`
  Classification: utility.
  Args: `root`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `current_head` at `litehive/git/ops.py:120`
  Classification: utility.
  Args: `root`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `rev_parse_verify` at `litehive/git/ops.py:136`
  Classification: utility.
  Args: `cwd, ref`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `list_remote_names` at `litehive/git/ops.py:152`
  Classification: utility.
  Args: `cwd`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `fetch` at `litehive/git/ops.py:167`
  Classification: utility.
  Args: `cwd, remote, *refs`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `check_origin_divergence` at `litehive/git/ops.py:180`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `delete_branch` at `litehive/git/ops.py:218`
  Classification: utility.
  Args: `cwd, branch`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `remote_url` at `litehive/git/ops.py:231`
  Classification: utility.
  Args: `cwd, remote`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `head_sha_strict` at `litehive/git/ops.py:247`
  Classification: utility.
  Args: `cwd`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `current_branch` at `litehive/git/ops.py:262`
  Classification: utility.
  Args: `cwd`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `cherry_pick_no_commit` at `litehive/git/ops.py:277`
  Classification: utility.
  Args: `cwd, sha`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `cherry_pick_abort` at `litehive/git/ops.py:293`
  Classification: utility.
  Args: `cwd`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `index_has_staged_changes` at `litehive/git/ops.py:305`
  Classification: utility.
  Args: `cwd`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `commit_reuse_message` at `litehive/git/ops.py:323`
  Classification: utility.
  Args: `cwd, sha`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `cherry_check` at `litehive/git/ops.py:339`
  Classification: utility.
  Args: `cwd, upstream_sha, head_sha`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `status_porcelain_with_options` at `litehive/git/ops.py:356`
  Classification: utility.
  Args: `cwd, include_ignored`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `add_paths` at `litehive/git/ops.py:375`
  Classification: utility.
  Args: `cwd, paths, all_flag`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `commit_with_message_stdin` at `litehive/git/ops.py:394`
  Classification: utility.
  Args: `cwd, message`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `commit_no_edit` at `litehive/git/ops.py:417`
  Classification: utility.
  Args: `cwd`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `is_path_tracked` at `litehive/git/ops.py:436`
  Classification: utility.
  Args: `cwd, path`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `check_ignore` at `litehive/git/ops.py:449`
  Classification: utility.
  Args: `cwd, path`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `stdout_or_none` at `litehive/git/ops.py:468`
  Classification: utility.
  Args: `cwd, *args`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `stdout_lines` at `litehive/git/ops.py:486`
  Classification: utility.
  Args: `cwd, *args`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `path_differs_at_ref` at `litehive/git/ops.py:502`
  Classification: utility.
  Args: `cwd, ref, path`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `add_worktree` at `litehive/git/ops.py:520`
  Classification: utility.
  Args: `root, path, ref`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `prune_worktrees` at `litehive/git/ops.py:534`
  Classification: utility.
  Args: `root, expire_now`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `add_worktree_branch` at `litehive/git/ops.py:552`
  Classification: utility.
  Args: `root, branch, path, ref, force`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `list_worktrees_porcelain` at `litehive/git/ops.py:572`
  Classification: utility.
  Args: `root`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `diff_name_status` at `litehive/git/ops.py:588`
  Classification: utility.
  Args: `cwd, *args`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `path_exists_in_ref` at `litehive/git/ops.py:612`
  Classification: utility.
  Args: `cwd, ref, path`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `show_at_ref` at `litehive/git/ops.py:625`
  Classification: utility.
  Args: `cwd, ref, path`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `checkout_ref` at `litehive/git/ops.py:640`
  Classification: utility.
  Args: `cwd, ref`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `stash_push` at `litehive/git/ops.py:653`
  Classification: utility.
  Args: `cwd, message, include_untracked, paths`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `stash_apply` at `litehive/git/ops.py:681`
  Classification: utility.
  Args: `cwd, ref`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `stash_drop` at `litehive/git/ops.py:695`
  Classification: utility.
  Args: `cwd, ref`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `checkout_ours` at `litehive/git/ops.py:707`
  Classification: utility.
  Args: `cwd, paths`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `restore_paths` at `litehive/git/ops.py:721`
  Classification: utility.
  Args: `cwd, paths, source, staged, worktree`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `stash_pop` at `litehive/git/ops.py:749`
  Classification: utility.
  Args: `cwd, ref, with_index`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `merge_no_edit` at `litehive/git/ops.py:770`
  Classification: utility.
  Args: `cwd, ref`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `merge_abort` at `litehive/git/ops.py:785`
  Classification: utility.
  Args: `cwd`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `unmerged_files` at `litehive/git/ops.py:798`
  Classification: utility.
  Args: `cwd`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `is_ancestor` at `litehive/git/ops.py:811`
  Classification: utility.
  Args: `root, ancestor_sha, descendant_sha`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `remove_worktree` at `litehive/git/ops.py:830`
  Classification: utility.
  Args: `root, path, force`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `rebase_worktree_onto` at `litehive/git/ops.py:849`
  Classification: utility.
  Args: `worktree, target_ref`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `default_commit_message` at `litehive/git/ops.py:883`
  Classification: utility.
  Args: `task_id, slug`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_clean_commit_text` at `litehive/git/ops.py:896`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_metadata_body` at `litehive/git/ops.py:914`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_acceptance_criteria_bullets` at `litehive/git/ops.py:937`
  Classification: utility.
  Args: `criteria`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `generated_completion_commit_message` at `litehive/git/ops.py:954`
  Classification: utility.
  Args: `task, detail`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_with_attempt_suffix` at `litehive/git/ops.py:971`
  Classification: utility.
  Args: `message, attempt`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_uses_generated_commit_message` at `litehive/git/ops.py:986`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `checkpoint_message` at `litehive/git/ops.py:1002`
  Classification: utility.
  Args: `task, attempt`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/lifecycle/engines.py`
- `ConfigBackedEngineSelector.__init__` at `litehive/lifecycle/engines.py:54`
  Classification: utility/protocol method.
  Args: `self, config, engine_factory, workspace, engine_override, model_override, check_quota`.
  Candidate owner: `ConfigBackedEngineSelector`.
  Note: Already a method; usually special/protocol behavior.
- `ConfigBackedEngineSelector._selection_task` at `litehive/lifecycle/engines.py:78`
  Classification: domain/service method.
  Args: `self, state, node_name`.
  Candidate owner: `ConfigBackedEngineSelector`.
  Note: Already on an object; review class responsibility before moving.
- `ConfigBackedEngineSelector.select` at `litehive/lifecycle/engines.py:101`
  Classification: domain/service method.
  Args: `self, state, node_name, excluded`.
  Candidate owner: `ConfigBackedEngineSelector`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/lifecycle/events.py`
- `Event.trigger_event_kind` at `litehive/lifecycle/events.py:31`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Event`.
  Note: Already on an object; review class responsibility before moving.
- `Event.failure_message` at `litehive/lifecycle/events.py:44`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Event`.
  Note: Already on an object; review class responsibility before moving.
- `Event.failure_source` at `litehive/lifecycle/events.py:58`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Event`.
  Note: Already on an object; review class responsibility before moving.
- `Event.failure_diagnostics` at `litehive/lifecycle/events.py:70`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Event`.
  Note: Already on an object; review class responsibility before moving.
- `Event.terminal_recovery_verdict` at `litehive/lifecycle/events.py:82`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Event`.
  Note: Already on an object; review class responsibility before moving.
- `Reject.trigger_event_kind` at `litehive/lifecycle/events.py:181`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Reject`.
  Note: Already on an object; review class responsibility before moving.
- `Reject.failure_message` at `litehive/lifecycle/events.py:196`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Reject`.
  Note: Already on an object; review class responsibility before moving.
- `Reject.failure_source` at `litehive/lifecycle/events.py:201`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Reject`.
  Note: Already on an object; review class responsibility before moving.
- `Reject.failure_diagnostics` at `litehive/lifecycle/events.py:206`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Reject`.
  Note: Already on an object; review class responsibility before moving.
- `Blocked.trigger_event_kind` at `litehive/lifecycle/events.py:243`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Blocked`.
  Note: Already on an object; review class responsibility before moving.
- `Blocked.failure_message` at `litehive/lifecycle/events.py:247`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Blocked`.
  Note: Already on an object; review class responsibility before moving.
- `Crash.trigger_event_kind` at `litehive/lifecycle/events.py:273`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Crash`.
  Note: Already on an object; review class responsibility before moving.
- `Crash.failure_message` at `litehive/lifecycle/events.py:277`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Crash`.
  Note: Already on an object; review class responsibility before moving.
- `Crash.failure_diagnostics` at `litehive/lifecycle/events.py:282`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Crash`.
  Note: Already on an object; review class responsibility before moving.
- `Crash.terminal_recovery_verdict` at `litehive/lifecycle/events.py:287`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Crash`.
  Note: Already on an object; review class responsibility before moving.
- `Timeout.trigger_event_kind` at `litehive/lifecycle/events.py:304`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Timeout`.
  Note: Already on an object; review class responsibility before moving.
- `Timeout.terminal_recovery_verdict` at `litehive/lifecycle/events.py:308`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Timeout`.
  Note: Already on an object; review class responsibility before moving.
- `StageRetryLimitHit.trigger_event_kind` at `litehive/lifecycle/events.py:329`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `StageRetryLimitHit`.
  Note: Already on an object; review class responsibility before moving.
- `StageRetryLimitHit.failure_message` at `litehive/lifecycle/events.py:333`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `StageRetryLimitHit`.
  Note: Already on an object; review class responsibility before moving.
- `StageRetryLimitHit.failure_diagnostics` at `litehive/lifecycle/events.py:338`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `StageRetryLimitHit`.
  Note: Already on an object; review class responsibility before moving.
- `OverallRetryLimitHit.trigger_event_kind` at `litehive/lifecycle/events.py:355`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `OverallRetryLimitHit`.
  Note: Already on an object; review class responsibility before moving.
- `OverallRetryLimitHit.failure_message` at `litehive/lifecycle/events.py:359`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `OverallRetryLimitHit`.
  Note: Already on an object; review class responsibility before moving.
- `TaskTimeBudgetExceeded.failure_message` at `litehive/lifecycle/events.py:379`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `TaskTimeBudgetExceeded`.
  Note: Already on an object; review class responsibility before moving.
- `RecoveryFailed.failure_message` at `litehive/lifecycle/events.py:416`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `RecoveryFailed`.
  Note: Already on an object; review class responsibility before moving.
- `RecoveryFailed.terminal_recovery_verdict` at `litehive/lifecycle/events.py:421`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `RecoveryFailed`.
  Note: Already on an object; review class responsibility before moving.
- `RecoveryBudgetHit.terminal_recovery_verdict` at `litehive/lifecycle/events.py:438`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `RecoveryBudgetHit`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/lifecycle/guards.py`
- `Guard.__call__` at `litehive/lifecycle/guards.py:23`
  Classification: utility/protocol method.
  Args: `self, state, event`.
  Candidate owner: `Guard`.
  Note: Already a method; usually special/protocol behavior.
- `Guard.__and__` at `litehive/lifecycle/guards.py:27`
  Classification: utility/protocol method.
  Args: `self, other`.
  Candidate owner: `Guard`.
  Note: Already a method; usually special/protocol behavior.
- `Guard.__and__.both` at `litehive/lifecycle/guards.py:39`
  Classification: domain/service method.
  Args: `state, event`.
  Candidate owner: `Guard`.
  Note: Already on an object; review class responsibility before moving.
- `Guard.__or__` at `litehive/lifecycle/guards.py:44`
  Classification: utility/protocol method.
  Args: `self, other`.
  Candidate owner: `Guard`.
  Note: Already a method; usually special/protocol behavior.
- `Guard.__or__.either` at `litehive/lifecycle/guards.py:54`
  Classification: domain/service method.
  Args: `state, event`.
  Candidate owner: `Guard`.
  Note: Already on an object; review class responsibility before moving.
- `Guard.__invert__` at `litehive/lifecycle/guards.py:59`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `Guard`.
  Note: Already a method; usually special/protocol behavior.
- `Guard.__invert__.negated` at `litehive/lifecycle/guards.py:69`
  Classification: domain/service method.
  Args: `state, event`.
  Candidate owner: `Guard`.
  Note: Already on an object; review class responsibility before moving.
- `mode` at `litehive/lifecycle/guards.py:75`
  Classification: domain/service candidate.
  Args: `m`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `mode.check` at `litehive/lifecycle/guards.py:88`
  Classification: utility.
  Args: `state, event`.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `stage_retries_remaining` at `litehive/lifecycle/guards.py:95`
  Classification: domain/service candidate.
  Args: `stage`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `stage_retries_remaining.check` at `litehive/lifecycle/guards.py:103`
  Classification: utility.
  Args: `state, event`.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `stage_retries_exhausted` at `litehive/lifecycle/guards.py:110`
  Classification: domain/service candidate.
  Args: `stage`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `stage_retries_exhausted.check` at `litehive/lifecycle/guards.py:119`
  Classification: utility.
  Args: `state, event`.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `last_hook_ok` at `litehive/lifecycle/guards.py:126`
  Classification: domain/service candidate.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `last_hook_ok.check` at `litehive/lifecycle/guards.py:135`
  Classification: utility.
  Args: `state, event`.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `hook_reject_loop_detected` at `litehive/lifecycle/guards.py:142`
  Classification: domain/service candidate.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `hook_reject_loop_detected.check` at `litehive/lifecycle/guards.py:151`
  Classification: utility.
  Args: `state, event`.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `rejection_loop_detected` at `litehive/lifecycle/guards.py:160`
  Classification: domain/service candidate.
  Args: `retry_target_stage`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `rejection_loop_detected.check` at `litehive/lifecycle/guards.py:171`
  Classification: utility.
  Args: `state, event`.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `zero_change_shortcut` at `litehive/lifecycle/guards.py:177`
  Classification: domain/service candidate.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `zero_change_shortcut.check` at `litehive/lifecycle/guards.py:187`
  Classification: utility.
  Args: `state, event`.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `pre_exec_budget_remaining` at `litehive/lifecycle/guards.py:194`
  Classification: domain/service candidate.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `pre_exec_budget_remaining.check` at `litehive/lifecycle/guards.py:204`
  Classification: utility.
  Args: `state, event`.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `recovery_budget_available` at `litehive/lifecycle/guards.py:211`
  Classification: domain/service candidate.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `recovery_budget_available.check` at `litehive/lifecycle/guards.py:221`
  Classification: utility.
  Args: `state, event`.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `recovery_budget_exhausted` at `litehive/lifecycle/guards.py:227`
  Classification: domain/service candidate.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `recovery_resume_is_concrete` at `litehive/lifecycle/guards.py:241`
  Classification: domain/service candidate.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `recovery_resume_is_concrete.check` at `litehive/lifecycle/guards.py:249`
  Classification: utility.
  Args: `state, event`.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `_always` at `litehive/lifecycle/guards.py:256`
  Classification: utility.
  Args: `state, event`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/lifecycle/heru_factory.py`
- `_NullSelector.select` at `litehive/lifecycle/heru_factory.py:88`
  Classification: domain/service method.
  Args: `self, state, node_name, excluded`.
  Candidate owner: `_NullSelector`.
  Note: Already on an object; review class responsibility before moving.
- `_NullSessions.get_or_create` at `litehive/lifecycle/heru_factory.py:110`
  Classification: domain/service method.
  Args: `self, task_id, node_name, engine_name`.
  Candidate owner: `_NullSessions`.
  Note: Already on an object; review class responsibility before moving.
- `_NullSessions.persist` at `litehive/lifecycle/heru_factory.py:122`
  Classification: domain/service method.
  Args: `self, task_id, node_name, engine_name, session`.
  Candidate owner: `_NullSessions`.
  Note: Already on an object; review class responsibility before moving.
- `_allowed_verdicts_for_stage` at `litehive/lifecycle/heru_factory.py:133`
  Classification: utility.
  Args: `stage`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_execution_checkout_path` at `litehive/lifecycle/heru_factory.py:142`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_recovery_execution_root` at `litehive/lifecycle/heru_factory.py:155`
  Classification: domain/service candidate.
  Args: `workspace, config`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_agent_execution_root` at `litehive/lifecycle/heru_factory.py:177`
  Classification: domain/service candidate.
  Args: `workspace, task, role, config`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `execution_checkout_status` at `litehive/lifecycle/heru_factory.py:186`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_display_path` at `litehive/lifecycle/heru_factory.py:200`
  Classification: domain/service candidate.
  Args: `workspace, path`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_rewrite_hallucinated_implementing_pass` at `litehive/lifecycle/heru_factory.py:212`
  Classification: domain/service candidate.
  Args: `workspace, task, latest, claimed_files, checkout`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_extract_test_results` at `litehive/lifecycle/heru_factory.py:312`
  Classification: utility.
  Args: `message`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `latest_verdict_after` at `litehive/lifecycle/heru_factory.py:335`
  Classification: domain/service candidate.
  Args: `workspace, task_id, stage, after_ts, source_subagent_id`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `HeruEngineAdapter.__init__` at `litehive/lifecycle/heru_factory.py:399`
  Classification: utility/protocol method.
  Args: `self, engine_name, workspace, config, model_name`.
  Candidate owner: `HeruEngineAdapter`.
  Note: Already a method; usually special/protocol behavior.
- `HeruEngineAdapter.with_model` at `litehive/lifecycle/heru_factory.py:419`
  Classification: domain/service method.
  Args: `self, model_name`.
  Candidate owner: `HeruEngineAdapter`.
  Note: Already on an object; review class responsibility before moving.
- `HeruEngineAdapter.run_turn` at `litehive/lifecycle/heru_factory.py:435`
  Classification: domain/service method.
  Args: `self, session, prompt, state`.
  Candidate owner: `HeruEngineAdapter`.
  Note: Already on an object; review class responsibility before moving.
- `HeruEngineAdapter._run_with_crash_resume` at `litehive/lifecycle/heru_factory.py:512`
  Classification: domain/service method.
  Args: `self, manager, task, role, prompt_text, session`.
  Candidate owner: `HeruEngineAdapter`.
  Note: Already on an object; review class responsibility before moving.
- `HeruEngineAdapter._crash_resume_prompt` at `litehive/lifecycle/heru_factory.py:563`
  Classification: domain/service method.
  Args: `cls, prompt_text`.
  Candidate owner: `HeruEngineAdapter`.
  Note: Already on an object; review class responsibility before moving.
- `HeruEngineAdapter._handle_startup_failure` at `litehive/lifecycle/heru_factory.py:575`
  Classification: domain/service method.
  Args: `self, state, task, role, startup_message, original_exc`.
  Candidate owner: `HeruEngineAdapter`.
  Note: Already on an object; review class responsibility before moving.
- `HeruEngineAdapter._attempt_direct_recovery_handoff` at `litehive/lifecycle/heru_factory.py:609`
  Classification: domain/service method.
  Args: `self, state, task, startup_message`.
  Candidate owner: `HeruEngineAdapter`.
  Note: Already on an object; review class responsibility before moving.
- `HeruEngineAdapter._direct_recovery_prompt` at `litehive/lifecycle/heru_factory.py:656`
  Classification: domain/service method.
  Args: `self, task, state, startup_message`.
  Candidate owner: `HeruEngineAdapter`.
  Note: Already on an object; review class responsibility before moving.
- `HeruEngineAdapter._direct_recovery_state` at `litehive/lifecycle/heru_factory.py:669`
  Classification: domain/service method.
  Args: `self, state, startup_message`.
  Candidate owner: `HeruEngineAdapter`.
  Note: Already on an object; review class responsibility before moving.
- `HeruEngineAdapter._direct_recovery_explanation` at `litehive/lifecycle/heru_factory.py:694`
  Classification: domain/service method.
  Args: `existing, startup_message`.
  Candidate owner: `HeruEngineAdapter`.
  Note: Already on an object; review class responsibility before moving.
- `HeruEngineAdapter._run_direct_recovery_turn` at `litehive/lifecycle/heru_factory.py:709`
  Classification: domain/service method.
  Args: `self, task_id, execution_root, prompt_text, source_subagent_id`.
  Candidate owner: `HeruEngineAdapter`.
  Note: Already on an object; review class responsibility before moving.
- `HeruEngineAdapter.extract_continuation_id` at `litehive/lifecycle/heru_factory.py:756`
  Classification: domain/service method.
  Args: `result, fallback`.
  Candidate owner: `HeruEngineAdapter`.
  Note: Already on an object; review class responsibility before moving.
- `HeruEngineAdapter._reraise` at `litehive/lifecycle/heru_factory.py:775`
  Classification: domain/service method.
  Args: `exc`.
  Candidate owner: `HeruEngineAdapter`.
  Note: Already on an object; review class responsibility before moving.
- `HeruEngineAdapter._reraise_failure` at `litehive/lifecycle/heru_factory.py:797`
  Classification: domain/service method.
  Args: `failure`.
  Candidate owner: `HeruEngineAdapter`.
  Note: Already on an object; review class responsibility before moving.
- `_is_retryable_failure` at `litehive/lifecycle/heru_factory.py:811`
  Classification: utility.
  Args: `exc`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `heru_engine_factory` at `litehive/lifecycle/heru_factory.py:819`
  Classification: domain/service candidate.
  Args: `workspace, config`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `heru_engine_factory._factory` at `litehive/lifecycle/heru_factory.py:822`
  Classification: utility.
  Args: `engine_name`.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.

### `litehive/lifecycle/hook_reports.py`
- `_normalize_hook_spec_data` at `litehive/lifecycle/hook_reports.py:29`
  Classification: utility.
  Args: `hook`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_build_hook_spec` at `litehive/lifecycle/hook_reports.py:44`
  Classification: utility.
  Args: `spec_data`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `hook_specs_from_config` at `litehive/lifecycle/hook_reports.py:69`
  Classification: domain/service candidate.
  Args: `config`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `_build_hook_specs_for_phase` at `litehive/lifecycle/hook_reports.py:86`
  Classification: utility.
  Args: `hooks`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_report_stage_for_phase` at `litehive/lifecycle/hook_reports.py:105`
  Classification: utility.
  Args: `phase`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_record_hook_warnings` at `litehive/lifecycle/hook_reports.py:127`
  Classification: domain/service candidate.
  Args: `workspace, task, phase, warnings`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_record_hook_reject` at `litehive/lifecycle/hook_reports.py:174`
  Classification: domain/service candidate.
  Args: `workspace, task, phase, reason, warnings, hook, consecutive_same_hook_rejects`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.

### `litehive/lifecycle/journal.py`
- `_event_payload` at `litehive/lifecycle/journal.py:27`
  Classification: utility.
  Args: `event`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_decode_transition_rows` at `litehive/lifecycle/journal.py:40`
  Classification: utility.
  Args: `rows`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_delta_payload` at `litehive/lifecycle/journal.py:67`
  Classification: utility.
  Args: `delta`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `PipelineJournal.__init__` at `litehive/lifecycle/journal.py:103`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `PipelineJournal`.
  Note: Already a method; usually special/protocol behavior.
- `PipelineJournal.task_started` at `litehive/lifecycle/journal.py:116`
  Classification: domain/service method.
  Args: `self, task_id, stage`.
  Candidate owner: `PipelineJournal`.
  Note: Already on an object; review class responsibility before moving.
- `PipelineJournal.transition` at `litehive/lifecycle/journal.py:126`
  Classification: domain/service method.
  Args: `self, task_id, from_stage, event, to_stage, rule_description, delta`.
  Candidate owner: `PipelineJournal`.
  Note: Already on an object; review class responsibility before moving.
- `PipelineJournal.stop_requested` at `litehive/lifecycle/journal.py:156`
  Classification: domain/service method.
  Args: `self, task_id, stage`.
  Candidate owner: `PipelineJournal`.
  Note: Already on an object; review class responsibility before moving.
- `PipelineJournal.task_finished` at `litehive/lifecycle/journal.py:166`
  Classification: domain/service method.
  Args: `self, task_id, stage`.
  Candidate owner: `PipelineJournal`.
  Note: Already on an object; review class responsibility before moving.
- `PipelineJournal._append` at `litehive/lifecycle/journal.py:178`
  Classification: domain/service method.
  Args: `self, kind, task_id, payload`.
  Candidate owner: `PipelineJournal`.
  Note: Already on an object; review class responsibility before moving.
- `PipelineJournal._log` at `litehive/lifecycle/journal.py:195`
  Classification: domain/service method.
  Args: `self, kind, task_id, payload`.
  Candidate owner: `PipelineJournal`.
  Note: Already on an object; review class responsibility before moving.
- `PipelineJournal._load_starting_seq` at `litehive/lifecycle/journal.py:219`
  Classification: domain/service method.
  Args: `self, task_id`.
  Candidate owner: `PipelineJournal`.
  Note: Already on an object; review class responsibility before moving.
- `PipelineJournal._store` at `litehive/lifecycle/journal.py:231`
  Classification: domain/service method.
  Args: `self, task_id, seq, created_at, kind, payload`.
  Candidate owner: `PipelineJournal`.
  Note: Already on an object; review class responsibility before moving.
- `SqliteJournal.__init__` at `litehive/lifecycle/journal.py:262`
  Classification: utility/protocol method.
  Args: `self, workspace`.
  Candidate owner: `SqliteJournal`.
  Note: Already a method; usually special/protocol behavior.
- `SqliteJournal._store` at `litehive/lifecycle/journal.py:274`
  Classification: domain/service method.
  Args: `self, task_id, seq, created_at, kind, payload`.
  Candidate owner: `SqliteJournal`.
  Note: Already on an object; review class responsibility before moving.
- `SqliteJournal._insert_transition` at `litehive/lifecycle/journal.py:294`
  Classification: domain/service method.
  Args: `self, task_id, seq, created_at, payload`.
  Candidate owner: `SqliteJournal`.
  Note: Already on an object; review class responsibility before moving.
- `SqliteJournal._insert_lifecycle` at `litehive/lifecycle/journal.py:350`
  Classification: domain/service method.
  Args: `self, task_id, seq, created_at, kind, payload`.
  Candidate owner: `SqliteJournal`.
  Note: Already on an object; review class responsibility before moving.
- `SqliteJournal._load_starting_seq` at `litehive/lifecycle/journal.py:390`
  Classification: domain/service method.
  Args: `self, task_id`.
  Candidate owner: `SqliteJournal`.
  Note: Already on an object; review class responsibility before moving.
- `SqliteJournal.load_transitions` at `litehive/lifecycle/journal.py:416`
  Classification: domain/service method.
  Args: `self, task_id`.
  Candidate owner: `SqliteJournal`.
  Note: Already on an object; review class responsibility before moving.
- `SqliteJournal.load_lifecycle` at `litehive/lifecycle/journal.py:438`
  Classification: domain/service method.
  Args: `self, task_id`.
  Candidate owner: `SqliteJournal`.
  Note: Already on an object; review class responsibility before moving.
- `NullJournal._store` at `litehive/lifecycle/journal.py:470`
  Classification: domain/service method.
  Args: `self, task_id, seq, created_at, kind, payload`.
  Candidate owner: `NullJournal`.
  Note: Already on an object; review class responsibility before moving.
- `NullJournal._log` at `litehive/lifecycle/journal.py:488`
  Classification: domain/service method.
  Args: `self, kind, task_id, payload`.
  Candidate owner: `NullJournal`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/lifecycle/launch_state.py`
- `_load_or_initialize` at `litehive/lifecycle/launch_state.py:23`
  Classification: utility.
  Args: `task_id, workspace, persistence`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_load_or_initialize._fresh_state` at `litehive/lifecycle/launch_state.py:49`
  Classification: utility.
  Args: `failed_run_history, recovery_history, recovery_budget_history_start`.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `_load_or_initialize._initialize_fresh_state` at `litehive/lifecycle/launch_state.py:85`
  Classification: utility.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `_entry_stage_for_task` at `litehive/lifecycle/launch_state.py:124`
  Classification: utility.
  Args: `task_record`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_launch_requires_fresh_pipeline_state` at `litehive/lifecycle/launch_state.py:147`
  Classification: utility.
  Args: `task_record`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_stale_launch_state_requires_reset` at `litehive/lifecycle/launch_state.py:163`
  Classification: utility.
  Args: `task_record, state, pipeline_mode, entry_stage`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/lifecycle/nodes/agent.py`
- `TransientError.__init__` at `litehive/lifecycle/nodes/agent.py:26`
  Classification: utility/protocol method.
  Args: `self, message, failure_kind`.
  Candidate owner: `TransientError`.
  Note: Already a method; usually special/protocol behavior.
- `Engine.run_turn` at `litehive/lifecycle/nodes/agent.py:94`
  Classification: domain/service method.
  Args: `self, session, prompt, state`.
  Candidate owner: `Engine`.
  Note: Already on an object; review class responsibility before moving.
- `EngineSelector.select` at `litehive/lifecycle/nodes/agent.py:115`
  Classification: domain/service method.
  Args: `self, state, node_name, excluded`.
  Candidate owner: `EngineSelector`.
  Note: Already on an object; review class responsibility before moving.
- `SessionProvider.get_or_create` at `litehive/lifecycle/nodes/agent.py:142`
  Classification: domain/service method.
  Args: `self, task_id, node_name, engine_name`.
  Candidate owner: `SessionProvider`.
  Note: Already on an object; review class responsibility before moving.
- `SessionProvider.persist` at `litehive/lifecycle/nodes/agent.py:146`
  Classification: domain/service method.
  Args: `self, task_id, node_name, engine_name, session`.
  Candidate owner: `SessionProvider`.
  Note: Already on an object; review class responsibility before moving.
- `AgentNode.__init__` at `litehive/lifecycle/nodes/agent.py:189`
  Classification: utility/protocol method.
  Args: `self, name, selector, session_provider, retry_budget, retry_on, retry_backoff_seconds, retry_backoff_multiplier, nudge_budget, sleep_fn, grace_period_seconds`.
  Candidate owner: `AgentNode`.
  Note: Already a method; usually special/protocol behavior.
- `AgentNode.build_prompt` at `litehive/lifecycle/nodes/agent.py:226`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `AgentNode`.
  Note: Already on an object; review class responsibility before moving.
- `AgentNode.build_nudge_prompt` at `litehive/lifecycle/nodes/agent.py:237`
  Classification: domain/service method.
  Args: `self, state, original_prompt`.
  Candidate owner: `AgentNode`.
  Note: Already on an object; review class responsibility before moving.
- `AgentNode.run` at `litehive/lifecycle/nodes/agent.py:270`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `AgentNode`.
  Note: Already on an object; review class responsibility before moving.
- `AgentNode._run_with_retries` at `litehive/lifecycle/nodes/agent.py:313`
  Classification: domain/service method.
  Args: `self, engine, session, prompt, state`.
  Candidate owner: `AgentNode`.
  Note: Already on an object; review class responsibility before moving.
- `AgentNode.verdict_to_event` at `litehive/lifecycle/nodes/agent.py:376`
  Classification: domain/service method.
  Args: `self, verdict`.
  Candidate owner: `AgentNode`.
  Note: Already on an object; review class responsibility before moving.
- `_metadata_classification` at `litehive/lifecycle/nodes/agent.py:408`
  Classification: utility.
  Args: `metadata`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/lifecycle/nodes/base.py`
- `Node.run` at `litehive/lifecycle/nodes/base.py:22`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `Node`.
  Note: Already on an object; review class responsibility before moving.
- `NodeRegistry.__init__` at `litehive/lifecycle/nodes/base.py:36`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `NodeRegistry`.
  Note: Already a method; usually special/protocol behavior.
- `NodeRegistry.register` at `litehive/lifecycle/nodes/base.py:47`
  Classification: domain/service method.
  Args: `self, node`.
  Candidate owner: `NodeRegistry`.
  Note: Already on an object; review class responsibility before moving.
- `NodeRegistry.get` at `litehive/lifecycle/nodes/base.py:58`
  Classification: domain/service method.
  Args: `self, name`.
  Candidate owner: `NodeRegistry`.
  Note: Already on an object; review class responsibility before moving.
- `NodeRegistry.names` at `litehive/lifecycle/nodes/base.py:69`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `NodeRegistry`.
  Note: Already on an object; review class responsibility before moving.
- `NodeRegistry.__contains__` at `litehive/lifecycle/nodes/base.py:73`
  Classification: utility/protocol method.
  Args: `self, name`.
  Candidate owner: `NodeRegistry`.
  Note: Already a method; usually special/protocol behavior.

### `litehive/lifecycle/nodes/hook.py`
- `HookRunner.run` at `litehive/lifecycle/nodes/hook.py:36`
  Classification: domain/service method.
  Args: `self, spec, state`.
  Candidate owner: `HookRunner`.
  Note: Already on an object; review class responsibility before moving.
- `_failed_process` at `litehive/lifecycle/nodes/hook.py:48`
  Classification: utility.
  Args: `command, code, stdout, stderr`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `SubprocessHookRunner.__init__` at `litehive/lifecycle/nodes/hook.py:68`
  Classification: utility/protocol method.
  Args: `self, workspace, execution_root_resolver, extra_env`.
  Candidate owner: `SubprocessHookRunner`.
  Note: Already a method; usually special/protocol behavior.
- `SubprocessHookRunner.run` at `litehive/lifecycle/nodes/hook.py:87`
  Classification: domain/service method.
  Args: `self, spec, state`.
  Candidate owner: `SubprocessHookRunner`.
  Note: Already on an object; review class responsibility before moving.
- `HookNode.__init__` at `litehive/lifecycle/nodes/hook.py:146`
  Classification: utility/protocol method.
  Args: `self, name, hooks, runner`.
  Candidate owner: `HookNode`.
  Note: Already a method; usually special/protocol behavior.
- `HookNode.run` at `litehive/lifecycle/nodes/hook.py:152`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `HookNode`.
  Note: Already on an object; review class responsibility before moving.
- `_reject` at `litehive/lifecycle/nodes/hook.py:161`
  Classification: utility.
  Args: `point, spec, result, state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/lifecycle/nodes/system.py`
- `MergeConflict.__init__` at `litehive/lifecycle/nodes/system.py:49`
  Classification: utility/protocol method.
  Args: `self, conflict_files`.
  Candidate owner: `MergeConflict`.
  Note: Already a method; usually special/protocol behavior.
- `SystemNode.__init__` at `litehive/lifecycle/nodes/system.py:72`
  Classification: utility/protocol method.
  Args: `self, name`.
  Candidate owner: `SystemNode`.
  Note: Already a method; usually special/protocol behavior.
- `SystemNode.run` at `litehive/lifecycle/nodes/system.py:77`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `SystemNode`.
  Note: Already on an object; review class responsibility before moving.
- `ReadyNode.__init__` at `litehive/lifecycle/nodes/system.py:103`
  Classification: utility/protocol method.
  Args: `self, probes`.
  Candidate owner: `ReadyNode`.
  Note: Already a method; usually special/protocol behavior.
- `ReadyNode.run` at `litehive/lifecycle/nodes/system.py:118`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `ReadyNode`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeSyncNode.__init__` at `litehive/lifecycle/nodes/system.py:168`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `WorktreeSyncNode`.
  Note: Already a method; usually special/protocol behavior.
- `WorktreeSyncNode.run` at `litehive/lifecycle/nodes/system.py:172`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `WorktreeSyncNode`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeSyncNode.sync` at `litehive/lifecycle/nodes/system.py:194`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `WorktreeSyncNode`.
  Note: Already on an object; review class responsibility before moving.
- `NoopWorktreeSyncNode.sync` at `litehive/lifecycle/nodes/system.py:210`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `NoopWorktreeSyncNode`.
  Note: Already on an object; review class responsibility before moving.
- `GitWorktreeSyncNode.__init__` at `litehive/lifecycle/nodes/system.py:229`
  Classification: utility/protocol method.
  Args: `self, workspace, worktree_resolver, main_ref`.
  Candidate owner: `GitWorktreeSyncNode`.
  Note: Already a method; usually special/protocol behavior.
- `GitWorktreeSyncNode.sync` at `litehive/lifecycle/nodes/system.py:249`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `GitWorktreeSyncNode`.
  Note: Already on an object; review class responsibility before moving.
- `PreExecRecoveryNode.__init__` at `litehive/lifecycle/nodes/system.py:290`
  Classification: utility/protocol method.
  Args: `self, repairs`.
  Candidate owner: `PreExecRecoveryNode`.
  Note: Already a method; usually special/protocol behavior.
- `PreExecRecoveryNode.run` at `litehive/lifecycle/nodes/system.py:305`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `PreExecRecoveryNode`.
  Note: Already on an object; review class responsibility before moving.
- `CommitNode.__init__` at `litehive/lifecycle/nodes/system.py:352`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `CommitNode`.
  Note: Already a method; usually special/protocol behavior.
- `CommitNode.run` at `litehive/lifecycle/nodes/system.py:356`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `CommitNode`.
  Note: Already on an object; review class responsibility before moving.
- `CommitNode._merge_worktree` at `litehive/lifecycle/nodes/system.py:374`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `CommitNode`.
  Note: Already on an object; review class responsibility before moving.
- `StubCommitNode._merge_worktree` at `litehive/lifecycle/nodes/system.py:392`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `StubCommitNode`.
  Note: Already on an object; review class responsibility before moving.
- `_is_runner_owned_metadata` at `litehive/lifecycle/nodes/system.py:402`
  Classification: utility.
  Args: `relpath, task_id`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_is_main_checkout_cleanup_excluded` at `litehive/lifecycle/nodes/system.py:422`
  Classification: utility.
  Args: `relpath`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_is_ignored_even_if_tracked` at `litehive/lifecycle/nodes/system.py:435`
  Classification: utility.
  Args: `repo_root, relpath`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_status_entry_needs_git_add` at `litehive/lifecycle/nodes/system.py:447`
  Classification: utility.
  Args: `code`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_is_untracked_embedded_git_repo` at `litehive/lifecycle/nodes/system.py:467`
  Classification: utility.
  Args: `repo_root, code, relpath`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `GitCommitNode.__init__` at `litehive/lifecycle/nodes/system.py:502`
  Classification: utility/protocol method.
  Args: `self, workspace, worktree_resolver, task_resolver`.
  Candidate owner: `GitCommitNode`.
  Note: Already a method; usually special/protocol behavior.
- `GitCommitNode._merge_worktree` at `litehive/lifecycle/nodes/system.py:521`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `GitCommitNode`.
  Note: Already on an object; review class responsibility before moving.
- `GitCommitNode.main_head` at `litehive/lifecycle/nodes/system.py:606`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `GitCommitNode`.
  Note: Already on an object; review class responsibility before moving.
- `GitCommitNode.autocommit_worktree_changes` at `litehive/lifecycle/nodes/system.py:610`
  Classification: domain/service method.
  Args: `self, worktree, state`.
  Candidate owner: `GitCommitNode`.
  Note: Already on an object; review class responsibility before moving.
- `GitCommitNode.autocommit_main_checkout_changes` at `litehive/lifecycle/nodes/system.py:647`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `GitCommitNode`.
  Note: Already on an object; review class responsibility before moving.
- `GitCommitNode._generated_commit_message` at `litehive/lifecycle/nodes/system.py:680`
  Classification: domain/service method.
  Args: `self, state, detail`.
  Candidate owner: `GitCommitNode`.
  Note: Already on an object; review class responsibility before moving.
- `GitCommitNode.git_status_entries` at `litehive/lifecycle/nodes/system.py:695`
  Classification: domain/service method.
  Args: `self, repo_root`.
  Candidate owner: `GitCommitNode`.
  Note: Already on an object; review class responsibility before moving.
- `GitCommitNode._filter_stageable_paths` at `litehive/lifecycle/nodes/system.py:699`
  Classification: domain/service method.
  Args: `self, repo_root, paths`.
  Candidate owner: `GitCommitNode`.
  Note: Already on an object; review class responsibility before moving.
- `GitCommitNode._git_status_entries_with_options` at `litehive/lifecycle/nodes/system.py:720`
  Classification: domain/service method.
  Args: `self, repo_root, include_ignored`.
  Candidate owner: `GitCommitNode`.
  Note: Already on an object; review class responsibility before moving.
- `GitCommitNode._worktree_local_only_paths` at `litehive/lifecycle/nodes/system.py:745`
  Classification: domain/service method.
  Args: `self, worktree`.
  Candidate owner: `GitCommitNode`.
  Note: Already on an object; review class responsibility before moving.
- `GitCommitNode._restore_local_only_paths` at `litehive/lifecycle/nodes/system.py:758`
  Classification: domain/service method.
  Args: `self, worktree, relpaths`.
  Candidate owner: `GitCommitNode`.
  Note: Already on an object; review class responsibility before moving.
- `GitCommitNode.worktree_head` at `litehive/lifecycle/nodes/system.py:771`
  Classification: domain/service method.
  Args: `self, worktree`.
  Candidate owner: `GitCommitNode`.
  Note: Already on an object; review class responsibility before moving.
- `GitCommitNode.worktree_branch` at `litehive/lifecycle/nodes/system.py:775`
  Classification: domain/service method.
  Args: `self, worktree`.
  Candidate owner: `GitCommitNode`.
  Note: Already on an object; review class responsibility before moving.
- `GitCommitNode._parse_dirty_checkout_files` at `litehive/lifecycle/nodes/system.py:780`
  Classification: domain/service method.
  Args: `stderr`.
  Candidate owner: `GitCommitNode`.
  Note: Already on an object; review class responsibility before moving.
- `GitCommitNode._merge_in_progress` at `litehive/lifecycle/nodes/system.py:809`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `GitCommitNode`.
  Note: Already on an object; review class responsibility before moving.
- `GitCommitNode._conclude_in_progress_merge` at `litehive/lifecycle/nodes/system.py:820`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `GitCommitNode`.
  Note: Already on an object; review class responsibility before moving.
- `GitCommitNode.worktree_patch_already_on_main` at `litehive/lifecycle/nodes/system.py:824`
  Classification: domain/service method.
  Args: `self, worktree_head, main_head`.
  Candidate owner: `GitCommitNode`.
  Note: Already on an object; review class responsibility before moving.
- `GitCommitNode._unresolved_conflicts` at `litehive/lifecycle/nodes/system.py:841`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `GitCommitNode`.
  Note: Already on an object; review class responsibility before moving.
- `GitCommitNode._abort_merge` at `litehive/lifecycle/nodes/system.py:845`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `GitCommitNode`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/lifecycle/nodes/terminal.py`
- `TerminalNode.__init__` at `litehive/lifecycle/nodes/terminal.py:17`
  Classification: utility/protocol method.
  Args: `self, name`.
  Candidate owner: `TerminalNode`.
  Note: Already a method; usually special/protocol behavior.
- `TerminalNode.run` at `litehive/lifecycle/nodes/terminal.py:21`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `TerminalNode`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/lifecycle/orchestration.py`
- `_sync_back_no_return` at `litehive/lifecycle/orchestration.py:75`
  Classification: utility.
  Args: `state, workspace`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `run_task_for_workspace` at `litehive/lifecycle/orchestration.py:97`
  Classification: domain/service candidate.
  Args: `workspace, config, task, engine_factory, engine_override, model_override`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_observe_transition` at `litehive/lifecycle/orchestration.py:208`
  Classification: domain/service candidate.
  Args: `workspace, state, from_stage, event, trans`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.

### `litehive/lifecycle/persistence.py`
- `LastReport.to_payload` at `litehive/lifecycle/persistence.py:31`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `LastReport`.
  Note: Already on an object; review class responsibility before moving.
- `LastReport.from_payload` at `litehive/lifecycle/persistence.py:42`
  Classification: domain/service method.
  Args: `cls, payload`.
  Candidate owner: `LastReport`.
  Note: Already on an object; review class responsibility before moving.
- `HookRejectFingerprint.to_payload` at `litehive/lifecycle/persistence.py:72`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `HookRejectFingerprint`.
  Note: Already on an object; review class responsibility before moving.
- `HookRejectFingerprint.from_payload` at `litehive/lifecycle/persistence.py:89`
  Classification: domain/service method.
  Args: `cls, payload`.
  Candidate owner: `HookRejectFingerprint`.
  Note: Already on an object; review class responsibility before moving.
- `LastRejection.to_payload` at `litehive/lifecycle/persistence.py:114`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `LastRejection`.
  Note: Already on an object; review class responsibility before moving.
- `LastRejection.from_payload` at `litehive/lifecycle/persistence.py:126`
  Classification: domain/service method.
  Args: `cls, payload`.
  Candidate owner: `LastRejection`.
  Note: Already on an object; review class responsibility before moving.
- `MergeContext.to_payload` at `litehive/lifecycle/persistence.py:141`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `MergeContext`.
  Note: Already on an object; review class responsibility before moving.
- `MergeContext.from_payload` at `litehive/lifecycle/persistence.py:155`
  Classification: domain/service method.
  Args: `cls, payload`.
  Candidate owner: `MergeContext`.
  Note: Already on an object; review class responsibility before moving.
- `CommitResult.to_payload` at `litehive/lifecycle/persistence.py:169`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `CommitResult`.
  Note: Already on an object; review class responsibility before moving.
- `CommitResult.from_payload` at `litehive/lifecycle/persistence.py:177`
  Classification: domain/service method.
  Args: `cls, payload`.
  Candidate owner: `CommitResult`.
  Note: Already on an object; review class responsibility before moving.
- `RejectionLoop.to_payload` at `litehive/lifecycle/persistence.py:191`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `RejectionLoop`.
  Note: Already on an object; review class responsibility before moving.
- `RejectionLoop.from_payload` at `litehive/lifecycle/persistence.py:207`
  Classification: domain/service method.
  Args: `cls, payload`.
  Candidate owner: `RejectionLoop`.
  Note: Already on an object; review class responsibility before moving.
- `FailedRunRecord.key` at `litehive/lifecycle/persistence.py:239`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `FailedRunRecord`.
  Note: Already on an object; review class responsibility before moving.
- `FailedRunRecord.to_payload` at `litehive/lifecycle/persistence.py:243`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `FailedRunRecord`.
  Note: Already on an object; review class responsibility before moving.
- `FailedRunRecord.from_payload` at `litehive/lifecycle/persistence.py:267`
  Classification: domain/service method.
  Args: `cls, payload`.
  Candidate owner: `FailedRunRecord`.
  Note: Already on an object; review class responsibility before moving.
- `failed_run_key` at `litehive/lifecycle/persistence.py:297`
  Classification: domain/service candidate.
  Args: `stage, failure_shape`.
  Candidate owner: `SqlitePersistence`.
  Note: Public module function in a domain/service package.
- `TaskState.__post_init__` at `litehive/lifecycle/persistence.py:384`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `TaskState`.
  Note: Already a method; usually special/protocol behavior.
- `TaskState.recovery_budget_available` at `litehive/lifecycle/persistence.py:403`
  Classification: domain/service method.
  Args: `self, trigger`.
  Candidate owner: `TaskState`.
  Note: Already on an object; review class responsibility before moving.
- `TaskState._budget_window_unconsumed_for` at `litehive/lifecycle/persistence.py:418`
  Classification: domain/service method.
  Args: `self, trigger`.
  Candidate owner: `TaskState`.
  Note: Already on an object; review class responsibility before moving.
- `TaskState._budget_recovery_history` at `litehive/lifecycle/persistence.py:433`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `TaskState`.
  Note: Already on an object; review class responsibility before moving.
- `Persistence.save` at `litehive/lifecycle/persistence.py:454`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `Persistence`.
  Note: Already on an object; review class responsibility before moving.
- `Persistence.load` at `litehive/lifecycle/persistence.py:458`
  Classification: domain/service method.
  Args: `self, task_id`.
  Candidate owner: `Persistence`.
  Note: Already on an object; review class responsibility before moving.
- `_string_list` at `litehive/lifecycle/persistence.py:463`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_state_payload` at `litehive/lifecycle/persistence.py:487`
  Classification: utility.
  Args: `state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_decode_stage_retry_map` at `litehive/lifecycle/persistence.py:544`
  Classification: utility.
  Args: `raw`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_decode_recovery_history` at `litehive/lifecycle/persistence.py:560`
  Classification: utility.
  Args: `raw`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_decode_last_rejection_by_stage` at `litehive/lifecycle/persistence.py:577`
  Classification: utility.
  Args: `raw`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_decode_failed_run_history` at `litehive/lifecycle/persistence.py:593`
  Classification: utility.
  Args: `raw`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_state_from_row` at `litehive/lifecycle/persistence.py:611`
  Classification: utility.
  Args: `task_id, stage, pipeline_mode, payload, limits`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `SqlitePersistence.__init__` at `litehive/lifecycle/persistence.py:713`
  Classification: utility/protocol method.
  Args: `self, workspace, limits`.
  Candidate owner: `SqlitePersistence`.
  Note: Already a method; usually special/protocol behavior.
- `SqlitePersistence.save` at `litehive/lifecycle/persistence.py:725`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `SqlitePersistence`.
  Note: Already on an object; review class responsibility before moving.
- `SqlitePersistence.load` at `litehive/lifecycle/persistence.py:772`
  Classification: domain/service method.
  Args: `self, task_id`.
  Candidate owner: `SqlitePersistence`.
  Note: Already on an object; review class responsibility before moving.
- `SqlitePersistence.reset_current_lifecycle_state` at `litehive/lifecycle/persistence.py:796`
  Classification: domain/service method.
  Args: `self, task_id, preserve_run_memory`.
  Candidate owner: `SqlitePersistence`.
  Note: Already on an object; review class responsibility before moving.
- `SqlitePersistence.reset_all` at `litehive/lifecycle/persistence.py:861`
  Classification: domain/service method.
  Args: `self, task_id`.
  Candidate owner: `SqlitePersistence`.
  Note: Already on an object; review class responsibility before moving.
- `SqlitePersistence.initialize` at `litehive/lifecycle/persistence.py:874`
  Classification: domain/service method.
  Args: `self, task_id, pipeline_mode, stage, entry_stage`.
  Candidate owner: `SqlitePersistence`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/lifecycle/prompt_sections.py`
- `_bullet_block` at `litehive/lifecycle/prompt_sections.py:22`
  Classification: utility.
  Args: `items`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_role_identity_sentence` at `litehive/lifecycle/prompt_sections.py:47`
  Classification: utility.
  Args: `role`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_header_section` at `litehive/lifecycle/prompt_sections.py:61`
  Classification: utility.
  Args: `prompt, task_record`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_instructions_section` at `litehive/lifecycle/prompt_sections.py:89`
  Classification: utility.
  Args: `prompt`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_goal_section` at `litehive/lifecycle/prompt_sections.py:111`
  Classification: utility.
  Args: `task_record`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_acceptance_criteria_section` at `litehive/lifecycle/prompt_sections.py:118`
  Classification: utility.
  Args: `task_record`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_plan_section` at `litehive/lifecycle/prompt_sections.py:126`
  Classification: utility.
  Args: `task_record`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_constraints_section` at `litehive/lifecycle/prompt_sections.py:134`
  Classification: utility.
  Args: `task_record`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_last_rejection_section` at `litehive/lifecycle/prompt_sections.py:148`
  Classification: utility.
  Args: `rejection`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_prior_work_section` at `litehive/lifecycle/prompt_sections.py:162`
  Classification: utility.
  Args: `last_report, last_rejection`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_recovery_trigger_section` at `litehive/lifecycle/prompt_sections.py:189`
  Classification: utility.
  Args: `recovery_trigger, prompt`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_recovery_execution_root_section` at `litehive/lifecycle/prompt_sections.py:207`
  Classification: utility.
  Args: `prompt`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_failed_subagent_diagnostics_section` at `litehive/lifecycle/prompt_sections.py:231`
  Classification: utility.
  Args: `diagnostics`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_compact_failure_signal` at `litehive/lifecycle/prompt_sections.py:282`
  Classification: utility.
  Args: `*texts`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_single_line` at `litehive/lifecycle/prompt_sections.py:301`
  Classification: utility.
  Args: `value, limit`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_recovery_history_section` at `litehive/lifecycle/prompt_sections.py:309`
  Classification: utility.
  Args: `recovery_history`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_failed_run_history_section` at `litehive/lifecycle/prompt_sections.py:340`
  Classification: utility.
  Args: `failed_run_history`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_repeated_recovery_fingerprint_section` at `litehive/lifecycle/prompt_sections.py:374`
  Classification: utility.
  Args: `repeated_recovery_fingerprint`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_scope_analysis_section` at `litehive/lifecycle/prompt_sections.py:396`
  Classification: utility.
  Args: `scope_analysis`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_test_failure_attribution_section` at `litehive/lifecycle/prompt_sections.py:435`
  Classification: utility.
  Args: `attribution`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_merge_conflict_section` at `litehive/lifecycle/prompt_sections.py:462`
  Classification: utility.
  Args: `conflict_files, merge_attempt`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_nudge_section` at `litehive/lifecycle/prompt_sections.py:479`
  Classification: utility.
  Args: `prompt`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_string_list` at `litehive/lifecycle/prompt_sections.py:500`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_compact_list` at `litehive/lifecycle/prompt_sections.py:521`
  Classification: utility.
  Args: `items, limit, separator`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_runner_hooks_section` at `litehive/lifecycle/prompt_sections.py:529`
  Classification: utility.
  Args: `stage, hooks`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_verdict_instructions_section` at `litehive/lifecycle/prompt_sections.py:554`
  Classification: utility.
  Args: `prompt`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_label_to_heading` at `litehive/lifecycle/prompt_sections.py:583`
  Classification: utility.
  Args: `label`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/lifecycle/prompt_serializer.py`
- `serialize_prompt` at `litehive/lifecycle/prompt_serializer.py:59`
  Classification: domain/service candidate.
  Args: `prompt, task_record, workspace`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `_load_task_activity_history` at `litehive/lifecycle/prompt_serializer.py:146`
  Classification: domain/service candidate.
  Args: `workspace, task_record`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_cap_message` at `litehive/lifecycle/prompt_serializer.py:182`
  Classification: utility.
  Args: `entry`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_entry_sources` at `litehive/lifecycle/prompt_serializer.py:198`
  Classification: utility.
  Args: `entry`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_matches_last_rejection` at `litehive/lifecycle/prompt_serializer.py:224`
  Classification: utility.
  Args: `entry, last_rejection`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_trim_activity_for_prompt` at `litehive/lifecycle/prompt_serializer.py:252`
  Classification: utility.
  Args: `activity, current_stage, last_rejection`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_trim_activity_for_prompt._last_where` at `litehive/lifecycle/prompt_serializer.py:282`
  Classification: utility.
  Args: `**match`.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `_activity_section` at `litehive/lifecycle/prompt_serializer.py:355`
  Classification: utility.
  Args: `activity, current_stage, last_rejection`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/lifecycle/registry.py`
- `_phase_hook_node` at `litehive/lifecycle/registry.py:53`
  Classification: utility.
  Args: `name, hooks, runner`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `build_registry` at `litehive/lifecycle/registry.py:65`
  Classification: boundary utility.
  Args: `selector, session_store, hook_runner, commit_node, prompt_context, worktree_sync_node, ready_node, pre_exec_recovery_node, hook_specs, retry_budget, retry_on`.
  Candidate owner: `DI container factory`.
  Note: Assembly function; free function is acceptable at the container boundary.

### `litehive/lifecycle/rules.py`
- `_recovery_rules` at `litehive/lifecycle/rules.py:55`
  Classification: utility.
  Args: `from_state, on_event, when`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_terminal_reject_rules` at `litehive/lifecycle/rules.py:81`
  Classification: utility.
  Args: `from_state, when, reason`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_epoch_terminal_reject_rules` at `litehive/lifecycle/rules.py:94`
  Classification: utility.
  Args: `epoch`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/lifecycle/runner.py`
- `_normalize_report_string_list` at `litehive/lifecycle/runner.py:30`
  Classification: utility.
  Args: `items`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `StateMachineRunner.__init__` at `litehive/lifecycle/runner.py:64`
  Classification: utility/protocol method.
  Args: `self, registry, persistence, rules, journal, session_store, stop_requested, state_sync, transition_observer, task_time_budget_seconds, clock`.
  Candidate owner: `StateMachineRunner`.
  Note: Already a method; usually special/protocol behavior.
- `StateMachineRunner.run_task` at `litehive/lifecycle/runner.py:97`
  Classification: domain/service method.
  Args: `self, task_id`.
  Candidate owner: `StateMachineRunner`.
  Note: Already on an object; review class responsibility before moving.
- `StateMachineRunner._task_time_budget_event` at `litehive/lifecycle/runner.py:129`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `StateMachineRunner`.
  Note: Already on an object; review class responsibility before moving.
- `StateMachineRunner._apply_transition` at `litehive/lifecycle/runner.py:154`
  Classification: domain/service method.
  Args: `self, state, from_stage, event, task_id`.
  Candidate owner: `StateMachineRunner`.
  Note: Already on an object; review class responsibility before moving.
- `StateMachineRunner.apply_event_side_effects` at `litehive/lifecycle/runner.py:196`
  Classification: domain/service method.
  Args: `state, event`.
  Candidate owner: `StateMachineRunner`.
  Note: Already on an object; review class responsibility before moving.
- `StateMachineRunner._reset_hook_reject_tracking_on_progress` at `litehive/lifecycle/runner.py:240`
  Classification: domain/service method.
  Args: `state, from_stage, to_stage, event`.
  Candidate owner: `StateMachineRunner`.
  Note: Already on an object; review class responsibility before moving.
- `StateMachineRunner._clear_hook_reject_tracking` at `litehive/lifecycle/runner.py:263`
  Classification: domain/service method.
  Args: `state, clear_recovery_invoked`.
  Candidate owner: `StateMachineRunner`.
  Note: Already on an object; review class responsibility before moving.
- `StateMachineRunner._apply_delta` at `litehive/lifecycle/runner.py:282`
  Classification: domain/service method.
  Args: `state, delta`.
  Candidate owner: `StateMachineRunner`.
  Note: Already on an object; review class responsibility before moving.
- `StateMachineRunner._record_failed_run` at `litehive/lifecycle/runner.py:336`
  Classification: domain/service method.
  Args: `state, record`.
  Candidate owner: `StateMachineRunner`.
  Note: Already on an object; review class responsibility before moving.
- `StateMachineRunner._reset_cross_agent_retry_sessions` at `litehive/lifecycle/runner.py:367`
  Classification: domain/service method.
  Args: `self, task_id, from_stage, to_stage, event`.
  Candidate owner: `StateMachineRunner`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/lifecycle/runtime_sync.py`
- `_runtime_hook_reject_fingerprint` at `litehive/lifecycle/runtime_sync.py:43`
  Classification: utility.
  Args: `state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_runtime_recovery_outcome` at `litehive/lifecycle/runtime_sync.py:63`
  Classification: utility.
  Args: `outcome`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_runtime_recovery_key` at `litehive/lifecycle/runtime_sync.py:88`
  Classification: utility.
  Args: `outcome`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_runtime_recovery_history_projection` at `litehive/lifecycle/runtime_sync.py:106`
  Classification: utility.
  Args: `current_state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_runtime_failed_run_record` at `litehive/lifecycle/runtime_sync.py:119`
  Classification: utility.
  Args: `record`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_runtime_failed_run_history_projection` at `litehive/lifecycle/runtime_sync.py:144`
  Classification: utility.
  Args: `current_state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_sync_runtime_fields` at `litehive/lifecycle/runtime_sync.py:149`
  Classification: utility.
  Args: `task_record, state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_latest_recovery_trigger` at `litehive/lifecycle/runtime_sync.py:193`
  Classification: utility.
  Args: `state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_recovery_origin_stage` at `litehive/lifecycle/runtime_sync.py:210`
  Classification: utility.
  Args: `origin_stage`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_sync_terminal_status` at `litehive/lifecycle/runtime_sync.py:230`
  Classification: utility.
  Args: `task_record, state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_sync_back` at `litehive/lifecycle/runtime_sync.py:307`
  Classification: utility.
  Args: `state, workspace`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_sync_recovery_follow_up` at `litehive/lifecycle/runtime_sync.py:363`
  Classification: domain/service candidate.
  Args: `workspace, task_record, state`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_clear_terminal_task_from_workspace_state` at `litehive/lifecycle/runtime_sync.py:418`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.

### `litehive/lifecycle/sessions.py`
- `EngineSessionContinuation.resume_session_id` at `litehive/lifecycle/sessions.py:32`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `EngineSessionContinuation`.
  Note: Already on an object; review class responsibility before moving.
- `Session.continuation_state` at `litehive/lifecycle/sessions.py:51`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Session`.
  Note: Already on an object; review class responsibility before moving.
- `Session.resume_session_id` at `litehive/lifecycle/sessions.py:61`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Session`.
  Note: Already on an object; review class responsibility before moving.
- `Session.capture_engine_session_id` at `litehive/lifecycle/sessions.py:67`
  Classification: domain/service method.
  Args: `self, resume_id`.
  Candidate owner: `Session`.
  Note: Already on an object; review class responsibility before moving.
- `Session.resumable` at `litehive/lifecycle/sessions.py:73`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Session`.
  Note: Already on an object; review class responsibility before moving.
- `SessionStore.get_or_create` at `litehive/lifecycle/sessions.py:94`
  Classification: domain/service method.
  Args: `self, task_id, node_name, engine_name`.
  Candidate owner: `SessionStore`.
  Note: Already on an object; review class responsibility before moving.
- `SessionStore.persist` at `litehive/lifecycle/sessions.py:106`
  Classification: domain/service method.
  Args: `self, task_id, node_name, engine_name, session`.
  Candidate owner: `SessionStore`.
  Note: Already on an object; review class responsibility before moving.
- `SessionStore.clear_node_sessions` at `litehive/lifecycle/sessions.py:118`
  Classification: domain/service method.
  Args: `self, task_id, node_name`.
  Candidate owner: `SessionStore`.
  Note: Already on an object; review class responsibility before moving.
- `SqliteSessionStore.__init__` at `litehive/lifecycle/sessions.py:140`
  Classification: utility/protocol method.
  Args: `self, workspace`.
  Candidate owner: `SqliteSessionStore`.
  Note: Already a method; usually special/protocol behavior.
- `SqliteSessionStore.get_or_create` at `litehive/lifecycle/sessions.py:151`
  Classification: domain/service method.
  Args: `self, task_id, node_name, engine_name`.
  Candidate owner: `SqliteSessionStore`.
  Note: Already on an object; review class responsibility before moving.
- `SqliteSessionStore.persist` at `litehive/lifecycle/sessions.py:177`
  Classification: domain/service method.
  Args: `self, task_id, node_name, engine_name, session`.
  Candidate owner: `SqliteSessionStore`.
  Note: Already on an object; review class responsibility before moving.
- `SqliteSessionStore.clear_node_sessions` at `litehive/lifecycle/sessions.py:211`
  Classification: domain/service method.
  Args: `self, task_id, node_name`.
  Candidate owner: `SqliteSessionStore`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/lifecycle/stages.py`
- `Stage.__post_init__` at `litehive/lifecycle/stages.py:26`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `Stage`.
  Note: Already a method; usually special/protocol behavior.
- `Stage.__eq__` at `litehive/lifecycle/stages.py:37`
  Classification: utility/protocol method.
  Args: `self, other`.
  Candidate owner: `Stage`.
  Note: Already a method; usually special/protocol behavior.
- `Stage.__hash__` at `litehive/lifecycle/stages.py:52`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `Stage`.
  Note: Already a method; usually special/protocol behavior.
- `Stage.__lt__` at `litehive/lifecycle/stages.py:56`
  Classification: utility/protocol method.
  Args: `self, other`.
  Candidate owner: `Stage`.
  Note: Already a method; usually special/protocol behavior.
- `Stage.__repr__` at `litehive/lifecycle/stages.py:64`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `Stage`.
  Note: Already a method; usually special/protocol behavior.

### `litehive/lifecycle/transitions.py`
- `_entry_phase` at `litehive/lifecycle/transitions.py:38`
  Classification: utility.
  Args: `stage`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `NoTransitionError.__init__` at `litehive/lifecycle/transitions.py:73`
  Classification: utility/protocol method.
  Args: `self, current, event`.
  Candidate owner: `NoTransitionError`.
  Note: Already a method; usually special/protocol behavior.
- `_matches_from` at `litehive/lifecycle/transitions.py:85`
  Classification: utility.
  Args: `pattern, current`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_matches_event` at `litehive/lifecycle/transitions.py:99`
  Classification: utility.
  Args: `pattern, event`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_resolve_to` at `litehive/lifecycle/transitions.py:109`
  Classification: utility.
  Args: `to, state, event`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `evaluate` at `litehive/lifecycle/transitions.py:123`
  Classification: domain/service candidate.
  Args: `rules, current, event, state`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `resume_from_origin` at `litehive/lifecycle/transitions.py:152`
  Classification: domain/service candidate.
  Args: `state, event`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `resume_from_pre_exec` at `litehive/lifecycle/transitions.py:178`
  Classification: domain/service candidate.
  Args: `state, event`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `entry_from_worktree_sync` at `litehive/lifecycle/transitions.py:196`
  Classification: domain/service candidate.
  Args: `state, event`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `retry_epoch_rules` at `litehive/lifecycle/transitions.py:218`
  Classification: domain/service candidate.
  Args: `counter_stage, phases, retry_target, exhausted_reason`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `list_transitions` at `litehive/lifecycle/transitions.py:280`
  Classification: domain/service candidate.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.

### `litehive/lifecycle/types.py`
- `before` at `litehive/lifecycle/types.py:28`
  Classification: domain/service candidate.
  Args: `stage`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `after` at `litehive/lifecycle/types.py:40`
  Classification: domain/service candidate.
  Args: `stage`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `pipeline_stage_for_phase` at `litehive/lifecycle/types.py:69`
  Classification: domain/service candidate.
  Args: `phase`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.

### `litehive/lifecycle/worktree_setup.py`
- `_resolve_worktree_for_workspace` at `litehive/lifecycle/worktree_setup.py:29`
  Classification: domain/service candidate.
  Args: `workspace, state`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_resolve_hook_execution_root_for_workspace` at `litehive/lifecycle/worktree_setup.py:35`
  Classification: domain/service candidate.
  Args: `workspace, state`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_task_recorded_worktree_for_workspace` at `litehive/lifecycle/worktree_setup.py:49`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `build_commit_node_for_workspace` at `litehive/lifecycle/worktree_setup.py:67`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_build_worktree_sync_node` at `litehive/lifecycle/worktree_setup.py:76`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_worktree_missing_probe` at `litehive/lifecycle/worktree_setup.py:84`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_worktree_missing_probe._probe` at `litehive/lifecycle/worktree_setup.py:96`
  Classification: utility.
  Args: `state`.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `_worktree_metadata_repair` at `litehive/lifecycle/worktree_setup.py:102`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_worktree_metadata_repair._repair` at `litehive/lifecycle/worktree_setup.py:113`
  Classification: utility.
  Args: `state`.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `_mark_task_interrupted_on_crash` at `litehive/lifecycle/worktree_setup.py:119`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_cleanup_terminal_worktree` at `litehive/lifecycle/worktree_setup.py:141`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `reconcile_terminal_commit_sha_for_workspace` at `litehive/lifecycle/worktree_setup.py:153`
  Classification: domain/service candidate.
  Args: `workspace, task, final_state, persistence`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/main.py`
- `_workspace_override_from_argv` at `litehive/main.py:23`
  Classification: utility.
  Args: `argv`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `dispatch_status` at `litehive/main.py:41`
  Classification: boundary utility.
  Args: `argv`.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.
- `main` at `litehive/main.py:83`
  Classification: boundary utility.
  Candidate owner: `CLI command object only if command body grows`.
  Note: CLI/process boundary; can stay free if it only dispatches.

### `litehive/observability/engine_monitoring.py`
- `load_engine_monitoring` at `litehive/observability/engine_monitoring.py:28`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `save_engine_monitoring` at `litehive/observability/engine_monitoring.py:41`
  Classification: domain/service candidate.
  Args: `workspace, monitoring`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_load_engine_monitoring_from_db` at `litehive/observability/engine_monitoring.py:55`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_save_engine_monitoring_to_db` at `litehive/observability/engine_monitoring.py:88`
  Classification: domain/service candidate.
  Args: `workspace, monitoring`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `record_engine_execution` at `litehive/observability/engine_monitoring.py:115`
  Classification: domain/service candidate.
  Args: `workspace, task_id, engine_name, adapter, execution, failure_kind, failure_reason`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_extract_usage_observation` at `litehive/observability/engine_monitoring.py:150`
  Classification: utility.
  Args: `adapter, execution`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `record_engine_observation` at `litehive/observability/engine_monitoring.py:164`
  Classification: domain/service candidate.
  Args: `workspace, task_id, engine_name, adapter, execution`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_apply_engine_observation` at `litehive/observability/engine_monitoring.py:203`
  Classification: utility.
  Args: `monitoring, engine_name, task_id, execution, observation, count_invocation, failure_kind, failure_reason`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_limit_kind` at `litehive/observability/engine_monitoring.py:268`
  Classification: utility.
  Args: `reason`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/observability/events.py`
- `PersistedTaskEvent.kind` at `litehive/observability/events.py:30`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `PersistedTaskEvent`.
  Note: Already on an object; review class responsibility before moving.
- `PersistedTaskEvent.data` at `litehive/observability/events.py:32`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `PersistedTaskEvent`.
  Note: Already on an object; review class responsibility before moving.
- `append_event` at `litehive/observability/events.py:35`
  Classification: domain/service candidate.
  Args: `workspace, task, event`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `read_events` at `litehive/observability/events.py:69`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `last_event_timestamp` at `litehive/observability/events.py:100`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `append_session_log` at `litehive/observability/events.py:126`
  Classification: domain/service candidate.
  Args: `base, name, content`.
  Candidate owner: `event log or repository object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `ensure_session_log` at `litehive/observability/events.py:152`
  Classification: domain/service candidate.
  Args: `base, name`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.

### `litehive/observability/status.py`
- `collect_task_pipeline_status_for_workspace` at `litehive/observability/status.py:121`
  Classification: domain/service candidate.
  Args: `workspace, read_only, diagnostics`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `render_task_pipeline_status_lines` at `litehive/observability/status.py:170`
  Classification: domain/service candidate.
  Args: `status, workspace, mode, retry_on_label`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `_first_or_none` at `litehive/observability/status.py:232`
  Classification: utility.
  Args: `items`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_runner_state_label_for_workspace` at `litehive/observability/status.py:246`
  Classification: domain/service candidate.
  Args: `workspace, runner`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_load_task_read_only_for_workspace` at `litehive/observability/status.py:266`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_operational_attention_lines` at `litehive/observability/status.py:304`
  Classification: utility.
  Args: `lines`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `render_active_task_detail_lines` at `litehive/observability/status.py:324`
  Classification: domain/service candidate.
  Args: `task, default_engine`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `render_runner_status_line` at `litehive/observability/status.py:346`
  Classification: domain/service candidate.
  Args: `runner, state`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `render_detailed_status_header_lines` at `litehive/observability/status.py:366`
  Classification: domain/service candidate.
  Args: `workspace, config, state, runner`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `render_runtime_policy_lines` at `litehive/observability/status.py:405`
  Classification: domain/service candidate.
  Args: `config, retry_on_label`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `render_engine_availability_lines` at `litehive/observability/status.py:428`
  Classification: domain/service candidate.
  Args: `config, monitoring`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.

### `litehive/observability/status_dashboard.py`
- `render_active_task_section` at `litehive/observability/status_dashboard.py:26`
  Classification: domain/service candidate.
  Args: `task, default_engine`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `render_active_tasks_section` at `litehive/observability/status_dashboard.py:60`
  Classification: domain/service candidate.
  Args: `tasks, default_engine`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `find_last_completed_task` at `litehive/observability/status_dashboard.py:91`
  Classification: domain/service candidate.
  Args: `tasks`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `render_last_completed_section` at `litehive/observability/status_dashboard.py:107`
  Classification: domain/service candidate.
  Args: `task, workspace`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `render_queue_section` at `litehive/observability/status_dashboard.py:127`
  Classification: domain/service candidate.
  Args: `queue, tasks`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `collect_recent_activity` at `litehive/observability/status_dashboard.py:154`
  Classification: domain/service candidate.
  Args: `workspace, limit`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `render_recent_activity_section` at `litehive/observability/status_dashboard.py:204`
  Classification: domain/service candidate.
  Args: `events`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.

### `litehive/observability/status_diagnostics.py`
- `collect_status_snapshot_for_workspace` at `litehive/observability/status_diagnostics.py:63`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `collect_operational_status_snapshot_for_workspace` at `litehive/observability/status_diagnostics.py:101`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/observability/status_health.py`
- `render_health_active_task_lines` at `litehive/observability/status_health.py:23`
  Classification: domain/service candidate.
  Args: `task`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `render_health_flagged_task_lines` at `litehive/observability/status_health.py:43`
  Classification: domain/service candidate.
  Args: `flagged_tasks, workspace`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `render_health_worktree_lines` at `litehive/observability/status_health.py:68`
  Classification: domain/service candidate.
  Args: `worktrees`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `render_health_worktree_finding_lines` at `litehive/observability/status_health.py:95`
  Classification: domain/service candidate.
  Args: `report`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `render_health_quota_lines` at `litehive/observability/status_health.py:124`
  Classification: domain/service candidate.
  Args: `quota_health`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `render_health_daemon_lines` at `litehive/observability/status_health.py:140`
  Classification: domain/service candidate.
  Args: `daemon_status, daemon_pid`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `render_health_recent_completion_lines` at `litehive/observability/status_health.py:157`
  Classification: domain/service candidate.
  Args: `completed, workspace`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.

### `litehive/observability/status_io.py`
- `_safe_yaml_mapping` at `litehive/observability/status_io.py:22`
  Classification: utility.
  Args: `path, key, remediation`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_safe_yaml_document` at `litehive/observability/status_io.py:49`
  Classification: utility.
  Args: `path, key, remediation`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_safe_json_mapping` at `litehive/observability/status_io.py:82`
  Classification: utility.
  Args: `path, key, remediation`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_heartbeat_age_seconds` at `litehive/observability/status_io.py:127`
  Classification: utility.
  Args: `heartbeat_at`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_validation_error_label` at `litehive/observability/status_io.py:145`
  Classification: utility.
  Args: `exc`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_yaml_location_label` at `litehive/observability/status_io.py:167`
  Classification: utility.
  Args: `exc`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/observability/status_loaders.py`
- `_load_config_for_status_for_workspace` at `litehive/observability/status_loaders.py:36`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_validate_status_config_data` at `litehive/observability/status_loaders.py:77`
  Classification: utility.
  Args: `data`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_best_effort_status_config` at `litehive/observability/status_loaders.py:89`
  Classification: utility.
  Args: `data`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_config_error_key` at `litehive/observability/status_loaders.py:113`
  Classification: utility.
  Args: `exc`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_load_state_for_status` at `litehive/observability/status_loaders.py:143`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_load_engine_monitoring_for_status` at `litehive/observability/status_loaders.py:191`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_load_runner_status_for_status_for_workspace` at `litehive/observability/status_loaders.py:217`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.

### `litehive/observability/status_probes.py`
- `_probe_runner_state_for_workspace` at `litehive/observability/status_probes.py:41`
  Classification: domain/service candidate.
  Args: `workspace, state, runner`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_probe_daemon_status_for_workspace` at `litehive/observability/status_probes.py:91`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_probe_last_cycle_for_workspace` at `litehive/observability/status_probes.py:129`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_probe_heru_link_for_workspace` at `litehive/observability/status_probes.py:167`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_probe_origin_divergence_for_workspace` at `litehive/observability/status_probes.py:209`
  Classification: domain/service candidate.
  Args: `workspace, state`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_probe_pool_stop_reason` at `litehive/observability/status_probes.py:235`
  Classification: utility.
  Args: `state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_probe_task_index_references_for_workspace` at `litehive/observability/status_probes.py:261`
  Classification: domain/service candidate.
  Args: `workspace, state, state_issues`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_probe_task_status_damage` at `litehive/observability/status_probes.py:318`
  Classification: domain/service candidate.
  Args: `workspace, state, runner, state_issues`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_live_active_pipeline_stage` at `litehive/observability/status_probes.py:371`
  Classification: utility.
  Args: `active_task_id, tasks`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_recovery_failure_issue` at `litehive/observability/status_probes.py:395`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_recovery_failure_context` at `litehive/observability/status_probes.py:431`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_task_issue_stage` at `litehive/observability/status_probes.py:470`
  Classification: utility.
  Args: `task, preferred_stage`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_backlog_damage_issue` at `litehive/observability/status_probes.py:490`
  Classification: utility.
  Args: `task, queued_ids, active_task_id, active_stage`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_runtime_resume_stage` at `litehive/observability/status_probes.py:546`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_task_has_resume_marker` at `litehive/observability/status_probes.py:569`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/observability/status_rendering.py`
- `status_has_problems` at `litehive/observability/status_rendering.py:14`
  Classification: domain/service candidate.
  Args: `issues`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `render_health_summary` at `litehive/observability/status_rendering.py:26`
  Classification: domain/service candidate.
  Args: `issues`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `render_issue_lines` at `litehive/observability/status_rendering.py:40`
  Classification: domain/service candidate.
  Args: `issues`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `render_operational_issue_lines` at `litehive/observability/status_rendering.py:54`
  Classification: domain/service candidate.
  Args: `issues`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `_terse_operational_issue_lines` at `litehive/observability/status_rendering.py:73`
  Classification: utility.
  Args: `issues`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_operational_issue_message` at `litehive/observability/status_rendering.py:90`
  Classification: utility.
  Args: `message`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/observability/status_summary.py`
- `estimate_task_execution` at `litehive/observability/status_summary.py:32`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_collect_report_durations` at `litehive/observability/status_summary.py:68`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_task_engine_label` at `litehive/observability/status_summary.py:84`
  Classification: utility.
  Args: `task, default_engine`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_task_stage_label` at `litehive/observability/status_summary.py:102`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_latest_stage_report_for_task` at `litehive/observability/status_summary.py:115`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_task_last_verdict_label` at `litehive/observability/status_summary.py:133`
  Classification: utility.
  Args: `task, workspace`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_task_last_summary_label` at `litehive/observability/status_summary.py:151`
  Classification: utility.
  Args: `task, workspace`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_latest_stage_failure_classification` at `litehive/observability/status_summary.py:175`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `render_task_summary` at `litehive/observability/status_summary.py:197`
  Classification: domain/service candidate.
  Args: `task, active, workspace`.
  Candidate owner: `renderer object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `_duration_label` at `litehive/observability/status_summary.py:366`
  Classification: utility.
  Args: `started_at, fallback_seconds`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_seconds_label` at `litehive/observability/status_summary.py:387`
  Classification: utility.
  Args: `seconds`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_pid_label` at `litehive/observability/status_summary.py:405`
  Classification: utility.
  Args: `pid`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_sandbox_label` at `litehive/observability/status_summary.py:418`
  Classification: utility.
  Args: `sandboxed, sandbox_summary`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/observability/status_types.py`
- `StatusIssue.render` at `litehive/observability/status_types.py:49`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `StatusIssue`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/observability/venv_health.py`
- `BrokenVenvExecutable.binary_name` at `litehive/observability/venv_health.py:39`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `BrokenVenvExecutable`.
  Note: Already on an object; review class responsibility before moving.
- `discover_workspace_venvs_for_workspace` at `litehive/observability/venv_health.py:50`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `probe_broken_venv_executables_for_workspace` at `litehive/observability/venv_health.py:79`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `broken_venv_issue_message` at `litehive/observability/venv_health.py:108`
  Classification: domain/service candidate.
  Args: `finding`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `daemon_broken_venv_message` at `litehive/observability/venv_health.py:126`
  Classification: domain/service candidate.
  Args: `findings`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `_broken_venv_bullets` at `litehive/observability/venv_health.py:144`
  Classification: utility.
  Args: `findings`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_venv_bin_dir` at `litehive/observability/venv_health.py:156`
  Classification: utility.
  Args: `venv_path`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_iter_probe_candidates` at `litehive/observability/venv_health.py:172`
  Classification: utility.
  Args: `bin_dir`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_probe_executable` at `litehive/observability/venv_health.py:195`
  Classification: utility.
  Args: `binary_path`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/recovery/detection.py`
- `TaskLaunchFailure.__init__` at `litehive/recovery/detection.py:25`
  Classification: utility/protocol method.
  Args: `self, context, summary, diagnostics`.
  Candidate owner: `TaskLaunchFailure`.
  Note: Already a method; usually special/protocol behavior.

### `litehive/recovery/execution_recovery.py`
- `recover_stale_runner_state_for_workspace` at `litehive/recovery/execution_recovery.py:42`
  Classification: domain/service candidate.
  Args: `workspace, summary`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_can_skip_recovery_scan` at `litehive/recovery/execution_recovery.py:116`
  Classification: utility.
  Args: `active_task_id, running_task_ids, current_thread_owns_runner_guard, runner_lock_held, has_repair_candidates`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/recovery/interrupted_subagent.py`
- `mark_interrupted_subagent` at `litehive/recovery/interrupted_subagent.py:19`
  Classification: domain/service candidate.
  Args: `workspace, task, reason, stage`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_interrupted_subagent_snippet` at `litehive/recovery/interrupted_subagent.py:63`
  Classification: domain/service candidate.
  Args: `workspace, task, active`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_interrupted_subagent_reason` at `litehive/recovery/interrupted_subagent.py:92`
  Classification: utility.
  Args: `task, reason`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_write_interrupted_subagent_artifacts` at `litehive/recovery/interrupted_subagent.py:116`
  Classification: domain/service candidate.
  Args: `workspace, task, subagent, resume_stage`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.

### `litehive/recovery/interruption_state.py`
- `prepare_interrupted_task` at `litehive/recovery/interruption_state.py:26`
  Classification: domain/service candidate.
  Args: `workspace, task, stage, summary, reason`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `interruption_journal_message` at `litehive/recovery/interruption_state.py:63`
  Classification: domain/service candidate.
  Args: `task`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `stale_interruption_reason` at `litehive/recovery/interruption_state.py:98`
  Classification: domain/service candidate.
  Args: `task, stage, stale_pid`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `_interruption_timestamps` at `litehive/recovery/interruption_state.py:120`
  Classification: utility.
  Args: `task, now`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_set_interruption_metadata` at `litehive/recovery/interruption_state.py:142`
  Classification: utility.
  Args: `task, workspace, stage, summary, reason, now, started_at, run_started_at, stage_started_at, interrupted_at`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/recovery/nonrunning_resumable_repair.py`
- `normalize_nonrunning_resumable_tasks` at `litehive/recovery/nonrunning_resumable_repair.py:26`
  Classification: utility.
  Args: `state, tasks_by_id, summary`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `has_nonrunning_resumable_repair_candidates` at `litehive/recovery/nonrunning_resumable_repair.py:118`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/recovery/running_task_recovery.py`
- `_stale_pid_warnings` at `litehive/recovery/running_task_recovery.py:45`
  Classification: utility.
  Args: `stale_pid`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `running_task_ids` at `litehive/recovery/running_task_recovery.py:59`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `should_requeue_commit_stage_task` at `litehive/recovery/running_task_recovery.py:83`
  Classification: domain/service candidate.
  Args: `task`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `can_attempt_stale_runner_recovery` at `litehive/recovery/running_task_recovery.py:99`
  Classification: domain/service candidate.
  Args: `workspace, tasks_by_id, running_task_ids`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `recover_running_tasks` at `litehive/recovery/running_task_recovery.py:124`
  Classification: domain/service candidate.
  Args: `workspace, state, tasks_by_id, running_task_ids, summary`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `update_active_task_after_recovery` at `litehive/recovery/running_task_recovery.py:169`
  Classification: domain/service candidate.
  Args: `workspace, state, tasks_by_id, prioritized_ids, running_task_ids, summary`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_has_inactive_running_tasks` at `litehive/recovery/running_task_recovery.py:218`
  Classification: domain/service candidate.
  Args: `workspace, tasks_by_id, timeout_seconds`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_record_stale_recovery` at `litehive/recovery/running_task_recovery.py:248`
  Classification: domain/service candidate.
  Args: `workspace, task, stage, journal_message, summary, stale_pid`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_recover_stale_running_task` at `litehive/recovery/running_task_recovery.py:286`
  Classification: domain/service candidate.
  Args: `workspace, task, summary`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_task_state_row_exists` at `litehive/recovery/running_task_recovery.py:331`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.

### `litehive/recovery/scope_analysis.py`
- `analyze_scope_changes` at `litehive/recovery/scope_analysis.py:30`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_is_file_broken_on_main` at `litehive/recovery/scope_analysis.py:83`
  Classification: domain/service candidate.
  Args: `workspace, file_path`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_is_test_file` at `litehive/recovery/scope_analysis.py:99`
  Classification: utility.
  Args: `file_path`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_is_test_broken_on_main` at `litehive/recovery/scope_analysis.py:120`
  Classification: domain/service candidate.
  Args: `workspace, test_file`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_has_syntax_errors_on_main` at `litehive/recovery/scope_analysis.py:156`
  Classification: domain/service candidate.
  Args: `workspace, file_path`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_classify_changes` at `litehive/recovery/scope_analysis.py:179`
  Classification: utility.
  Args: `deleted_files, broken_on_main, healthy_on_main`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/recovery/workspace_repair.py`
- `repair_workspace_state` at `litehive/recovery/workspace_repair.py:27`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_normalize_stale_terminal_tasks` at `litehive/recovery/workspace_repair.py:44`
  Classification: domain/service candidate.
  Args: `workspace, summary`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_stale_terminal_candidate_ids` at `litehive/recovery/workspace_repair.py:127`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.

### `litehive/roles/base.py`
- `PromptContext.workspace_root` at `litehive/roles/base.py:39`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `PromptContext`.
  Note: Already on an object; review class responsibility before moving.
- `_bulletize` at `litehive/roles/base.py:44`
  Classification: utility.
  Args: `lines`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_stage_hook_summaries` at `litehive/roles/base.py:60`
  Classification: utility.
  Args: `raw_hooks`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `RoleAgent.__init__` at `litehive/roles/base.py:114`
  Classification: utility/protocol method.
  Args: `self, selector, session_provider, prompt_context, retry_budget, retry_on, retry_backoff_seconds, retry_backoff_multiplier, grace_period_seconds`.
  Candidate owner: `RoleAgent`.
  Note: Already a method; usually special/protocol behavior.
- `RoleAgent.build_prompt` at `litehive/roles/base.py:151`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `RoleAgent`.
  Note: Already on an object; review class responsibility before moving.
- `RoleAgent._last_rejection_for_prompt` at `litehive/roles/base.py:175`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `RoleAgent`.
  Note: Already on an object; review class responsibility before moving.
- `RoleAgent._runner_hooks_for_stage` at `litehive/roles/base.py:192`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `RoleAgent`.
  Note: Already on an object; review class responsibility before moving.
- `RoleAgent._assemble_instruction_layers` at `litehive/roles/base.py:208`
  Classification: domain/service method.
  Args: `self, last_rejection`.
  Candidate owner: `RoleAgent`.
  Note: Already on an object; review class responsibility before moving.
- `RoleAgent._attempt_instruction_layer` at `litehive/roles/base.py:242`
  Classification: domain/service method.
  Args: `self, last_rejection`.
  Candidate owner: `RoleAgent`.
  Note: Already on an object; review class responsibility before moving.
- `RoleAgent._startup_guidance_for` at `litehive/roles/base.py:260`
  Classification: domain/service method.
  Args: `self, key`.
  Candidate owner: `RoleAgent`.
  Note: Already on an object; review class responsibility before moving.
- `RoleAgent._load_overlay_md` at `litehive/roles/base.py:266`
  Classification: domain/service method.
  Args: `self, key`.
  Candidate owner: `RoleAgent`.
  Note: Already on an object; review class responsibility before moving.
- `_last_rejection_payload_or_none` at `litehive/roles/base.py:283`
  Classification: utility.
  Args: `last_rejection`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_last_rejection_payload` at `litehive/roles/base.py:290`
  Classification: utility.
  Args: `last_rejection`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_failed_run_history_payload` at `litehive/roles/base.py:310`
  Classification: utility.
  Args: `state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_build_implementing_retry_origin_by_phase` at `litehive/roles/base.py:332`
  Classification: utility.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_latest_reject_stage_for_implementing` at `litehive/roles/base.py:355`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.

### `litehive/roles/guidance.py`
- `default_startup_guidance` at `litehive/roles/guidance.py:27`
  Classification: domain/service candidate.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.

### `litehive/roles/merge.py`
- `MergeAgent.build_prompt` at `litehive/roles/merge.py:54`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `MergeAgent`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/roles/recovery.py`
- `RecoveryAgent.build_prompt` at `litehive/roles/recovery.py:79`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `RecoveryAgent`.
  Note: Already on an object; review class responsibility before moving.
- `RecoveryAgent.verdict_to_event` at `litehive/roles/recovery.py:147`
  Classification: domain/service method.
  Args: `self, verdict`.
  Candidate owner: `RecoveryAgent`.
  Note: Already on an object; review class responsibility before moving.
- `_recovery_history_key` at `litehive/roles/recovery.py:178`
  Classification: utility.
  Args: `item`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_state_recovery_payload` at `litehive/roles/recovery.py:197`
  Classification: utility.
  Args: `outcome`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_runtime_recovery_projection_payload` at `litehive/roles/recovery.py:214`
  Classification: utility.
  Args: `outcome`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_merged_recovery_history_payload` at `litehive/roles/recovery.py:219`
  Classification: utility.
  Args: `state, task_record`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_same_recovery_path` at `litehive/roles/recovery.py:245`
  Classification: utility.
  Args: `current_trigger, prior`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_repeated_recovery_fingerprint_payload` at `litehive/roles/recovery.py:263`
  Classification: utility.
  Args: `trigger, recovery_history`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_recovery_source_checkout` at `litehive/roles/recovery.py:292`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_recovery_source_checkout_diagnostic` at `litehive/roles/recovery.py:316`
  Classification: domain/service candidate.
  Args: `workspace, exc`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_failed_subagent_diagnostics_payload` at `litehive/roles/recovery.py:326`
  Classification: domain/service candidate.
  Args: `workspace, task_record`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_read_subagent_artifact` at `litehive/roles/recovery.py:421`
  Classification: utility.
  Args: `subagent_base, artifact_name`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/sandbox/adapter.py`
- `SandboxedAdapter.__init__` at `litehive/sandbox/adapter.py:29`
  Classification: utility/protocol method.
  Args: `self, adapter, launcher, engine_name, role`.
  Candidate owner: `SandboxedAdapter`.
  Note: Already a method; usually special/protocol behavior.
- `SandboxedAdapter.build_command` at `litehive/sandbox/adapter.py:51`
  Classification: domain/service method.
  Args: `self, prompt, cwd, model, max_turns, resume_session_id`.
  Candidate owner: `SandboxedAdapter`.
  Note: Already on an object; review class responsibility before moving.
- `SandboxedAdapter.detect_capabilities` at `litehive/sandbox/adapter.py:68`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SandboxedAdapter`.
  Note: Already on an object; review class responsibility before moving.
- `SandboxedAdapter.finalize_invocation` at `litehive/sandbox/adapter.py:72`
  Classification: domain/service method.
  Args: `self, invocation`.
  Candidate owner: `SandboxedAdapter`.
  Note: Already on an object; review class responsibility before moving.
- `SandboxedAdapter.sandbox_details` at `litehive/sandbox/adapter.py:81`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SandboxedAdapter`.
  Note: Already on an object; review class responsibility before moving.
- `SandboxedAdapter.run` at `litehive/sandbox/adapter.py:85`
  Classification: domain/service method.
  Args: `self, prompt, cwd, model, max_turns, resume_session_id, on_started, extra_env, emit_unified`.
  Candidate owner: `SandboxedAdapter`.
  Note: Already on an object; review class responsibility before moving.
- `SandboxedAdapter.run_live` at `litehive/sandbox/adapter.py:129`
  Classification: domain/service method.
  Args: `self, prompt, cwd, model, max_turns, resume_session_id, on_started, on_update, inactivity_timeout_seconds, extra_env, emit_unified`.
  Candidate owner: `SandboxedAdapter`.
  Note: Already on an object; review class responsibility before moving.
- `SandboxedAdapter.render_transcript` at `litehive/sandbox/adapter.py:178`
  Classification: domain/service method.
  Args: `self, execution`.
  Candidate owner: `SandboxedAdapter`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/sandbox/git_wrapper.py`
- `main` at `litehive/sandbox/git_wrapper.py:23`
  Classification: domain/service candidate.
  Args: `argv, real_git_path, workspace_root`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `rejection_reason` at `litehive/sandbox/git_wrapper.py:43`
  Classification: domain/service candidate.
  Args: `argv, cwd`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `_non_option_args` at `litehive/sandbox/git_wrapper.py:89`
  Classification: utility.
  Args: `argv`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_is_origin_ref` at `litehive/sandbox/git_wrapper.py:101`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_is_protected_ref` at `litehive/sandbox/git_wrapper.py:113`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_current_ref` at `litehive/sandbox/git_wrapper.py:125`
  Classification: utility.
  Args: `cwd`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_resolve_git_dir` at `litehive/sandbox/git_wrapper.py:153`
  Classification: utility.
  Args: `cwd`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_format_cmd` at `litehive/sandbox/git_wrapper.py:182`
  Classification: utility.
  Args: `argv`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/sandbox/launcher.py`
- `SandboxProfile.for_role` at `litehive/sandbox/launcher.py:27`
  Classification: domain/service method.
  Args: `cls, role`.
  Candidate owner: `SandboxProfile`.
  Note: Already on an object; review class responsibility before moving.
- `SandboxPolicySummary.from_mapping` at `litehive/sandbox/launcher.py:55`
  Classification: domain/service method.
  Args: `cls, payload`.
  Candidate owner: `SandboxPolicySummary`.
  Note: Already on an object; review class responsibility before moving.
- `SandboxPolicySummary.as_dict` at `litehive/sandbox/launcher.py:75`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SandboxPolicySummary`.
  Note: Already on an object; review class responsibility before moving.
- `SandboxPolicySummary.summary` at `litehive/sandbox/launcher.py:95`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `SandboxPolicySummary`.
  Note: Already on an object; review class responsibility before moving.
- `_optional_str` at `litehive/sandbox/launcher.py:122`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_string_tuple` at `litehive/sandbox/launcher.py:131`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `SandboxLauncher.policy_summary` at `litehive/sandbox/launcher.py:151`
  Classification: domain/service method.
  Args: `self, engine_name`.
  Candidate owner: `SandboxLauncher`.
  Note: Already on an object; review class responsibility before moving.
- `SandboxLauncher.wrap_invocation` at `litehive/sandbox/launcher.py:155`
  Classification: domain/service method.
  Args: `self, engine_name, binary_name, invocation, role`.
  Candidate owner: `SandboxLauncher`.
  Note: Already on an object; review class responsibility before moving.
- `DockerSandboxLauncher.__init__` at `litehive/sandbox/launcher.py:169`
  Classification: utility/protocol method.
  Args: `self, workspace, config`.
  Candidate owner: `DockerSandboxLauncher`.
  Note: Already a method; usually special/protocol behavior.
- `DockerSandboxLauncher.policy_summary` at `litehive/sandbox/launcher.py:174`
  Classification: domain/service method.
  Args: `self, engine_name`.
  Candidate owner: `DockerSandboxLauncher`.
  Note: Already on an object; review class responsibility before moving.
- `DockerSandboxLauncher.wrap_invocation` at `litehive/sandbox/launcher.py:199`
  Classification: domain/service method.
  Args: `self, engine_name, binary_name, invocation, role`.
  Candidate owner: `DockerSandboxLauncher`.
  Note: Already on an object; review class responsibility before moving.
- `DockerSandboxLauncher._wrap_docker` at `litehive/sandbox/launcher.py:240`
  Classification: domain/service method.
  Args: `self, engine_name, role, binary_path, invocation`.
  Candidate owner: `DockerSandboxLauncher`.
  Note: Already on an object; review class responsibility before moving.
- `DockerSandboxLauncher.ensure_docker_git_wrappers` at `litehive/sandbox/launcher.py:384`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `DockerSandboxLauncher`.
  Note: Already on an object; review class responsibility before moving.
- `DockerSandboxLauncher._policy_for_engine` at `litehive/sandbox/launcher.py:405`
  Classification: domain/service method.
  Args: `self, engine_name`.
  Candidate owner: `DockerSandboxLauncher`.
  Note: Already on an object; review class responsibility before moving.
- `DockerSandboxLauncher._resolved_extra_ro_binds` at `litehive/sandbox/launcher.py:410`
  Classification: domain/service method.
  Args: `engine_name, policy, env`.
  Candidate owner: `DockerSandboxLauncher`.
  Note: Already on an object; review class responsibility before moving.
- `DockerSandboxLauncher._resolved_extra_rw_binds` at `litehive/sandbox/launcher.py:446`
  Classification: domain/service method.
  Args: `engine_name, policy, env`.
  Candidate owner: `DockerSandboxLauncher`.
  Note: Already on an object; review class responsibility before moving.
- `DockerSandboxLauncher._translate_container_argv` at `litehive/sandbox/launcher.py:486`
  Classification: domain/service method.
  Args: `argv, host_root, container_root`.
  Candidate owner: `DockerSandboxLauncher`.
  Note: Already on an object; review class responsibility before moving.
- `DockerSandboxLauncher._bind_mount_spec` at `litehive/sandbox/launcher.py:514`
  Classification: domain/service method.
  Args: `source, target, read_only`.
  Candidate owner: `DockerSandboxLauncher`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/sandbox/support.py`
- `forced_engine_rw_state_dirs` at `litehive/sandbox/support.py:7`
  Classification: domain/service candidate.
  Args: `engine_name, policy, env`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `sanitize_path_env` at `litehive/sandbox/support.py:60`
  Classification: domain/service candidate.
  Args: `raw_path`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.

### `litehive/state/backup.py`
- `_backup_timestamp` at `litehive/state/backup.py:33`
  Classification: utility.
  Args: `when`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_backup_path_for_workspace` at `litehive/state/backup.py:45`
  Classification: domain/service candidate.
  Args: `workspace, when`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_parse_backup_path` at `litehive/state/backup.py:56`
  Classification: utility.
  Args: `path`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `list_workspace_backups_for_workspace` at `litehive/state/backup.py:78`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `prune_workspace_backups_for_workspace` at `litehive/state/backup.py:98`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `create_workspace_backup_for_workspace` at `litehive/state/backup.py:136`
  Classification: domain/service candidate.
  Args: `workspace, when`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `create_scheduled_workspace_backup_for_workspace` at `litehive/state/backup.py:195`
  Classification: domain/service candidate.
  Args: `workspace, now`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `restore_workspace_backup_for_workspace` at `litehive/state/backup.py:213`
  Classification: domain/service candidate.
  Args: `workspace, timestamp`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/state/lock_manager.py`
- `WorkspaceLockManager._is_held_in_process` at `litehive/state/lock_manager.py:30`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `WorkspaceLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `WorkspaceLockManager._parse_metadata_text` at `litehive/state/lock_manager.py:43`
  Classification: domain/service method.
  Args: `self, text, strict`.
  Candidate owner: `WorkspaceLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `WorkspaceLockManager.read_metadata` at `litehive/state/lock_manager.py:67`
  Classification: domain/service method.
  Args: `self, strict`.
  Candidate owner: `WorkspaceLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `WorkspaceLockManager.read_locked_metadata` at `litehive/state/lock_manager.py:86`
  Classification: domain/service method.
  Args: `self, handle`.
  Candidate owner: `WorkspaceLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `WorkspaceLockManager.write_locked_metadata` at `litehive/state/lock_manager.py:101`
  Classification: domain/service method.
  Args: `self, handle, payload`.
  Candidate owner: `WorkspaceLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `WorkspaceLockManager.clear_locked_metadata` at `litehive/state/lock_manager.py:119`
  Classification: domain/service method.
  Args: `self, handle`.
  Candidate owner: `WorkspaceLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `WorkspaceLockManager.open` at `litehive/state/lock_manager.py:133`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `WorkspaceLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `WorkspaceLockManager.lock` at `litehive/state/lock_manager.py:145`
  Classification: domain/service method.
  Args: `self, handle, nonblocking`.
  Candidate owner: `WorkspaceLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `WorkspaceLockManager.unlock` at `litehive/state/lock_manager.py:161`
  Classification: domain/service method.
  Args: `self, handle`.
  Candidate owner: `WorkspaceLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `WorkspaceLockManager.acquire` at `litehive/state/lock_manager.py:171`
  Classification: domain/service method.
  Args: `self, nonblocking, cleanup_stale_inode`.
  Candidate owner: `WorkspaceLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `WorkspaceLockManager.release` at `litehive/state/lock_manager.py:190`
  Classification: domain/service method.
  Args: `self, handle, clear_metadata`.
  Candidate owner: `WorkspaceLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `WorkspaceLockManager.is_active` at `litehive/state/lock_manager.py:208`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `WorkspaceLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `WorkspaceLockManager._pid_is_live` at `litehive/state/lock_manager.py:231`
  Classification: domain/service method.
  Args: `self, metadata`.
  Candidate owner: `WorkspaceLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `WorkspaceLockManager.pid_is_stale` at `litehive/state/lock_manager.py:245`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `WorkspaceLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `WorkspaceLockManager.clear_metadata_if_unlocked` at `litehive/state/lock_manager.py:262`
  Classification: domain/service method.
  Args: `self, expected_pid, require_stale_pid`.
  Candidate owner: `WorkspaceLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `WorkspaceLockManager.remove_stale_lockfile` at `litehive/state/lock_manager.py:296`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `WorkspaceLockManager`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/state/locking.py`
- `_runner_lock_key_for_workspace` at `litehive/state/locking.py:35`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_pid_is_zombie` at `litehive/state/locking.py:42`
  Classification: utility.
  Args: `pid`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_runner_lock_manager_for_workspace` at `litehive/state/locking.py:59`
  Classification: domain/service candidate.
  Args: `workspace, held_in_process`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `workspace_lock_for_workspace` at `litehive/state/locking.py:83`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `write_runner_lock_metadata` at `litehive/state/locking.py:103`
  Classification: domain/service candidate.
  Args: `handle, status`.
  Candidate owner: `writer/store object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `_save_runner_process_state_for_workspace` at `litehive/state/locking.py:123`
  Classification: domain/service candidate.
  Args: `workspace, status`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_clear_runner_process_state_for_workspace` at `litehive/state/locking.py:138`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `read_runner_lock_metadata_for_workspace` at `litehive/state/locking.py:149`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `runner_metadata_present` at `litehive/state/locking.py:163`
  Classification: domain/service candidate.
  Args: `status`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `runner_lock_is_active_for_workspace` at `litehive/state/locking.py:183`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `runner_status_needs_reconciliation_for_workspace` at `litehive/state/locking.py:191`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `clear_runner_lock_metadata_for_workspace` at `litehive/state/locking.py:212`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `heartbeat_is_late` at `litehive/state/locking.py:226`
  Classification: domain/service candidate.
  Args: `heartbeat_at`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `runner_status_for_workspace` at `litehive/state/locking.py:246`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `touch_runner_status_for_workspace` at `litehive/state/locking.py:268`
  Classification: domain/service candidate.
  Args: `workspace, active_task_id`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `runner_heartbeat_for_workspace` at `litehive/state/locking.py:294`
  Classification: domain/service candidate.
  Args: `workspace, active_task_id, interval_seconds`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `runner_heartbeat_for_workspace._heartbeat_loop` at `litehive/state/locking.py:309`
  Classification: utility.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `current_thread_owns_runner_guard_for_workspace` at `litehive/state/locking.py:332`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `runner_pid_is_alive` at `litehive/state/locking.py:343`
  Classification: domain/service candidate.
  Args: `pid`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `subagent_process_is_stale` at `litehive/state/locking.py:369`
  Classification: domain/service candidate.
  Args: `task`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `runner_lock_pid_is_stale_for_workspace` at `litehive/state/locking.py:386`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `runner_lock_is_held_for_workspace` at `litehive/state/locking.py:393`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `runner_conflict_message_for_workspace` at `litehive/state/locking.py:403`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_auto_repair_stale_state` at `litehive/state/locking.py:435`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `workspace_runner_guard` at `litehive/state/locking.py:464`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `workspace_mutation_guard_for_workspace` at `litehive/state/locking.py:542`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `ensure_future_task_mutation_allowed_for_workspace` at `litehive/state/locking.py:562`
  Classification: domain/service candidate.
  Args: `workspace, task_ids, state`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `persist_future_task_update_for_workspace` at `litehive/state/locking.py:606`
  Classification: domain/service candidate.
  Args: `workspace, task, journal_message, audit_entries`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/state/persist.py`
- `skip_bootstrap_load_state` at `litehive/state/persist.py:26`
  Classification: domain/service candidate.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `load_state_for_workspace` at `litehive/state/persist.py:42`
  Classification: domain/service candidate.
  Args: `workspace, bootstrap`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `atomic_write_text` at `litehive/state/persist.py:61`
  Classification: domain/service candidate.
  Args: `path, content`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `atomic_write_gzip_text` at `litehive/state/persist.py:84`
  Classification: domain/service candidate.
  Args: `path, content`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `_file_snapshot` at `litehive/state/persist.py:103`
  Classification: utility.
  Args: `path`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `write_atomic_files` at `litehive/state/persist.py:116`
  Classification: domain/service candidate.
  Args: `writes`.
  Candidate owner: `writer/store object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `write_atomic_files_and_then` at `litehive/state/persist.py:143`
  Classification: domain/service candidate.
  Args: `writes, callback`.
  Candidate owner: `writer/store object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `save_state_for_workspace` at `litehive/state/persist.py:171`
  Classification: domain/service candidate.
  Args: `workspace, state`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `save_state_without_runner_guard_for_workspace` at `litehive/state/persist.py:183`
  Classification: domain/service candidate.
  Args: `workspace, state, audit_entries`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `record_task_completion_for_workspace` at `litehive/state/persist.py:206`
  Classification: domain/service candidate.
  Args: `workspace, final_stage`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `set_pool_stop_reason_for_workspace` at `litehive/state/persist.py:230`
  Classification: domain/service candidate.
  Args: `workspace, stop_reason`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_merge_queue_preserving_future_changes` at `litehive/state/persist.py:248`
  Classification: utility.
  Args: `desired_queue, latest_queue, protected_task_ids`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `merged_state_for_runner_owned_write_for_workspace` at `litehive/state/persist.py:298`
  Classification: domain/service candidate.
  Args: `workspace, state, protected_task_ids`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `persist_task_and_state_for_workspace` at `litehive/state/persist.py:322`
  Classification: domain/service candidate.
  Args: `workspace, task, state, journal_message, protected_task_ids, audit_entries`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `persist_tasks_and_state_for_workspace` at `litehive/state/persist.py:352`
  Classification: domain/service candidate.
  Args: `workspace, tasks, state, journal_messages, protected_task_ids, audit_entries`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `persist_tasks_and_state_without_runner_guard_for_workspace` at `litehive/state/persist.py:389`
  Classification: domain/service candidate.
  Args: `workspace, tasks, state, journal_messages, protected_task_ids, audit_entries`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `persist_task_and_state_without_runner_guard_for_workspace` at `litehive/state/persist.py:425`
  Classification: domain/service candidate.
  Args: `workspace, task, state, journal_message, protected_task_ids, audit_entries`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/state/process_lock.py`
- `ProcessLockManager.is_active` at `litehive/state/process_lock.py:29`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `ProcessLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `ProcessLockManager.read_metadata` at `litehive/state/process_lock.py:33`
  Classification: domain/service method.
  Args: `self, strict`.
  Candidate owner: `ProcessLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `ProcessLockManager.read_locked_metadata` at `litehive/state/process_lock.py:37`
  Classification: domain/service method.
  Args: `self, handle`.
  Candidate owner: `ProcessLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `ProcessLockManager.write_locked_metadata` at `litehive/state/process_lock.py:41`
  Classification: domain/service method.
  Args: `self, handle, payload`.
  Candidate owner: `ProcessLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `ProcessLockManager.clear_metadata_if_unlocked` at `litehive/state/process_lock.py:45`
  Classification: domain/service method.
  Args: `self, expected_pid, require_stale_pid`.
  Candidate owner: `ProcessLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `ProcessLockManager.pid_is_stale` at `litehive/state/process_lock.py:61`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `ProcessLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `ProcessLockManager.remove_stale_lockfile` at `litehive/state/process_lock.py:65`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `ProcessLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `ProcessLockManager.open_locked` at `litehive/state/process_lock.py:70`
  Classification: domain/service method.
  Args: `self, nonblocking`.
  Candidate owner: `ProcessLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `ProcessLockManager.acquire_with_metadata` at `litehive/state/process_lock.py:84`
  Classification: domain/service method.
  Args: `self, metadata, nonblocking`.
  Candidate owner: `ProcessLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `ProcessLockManager.release_with_cleanup` at `litehive/state/process_lock.py:101`
  Classification: domain/service method.
  Args: `self, handle, clear_metadata`.
  Candidate owner: `ProcessLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `ProcessLockManager.create_base_metadata` at `litehive/state/process_lock.py:112`
  Classification: domain/service method.
  Args: `self, pid, extra`.
  Candidate owner: `ProcessLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `ProcessLockManager.update_heartbeat` at `litehive/state/process_lock.py:130`
  Classification: domain/service method.
  Args: `self, handle, extra_updates`.
  Candidate owner: `ProcessLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `ProcessLockManager.save_process_state` at `litehive/state/process_lock.py:145`
  Classification: domain/service method.
  Args: `self, payload, status`.
  Candidate owner: `ProcessLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `ProcessLockManager.clear_process_state` at `litehive/state/process_lock.py:160`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `ProcessLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `ProcessLockManager.clear_stale_state` at `litehive/state/process_lock.py:171`
  Classification: domain/service method.
  Args: `self, expected_pid`.
  Candidate owner: `ProcessLockManager`.
  Note: Already on an object; review class responsibility before moving.
- `ProcessLockManager._runtime_store` at `litehive/state/process_lock.py:187`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `ProcessLockManager`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/state/rebuild_safety.py`
- `sqlite_task_ids` at `litehive/state/rebuild_safety.py:47`
  Classification: domain/service candidate.
  Args: `db_path`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `task_artifact_dir_ids_for_workspace` at `litehive/state/rebuild_safety.py:73`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `event_log_replay_task_ids_for_workspace` at `litehive/state/rebuild_safety.py:100`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `assert_database_rebuild_safe_for_workspace` at `litehive/state/rebuild_safety.py:137`
  Classification: domain/service candidate.
  Args: `workspace, db_path, replay_task_ids, operation`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `backup_database_before_rebuild_for_workspace` at `litehive/state/rebuild_safety.py:185`
  Classification: domain/service candidate.
  Args: `workspace, db_path, label`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/state/records.py`
- `_highest_task_number_in_store_for_workspace` at `litehive/state/records.py:53`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_reserve_next_task_numbers_for_workspace` at `litehive/state/records.py:64`
  Classification: domain/service candidate.
  Args: `workspace, state, count`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_task_creation_stage_for_workspace` at `litehive/state/records.py:86`
  Classification: domain/service candidate.
  Args: `workspace, current_task_id`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_default_task_creation_source_for_workspace` at `litehive/state/records.py:112`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `ensure_runtime_ignored_for_workspace` at `litehive/state/records.py:140`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `task_state_for_storage` at `litehive/state/records.py:155`
  Classification: domain/service candidate.
  Args: `task`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `write_task_runtime_for_workspace` at `litehive/state/records.py:170`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `set_task_commit_sha` at `litehive/state/records.py:182`
  Classification: domain/service candidate.
  Args: `task, commit_sha`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `get_task_worktree_path` at `litehive/state/records.py:195`
  Classification: domain/service candidate.
  Args: `task`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `set_task_worktree_path` at `litehive/state/records.py:206`
  Classification: domain/service candidate.
  Args: `task, worktree_path`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `clear_task_worktree_path` at `litehive/state/records.py:218`
  Classification: domain/service candidate.
  Args: `task`.
  Candidate owner: `repository/store object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `_normalize_task_worktree_state` at `litehive/state/records.py:229`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_normalize_task_commit_sha_state` at `litehive/state/records.py:244`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_normalize_task_flag_reason` at `litehive/state/records.py:260`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_created_from_payload` at `litehive/state/records.py:276`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_create_task_runtime_dirs` at `litehive/state/records.py:289`
  Classification: utility.
  Args: `base`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_cleanup_created_task_dirs` at `litehive/state/records.py:303`
  Classification: utility.
  Args: `paths`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_persist_created_tasks_for_workspace` at `litehive/state/records.py:321`
  Classification: domain/service candidate.
  Args: `workspace, tasks, state, task_journal_messages, cleanup_dirs, audit_entries`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_persist_created_tasks_for_workspace.callback` at `litehive/state/records.py:351`
  Classification: utility.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `save_task_runtime_for_workspace` at `litehive/state/records.py:379`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_load_task_runtime_for_workspace` at `litehive/state/records.py:392`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `create_task_for_workspace` at `litehive/state/records.py:411`
  Classification: domain/service candidate.
  Args: `workspace, title, depends_on, pipeline_mode, model, retry_limit, goal, acceptance_criteria, auto_commit, priority`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_follow_up_journal_messages` at `litehive/state/records.py:509`
  Classification: utility.
  Args: `created_tasks, follow_ups, parent_task, stage`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_follow_up_audit_entries` at `litehive/state/records.py:533`
  Classification: utility.
  Args: `created_tasks, queue_after`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `create_follow_up_tasks_for_workspace` at `litehive/state/records.py:566`
  Classification: domain/service candidate.
  Args: `workspace, parent_task, stage, follow_ups`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `discard_created_task_for_workspace` at `litehive/state/records.py:639`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_load_tasks_from_store_for_workspace` at `litehive/state/records.py:680`
  Classification: domain/service candidate.
  Args: `workspace, include_runtime, strict`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `list_tasks_for_workspace` at `litehive/state/records.py:714`
  Classification: domain/service candidate.
  Args: `workspace, include_runtime, strict`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `list_tasks_state_first_for_workspace` at `litehive/state/records.py:734`
  Classification: domain/service candidate.
  Args: `workspace, state, include_runtime`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `list_tasks_state_first_for_workspace.add` at `litehive/state/records.py:759`
  Classification: utility.
  Args: `task_id`.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `get_task_for_workspace` at `litehive/state/records.py:781`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `get_task_record_for_workspace` at `litehive/state/records.py:797`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `require_task_for_workspace` at `litehive/state/records.py:817`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `save_task_for_workspace` at `litehive/state/records.py:832`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/state/store.py`
- `RuntimeStore.__init__` at `litehive/state/store.py:49`
  Classification: utility/protocol method.
  Args: `self, workspace`.
  Candidate owner: `RuntimeStore`.
  Note: Already a method; usually special/protocol behavior.
- `RuntimeStore.bootstrap` at `litehive/state/store.py:60`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore._should_rebuild_from_task_event_log` at `litehive/state/store.py:79`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore.load_workspace_state` at `litehive/state/store.py:92`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore.load_workspace_state_read_only` at `litehive/state/store.py:117`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore.save_workspace_state` at `litehive/state/store.py:146`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore.load_task_state` at `litehive/state/store.py:161`
  Classification: domain/service method.
  Args: `self, task_id`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore.load_task_intent` at `litehive/state/store.py:179`
  Classification: domain/service method.
  Args: `self, task_id`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore.list_task_intents` at `litehive/state/store.py:198`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore.save_task_intent` at `litehive/state/store.py:216`
  Classification: domain/service method.
  Args: `self, task_id, intent`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore.save_task_state` at `litehive/state/store.py:235`
  Classification: domain/service method.
  Args: `self, task_id, state`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore.save_runtime_transaction` at `litehive/state/store.py:254`
  Classification: domain/service method.
  Args: `self, task_intents, task_states, workspace_state, task_journal_messages, audit_entries`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore.delete_task_records` at `litehive/state/store.py:292`
  Classification: domain/service method.
  Args: `self, task_id, audit_entries`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore._append_workspace_state_event` at `litehive/state/store.py:326`
  Classification: domain/service method.
  Args: `self, state`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore._append_runtime_transaction_events` at `litehive/state/store.py:341`
  Classification: domain/service method.
  Args: `self, task_intents, task_states, workspace_state, task_journal_entries, audit_entries`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore._append_runtime_transaction_events.payload_for_task` at `litehive/state/store.py:364`
  Classification: domain/service method.
  Args: `task_id`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore._save_workspace_state` at `litehive/state/store.py:417`
  Classification: domain/service method.
  Args: `self, connection, state`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore.load_task_runtime` at `litehive/state/store.py:450`
  Classification: domain/service method.
  Args: `self, task_id`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore.save_task_runtime` at `litehive/state/store.py:463`
  Classification: domain/service method.
  Args: `self, task_id, runtime`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore._save_task_state` at `litehive/state/store.py:477`
  Classification: domain/service method.
  Args: `self, connection, task_id, state`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore._save_task_intent` at `litehive/state/store.py:517`
  Classification: domain/service method.
  Args: `self, connection, task_id, intent`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore.append_task_journal` at `litehive/state/store.py:589`
  Classification: domain/service method.
  Args: `self, task_id, message`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore._append_task_journal` at `litehive/state/store.py:608`
  Classification: domain/service method.
  Args: `self, connection, task_id, message`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore.save_process_state` at `litehive/state/store.py:647`
  Classification: domain/service method.
  Args: `self, process_key, status, payload`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore.clear_process_state` at `litehive/state/store.py:707`
  Classification: domain/service method.
  Args: `self, process_key`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore.load_process_state` at `litehive/state/store.py:719`
  Classification: domain/service method.
  Args: `self, process_key`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore.highest_task_number` at `litehive/state/store.py:767`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `RuntimeStore.create_workspace_state_rows` at `litehive/state/store.py:790`
  Classification: domain/service method.
  Args: `connection`.
  Candidate owner: `RuntimeStore`.
  Note: Already on an object; review class responsibility before moving.
- `runtime_store_for_workspace` at `litehive/state/store.py:824`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `RuntimeStore`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_load_task_state_for_intent_columns` at `litehive/state/store.py:835`
  Classification: utility.
  Args: `connection, task_id`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_task_intent_column_values` at `litehive/state/store.py:858`
  Classification: utility.
  Args: `intent, state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_optional_str` at `litehive/state/store.py:896`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_optional_int` at `litehive/state/store.py:909`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/tasks/_process_signals.py`
- `terminate_subagent_pid` at `litehive/tasks/_process_signals.py:19`
  Classification: domain/service candidate.
  Args: `task_id, pid, wait_timeout_seconds, poll_interval_seconds`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `terminate_subagent_pid._pid_is_dead` at `litehive/tasks/_process_signals.py:35`
  Classification: utility.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `terminate_subagent_pid._wait_until_dead` at `litehive/tasks/_process_signals.py:67`
  Classification: utility.
  Args: `timeout_seconds`.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.

### `litehive/tasks/_status_helpers.py`
- `_reset_pipeline_state` at `litehive/tasks/_status_helpers.py:30`
  Classification: domain/service candidate.
  Args: `workspace, task_id, preserve_run_memory`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_persist_transition` at `litehive/tasks/_status_helpers.py:44`
  Classification: domain/service candidate.
  Args: `workspace, task, state, journal_message, action, actor, source, before_task, before_queue, context`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_queue_task` at `litehive/tasks/_status_helpers.py:86`
  Classification: utility.
  Args: `state, task_id, front`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_apply_cancelled_task_state` at `litehive/tasks/_status_helpers.py:101`
  Classification: utility.
  Args: `task, reason`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_apply_close_task_state` at `litehive/tasks/_status_helpers.py:125`
  Classification: utility.
  Args: `task, close_reason, reason, follow_up_task_id, pipeline_status`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_apply_parked_task_state` at `litehive/tasks/_status_helpers.py:181`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/tasks/activity.py`
- `TaskActivityLog.load` at `litehive/tasks/activity.py:32`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `TaskActivityLog`.
  Note: Already on an object; review class responsibility before moving.
- `TaskActivityLog.save` at `litehive/tasks/activity.py:65`
  Classification: domain/service method.
  Args: `self, activity`.
  Candidate owner: `TaskActivityLog`.
  Note: Already on an object; review class responsibility before moving.
- `TaskActivityLog.append` at `litehive/tasks/activity.py:91`
  Classification: domain/service method.
  Args: `self, entry`.
  Candidate owner: `TaskActivityLog`.
  Note: Already on an object; review class responsibility before moving.
- `TaskActivityLog.latest_entry` at `litehive/tasks/activity.py:102`
  Classification: domain/service method.
  Args: `self, role, stage, source_subagent_id, verdicts, after`.
  Candidate owner: `TaskActivityLog`.
  Note: Already on an object; review class responsibility before moving.
- `TaskActivityLog.latest` at `litehive/tasks/activity.py:136`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `TaskActivityLog`.
  Note: Already on an object; review class responsibility before moving.
- `load_task_activity` at `litehive/tasks/activity.py:143`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `save_task_activity` at `litehive/tasks/activity.py:150`
  Classification: domain/service candidate.
  Args: `workspace, task, activity`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `append_task_activity` at `litehive/tasks/activity.py:157`
  Classification: domain/service candidate.
  Args: `workspace, task, entry`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `latest_task_activity_entry` at `litehive/tasks/activity.py:164`
  Classification: domain/service candidate.
  Args: `workspace, task, role, stage, source_subagent_id, verdicts, after`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_parse_created_at` at `litehive/tasks/activity.py:189`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/tasks/activity_rendering.py`
- `append_activity_entry` at `litehive/tasks/activity_rendering.py:15`
  Classification: domain/service candidate.
  Args: `workspace, task, entry`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `normalized_files_changed` at `litehive/tasks/activity_rendering.py:26`
  Classification: domain/service candidate.
  Args: `paths`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `is_retracted_activity_entry` at `litehive/tasks/activity_rendering.py:47`
  Classification: domain/service candidate.
  Args: `entry`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `is_retractable_pass_entry` at `litehive/tasks/activity_rendering.py:59`
  Classification: domain/service candidate.
  Args: `entry`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `retract_activity_entry` at `litehive/tasks/activity_rendering.py:75`
  Classification: domain/service candidate.
  Args: `entry`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `render_task_activity` at `litehive/tasks/activity_rendering.py:90`
  Classification: domain/service candidate.
  Args: `workspace, task, for_prompt`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/tasks/audit.py`
- `snapshot_task_audit_state` at `litehive/tasks/audit.py:56`
  Classification: domain/service candidate.
  Args: `task`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `queue_position` at `litehive/tasks/audit.py:70`
  Classification: domain/service candidate.
  Args: `queue, task_id`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `build_task_audit_entry` at `litehive/tasks/audit.py:84`
  Classification: boundary utility.
  Args: `task_id, action, actor, source, before_task, after_task, before_queue, after_queue, context, created_at`.
  Candidate owner: `DI container factory`.
  Note: Assembly function; free function is acceptable at the container boundary.
- `insert_task_audit_entries` at `litehive/tasks/audit.py:139`
  Classification: domain/service candidate.
  Args: `connection, entries`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `append_task_audit_entries` at `litehive/tasks/audit.py:189`
  Classification: domain/service candidate.
  Args: `workspace, entries`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `load_task_audit_entries` at `litehive/tasks/audit.py:213`
  Classification: domain/service candidate.
  Args: `workspace, task_id, action, limit`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/tasks/completed_task_recovery.py`
- `require_completed_task` at `litehive/tasks/completed_task_recovery.py:15`
  Classification: domain/service candidate.
  Args: `task, action`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `recover_completed_task_for_workspace` at `litehive/tasks/completed_task_recovery.py:27`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/tasks/event_log.py`
- `_ReplayState.empty` at `litehive/tasks/event_log.py:98`
  Classification: domain/service method.
  Args: `cls`.
  Candidate owner: `_ReplayState`.
  Note: Already on an object; review class responsibility before moving.
- `task_event_log_path` at `litehive/tasks/event_log.py:121`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `task_event_logging_suppressed` at `litehive/tasks/event_log.py:126`
  Classification: domain/service candidate.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `suppress_task_event_logging` at `litehive/tasks/event_log.py:138`
  Classification: domain/service candidate.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `task_event_type_for_audit_action` at `litehive/tasks/event_log.py:154`
  Classification: domain/service candidate.
  Args: `action`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `append_task_event` at `litehive/tasks/event_log.py:166`
  Classification: domain/service candidate.
  Args: `workspace, event_type, task_id, payload, timestamp`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `read_task_events` at `litehive/tasks/event_log.py:204`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `task_event_log_has_events` at `litehive/tasks/event_log.py:237`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `rebuild_sqlite_from_task_event_log` at `litehive/tasks/event_log.py:262`
  Classification: domain/service candidate.
  Args: `workspace, clear_existing`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `sqlite_task_tables_empty` at `litehive/tasks/event_log.py:306`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_apply_event` at `litehive/tasks/event_log.py:321`
  Classification: utility.
  Args: `replay_state, event`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_delete_task_scoped_replay_state` at `litehive/tasks/event_log.py:391`
  Classification: utility.
  Args: `replay_state, task_id`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_rewrite_latest_stage_report` at `litehive/tasks/event_log.py:417`
  Classification: utility.
  Args: `replay_state, report`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_clear_replay_tables` at `litehive/tasks/event_log.py:437`
  Classification: utility.
  Args: `connection`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_write_replay_state` at `litehive/tasks/event_log.py:449`
  Classification: utility.
  Args: `connection, replay_state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_workspace_state_payload` at `litehive/tasks/event_log.py:481`
  Classification: utility.
  Args: `replay_state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_highest_task_number` at `litehive/tasks/event_log.py:495`
  Classification: utility.
  Args: `replay_state`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_insert_workspace_state` at `litehive/tasks/event_log.py:512`
  Classification: utility.
  Args: `connection, payload`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_insert_task_intent` at `litehive/tasks/event_log.py:546`
  Classification: utility.
  Args: `connection, task_id, payload`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_insert_task_state` at `litehive/tasks/event_log.py:611`
  Classification: utility.
  Args: `connection, task_id, payload`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_insert_task_journal` at `litehive/tasks/event_log.py:642`
  Classification: utility.
  Args: `connection, task_id, journal`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_insert_task_activity` at `litehive/tasks/event_log.py:673`
  Classification: utility.
  Args: `connection, task_id, activity`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_insert_stage_report` at `litehive/tasks/event_log.py:692`
  Classification: utility.
  Args: `connection, report`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_insert_recovery_report` at `litehive/tasks/event_log.py:712`
  Classification: utility.
  Args: `connection, report`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_insert_task_audit_entry` at `litehive/tasks/event_log.py:738`
  Classification: utility.
  Args: `connection, entry`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_insert_pipeline_task_state` at `litehive/tasks/event_log.py:782`
  Classification: utility.
  Args: `connection, task_id, row`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_insert_pipeline_transition` at `litehive/tasks/event_log.py:810`
  Classification: utility.
  Args: `connection, row`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_insert_pipeline_journal` at `litehive/tasks/event_log.py:841`
  Classification: utility.
  Args: `connection, row`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_optional_str` at `litehive/tasks/event_log.py:865`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_optional_int` at `litehive/tasks/event_log.py:878`
  Classification: utility.
  Args: `value`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_task_intent_column_values` at `litehive/tasks/event_log.py:895`
  Classification: utility.
  Args: `intent`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/tasks/failed_runs.py`
- `blocking_failed_run_records` at `litehive/tasks/failed_runs.py:12`
  Classification: domain/service candidate.
  Args: `task`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `has_blocking_failed_run_history` at `litehive/tasks/failed_runs.py:31`
  Classification: domain/service candidate.
  Args: `task`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `mark_failed_run_operator_override` at `litehive/tasks/failed_runs.py:42`
  Classification: domain/service candidate.
  Args: `workspace, task, records`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `failed_run_block_message` at `litehive/tasks/failed_runs.py:108`
  Classification: domain/service candidate.
  Args: `task, records`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `_failed_run_record_details` at `litehive/tasks/failed_runs.py:124`
  Classification: utility.
  Args: `records`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/tasks/journal.py`
- `append_journal` at `litehive/tasks/journal.py:28`
  Classification: domain/service candidate.
  Args: `workspace, task, message`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `load_task_journal` at `litehive/tasks/journal.py:39`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `render_task_journal` at `litehive/tasks/journal.py:79`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/tasks/normalization.py`
- `normalize_acceptance_criteria` at `litehive/tasks/normalization.py:7`
  Classification: utility.
  Args: `items`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `normalize_task_text_list` at `litehive/tasks/normalization.py:27`
  Classification: utility.
  Args: `items`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `missing_acceptance_criteria_reason` at `litehive/tasks/normalization.py:38`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `missing_acceptance_criteria_cli_warning` at `litehive/tasks/normalization.py:58`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `implementation_entry_stage` at `litehive/tasks/normalization.py:75`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `needs_normalization` at `litehive/tasks/normalization.py:91`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `reroute_stage_for_acceptance_criteria` at `litehive/tasks/normalization.py:122`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_acceptance_criteria_requirement_signals` at `litehive/tasks/normalization.py:140`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/tasks/paths.py`
- `_worktree_workspace_dir` at `litehive/tasks/paths.py:14`
  Classification: utility.
  Args: `root`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `tasks_root` at `litehive/tasks/paths.py:30`
  Classification: domain/service candidate.
  Args: `root, bootstrap`.
  Candidate owner: `Workspace, WorktreeService, or GitRepository depending on module`.
  Note: Raw path identity below boundary; migrate when touched.
- `runner_lock_path` at `litehive/tasks/paths.py:49`
  Classification: domain/service candidate.
  Args: `root`.
  Candidate owner: `Workspace, WorktreeService, or GitRepository depending on module`.
  Note: Raw path identity below boundary; migrate when touched.
- `slugify` at `litehive/tasks/paths.py:63`
  Classification: domain/service candidate.
  Args: `value, max_length`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `task_dir` at `litehive/tasks/paths.py:80`
  Classification: domain/service candidate.
  Args: `root, task, bootstrap`.
  Candidate owner: `Workspace, WorktreeService, or GitRepository depending on module`.
  Note: Raw path identity below boundary; migrate when touched.
- `task_recovery_dir` at `litehive/tasks/paths.py:91`
  Classification: domain/service candidate.
  Args: `root, task, bootstrap`.
  Candidate owner: `Workspace, WorktreeService, or GitRepository depending on module`.
  Note: Raw path identity below boundary; migrate when touched.
- `latest_path` at `litehive/tasks/paths.py:102`
  Classification: domain/service candidate.
  Args: `paths`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `_artifact_candidates` at `litehive/tasks/paths.py:116`
  Classification: utility.
  Args: `base, *names`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `resolve_artifact_path` at `litehive/tasks/paths.py:133`
  Classification: domain/service candidate.
  Args: `base, *names`.
  Candidate owner: `resolver service for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `read_text_artifact` at `litehive/tasks/paths.py:147`
  Classification: domain/service candidate.
  Args: `path`.
  Candidate owner: `reader/repository object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `latest_run_all_log_path_for_workspace` at `litehive/tasks/paths.py:161`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `latest_subagent_base_for_workspace` at `litehive/tasks/paths.py:182`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `status_entry_paths` at `litehive/tasks/paths.py:209`
  Classification: domain/service candidate.
  Args: `entries`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.

### `litehive/tasks/queue_eligibility.py`
- `_normalize_resumable_stage_name` at `litehive/tasks/queue_eligibility.py:46`
  Classification: utility.
  Args: `stage`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `resumable_queue_stage` at `litehive/tasks/queue_eligibility.py:60`
  Classification: domain/service candidate.
  Args: `task`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `resumable_running_stage` at `litehive/tasks/queue_eligibility.py:90`
  Classification: domain/service candidate.
  Args: `task`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `_needs_manual_intervention` at `litehive/tasks/queue_eligibility.py:107`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_is_recovery_budget_exhausted` at `litehive/tasks/queue_eligibility.py:132`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_should_requeue_commit_stage_task` at `litehive/tasks/queue_eligibility.py:148`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_has_terminal_execution_status` at `litehive/tasks/queue_eligibility.py:163`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_has_terminal_outcome_kind` at `litehive/tasks/queue_eligibility.py:175`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `task_has_resume_marker` at `litehive/tasks/queue_eligibility.py:188`
  Classification: domain/service candidate.
  Args: `task`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `_is_parked_task` at `litehive/tasks/queue_eligibility.py:207`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `is_task_eligible_for_execution` at `litehive/tasks/queue_eligibility.py:218`
  Classification: domain/service candidate.
  Args: `task`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `_auto_recovery_stage_for_flagged_task` at `litehive/tasks/queue_eligibility.py:246`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_is_task_completed` at `litehive/tasks/queue_eligibility.py:260`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_task_blockers` at `litehive/tasks/queue_eligibility.py:271`
  Classification: utility.
  Args: `task, tasks_by_id`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `validate_task_dependencies_for_workspace` at `litehive/tasks/queue_eligibility.py:295`
  Classification: domain/service candidate.
  Args: `workspace, task_id, depends_on`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_dependency_reaches_task` at `litehive/tasks/queue_eligibility.py:318`
  Classification: utility.
  Args: `task_id, dependency_id, tasks_by_id`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_is_interrupted_task` at `litehive/tasks/queue_eligibility.py:343`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_live_active_pipeline_stage` at `litehive/tasks/queue_eligibility.py:357`
  Classification: utility.
  Args: `state, tasks_by_id`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/tasks/queue_mutations.py`
- `enqueue_task_for_workspace` at `litehive/tasks/queue_mutations.py:32`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `enqueue_task_front_for_workspace` at `litehive/tasks/queue_mutations.py:39`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_enqueue_task_for_workspace` at `litehive/tasks/queue_mutations.py:46`
  Classification: domain/service candidate.
  Args: `workspace, task_id, front`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `move_queued_task_for_workspace` at `litehive/tasks/queue_mutations.py:86`
  Classification: domain/service candidate.
  Args: `workspace, task_id, position`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_prioritize_audit_entries` at `litehive/tasks/queue_mutations.py:129`
  Classification: utility.
  Args: `task_ids, queued_tasks, before_tasks, queue_before, queue_after`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `prioritize_queued_tasks_for_workspace` at `litehive/tasks/queue_mutations.py:162`
  Classification: domain/service candidate.
  Args: `workspace, task_ids`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `reset_task_for_recovery` at `litehive/tasks/queue_mutations.py:210`
  Classification: domain/service candidate.
  Args: `task, status, pipeline_status, clear_last_outcome`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `enqueue_recovered_task` at `litehive/tasks/queue_mutations.py:247`
  Classification: domain/service candidate.
  Args: `state, task_id`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `drop_task_from_workspace_state` at `litehive/tasks/queue_mutations.py:260`
  Classification: domain/service candidate.
  Args: `state, task_id`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `prepare_completed_task_for_recovery` at `litehive/tasks/queue_mutations.py:281`
  Classification: domain/service candidate.
  Args: `task, recovery_stage`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `canonicalize_resumable_queue_task` at `litehive/tasks/queue_mutations.py:298`
  Classification: domain/service candidate.
  Args: `task, stage`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.

### `litehive/tasks/queue_selection.py`
- `_normalize_stale_pipeline_statuses` at `litehive/tasks/queue_selection.py:61`
  Classification: utility.
  Args: `state, tasks_by_id`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `set_active_task` at `litehive/tasks/queue_selection.py:97`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `peek_next_task` at `litehive/tasks/queue_selection.py:122`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `peek_next_task_selection` at `litehive/tasks/queue_selection.py:134`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `dequeue_next_task` at `litehive/tasks/queue_selection.py:155`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `dequeue_next_task_selection` at `litehive/tasks/queue_selection.py:167`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_dependent_task_count` at `litehive/tasks/queue_selection.py:255`
  Classification: utility.
  Args: `task_id, queue, tasks_by_id`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_task_selection_key` at `litehive/tasks/queue_selection.py:288`
  Classification: utility.
  Args: `task, queue_index, queue, tasks_by_id`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_resolve_next_task_from_state` at `litehive/tasks/queue_selection.py:314`
  Classification: domain/service candidate.
  Args: `workspace, state`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `restore_missing_queued_tasks` at `litehive/tasks/queue_selection.py:343`
  Classification: domain/service candidate.
  Args: `state, tasks_by_id`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `_resolve_next_task_from_snapshot` at `litehive/tasks/queue_selection.py:372`
  Classification: utility.
  Args: `state, tasks_by_id`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `clear_active_task` at `litehive/tasks/queue_selection.py:446`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `restore_untouched_active_task` at `litehive/tasks/queue_selection.py:457`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `active_task_markers_for_workspace` at `litehive/tasks/queue_selection.py:532`
  Classification: domain/service candidate.
  Args: `workspace, state`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `validate_single_active_task_for_workspace` at `litehive/tasks/queue_selection.py:570`
  Classification: domain/service candidate.
  Args: `workspace, state`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_format_active_task_markers` at `litehive/tasks/queue_selection.py:587`
  Classification: utility.
  Args: `markers`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/tasks/recovery_engine.py`
- `resolve_recovery_engine` at `litehive/tasks/recovery_engine.py:9`
  Classification: domain/service candidate.
  Args: `workspace, task, config`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/tasks/recovery_evidence.py`
- `collect_recovery_evidence` at `litehive/tasks/recovery_evidence.py:25`
  Classification: domain/service candidate.
  Args: `workspace, task, stage`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `stage_report_context` at `litehive/tasks/recovery_evidence.py:261`
  Classification: domain/service candidate.
  Args: `report`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.

### `litehive/tasks/recovery_reports.py`
- `record_recovery_report` at `litehive/tasks/recovery_reports.py:17`
  Classification: domain/service candidate.
  Args: `workspace, task, trigger_event_kind, origin_stage, summary, runnable_state, actions, failure_classification, blocker, warnings`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/tasks/report_storage.py`
- `ReportReference.display` at `litehive/tasks/report_storage.py:27`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `ReportReference`.
  Note: Already on an object; review class responsibility before moving.
- `ReportReference.__str__` at `litehive/tasks/report_storage.py:37`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `ReportReference`.
  Note: Already a method; usually special/protocol behavior.
- `insert_recovery_report` at `litehive/tasks/report_storage.py:47`
  Classification: domain/service candidate.
  Args: `workspace, task, report`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `load_recovery_reports` at `litehive/tasks/report_storage.py:74`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `latest_recovery_report` at `litehive/tasks/report_storage.py:108`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `record_stage_report` at `litehive/tasks/report_storage.py:122`
  Classification: domain/service candidate.
  Args: `workspace, task, report`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `rewrite_latest_stage_report` at `litehive/tasks/report_storage.py:149`
  Classification: domain/service candidate.
  Args: `workspace, task, report`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `load_stage_reports_for_task_id` at `litehive/tasks/report_storage.py:192`
  Classification: domain/service candidate.
  Args: `workspace, task_id, pipeline_state`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `load_workspace_stage_reports` at `litehive/tasks/report_storage.py:207`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `load_stage_reports` at `litehive/tasks/report_storage.py:218`
  Classification: domain/service candidate.
  Args: `workspace, task, pipeline_state, stage`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `latest_stage_report` at `litehive/tasks/report_storage.py:238`
  Classification: domain/service candidate.
  Args: `workspace, task, source`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_load_stage_reports` at `litehive/tasks/report_storage.py:254`
  Classification: domain/service candidate.
  Args: `workspace, task_id, pipeline_state`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_deserialize_stage_report_payload` at `litehive/tasks/report_storage.py:300`
  Classification: utility.
  Args: `payload`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.

### `litehive/tasks/runtime.py`
- `idle_stage_state` at `litehive/tasks/runtime.py:32`
  Classification: domain/service candidate.
  Args: `updated_at, stage`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `_running_stage_state` at `litehive/tasks/runtime.py:43`
  Classification: utility.
  Args: `stage, started_at`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_runtime_subagent_state` at `litehive/tasks/runtime.py:59`
  Classification: utility.
  Args: `subagent, started_at, updated_at, pid, completed_at, exit_code, execution_trace_snippet, interruption_reason, continuation`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `clear_task_run_activity` at `litehive/tasks/runtime.py:98`
  Classification: domain/service candidate.
  Args: `task, execution_status, updated_at, clear_interruption`.
  Candidate owner: `repository/store object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `mark_task_run_started_for_workspace` at `litehive/tasks/runtime.py:125`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `apply_task_run_started` at `litehive/tasks/runtime.py:133`
  Classification: domain/service candidate.
  Args: `task`.
  Candidate owner: `runtime transition object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `mark_task_run_finished_for_workspace` at `litehive/tasks/runtime.py:145`
  Classification: domain/service candidate.
  Args: `workspace, task, final_status`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `apply_task_run_finished` at `litehive/tasks/runtime.py:157`
  Classification: domain/service candidate.
  Args: `task, final_status`.
  Candidate owner: `runtime transition object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `apply_flag_count_auto_defer` at `litehive/tasks/runtime.py:164`
  Classification: domain/service candidate.
  Args: `task`.
  Candidate owner: `runtime transition object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `finish_task_run_transition_for_workspace` at `litehive/tasks/runtime.py:180`
  Classification: domain/service candidate.
  Args: `workspace, task, final_status`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `set_task_retry_state_for_workspace` at `litehive/tasks/runtime.py:228`
  Classification: domain/service candidate.
  Args: `workspace, task, retry_count, retry_limit`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `clear_task_outcome_for_workspace` at `litehive/tasks/runtime.py:245`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_apply_task_retry_state` at `litehive/tasks/runtime.py:253`
  Classification: utility.
  Args: `task, retry_count, retry_limit`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_clear_task_outcome` at `litehive/tasks/runtime.py:270`
  Classification: utility.
  Args: `task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `mark_task_outcome_for_workspace` at `litehive/tasks/runtime.py:282`
  Classification: domain/service candidate.
  Args: `workspace, task, kind, stage, reason_code, reason, retry_count, retry_limit, follow_up_task_id, failure_classification, failure_diagnostics`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `apply_task_outcome` at `litehive/tasks/runtime.py:313`
  Classification: domain/service candidate.
  Args: `task, kind, stage, reason_code, reason, retry_count, retry_limit, follow_up_task_id, failure_classification, failure_diagnostics`.
  Candidate owner: `runtime transition object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `_normalize_failure_diagnostics` at `litehive/tasks/runtime.py:353`
  Classification: utility.
  Args: `failure_diagnostics`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `mark_stage_started_for_workspace` at `litehive/tasks/runtime.py:366`
  Classification: domain/service candidate.
  Args: `workspace, task, stage`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `apply_stage_started` at `litehive/tasks/runtime.py:374`
  Classification: domain/service candidate.
  Args: `task, stage`.
  Candidate owner: `runtime transition object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `mark_stage_finished_for_workspace` at `litehive/tasks/runtime.py:383`
  Classification: domain/service candidate.
  Args: `workspace, task, report`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `apply_stage_finished` at `litehive/tasks/runtime.py:392`
  Classification: domain/service candidate.
  Args: `task`.
  Candidate owner: `runtime transition object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `mark_subagent_started_for_workspace` at `litehive/tasks/runtime.py:405`
  Classification: domain/service candidate.
  Args: `workspace, task, ref`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `apply_subagent_started` at `litehive/tasks/runtime.py:413`
  Classification: domain/service candidate.
  Args: `task, ref`.
  Candidate owner: `runtime transition object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `mark_subagent_pid_for_workspace` at `litehive/tasks/runtime.py:422`
  Classification: domain/service candidate.
  Args: `workspace, task, pid`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `apply_subagent_pid` at `litehive/tasks/runtime.py:431`
  Classification: domain/service candidate.
  Args: `task, pid`.
  Candidate owner: `runtime transition object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `mark_subagent_progress_for_workspace` at `litehive/tasks/runtime.py:449`
  Classification: domain/service candidate.
  Args: `workspace, task, pid, transcript, continuation`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `apply_subagent_progress` at `litehive/tasks/runtime.py:464`
  Classification: domain/service candidate.
  Args: `task, pid, transcript, continuation`.
  Candidate owner: `runtime transition object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `mark_subagent_finished_for_workspace` at `litehive/tasks/runtime.py:490`
  Classification: domain/service candidate.
  Args: `workspace, task, ref, transcript, exit_code, pid, interruption_reason, continuation`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `apply_subagent_finished` at `litehive/tasks/runtime.py:508`
  Classification: domain/service candidate.
  Args: `task`.
  Candidate owner: `runtime transition object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `mark_engine_switch_for_workspace` at `litehive/tasks/runtime.py:517`
  Classification: domain/service candidate.
  Args: `workspace, task, stage, from_engine, to_engine, reason`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `apply_engine_switch` at `litehive/tasks/runtime.py:538`
  Classification: domain/service candidate.
  Args: `task, stage, from_engine, to_engine, reason`.
  Candidate owner: `runtime transition object for the module concern`.
  Note: Verb-shaped business operation; method/service candidate.
- `summarize_transcript` at `litehive/tasks/runtime.py:559`
  Classification: domain/service candidate.
  Args: `transcript, limit`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `duration_seconds` at `litehive/tasks/runtime.py:580`
  Classification: domain/service candidate.
  Args: `started_at, ended_at`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.

### `litehive/tasks/status_close.py`
- `_allowed_close_outcome_values` at `litehive/tasks/status_close.py:40`
  Classification: utility.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_abandon_task_transition` at `litehive/tasks/status_close.py:50`
  Classification: domain/service candidate.
  Args: `workspace, task_id, reason, audit_actor, audit_source`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_close_task_transition` at `litehive/tasks/status_close.py:99`
  Classification: domain/service candidate.
  Args: `workspace, task_id, outcome, reason, follow_up_task_id, audit_actor, audit_source`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_park_task_transition` at `litehive/tasks/status_close.py:185`
  Classification: domain/service candidate.
  Args: `workspace, task_id, reason, audit_actor, audit_source`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `abandon_task_for_workspace` at `litehive/tasks/status_close.py:224`
  Classification: domain/service candidate.
  Args: `workspace, task_id, reason, audit_actor, audit_source`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `close_task_for_workspace` at `litehive/tasks/status_close.py:243`
  Classification: domain/service candidate.
  Args: `workspace, task_id, outcome, reason, follow_up_task_id, audit_actor, audit_source`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `park_task_for_workspace` at `litehive/tasks/status_close.py:266`
  Classification: domain/service candidate.
  Args: `workspace, task_id, reason, audit_actor, audit_source`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/tasks/status_resume.py`
- `_requeue_task_transition` at `litehive/tasks/status_resume.py:53`
  Classification: domain/service candidate.
  Args: `workspace, task_id, front, force, audit_actor, audit_source`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_requeue_task_transition._task_checkout_path` at `litehive/tasks/status_resume.py:69`
  Classification: utility.
  Args: `task`.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `_requeue_task_transition._path_differs_from_main` at `litehive/tasks/status_resume.py:85`
  Classification: utility.
  Args: `checkout_path, main_ref, relative_path`.
  Candidate owner: `enclosing function`.
  Note: Nested helper; keep local unless the enclosing operation moves to an object.
- `_resume_task_transition` at `litehive/tasks/status_resume.py:160`
  Classification: domain/service candidate.
  Args: `workspace, task_id, front`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `requeue_task_for_workspace` at `litehive/tasks/status_resume.py:226`
  Classification: domain/service candidate.
  Args: `workspace, task_id, front, force, audit_actor, audit_source`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `resume_task_for_workspace` at `litehive/tasks/status_resume.py:247`
  Classification: domain/service candidate.
  Args: `workspace, task_id, front`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/tasks/status_update.py`
- `_update_task_transition` at `litehive/tasks/status_update.py:36`
  Classification: domain/service candidate.
  Args: `workspace, task_id, title, depends_on, model, retry_limit, priority, goal, acceptance_criteria, constraints, plan, auto_commit, outcome, outcome_reason, action, allow_active_agent_task_mutation, journal_message, audit_actor, audit_source`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `update_task_for_workspace` at `litehive/tasks/status_update.py:201`
  Classification: domain/service candidate.
  Args: `workspace, task_id, title, depends_on, model, retry_limit, priority, goal, acceptance_criteria, constraints, plan, auto_commit, outcome, outcome_reason, action, allow_active_agent_task_mutation, journal_message, audit_actor, audit_source`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/tasks/stop.py`
- `_active_task_id_for_stop` at `litehive/tasks/stop.py:40`
  Classification: domain/service candidate.
  Args: `workspace, state`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_stop_active_task_without_runner_guard` at `litehive/tasks/stop.py:57`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `stop_current_task` at `litehive/tasks/stop.py:135`
  Classification: domain/service candidate.
  Args: `workspace, wait_timeout_seconds, poll_interval_seconds`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/tasks/switch_engine.py`
- `_effective_task_engine` at `litehive/tasks/switch_engine.py:33`
  Classification: utility.
  Args: `default_engine, task`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_switch_prior_work_paths` at `litehive/tasks/switch_engine.py:50`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_switch_activity_entry_message` at `litehive/tasks/switch_engine.py:72`
  Classification: utility.
  Args: `task, reason, previous_engine, new_engine, prior_work_paths`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `switch_task_engine_for_workspace` at `litehive/tasks/switch_engine.py:100`
  Classification: domain/service candidate.
  Args: `workspace, task_id, engine, reason, audit_actor, audit_source`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/workspace.py`
- `Workspace.__init__` at `litehive/workspace.py:56`
  Classification: utility/protocol method.
  Args: `self, root`.
  Candidate owner: `Workspace`.
  Note: Already a method; usually special/protocol behavior.
- `Workspace.__repr__` at `litehive/workspace.py:67`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `Workspace`.
  Note: Already a method; usually special/protocol behavior.
- `Workspace.__eq__` at `litehive/workspace.py:70`
  Classification: utility/protocol method.
  Args: `self, other`.
  Candidate owner: `Workspace`.
  Note: Already a method; usually special/protocol behavior.
- `Workspace.__hash__` at `litehive/workspace.py:75`
  Classification: utility/protocol method.
  Args: `self`.
  Candidate owner: `Workspace`.
  Note: Already a method; usually special/protocol behavior.
- `Workspace.from_path` at `litehive/workspace.py:79`
  Classification: domain/service method.
  Args: `cls, root`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.
- `Workspace.connect` at `litehive/workspace.py:96`
  Classification: domain/service method.
  Args: `self, migrate`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.
- `Workspace.load_config` at `litehive/workspace.py:110`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.
- `Workspace.config` at `litehive/workspace.py:132`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.
- `Workspace.require_existing` at `litehive/workspace.py:138`
  Classification: domain/service method.
  Args: `self, source`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.
- `Workspace.create` at `litehive/workspace.py:151`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.
- `Workspace.runtime_dir` at `litehive/workspace.py:163`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.
- `Workspace.runtime_path` at `litehive/workspace.py:174`
  Classification: domain/service method.
  Args: `self, *parts`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.
- `Workspace.control_dir` at `litehive/workspace.py:186`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.
- `Workspace.control_files` at `litehive/workspace.py:198`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.
- `Workspace.task_dir` at `litehive/workspace.py:208`
  Classification: domain/service method.
  Args: `self, task, bootstrap`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.
- `Workspace.list_tasks` at `litehive/workspace.py:224`
  Classification: domain/service method.
  Args: `self, include_runtime, strict`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.
- `Workspace.get_task` at `litehive/workspace.py:238`
  Classification: domain/service method.
  Args: `self, task_id`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.
- `Workspace.get_task_record` at `litehive/workspace.py:247`
  Classification: domain/service method.
  Args: `self, task_id`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.
- `Workspace.require_task` at `litehive/workspace.py:256`
  Classification: domain/service method.
  Args: `self, task_id`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.
- `Workspace.save_task` at `litehive/workspace.py:265`
  Classification: domain/service method.
  Args: `self, task`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.
- `Workspace.task_activity` at `litehive/workspace.py:274`
  Classification: domain/service method.
  Args: `self, task`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.
- `Workspace.append_event` at `litehive/workspace.py:288`
  Classification: domain/service method.
  Args: `self, task, event`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.
- `Workspace.load_subagent_session` at `litehive/workspace.py:298`
  Classification: domain/service method.
  Args: `self, task_id, subagent_id`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.
- `Workspace.load_subagent_session_record` at `litehive/workspace.py:308`
  Classification: domain/service method.
  Args: `self, task_id, subagent_id`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.
- `Workspace.load_subagent_session_created_at` at `litehive/workspace.py:318`
  Classification: domain/service method.
  Args: `self, task_id, subagent_id`.
  Candidate owner: `Workspace`.
  Note: Already on an object; review class responsibility before moving.

### `litehive/worktree/cleanup.py`
- `cleanup_terminal_task_worktree_for_workspace` at `litehive/worktree/cleanup.py:33`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `collect_managed_worktrees_for_workspace` at `litehive/worktree/cleanup.py:55`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `remove_cleanable_worktrees_for_workspace` at `litehive/worktree/cleanup.py:108`
  Classification: domain/service candidate.
  Args: `workspace, dry_run`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/worktree/execution_root.py`
- `resolve_task_execution_root_for_workspace` at `litehive/worktree/execution_root.py:35`
  Classification: domain/service candidate.
  Args: `workspace, task, config`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/worktree/inspection.py`
- `inspect_dirty_worktree_gate` at `litehive/worktree/inspection.py:37`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `dirty_entry_paths` at `litehive/worktree/inspection.py:116`
  Classification: domain/service candidate.
  Args: `dirty_entries`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `worktree_uncommitted_changes` at `litehive/worktree/inspection.py:140`
  Classification: domain/service candidate.
  Args: `worktree_path`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `worktree_committed_changes_for_workspace` at `litehive/worktree/inspection.py:156`
  Classification: domain/service candidate.
  Args: `workspace, worktree_path`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_allowed_commit_paths` at `litehive/worktree/inspection.py:173`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_unexpected_dirty_paths` at `litehive/worktree/inspection.py:191`
  Classification: utility.
  Args: `dirty_entries, allowed_paths`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_path_is_within_allowed_paths` at `litehive/worktree/inspection.py:224`
  Classification: utility.
  Args: `raw, allowed_paths`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_task_can_resume_with_owned_dirty_paths` at `litehive/worktree/inspection.py:242`
  Classification: domain/service candidate.
  Args: `workspace, task, dirty_entries`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.

### `litehive/worktree/paths.py`
- `task_worktree_path_for_workspace` at `litehive/worktree/paths.py:30`
  Classification: domain/service candidate.
  Args: `workspace, task`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `task_worktree_branch` at `litehive/worktree/paths.py:43`
  Classification: domain/service candidate.
  Args: `task`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `is_managed_worktree_path_for_workspace` at `litehive/worktree/paths.py:54`
  Classification: domain/service candidate.
  Args: `workspace, worktree_path`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `resolve_recorded_worktree_path_for_workspace` at `litehive/worktree/paths.py:75`
  Classification: domain/service candidate.
  Args: `workspace, worktree_path`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `serialize_worktree_path` at `litehive/worktree/paths.py:97`
  Classification: domain/service candidate.
  Args: `path`.
  Candidate owner: `new focused service for module concern`.
  Note: Public module function in a domain/service package.
- `ensure_worktree_venv_link_for_workspace` at `litehive/worktree/paths.py:109`
  Classification: domain/service candidate.
  Args: `workspace, worktree_path`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.

### `litehive/worktree/rescue.py`
- `collect_rescue_candidates_for_workspace` at `litehive/worktree/rescue.py:54`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `require_clean_main_checkout_for_workspace` at `litehive/worktree/rescue.py:88`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `apply_rescue_candidate_for_workspace` at `litehive/worktree/rescue.py:106`
  Classification: domain/service candidate.
  Args: `workspace, candidate`.
  Candidate owner: `Workspace or focused workspace service`.
  Note: Workspace-scoped behavior; likely should be bound to an object.
- `_worktree_commits_ahead_of_main_for_workspace` at `litehive/worktree/rescue.py:311`
  Classification: domain/service candidate.
  Args: `workspace, worktree_path`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_worktree_patch_already_on_main_for_workspace` at `litehive/worktree/rescue.py:328`
  Classification: domain/service candidate.
  Args: `workspace, wt_head, main_head`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_is_task_metadata_path` at `litehive/worktree/rescue.py:343`
  Classification: utility.
  Args: `path, task_id`.
  Candidate owner: `none`.
  Note: Pure helper or boundary helper; keep free unless it grows state.
- `_resolve_metadata_conflicts_for_workspace` at `litehive/worktree/rescue.py:357`
  Classification: domain/service candidate.
  Args: `workspace, paths`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_drop_task_metadata_changes_for_workspace` at `litehive/worktree/rescue.py:379`
  Classification: domain/service candidate.
  Args: `workspace, task_id`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_finalize_rescue_for_workspace` at `litehive/worktree/rescue.py:393`
  Classification: domain/service candidate.
  Args: `workspace, task, outcome, head_sha`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_ensure_unmerged_worktree_state_for_workspace` at `litehive/worktree/rescue.py:432`
  Classification: domain/service candidate.
  Args: `workspace, task_id, worktree_rel`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_stash_litehive_changes_for_workspace` at `litehive/worktree/rescue.py:450`
  Classification: domain/service candidate.
  Args: `workspace`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_restore_litehive_changes_for_workspace` at `litehive/worktree/rescue.py:476`
  Classification: domain/service candidate.
  Args: `workspace, stash_ref`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.
- `_worktree_has_non_metadata_changes_for_workspace` at `litehive/worktree/rescue.py:495`
  Classification: domain/service candidate.
  Args: `workspace, worktree_path, task_id`.
  Candidate owner: `Workspace or focused service`.
  Note: Private/helper-shaped, but it carries workspace identity.

### `litehive/worktree/service.py`
- `status_porcelain_untracked` at `litehive/worktree/service.py:70`
  Classification: domain/service candidate.
  Args: `cwd`.
  Candidate owner: `WorktreeService`.
  Note: Public module function in a domain/service package.
- `WorktreeService.__init__` at `litehive/worktree/service.py:94`
  Classification: utility/protocol method.
  Args: `self, workspace`.
  Candidate owner: `WorktreeService`.
  Note: Already a method; usually special/protocol behavior.
- `WorktreeService.sync_task_worktree` at `litehive/worktree/service.py:104`
  Classification: domain/service method.
  Args: `self, task_id, entry_stage, worktree_resolver, resolver_state, main_ref`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeService.collect_managed_worktrees` at `litehive/worktree/service.py:167`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeService.remove_cleanable_worktrees` at `litehive/worktree/service.py:177`
  Classification: domain/service method.
  Args: `self, dry_run`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeService.collect_rescue_candidates` at `litehive/worktree/service.py:187`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeService.apply_rescue_candidate` at `litehive/worktree/service.py:197`
  Classification: domain/service method.
  Args: `self, candidate`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeService.inspect_task_worktree` at `litehive/worktree/service.py:207`
  Classification: domain/service method.
  Args: `self, task`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeService.task_has_missing_recorded_worktree` at `litehive/worktree/service.py:236`
  Classification: domain/service method.
  Args: `self, task_id`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeService.clear_missing_recorded_worktree` at `litehive/worktree/service.py:251`
  Classification: domain/service method.
  Args: `self, task_id`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeService.cleanup_terminal_task_worktree` at `litehive/worktree/service.py:266`
  Classification: domain/service method.
  Args: `self, task`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeService.require_clean_main_checkout` at `litehive/worktree/service.py:276`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeService.prune_stale_worktrees` at `litehive/worktree/service.py:287`
  Classification: domain/service method.
  Args: `self`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeService.registered_worktree_for_branch` at `litehive/worktree/service.py:299`
  Classification: domain/service method.
  Args: `self, branch`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeService._resolved_lifecycle_worktree` at `litehive/worktree/service.py:330`
  Classification: domain/service method.
  Args: `self, recorded, worktree_resolver, resolver_state`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeService._rebase_existing_worktree_onto_local_main` at `litehive/worktree/service.py:349`
  Classification: domain/service method.
  Args: `self, worktree`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeService._merge_origin_main` at `litehive/worktree/service.py:377`
  Classification: domain/service method.
  Args: `self, worktree, main_ref`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeService._head` at `litehive/worktree/service.py:417`
  Classification: domain/service method.
  Args: `worktree`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeService._is_dirty` at `litehive/worktree/service.py:428`
  Classification: domain/service method.
  Args: `worktree`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeService._has_origin` at `litehive/worktree/service.py:439`
  Classification: domain/service method.
  Args: `worktree`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeService._unresolved` at `litehive/worktree/service.py:451`
  Classification: domain/service method.
  Args: `worktree`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeService._stash_local_changes` at `litehive/worktree/service.py:464`
  Classification: domain/service method.
  Args: `worktree`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.
- `WorktreeService._restore_local_changes` at `litehive/worktree/service.py:487`
  Classification: domain/service method.
  Args: `self, worktree, stash_ref`.
  Candidate owner: `WorktreeService`.
  Note: Already on an object; review class responsibility before moving.

## Class Inventory

### `litehive/agents/artifacts.py`
- `ArtifactService` at `litehive/agents/artifacts.py:27`
  Purpose: Persist file-backed subagent debug evidence under one artifact root.
  Bases: `-`.
  Methods:
  - `__init__`
  - `write_stream`
  - `write_text`
  - `remove_text`
  Domain doc cross-check: not a domain-doc class.

### `litehive/agents/callbacks.py`
- `SubagentPidRecorder` at `litehive/agents/callbacks.py:16`
  Purpose: Object that can persist the live PID for a subagent.
  Bases: `Protocol`.
  Methods:
  - `record_subagent_pid`
  Domain doc cross-check: not a domain-doc class.
- `ProgressSnapshotWriter` at `litehive/agents/callbacks.py:28`
  Purpose: Object that can persist one live progress snapshot.
  Bases: `Protocol`.
  Methods:
  - `write_session_progress`
  Domain doc cross-check: not a domain-doc class.
- `CallbackWarnings` at `litehive/agents/callbacks.py:49`
  Purpose: Collect non-fatal callback bookkeeping warnings for one subagent run.
  Bases: `-`.
  Methods:
  - `record_failure`
  - `merged_with`
  Domain doc cross-check: not a domain-doc class.
- `SubagentRunCallbacks` at `litehive/agents/callbacks.py:94`
  Purpose: Safe engine callbacks for one subagent process.
  Bases: `-`.
  Methods:
  - `on_started`
  - `on_update`
  Domain doc cross-check: mentioned by name.

### `litehive/agents/engine_manager.py`
- `EngineManager` at `litehive/agents/engine_manager.py:15`
  Purpose: Resolve heru engine adapters and model overrides for subagent runs.
  Bases: `-`.
  Methods:
  - `engine_for`
  - `resume_safe_model`
  Domain doc cross-check: mentioned by name.

### `litehive/agents/execution_trace.py`
- `ExecutionTraceView` at `litehive/agents/execution_trace.py:24`
  Purpose: Human-readable execution trace plus the artifact it was derived from.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `ParsedUnifiedEvents` at `litehive/agents/execution_trace.py:33`
  Purpose: Parsed unified events recovered from an engine stdout buffer.
  Bases: `-`.
  Methods:
  - `__bool__`
  Domain doc cross-check: not a domain-doc class.

### `litehive/agents/manager.py`
- `SubagentRunContext` at `litehive/agents/manager.py:67`
  Purpose: Prepared filesystem/task state for one subagent invocation.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `EngineProcessResult` at `litehive/agents/manager.py:81`
  Purpose: Adapter selected for execution plus the process result it returned.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `EngineRunOutcome` at `litehive/agents/manager.py:91`
  Purpose: Result of invoking and classifying one engine process.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `SubagentStartupError` at `litehive/agents/manager.py:168`
  Purpose: Launch-boundary failure raised by ``SubagentManager``.
  Bases: `RuntimeError`.
  Methods:
  - `__init__`
  Domain doc cross-check: mentioned by name.
- `SubagentManager` at `litehive/agents/manager.py:198`
  Purpose: Run external CLI subagents inside a task-scoped folder.
  Bases: `-`.
  Methods:
  - `__init__`
  - `run`
  - `_prepare_subagent_run`
  - `_execute_subagent_engine`
  - `_run_engine_process`
  - `_run_live_engine_process`
  - `_run_single_engine_process`
  - `_classify_completed_execution`
  - `_finalize_subagent_run`
  - `_write_session_finish`
  - `write_session_progress`
  - `_parse_execution_report`
  Domain doc cross-check: mentioned by name.

### `litehive/agents/report_extraction.py`
- `MissingVerdictError` at `litehive/agents/report_extraction.py:19`
  Purpose: The subagent finished its turn without submitting a verdict.
  Bases: `Exception`.
  Methods:
  - `__init__`
  Domain doc cross-check: not a domain-doc class.

### `litehive/agents/report_submission.py`
- `AgentReportSubmissionError` at `litehive/agents/report_submission.py:15`
  Purpose: Inferred from `litehive/agents/report_submission.py` and class name: owns the AgentReportSubmissionError concept.
  Bases: `Exception`.
  Methods:
  - `__str__`
  Domain doc cross-check: not a domain-doc class.
- `AgentReportIdentity` at `litehive/agents/report_submission.py:24`
  Purpose: Inferred from `litehive/agents/report_submission.py` and class name: owns the AgentReportIdentity concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `AgentReportRequest` at `litehive/agents/report_submission.py:30`
  Purpose: Inferred from `litehive/agents/report_submission.py` and class name: owns the AgentReportRequest concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `AgentReportSubmission` at `litehive/agents/report_submission.py:41`
  Purpose: Inferred from `litehive/agents/report_submission.py` and class name: owns the AgentReportSubmission concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `AgentReportSubmitter` at `litehive/agents/report_submission.py:53`
  Purpose: Inferred from `litehive/agents/report_submission.py` and class name: owns the AgentReportSubmitter concept.
  Bases: `-`.
  Methods:
  - `submit`
  - `_load_task`
  - `_resolve_identity`
  - `_check_verdict`
  - `_resolve_follow_up_task`
  - `_load_pipeline_stage`
  - `_resolve_stage`
  Domain doc cross-check: not a domain-doc class.

### `litehive/agents/session.py`
- `SubagentSessionManager` at `litehive/agents/session.py:44`
  Purpose: Persist subagent session state and stream artifacts for one manager.
  Bases: `-`.
  Methods:
  - `session_storage_fields`
  - `extract_execution_continuation`
  - `extract_execution_event_stream`
  - `append_stream_delta`
  - `write_session_start`
  - `write_running_session_metadata`
  - `record_subagent_pid`
  - `subagent_inactivity_timeout_seconds`
  - `completed_inactivity_timeout`
  - `check_stdout_inactivity`
  - `terminate_stale_pid`
  - `write_event_stream`
  - `write_session_snapshot`
  - `write_snapshot_artifacts`
  - `session_row_for_snapshot`
  Domain doc cross-check: mentioned by name.

### `litehive/agents/session_continuation.py`
- `SubagentContinuationState` at `litehive/agents/session_continuation.py:9`
  Purpose: Continuation state carried by report/session snapshot objects.
  Bases: `Protocol`.
  Methods:
  - `payload`
  Domain doc cross-check: not a domain-doc class.
- `NoSubagentContinuation` at `litehive/agents/session_continuation.py:21`
  Purpose: Explicit state for a subagent turn without a continuation token.
  Bases: `-`.
  Methods:
  - `payload`
  Domain doc cross-check: not a domain-doc class.
- `CapturedSubagentContinuation` at `litehive/agents/session_continuation.py:34`
  Purpose: Explicit state for a subagent turn with a captured continuation token.
  Bases: `-`.
  Methods:
  - `payload`
  Domain doc cross-check: not a domain-doc class.

### `litehive/agents/session_events.py`
- `SubagentStartedEvent` at `litehive/agents/session_events.py:10`
  Purpose: Persisted when a subagent session is allocated before engine launch.
  Bases: `-`.
  Methods:
  - `kind`
  - `data`
  Domain doc cross-check: not a domain-doc class.
- `SubagentPidEvent` at `litehive/agents/session_events.py:43`
  Purpose: Persisted when the runner learns the external engine process pid.
  Bases: `-`.
  Methods:
  - `kind`
  - `data`
  Domain doc cross-check: not a domain-doc class.
- `SubagentProgressEvent` at `litehive/agents/session_events.py:69`
  Purpose: Persisted when a live progress snapshot is written.
  Bases: `-`.
  Methods:
  - `kind`
  - `data`
  Domain doc cross-check: not a domain-doc class.
- `SubagentFinishedEvent` at `litehive/agents/session_events.py:95`
  Purpose: Persisted when a subagent reaches terminal session persistence.
  Bases: `-`.
  Methods:
  - `kind`
  - `data`
  Domain doc cross-check: not a domain-doc class.

### `litehive/agents/session_inactivity.py`
- `SubagentInactivityTimeoutPolicy` at `litehive/agents/session_inactivity.py:26`
  Purpose: Timeout rules for live and completed subagent executions.
  Bases: `-`.
  Methods:
  - `live_timeout_seconds`
  - `completed_timeout`
  Domain doc cross-check: not a domain-doc class.
- `SubagentInactivityMonitor` at `litehive/agents/session_inactivity.py:62`
  Purpose: Watch stdout activity and terminate stale subagent engine processes.
  Bases: `-`.
  Methods:
  - `live_timeout_seconds`
  - `completed_timeout`
  - `check_stdout_inactivity`
  - `terminate_stale_pid`
  Domain doc cross-check: not a domain-doc class.

### `litehive/agents/session_reports.py`
- `SubagentReportPayload` at `litehive/agents/session_reports.py:12`
  Purpose: Structured report slice persisted inside ``subagent_sessions``.
  Bases: `-`.
  Methods:
  - `as_dict`
  - `continuation_payload`
  Domain doc cross-check: not a domain-doc class.

### `litehive/agents/session_snapshots.py`
- `SubagentSessionMetadata` at `litehive/agents/session_snapshots.py:12`
  Purpose: Metadata slice stored with a subagent session row.
  Bases: `-`.
  Methods:
  - `continuation_payload`
  Domain doc cross-check: not a domain-doc class.
- `RunningSubagentSessionMetadata` at `litehive/agents/session_snapshots.py:33`
  Purpose: Metadata-only update for a running subagent session.
  Bases: `-`.
  Methods:
  - `continuation_payload`
  Domain doc cross-check: not a domain-doc class.
- `SubagentSessionStorageFields` at `litehive/agents/session_snapshots.py:55`
  Purpose: Common persisted fields for one subagent session row.
  Bases: `-`.
  Methods:
  - `as_dict`
  Domain doc cross-check: not a domain-doc class.
- `RunningSubagentSessionRow` at `litehive/agents/session_snapshots.py:88`
  Purpose: Persisted session row while a subagent is still running.
  Bases: `-`.
  Methods:
  - `as_dict`
  Domain doc cross-check: not a domain-doc class.
- `TerminalSubagentSessionRow` at `litehive/agents/session_snapshots.py:118`
  Purpose: Persisted session row after the engine process has exited.
  Bases: `-`.
  Methods:
  - `as_dict`
  Domain doc cross-check: not a domain-doc class.
- `InterruptedSubagentSessionRow` at `litehive/agents/session_snapshots.py:146`
  Purpose: Persisted session row for a paused subagent that may be resumed.
  Bases: `-`.
  Methods:
  - `as_dict`
  Domain doc cross-check: not a domain-doc class.
- `SubagentSessionSnapshot` at `litehive/agents/session_snapshots.py:179`
  Purpose: Complete subagent snapshot written by ``SubagentSessionManager``.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/agents/session_store.py`
- `SubagentArtifactSlice` at `litehive/agents/session_store.py:16`
  Purpose: Named top-level slices inside one subagent artifact payload.
  Bases: `str, Enum`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `LoadedSubagentSession` at `litehive/agents/session_store.py:27`
  Purpose: Typed view of one loaded engine-session metadata slice.
  Bases: `-`.
  Methods:
  - `from_payload`
  - `__bool__`
  - `subagent_id`
  - `role`
  - `updated_at`
  - `exit_code`
  - `_non_empty_string`
  Domain doc cross-check: not a domain-doc class.
- `SerializableSubagentSession` at `litehive/agents/session_store.py:89`
  Purpose: Concrete session row object accepted by the persistence boundary.
  Bases: `Protocol`.
  Methods:
  - `as_dict`
  Domain doc cross-check: not a domain-doc class.
- `SerializableSubagentReport` at `litehive/agents/session_store.py:98`
  Purpose: Concrete report payload object accepted by the persistence boundary.
  Bases: `Protocol`.
  Methods:
  - `as_dict`
  Domain doc cross-check: not a domain-doc class.
- `SubagentArtifactPayload` at `litehive/agents/session_store.py:107`
  Purpose: Explicit wrapper for legacy session/report payload dictionaries.
  Bases: `-`.
  Methods:
  - `as_dict`
  Domain doc cross-check: not a domain-doc class.
- `SubagentEventStreamPayload` at `litehive/agents/session_store.py:119`
  Purpose: Typed event-stream payload accepted by the persistence boundary.
  Bases: `-`.
  Methods:
  - `as_dict`
  Domain doc cross-check: not a domain-doc class.
- `SubagentArtifactStore` at `litehive/agents/session_store.py:131`
  Purpose: Persistence handle for one subagent belonging to one workspace task.
  Bases: `-`.
  Methods:
  - `load_all`
  - `load_session_record`
  - `load_session`
  - `load_report`
  - `load_event_stream`
  - `save`
  - `_load_slice`
  Domain doc cross-check: not a domain-doc class.

### `litehive/agents/session_streams.py`
- `SubagentStreamLog` at `litehive/agents/session_streams.py:11`
  Purpose: Track append-only stdout/stderr offsets for live subagent sessions.
  Bases: `-`.
  Methods:
  - `ensure`
  - `append_delta`
  Domain doc cross-check: not a domain-doc class.

### `litehive/agents/subagent_ids.py`
- `SubagentIdRepository` at `litehive/agents/subagent_ids.py:14`
  Purpose: Allocate task-scoped ``SA-NNNN`` ids from SQLite.
  Bases: `-`.
  Methods:
  - `__init__`
  - `reserve_next_id`
  - `_counter_next_number`
  - `_session_next_number`
  - `_save_counter_next_number`
  Domain doc cross-check: not a domain-doc class.

### `litehive/agents/task_mutation.py`
- `AgentTaskMutationError` at `litehive/agents/task_mutation.py:15`
  Purpose: Inferred from `litehive/agents/task_mutation.py` and class name: owns the AgentTaskMutationError concept.
  Bases: `Exception`.
  Methods:
  - `__str__`
  Domain doc cross-check: not a domain-doc class.
- `AgentTaskMutationTarget` at `litehive/agents/task_mutation.py:24`
  Purpose: Inferred from `litehive/agents/task_mutation.py` and class name: owns the AgentTaskMutationTarget concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `AgentTaskUpdateRequest` at `litehive/agents/task_mutation.py:31`
  Purpose: Inferred from `litehive/agents/task_mutation.py` and class name: owns the AgentTaskUpdateRequest concept.
  Bases: `-`.
  Methods:
  - `has_changes`
  Domain doc cross-check: not a domain-doc class.
- `AgentTaskCloseRequest` at `litehive/agents/task_mutation.py:49`
  Purpose: Inferred from `litehive/agents/task_mutation.py` and class name: owns the AgentTaskCloseRequest concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `AgentTaskMutationAuthorizer` at `litehive/agents/task_mutation.py:55`
  Purpose: Inferred from `litehive/agents/task_mutation.py` and class name: owns the AgentTaskMutationAuthorizer concept.
  Bases: `-`.
  Methods:
  - `authorize`
  - `_authorized_role`
  - `_resolve_workspace`
  Domain doc cross-check: not a domain-doc class.
- `AgentTaskMutator` at `litehive/agents/task_mutation.py:100`
  Purpose: Applies authorized task mutations requested by an in-pipeline agent.
  Bases: `-`.
  Methods:
  - `update`
  - `close`
  Domain doc cross-check: not a domain-doc class.

### `litehive/attention.py`
- `OperatorNeededState` at `litehive/attention.py:40`
  Purpose: "Is operator action required?" snapshot rendered by status surfaces.
  Bases: `-`.
  Methods:
  - `needed`
  Domain doc cross-check: not a domain-doc class.
- `AttentionLogEntry` at `litehive/attention.py:68`
  Purpose: One row of the ``attention_log`` table.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `AttentionRepository` at `litehive/attention.py:83`
  Purpose: SQLite repository for free-form operator-attention diagnostics.
  Bases: `-`.
  Methods:
  - `__init__`
  - `append`
  Domain doc cross-check: not a domain-doc class.

### `litehive/cli/runner.py`
- `_RunCommandIteration` at `litehive/cli/runner.py:183`
  Purpose: One pass of :func:`run_once`.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/cli/workspace.py`
- `_QuotaHealth` at `litehive/cli/workspace.py:245`
  Purpose: Inferred from `litehive/cli/workspace.py` and class name: owns the _QuotaHealth concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/config/engine_models.py`
- `EngineSkip` at `litehive/config/engine_models.py:31`
  Purpose: Diagnostic entry for one candidate engine that selection bypassed.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `EngineSelection` at `litehive/config/engine_models.py:45`
  Purpose: Result of resolving an executable engine/model pair for a task.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `EngineSelectionRequest` at `litehive/config/engine_models.py:66`
  Purpose: Optional controls for one engine-selection pass.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/config/engine_quota.py`
- `QuotaWindow` at `litehive/config/engine_quota.py:18`
  Purpose: Inferred from `litehive/config/engine_quota.py` and class name: owns the QuotaWindow concept.
  Bases: `Protocol`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `QuotaStatus` at `litehive/config/engine_quota.py:22`
  Purpose: Inferred from `litehive/config/engine_quota.py` and class name: owns the QuotaStatus concept.
  Bases: `Protocol`.
  Methods:
  - `limit_reached`
  - `short_term`
  - `long_term`
  Domain doc cross-check: not a domain-doc class.
- `EngineQuotaBlock` at `litehive/config/engine_quota.py:39`
  Purpose: Present quota block returned by vendor quota probes.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/config/environment.py`
- `LitehiveEnvironment` at `litehive/config/environment.py:9`
  Purpose: Values Litehive reads from the process environment at CLI boundaries.
  Bases: `-`.
  Methods:
  - `from_process`
  - `from_mapping`
  Domain doc cross-check: not a domain-doc class.

### `litehive/config/model.py`
- `SandboxCredentialInput` at `litehive/config/model.py:99`
  Purpose: Inferred from `litehive/config/model.py` and class name: owns the SandboxCredentialInput concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `ExternalEngineSandboxPolicy` at `litehive/config/model.py:105`
  Purpose: Inferred from `litehive/config/model.py` and class name: owns the ExternalEngineSandboxPolicy concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `ResolvedExternalEngineSandboxPolicy` at `litehive/config/model.py:124`
  Purpose: Inferred from `litehive/config/model.py` and class name: owns the ResolvedExternalEngineSandboxPolicy concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `DaemonConfig` at `litehive/config/model.py:136`
  Purpose: Inferred from `litehive/config/model.py` and class name: owns the DaemonConfig concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `ExternalEngineSandboxConfig` at `litehive/config/model.py:147`
  Purpose: Inferred from `litehive/config/model.py` and class name: owns the ExternalEngineSandboxConfig concept.
  Bases: `-`.
  Methods:
  - `policy_for_engine`
  Domain doc cross-check: not a domain-doc class.
- `LitehiveConfig` at `litehive/config/model.py:195`
  Purpose: Workspace-level configuration aggregate for Litehive.
  Bases: `-`.
  Methods:
  - `__post_init__`
  - `model_for_engine`
  - `engine_attempt_order`
  Domain doc cross-check: not a domain-doc class.

### `litehive/config/profiles/model.py`
- `ProcessProfile` at `litehive/config/profiles/model.py:17`
  Purpose: Resolved process profile consumed by rendering and prompt assembly.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/config/runtime_settings.py`
- `RuntimeSettingKey` at `litehive/config/runtime_settings.py:24`
  Purpose: Audited runtime-setting keys stored in SQLite.
  Bases: `StringEnum`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `RuntimeSettingChange` at `litehive/config/runtime_settings.py:54`
  Purpose: Inferred from `litehive/config/runtime_settings.py` and class name: owns the RuntimeSettingChange concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `RuntimeSettingAuditEntry` at `litehive/config/runtime_settings.py:62`
  Purpose: Inferred from `litehive/config/runtime_settings.py` and class name: owns the RuntimeSettingAuditEntry concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/config/workspace_files.py`
- `WorkspaceControlFiles` at `litehive/config/workspace_files.py:17`
  Purpose: Bound paths for one workspace's repo-local ``.litehive`` files.
  Bases: `-`.
  Methods:
  - `directory`
  - `config`
  - `context`
  - `gitignore`
  Domain doc cross-check: not a domain-doc class.

### `litehive/container.py`
- `LitehiveContainer` at `litehive/container.py:17`
  Purpose: Ready dependency graph for one workspace.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `PipelineContainer` at `litehive/container.py:31`
  Purpose: Pipeline diagnostics and mutation stores for one workspace.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `DaemonContainer` at `litehive/container.py:42`
  Purpose: Daemon-loop dependencies for one workspace.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/daemon/execution.py`
- `DaemonStatusSnapshot` at `litehive/daemon/execution.py:92`
  Purpose: Status state plus rendered daemon-loop text for one observation.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `DaemonOutput` at `litehive/daemon/execution.py:107`
  Purpose: Stream-bound renderer for daemon-loop and child-process output.
  Bases: `-`.
  Methods:
  - `__init__`
  - `line`
  - `child_line`
  - `runner_wait`
  Domain doc cross-check: not a domain-doc class.
- `DaemonExecutor` at `litehive/daemon/execution.py:394`
  Purpose: Executes one daemon loop from injected workspace dependencies.
  Bases: `-`.
  Methods:
  - `run`
  Domain doc cross-check: not a domain-doc class.

### `litehive/daemon/logs.py`
- `DaemonLogs` at `litehive/daemon/logs.py:25`
  Purpose: Run-all log path helper bound to one workspace.
  Bases: `-`.
  Methods:
  - `run_all_base`
  - `prepare_session`
  - `prune_sessions`
  - `latest_run_all_dir`
  - `latest_matching`
  Domain doc cross-check: not a domain-doc class.

### `litehive/daemon/registry.py`
- `DaemonRegistryEntry` at `litehive/daemon/registry.py:41`
  Purpose: Typed daemon registration row exposed to daemon/status consumers.
  Bases: `-`.
  Methods:
  - `from_metadata`
  Domain doc cross-check: not a domain-doc class.

### `litehive/db/schema.py`
- `Migration` at `litehive/db/schema.py:65`
  Purpose: Inferred from `litehive/db/schema.py` and class name: owns the Migration concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `MigrationStatus` at `litehive/db/schema.py:72`
  Purpose: Inferred from `litehive/db/schema.py` and class name: owns the MigrationStatus concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `MigrationPlan` at `litehive/db/schema.py:79`
  Purpose: Inferred from `litehive/db/schema.py` and class name: owns the MigrationPlan concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `MigrationApplyError` at `litehive/db/schema.py:85`
  Purpose: Raised when a single schema migration fails.
  Bases: `RuntimeError`.
  Methods:
  - `__init__`
  Domain doc cross-check: not a domain-doc class.

### `litehive/domain/agent.py`
- `ExecutionTrace` at `litehive/domain/agent.py:25`
  Purpose: Rendered subagent trace split into logical chunks.
  Bases: `-`.
  Methods:
  - `from_text`
  - `text`
  - `__bool__`
  Domain doc cross-check: module covered: subagent execution result models and exceptions.
- `EngineFailure` at `litehive/domain/agent.py:56`
  Purpose: Engine-side failure description attached to a ``SubagentResult``.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: mentioned by name.
- `SubagentResult` at `litehive/domain/agent.py:73`
  Purpose: Single-run summary the lifecycle layer reads back from the runner.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: mentioned by name.
- `SubagentInactivityTimeout` at `litehive/domain/agent.py:96`
  Purpose: Raised when a live subagent stops producing stdout for too long.
  Bases: `RuntimeError`.
  Methods:
  - `__init__`
  Domain doc cross-check: module covered: subagent execution result models and exceptions.

### `litehive/domain/common.py`
- `StringEnum` at `litehive/domain/common.py:29`
  Purpose: Base class for string-valued enums used across persisted models.
  Bases: `str, Enum`.
  Methods:
  - `__str__`
  Domain doc cross-check: module covered: shared enums, projections, and helpers.
- `PipelineMode` at `litehive/domain/common.py:66`
  Purpose: Top-level execution mode for a task: full pipeline vs. single stage.
  Bases: `StringEnum`.
  Methods: none.
  Domain doc cross-check: module covered: shared enums, projections, and helpers.
- `PipelineState` at `litehive/domain/common.py:81`
  Purpose: Canonical internal state-machine positions.
  Bases: `StringEnum`.
  Methods:
  - `human_label`
  - `task_stage`
  - `pipeline_status`
  - `primary_stage`
  - `accepts_runner_hook`
  Domain doc cross-check: mentioned by name.
- `TaskStage` at `litehive/domain/common.py:259`
  Purpose: Operator-facing work phases in the task lifecycle.
  Bases: `StringEnum`.
  Methods:
  - `owner_role`
  - `retry_counter_state`
  Domain doc cross-check: mentioned by name.
- `TaskStatus` at `litehive/domain/common.py:331`
  Purpose: High-level execution or terminal category for a task.
  Bases: `StringEnum`.
  Methods: none.
  Domain doc cross-check: mentioned by name.
- `TaskExecutionStatus` at `litehive/domain/common.py:354`
  Purpose: Per-task runner execution marker persisted on ``TaskRuntime.pipeline``.
  Bases: `StringEnum`.
  Methods: none.
  Domain doc cross-check: module covered: shared enums, projections, and helpers.
- `RuntimeStageStatus` at `litehive/domain/common.py:376`
  Purpose: Fine-grained status for ``TaskRuntime.pipeline.current_stage``.
  Bases: `StringEnum`.
  Methods: none.
  Domain doc cross-check: module covered: shared enums, projections, and helpers.
- `SubagentStatus` at `litehive/domain/common.py:391`
  Purpose: Lifecycle status for a Litehive-managed subagent run.
  Bases: `StringEnum`.
  Methods: none.
  Domain doc cross-check: module covered: shared enums, projections, and helpers.
- `PipelineStatus` at `litehive/domain/common.py:408`
  Purpose: Operator-facing projection of pipeline progress.
  Bases: `StringEnum`.
  Methods: none.
  Domain doc cross-check: mentioned by name.
- `RunnerStatus` at `litehive/domain/common.py:492`
  Purpose: Health states for the top-level runner process.
  Bases: `StringEnum`.
  Methods: none.
  Domain doc cross-check: module covered: shared enums, projections, and helpers.
- `TransientFailureKind` at `litehive/domain/common.py:509`
  Purpose: Retry-eligible transient failure categories.
  Bases: `StringEnum`.
  Methods: none.
  Domain doc cross-check: module covered: shared enums, projections, and helpers.
- `Verdict` at `litehive/domain/common.py:524`
  Purpose: Decision submitted for an executable pipeline state.
  Bases: `StringEnum`.
  Methods:
  - `stage_report_verdict`
  Domain doc cross-check: mentioned by name.

### `litehive/domain/engine.py`
- `EngineUsageRecord` at `litehive/domain/engine.py:25`
  Purpose: Per-engine counters and the most recent usage window.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: module covered: engine monitoring and live event-stream models.
- `WorkspaceEngineMonitoring` at `litehive/domain/engine.py:51`
  Purpose: Workspace-wide map from engine name to ``EngineUsageRecord``.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: module covered: engine monitoring and live event-stream models.

### `litehive/domain/failure_diagnostics.py`
- `FailureDiagnostics` at `litehive/domain/failure_diagnostics.py:11`
  Purpose: Typed report-local failure evidence.
  Bases: `RootModel[dict[str, FailureDiagnosticValue]]`.
  Methods:
  - `__bool__`
  - `__getitem__`
  - `get`
  - `as_dict`
  Domain doc cross-check: mentioned by name.

### `litehive/domain/lifecycle_deltas.py`
- `StateDelta` at `litehive/domain/lifecycle_deltas.py:54`
  Purpose: Typed patch applied by the runner after a transition fires.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: module covered: transition deltas and recovery trigger construction.
- `IncStageRetry` at `litehive/domain/lifecycle_deltas.py:608`
  Purpose: Effect: bump the stage retry counter and capture the rejection.
  Bases: `-`.
  Methods:
  - `__call__`
  Domain doc cross-check: module covered: transition deltas and recovery trigger construction.
- `RememberRejection` at `litehive/domain/lifecycle_deltas.py:639`
  Purpose: Capture a rejection for a downstream prompt without bumping retries.
  Bases: `-`.
  Methods:
  - `__call__`
  Domain doc cross-check: module covered: transition deltas and recovery trigger construction.
- `FailRejectionLoop` at `litehive/domain/lifecycle_deltas.py:705`
  Purpose: Effect: fail the task when reviewer rejects keep bouncing it back without progress.
  Bases: `-`.
  Methods:
  - `__call__`
  Domain doc cross-check: module covered: transition deltas and recovery trigger construction.
- `Fail` at `litehive/domain/lifecycle_deltas.py:791`
  Purpose: Effect: drive the task into ``FAILED`` and record the cause.
  Bases: `-`.
  Methods:
  - `__post_init__`
  - `__call__`
  Domain doc cross-check: mentioned by name.

### `litehive/domain/outcomes.py`
- `TaskOutcomeKind` at `litehive/domain/outcomes.py:24`
  Purpose: Terminal outcome categories for a finished task.
  Bases: `StringEnum`.
  Methods: none.
  Domain doc cross-check: mentioned by name.
- `TaskCloseReason` at `litehive/domain/outcomes.py:45`
  Purpose: Operator-facing reasons accepted by `litehive task close`.
  Bases: `StringEnum`.
  Methods:
  - `outcome_reason_code`
  - `task_close_label`
  Domain doc cross-check: mentioned by name.
- `OutcomeReasonCode` at `litehive/domain/outcomes.py:85`
  Purpose: Normalized reason codes for stage outcomes and task interruptions.
  Bases: `StringEnum`.
  Methods: none.
  Domain doc cross-check: mentioned by name.

### `litehive/domain/pool.py`
- `PoolStopReason` at `litehive/domain/pool.py:17`
  Purpose: Machine-readable reasons a pool run can stop.
  Bases: `StringEnum`.
  Methods:
  - `from_value`
  - `operator_label`
  - `progress_report`
  Domain doc cross-check: mentioned by name.
- `PoolProgressReport` at `litehive/domain/pool.py:136`
  Purpose: Progress/action summary attached to non-progress pool stops.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: module covered: pool stop reasons, pool summary reports, worktree reports, and dirty-worktree gate reports.
- `PoolTaskReportEntry` at `litehive/domain/pool.py:146`
  Purpose: One task row in the pool summary.
  Bases: `-`.
  Methods:
  - `from_mapping`
  Domain doc cross-check: module covered: pool stop reasons, pool summary reports, worktree reports, and dirty-worktree gate reports.
- `PoolSummaryReport` at `litehive/domain/pool.py:187`
  Purpose: Structured summary of one pool run.
  Bases: `-`.
  Methods:
  - `from_mapping`
  - `stop_condition`
  - `completed_count`
  - `flagged_count`
  - `resumable_count`
  - `closed_count`
  - `skipped_count`
  - `remaining_count`
  - `with_derived_progress_report`
  Domain doc cross-check: module covered: pool stop reasons, pool summary reports, worktree reports, and dirty-worktree gate reports.
- `DirtyWorktreeLocationKind` at `litehive/domain/pool.py:311`
  Purpose: Where a dirty-worktree finding was detected.
  Bases: `StringEnum`.
  Methods: none.
  Domain doc cross-check: module covered: pool stop reasons, pool summary reports, worktree reports, and dirty-worktree gate reports.
- `DirtyWorktreeOwnership` at `litehive/domain/pool.py:324`
  Purpose: Ownership classification for dirty worktree paths.
  Bases: `StringEnum`.
  Methods:
  - `blocks_pool`
  Domain doc cross-check: module covered: pool stop reasons, pool summary reports, worktree reports, and dirty-worktree gate reports.
- `DirtyWorktreeFinding` at `litehive/domain/pool.py:352`
  Purpose: One dirty-state finding pinned to a workspace location.
  Bases: `-`.
  Methods:
  - `__post_init__`
  Domain doc cross-check: module covered: pool stop reasons, pool summary reports, worktree reports, and dirty-worktree gate reports.
- `DirtyWorktreeGateReport` at `litehive/domain/pool.py:383`
  Purpose: Aggregate of every dirty-worktree finding for the pool gate.
  Bases: `-`.
  Methods:
  - `is_clean`
  - `blocks_pool`
  Domain doc cross-check: module covered: pool stop reasons, pool summary reports, worktree reports, and dirty-worktree gate reports.

### `litehive/domain/recovery.py`
- `TriggerEventKind` at `litehive/domain/recovery.py:34`
  Purpose: Categories the recovery agent prompt branches on.
  Bases: `StringEnum`.
  Methods: none.
  Domain doc cross-check: module covered: recovery enums and persisted value objects.
- `RecoveryDisposition` at `litehive/domain/recovery.py:57`
  Purpose: How a recovery attempt concluded.
  Bases: `StringEnum`.
  Methods: none.
  Domain doc cross-check: module covered: recovery enums and persisted value objects.
- `FailureFingerprint` at `litehive/domain/recovery.py:75`
  Purpose: Normalized recovery-budget key plus the diagnostics that explain it.
  Bases: `-`.
  Methods:
  - `budget_key`
  - `to_payload`
  - `from_payload`
  Domain doc cross-check: mentioned by name.
- `RecoveryTrigger` at `litehive/domain/recovery.py:135`
  Purpose: Structured description of what sent the task into recovery.
  Bases: `-`.
  Methods:
  - `budget_key`
  - `to_payload`
  - `from_payload`
  Domain doc cross-check: mentioned by name.
- `RecoveryOutcome` at `litehive/domain/recovery.py:206`
  Purpose: Persisted result for one recovery attempt (or denial).
  Bases: `-`.
  Methods:
  - `to_payload`
  - `from_payload`
  Domain doc cross-check: mentioned by name.

### `litehive/domain/reports.py`
- `StageReport` at `litehive/domain/reports.py:136`
  Purpose: Normalized, machine-readable summary of one pipeline-state execution.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: mentioned by name.
- `FollowUpTaskSpec` at `litehive/domain/reports.py:174`
  Purpose: Spec for a follow-up task spawned by a running stage.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: module covered: stage reports, recovery reports, task activity, and report projections.
- `RecoveryEvidenceItem` at `litehive/domain/reports.py:193`
  Purpose: One piece of evidence recorded by the recovery agent.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: module covered: stage reports, recovery reports, task activity, and report projections.
- `RecoveryAction` at `litehive/domain/reports.py:212`
  Purpose: One action the recovery agent reports it performed.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: module covered: stage reports, recovery reports, task activity, and report projections.
- `RecoveryReport` at `litehive/domain/reports.py:227`
  Purpose: Complete machine-readable record of a recovery attempt.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: module covered: stage reports, recovery reports, task activity, and report projections.
- `ExecutionEstimate` at `litehive/domain/reports.py:252`
  Purpose: ETA projection for an in-flight task.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: module covered: stage reports, recovery reports, task activity, and report projections.
- `TaskActivityEntry` at `litehive/domain/reports.py:268`
  Purpose: One row of the human-readable task activity log.
  Bases: `BaseModel`.
  Methods:
  - `_reject_stage_report_aliases`
  - `_default_legacy_source`
  - `_require_agent_source_subagent_id`
  Domain doc cross-check: module covered: stage reports, recovery reports, task activity, and report projections.

### `litehive/domain/roles.py`
- `AgentRole` at `litehive/domain/roles.py:35`
  Purpose: Canonical subagent roles that have lifecycle-owned stage defaults.
  Bases: `StringEnum`.
  Methods:
  - `default_stage`
  - `pipeline_stages`
  - `task_stages`
  - `allowed_activity_verdicts`
  Domain doc cross-check: domain module not listed explicitly.

### `litehive/domain/runtime.py`
- `RuntimeGitState` at `litehive/domain/runtime.py:56`
  Purpose: Per-task git context (commit + worktree) the runner is operating in.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: module covered: runtime, interruption, subagent, and runner state models.
- `RuntimeStageState` at `litehive/domain/runtime.py:70`
  Purpose: Snapshot of which stage is running and for how long.
  Bases: `BaseModel`.
  Methods:
  - `model_copy`
  Domain doc cross-check: module covered: runtime, interruption, subagent, and runner state models.
- `RuntimeSubagentState` at `litehive/domain/runtime.py:101`
  Purpose: Subagent execution snapshot stored on the active task.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: module covered: runtime, interruption, subagent, and runner state models.
- `RuntimeEngineSwitch` at `litehive/domain/runtime.py:145`
  Purpose: Bookkeeping for the most recent engine switch on a task.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: module covered: runtime, interruption, subagent, and runner state models.
- `RuntimeHookRejectFingerprint` at `litehive/domain/runtime.py:163`
  Purpose: Persisted shape of a hook rejection used for loop detection.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: module covered: runtime, interruption, subagent, and runner state models.
- `RuntimeRecoveryOutcome` at `litehive/domain/runtime.py:180`
  Purpose: Compact projection of ``RecoveryOutcome`` stored on runtime for display.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: mentioned by name.
- `RuntimeFailedRunRecord` at `litehive/domain/runtime.py:204`
  Purpose: Compact projection of ``FailedRunRecord`` exposed on runtime.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: module covered: runtime, interruption, subagent, and runner state models.
- `TaskOutcomeState` at `litehive/domain/runtime.py:229`
  Purpose: Terminal outcome record stored on runtime when a task ends.
  Bases: `BaseModel`.
  Methods:
  - `_serialize_runtime_enum_value`
  Domain doc cross-check: module covered: runtime, interruption, subagent, and runner state models.
- `RuntimeInterruptionState` at `litehive/domain/runtime.py:268`
  Purpose: Interruption context the runner persists when a task pauses.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: module covered: runtime, interruption, subagent, and runner state models.
- `PipelineRuntime` at `litehive/domain/runtime.py:292`
  Purpose: Lifecycle projection persisted alongside the task.
  Bases: `BaseModel`.
  Methods:
  - `_serialize_execution_status`
  - `current_stage_name`
  Domain doc cross-check: module covered: runtime, interruption, subagent, and runner state models.
- `ExecutionRuntime` at `litehive/domain/runtime.py:334`
  Purpose: Subagent-execution slice of the task runtime.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: module covered: runtime, interruption, subagent, and runner state models.
- `TaskRuntime` at `litehive/domain/runtime.py:351`
  Purpose: Task-scoped runtime split by ownership: pipeline + execution.
  Bases: `BaseModel`.
  Methods:
  - `current_stage_name`
  - `for_storage`
  Domain doc cross-check: mentioned by name.
- `RunnerStatusState` at `litehive/domain/runtime.py:394`
  Purpose: Heartbeat-driven snapshot of the workspace's runner process.
  Bases: `BaseModel`.
  Methods:
  - `_serialize_status`
  Domain doc cross-check: module covered: runtime, interruption, subagent, and runner state models.

### `litehive/domain/task.py`
- `TaskRetryPolicy` at `litehive/domain/task.py:51`
  Purpose: Per-task overrides for the retry budget.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: module covered: task and workspace records.
- `TaskCreationSource` at `litehive/domain/task.py:68`
  Purpose: Provenance record stored on every newly-created task.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: module covered: task and workspace records.
- `GitSettings` at `litehive/domain/task.py:90`
  Purpose: Combined operator-intent + runtime-state git settings for a task.
  Bases: `BaseModel`.
  Methods:
  - `to_intent_git_settings`
  - `to_state_git_settings`
  Domain doc cross-check: module covered: task and workspace records.
- `TaskIntentGitSettings` at `litehive/domain/task.py:135`
  Purpose: Operator-supplied git settings persisted on the intent row.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: module covered: task and workspace records.
- `TaskStateGitSettings` at `litehive/domain/task.py:151`
  Purpose: Runtime-managed git fields persisted on the state row.
  Bases: `BaseModel`.
  Methods:
  - `to_git_updates`
  Domain doc cross-check: module covered: task and workspace records.
- `TaskIntentRecord` at `litehive/domain/task.py:181`
  Purpose: Persistence half of a ``TaskRecord`` carrying operator intent.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: module covered: task and workspace records.
- `TaskStateRecord` at `litehive/domain/task.py:210`
  Purpose: Persistence half of a ``TaskRecord`` carrying runtime state.
  Bases: `BaseModel`.
  Methods:
  - `apply_to_task`
  Domain doc cross-check: module covered: task and workspace records.
- `TaskRecord` at `litehive/domain/task.py:258`
  Purpose: The aggregate root for a single unit of work tracked by Litehive.
  Bases: `BaseModel`.
  Methods:
  - `current_pipeline_stage`
  - `is_pool_pending`
  - `is_resumable`
  - `is_closed`
  - `to_intent_record`
  - `to_state_record`
  - `to_storage_state_record`
  - `from_intent_and_state`
  Domain doc cross-check: mentioned by name.
- `UnmergedWorktree` at `litehive/domain/task.py:423`
  Purpose: Pointer to a worktree whose branch never made it back into main.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: module covered: task and workspace records.
- `WorkspaceState` at `litehive/domain/task.py:438`
  Purpose: Workspace-scoped runtime state that doesn't belong on any single task.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: module covered: task and workspace records.

### `litehive/domain/task_ops.py`
- `RunnerLockState` at `litehive/domain/task_ops.py:20`
  Purpose: Thread-safe handle for the runner's exclusive workspace lock.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: module covered: task-operation result and error dataclasses.
- `BlockedTask` at `litehive/domain/task_ops.py:39`
  Purpose: Summary row for a task that the queue can't pick because deps aren't done.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: module covered: task-operation result and error dataclasses.
- `TaskSelection` at `litehive/domain/task_ops.py:56`
  Purpose: Output of "pick the next task to run".
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: module covered: task-operation result and error dataclasses.
- `WorkspaceRepairSummary` at `litehive/domain/task_ops.py:72`
  Purpose: Per-invocation report from the workspace-repair flow.
  Bases: `-`.
  Methods:
  - `repaired`
  Domain doc cross-check: module covered: task-operation result and error dataclasses.
- `WorkspaceConflictError` at `litehive/domain/task_ops.py:103`
  Purpose: Raised when a workspace mutation would race a live runner.
  Bases: `ValueError`.
  Methods: none.
  Domain doc cross-check: module covered: task-operation result and error dataclasses.
- `StopTaskSummary` at `litehive/domain/task_ops.py:117`
  Purpose: Outcome record returned by ``stop_task``.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: module covered: task-operation result and error dataclasses.
- `SwitchTaskSummary` at `litehive/domain/task_ops.py:134`
  Purpose: Outcome record returned by ``switch_task_engine``.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: module covered: task-operation result and error dataclasses.

### `litehive/domain/worktree.py`
- `ManagedWorktree` at `litehive/domain/worktree.py:19`
  Purpose: Snapshot of one Litehive-managed task worktree.
  Bases: `-`.
  Methods:
  - `cleanable`
  Domain doc cross-check: domain module not listed explicitly.
- `RescueCandidate` at `litehive/domain/worktree.py:52`
  Purpose: Candidate identified by the rescue flow as needing operator triage.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: domain module not listed explicitly.
- `RescueResult` at `litehive/domain/worktree.py:69`
  Purpose: Outcome record returned for one ``RescueCandidate``.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: domain module not listed explicitly.
- `WorktreeMergeConflict` at `litehive/domain/worktree.py:88`
  Purpose: Raised when worktree sync ends with unresolved files in the index.
  Bases: `Exception`.
  Methods:
  - `__init__`
  Domain doc cross-check: domain module not listed explicitly.
- `WorktreeSyncResult` at `litehive/domain/worktree.py:114`
  Purpose: Outcome record returned by lifecycle pre-exec worktree sync.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: domain module not listed explicitly.
- `TaskWorktreeInspection` at `litehive/domain/worktree.py:129`
  Purpose: Per-task worktree snapshot rendered by status and diagnostics.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: domain module not listed explicitly.

### `litehive/git/ops.py`
- `GitError` at `litehive/git/ops.py:27`
  Purpose: Raised when a git operation fails in a way the caller must handle.
  Bases: `RuntimeError`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/lifecycle/engines.py`
- `ConfigBackedEngineSelector` at `litehive/lifecycle/engines.py:38`
  Purpose: ``EngineSelector`` driven by ``LitehiveConfig``.
  Bases: `-`.
  Methods:
  - `__init__`
  - `_selection_task`
  - `select`
  Domain doc cross-check: not a domain-doc class.

### `litehive/lifecycle/events.py`
- `Event` at `litehive/lifecycle/events.py:22`
  Purpose: Base class for all transition-triggering events.
  Bases: `-`.
  Methods:
  - `trigger_event_kind`
  - `failure_message`
  - `failure_source`
  - `failure_diagnostics`
  - `terminal_recovery_verdict`
  Domain doc cross-check: not a domain-doc class.
- `Pass` at `litehive/lifecycle/events.py:97`
  Purpose: The node succeeded at its job; advance on the happy path.
  Bases: `Event`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `HookOk` at `litehive/lifecycle/events.py:121`
  Purpose: The current hook phase finished and execution should continue.
  Bases: `Event`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `CleanState` at `litehive/lifecycle/events.py:132`
  Purpose: The ``ready`` probe found no pre-execution trouble; enter the pipeline.
  Bases: `Event`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `NeedsPreExecRecovery` at `litehive/lifecycle/events.py:142`
  Purpose: The ``ready`` probe detected broken pre-execution state.
  Bases: `Event`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `Reject` at `litehive/lifecycle/events.py:154`
  Purpose: Some code path decided this stage's work isn't acceptable.
  Bases: `Event`.
  Methods:
  - `trigger_event_kind`
  - `failure_message`
  - `failure_source`
  - `failure_diagnostics`
  Domain doc cross-check: not a domain-doc class.
- `MergeConflictDetected` at `litehive/lifecycle/events.py:212`
  Purpose: Automatic git merge hit conflicts — hand off to the merge agent.
  Bases: `Event`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `Blocked` at `litehive/lifecycle/events.py:230`
  Purpose: System-detected infrastructure blockage; don't retry, route to recovery.
  Bases: `Event`.
  Methods:
  - `trigger_event_kind`
  - `failure_message`
  Domain doc cross-check: not a domain-doc class.
- `Crash` at `litehive/lifecycle/events.py:253`
  Purpose: Unrecoverable error inside a node — escalate to the state machine.
  Bases: `Event`.
  Methods:
  - `trigger_event_kind`
  - `failure_message`
  - `failure_diagnostics`
  - `terminal_recovery_verdict`
  Domain doc cross-check: not a domain-doc class.
- `Timeout` at `litehive/lifecycle/events.py:293`
  Purpose: A node's ``run()`` exceeded its grace period.
  Bases: `Event`.
  Methods:
  - `trigger_event_kind`
  - `terminal_recovery_verdict`
  Domain doc cross-check: not a domain-doc class.
- `StageRetryLimitHit` at `litehive/lifecycle/events.py:314`
  Purpose: A stage's retry counter reached its configured limit.
  Bases: `Event`.
  Methods:
  - `trigger_event_kind`
  - `failure_message`
  - `failure_diagnostics`
  Domain doc cross-check: not a domain-doc class.
- `OverallRetryLimitHit` at `litehive/lifecycle/events.py:344`
  Purpose: Whole-task retry budget exhausted across all stages.
  Bases: `Event`.
  Methods:
  - `trigger_event_kind`
  - `failure_message`
  Domain doc cross-check: not a domain-doc class.
- `TaskTimeBudgetExceeded` at `litehive/lifecycle/events.py:365`
  Purpose: A task exceeded its cumulative agent wall-clock budget before commit.
  Bases: `Event`.
  Methods:
  - `failure_message`
  Domain doc cross-check: not a domain-doc class.
- `RecoverySucceeded` at `litehive/lifecycle/events.py:388`
  Purpose: The recovery agent returned a successful verdict.
  Bases: `Event`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `RecoveryFailed` at `litehive/lifecycle/events.py:404`
  Purpose: The recovery agent gave up without a fix.
  Bases: `Event`.
  Methods:
  - `failure_message`
  - `terminal_recovery_verdict`
  Domain doc cross-check: not a domain-doc class.
- `RecoveryBudgetHit` at `litehive/lifecycle/events.py:427`
  Purpose: Recovery was requested for a stage that already used its one shot.
  Bases: `Event`.
  Methods:
  - `terminal_recovery_verdict`
  Domain doc cross-check: not a domain-doc class.
- `PreExecRecoverySucceeded` at `litehive/lifecycle/events.py:444`
  Purpose: Pre-exec recovery cleared whatever was wrong; resume the pipeline.
  Bases: `Event`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `PreExecRecoveryFailed` at `litehive/lifecycle/events.py:457`
  Purpose: Pre-exec recovery couldn't salvage the task.
  Bases: `Event`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `PreExecRecoveryBudgetHit` at `litehive/lifecycle/events.py:470`
  Purpose: Pre-exec recovery was attempted a second time.
  Bases: `Event`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/lifecycle/guards.py`
- `Guard` at `litehive/lifecycle/guards.py:17`
  Purpose: Composable predicate used by transition rules.
  Bases: `-`.
  Methods:
  - `__call__`
  - `__and__`
  - `__or__`
  - `__invert__`
  Domain doc cross-check: not a domain-doc class.

### `litehive/lifecycle/heru_factory.py`
- `_MissingActivityEntry` at `litehive/lifecycle/heru_factory.py:71`
  Purpose: Internal: agent finished without producing a fresh activity entry.
  Bases: `Exception`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `_NullSelector` at `litehive/lifecycle/heru_factory.py:78`
  Purpose: Stub engine selector for the direct-recovery prompt build.
  Bases: `-`.
  Methods:
  - `select`
  Domain doc cross-check: not a domain-doc class.
- `_NullSessions` at `litehive/lifecycle/heru_factory.py:100`
  Purpose: Stub session store paired with ``_NullSelector``.
  Bases: `-`.
  Methods:
  - `get_or_create`
  - `persist`
  Domain doc cross-check: not a domain-doc class.
- `HeruEngineAdapter` at `litehive/lifecycle/heru_factory.py:394`
  Purpose: ``Engine`` that delegates to ``SubagentManager`` for one turn.
  Bases: `-`.
  Methods:
  - `__init__`
  - `with_model`
  - `run_turn`
  - `_run_with_crash_resume`
  - `_crash_resume_prompt`
  - `_handle_startup_failure`
  - `_attempt_direct_recovery_handoff`
  - `_direct_recovery_prompt`
  - `_direct_recovery_state`
  - `_direct_recovery_explanation`
  - `_run_direct_recovery_turn`
  - `extract_continuation_id`
  - `_reraise`
  - `_reraise_failure`
  Domain doc cross-check: mentioned by name.

### `litehive/lifecycle/journal.py`
- `PipelineJournal` at `litehive/lifecycle/journal.py:90`
  Purpose: Abstract base class for runner-event journals.
  Bases: `ABC`.
  Methods:
  - `__init__`
  - `task_started`
  - `transition`
  - `stop_requested`
  - `task_finished`
  - `_append`
  - `_log`
  - `_load_starting_seq`
  - `_store`
  Domain doc cross-check: not a domain-doc class.
- `SqliteJournal` at `litehive/lifecycle/journal.py:250`
  Purpose: Writes journal events to the workspace SQLite db.
  Bases: `PipelineJournal`.
  Methods:
  - `__init__`
  - `_store`
  - `_insert_transition`
  - `_insert_lifecycle`
  - `_load_starting_seq`
  - `load_transitions`
  - `load_lifecycle`
  Domain doc cross-check: not a domain-doc class.
- `NullJournal` at `litehive/lifecycle/journal.py:467`
  Purpose: Drops every record; use when the runner should not journal at all.
  Bases: `PipelineJournal`.
  Methods:
  - `_store`
  - `_log`
  Domain doc cross-check: not a domain-doc class.

### `litehive/lifecycle/nodes/agent.py`
- `TransientError` at `litehive/lifecycle/nodes/agent.py:18`
  Purpose: Retry on the same engine, same session.
  Bases: `Exception`.
  Methods:
  - `__init__`
  Domain doc cross-check: not a domain-doc class.
- `EngineBlockedError` at `litehive/lifecycle/nodes/agent.py:39`
  Purpose: Base class for 'this engine is unavailable — switch to another one'.
  Bases: `Exception`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `QuotaExceeded` at `litehive/lifecycle/nodes/agent.py:48`
  Purpose: Engine hit a quota / rate limit that won't clear soon enough.
  Bases: `EngineBlockedError`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `EngineOverloaded` at `litehive/lifecycle/nodes/agent.py:52`
  Purpose: Engine responded with 'overloaded, try later'.
  Bases: `EngineBlockedError`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `ModelUnavailable` at `litehive/lifecycle/nodes/agent.py:56`
  Purpose: The requested model isn't served by this engine right now.
  Bases: `EngineBlockedError`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `UnrecoverableError` at `litehive/lifecycle/nodes/agent.py:60`
  Purpose: Escalate to the state machine as a ``Crash`` event.
  Bases: `Exception`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `NudgeRequired` at `litehive/lifecycle/nodes/agent.py:70`
  Purpose: Agent finished its turn without submitting a verdict — nudge it.
  Bases: `Exception`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `AgentVerdict` at `litehive/lifecycle/nodes/agent.py:83`
  Purpose: Inferred from `litehive/lifecycle/nodes/agent.py` and class name: owns the AgentVerdict concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `Engine` at `litehive/lifecycle/nodes/agent.py:91`
  Purpose: Inferred from `litehive/lifecycle/nodes/agent.py` and class name: owns the Engine concept.
  Bases: `Protocol`.
  Methods:
  - `run_turn`
  Domain doc cross-check: mentioned by name.
- `EngineSelector` at `litehive/lifecycle/nodes/agent.py:106`
  Purpose: Policy that picks an engine for a node.
  Bases: `Protocol`.
  Methods:
  - `select`
  Domain doc cross-check: not a domain-doc class.
- `SessionProvider` at `litehive/lifecycle/nodes/agent.py:132`
  Purpose: Per-(task, node, engine) session store the AgentNode uses.
  Bases: `Protocol`.
  Methods:
  - `get_or_create`
  - `persist`
  Domain doc cross-check: not a domain-doc class.
- `AgentNode` at `litehive/lifecycle/nodes/agent.py:151`
  Purpose: Base for agent-backed stages.
  Bases: `Node`.
  Methods:
  - `__init__`
  - `build_prompt`
  - `build_nudge_prompt`
  - `run`
  - `_run_with_retries`
  - `verdict_to_event`
  Domain doc cross-check: not a domain-doc class.

### `litehive/lifecycle/nodes/base.py`
- `Node` at `litehive/lifecycle/nodes/base.py:9`
  Purpose: A node the machine can be in. Executes itself and returns an Event.
  Bases: `ABC`.
  Methods:
  - `run`
  Domain doc cross-check: mentioned by name.
- `NodeRegistry` at `litehive/lifecycle/nodes/base.py:33`
  Purpose: Maps node names to their Node implementations.
  Bases: `-`.
  Methods:
  - `__init__`
  - `register`
  - `get`
  - `names`
  - `__contains__`
  Domain doc cross-check: not a domain-doc class.

### `litehive/lifecycle/nodes/hook.py`
- `HookSpec` at `litehive/lifecycle/nodes/hook.py:21`
  Purpose: Configured hook command for a state-machine node.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `HookRunner` at `litehive/lifecycle/nodes/hook.py:35`
  Purpose: Inferred from `litehive/lifecycle/nodes/hook.py` and class name: owns the HookRunner concept.
  Bases: `Protocol`.
  Methods:
  - `run`
  Domain doc cross-check: not a domain-doc class.
- `SubprocessHookRunner` at `litehive/lifecycle/nodes/hook.py:60`
  Purpose: Production HookRunner that shells out under ``workspace`` with task identity in the env.
  Bases: `HookRunner`.
  Methods:
  - `__init__`
  - `run`
  Domain doc cross-check: not a domain-doc class.
- `HookNode` at `litehive/lifecycle/nodes/hook.py:136`
  Purpose: State-machine node that runs configured hooks for a stage and emits ``HookOk`` or ``Reject``.
  Bases: `Node`.
  Methods:
  - `__init__`
  - `run`
  Domain doc cross-check: not a domain-doc class.

### `litehive/lifecycle/nodes/system.py`
- `MergeConflict` at `litehive/lifecycle/nodes/system.py:44`
  Purpose: Raised by ``CommitNode._merge_worktree`` when git merge leaves files in an unresolved state. ``conflict_files`` is the list of paths that ``git diff --name-only --diff-filter=U`` reported.
  Bases: `Exception`.
  Methods:
  - `__init__`
  Domain doc cross-check: not a domain-doc class.
- `SystemNode` at `litehive/lifecycle/nodes/system.py:62`
  Purpose: Base class for pipeline nodes the runner drives without invoking an agent.
  Bases: `Node`.
  Methods:
  - `__init__`
  - `run`
  Domain doc cross-check: not a domain-doc class.
- `ReadyNode` at `litehive/lifecycle/nodes/system.py:89`
  Purpose: Entry probe for a task. Decides between clean entry and pre-exec recovery.
  Bases: `SystemNode`.
  Methods:
  - `__init__`
  - `run`
  Domain doc cross-check: not a domain-doc class.
- `WorktreeSyncNode` at `litehive/lifecycle/nodes/system.py:141`
  Purpose: Pull main into the task worktree before the pipeline runs.
  Bases: `SystemNode`.
  Methods:
  - `__init__`
  - `run`
  - `sync`
  Domain doc cross-check: not a domain-doc class.
- `NoopWorktreeSyncNode` at `litehive/lifecycle/nodes/system.py:207`
  Purpose: Always-pass variant — use when worktrees aren't in play (tests, dry runs).
  Bases: `WorktreeSyncNode`.
  Methods:
  - `sync`
  Domain doc cross-check: not a domain-doc class.
- `GitWorktreeSyncNode` at `litehive/lifecycle/nodes/system.py:216`
  Purpose: Real worktree sync — provisions a task worktree, then syncs from ``main``.
  Bases: `WorktreeSyncNode`.
  Methods:
  - `__init__`
  - `sync`
  Domain doc cross-check: not a domain-doc class.
- `PreExecRecoveryNode` at `litehive/lifecycle/nodes/system.py:276`
  Purpose: Runs pre-execution recovery before the task enters the pipeline proper.
  Bases: `SystemNode`.
  Methods:
  - `__init__`
  - `run`
  Domain doc cross-check: not a domain-doc class.
- `CommitNode` at `litehive/lifecycle/nodes/system.py:335`
  Purpose: Automatic git merge — no agents involved.
  Bases: `SystemNode`.
  Methods:
  - `__init__`
  - `run`
  - `_merge_worktree`
  Domain doc cross-check: not a domain-doc class.
- `StubCommitNode` at `litehive/lifecycle/nodes/system.py:386`
  Purpose: Always-pass commit node for tests that don't involve real git.
  Bases: `CommitNode`.
  Methods:
  - `_merge_worktree`
  Domain doc cross-check: not a domain-doc class.
- `GitCommitNode` at `litehive/lifecycle/nodes/system.py:487`
  Purpose: Real ``commit`` node — plain automatic merge, no agents.
  Bases: `CommitNode`.
  Methods:
  - `__init__`
  - `_merge_worktree`
  - `main_head`
  - `autocommit_worktree_changes`
  - `autocommit_main_checkout_changes`
  - `_generated_commit_message`
  - `git_status_entries`
  - `_filter_stageable_paths`
  - `_git_status_entries_with_options`
  - `_worktree_local_only_paths`
  - `_restore_local_only_paths`
  - `worktree_head`
  - `worktree_branch`
  - `_parse_dirty_checkout_files`
  - `_merge_in_progress`
  - `_conclude_in_progress_merge`
  - `worktree_patch_already_on_main`
  - `_unresolved_conflicts`
  - `_abort_merge`
  Domain doc cross-check: not a domain-doc class.

### `litehive/lifecycle/nodes/terminal.py`
- `TerminalNode` at `litehive/lifecycle/nodes/terminal.py:8`
  Purpose: A state the machine rests in; run() is a no-op.
  Bases: `Node`.
  Methods:
  - `__init__`
  - `run`
  Domain doc cross-check: not a domain-doc class.

### `litehive/lifecycle/orchestration.py`
- `ExecutionResult` at `litehive/lifecycle/orchestration.py:87`
  Purpose: Result of running one task through the pipeline state machine.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/lifecycle/persistence.py`
- `Limits` at `litehive/lifecycle/persistence.py:14`
  Purpose: Inferred from `litehive/lifecycle/persistence.py` and class name: owns the Limits concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: module covered: persisted lifecycle TaskState.
- `LastReport` at `litehive/lifecycle/persistence.py:24`
  Purpose: Inferred from `litehive/lifecycle/persistence.py` and class name: owns the LastReport concept.
  Bases: `-`.
  Methods:
  - `to_payload`
  - `from_payload`
  Domain doc cross-check: module covered: persisted lifecycle TaskState.
- `HookRejectFingerprint` at `litehive/lifecycle/persistence.py:66`
  Purpose: Inferred from `litehive/lifecycle/persistence.py` and class name: owns the HookRejectFingerprint concept.
  Bases: `-`.
  Methods:
  - `to_payload`
  - `from_payload`
  Domain doc cross-check: module covered: persisted lifecycle TaskState.
- `LastRejection` at `litehive/lifecycle/persistence.py:100`
  Purpose: Most recent reject against a retry-eligible stage.
  Bases: `-`.
  Methods:
  - `to_payload`
  - `from_payload`
  Domain doc cross-check: module covered: persisted lifecycle TaskState.
- `MergeContext` at `litehive/lifecycle/persistence.py:137`
  Purpose: Inferred from `litehive/lifecycle/persistence.py` and class name: owns the MergeContext concept.
  Bases: `-`.
  Methods:
  - `to_payload`
  - `from_payload`
  Domain doc cross-check: module covered: persisted lifecycle TaskState.
- `CommitResult` at `litehive/lifecycle/persistence.py:165`
  Purpose: Inferred from `litehive/lifecycle/persistence.py` and class name: owns the CommitResult concept.
  Bases: `-`.
  Methods:
  - `to_payload`
  - `from_payload`
  Domain doc cross-check: module covered: persisted lifecycle TaskState.
- `RejectionLoop` at `litehive/lifecycle/persistence.py:186`
  Purpose: Inferred from `litehive/lifecycle/persistence.py` and class name: owns the RejectionLoop concept.
  Bases: `-`.
  Methods:
  - `to_payload`
  - `from_payload`
  Domain doc cross-check: module covered: persisted lifecycle TaskState.
- `FailedRunRecord` at `litehive/lifecycle/persistence.py:217`
  Purpose: Persistent summary of a terminal failed run shape.
  Bases: `-`.
  Methods:
  - `key`
  - `to_payload`
  - `from_payload`
  Domain doc cross-check: module covered: persisted lifecycle TaskState.
- `TaskState` at `litehive/lifecycle/persistence.py:310`
  Purpose: Single source of truth for task state the machine reads and writes.
  Bases: `-`.
  Methods:
  - `__post_init__`
  - `recovery_budget_available`
  - `_budget_window_unconsumed_for`
  - `_budget_recovery_history`
  Domain doc cross-check: mentioned by name.
- `Persistence` at `litehive/lifecycle/persistence.py:445`
  Purpose: Structural type the runner depends on for state persistence.
  Bases: `Protocol`.
  Methods:
  - `save`
  - `load`
  Domain doc cross-check: module covered: persisted lifecycle TaskState.
- `TaskNotFound` at `litehive/lifecycle/persistence.py:689`
  Purpose: Raised when ``SqlitePersistence.load`` is called on an unknown task id.
  Bases: `LookupError`.
  Methods: none.
  Domain doc cross-check: module covered: persisted lifecycle TaskState.
- `SqlitePersistence` at `litehive/lifecycle/persistence.py:693`
  Purpose: Persists the current lifecycle cursor to ``pipeline_task_state``.
  Bases: `-`.
  Methods:
  - `__init__`
  - `save`
  - `load`
  - `reset_current_lifecycle_state`
  - `reset_all`
  - `initialize`
  Domain doc cross-check: module covered: persisted lifecycle TaskState.

### `litehive/lifecycle/prompt_types.py`
- `FailedSubagentDiagnostics` at `litehive/lifecycle/prompt_types.py:31`
  Purpose: Prompt-ready evidence for the failed subagent that triggered recovery.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `AgentPrompt` at `litehive/lifecycle/prompt_types.py:52`
  Purpose: Structured payload every stage agent passes to the engine adapter.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `RecoveryPrompt` at `litehive/lifecycle/prompt_types.py:139`
  Purpose: Recovery-only extension carrying the extra diagnostic fields the recovery role needs to fix Litehive infrastructure bugs without re-running the failed stage.
  Bases: `AgentPrompt`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/lifecycle/runner.py`
- `StateMachineRunner` at `litehive/lifecycle/runner.py:48`
  Purpose: Drives a task through the state machine.
  Bases: `-`.
  Methods:
  - `__init__`
  - `run_task`
  - `_task_time_budget_event`
  - `_apply_transition`
  - `apply_event_side_effects`
  - `_reset_hook_reject_tracking_on_progress`
  - `_clear_hook_reject_tracking`
  - `_apply_delta`
  - `_record_failed_run`
  - `_reset_cross_agent_retry_sessions`
  Domain doc cross-check: not a domain-doc class.

### `litehive/lifecycle/sessions.py`
- `FreshEngineSession` at `litehive/lifecycle/sessions.py:9`
  Purpose: Lifecycle state for an engine turn without a continuation handle.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `ResumableEngineSession` at `litehive/lifecycle/sessions.py:18`
  Purpose: Lifecycle state for an engine turn with a continuation handle.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `EngineSessionContinuation` at `litehive/lifecycle/sessions.py:26`
  Purpose: Start-vs-continue state exposed by ``Session``.
  Bases: `Protocol`.
  Methods:
  - `resume_session_id`
  Domain doc cross-check: not a domain-doc class.
- `Session` at `litehive/lifecycle/sessions.py:36`
  Purpose: One agent conversation with one engine for one task.
  Bases: `-`.
  Methods:
  - `continuation_state`
  - `resume_session_id`
  - `capture_engine_session_id`
  - `resumable`
  Domain doc cross-check: mentioned by name.
- `SessionStore` at `litehive/lifecycle/sessions.py:85`
  Purpose: Keyed by ``(task_id, node_name, engine_name)``.
  Bases: `Protocol`.
  Methods:
  - `get_or_create`
  - `persist`
  - `clear_node_sessions`
  Domain doc cross-check: not a domain-doc class.
- `SqliteSessionStore` at `litehive/lifecycle/sessions.py:131`
  Purpose: Persists ``Session`` rows to the ``pipeline_sessions`` sqlite table.
  Bases: `-`.
  Methods:
  - `__init__`
  - `get_or_create`
  - `persist`
  - `clear_node_sessions`
  Domain doc cross-check: not a domain-doc class.

### `litehive/lifecycle/stages.py`
- `Stage` at `litehive/lifecycle/stages.py:22`
  Purpose: Inferred from `litehive/lifecycle/stages.py` and class name: owns the Stage concept.
  Bases: `-`.
  Methods:
  - `__post_init__`
  - `__eq__`
  - `__hash__`
  - `__lt__`
  - `__repr__`
  Domain doc cross-check: mentioned by name.
- `Stages` at `litehive/lifecycle/stages.py:69`
  Purpose: Inferred from `litehive/lifecycle/stages.py` and class name: owns the Stages concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/lifecycle/transitions.py`
- `Rule` at `litehive/lifecycle/transitions.py:56`
  Purpose: Inferred from `litehive/lifecycle/transitions.py` and class name: owns the Rule concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: mentioned by name.
- `Transition` at `litehive/lifecycle/transitions.py:66`
  Purpose: Inferred from `litehive/lifecycle/transitions.py` and class name: owns the Transition concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `NoTransitionError` at `litehive/lifecycle/transitions.py:72`
  Purpose: Inferred from `litehive/lifecycle/transitions.py` and class name: owns the NoTransitionError concept.
  Bases: `RuntimeError`.
  Methods:
  - `__init__`
  Domain doc cross-check: not a domain-doc class.

### `litehive/lifecycle/types.py`
- `NodeType` at `litehive/lifecycle/types.py:11`
  Purpose: Inferred from `litehive/lifecycle/types.py` and class name: owns the NodeType concept.
  Bases: `str, Enum`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `FailedReason` at `litehive/lifecycle/types.py:91`
  Purpose: Inferred from `litehive/lifecycle/types.py` and class name: owns the FailedReason concept.
  Bases: `StringEnum`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/observability/events.py`
- `PersistedTaskEvent` at `litehive/observability/events.py:21`
  Purpose: Typed event object accepted by ``append_event``.
  Bases: `Protocol`.
  Methods:
  - `kind`
  - `data`
  Domain doc cross-check: not a domain-doc class.

### `litehive/observability/status.py`
- `TaskPipelineStatusData` at `litehive/observability/status.py:108`
  Purpose: Inferred from `litehive/observability/status.py` and class name: owns the TaskPipelineStatusData concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/observability/status_types.py`
- `StatusIssue` at `litehive/observability/status_types.py:44`
  Purpose: Inferred from `litehive/observability/status_types.py` and class name: owns the StatusIssue concept.
  Bases: `-`.
  Methods:
  - `render`
  Domain doc cross-check: not a domain-doc class.
- `StatusSnapshot` at `litehive/observability/status_types.py:63`
  Purpose: Inferred from `litehive/observability/status_types.py` and class name: owns the StatusSnapshot concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `_RecoveryFailureContext` at `litehive/observability/status_types.py:72`
  Purpose: Inferred from `litehive/observability/status_types.py` and class name: owns the _RecoveryFailureContext concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/observability/venv_health.py`
- `VenvCheckout` at `litehive/observability/venv_health.py:27`
  Purpose: Inferred from `litehive/observability/venv_health.py` and class name: owns the VenvCheckout concept.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `BrokenVenvExecutable` at `litehive/observability/venv_health.py:33`
  Purpose: Inferred from `litehive/observability/venv_health.py` and class name: owns the BrokenVenvExecutable concept.
  Bases: `-`.
  Methods:
  - `binary_name`
  Domain doc cross-check: not a domain-doc class.

### `litehive/recovery/detection.py`
- `TaskLaunchFailure` at `litehive/recovery/detection.py:15`
  Purpose: Raised when a task cannot be selected or prepared for execution.
  Bases: `RuntimeError`.
  Methods:
  - `__init__`
  Domain doc cross-check: not a domain-doc class.

### `litehive/recovery/nonrunning_resumable_repair.py`
- `NonrunningResumableRepairResult` at `litehive/recovery/nonrunning_resumable_repair.py:18`
  Purpose: Repair summary returned by :func:`normalize_nonrunning_resumable_tasks` so the caller can persist transitions and journal entries without a second walk.
  Bases: `TypedDict`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/recovery/running_task_recovery.py`
- `RunningTaskRecoveryResult` at `litehive/recovery/running_task_recovery.py:36`
  Purpose: Recovery summary returned by :func:`recover_running_tasks` so the caller can route post-mortem state without re-walking the task population.
  Bases: `TypedDict`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/recovery/scope_analysis.py`
- `ScopeAnalysisError` at `litehive/recovery/scope_analysis.py:19`
  Purpose: Raised when scope analysis cannot inspect the current worktree.
  Bases: `RuntimeError`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/roles/base.py`
- `PromptContext` at `litehive/roles/base.py:17`
  Purpose: Workspace-level context the runner provides to agents at construction.
  Bases: `-`.
  Methods:
  - `workspace_root`
  Domain doc cross-check: not a domain-doc class.
- `RoleAgent` at `litehive/roles/base.py:78`
  Purpose: Base for stage-bound agents.
  Bases: `AgentNode`.
  Methods:
  - `__init__`
  - `build_prompt`
  - `_last_rejection_for_prompt`
  - `_runner_hooks_for_stage`
  - `_assemble_instruction_layers`
  - `_attempt_instruction_layer`
  - `_startup_guidance_for`
  - `_load_overlay_md`
  Domain doc cross-check: not a domain-doc class.

### `litehive/roles/merge.py`
- `MergeAgent` at `litehive/roles/merge.py:37`
  Purpose: Resolves git merge conflicts encountered during the commit stage.
  Bases: `RoleAgent`.
  Methods:
  - `build_prompt`
  Domain doc cross-check: not a domain-doc class.

### `litehive/roles/planner.py`
- `PlannerAgent` at `litehive/roles/planner.py:23`
  Purpose: Grooming-stage agent.
  Bases: `RoleAgent`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/roles/qa.py`
- `QAAgent` at `litehive/roles/qa.py:27`
  Purpose: Testing-stage agent.
  Bases: `RoleAgent`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/roles/recovery.py`
- `RecoveryAgent` at `litehive/roles/recovery.py:60`
  Purpose: Singleton recovery node, reachable from any stage.
  Bases: `RoleAgent`.
  Methods:
  - `build_prompt`
  - `verdict_to_event`
  Domain doc cross-check: not a domain-doc class.

### `litehive/roles/reviewer.py`
- `ReviewerAgent` at `litehive/roles/reviewer.py:34`
  Purpose: Accepting-stage agent.
  Bases: `RoleAgent`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/roles/swe.py`
- `SWEAgent` at `litehive/roles/swe.py:47`
  Purpose: Implementing-stage agent.
  Bases: `RoleAgent`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/sandbox/adapter.py`
- `SandboxedAdapter` at `litehive/sandbox/adapter.py:18`
  Purpose: Wrap a heru engine adapter so every invocation is finalized through the workspace sandbox launcher.
  Bases: `ExternalCLIAdapter`.
  Methods:
  - `__init__`
  - `build_command`
  - `detect_capabilities`
  - `finalize_invocation`
  - `sandbox_details`
  - `run`
  - `run_live`
  - `render_transcript`
  Domain doc cross-check: not a domain-doc class.

### `litehive/sandbox/launcher.py`
- `SandboxProfile` at `litehive/sandbox/launcher.py:20`
  Purpose: Git wrapper profile applied to a sandboxed engine invocation.
  Bases: `str, Enum`.
  Methods:
  - `for_role`
  Domain doc cross-check: not a domain-doc class.
- `SandboxPolicySummary` at `litehive/sandbox/launcher.py:41`
  Purpose: Snapshot of the resolved sandbox policy for one engine/role pair, surfaced to operators in subagent reports.
  Bases: `-`.
  Methods:
  - `from_mapping`
  - `as_dict`
  - `summary`
  Domain doc cross-check: not a domain-doc class.
- `SandboxError` at `litehive/sandbox/launcher.py:144`
  Purpose: Raised when sandbox configuration cannot be applied.
  Bases: `RuntimeError`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `SandboxLauncher` at `litehive/sandbox/launcher.py:148`
  Purpose: Generic contract for sandbox implementations.
  Bases: `Protocol`.
  Methods:
  - `policy_summary`
  - `wrap_invocation`
  Domain doc cross-check: mentioned by name.
- `DockerSandboxLauncher` at `litehive/sandbox/launcher.py:166`
  Purpose: Builds docker-run argv that wraps every external engine invocation.
  Bases: `-`.
  Methods:
  - `__init__`
  - `policy_summary`
  - `wrap_invocation`
  - `_wrap_docker`
  - `ensure_docker_git_wrappers`
  - `_policy_for_engine`
  - `_resolved_extra_ro_binds`
  - `_resolved_extra_rw_binds`
  - `_translate_container_argv`
  - `_bind_mount_spec`
  Domain doc cross-check: not a domain-doc class.

### `litehive/state/backup.py`
- `WorkspaceBackup` at `litehive/state/backup.py:18`
  Purpose: One successfully captured workspace database snapshot.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/state/lock_manager.py`
- `WorkspaceLockManager` at `litehive/state/lock_manager.py:13`
  Purpose: Single owner for workspace lockfile metadata.
  Bases: `-`.
  Methods:
  - `_is_held_in_process`
  - `_parse_metadata_text`
  - `read_metadata`
  - `read_locked_metadata`
  - `write_locked_metadata`
  - `clear_locked_metadata`
  - `open`
  - `lock`
  - `unlock`
  - `acquire`
  - `release`
  - `is_active`
  - `_pid_is_live`
  - `pid_is_stale`
  - `clear_metadata_if_unlocked`
  - `remove_stale_lockfile`
  Domain doc cross-check: not a domain-doc class.

### `litehive/state/process_lock.py`
- `ProcessLockManager` at `litehive/state/process_lock.py:14`
  Purpose: High-level process lock manager for runner and daemon processes.
  Bases: `-`.
  Methods:
  - `is_active`
  - `read_metadata`
  - `read_locked_metadata`
  - `write_locked_metadata`
  - `clear_metadata_if_unlocked`
  - `pid_is_stale`
  - `remove_stale_lockfile`
  - `open_locked`
  - `acquire_with_metadata`
  - `release_with_cleanup`
  - `create_base_metadata`
  - `update_heartbeat`
  - `save_process_state`
  - `clear_process_state`
  - `clear_stale_state`
  - `_runtime_store`
  Domain doc cross-check: not a domain-doc class.

### `litehive/state/rebuild_safety.py`
- `RebuildSafetyError` at `litehive/state/rebuild_safety.py:20`
  Purpose: Raised when a rebuild would drop task rows that still have evidence.
  Bases: `RuntimeError`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `RebuildSafetyReport` at `litehive/state/rebuild_safety.py:31`
  Purpose: Diagnostic snapshot returned by ``assert_database_rebuild_safe``.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/state/records.py`
- `TaskStateMissingError` at `litehive/state/records.py:42`
  Purpose: Raised when a task has no SQLite runtime state row.
  Bases: `RuntimeError`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/state/store.py`
- `RuntimeStore` at `litehive/state/store.py:39`
  Purpose: Small repository-style API over the workspace runtime database.
  Bases: `-`.
  Methods:
  - `__init__`
  - `bootstrap`
  - `_should_rebuild_from_task_event_log`
  - `load_workspace_state`
  - `load_workspace_state_read_only`
  - `save_workspace_state`
  - `load_task_state`
  - `load_task_intent`
  - `list_task_intents`
  - `save_task_intent`
  - `save_task_state`
  - `save_runtime_transaction`
  - `delete_task_records`
  - `_append_workspace_state_event`
  - `_append_runtime_transaction_events`
  - `_save_workspace_state`
  - `load_task_runtime`
  - `save_task_runtime`
  - `_save_task_state`
  - `_save_task_intent`
  - `append_task_journal`
  - `_append_task_journal`
  - `save_process_state`
  - `clear_process_state`
  - `load_process_state`
  - `highest_task_number`
  - `create_workspace_state_rows`
  Domain doc cross-check: not a domain-doc class.

### `litehive/tasks/activity.py`
- `TaskActivityLog` at `litehive/tasks/activity.py:19`
  Purpose: Workspace-scoped activity feed for one task.
  Bases: `-`.
  Methods:
  - `load`
  - `save`
  - `append`
  - `latest_entry`
  - `latest`
  Domain doc cross-check: not a domain-doc class.

### `litehive/tasks/audit.py`
- `TaskAuditState` at `litehive/tasks/audit.py:17`
  Purpose: Lightweight task snapshot for the audit log's before/after fields.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `TaskAuditEntry` at `litehive/tasks/audit.py:29`
  Purpose: Structured audit record for a task lifecycle or queue mutation.
  Bases: `BaseModel`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/tasks/event_log.py`
- `TaskEventLogReplaySummary` at `litehive/tasks/event_log.py:62`
  Purpose: Summary of a task event log replay into SQLite.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.
- `_ReplayState` at `litehive/tasks/event_log.py:84`
  Purpose: Inferred from `litehive/tasks/event_log.py` and class name: owns the _ReplayState concept.
  Bases: `-`.
  Methods:
  - `empty`
  Domain doc cross-check: not a domain-doc class.

### `litehive/tasks/journal.py`
- `TaskJournalEntry` at `litehive/tasks/journal.py:12`
  Purpose: One row of the per-task journal.
  Bases: `-`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/tasks/report_storage.py`
- `ReportReference` at `litehive/tasks/report_storage.py:15`
  Purpose: Opaque pointer to a stored report row.
  Bases: `-`.
  Methods:
  - `display`
  - `__str__`
  Domain doc cross-check: not a domain-doc class.

### `litehive/workspace.py`
- `Workspace` at `litehive/workspace.py:43`
  Purpose: Bundle of workspace identity, on-demand SQLite access, lazy config, and subpath helpers.
  Bases: `-`.
  Methods:
  - `__init__`
  - `__repr__`
  - `__eq__`
  - `__hash__`
  - `from_path`
  - `connect`
  - `load_config`
  - `config`
  - `require_existing`
  - `create`
  - `runtime_dir`
  - `runtime_path`
  - `control_dir`
  - `control_files`
  - `task_dir`
  - `list_tasks`
  - `get_task`
  - `get_task_record`
  - `require_task`
  - `save_task`
  - `task_activity`
  - `append_event`
  - `load_subagent_session`
  - `load_subagent_session_record`
  - `load_subagent_session_created_at`
  Domain doc cross-check: mentioned by name.

### `litehive/worktree/cleanup.py`
- `WorktreeCleanupResult` at `litehive/worktree/cleanup.py:100`
  Purpose: Inferred from `litehive/worktree/cleanup.py` and class name: owns the WorktreeCleanupResult concept.
  Bases: `TypedDict`.
  Methods: none.
  Domain doc cross-check: not a domain-doc class.

### `litehive/worktree/service.py`
- `WorktreeService` at `litehive/worktree/service.py:82`
  Purpose: Worktree decisions shared by lifecycle, recovery, and the worktree CLI.
  Bases: `-`.
  Methods:
  - `__init__`
  - `sync_task_worktree`
  - `collect_managed_worktrees`
  - `remove_cleanable_worktrees`
  - `collect_rescue_candidates`
  - `apply_rescue_candidate`
  - `inspect_task_worktree`
  - `task_has_missing_recorded_worktree`
  - `clear_missing_recorded_worktree`
  - `cleanup_terminal_task_worktree`
  - `require_clean_main_checkout`
  - `prune_stale_worktrees`
  - `registered_worktree_for_branch`
  - `_resolved_lifecycle_worktree`
  - `_rebase_existing_worktree_onto_local_main`
  - `_merge_origin_main`
  - `_head`
  - `_is_dirty`
  - `_has_origin`
  - `_unresolved`
  - `_stash_local_changes`
  - `_restore_local_changes`
  Domain doc cross-check: not a domain-doc class.

## Domain Doc Cross-Check

`docs/domain.md` is currently vocabulary- and module-oriented. It names core concepts, module ownership, the subagent execution boundary, and storage rules. It does not need to name every dataclass or service.
- `SubagentRunCallbacks` at `litehive/agents/callbacks.py:94`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `EngineManager` at `litehive/agents/engine_manager.py:15`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `SubagentManager` at `litehive/agents/manager.py:198`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `SubagentSessionManager` at `litehive/agents/session.py:44`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `ExecutionTrace` at `litehive/domain/agent.py:25`
  Coverage: module covered: subagent execution result models and exceptions.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `EngineFailure` at `litehive/domain/agent.py:56`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `SubagentResult` at `litehive/domain/agent.py:73`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `SubagentInactivityTimeout` at `litehive/domain/agent.py:96`
  Coverage: module covered: subagent execution result models and exceptions.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `StringEnum` at `litehive/domain/common.py:29`
  Coverage: module covered: shared enums, projections, and helpers.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `PipelineMode` at `litehive/domain/common.py:66`
  Coverage: module covered: shared enums, projections, and helpers.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `PipelineState` at `litehive/domain/common.py:81`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `TaskStage` at `litehive/domain/common.py:259`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `TaskStatus` at `litehive/domain/common.py:331`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `TaskExecutionStatus` at `litehive/domain/common.py:354`
  Coverage: module covered: shared enums, projections, and helpers.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `RuntimeStageStatus` at `litehive/domain/common.py:376`
  Coverage: module covered: shared enums, projections, and helpers.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `SubagentStatus` at `litehive/domain/common.py:391`
  Coverage: module covered: shared enums, projections, and helpers.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `PipelineStatus` at `litehive/domain/common.py:408`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `RunnerStatus` at `litehive/domain/common.py:492`
  Coverage: module covered: shared enums, projections, and helpers.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `TransientFailureKind` at `litehive/domain/common.py:509`
  Coverage: module covered: shared enums, projections, and helpers.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `Verdict` at `litehive/domain/common.py:524`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `EngineUsageRecord` at `litehive/domain/engine.py:25`
  Coverage: module covered: engine monitoring and live event-stream models.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `WorkspaceEngineMonitoring` at `litehive/domain/engine.py:51`
  Coverage: module covered: engine monitoring and live event-stream models.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `FailureDiagnostics` at `litehive/domain/failure_diagnostics.py:11`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `StateDelta` at `litehive/domain/lifecycle_deltas.py:54`
  Coverage: module covered: transition deltas and recovery trigger construction.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `IncStageRetry` at `litehive/domain/lifecycle_deltas.py:608`
  Coverage: module covered: transition deltas and recovery trigger construction.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `RememberRejection` at `litehive/domain/lifecycle_deltas.py:639`
  Coverage: module covered: transition deltas and recovery trigger construction.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `FailRejectionLoop` at `litehive/domain/lifecycle_deltas.py:705`
  Coverage: module covered: transition deltas and recovery trigger construction.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `Fail` at `litehive/domain/lifecycle_deltas.py:791`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `TaskOutcomeKind` at `litehive/domain/outcomes.py:24`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `TaskCloseReason` at `litehive/domain/outcomes.py:45`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `OutcomeReasonCode` at `litehive/domain/outcomes.py:85`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `PoolStopReason` at `litehive/domain/pool.py:17`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `PoolProgressReport` at `litehive/domain/pool.py:136`
  Coverage: module covered: pool stop reasons, pool summary reports, worktree reports, and dirty-worktree gate reports.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `PoolTaskReportEntry` at `litehive/domain/pool.py:146`
  Coverage: module covered: pool stop reasons, pool summary reports, worktree reports, and dirty-worktree gate reports.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `PoolSummaryReport` at `litehive/domain/pool.py:187`
  Coverage: module covered: pool stop reasons, pool summary reports, worktree reports, and dirty-worktree gate reports.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `DirtyWorktreeLocationKind` at `litehive/domain/pool.py:311`
  Coverage: module covered: pool stop reasons, pool summary reports, worktree reports, and dirty-worktree gate reports.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `DirtyWorktreeOwnership` at `litehive/domain/pool.py:324`
  Coverage: module covered: pool stop reasons, pool summary reports, worktree reports, and dirty-worktree gate reports.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `DirtyWorktreeFinding` at `litehive/domain/pool.py:352`
  Coverage: module covered: pool stop reasons, pool summary reports, worktree reports, and dirty-worktree gate reports.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `DirtyWorktreeGateReport` at `litehive/domain/pool.py:383`
  Coverage: module covered: pool stop reasons, pool summary reports, worktree reports, and dirty-worktree gate reports.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `TriggerEventKind` at `litehive/domain/recovery.py:34`
  Coverage: module covered: recovery enums and persisted value objects.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `RecoveryDisposition` at `litehive/domain/recovery.py:57`
  Coverage: module covered: recovery enums and persisted value objects.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `FailureFingerprint` at `litehive/domain/recovery.py:75`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `RecoveryTrigger` at `litehive/domain/recovery.py:135`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `RecoveryOutcome` at `litehive/domain/recovery.py:206`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `StageReport` at `litehive/domain/reports.py:136`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `FollowUpTaskSpec` at `litehive/domain/reports.py:174`
  Coverage: module covered: stage reports, recovery reports, task activity, and report projections.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `RecoveryEvidenceItem` at `litehive/domain/reports.py:193`
  Coverage: module covered: stage reports, recovery reports, task activity, and report projections.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `RecoveryAction` at `litehive/domain/reports.py:212`
  Coverage: module covered: stage reports, recovery reports, task activity, and report projections.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `RecoveryReport` at `litehive/domain/reports.py:227`
  Coverage: module covered: stage reports, recovery reports, task activity, and report projections.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `ExecutionEstimate` at `litehive/domain/reports.py:252`
  Coverage: module covered: stage reports, recovery reports, task activity, and report projections.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `TaskActivityEntry` at `litehive/domain/reports.py:268`
  Coverage: module covered: stage reports, recovery reports, task activity, and report projections.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `AgentRole` at `litehive/domain/roles.py:35`
  Coverage: domain module not listed explicitly.
  Recommendation: Consider adding this class or its concept family to `docs/domain.md`.
- `RuntimeGitState` at `litehive/domain/runtime.py:56`
  Coverage: module covered: runtime, interruption, subagent, and runner state models.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `RuntimeStageState` at `litehive/domain/runtime.py:70`
  Coverage: module covered: runtime, interruption, subagent, and runner state models.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `RuntimeSubagentState` at `litehive/domain/runtime.py:101`
  Coverage: module covered: runtime, interruption, subagent, and runner state models.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `RuntimeEngineSwitch` at `litehive/domain/runtime.py:145`
  Coverage: module covered: runtime, interruption, subagent, and runner state models.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `RuntimeHookRejectFingerprint` at `litehive/domain/runtime.py:163`
  Coverage: module covered: runtime, interruption, subagent, and runner state models.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `RuntimeRecoveryOutcome` at `litehive/domain/runtime.py:180`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `RuntimeFailedRunRecord` at `litehive/domain/runtime.py:204`
  Coverage: module covered: runtime, interruption, subagent, and runner state models.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `TaskOutcomeState` at `litehive/domain/runtime.py:229`
  Coverage: module covered: runtime, interruption, subagent, and runner state models.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `RuntimeInterruptionState` at `litehive/domain/runtime.py:268`
  Coverage: module covered: runtime, interruption, subagent, and runner state models.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `PipelineRuntime` at `litehive/domain/runtime.py:292`
  Coverage: module covered: runtime, interruption, subagent, and runner state models.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `ExecutionRuntime` at `litehive/domain/runtime.py:334`
  Coverage: module covered: runtime, interruption, subagent, and runner state models.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `TaskRuntime` at `litehive/domain/runtime.py:351`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `RunnerStatusState` at `litehive/domain/runtime.py:394`
  Coverage: module covered: runtime, interruption, subagent, and runner state models.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `TaskRetryPolicy` at `litehive/domain/task.py:51`
  Coverage: module covered: task and workspace records.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `TaskCreationSource` at `litehive/domain/task.py:68`
  Coverage: module covered: task and workspace records.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `GitSettings` at `litehive/domain/task.py:90`
  Coverage: module covered: task and workspace records.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `TaskIntentGitSettings` at `litehive/domain/task.py:135`
  Coverage: module covered: task and workspace records.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `TaskStateGitSettings` at `litehive/domain/task.py:151`
  Coverage: module covered: task and workspace records.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `TaskIntentRecord` at `litehive/domain/task.py:181`
  Coverage: module covered: task and workspace records.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `TaskStateRecord` at `litehive/domain/task.py:210`
  Coverage: module covered: task and workspace records.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `TaskRecord` at `litehive/domain/task.py:258`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `UnmergedWorktree` at `litehive/domain/task.py:423`
  Coverage: module covered: task and workspace records.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `WorkspaceState` at `litehive/domain/task.py:438`
  Coverage: module covered: task and workspace records.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `RunnerLockState` at `litehive/domain/task_ops.py:20`
  Coverage: module covered: task-operation result and error dataclasses.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `BlockedTask` at `litehive/domain/task_ops.py:39`
  Coverage: module covered: task-operation result and error dataclasses.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `TaskSelection` at `litehive/domain/task_ops.py:56`
  Coverage: module covered: task-operation result and error dataclasses.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `WorkspaceRepairSummary` at `litehive/domain/task_ops.py:72`
  Coverage: module covered: task-operation result and error dataclasses.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `WorkspaceConflictError` at `litehive/domain/task_ops.py:103`
  Coverage: module covered: task-operation result and error dataclasses.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `StopTaskSummary` at `litehive/domain/task_ops.py:117`
  Coverage: module covered: task-operation result and error dataclasses.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `SwitchTaskSummary` at `litehive/domain/task_ops.py:134`
  Coverage: module covered: task-operation result and error dataclasses.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `ManagedWorktree` at `litehive/domain/worktree.py:19`
  Coverage: domain module not listed explicitly.
  Recommendation: Consider adding this class or its concept family to `docs/domain.md`.
- `RescueCandidate` at `litehive/domain/worktree.py:52`
  Coverage: domain module not listed explicitly.
  Recommendation: Consider adding this class or its concept family to `docs/domain.md`.
- `RescueResult` at `litehive/domain/worktree.py:69`
  Coverage: domain module not listed explicitly.
  Recommendation: Consider adding this class or its concept family to `docs/domain.md`.
- `WorktreeMergeConflict` at `litehive/domain/worktree.py:88`
  Coverage: domain module not listed explicitly.
  Recommendation: Consider adding this class or its concept family to `docs/domain.md`.
- `WorktreeSyncResult` at `litehive/domain/worktree.py:114`
  Coverage: domain module not listed explicitly.
  Recommendation: Consider adding this class or its concept family to `docs/domain.md`.
- `TaskWorktreeInspection` at `litehive/domain/worktree.py:129`
  Coverage: domain module not listed explicitly.
  Recommendation: Consider adding this class or its concept family to `docs/domain.md`.
- `Limits` at `litehive/lifecycle/persistence.py:14`
  Coverage: module covered: persisted lifecycle TaskState.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `LastReport` at `litehive/lifecycle/persistence.py:24`
  Coverage: module covered: persisted lifecycle TaskState.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `HookRejectFingerprint` at `litehive/lifecycle/persistence.py:66`
  Coverage: module covered: persisted lifecycle TaskState.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `LastRejection` at `litehive/lifecycle/persistence.py:100`
  Coverage: module covered: persisted lifecycle TaskState.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `MergeContext` at `litehive/lifecycle/persistence.py:137`
  Coverage: module covered: persisted lifecycle TaskState.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `CommitResult` at `litehive/lifecycle/persistence.py:165`
  Coverage: module covered: persisted lifecycle TaskState.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `RejectionLoop` at `litehive/lifecycle/persistence.py:186`
  Coverage: module covered: persisted lifecycle TaskState.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `FailedRunRecord` at `litehive/lifecycle/persistence.py:217`
  Coverage: module covered: persisted lifecycle TaskState.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `TaskState` at `litehive/lifecycle/persistence.py:310`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `Persistence` at `litehive/lifecycle/persistence.py:445`
  Coverage: module covered: persisted lifecycle TaskState.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `TaskNotFound` at `litehive/lifecycle/persistence.py:689`
  Coverage: module covered: persisted lifecycle TaskState.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `SqlitePersistence` at `litehive/lifecycle/persistence.py:693`
  Coverage: module covered: persisted lifecycle TaskState.
  Recommendation: Acceptable for small value objects; add name-level docs only if the class becomes operator-facing or cross-module vocabulary.
- `SandboxLauncher` at `litehive/sandbox/launcher.py:148`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.
- `Workspace` at `litehive/workspace.py:43`
  Coverage: mentioned by name.
  Recommendation: No immediate doc action.

## SOLID Ownership Plan

The function inventory classifies every production function. The plan below groups those rows into ownership decisions so future refactors are small, testable slices instead of a single class explosion.

### Utility Functions To Keep Free

- Low-level git subprocess helpers
  Current locations: `litehive/git/ops.py`.
  Decision: Keep free inside the git-owned module for now. Consider `GitRepository(root)` only if callers repeatedly perform multi-step git workflows with the same root.
  Reason: The module is already the allowed boundary for git subprocess calls. A class would not improve SRP until it owns a workflow, not just subprocess wrappers.
- Parsers, normalizers, canonicalizers
  Current locations: `litehive/tasks/normalization.py`, `litehive/config/time_parsing.py`, enum/domain helper modules.
  Decision: Keep free unless one specific value object owns the rule.
  Reason: Pure functions are easier to test and do not need injected state.
- Render helpers that only format a value
  Current locations: small `render_*` helpers across CLI/status/prompt modules.
  Decision: Keep private/free when they only format one value. Move only larger render pipelines behind a renderer object.
  Reason: Formatting one value is utility behavior; rendering a full status/prompt surface is an object concern.
- CLI command registration and simple dispatch
  Current locations: `litehive/cli/*.py`.
  Decision: Keep Typer command functions free if they only parse inputs and dispatch. Move business decisions into services.
  Reason: CLI code is a boundary. A command class is unnecessary unless it owns substantial state, which it should not.
- Container builders
  Current locations: `litehive/container.py`.
  Decision: Keep free as DI boundary functions.
  Reason: `build_container(...)` and narrower builders are the composition root. Turning them into methods would hide the assembly point.
- Nested helpers
  Current locations: rows classified with owner `enclosing function`.
  Decision: Keep local unless the enclosing operation moves to a service.
  Reason: Local helpers often document a single algorithm step and should not become public surface.

### Functions That Should Move To Existing Classes
- `Workspace`
  Move: Simple path and identity operations currently taking `workspace` or `root`, especially where the caller only needs runtime/control paths.
  Why: `Workspace` owns validated root identity and path composition.
  Notes: Keep `Workspace` from becoming a grab bag: only direct workspace capabilities belong here. Persistence and policy should move to narrower services.
- `RuntimeStore`
  Move: Direct SQLite row operations in `litehive/state/records.py`, `litehive/state/persist.py`, and task runtime write/read helpers that mostly delegate to the store.
  Why: The store already owns direct writes to SQLite tables and transaction shape.
  Notes: Do not add lifecycle policy here; only persistence operations and projections to storage rows.
- `ProcessLockManager` / `WorkspaceLockManager`
  Move: Lower-level runner-lock metadata read/write/probe helpers from `litehive/state/locking.py`.
  Why: These classes already model lockfile/process lock mechanics.
  Notes: Keep workspace-level orchestration outside these classes; they should not load tasks or inspect queue state.
- `SubagentArtifactStore` / `SubagentSessionManager`
  Move: Subagent report/session/event-stream load/save helpers in `litehive/agents/session_store.py`, `litehive/agents/session_events.py`, and related state-record write helpers.
  Why: These classes already bind workspace/task/subagent identity and own persistence slices for subagent artifacts.
  Notes: Session persistence should not be spread across manager, state records, and loose helper functions.
- `ArtifactService`
  Move: `write_stream_artifact`, `write_text_artifact`, `remove_text_artifact`.
  Why: The class already wraps artifact-root file writes.
  Notes: Keep a temporary compatibility wrapper only if many call sites need a staged migration.
- `EngineManager`
  Move: Engine adapter lookup and resume-safe model policy helpers.
  Why: It already owns engine resolution for subagent runs.
  Notes: Quota/freeze/default routing belongs to `EngineRoutingPolicy`, not here.
- `WorktreeService`
  Move: Worktree rescue/inspection/path helpers that are currently free under `litehive/worktree/` or lifecycle worktree setup.
  Why: The service already owns worktree decisions shared by lifecycle, recovery, and CLI.
  Notes: Split first if adding these makes it too broad.

### Functions That Need New Classes
- `WorkspaceTasks` or `TaskRepository`
  Responsibility: Task record and runtime persistence API for one workspace.
  Constructor dependencies: `Workspace`, `RuntimeStore`, event/audit collaborators.
- `TaskRuntimeTransitions`
  Responsibility: In-memory task runtime transitions plus narrow persistence calls that make them atomic.
  Constructor dependencies: `WorkspaceTasks` or `RuntimeStore`, lock/guard collaborator, clock.
- `RuntimeSettingsRepository`
  Responsibility: Audited mutable runtime settings stored in SQLite.
  Constructor dependencies: `Workspace`, config-data loader, clock.
- `EngineRoutingPolicy`
  Responsibility: Select/default/freeze/quota/recovery engine decision policy.
  Constructor dependencies: `RuntimeSettingsRepository`, `LitehiveConfig`, engine monitoring repository, clock.
- `StatusSnapshotCollector`
  Responsibility: Tolerant read-only status collection for one workspace.
  Constructor dependencies: `Workspace`, config loader, runtime store, daemon/runner probes, engine monitoring repository.
- `PoolService`
  Responsibility: Pool run planning/reporting over queued workspace tasks.
  Constructor dependencies: `WorkspaceTasks`, queue store, runtime transitions, clock.
- `TaskQueueService`
  Responsibility: Queue eligibility, selection, reinsertion, and repair.
  Constructor dependencies: `WorkspaceTasks`, runtime state store, task predicates.
- `TaskReportStore`
  Responsibility: Stage and recovery report persistence/query API.
  Constructor dependencies: `Workspace`, `RuntimeStore` or SQLite connection factory.
- `TaskEventLog`
  Responsibility: Durable task event log and SQLite rebuild boundary.
  Constructor dependencies: `Workspace`, runtime store/rebuilder.
- `AgentReportService`
  Responsibility: Convert subagent activity into typed stage reports and follow-up actions.
  Constructor dependencies: `Workspace`, report store, task activity log.
- `ExecutionTraceRenderer`
  Responsibility: Parse/render execution traces from event streams and artifacts.
  Constructor dependencies: Optional artifact/session store; otherwise stateless renderer.
- `DaemonExecution` / `WorkspaceDaemon` refinements
  Responsibility: Daemon loop decisions, stop reasons, backup cadence, and cycle execution.
  Constructor dependencies: `DaemonContainer`, runner service, backup service, clock/sleeper.

### Classes To Split Or Watch
- `RuntimeStore` at `litehive/state/store.py:39`
  Method count: 27.
  Purpose: Small repository-style API over the workspace runtime database.
  Recommendation: split into workspace-state, task-state, task-intent, process-state, and subagent-counter stores behind a temporary facade.
- `Workspace` at `litehive/workspace.py:43`
  Method count: 25.
  Purpose: Bundle of workspace identity, on-demand SQLite access, lazy config, and subpath helpers.
  Recommendation: keep identity/config/path/connection; move task/session APIs to services exposed by the container.
- `WorktreeService` at `litehive/worktree/service.py:82`
  Method count: 22.
  Purpose: Worktree decisions shared by lifecycle, recovery, and the worktree CLI.
  Recommendation: split sync, cleanup, rescue, and inspection into separate collaborators.
- `GitCommitNode` at `litehive/lifecycle/nodes/system.py:487`
  Method count: 19.
  Purpose: Real ``commit`` node — plain automatic merge, no agents.
  Recommendation: watch for SRP drift before adding more methods; extract collaborators when a second responsibility appears.
- `WorkspaceLockManager` at `litehive/state/lock_manager.py:13`
  Method count: 16.
  Purpose: Single owner for workspace lockfile metadata.
  Recommendation: watch for SRP drift before adding more methods; extract collaborators when a second responsibility appears.
- `ProcessLockManager` at `litehive/state/process_lock.py:14`
  Method count: 16.
  Purpose: High-level process lock manager for runner and daemon processes.
  Recommendation: watch for SRP drift before adding more methods; extract collaborators when a second responsibility appears.
- `SubagentSessionManager` at `litehive/agents/session.py:44`
  Method count: 15.
  Purpose: Persist subagent session state and stream artifacts for one manager.
  Recommendation: watch for SRP drift before adding more methods; extract collaborators when a second responsibility appears.
- `HeruEngineAdapter` at `litehive/lifecycle/heru_factory.py:394`
  Method count: 14.
  Purpose: ``Engine`` that delegates to ``SubagentManager`` for one turn.
  Recommendation: watch for SRP drift before adding more methods; extract collaborators when a second responsibility appears.
- `SubagentManager` at `litehive/agents/manager.py:198`
  Method count: 12.
  Purpose: Run external CLI subagents inside a task-scoped folder.
  Recommendation: watch for SRP drift before adding more methods; extract collaborators when a second responsibility appears.
- `StateMachineRunner` at `litehive/lifecycle/runner.py:48`
  Method count: 10.
  Purpose: Drives a task through the state machine.
  Recommendation: watch for SRP drift before adding more methods; extract collaborators when a second responsibility appears.
- `DockerSandboxLauncher` at `litehive/sandbox/launcher.py:166`
  Method count: 10.
  Purpose: Builds docker-run argv that wraps every external engine invocation.
  Recommendation: watch for SRP drift before adding more methods; extract collaborators when a second responsibility appears.
- `PoolSummaryReport` at `litehive/domain/pool.py:187`
  Method count: 9.
  Purpose: Structured summary of one pool run.
  Recommendation: watch for SRP drift before adding more methods; extract collaborators when a second responsibility appears.
- `PipelineJournal` at `litehive/lifecycle/journal.py:90`
  Method count: 9.
  Purpose: Abstract base class for runner-event journals.
  Recommendation: watch for SRP drift before adding more methods; extract collaborators when a second responsibility appears.
- `TaskRecord` at `litehive/domain/task.py:258`
  Method count: 8.
  Purpose: The aggregate root for a single unit of work tracked by Litehive.
  Recommendation: watch for SRP drift before adding more methods; extract collaborators when a second responsibility appears.
- `RoleAgent` at `litehive/roles/base.py:78`
  Method count: 8.
  Purpose: Base for stage-bound agents.
  Recommendation: watch for SRP drift before adding more methods; extract collaborators when a second responsibility appears.

### Refactor Sequencing
1. Start with repository/service extractions that can preserve public wrappers: `RuntimeSettingsRepository`, `TaskRepository`, and `ExecutionTraceRenderer`.
2. For each extraction, add a focused characterization test against the existing public function first, then introduce the object, then route callers, then delete wrappers when the call graph is small enough.
3. Prefer constructor injection through `litehive/container.py`; do not let new classes call `Workspace.from_path`, `load_config`, or `runtime_store_for_workspace` inside their constructors.
4. Keep facades short during migration. A facade is acceptable when it preserves compatibility, but it should delegate to cohesive collaborators rather than collecting more behavior.
5. Do not split dataclasses or enums just because they have methods. Split only behavior-owning services whose responsibilities cannot be summarized in one sentence.

## Method Candidate Index

These are the top-level functions most likely to become methods or move behind focused service objects. This index intentionally excludes functions already classified as utilities or existing methods.

### `litehive/agents/artifacts.py`
- `write_stream_artifact` at line 105 -> `ArtifactService`
- `write_text_if_changed` at line 116 -> `ArtifactService`
- `write_text_artifact` at line 131 -> `ArtifactService`
- `remove_text_artifact` at line 148 -> `ArtifactService`

### `litehive/agents/command_policy.py`
- `agent_command_is_allowed` at line 30 -> `new focused service for module concern`

### `litehive/agents/engine_callables.py`
- `resolve_cli_execution_callable` at line 10 -> `resolver service for the module concern`

### `litehive/agents/execution_trace.py`
- `render_event_for_execution_trace` at line 82 -> `renderer object for the module concern`
- `render_execution_trace_from_events` at line 114 -> `renderer object for the module concern`
- `render_execution_trace` at line 132 -> `renderer object for the module concern`
- `recovered_timeline_from_events` at line 146 -> `new focused service for module concern`
- `render_execution_trace_from_streams` at line 184 -> `renderer object for the module concern`
- `render_execution_trace_from_event_stream_payload` at line 202 -> `renderer object for the module concern`
- `load_subagent_execution_trace` at line 225 -> `Workspace or focused workspace service`

### `litehive/agents/manager.py`
- `_latest_report_files_changed` at line 103 -> `Workspace or focused service`

### `litehive/agents/merge_resolver.py`
- `run_worktree_merge_agent` at line 32 -> `Workspace or focused workspace service`

### `litehive/agents/report_extraction.py`
- `stage_report_from_subagent` at line 46 -> `new focused service for module concern`

### `litehive/agents/session_continuation.py`
- `subagent_continuation_state` at line 48 -> `new focused service for module concern`

### `litehive/agents/session_store.py`
- `subagent_artifacts` at line 214 -> `SubagentSessionStore / bound subagent artifact store`
- `_load_subagent_payload` at line 221 -> `Workspace or focused service`
- `load_subagent_artifacts` at line 249 -> `SubagentSessionStore / bound subagent artifact store`
- `_load_subagent_artifact_slice` at line 254 -> `Workspace or focused service`
- `load_subagent_session_record` at line 269 -> `SubagentSessionStore / bound subagent artifact store`
- `load_subagent_session` at line 276 -> `SubagentSessionStore / bound subagent artifact store`
- `load_subagent_report` at line 281 -> `SubagentSessionStore / bound subagent artifact store`
- `load_subagent_event_stream` at line 286 -> `SubagentSessionStore / bound subagent artifact store`

### `litehive/attention.py`
- `read_attention_log` at line 109 -> `AttentionRepository`
- `collect_operator_needed_state_for_workspace` at line 130 -> `AttentionRepository`
- `waiting_for_you_lines_for_workspace` at line 154 -> `AttentionRepository`

### `litehive/cli/daemon_cli.py`
- `daemon_run` at line 31 -> `Workspace or focused workspace service`
- `daemon_status` at line 45 -> `Workspace or focused workspace service`
- `daemon_stop` at line 65 -> `Workspace or focused workspace service`
- `daemon_restart` at line 78 -> `Workspace or focused workspace service`
- `daemon_worker` at line 90 -> `Workspace or focused workspace service`

### `litehive/cli/engine.py`
- `_engine_status_command` at line 67 -> `Workspace or focused service`
- `_engine_audit_command` at line 77 -> `Workspace or focused service`
- `_engine_default_command` at line 83 -> `Workspace or focused service`
- `_engine_preference_command` at line 99 -> `Workspace or focused service`
- `_engine_freeze_command` at line 124 -> `Workspace or focused service`
- `_engine_unfreeze_command` at line 145 -> `Workspace or focused service`
- `_render_engine_audit_lines` at line 224 -> `Workspace or focused service`

### `litehive/cli/pipeline_cli.py`
- `_print_pipeline_report_lines` at line 142 -> `Workspace or focused service`

### `litehive/cli/pool.py`
- `task_stage_outcomes_for_workspace` at line 9 -> `Workspace or focused workspace service`
- `_pool_task_report_entry_for_workspace` at line 20 -> `Workspace or focused service`
- `_pending_pool_tasks_for_workspace` at line 54 -> `Workspace or focused service`
- `_resumable_pool_tasks_for_workspace` at line 74 -> `Workspace or focused service`
- `_closed_pool_tasks_for_workspace` at line 98 -> `Workspace or focused service`
- `_pool_summary_report_data_for_workspace` at line 179 -> `Workspace or focused service`
- `_write_pool_summary_report` at line 298 -> `Workspace or focused service`

### `litehive/cli/queue_cli.py`
- `stop` at line 219 -> `Workspace or focused workspace service`

### `litehive/cli/runner.py`
- `start` at line 80 -> `Workspace or focused workspace service`
- `daemon_status` at line 105 -> `Workspace or focused workspace service`
- `stop` at line 120 -> `Workspace or focused workspace service`
- `restart` at line 142 -> `Workspace or focused workspace service`
- `daemon_worker` at line 170 -> `Workspace or focused workspace service`
- `_existing_consecutive_task_failure_stop` at line 295 -> `Workspace or focused service`
- `_workspace_has_dirty_non_litehive_changes` at line 396 -> `Workspace or focused service`
- `run_command` at line 464 -> `Workspace or focused workspace service`
- `backup_create` at line 596 -> `Workspace or focused workspace service`
- `backup_list` at line 619 -> `Workspace or focused workspace service`
- `db_status` at line 688 -> `Workspace or focused workspace service`
- `db_migrate` at line 708 -> `Workspace or focused workspace service`
- `db_rebuild_from_events` at line 746 -> `Workspace or focused workspace service`
- `db_settings` at line 811 -> `Workspace or focused workspace service`

### `litehive/cli/task_cli.py`
- `_show_dependency_label` at line 73 -> `Workspace or focused service`
- `list_tasks_command` at line 281 -> `Workspace or focused workspace service`

### `litehive/cli/task_debug_support.py`
- `render_task_evidence_for_workspace` at line 24 -> `Workspace or focused workspace service`
- `debug_all_for_workspace` at line 40 -> `Workspace or focused workspace service`
- `debug_latest_for_workspace` at line 60 -> `Workspace or focused workspace service`
- `_print_lifecycle_evidence` at line 71 -> `Workspace or focused service`
- `_print_latest_report` at line 123 -> `Workspace or focused service`
- `_print_latest_activity` at line 144 -> `Workspace or focused service`
- `_print_latest_subagent` at line 164 -> `Workspace or focused service`
- `debug_worktree_for_workspace` at line 229 -> `Workspace or focused workspace service`
- `_print_worktree_evidence` at line 238 -> `Workspace or focused service`
- `_read_exit_code` at line 268 -> `Workspace or focused service`

### `litehive/cli/task_logs_support.py`
- `show_latest_daemon_log_for_workspace` at line 26 -> `Workspace or focused workspace service`
- `list_daemon_sessions_for_workspace` at line 46 -> `Workspace or focused workspace service`
- `show_task_journal_for_workspace` at line 74 -> `Workspace or focused workspace service`
- `show_latest_subagent_for_workspace` at line 89 -> `Workspace or focused workspace service`
- `list_task_subagents_for_workspace` at line 102 -> `Workspace or focused workspace service`
- `follow_active_subagent_for_workspace` at line 134 -> `Workspace or focused workspace service`
- `resolve_follow_task_for_workspace` at line 375 -> `Workspace or focused workspace service`
- `load_task_with_runtime_for_workspace` at line 394 -> `Workspace or focused workspace service`

### `litehive/cli/workspace.py`
- `status_command` at line 135 -> `Workspace or focused workspace service`
- `repair_command` at line 202 -> `Workspace or focused workspace service`
- `health_command` at line 252 -> `Workspace or focused workspace service`
- `health_daemon_status_for_workspace` at line 320 -> `Workspace or focused workspace service`

### `litehive/cli/worktree_cli.py`
- `ls` at line 22 -> `Workspace or focused workspace service`
- `clean` at line 55 -> `Workspace or focused workspace service`
- `rescue` at line 110 -> `Workspace or focused workspace service`

### `litehive/config/engine_freezes.py`
- `is_engine_frozen` at line 17 -> `new focused service for module concern`
- `active_engine_freezes` at line 31 -> `new focused service for module concern`
- `persist_engine_freeze_iso_for_workspace` at line 51 -> `Workspace or focused workspace service`
- `clear_persisted_engine_freeze_for_workspace` at line 82 -> `Workspace or focused workspace service`

### `litehive/config/engine_models.py`
- `_persist_engine_freeze` at line 112 -> `Workspace or focused service`
- `_clear_engine_freeze` at line 141 -> `Workspace or focused service`
- `select_engine_for_workspace` at line 156 -> `Workspace or focused workspace service`
- `resolve_model` at line 248 -> `resolver service for the module concern`
- `resolve_engine_name` at line 273 -> `resolver service for the module concern`
- `resolve_engine_attempt_order` at line 294 -> `resolver service for the module concern`
- `resolve_engine_plan` at line 321 -> `resolver service for the module concern`
- `resolve_task_retry_policy` at line 346 -> `resolver service for the module concern`
- `resolve_task_rejection_loop_limit` at line 377 -> `resolver service for the module concern`

### `litehive/config/engine_quota.py`
- `engine_quota_block` at line 101 -> `new focused service for module concern`
- `collect_engine_quota_statuses` at line 120 -> `collector/query service for the module concern`

### `litehive/config/loading.py`
- `merge_config_layers` at line 41 -> `new focused service for module concern`
- `load_effective_config_data_for_workspace` at line 61 -> `Workspace or focused workspace service`
- `load_config_for_workspace` at line 76 -> `Workspace or focused workspace service`
- `load_context_for_workspace` at line 94 -> `Workspace or focused workspace service`

### `litehive/config/model.py`
- `validate_config_data` at line 315 -> `new focused service for module concern`

### `litehive/config/profiles/loader.py`
- `available_process_profiles` at line 19 -> `new focused service for module concern`
- `resolve_process_profile` at line 24 -> `resolver service for the module concern`

### `litehive/config/profiles/rendering.py`
- `render_context_template` at line 95 -> `renderer object for the module concern`

### `litehive/config/runtime_settings.py`
- `_bootstrap_config_data` at line 73 -> `Workspace or focused service`
- `bootstrap_runtime_settings` at line 217 -> `Workspace or focused workspace service`
- `load_runtime_settings` at line 253 -> `Workspace or focused workspace service`
- `apply_runtime_settings_to_config_data` at line 269 -> `Workspace or focused workspace service`
- `set_runtime_setting` at line 290 -> `Workspace or focused workspace service`
- `set_default_engine` at line 359 -> `Workspace or focused workspace service`
- `set_engine_preference` at line 385 -> `Workspace or focused workspace service`
- `set_engine_freeze` at line 412 -> `Workspace or focused workspace service`
- `clear_engine_freeze` at line 449 -> `Workspace or focused workspace service`
- `load_runtime_setting_audit_entries` at line 533 -> `Workspace or focused workspace service`

### `litehive/config/workspace.py`
- `render_workspace_gitignore` at line 27 -> `renderer object for the module concern`
- `require_existing_workspace` at line 52 -> `Workspace, WorktreeService, or GitRepository depending on module`
- `_task_exists_in_workspace` at line 101 -> `Workspace or focused service`
- `resolve_workspace` at line 112 -> `resolver service for the module concern`
- `create_workspace` at line 142 -> `Workspace, WorktreeService, or GitRepository depending on module`

### `litehive/container.py`
- `build_container` at line 56 -> `Workspace, WorktreeService, or GitRepository depending on module`
- `build_pipeline_container` at line 72 -> `Workspace, WorktreeService, or GitRepository depending on module`
- `build_daemon_container` at line 84 -> `Workspace, WorktreeService, or GitRepository depending on module`
- `build_workspace` at line 96 -> `Workspace, WorktreeService, or GitRepository depending on module`
- `build_agent_report_submitter` at line 107 -> `DI container factory module`
- `build_agent_task_mutator_for_workspace` at line 134 -> `DI container factory module`
- `build_subagent_manager_for_workspace` at line 144 -> `DI container factory module`

### `litehive/daemon/execution.py`
- `register_daemon` at line 73 -> `Workspace or focused workspace service`
- `unregister_daemon` at line 83 -> `Workspace or focused workspace service`
- `_halt_for_origin_divergence` at line 158 -> `Workspace or focused service`
- `sleep_with_stop` at line 179 -> `new focused service for module concern`
- `_daemon_status_snapshot_for_workspace` at line 197 -> `Workspace or focused service`
- `create_workspace_venvs_ready_for_workspace` at line 313 -> `Workspace or focused workspace service`
- `maybe_run_workspace_backup` at line 331 -> `Workspace or focused workspace service`
- `run_logged_subprocess` at line 354 -> `new focused service for module concern`
- `run_daemon_loop` at line 536 -> `Workspace or focused workspace service`
- `_daemon_heartbeat_loop` at line 567 -> `Workspace or focused service`
- `start_background_daemon` at line 587 -> `Workspace or focused workspace service`
- `stop_workspace_daemon` at line 644 -> `Workspace or focused workspace service`
- `daemon_status_lines` at line 671 -> `Workspace or focused workspace service`
- `daemon_status_lines_for_workspace` at line 682 -> `Workspace or focused workspace service`

### `litehive/daemon/logs.py`
- `latest_run_all_log_dir_for_workspace` at line 64 -> `Workspace or focused workspace service`
- `prune_run_all_log_dirs` at line 77 -> `new focused service for module concern`
- `latest_matching` at line 99 -> `new focused service for module concern`

### `litehive/daemon/registry.py`
- `_daemon_lock_key_for_workspace` at line 33 -> `Workspace or focused service`
- `_daemon_lock_path_for_workspace` at line 85 -> `Workspace or focused service`
- `_daemon_lock_is_held_in_process` at line 97 -> `Workspace or focused service`
- `_daemon_lock_manager_for_workspace` at line 111 -> `Workspace or focused service`
- `daemon_lock_is_active_for_workspace` at line 134 -> `Workspace or focused workspace service`
- `_clear_stale_daemon_metadata_for_workspace` at line 146 -> `Workspace or focused service`
- `daemon_metadata_for_workspace` at line 160 -> `Workspace or focused workspace service`
- `get_workspace_daemon_for_workspace` at line 181 -> `Workspace or focused workspace service`
- `register_daemon_for_workspace` at line 198 -> `Workspace or focused workspace service`
- `unregister_daemon_for_workspace` at line 248 -> `Workspace or focused workspace service`
- `touch_daemon_for_workspace` at line 274 -> `Workspace or focused workspace service`
- `stale_daemon_metadata_for_workspace` at line 302 -> `Workspace or focused workspace service`

### `litehive/daemon/termination.py`
- `wait_for_pid_exit` at line 22 -> `new focused service for module concern`
- `force_kill_recorded_daemon` at line 41 -> `Workspace or focused workspace service`
- `terminate_recorded_daemon` at line 70 -> `Workspace or focused workspace service`
- `terminate_child_process` at line 99 -> `new focused service for module concern`

### `litehive/domain/common.py`
- `runner_hook_points` at line 324 -> `existing domain value object or new domain service`
- `task_stage_for_pipeline_state` at line 446 -> `existing domain value object or new domain service`
- `pipeline_stage_key` at line 459 -> `existing domain value object or new domain service`
- `pipeline_status_for_pipeline_state` at line 479 -> `existing domain value object or new domain service`

### `litehive/domain/failure_diagnostics.py`
- `empty_failure_diagnostics` at line 38 -> `existing domain value object or new domain service`

### `litehive/domain/lifecycle_deltas.py`
- `recovery_trigger_from_event` at line 278 -> `existing domain value object or new domain service`
- `rejection_loop_detected` at line 426 -> `existing domain value object or new domain service`
- `enter_recovery` at line 455 -> `existing domain value object or new domain service`
- `enter_pre_exec_recovery` at line 487 -> `existing domain value object or new domain service`
- `record_recovery_success` at line 560 -> `existing domain value object or new domain service`
- `clear_completed_rejection_loop` at line 746 -> `existing domain value object or new domain service`
- `stash_conflict_files` at line 766 -> `existing domain value object or new domain service`
- `exhaust_recovery_budget` at line 870 -> `existing domain value object or new domain service`

### `litehive/domain/recovery.py`
- `blocked_on_follow_up_reason` at line 265 -> `existing domain value object or new domain service`

### `litehive/domain/reports.py`
- `classify_task_activity_verdict` at line 62 -> `existing domain value object or new domain service`

### `litehive/domain/roles.py`
- `agent_startup_guidance_keys` at line 121 -> `existing domain value object or new domain service`
- `known_agent_role` at line 139 -> `existing domain value object or new domain service`
- `agent_activity_verdicts_for_role` at line 155 -> `existing domain value object or new domain service`
- `agent_verdict_requires_target_stage` at line 169 -> `existing domain value object or new domain service`
- `agent_stage_for_task` at line 176 -> `existing domain value object or new domain service`

### `litehive/domain/task.py`
- `canonicalize_task_terminal_state` at line 25 -> `existing domain value object or new domain service`

### `litehive/git/ops.py`
- `check_origin_divergence` at line 180 -> `Workspace or focused service`

### `litehive/lifecycle/guards.py`
- `mode` at line 75 -> `new focused service for module concern`
- `stage_retries_remaining` at line 95 -> `new focused service for module concern`
- `stage_retries_exhausted` at line 110 -> `new focused service for module concern`
- `last_hook_ok` at line 126 -> `new focused service for module concern`
- `hook_reject_loop_detected` at line 142 -> `new focused service for module concern`
- `rejection_loop_detected` at line 160 -> `new focused service for module concern`
- `zero_change_shortcut` at line 177 -> `new focused service for module concern`
- `pre_exec_budget_remaining` at line 194 -> `new focused service for module concern`
- `recovery_budget_available` at line 211 -> `new focused service for module concern`
- `recovery_budget_exhausted` at line 227 -> `new focused service for module concern`
- `recovery_resume_is_concrete` at line 241 -> `new focused service for module concern`

### `litehive/lifecycle/heru_factory.py`
- `_execution_checkout_path` at line 142 -> `Workspace or focused service`
- `_recovery_execution_root` at line 155 -> `Workspace or focused service`
- `_agent_execution_root` at line 177 -> `Workspace or focused service`
- `execution_checkout_status` at line 186 -> `Workspace or focused workspace service`
- `_display_path` at line 200 -> `Workspace or focused service`
- `_rewrite_hallucinated_implementing_pass` at line 212 -> `Workspace or focused service`
- `latest_verdict_after` at line 335 -> `Workspace or focused workspace service`
- `heru_engine_factory` at line 819 -> `Workspace or focused workspace service`

### `litehive/lifecycle/hook_reports.py`
- `hook_specs_from_config` at line 69 -> `new focused service for module concern`
- `_record_hook_warnings` at line 127 -> `Workspace or focused service`
- `_record_hook_reject` at line 174 -> `Workspace or focused service`

### `litehive/lifecycle/orchestration.py`
- `run_task_for_workspace` at line 97 -> `Workspace or focused workspace service`
- `_observe_transition` at line 208 -> `Workspace or focused service`

### `litehive/lifecycle/persistence.py`
- `failed_run_key` at line 297 -> `SqlitePersistence`

### `litehive/lifecycle/prompt_serializer.py`
- `serialize_prompt` at line 59 -> `new focused service for module concern`
- `_load_task_activity_history` at line 146 -> `Workspace or focused service`

### `litehive/lifecycle/runtime_sync.py`
- `_sync_recovery_follow_up` at line 363 -> `Workspace or focused service`
- `_clear_terminal_task_from_workspace_state` at line 418 -> `Workspace or focused service`

### `litehive/lifecycle/transitions.py`
- `evaluate` at line 123 -> `new focused service for module concern`
- `resume_from_origin` at line 152 -> `new focused service for module concern`
- `resume_from_pre_exec` at line 178 -> `new focused service for module concern`
- `entry_from_worktree_sync` at line 196 -> `new focused service for module concern`
- `retry_epoch_rules` at line 218 -> `new focused service for module concern`
- `list_transitions` at line 280 -> `new focused service for module concern`

### `litehive/lifecycle/types.py`
- `before` at line 28 -> `new focused service for module concern`
- `after` at line 40 -> `new focused service for module concern`
- `pipeline_stage_for_phase` at line 69 -> `new focused service for module concern`

### `litehive/lifecycle/worktree_setup.py`
- `_resolve_worktree_for_workspace` at line 29 -> `Workspace or focused service`
- `_resolve_hook_execution_root_for_workspace` at line 35 -> `Workspace or focused service`
- `_task_recorded_worktree_for_workspace` at line 49 -> `Workspace or focused service`
- `build_commit_node_for_workspace` at line 67 -> `Workspace or focused workspace service`
- `_build_worktree_sync_node` at line 76 -> `Workspace or focused service`
- `_worktree_missing_probe` at line 84 -> `Workspace or focused service`
- `_worktree_metadata_repair` at line 102 -> `Workspace or focused service`
- `_mark_task_interrupted_on_crash` at line 119 -> `Workspace or focused service`
- `_cleanup_terminal_worktree` at line 141 -> `Workspace or focused service`
- `reconcile_terminal_commit_sha_for_workspace` at line 153 -> `Workspace or focused workspace service`

### `litehive/observability/engine_monitoring.py`
- `load_engine_monitoring` at line 28 -> `Workspace or focused workspace service`
- `save_engine_monitoring` at line 41 -> `Workspace or focused workspace service`
- `_load_engine_monitoring_from_db` at line 55 -> `Workspace or focused service`
- `_save_engine_monitoring_to_db` at line 88 -> `Workspace or focused service`
- `record_engine_execution` at line 115 -> `Workspace or focused workspace service`
- `record_engine_observation` at line 164 -> `Workspace or focused workspace service`

### `litehive/observability/events.py`
- `append_event` at line 35 -> `Workspace or focused workspace service`
- `read_events` at line 69 -> `Workspace or focused workspace service`
- `last_event_timestamp` at line 100 -> `Workspace or focused workspace service`
- `append_session_log` at line 126 -> `event log or repository object for the module concern`
- `ensure_session_log` at line 152 -> `new focused service for module concern`

### `litehive/observability/status.py`
- `collect_task_pipeline_status_for_workspace` at line 121 -> `Workspace or focused workspace service`
- `render_task_pipeline_status_lines` at line 170 -> `renderer object for the module concern`
- `_runner_state_label_for_workspace` at line 246 -> `Workspace or focused service`
- `_load_task_read_only_for_workspace` at line 266 -> `Workspace or focused service`
- `render_active_task_detail_lines` at line 324 -> `renderer object for the module concern`
- `render_runner_status_line` at line 346 -> `renderer object for the module concern`
- `render_detailed_status_header_lines` at line 366 -> `Workspace or focused workspace service`
- `render_runtime_policy_lines` at line 405 -> `renderer object for the module concern`
- `render_engine_availability_lines` at line 428 -> `renderer object for the module concern`

### `litehive/observability/status_dashboard.py`
- `render_active_task_section` at line 26 -> `renderer object for the module concern`
- `render_active_tasks_section` at line 60 -> `renderer object for the module concern`
- `find_last_completed_task` at line 91 -> `new focused service for module concern`
- `render_last_completed_section` at line 107 -> `renderer object for the module concern`
- `render_queue_section` at line 127 -> `renderer object for the module concern`
- `collect_recent_activity` at line 154 -> `Workspace or focused workspace service`
- `render_recent_activity_section` at line 204 -> `renderer object for the module concern`

### `litehive/observability/status_diagnostics.py`
- `collect_status_snapshot_for_workspace` at line 63 -> `Workspace or focused workspace service`
- `collect_operational_status_snapshot_for_workspace` at line 101 -> `Workspace or focused workspace service`

### `litehive/observability/status_health.py`
- `render_health_active_task_lines` at line 23 -> `renderer object for the module concern`
- `render_health_flagged_task_lines` at line 43 -> `renderer object for the module concern`
- `render_health_worktree_lines` at line 68 -> `renderer object for the module concern`
- `render_health_worktree_finding_lines` at line 95 -> `renderer object for the module concern`
- `render_health_quota_lines` at line 124 -> `renderer object for the module concern`
- `render_health_daemon_lines` at line 140 -> `renderer object for the module concern`
- `render_health_recent_completion_lines` at line 157 -> `renderer object for the module concern`

### `litehive/observability/status_loaders.py`
- `_load_config_for_status_for_workspace` at line 36 -> `Workspace or focused service`
- `_load_state_for_status` at line 143 -> `Workspace or focused service`
- `_load_engine_monitoring_for_status` at line 191 -> `Workspace or focused service`
- `_load_runner_status_for_status_for_workspace` at line 217 -> `Workspace or focused service`

### `litehive/observability/status_probes.py`
- `_probe_runner_state_for_workspace` at line 41 -> `Workspace or focused service`
- `_probe_daemon_status_for_workspace` at line 91 -> `Workspace or focused service`
- `_probe_last_cycle_for_workspace` at line 129 -> `Workspace or focused service`
- `_probe_heru_link_for_workspace` at line 167 -> `Workspace or focused service`
- `_probe_origin_divergence_for_workspace` at line 209 -> `Workspace or focused service`
- `_probe_task_index_references_for_workspace` at line 261 -> `Workspace or focused service`
- `_probe_task_status_damage` at line 318 -> `Workspace or focused service`
- `_recovery_failure_issue` at line 395 -> `Workspace or focused service`
- `_recovery_failure_context` at line 431 -> `Workspace or focused service`

### `litehive/observability/status_rendering.py`
- `status_has_problems` at line 14 -> `new focused service for module concern`
- `render_health_summary` at line 26 -> `renderer object for the module concern`
- `render_issue_lines` at line 40 -> `renderer object for the module concern`
- `render_operational_issue_lines` at line 54 -> `renderer object for the module concern`

### `litehive/observability/status_summary.py`
- `estimate_task_execution` at line 32 -> `Workspace or focused workspace service`
- `_collect_report_durations` at line 68 -> `Workspace or focused service`
- `_latest_stage_report_for_task` at line 115 -> `Workspace or focused service`
- `_latest_stage_failure_classification` at line 175 -> `Workspace or focused service`
- `render_task_summary` at line 197 -> `renderer object for the module concern`

### `litehive/observability/venv_health.py`
- `discover_workspace_venvs_for_workspace` at line 50 -> `Workspace or focused workspace service`
- `probe_broken_venv_executables_for_workspace` at line 79 -> `Workspace or focused workspace service`
- `broken_venv_issue_message` at line 108 -> `new focused service for module concern`
- `daemon_broken_venv_message` at line 126 -> `new focused service for module concern`

### `litehive/recovery/execution_recovery.py`
- `recover_stale_runner_state_for_workspace` at line 42 -> `Workspace or focused workspace service`

### `litehive/recovery/interrupted_subagent.py`
- `mark_interrupted_subagent` at line 19 -> `Workspace or focused workspace service`
- `_interrupted_subagent_snippet` at line 63 -> `Workspace or focused service`
- `_write_interrupted_subagent_artifacts` at line 116 -> `Workspace or focused service`

### `litehive/recovery/interruption_state.py`
- `prepare_interrupted_task` at line 26 -> `Workspace or focused workspace service`
- `interruption_journal_message` at line 63 -> `new focused service for module concern`
- `stale_interruption_reason` at line 98 -> `new focused service for module concern`

### `litehive/recovery/nonrunning_resumable_repair.py`
- `has_nonrunning_resumable_repair_candidates` at line 118 -> `Workspace or focused workspace service`

### `litehive/recovery/running_task_recovery.py`
- `running_task_ids` at line 59 -> `Workspace or focused workspace service`
- `should_requeue_commit_stage_task` at line 83 -> `new focused service for module concern`
- `can_attempt_stale_runner_recovery` at line 99 -> `Workspace or focused workspace service`
- `recover_running_tasks` at line 124 -> `Workspace or focused workspace service`
- `update_active_task_after_recovery` at line 169 -> `Workspace or focused workspace service`
- `_has_inactive_running_tasks` at line 218 -> `Workspace or focused service`
- `_record_stale_recovery` at line 248 -> `Workspace or focused service`
- `_recover_stale_running_task` at line 286 -> `Workspace or focused service`
- `_task_state_row_exists` at line 331 -> `Workspace or focused service`

### `litehive/recovery/scope_analysis.py`
- `analyze_scope_changes` at line 30 -> `Workspace or focused workspace service`
- `_is_file_broken_on_main` at line 83 -> `Workspace or focused service`
- `_is_test_broken_on_main` at line 120 -> `Workspace or focused service`
- `_has_syntax_errors_on_main` at line 156 -> `Workspace or focused service`

### `litehive/recovery/workspace_repair.py`
- `repair_workspace_state` at line 27 -> `Workspace or focused workspace service`
- `_normalize_stale_terminal_tasks` at line 44 -> `Workspace or focused service`
- `_stale_terminal_candidate_ids` at line 127 -> `Workspace or focused service`

### `litehive/roles/base.py`
- `_latest_reject_stage_for_implementing` at line 355 -> `Workspace or focused service`

### `litehive/roles/guidance.py`
- `default_startup_guidance` at line 27 -> `new focused service for module concern`

### `litehive/roles/recovery.py`
- `_recovery_source_checkout` at line 292 -> `Workspace or focused service`
- `_recovery_source_checkout_diagnostic` at line 316 -> `Workspace or focused service`
- `_failed_subagent_diagnostics_payload` at line 326 -> `Workspace or focused service`

### `litehive/sandbox/git_wrapper.py`
- `main` at line 23 -> `new focused service for module concern`
- `rejection_reason` at line 43 -> `new focused service for module concern`

### `litehive/sandbox/support.py`
- `forced_engine_rw_state_dirs` at line 7 -> `new focused service for module concern`
- `sanitize_path_env` at line 60 -> `new focused service for module concern`

### `litehive/state/backup.py`
- `_backup_path_for_workspace` at line 45 -> `Workspace or focused service`
- `list_workspace_backups_for_workspace` at line 78 -> `Workspace or focused workspace service`
- `prune_workspace_backups_for_workspace` at line 98 -> `Workspace or focused workspace service`
- `create_workspace_backup_for_workspace` at line 136 -> `Workspace or focused workspace service`
- `create_scheduled_workspace_backup_for_workspace` at line 195 -> `Workspace or focused workspace service`
- `restore_workspace_backup_for_workspace` at line 213 -> `Workspace or focused workspace service`

### `litehive/state/locking.py`
- `_runner_lock_key_for_workspace` at line 35 -> `Workspace or focused service`
- `_runner_lock_manager_for_workspace` at line 59 -> `Workspace or focused service`
- `workspace_lock_for_workspace` at line 83 -> `Workspace or focused workspace service`
- `write_runner_lock_metadata` at line 103 -> `writer/store object for the module concern`
- `_save_runner_process_state_for_workspace` at line 123 -> `Workspace or focused service`
- `_clear_runner_process_state_for_workspace` at line 138 -> `Workspace or focused service`
- `read_runner_lock_metadata_for_workspace` at line 149 -> `Workspace or focused workspace service`
- `runner_metadata_present` at line 163 -> `new focused service for module concern`
- `runner_lock_is_active_for_workspace` at line 183 -> `Workspace or focused workspace service`
- `runner_status_needs_reconciliation_for_workspace` at line 191 -> `Workspace or focused workspace service`
- `clear_runner_lock_metadata_for_workspace` at line 212 -> `Workspace or focused workspace service`
- `heartbeat_is_late` at line 226 -> `new focused service for module concern`
- `runner_status_for_workspace` at line 246 -> `Workspace or focused workspace service`
- `touch_runner_status_for_workspace` at line 268 -> `Workspace or focused workspace service`
- `runner_heartbeat_for_workspace` at line 294 -> `Workspace or focused workspace service`
- `current_thread_owns_runner_guard_for_workspace` at line 332 -> `Workspace or focused workspace service`
- `runner_pid_is_alive` at line 343 -> `new focused service for module concern`
- `subagent_process_is_stale` at line 369 -> `new focused service for module concern`
- `runner_lock_pid_is_stale_for_workspace` at line 386 -> `Workspace or focused workspace service`
- `runner_lock_is_held_for_workspace` at line 393 -> `Workspace or focused workspace service`
- `runner_conflict_message_for_workspace` at line 403 -> `Workspace or focused workspace service`
- `_auto_repair_stale_state` at line 435 -> `Workspace or focused service`
- `workspace_runner_guard` at line 464 -> `Workspace or focused workspace service`
- `workspace_mutation_guard_for_workspace` at line 542 -> `Workspace or focused workspace service`
- `ensure_future_task_mutation_allowed_for_workspace` at line 562 -> `Workspace or focused workspace service`
- `persist_future_task_update_for_workspace` at line 606 -> `Workspace or focused workspace service`

### `litehive/state/persist.py`
- `skip_bootstrap_load_state` at line 26 -> `new focused service for module concern`
- `load_state_for_workspace` at line 42 -> `Workspace or focused workspace service`
- `atomic_write_text` at line 61 -> `new focused service for module concern`
- `atomic_write_gzip_text` at line 84 -> `new focused service for module concern`
- `write_atomic_files` at line 116 -> `writer/store object for the module concern`
- `write_atomic_files_and_then` at line 143 -> `writer/store object for the module concern`
- `save_state_for_workspace` at line 171 -> `Workspace or focused workspace service`
- `save_state_without_runner_guard_for_workspace` at line 183 -> `Workspace or focused workspace service`
- `record_task_completion_for_workspace` at line 206 -> `Workspace or focused workspace service`
- `set_pool_stop_reason_for_workspace` at line 230 -> `Workspace or focused workspace service`
- `merged_state_for_runner_owned_write_for_workspace` at line 298 -> `Workspace or focused workspace service`
- `persist_task_and_state_for_workspace` at line 322 -> `Workspace or focused workspace service`
- `persist_tasks_and_state_for_workspace` at line 352 -> `Workspace or focused workspace service`
- `persist_tasks_and_state_without_runner_guard_for_workspace` at line 389 -> `Workspace or focused workspace service`
- `persist_task_and_state_without_runner_guard_for_workspace` at line 425 -> `Workspace or focused workspace service`

### `litehive/state/rebuild_safety.py`
- `sqlite_task_ids` at line 47 -> `new focused service for module concern`
- `task_artifact_dir_ids_for_workspace` at line 73 -> `Workspace or focused workspace service`
- `event_log_replay_task_ids_for_workspace` at line 100 -> `Workspace or focused workspace service`
- `assert_database_rebuild_safe_for_workspace` at line 137 -> `Workspace or focused workspace service`
- `backup_database_before_rebuild_for_workspace` at line 185 -> `Workspace or focused workspace service`

### `litehive/state/records.py`
- `_highest_task_number_in_store_for_workspace` at line 53 -> `Workspace or focused service`
- `_reserve_next_task_numbers_for_workspace` at line 64 -> `Workspace or focused service`
- `_task_creation_stage_for_workspace` at line 86 -> `Workspace or focused service`
- `_default_task_creation_source_for_workspace` at line 112 -> `Workspace or focused service`
- `ensure_runtime_ignored_for_workspace` at line 140 -> `Workspace or focused workspace service`
- `task_state_for_storage` at line 155 -> `new focused service for module concern`
- `write_task_runtime_for_workspace` at line 170 -> `Workspace or focused workspace service`
- `set_task_commit_sha` at line 182 -> `new focused service for module concern`
- `get_task_worktree_path` at line 195 -> `new focused service for module concern`
- `set_task_worktree_path` at line 206 -> `new focused service for module concern`
- `clear_task_worktree_path` at line 218 -> `repository/store object for the module concern`
- `_persist_created_tasks_for_workspace` at line 321 -> `Workspace or focused service`
- `save_task_runtime_for_workspace` at line 379 -> `Workspace or focused workspace service`
- `_load_task_runtime_for_workspace` at line 392 -> `Workspace or focused service`
- `create_task_for_workspace` at line 411 -> `Workspace or focused workspace service`
- `create_follow_up_tasks_for_workspace` at line 566 -> `Workspace or focused workspace service`
- `discard_created_task_for_workspace` at line 639 -> `Workspace or focused workspace service`
- `_load_tasks_from_store_for_workspace` at line 680 -> `Workspace or focused service`
- `list_tasks_for_workspace` at line 714 -> `Workspace or focused workspace service`
- `list_tasks_state_first_for_workspace` at line 734 -> `Workspace or focused workspace service`
- `get_task_for_workspace` at line 781 -> `Workspace or focused workspace service`
- `get_task_record_for_workspace` at line 797 -> `Workspace or focused workspace service`
- `require_task_for_workspace` at line 817 -> `Workspace or focused workspace service`
- `save_task_for_workspace` at line 832 -> `Workspace or focused workspace service`

### `litehive/state/store.py`
- `runtime_store_for_workspace` at line 824 -> `RuntimeStore`

### `litehive/tasks/_process_signals.py`
- `terminate_subagent_pid` at line 19 -> `new focused service for module concern`

### `litehive/tasks/_status_helpers.py`
- `_reset_pipeline_state` at line 30 -> `Workspace or focused service`
- `_persist_transition` at line 44 -> `Workspace or focused service`

### `litehive/tasks/activity.py`
- `load_task_activity` at line 143 -> `Workspace or focused workspace service`
- `save_task_activity` at line 150 -> `Workspace or focused workspace service`
- `append_task_activity` at line 157 -> `Workspace or focused workspace service`
- `latest_task_activity_entry` at line 164 -> `Workspace or focused workspace service`

### `litehive/tasks/activity_rendering.py`
- `append_activity_entry` at line 15 -> `Workspace or focused workspace service`
- `normalized_files_changed` at line 26 -> `new focused service for module concern`
- `is_retracted_activity_entry` at line 47 -> `new focused service for module concern`
- `is_retractable_pass_entry` at line 59 -> `new focused service for module concern`
- `retract_activity_entry` at line 75 -> `new focused service for module concern`
- `render_task_activity` at line 90 -> `Workspace or focused workspace service`

### `litehive/tasks/audit.py`
- `snapshot_task_audit_state` at line 56 -> `new focused service for module concern`
- `queue_position` at line 70 -> `new focused service for module concern`
- `insert_task_audit_entries` at line 139 -> `new focused service for module concern`
- `append_task_audit_entries` at line 189 -> `Workspace or focused workspace service`
- `load_task_audit_entries` at line 213 -> `Workspace or focused workspace service`

### `litehive/tasks/completed_task_recovery.py`
- `require_completed_task` at line 15 -> `new focused service for module concern`
- `recover_completed_task_for_workspace` at line 27 -> `Workspace or focused workspace service`

### `litehive/tasks/event_log.py`
- `task_event_log_path` at line 121 -> `Workspace or focused workspace service`
- `task_event_logging_suppressed` at line 126 -> `new focused service for module concern`
- `suppress_task_event_logging` at line 138 -> `new focused service for module concern`
- `task_event_type_for_audit_action` at line 154 -> `new focused service for module concern`
- `append_task_event` at line 166 -> `Workspace or focused workspace service`
- `read_task_events` at line 204 -> `Workspace or focused workspace service`
- `task_event_log_has_events` at line 237 -> `Workspace or focused workspace service`
- `rebuild_sqlite_from_task_event_log` at line 262 -> `Workspace or focused workspace service`
- `sqlite_task_tables_empty` at line 306 -> `Workspace or focused workspace service`

### `litehive/tasks/failed_runs.py`
- `blocking_failed_run_records` at line 12 -> `new focused service for module concern`
- `has_blocking_failed_run_history` at line 31 -> `new focused service for module concern`
- `mark_failed_run_operator_override` at line 42 -> `Workspace or focused workspace service`
- `failed_run_block_message` at line 108 -> `new focused service for module concern`

### `litehive/tasks/journal.py`
- `append_journal` at line 28 -> `Workspace or focused workspace service`
- `load_task_journal` at line 39 -> `Workspace or focused workspace service`
- `render_task_journal` at line 79 -> `Workspace or focused workspace service`

### `litehive/tasks/paths.py`
- `tasks_root` at line 30 -> `Workspace, WorktreeService, or GitRepository depending on module`
- `runner_lock_path` at line 49 -> `Workspace, WorktreeService, or GitRepository depending on module`
- `slugify` at line 63 -> `new focused service for module concern`
- `task_dir` at line 80 -> `Workspace, WorktreeService, or GitRepository depending on module`
- `task_recovery_dir` at line 91 -> `Workspace, WorktreeService, or GitRepository depending on module`
- `latest_path` at line 102 -> `new focused service for module concern`
- `resolve_artifact_path` at line 133 -> `resolver service for the module concern`
- `read_text_artifact` at line 147 -> `reader/repository object for the module concern`
- `latest_run_all_log_path_for_workspace` at line 161 -> `Workspace or focused workspace service`
- `latest_subagent_base_for_workspace` at line 182 -> `Workspace or focused workspace service`
- `status_entry_paths` at line 209 -> `new focused service for module concern`

### `litehive/tasks/queue_eligibility.py`
- `resumable_queue_stage` at line 60 -> `new focused service for module concern`
- `resumable_running_stage` at line 90 -> `new focused service for module concern`
- `task_has_resume_marker` at line 188 -> `new focused service for module concern`
- `is_task_eligible_for_execution` at line 218 -> `new focused service for module concern`
- `validate_task_dependencies_for_workspace` at line 295 -> `Workspace or focused workspace service`

### `litehive/tasks/queue_mutations.py`
- `enqueue_task_for_workspace` at line 32 -> `Workspace or focused workspace service`
- `enqueue_task_front_for_workspace` at line 39 -> `Workspace or focused workspace service`
- `_enqueue_task_for_workspace` at line 46 -> `Workspace or focused service`
- `move_queued_task_for_workspace` at line 86 -> `Workspace or focused workspace service`
- `prioritize_queued_tasks_for_workspace` at line 162 -> `Workspace or focused workspace service`
- `reset_task_for_recovery` at line 210 -> `new focused service for module concern`
- `enqueue_recovered_task` at line 247 -> `new focused service for module concern`
- `drop_task_from_workspace_state` at line 260 -> `new focused service for module concern`
- `prepare_completed_task_for_recovery` at line 281 -> `new focused service for module concern`
- `canonicalize_resumable_queue_task` at line 298 -> `new focused service for module concern`

### `litehive/tasks/queue_selection.py`
- `set_active_task` at line 97 -> `Workspace or focused workspace service`
- `peek_next_task` at line 122 -> `Workspace or focused workspace service`
- `peek_next_task_selection` at line 134 -> `Workspace or focused workspace service`
- `dequeue_next_task` at line 155 -> `Workspace or focused workspace service`
- `dequeue_next_task_selection` at line 167 -> `Workspace or focused workspace service`
- `_resolve_next_task_from_state` at line 314 -> `Workspace or focused service`
- `restore_missing_queued_tasks` at line 343 -> `new focused service for module concern`
- `clear_active_task` at line 446 -> `Workspace or focused workspace service`
- `restore_untouched_active_task` at line 457 -> `Workspace or focused workspace service`
- `active_task_markers_for_workspace` at line 532 -> `Workspace or focused workspace service`
- `validate_single_active_task_for_workspace` at line 570 -> `Workspace or focused workspace service`

### `litehive/tasks/recovery_engine.py`
- `resolve_recovery_engine` at line 9 -> `Workspace or focused workspace service`

### `litehive/tasks/recovery_evidence.py`
- `collect_recovery_evidence` at line 25 -> `Workspace or focused workspace service`
- `stage_report_context` at line 261 -> `new focused service for module concern`

### `litehive/tasks/recovery_reports.py`
- `record_recovery_report` at line 17 -> `Workspace or focused workspace service`

### `litehive/tasks/report_storage.py`
- `insert_recovery_report` at line 47 -> `Workspace or focused workspace service`
- `load_recovery_reports` at line 74 -> `Workspace or focused workspace service`
- `latest_recovery_report` at line 108 -> `Workspace or focused workspace service`
- `record_stage_report` at line 122 -> `Workspace or focused workspace service`
- `rewrite_latest_stage_report` at line 149 -> `Workspace or focused workspace service`
- `load_stage_reports_for_task_id` at line 192 -> `Workspace or focused workspace service`
- `load_workspace_stage_reports` at line 207 -> `Workspace or focused workspace service`
- `load_stage_reports` at line 218 -> `Workspace or focused workspace service`
- `latest_stage_report` at line 238 -> `Workspace or focused workspace service`
- `_load_stage_reports` at line 254 -> `Workspace or focused service`

### `litehive/tasks/runtime.py`
- `idle_stage_state` at line 32 -> `new focused service for module concern`
- `clear_task_run_activity` at line 98 -> `repository/store object for the module concern`
- `mark_task_run_started_for_workspace` at line 125 -> `Workspace or focused workspace service`
- `apply_task_run_started` at line 133 -> `runtime transition object for the module concern`
- `mark_task_run_finished_for_workspace` at line 145 -> `Workspace or focused workspace service`
- `apply_task_run_finished` at line 157 -> `runtime transition object for the module concern`
- `apply_flag_count_auto_defer` at line 164 -> `runtime transition object for the module concern`
- `finish_task_run_transition_for_workspace` at line 180 -> `Workspace or focused workspace service`
- `set_task_retry_state_for_workspace` at line 228 -> `Workspace or focused workspace service`
- `clear_task_outcome_for_workspace` at line 245 -> `Workspace or focused workspace service`
- `mark_task_outcome_for_workspace` at line 282 -> `Workspace or focused workspace service`
- `apply_task_outcome` at line 313 -> `runtime transition object for the module concern`
- `mark_stage_started_for_workspace` at line 366 -> `Workspace or focused workspace service`
- `apply_stage_started` at line 374 -> `runtime transition object for the module concern`
- `mark_stage_finished_for_workspace` at line 383 -> `Workspace or focused workspace service`
- `apply_stage_finished` at line 392 -> `runtime transition object for the module concern`
- `mark_subagent_started_for_workspace` at line 405 -> `Workspace or focused workspace service`
- `apply_subagent_started` at line 413 -> `runtime transition object for the module concern`
- `mark_subagent_pid_for_workspace` at line 422 -> `Workspace or focused workspace service`
- `apply_subagent_pid` at line 431 -> `runtime transition object for the module concern`
- `mark_subagent_progress_for_workspace` at line 449 -> `Workspace or focused workspace service`
- `apply_subagent_progress` at line 464 -> `runtime transition object for the module concern`
- `mark_subagent_finished_for_workspace` at line 490 -> `Workspace or focused workspace service`
- `apply_subagent_finished` at line 508 -> `runtime transition object for the module concern`
- `mark_engine_switch_for_workspace` at line 517 -> `Workspace or focused workspace service`
- `apply_engine_switch` at line 538 -> `runtime transition object for the module concern`
- `summarize_transcript` at line 559 -> `new focused service for module concern`
- `duration_seconds` at line 580 -> `new focused service for module concern`

### `litehive/tasks/status_close.py`
- `_abandon_task_transition` at line 50 -> `Workspace or focused service`
- `_close_task_transition` at line 99 -> `Workspace or focused service`
- `_park_task_transition` at line 185 -> `Workspace or focused service`
- `abandon_task_for_workspace` at line 224 -> `Workspace or focused workspace service`
- `close_task_for_workspace` at line 243 -> `Workspace or focused workspace service`
- `park_task_for_workspace` at line 266 -> `Workspace or focused workspace service`

### `litehive/tasks/status_resume.py`
- `_requeue_task_transition` at line 53 -> `Workspace or focused service`
- `_resume_task_transition` at line 160 -> `Workspace or focused service`
- `requeue_task_for_workspace` at line 226 -> `Workspace or focused workspace service`
- `resume_task_for_workspace` at line 247 -> `Workspace or focused workspace service`

### `litehive/tasks/status_update.py`
- `_update_task_transition` at line 36 -> `Workspace or focused service`
- `update_task_for_workspace` at line 201 -> `Workspace or focused workspace service`

### `litehive/tasks/stop.py`
- `_active_task_id_for_stop` at line 40 -> `Workspace or focused service`
- `_stop_active_task_without_runner_guard` at line 57 -> `Workspace or focused service`
- `stop_current_task` at line 135 -> `Workspace or focused workspace service`

### `litehive/tasks/switch_engine.py`
- `_switch_prior_work_paths` at line 50 -> `Workspace or focused service`
- `switch_task_engine_for_workspace` at line 100 -> `Workspace or focused workspace service`

### `litehive/worktree/cleanup.py`
- `cleanup_terminal_task_worktree_for_workspace` at line 33 -> `Workspace or focused workspace service`
- `collect_managed_worktrees_for_workspace` at line 55 -> `Workspace or focused workspace service`
- `remove_cleanable_worktrees_for_workspace` at line 108 -> `Workspace or focused workspace service`

### `litehive/worktree/execution_root.py`
- `resolve_task_execution_root_for_workspace` at line 35 -> `Workspace or focused workspace service`

### `litehive/worktree/inspection.py`
- `inspect_dirty_worktree_gate` at line 37 -> `Workspace or focused workspace service`
- `dirty_entry_paths` at line 116 -> `new focused service for module concern`
- `worktree_uncommitted_changes` at line 140 -> `new focused service for module concern`
- `worktree_committed_changes_for_workspace` at line 156 -> `Workspace or focused workspace service`
- `_allowed_commit_paths` at line 173 -> `Workspace or focused service`
- `_task_can_resume_with_owned_dirty_paths` at line 242 -> `Workspace or focused service`

### `litehive/worktree/paths.py`
- `task_worktree_path_for_workspace` at line 30 -> `Workspace or focused workspace service`
- `task_worktree_branch` at line 43 -> `new focused service for module concern`
- `is_managed_worktree_path_for_workspace` at line 54 -> `Workspace or focused workspace service`
- `resolve_recorded_worktree_path_for_workspace` at line 75 -> `Workspace or focused workspace service`
- `serialize_worktree_path` at line 97 -> `new focused service for module concern`
- `ensure_worktree_venv_link_for_workspace` at line 109 -> `Workspace or focused workspace service`

### `litehive/worktree/rescue.py`
- `collect_rescue_candidates_for_workspace` at line 54 -> `Workspace or focused workspace service`
- `require_clean_main_checkout_for_workspace` at line 88 -> `Workspace or focused workspace service`
- `apply_rescue_candidate_for_workspace` at line 106 -> `Workspace or focused workspace service`
- `_worktree_commits_ahead_of_main_for_workspace` at line 311 -> `Workspace or focused service`
- `_worktree_patch_already_on_main_for_workspace` at line 328 -> `Workspace or focused service`
- `_resolve_metadata_conflicts_for_workspace` at line 357 -> `Workspace or focused service`
- `_drop_task_metadata_changes_for_workspace` at line 379 -> `Workspace or focused service`
- `_finalize_rescue_for_workspace` at line 393 -> `Workspace or focused service`
- `_ensure_unmerged_worktree_state_for_workspace` at line 432 -> `Workspace or focused service`
- `_stash_litehive_changes_for_workspace` at line 450 -> `Workspace or focused service`
- `_restore_litehive_changes_for_workspace` at line 476 -> `Workspace or focused service`
- `_worktree_has_non_metadata_changes_for_workspace` at line 495 -> `Workspace or focused service`

### `litehive/worktree/service.py`
- `status_porcelain_untracked` at line 70 -> `WorktreeService`
