"""MCP server end to end against the fake Epic server.

Goes through the real MCP tool layer: list tools, connect (browser simulated),
poll status, then call every read tool and check the output.
"""
import asyncio
import http.server
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request

from test_offline import CLIENT_ID, FakeEpic, free_port

from epic_patient_fhir import mcp_server as srv


def _payload(result):
    """Tool results come back as JSON text content."""
    if result.is_error:
        raise AssertionError(result.content[0].text)
    return json.loads(result.content[0].text)


class McpEndToEnd(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeEpic)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}/fhir"
        port = free_port()
        srv.CALLBACK_PORT = port
        os.environ["EPIC_CLIENT_ID_PROD"] = CLIENT_ID
        os.environ["EPIC_REDIRECT_URI"] = f"http://localhost:{port}/callback"
        srv.LAUNCHER = lambda url: threading.Thread(
            target=lambda: urllib.request.urlopen(url, timeout=10).read(), daemon=True
        ).start()
        srv.STATE.reset()

    def tearDown(self):
        srv.STATE.reset()
        self.server.shutdown()
        self.server.server_close()

    def call(self, name, args=None):
        return _payload(asyncio.run(srv.mcp.call_tool(name, args or {})))

    def test_full_session(self):
        tools = {t.name for t in asyncio.run(srv.mcp.list_tools())}
        for expected in ("connect", "connection_status", "get_labs", "get_lab_trend", "get_note", "get_report", "export_record"):
            self.assertIn(expected, tools)

        # Not connected yet: tools explain instead of crashing
        self.assertIn("connect", self.call("get_labs")["error"])

        started = self.call("connect", {"custom_fhir_base": self.base})
        self.assertEqual(started["status"], "waiting_for_sign_in")

        deadline = time.monotonic() + 15
        while self.call("connection_status")["status"] != "connected":
            self.assertLess(time.monotonic(), deadline, self.call("connection_status"))
            time.sleep(0.2)

        self.assertEqual(self.call("get_patient")["names"], ["Test Patient"])

        labs = self.call("get_labs", {"name": "wbc"})
        self.assertEqual(labs["count"], 1)
        self.assertEqual(labs["results"][0]["flag"], "High")

        trend = self.call("get_lab_trend", {"name": "crp"})
        self.assertEqual(trend["CRP"][0]["value"], "34.6")

        self.assertEqual(self.call("get_labs", {"since": "2026-10-30"})["count"], 1)
        self.assertEqual(self.call("get_vitals")["results"][0]["value"], "116/62")

        conds = self.call("get_conditions")
        self.assertIn("403", conds["health_concerns"]["error"])

        self.assertEqual(self.call("get_medications", {"status": "active"})["medications"][0]["dosage"], "4.5 g IV q8h")

        notes = self.call("list_notes")
        self.assertEqual(notes["notes"][0]["type"], "Progress Note")
        note = self.call("get_note", {"note_id": notes["notes"][0]["id"]})
        self.assertIn("Hospital Day: 2", note["text"])

        reps = self.call("list_reports", {"query": "imaging"})
        rep = self.call("get_report", {"report_id": reps["reports"][0]["id"]})
        self.assertIn("IMPRESSION", rep["text"])
        self.assertEqual(rep["results"][0]["name"], "WBC")

        with tempfile.TemporaryDirectory() as td:
            exp = self.call("export_record", {"folder": td})
            self.assertEqual(exp["files"]["notes"], 1)

        self.assertEqual(self.call("disconnect")["status"], "disconnected")
        self.assertIn("connect", self.call("get_labs")["error"])


if __name__ == "__main__":
    unittest.main()
