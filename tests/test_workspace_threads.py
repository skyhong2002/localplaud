"""New threads start without the caller's workspace; every spawn must account for it.

A thread that is not bound reads and writes the original workspace. That is
right for the daemon and wrong for work a member's request starts, so each
spawn site must bind the caller's scope, adopt a recording's workspace, or be
listed here as deliberately workspace-free.
"""

import ast
from pathlib import Path

SOURCE = Path(__file__).parents[1] / "src" / "localplaud"

# Targets that pick their own workspace or never touch workspace data.
SAFE_TARGETS = {
    # @scoped_to_file: run in the recording's own workspace.
    "process_file",
    "process_derived_artifacts",
    "process_mind_map_only",
    "reindex_file",
    # Workspace-free: remote job ledger, audio peaks, provider health.
    "resume_pending_jobs",
    "waveform_peaks",
    "_llm_health",
    # Daemon slot: rotates through every workspace explicitly.
    "self._parallel_cycle",
}
BOUND = ("run_in_current_workspace(", "copy_context().run")


def _spawns():
    for path in SOURCE.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ast.unparse(node.func)
            if name.endswith("Thread"):
                target = next((k.value for k in node.keywords if k.arg == "target"), None)
            elif name.endswith(("pool.submit", "executor.submit")) and node.args:
                target = node.args[0]
            else:
                continue
            if target is not None:
                yield path.relative_to(SOURCE), node.lineno, ast.unparse(target)


def test_every_thread_spawn_accounts_for_the_workspace():
    unbound = [
        f"{path}:{line} target={target}"
        for path, line, target in _spawns()
        if not any(marker in target for marker in BOUND) and target not in SAFE_TARGETS
    ]
    assert unbound == [], "bind with run_in_current_workspace() or justify in SAFE_TARGETS"


def test_safe_recording_targets_are_scoped_to_their_recording():
    from localplaud.worker import pipeline, reindex

    for function in (
        pipeline.process_file,
        pipeline.process_derived_artifacts,
        pipeline.process_mind_map_only,
        reindex.reindex_file,
    ):
        assert getattr(function, "scoped_to_recording", False), function
