#!/usr/bin/env python3

import argparse
import fcntl
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import termios
import time
import tty
from collections import deque
from pathlib import Path
from typing import Optional


DEFAULT_STATE_FILE = Path("/tmp/codex-cli-prompt-state.json")
# Debug output goes to this file, never to stderr: the wrapper shares the
# terminal with Codex's full-screen TUI, so printing debug lines to the screen
# would corrupt its rendering (scrolling / blank bottom line) on every repaint.
DEFAULT_DEBUG_LOG = Path("/tmp/codex-cli-wrapper.log")
DEFAULT_AUTO_FOCUS = True
DEFAULT_RESTORE_FOCUS = False
ROLLING_TEXT_LIMIT = 12000
PROMPT_SCAN_TAIL_CHARS = 2200
PROMPT_ENTER_STABILITY_SECONDS = 0.2
PROMPT_EXIT_GRACE_SECONDS = 0.75
PROMPT_RESOLUTION_TIMEOUT_SECONDS = 4.0
PROMPT_REACTIVATE_INTERVAL_SECONDS = 2.0
PROMPT_TITLE_CLEAR_STABILITY_SECONDS = 1.0
# Sticky-reminder cadence: how often to re-post the macOS notification while a
# prompt stays unanswered and the terminal is not frontmost. Re-focus happens on
# PROMPT_REACTIVATE_INTERVAL_SECONDS; the notification repeats more slowly so it
# nudges without spamming.
NOTIFY_REPEAT_INTERVAL_SECONDS = 20.0
# Set True to also ring the terminal bell on each reminder for an audible nudge.
REMIND_WITH_BELL = False
PROMPT_CLEAR_ACTION_BYTES = {
    b"\r",
    b"\n",
    b"\x1b",
    b"1",
    b"2",
    b"3",
    b"4",
}
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OSC_ESCAPE_RE = re.compile(r"\x1b\].*?(?:\x07|\x1b\\)")
CSI_PRIVATE_RE = re.compile(r"\x1b[PX^_].*?\x1b\\", re.DOTALL)
OSC_TITLE_RE = re.compile(r"\x1b\](?:0|1|2);(.*?)(?:\x07|\x1b\\)")
ACTION_REQUIRED_TITLE_RE = re.compile(r"\[\s*[!.]\s*\](?:\s+Action Required\s+\|)?")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Wrap Codex CLI, detect approval prompts, and emit explicit prompt state."
    )
    parser.add_argument(
        "--state-file",
        default=os.environ.get("CODEX_PROMPT_STATE_FILE", str(DEFAULT_STATE_FILE)),
        help="Path to the prompt state JSON file.",
    )
    parser.add_argument(
        "--no-notify",
        action="store_true",
        help="Disable macOS notifications when a prompt appears.",
    )
    parser.add_argument(
        "--no-activate",
        action="store_true",
        help="Do not activate the terminal app when a prompt appears.",
    )
    parser.add_argument(
        "--no-restore",
        action="store_true",
        help="Do not restore the previous frontmost app after the prompt clears.",
    )
    parser.add_argument(
        "--terminal-app",
        default=os.environ.get("CODEX_PROMPT_TERMINAL_APP"),
        help="Override the macOS app to activate when a prompt appears, e.g. Terminal, iTerm2, or Ghostty.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Log title and prompt state transitions to the debug file (never the terminal).",
    )
    parser.add_argument(
        "--debug-file",
        default=os.environ.get("CODEX_PROMPT_DEBUG_FILE", str(DEFAULT_DEBUG_LOG)),
        help="Path to the debug log file written when --debug is set.",
    )
    parser.add_argument(
        "codex_args",
        nargs=argparse.REMAINDER,
        help="Arguments passed through to codex. Prefix with -- if needed.",
    )
    return parser.parse_args()


def clean_args(raw_args):
    if raw_args and raw_args[0] == "--":
      return raw_args[1:]
    return raw_args


def codex_command(extra_args):
    codex_bin = shutil.which("codex")
    if not codex_bin:
        raise SystemExit("codex binary not found on PATH")
    return [codex_bin, *extra_args]


def current_terminal_app_name() -> Optional[str]:
    term_program = os.environ.get("TERM_PROGRAM", "")
    if term_program == "Apple_Terminal":
        return "Terminal"
    if term_program == "iTerm.app":
        return "iTerm2"
    if term_program == "WezTerm":
        return "WezTerm"
    if term_program == "Hyper":
        return "Hyper"
    if term_program == "vscode":
        return "Visual Studio Code"

    term = os.environ.get("TERM", "")
    colorterm = os.environ.get("COLORTERM", "")

    if "ghostty" in term.lower():
        return "Ghostty"
    if "warp" in term.lower() or "warp" in colorterm.lower():
        return "Warp"
    if "wezterm" in term.lower():
        return "WezTerm"
    if "xterm-kitty" in term.lower():
        return "kitty"
    if "alacritty" in term.lower():
        return "Alacritty"

    return None


def run_osascript(script: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None

    output = (result.stdout or "").strip()
    return output or None


def escape_applescript(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def current_frontmost_app() -> Optional[str]:
    script = 'tell application "System Events" to get name of first application process whose frontmost is true'
    return run_osascript(script)


def activate_app(app_name: str) -> None:
    if not app_name:
        return
    safe_name = escape_applescript(app_name)
    run_osascript(f'tell application "{safe_name}" to activate')
    run_osascript(f'tell application "System Events" to tell process "{safe_name}" to set frontmost to true')
    try:
        subprocess.run(["open", "-a", app_name], check=False, capture_output=True, text=True)
    except OSError:
        pass


def activate_terminal_window(app_name: Optional[str], tty_name: Optional[str]) -> None:
    if not app_name:
        return

    if not tty_name:
        activate_app(app_name)
        return

    safe_app = escape_applescript(app_name)
    safe_tty = escape_applescript(tty_name)

    if app_name == "Terminal":
        script = f'''
tell application "Terminal"
  activate
  repeat with w in windows
    repeat with t in tabs of w
      try
        if tty of t is "{safe_tty}" then
          set selected of t to true
          set index of w to 1
          return "ok"
        end if
      end try
    end repeat
  end repeat
end tell
'''
        result = run_osascript(script)
        if result == "ok":
            return

    if app_name == "iTerm2":
        script = f'''
tell application "iTerm2"
  activate
  repeat with w in windows
    repeat with t in tabs of w
      repeat with s in sessions of t
        try
          if tty of s is "{safe_tty}" then
            tell w to set current tab to t
            tell t to set current session to s
            return "ok"
          end if
        end try
      end repeat
    end repeat
  end repeat
end tell
'''
        result = run_osascript(script)
        if result == "ok":
            return

    activate_app(app_name)


def notify(title: str, body: str) -> None:
    safe_title = title.replace('"', '\\"')
    safe_body = body.replace('"', '\\"')
    run_osascript(f'display notification "{safe_body}" with title "{safe_title}"')


def terminal_size(stdin_fd: int):
    packed = fcntl.ioctl(stdin_fd, termios.TIOCGWINSZ, struct.pack("HHHH", 0, 0, 0, 0))
    rows, cols, xpixels, ypixels = struct.unpack("HHHH", packed)
    return rows, cols, xpixels, ypixels


def set_pty_size(fd: int, size) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", *size))


def sanitize_terminal_text(chunk: bytes) -> str:
    text = chunk.decode("utf-8", errors="ignore")
    text = OSC_ESCAPE_RE.sub("", text)
    text = CSI_PRIVATE_RE.sub("", text)
    text = ANSI_ESCAPE_RE.sub("", text)
    return text


def extract_terminal_titles(chunk: bytes):
    text = chunk.decode("utf-8", errors="ignore")
    return [match.group(1) for match in OSC_TITLE_RE.finditer(text) if match.group(1)]


def title_indicates_prompt(title: str) -> bool:
    return ACTION_REQUIRED_TITLE_RE.search(title) is not None


def prompt_visible(text: str) -> bool:
    if not text:
        return False

    has_choice1 = "Yes (1)" in text or re.search(r"(^|\n)\s*1\.\s+Yes\b", text) is not None
    has_choice2 = "Yes, (2)" in text or re.search(r"(^|\n)\s*2\.\s+Yes\b", text) is not None
    has_choice3 = (
        "No (3)" in text
        or "No, (3)" in text
        or re.search(r"(^|\n)\s*3\.\s+No\b", text) is not None
    )
    has_choice4 = "fill-in (4)" in text or re.search(r"(^|\n)\s*4\.", text) is not None
    has_codex_negative = (
        "don't ask again" in text
        or "tell Codex what to do differently" in text
        or "No, and tell Codex what to do differently" in text
        or "Yes, and don't ask again" in text
    )

    return (
        (has_choice1 and has_choice3)
        or (has_choice1 and has_choice2 and has_choice3)
        or (has_choice1 and has_choice3 and has_choice4)
        or has_codex_negative
    )


class PromptState:
    def __init__(
        self,
        state_file: Path,
        notify_enabled: bool,
        activate_enabled: bool,
        restore_enabled: bool,
        debug_enabled: bool,
        terminal_app: Optional[str] = None,
        debug_log: Path = DEFAULT_DEBUG_LOG,
    ):
        self.state_file = state_file
        self.notify_enabled = notify_enabled
        self.activate_enabled = activate_enabled
        self.restore_enabled = restore_enabled
        self.debug_enabled = debug_enabled
        self.debug_log = debug_log
        self.active = False
        self.saved_frontmost_app = None
        self.terminal_app = terminal_app or current_terminal_app_name() or current_frontmost_app()
        self.terminal_tty = None
        self.last_excerpt = None
        self.last_prompt_seen_at = 0.0
        self.pending_resolution_until = 0.0
        self.last_activate_at = 0.0
        self.last_notify_at = 0.0
        self.pending_prompt_since = 0.0
        self.last_terminal_title = None
        self.pending_title_clear_since = 0.0
        self.seen_terminal_title = False

    def debug(self, message: str) -> None:
        if not self.debug_enabled:
            return
        # Append to a log file rather than stderr so debug output never lands on
        # the terminal Codex is drawing to. Tail it with: tail -f the log path.
        line = f"{time.strftime('%H:%M:%S')} [codex-wrapper] {message}\n"
        try:
            with open(self.debug_log, "a") as handle:
                handle.write(line)
        except OSError:
            pass

    def write_state(self, active: bool, excerpt: str = "") -> None:
        payload = {
            "active": active,
            "terminalApp": self.terminal_app,
            "savedFrontmostApp": self.saved_frontmost_app,
            "excerpt": excerpt,
            "pid": os.getpid(),
        }
        self.state_file.write_text(json.dumps(payload, indent=2))

    def clear_state(self) -> None:
        try:
            self.state_file.unlink()
        except FileNotFoundError:
            pass

    def enter(self, excerpt: str) -> None:
        now = time.monotonic()
        self.last_prompt_seen_at = now
        self.pending_resolution_until = 0.0

        if self.active:
            self.last_excerpt = excerpt
            self.write_state(True, excerpt)
            self.maybe_reactivate_terminal(now)
            self.debug("PROMPT_STILL_ACTIVE")
            return

        self.active = True
        self.last_excerpt = excerpt
        frontmost = current_frontmost_app()
        self.saved_frontmost_app = frontmost if frontmost != self.terminal_app else None
        self.write_state(True, excerpt)
        self.debug(f"PROMPT_ENTER terminalApp={self.terminal_app!r} savedFrontmostApp={self.saved_frontmost_app!r}")

        if self.notify_enabled:
            notify("Codex CLI prompt", "Approval needed in terminal")
            self.last_notify_at = now

        self.force_activate_terminal(now)

    def exit(self) -> None:
        if not self.active:
            self.clear_state()
            return

        self.debug("PROMPT_EXIT")
        self.active = False
        self.write_state(False, self.last_excerpt or "")
        self.clear_state()

        if self.restore_enabled and self.saved_frontmost_app and self.saved_frontmost_app != self.terminal_app:
            activate_app(self.saved_frontmost_app)

        self.saved_frontmost_app = None
        self.last_excerpt = None
        self.last_prompt_seen_at = 0.0
        self.pending_resolution_until = 0.0
        self.last_activate_at = 0.0
        self.last_notify_at = 0.0
        self.pending_prompt_since = 0.0
        self.last_terminal_title = None
        self.pending_title_clear_since = 0.0
        self.seen_terminal_title = False

    def mark_prompt_seen(self, excerpt: str) -> None:
        self.last_excerpt = excerpt
        self.last_prompt_seen_at = time.monotonic()
        if self.active:
            self.write_state(True, excerpt)

    def maybe_enter_after_output(self, prompt_is_visible: bool, excerpt: str, immediate: bool = False) -> None:
        now = time.monotonic()

        if prompt_is_visible:
            if immediate:
                self.pending_prompt_since = 0.0
                self.enter(excerpt)
                return

            if self.active:
                self.enter(excerpt)
                return

            if self.pending_prompt_since == 0.0:
                self.pending_prompt_since = now
                return

            if now - self.pending_prompt_since >= PROMPT_ENTER_STABILITY_SECONDS:
                self.enter(excerpt)
            return

        self.pending_prompt_since = 0.0

    def update_terminal_title(self, title: str) -> None:
        self.last_terminal_title = title
        self.seen_terminal_title = True
        self.debug(f"TITLE {title}")

    def mark_title_prompt_state(self, title_prompt_visible: bool) -> None:
        now = time.monotonic()
        if title_prompt_visible:
            self.pending_title_clear_since = 0.0
        elif self.active:
            if self.pending_title_clear_since == 0.0:
                self.pending_title_clear_since = now

    def maybe_exit_after_title_clear(self, text_prompt_visible: bool) -> None:
        if not self.active:
            return

        if self.pending_title_clear_since == 0.0:
            return

        if text_prompt_visible:
            self.pending_title_clear_since = 0.0
            return

        if time.monotonic() - self.pending_title_clear_since >= PROMPT_TITLE_CLEAR_STABILITY_SECONDS:
            self.exit()

    def mark_resolution_attempt(self) -> None:
        if not self.active:
            return
        self.pending_resolution_until = time.monotonic() + PROMPT_RESOLUTION_TIMEOUT_SECONDS

    def maybe_exit_after_output(self, prompt_is_visible: bool) -> None:
        if not self.active:
            return

        now = time.monotonic()

        if prompt_is_visible:
            self.pending_resolution_until = 0.0

    def force_activate_terminal(self, now: Optional[float] = None) -> None:
        if not self.activate_enabled or not self.terminal_app:
            return
        activate_terminal_window(self.terminal_app, self.terminal_tty)
        self.last_activate_at = now if now is not None else time.monotonic()

    def maybe_reactivate_terminal(self, now: Optional[float] = None) -> None:
        if not self.active or not self.activate_enabled or not self.terminal_app:
            return

        current_time = now if now is not None else time.monotonic()
        if current_time - self.last_activate_at < PROMPT_REACTIVATE_INTERVAL_SECONDS:
            return

        frontmost = current_frontmost_app()
        if frontmost != self.terminal_app:
            self.force_activate_terminal(current_time)
            return

        if self.pending_resolution_until > 0.0:
            if now < self.pending_resolution_until:
                if now - self.last_prompt_seen_at >= PROMPT_EXIT_GRACE_SECONDS:
                    self.exit()
                return

            self.pending_resolution_until = 0.0

    def tick(self) -> None:
        # Called every main-loop iteration, even when no output arrives, so a
        # static permission prompt still gets re-focused and re-notified.
        if not self.active:
            return
        self.remind(time.monotonic())

    def remind(self, now: float) -> None:
        if not self.activate_enabled or not self.terminal_app:
            return
        if now - self.last_activate_at < PROMPT_REACTIVATE_INTERVAL_SECONDS:
            return

        if current_frontmost_app() == self.terminal_app:
            # The user is already looking at the terminal. Don't steal focus or
            # beep; just arm the notification timer so a later defocus re-nudges.
            self.last_notify_at = now
            return

        self.force_activate_terminal(now)

        if self.notify_enabled and now - self.last_notify_at >= NOTIFY_REPEAT_INTERVAL_SECONDS:
            notify("Codex needs you", "Approval waiting in the terminal")
            self.last_notify_at = now

        if REMIND_WITH_BELL:
            try:
                sys.stdout.write("\a")
                sys.stdout.flush()
            except (OSError, ValueError):
                pass


def main() -> int:
    args = parse_args()
    extra_args = clean_args(args.codex_args)
    command = codex_command(extra_args)
    state_file = Path(args.state_file).expanduser()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    debug_log = Path(args.debug_file).expanduser()

    state = PromptState(
        state_file=state_file,
        notify_enabled=not args.no_notify,
        activate_enabled=(not args.no_activate) and DEFAULT_AUTO_FOCUS,
        restore_enabled=(not args.no_restore) and DEFAULT_RESTORE_FOCUS,
        debug_enabled=args.debug,
        terminal_app=args.terminal_app,
        debug_log=debug_log,
    )

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    try:
        state.terminal_tty = os.ttyname(stdin_fd)
    except OSError:
        state.terminal_tty = None
    state.debug(f"TERMINAL_APP {state.terminal_app!r} TTY={state.terminal_tty!r}")

    master_fd, slave_fd = pty.openpty()
    set_pty_size(slave_fd, terminal_size(stdin_fd))

    child = subprocess.Popen(
        command,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        preexec_fn=os.setsid,
        close_fds=True,
    )
    os.close(slave_fd)

    original_tty = termios.tcgetattr(stdin_fd)
    rolling_text = deque(maxlen=ROLLING_TEXT_LIMIT)

    def on_sigwinch(signum, frame):
        del signum, frame
        try:
            set_pty_size(master_fd, terminal_size(stdin_fd))
        except OSError:
            pass

    signal.signal(signal.SIGWINCH, on_sigwinch)

    try:
        tty.setraw(stdin_fd)

        while True:
            if child.poll() is not None:
                break

            readable, _, _ = select.select([stdin_fd, master_fd], [], [], 0.1)

            if master_fd in readable:
                try:
                    data = os.read(master_fd, 4096)
                except OSError:
                    break

                if not data:
                    break

                os.write(stdout_fd, data)

                titles = extract_terminal_titles(data)
                title_prompt_visible = False
                if titles:
                    latest_title = titles[-1]
                    state.update_terminal_title(latest_title)
                    title_prompt_visible = title_indicates_prompt(latest_title)
                    state.mark_title_prompt_state(title_prompt_visible)

                cleaned = sanitize_terminal_text(data)
                if cleaned:
                    rolling_text.extend(cleaned)
                    joined = "".join(rolling_text)
                    scan_text = joined[-PROMPT_SCAN_TAIL_CHARS:]
                    text_visible = prompt_visible(scan_text)
                    visible = title_prompt_visible if state.seen_terminal_title else (title_prompt_visible or text_visible)
                    excerpt = scan_text[-500:]
                    state.maybe_enter_after_output(visible, excerpt, title_prompt_visible)
                    if visible and state.active:
                        state.mark_prompt_seen(excerpt)
                    state.maybe_exit_after_title_clear(text_visible if not state.seen_terminal_title else False)
                    if not visible:
                        state.maybe_exit_after_output(False)
                elif title_prompt_visible:
                    state.maybe_enter_after_output(True, state.last_excerpt or "", True)
                else:
                    state.maybe_exit_after_title_clear(False)

            if stdin_fd in readable:
                try:
                    user_input = os.read(stdin_fd, 1024)
                except OSError:
                    user_input = b""

                if user_input:
                    os.write(master_fd, user_input)
                    if state.active and any(token in user_input for token in PROMPT_CLEAR_ACTION_BYTES):
                        state.mark_resolution_attempt()

            state.tick()
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, original_tty)
        state.exit()
        os.close(master_fd)

    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
