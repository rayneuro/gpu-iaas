"""Optional administrator REST adapter. Bind to loopback only."""
import argparse
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import uuid

from controller.main import dispatch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/controller.json")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    token = os.environ.get("GPU_IAAS_API_TOKEN", "")
    if len(token) < 32:
        parser.error("GPU_IAAS_API_TOKEN must contain at least 32 characters")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, status, result):
            payload = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def handle_request(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.respond(401, {"error": "Unauthorized"})
                return
            try:
                if self.command == "GET" and self.path in {"/instances", "/devices"}:
                    command = "status" if self.path == "/instances" else "devices"
                    result = dispatch(args.config, command)
                elif self.command == "POST":
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 65536:
                        raise ValueError("JSON body length must be 1..65536")
                    body = json.loads(self.rfile.read(length))
                    if self.path == "/instances":
                        result = dispatch(args.config, "create", body["request_id"], body["spec"])
                    elif self.path.startswith("/instances/") and self.path.endswith("/stop"):
                        identifier = self.path.split("/")[2]
                        uuid.UUID(identifier)
                        if type(body.get("force", False)) is not bool:
                            raise ValueError("force must be boolean")
                        result = dispatch(args.config, "stop", identifier=identifier,
                                          force=body.get("force", False))
                    else:
                        self.respond(404, {"error": "Unknown endpoint"})
                        return
                else:
                    self.respond(404, {"error": "Unknown endpoint"})
                    return
                self.respond(200, result)
            except (ValueError, KeyError, TypeError) as error:
                self.respond(400, {"error": str(error)})
            except Exception as error:
                self.respond(409, {"error": str(error)})

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

    class Server(HTTPServer):
        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(10)
            return connection, address

    Server(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
