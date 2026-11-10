"""Run with python3 -m controller.main --config configs/controller.json ..."""
from contextlib import closing
import argparse
import fcntl
import json
from pathlib import Path
import sys

from controller.api.device_client import DeviceClient
from controller.models.store import Store
from controller.scheduler.manager import Manager


def dispatch(config_path, command, request_id=None, spec=None, identifier=None, force=False):
    config = json.loads(Path(config_path).read_text())
    database = Path(config["database"])
    database.parent.mkdir(parents=True, exist_ok=True)
    # One controller operation at a time, including across separate CLI processes.
    with open(str(database) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        store = Store(database)
        with closing(store.connection):
            devices = DeviceClient(config["device_socket"])
            if command == "devices":
                result = devices.request("LIST")
            elif command == "status":
                result = {"instances": store.list(), "devices": devices.request("STATUS")}
            else:
                manager = Manager(config, store, devices)
                if command == "create":
                    result = manager.create(request_id, spec)
                elif command == "stop":
                    result = manager.stop(identifier, force)
                else:
                    result = manager.reconcile()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/controller.json")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("devices")
    commands.add_parser("status")
    commands.add_parser("reconcile")
    create = commands.add_parser("create")
    create.add_argument("--request-id", required=True)
    create.add_argument("--spec", required=True)
    stop = commands.add_parser("stop")
    stop.add_argument("id")
    stop.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        result = dispatch(args.config, args.command,
                          getattr(args, "request_id", None),
                          json.loads(Path(args.spec).read_text()) if hasattr(args, "spec") else None,
                          getattr(args, "id", None), getattr(args, "force", False))
        print(json.dumps(result, indent=2))
    except Exception as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
