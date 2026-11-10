import json
import sqlite3


class Store:
    def __init__(self, path):
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS instances (
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                spec TEXT NOT NULL, state TEXT NOT NULL, error TEXT);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, instance_id TEXT NOT NULL,
                state TEXT NOT NULL, detail TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        """)

    def get(self, identifier):
        row = self.connection.execute("SELECT * FROM instances WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise ValueError("Unknown instance")
        result = dict(row)
        result["spec"] = json.loads(result["spec"])
        return result

    def list(self):
        return [self.get(row[0]) for row in self.connection.execute("SELECT id FROM instances")]

    def create(self, identifier, request_id, spec):
        existing = self.connection.execute(
            "SELECT id FROM instances WHERE request_id=?", (request_id,)).fetchone()
        if existing:
            instance = self.get(existing[0])
            if instance["spec"] != spec:
                raise ValueError("Request id reused with different specification")
            return instance
        with self.connection:
            self.connection.execute("INSERT INTO instances VALUES (?,?,?,?,NULL)",
                                    (identifier, request_id, json.dumps(spec), "REQUESTED"))
        return self.get(identifier)

    def transition(self, identifier, state, detail=None):
        with self.connection:
            self.connection.execute("UPDATE instances SET state=?, error=? WHERE id=?",
                                    (state, detail, identifier))
            self.connection.execute("INSERT INTO events(instance_id,state,detail) VALUES (?,?,?)",
                                    (identifier, state, detail))
