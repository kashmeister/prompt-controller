#!/usr/bin/env python3
"""Replay recorded terminal streams through the wrappers' prompt detection.

The wrappers detect approval prompts by watching what the CLI paints, so they are
exposed to TUI changes in Codex and Claude Code. This replays real captures --
approval menus the wrappers must fire on, and ordinary working output they must
stay quiet on -- through the same scan_output() the live wrappers use.

    python3 tests/selftest.py

To add a case after a CLI update, capture a session's raw PTY output to a file and
drop it in tests/fixtures/ with the matching expectation below.
"""

import importlib.util
import os
import sys
from collections import deque
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# (wrapper, fixture, should a prompt be detected?)
CASES = [
    ("codex", "codex-trust-prompt.raw", True),
    ("codex", "codex-command-approval.raw", True),
    ("codex", "codex-no-prompt.raw", False),
    ("claude", "claude-trust-prompt.raw", True),
    ("claude", "claude-write-approval.raw", True),
    ("claude", "claude-bash-approval.raw", True),
    ("claude", "claude-no-prompt.raw", False),
]

# The wrappers read at most this much per loop iteration, and idle-tick on this
# cadence; both matter to detection, so the replay matches them.
READ_SIZE = 4096
TICK_SECONDS = 0.1
# Seconds of quiet to simulate after the stream ends, so timers that promote a
# pending prompt or re-scan a static screen get a chance to run.
TRAILING_QUIET_SECONDS = 5.0


class FakeClock:
    """Stands in for the time module so a replay runs faster than real time."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def strftime(self, fmt):
        return "%08.3f" % self.t


def load_wrapper(which):
    path = REPO / f"{which}_cli_wrapper.py"
    spec = importlib.util.spec_from_file_location(f"{which}_cli_wrapper", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_case(which, fixture, expect_prompt):
    module = load_wrapper(which)
    clock = FakeClock()
    module.time = clock
    # Nothing may touch the real desktop or the real state file during a replay.
    module.activate_terminal_window = lambda *a, **k: None
    module.activate_app = lambda *a, **k: None
    module.unminimize_windows = lambda *a, **k: None
    module.notify = lambda *a, **k: None
    module.current_frontmost_app = lambda: "SomeOtherApp"

    entered = []

    class Recorder(module.PromptState):
        def enter(self, excerpt):
            was_active = self.active
            super().enter(excerpt)
            if not was_active:
                entered.append((clock.t, excerpt))

        def write_state(self, active, excerpt=""):
            pass

        def clear_state(self):
            pass

    state = Recorder(
        state_file=Path(os.devnull),
        notify_enabled=False,
        activate_enabled=True,
        restore_enabled=False,
        debug_enabled=False,
        terminal_app="Terminal",
    )
    state.terminal_tty = "/dev/ttys999"

    rolling_text = deque(maxlen=module.ROLLING_TEXT_LIMIT)
    data = (FIXTURES / fixture).read_bytes()

    for offset in range(0, len(data), READ_SIZE):
        module.scan_output(state, data[offset : offset + READ_SIZE], rolling_text)
        clock.t += TICK_SECONDS
        state.tick()

    quiet_until = clock.t + TRAILING_QUIET_SECONDS
    while clock.t < quiet_until:
        clock.t += TICK_SECONDS
        state.tick()

    ok = bool(entered) == expect_prompt
    detail = ""
    if entered:
        excerpt = entered[0][1][-70:].replace("\n", " ").replace("\r", " ")
        detail = f"entered at {entered[0][0]:.2f}s: …{excerpt}"
    elif expect_prompt:
        detail = "no prompt detected"
    return ok, detail


def main():
    failures = 0
    width = max(len(f) for _, f, _ in CASES)
    for which, fixture, expect_prompt in CASES:
        ok, detail = run_case(which, fixture, expect_prompt)
        if not ok:
            failures += 1
        want = "prompt" if expect_prompt else "quiet "
        print(f"{'PASS' if ok else 'FAIL'}  {which:6} {fixture:<{width}}  want {want}  {detail}")

    print()
    if failures:
        print(f"{failures} of {len(CASES)} cases failed")
        return 1
    print(f"all {len(CASES)} cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
