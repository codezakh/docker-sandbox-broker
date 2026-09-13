"""Run a command with stdout/stderr captured in bounded, rotating files."""

import argparse
import logging
import os
import signal
import subprocess
from logging.handlers import RotatingFileHandler
from pathlib import Path


class StrictRotatingFileHandler(RotatingFileHandler):
    def handleError(self, record):
        raise RuntimeError("log write failed")


def positive_int(value: str) -> int:
    result = int(value)
    if result < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-file", type=Path, required=True)
    parser.add_argument("--max-bytes", type=positive_int, default=10 * 1024 * 1024)
    parser.add_argument("--backups", type=positive_int, default=5)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        parser.error("a command is required after --")

    os.umask(0o077)
    args.log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = StrictRotatingFileHandler(
        args.log_file, maxBytes=args.max_bytes, backupCount=args.backups, encoding="utf-8"
    )
    handler.terminator = ""
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True
    ) as process:

        def forward(signum, _frame):
            if process.poll() is None:
                os.killpg(process.pid, signum)

        signal.signal(signal.SIGTERM, forward)
        signal.signal(signal.SIGINT, forward)
        try:
            while chunk := process.stdout.read1(min(65536, args.max_bytes)):
                handler.emit(
                    logging.LogRecord(
                        "broker",
                        logging.INFO,
                        "",
                        0,
                        chunk.decode("utf-8", errors="replace"),
                        (),
                        None,
                    )
                )
            returncode = process.wait()
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            handler.close()
    return returncode if returncode >= 0 else 128 - returncode


if __name__ == "__main__":
    raise SystemExit(main())
