"""Command-line entry point for the host broker."""

import argparse
import os
from pathlib import Path

import uvicorn

from docker_sandbox_broker.api import create_app
from docker_sandbox_broker.config import BrokerSettings
from docker_sandbox_broker.logging import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the local Docker sandbox broker")
    parser.add_argument("--uds", type=Path, help="user-owned Unix socket to listen on")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    configure_logging()
    settings = BrokerSettings.from_environment()
    app = create_app(settings=settings)
    if args.uds:
        args.uds.parent.mkdir(parents=True, exist_ok=True)
        previous_umask = os.umask(0o077)
        try:
            uvicorn.run(app, uds=str(args.uds))
        finally:
            os.umask(previous_umask)
        return
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
