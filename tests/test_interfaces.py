from http.client import HTTPConnection
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest

from river_valley import World, replay
from river_valley.web import make_server


class InterfaceTests(unittest.TestCase):
    def test_cli_simulation_replay_and_scheduled_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            inputs = folder / "inputs.json"
            inputs.write_text(json.dumps([
                dict(day=0, type="action", actor="player", kind="divert", amount=100),
                dict(day=2, type="rainfall", value=20)]))
            simulated = subprocess.run([sys.executable, "-m", "river_valley", "simulate",
                                        "--days", "95", "--inputs", str(inputs),
                                        "--output", str(folder / "run")], capture_output=True, text=True)
            self.assertEqual(simulated.returncode, 0, simulated.stderr)
            summary = json.loads(simulated.stdout)
            self.assertEqual(summary["day"], 95)
            self.assertGreater(summary["harvest"], 0)
            result = subprocess.run([sys.executable, "-m", "river_valley", "replay",
                                     str(folder / "run/events.jsonl")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["state_hash"], summary["state_hash"])
            saved = World.load(folder / "run/snapshot.json")
            self.assertEqual(saved.summary()["state_hash"], summary["state_hash"])

    def test_http_map_actions_validation_and_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            sink = io.StringIO()
            world = World(event_sink=sink)
            server = make_server(world, port=0, output=directory)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=30)
            try:
                connection.request("GET", "/")
                page = connection.getresponse()
                self.assertEqual(page.status, 200)
                html = page.read().decode()
                self.assertIn('id="map"', html)
                token = re.search(r"const token='([a-f0-9]+)'", html).group(1)

                def request(path, payload, authenticated=True):
                    headers = {"Content-Type": "application/json"}
                    if authenticated:
                        headers["X-River-Token"] = token
                    connection.request("POST", path, json.dumps(payload), headers)
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())

                self.assertEqual(request("/api/advance", dict(days=1), False)[0], 403)
                self.assertEqual(world.state["day"], 0)
                self.assertEqual(request("/api/action", dict(kind="transfer", target="h01", amount=10))[0], 200)
                self.assertEqual(request("/api/action", dict(kind="produce"))[0], 400)
                self.assertEqual(request("/api/rainfall", dict(value=0))[0], 200)
                self.assertEqual(request("/api/advance", dict(days=90))[0], 200)
                self.assertEqual(world.state["day"], 90)
                self.assertEqual(world.state["ledger"]["harvest"], 0)
                self.assertEqual(request("/api/advance", dict(days=1000))[0], 400)
                self.assertEqual(request("/api/save", {})[0], 200)
                checkpoint = World.load(Path(directory) / "snapshot.json")
                self.assertEqual(checkpoint.state, world.state)
                self.assertEqual(replay(io.StringIO(sink.getvalue())).state, world.state)
                connection.request("GET", "/api/state")
                response = connection.getresponse()
                payload = json.loads(response.read())
                self.assertEqual(len(payload["state"]["people"]), 121)
                self.assertEqual(payload["summary"]["day"], 90)
                self.assertGreater(len(payload["events"]), 0)
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == "__main__":
    unittest.main()
