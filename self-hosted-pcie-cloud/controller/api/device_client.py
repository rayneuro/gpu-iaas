"""Private Unix socket protocol. The daemon accepts only its own Unix uid."""
import json
import re
import socket


class DeviceClient:
    def __init__(self, path):
        self.path = path

    def request(self, command, *arguments):
        if command not in {"LIST", "STATUS", "ACQUIRE", "RELEASE"}:
            raise ValueError("Unknown daemon command")
        if any(not re.fullmatch(r"[A-Za-z0-9_.:-]+", value) for value in arguments):
            raise ValueError("Invalid daemon argument")
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(60)
            connection.connect(self.path)
            connection.sendall((" ".join((command, *arguments)) + "\n").encode())
            with connection.makefile("rb") as stream:
                raw = stream.readline(1024 * 1024)
        result = json.loads(raw)
        if not result["ok"]:
            raise RuntimeError(result["error"])
        return result
