"""Acceptance-runner failure tests; never connect to actual Joplin."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPT = Path(__file__).resolve().with_name("live_acceptance.py")
spec = importlib.util.spec_from_file_location("acceptance_runner", SCRIPT)
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


class FakeClient:
    def __init__(self, bad_create=False, tag_error=False, foreign_note=False):
        self.bad_create = bad_create
        self.tag_error = tag_error
        self.foreign_note = foreign_note
        self.notes = {}
        self.folders = []
        self.deleted = []
        self.closed = False
        self.calls = []
        self.server_info = {"name": "fake", "version": "1"}
        self.protocol_version = live.mcp.PROTOCOL_VERSION

    def close(self):
        self.closed = True

    def tools(self):
        return [{"name": name} for name in live.mcp.EXPECTED_TOOLS]

    def call(self, tool, args):
        self.calls.append((tool, args))
        if tool == "list_notebooks":
            data = {"notebooks": [dict(folder, note_count=sum(
                note["notebook_id"] == folder["id"] and nid not in self.deleted
                for nid, note in self.notes.items())) for folder in self.folders]}
        elif tool == "create_notebook":
            data = dict(args, id=("a" if not self.folders else "b") * 32)
            self.folders.append(data)
        elif tool == "create_note":
            nid = ("c" if not self.notes else "d") * 32
            self.notes[nid] = dict(args, id=nid)
            data = dict(self.notes[nid])
            if self.bad_create:
                data["notebook_id"] = "unexpected-response"
        elif tool == "read_note":
            if args["id"] in self.deleted:
                raise live.mcp.ClientError("tool", "Trashed")
            data = dict(self.notes[args["id"]])
            # Stop the runner after creating both test notes. Cleanup still
            # receives the real title and folder so ownership can be checked.
            data["body"] = "unexpected body"
            if self.foreign_note:
                data["title"] = "pre-existing personal note"
        elif tool == "manage_tags":
            if self.tag_error:
                raise live.mcp.ClientError("tool", "Tag cleanup failed")
            data = {"tags": []}
        elif tool == "delete_note":
            self.deleted.append(args["id"])
            data = {"trashed": True}
        else:
            raise AssertionError("Unexpected fake call: " + tool)
        return {"content": [{"type": "text", "text": json.dumps(data)}]}


class AcceptanceFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.target = Path(self.tmp.name) / "report.json"

    def run_acceptance(self, client):
        doctor = {"ok": True, "data": {"missing_expected_tools": [], "disabled_tools": []}}
        proc = subprocess.CompletedProcess([], 0, stdout=json.dumps(doctor), stderr="")
        output = io.StringIO()
        with mock.patch.object(sys, "argv", [str(SCRIPT), "--output", str(self.target)]), \
                mock.patch.object(live.mcp, "make_settings", return_value={"token": "synthetic-live-token", "endpoint": "http://example.invalid/mcp"}), \
                mock.patch.object(live.mcp, "McpClient", return_value=client), \
                mock.patch.object(live.subprocess, "run", return_value=proc), \
                contextlib.redirect_stdout(output):
            status = live.main()
        return status, json.loads(output.getvalue())

    def test_created_note_registered_before_result_assertion(self):
        client = FakeClient(bad_create=True)
        status, result = self.run_acceptance(client)
        self.assertEqual(status, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(client.deleted, ["c" * 32])
        self.assertTrue(client.closed)
        report = json.loads(self.target.read_text(encoding="utf-8"))
        self.assertEqual(report["cleanup"]["errors"], [])
        self.assertEqual(set(report["cleanup"]["remaining_note_counts"].values()), {0})
        if os.name == "posix":
            self.assertEqual(self.target.stat().st_mode & 0o777, 0o600)

    def test_tag_cleanup_error_does_not_skip_note_deletion(self):
        client = FakeClient(tag_error=True)
        status, result = self.run_acceptance(client)
        self.assertEqual(status, 1)
        self.assertEqual(client.deleted, ["c" * 32, "d" * 32])
        self.assertTrue(result["cleanup_errors"])
        self.assertTrue(client.closed)

    def test_cleanup_refuses_unconfirmed_note_ownership(self):
        client = FakeClient(foreign_note=True)
        status, result = self.run_acceptance(client)
        self.assertEqual(status, 1)
        self.assertEqual(client.deleted, [])
        self.assertTrue(result["cleanup_errors"])
        self.assertFalse(any(tool == "manage_tags" for tool, _ in client.calls))

    def test_existing_report_refused_before_any_joplin_operation(self):
        self.target.write_text("keep existing data", encoding="utf-8")
        client = FakeClient()
        status, result = self.run_acceptance(client)
        self.assertEqual(status, 2)
        self.assertFalse(result["ok"])
        self.assertEqual(client.calls, [])
        self.assertEqual(self.target.read_text(encoding="utf-8"), "keep existing data")
        self.assertTrue(client.closed)


if __name__ == "__main__":
    unittest.main()
