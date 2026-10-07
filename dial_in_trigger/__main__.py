import argparse
import json
import logging
import os

from .config import WebConfig, WorkerConfig, state_dir
from .store import Store
from .worker import serve


def main():
    parser = argparse.ArgumentParser(description="Run phone-triggered scripts on this server.")
    parser.add_argument("command", choices=("worker", "check-config", "jobs"))
    args = parser.parse_args()
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.command == "worker":
            serve(WorkerConfig.from_env())
        elif args.command == "check-config":
            web, worker = WebConfig.from_env(), WorkerConfig.from_env()
            print(f"Webhook: {web.public_url}/voice")
            print(f"Number: {web.phone_number}")
            print(f"Local script: {worker.script}")
            print(f"State: {worker.state}")
            print("Configuration valid. No call placed or script executed.")
        else:
            print(json.dumps(Store(state_dir()).jobs(), indent=2))
    except (ValueError, RuntimeError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
