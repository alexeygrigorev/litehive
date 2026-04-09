from litehive.cli.parsers.abandon import register_abandon_parser
from litehive.cli.parsers.add import register_add_parser
from litehive.cli.parsers.archive import register_archive_parser
from litehive.cli.parsers.cleanup import register_cleanup_parser
from litehive.cli.parsers.close import register_close_parser
from litehive.cli.parsers.configure import register_configure_parser
from litehive.cli.parsers.daemon import register_daemon_parser
from litehive.cli.parsers.debug import register_debug_parser
from litehive.cli.parsers.dirty_worktree_gate import register_dirty_worktree_gate_parser
from litehive.cli.parsers.engine import register_engine_parser
from litehive.cli.parsers.github_import import (
    register_import_issue_parser,
    register_import_issues_parser,
)
from litehive.cli.parsers.health import register_health_parser
from litehive.cli.parsers.intake import register_intake_parser
from litehive.cli.parsers.issue import register_issue_parser
from litehive.cli.parsers.list import register_list_parser
from litehive.cli.parsers.logs import register_logs_parser
from litehive.cli.parsers.move import register_move_parser
from litehive.cli.parsers.prioritize import register_prioritize_parser
from litehive.cli.parsers.promote import register_promote_parser
from litehive.cli.parsers.queue import register_queue_parser
from litehive.cli.parsers.recover import register_recover_parser
from litehive.cli.parsers.repair import register_repair_parser
from litehive.cli.parsers.report import register_report_parser
from litehive.cli.parsers.requeue import register_requeue_parser
from litehive.cli.parsers.resume import register_resume_parser
from litehive.cli.parsers.rollback import register_rollback_parser
from litehive.cli.parsers.run import register_run_parser
from litehive.cli.parsers.show import register_show_parser
from litehive.cli.parsers.status import register_status_parser
from litehive.cli.parsers.stop import register_stop_parser
from litehive.cli.parsers.switch import register_switch_parser
from litehive.cli.parsers.tasks import register_tasks_parser
from litehive.cli.parsers.update import register_update_parser
from litehive.cli.parsers.web import register_web_parser
from litehive.cli.parsers.worktree import register_worktree_parser

COMMAND_PARSER_BUILDERS = (
    register_configure_parser,
    register_status_parser,
    register_health_parser,
    register_engine_parser,
    register_queue_parser,
    register_repair_parser,
    register_tasks_parser,
    register_web_parser,
    register_daemon_parser,
    register_add_parser,
    register_issue_parser,
    register_intake_parser,
    register_run_parser,
    register_dirty_worktree_gate_parser,
    register_rollback_parser,
    register_recover_parser,
    register_move_parser,
    register_prioritize_parser,
    register_promote_parser,
    register_requeue_parser,
    register_resume_parser,
    register_abandon_parser,
    register_stop_parser,
    register_switch_parser,
    register_close_parser,
    register_update_parser,
    register_report_parser,
    register_import_issue_parser,
    register_import_issues_parser,
    register_debug_parser,
    register_logs_parser,
    register_worktree_parser,
    register_list_parser,
    register_show_parser,
    register_archive_parser,
    register_cleanup_parser,
)
