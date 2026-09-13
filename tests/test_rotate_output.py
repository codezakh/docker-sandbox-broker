"""Behavior of the foreground rotating-output launcher."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rotate_output.py"


def launch(log, code, *options):
    return [
        sys.executable,
        str(SCRIPT),
        "--log-file",
        str(log),
        *options,
        "--",
        sys.executable,
        "-u",
        "-c",
        code,
    ]


def describe_rotating_output():
    def it_captures_both_streams_and_preserves_failure_status(tmp_path):
        """Captures stdout and stderr privately and returns the command's exit status."""
        log = tmp_path / "logs" / "broker.log"
        result = subprocess.run(
            launch(log, "import sys; print('out'); print('err', file=sys.stderr); sys.exit(7)"),
            capture_output=True,
        )
        assert result.returncode == 7
        assert result.stdout == result.stderr == b""
        assert "out" in log.read_text() and "err" in log.read_text()
        assert log.stat().st_mode & 0o077 == 0

    def it_bounds_retained_output(tmp_path):
        """Rotates a noisy command and retains only the configured backup count."""
        log = tmp_path / "broker.log"
        result = subprocess.run(
            launch(
                log, "print('x' * 20000); print('LAST')", "--max-bytes", "1024", "--backups", "2"
            ),
            capture_output=True,
        )
        assert result.returncode == 0
        files = list(tmp_path.glob("broker.log*"))
        assert len(files) == 3
        assert all(p.stat().st_size <= 1024 for p in files)
        assert "LAST" in log.read_text()

    def it_forwards_termination(tmp_path):
        """Forwards termination to the child and records its final output."""
        log = tmp_path / "broker.log"
        code = (
            "import signal,time,sys; "
            "signal.signal(signal.SIGTERM, lambda *_: (print('stopped'), sys.exit(0))); "
            "print('ready'); time.sleep(60)"
        )
        process = subprocess.Popen(launch(log, code), start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            while not log.exists() or "ready" not in log.read_text():
                assert time.monotonic() < deadline
                time.sleep(0.02)
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=5) == 0
            assert "stopped" in log.read_text()
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
