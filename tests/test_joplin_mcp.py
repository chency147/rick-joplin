"""Standard-library protocol, CLI, batch, image and failure tests."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "joplin_mcp.py"
spec = importlib.util.spec_from_file_location("rick_joplin", SCRIPT)
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)
TOKEN = "unit-token-含中文&secret"
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jN9kAAAAASUVORK5CYII=")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            # Expected when timeout/oversize tests deliberately close a socket.
            pass

    def send(self, status, body=b"", content_type="application/json", session=False):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body) + (100 if self.server.state.get("mode") == "truncated-http" and self.server.state["requests"][-1]["body"]["method"] == "tools/call" else 0)))
        self.send_header("Content-Type", content_type)
        if self.server.state.get("mode") == "truncated-http":
            self.send_header("Connection", "close")
            self.close_connection = True
        if session:
            value = "unit-session\tbad" if self.server.state.get("mode") == "invalid-session" else "unit-session"
            self.send_header("Mcp-Session-Id", value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        state = self.server.state
        state["requests"].append({"body": body, "headers": dict(self.headers), "port": self.client_address[1]})
        if parse_qs(urlsplit(self.path).query).get("token") != [TOKEN]:
            self.send(401, b"invalid token")
            return
        mode = state.get("mode", "normal")
        if mode == "timeout":
            time.sleep(0.15)
        if mode.startswith("http-"):
            self.send(int(mode[5:]), ("failed " + self.path).encode())
            return
        if mode == "invalid-json":
            self.send(200, b"not json")
            return
        if mode == "empty":
            self.send(202)
            return
        if mode == "sse":
            self.send(200, b"data: {}\n\n", "text/event-stream")
            return
        if "id" not in body:
            self.send(500 if mode == "notification-error" else 202)
            return
        method = body["method"]
        params = body.get("params", {})
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "unit", "version": "1"}}
            if mode == "no-tools-capability":
                result["capabilities"] = {}
            elif mode == "unsupported-protocol":
                result["protocolVersion"] = "2099-01-01"
            elif mode == "invalid-tools-capability":
                result["capabilities"]["tools"] = False
            elif mode == "invalid-server-info":
                result["serverInfo"] = None
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            tools = [{"name": name, "description": "Description " + name, "inputSchema": {"type": "object"}} for name in mcp.EXPECTED_TOOLS]
            tools.append({"name": "future_tool", "description": "Future tool", "inputSchema": {"type": "object"}})
            if mode == "disabled":
                tools[0]["description"] = "(Disabled tool) enable it"
            if mode == "bad-description":
                tools[0]["description"] = None
            elif mode == "bad-schema":
                tools[0]["inputSchema"] = []
            elif mode == "duplicate-tools":
                tools.append(tools[0])
            if mode == "endless-pages":
                result = {"tools": [], "nextCursor": str(len(state["requests"]))}
            elif mode == "bad-cursor":
                result = {"tools": tools, "nextCursor": 0}
            elif mode == "paginated-tools":
                if params.get("cursor"):
                    result = {"tools": tools[5:]}
                else:
                    result = {"tools": tools[:5], "nextCursor": "page2"}
            elif mode == "repeated-cursor":
                result = {"tools": [], "nextCursor": "same"}
            else:
                result = {"tools": tools}
        elif method == "tools/call":
            name = params["name"]
            if mode.startswith("tool-http-"):
                self.send(int(mode[len("tool-http-"):]), b"Failure after tool dispatch")
                return
            if mode == "truncated-token":
                self.send(500, ("x" * 1985 + TOKEN).encode())
                return
            if mode == "tool-error":
                result = {"isError": True, "content": [{"type": "text", "text": "Read refused"}]}
            elif name == "partial_failure":
                state["mutation_count"] = state.get("mutation_count", 0) + 1
                result = {"isError": True, "content": [{"type": "text", "text": "Failure after partial write"}]}
            elif mode == "bad-mime":
                result = {"content": [{"type": "image", "mimeType": [], "data": "YQ=="}]}
            elif mode == "bad-is-error":
                result = {"isError": "false", "content": []}
            elif mode == "bad-content-type":
                result = {"content": [{}]}
            elif name == "malformed":
                result = {"content": [None]}
            elif name == "write_image":
                result = {"content": [{"type": "image", "data": base64.b64encode(PNG).decode(), "mimeType": "image/png"}]}
            elif name == "fail":
                result = {"isError": True, "content": [{"type": "text", "text": "Tool refused: " + TOKEN}]}
            elif name == "read_image":
                result = {"content": [{"type": "image", "data": base64.b64encode(PNG).decode(), "mimeType": "image/png"}]}
            elif name == "plain":
                result = {"content": [{"type": "text", "text": "plain response"}]}
            elif name == "multi":
                result = {"content": [{"type": "text", "text": '{"x":1}'}, {"type": "resource_link", "uri": "local://test"}], "_meta": {"x": 2}}
            elif name == "write_timeout":
                time.sleep(0.3)
                result = {"content": [{"type": "text", "text": '{"id":"written"}'}]}
            else:
                result = {"content": [{"type": "text", "text": json.dumps({"tool": name, "arguments": params.get("arguments", {}), "body": "中文 😀"}, ensure_ascii=False)}]}
        else:
            result = {}
        envelope = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        if mode == "rpc-error":
            envelope = {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32602, "message": "bad params"}}
        elif mode == "wrong-id":
            envelope["id"] = 999
        elif mode == "missing-result":
            envelope.pop("result")
        elif mode == "boolean-id":
            envelope["id"] = True
        elif mode == "fractional-id":
            envelope["id"] = float(body["id"])
        elif method == "tools/call" and mode == "both-result-and-error":
            envelope["error"] = {"code": -32602, "message": "ambiguous response"}
        elif method == "tools/call" and mode == "invalid-rpc-error":
            envelope = {"jsonrpc": "2.0", "id": body["id"], "error": {"code": True, "message": {}}}
        elif method == "tools/call" and mode.startswith("tool-rpc-"):
            envelope = {"jsonrpc": "2.0", "id": body["id"], "error": {
                "code": int(mode[len("tool-rpc-"):]), "message": "Tool RPC failed",
                "data": {TOKEN: "diagnostic"},
            }}
        data = json.dumps(envelope, ensure_ascii=True).encode()
        if method == "tools/call" and mode == "deep-json":
            data = b'[' * 1500 + b'0' + b']' * 1500
        elif method == "tools/call" and mode == "duplicate-response-keys":
            data = ('{"jsonrpc":"2.0","id":%s,"result":{},"result":{}}' % body["id"]).encode()
        elif method == "tools/call" and mode == "escaped-token":
            self.send(500, json.dumps({"echo": TOKEN}, ensure_ascii=True).encode())
            return
        self.send(200, data, session=method == "initialize")


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = "http://127.0.0.1:%s/mcp" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.server.state = {"mode": "normal", "requests": []}
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = mcp.make_settings(mcp.build_parser().parse_args(["--url", self.url, "ping"]), {"JOPLIN_TOKEN": TOKEN})
        self.client = mcp.McpClient(self.settings)
        self.addCleanup(self.client.close)

    def cli(self, *args, stdin=None, env_update=None):
        env = os.environ.copy()
        for key in ("JOPLIN_TOKEN", "JOPLIN_MCP_URL", "JOPLIN_URL"):
            env.pop(key, None)
        env.update({"JOPLIN_TOKEN": TOKEN, "JOPLIN_MCP_URL": self.url})
        if env_update:
            for key, value in env_update.items():
                if value is None:
                    env.pop(key, None)
                else:
                    env[key] = value
        p = subprocess.run([sys.executable, str(SCRIPT), *args], input=stdin, text=True, encoding="utf-8", capture_output=True, env=env, timeout=5)
        return p, json.loads(p.stdout)

    def test_initialize_notification_and_ping(self):
        self.client.initialize()
        self.assertEqual(self.client.rpc("ping", {}), {})
        requests = self.server.state["requests"]
        self.assertEqual([r["body"]["method"] for r in requests], ["initialize", "notifications/initialized", "ping"])
        self.assertNotIn("id", requests[1]["body"])
        self.assertEqual(requests[2]["headers"]["Mcp-Session-Id"], "unit-session")
        self.assertEqual(requests[2]["headers"]["MCP-Protocol-Version"], "2025-06-18")
        self.assertEqual(len({r["port"] for r in requests}), 1)

    def test_all_tools_and_future_tool_passthrough(self):
        names = {t["name"] for t in self.client.tools()}
        self.assertTrue(set(mcp.EXPECTED_TOOLS) <= names)
        self.assertIn("future_tool", names)
        result = self.client.call("future_tool", {"custom": {"中文": "😀"}})
        self.assertEqual(json.loads(result["content"][0]["text"])["arguments"], {"custom": {"中文": "😀"}})

    def test_tools_cache(self):
        self.client.tools()
        self.client.tools()
        self.assertEqual(sum(r["body"]["method"] == "tools/list" for r in self.server.state["requests"]), 1)

    def test_tools_pagination(self):
        self.server.state["mode"] = "paginated-tools"
        self.assertEqual(len(self.client.tools()), 12)

    def test_repeated_cursor_rejected(self):
        self.server.state["mode"] = "repeated-cursor"
        with self.assertRaises(mcp.ClientError) as caught:
            self.client.tools()
        self.assertEqual(caught.exception.kind, "protocol")

    def test_doctor_reports_disabled(self):
        self.server.state["mode"] = "disabled"
        p, result = self.cli("doctor")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(result["data"]["disabled_tools"], ["search_notes"])
        self.assertEqual(result["data"]["missing_expected_tools"], [])
        self.assertNotIn(TOKEN, p.stdout)

    def test_describe_preserves_schema(self):
        p, result = self.cli("describe", "read_note")
        self.assertEqual(p.returncode, 0)
        self.assertEqual(result["data"]["inputSchema"], {"type": "object"})

    def test_unknown_description_is_error(self):
        p, result = self.cli("describe", "missing")
        self.assertEqual(p.returncode, 2)
        self.assertFalse(result["ok"])

    def test_config_and_precedence(self):
        path = Path(self.tmp.name) / "config.json"
        path.write_text(json.dumps({"url": "http://example.invalid:1", "token": "other", "timeout": 12}), encoding="utf-8")
        args = mcp.build_parser().parse_args(["--config", str(path), "ping", "--url", self.url, "--timeout", "2"])
        settings = mcp.make_settings(args, {"JOPLIN_TOKEN": TOKEN, "JOPLIN_MCP_URL": "http://env.invalid"})
        self.assertEqual(settings["token"], TOKEN)
        self.assertEqual(settings["timeout"], 2)
        self.assertEqual(settings["endpoint"], self.url)

    def test_options_before_and_after_command(self):
        p, result = self.cli("--url", self.url, "call", "read_note", "--timeout", "2", "--args", '{"id":"abc"}')
        self.assertEqual(p.returncode, 0)
        self.assertEqual(result["data"]["arguments"], {"id": "abc"})

    def test_base_url_compatibility(self):
        args = mcp.build_parser().parse_args(["ping"])
        settings = mcp.make_settings(args, {"JOPLIN_TOKEN": TOKEN, "JOPLIN_URL": self.url[:-4]})
        self.assertEqual(settings["endpoint"], self.url)

    def test_existing_query_token_replaced(self):
        args = mcp.build_parser().parse_args(["ping", "--url", self.url + "?x=1&token=old"])
        settings = mcp.make_settings(args, {"JOPLIN_TOKEN": TOKEN})
        self.assertEqual(parse_qs(urlsplit(settings["target"]).query), {"x": ["1"], "token": [TOKEN]})

    def test_token_in_url_supported(self):
        from urllib.parse import urlencode
        args = mcp.build_parser().parse_args(["ping", "--url", self.url + "?" + urlencode({"token": TOKEN})])
        self.assertEqual(mcp.make_settings(args, {})["token"], TOKEN)

    def test_missing_token_fails_before_network(self):
        p, result = self.cli("ping", env_update={"JOPLIN_TOKEN": None})
        self.assertEqual(p.returncode, 2)
        self.assertEqual(result["error"]["kind"], "configuration")
        self.assertEqual(self.server.state["requests"], [])

    def test_invalid_urls_and_timeouts(self):
        for url in ("file:///tmp/a", "http://a:bad", "http://user:password@host", "http://host/#fragment"):
            with self.subTest(url=url), self.assertRaises(mcp.ClientError):
                mcp.make_settings(mcp.build_parser().parse_args(["ping", "--url", url]), {"JOPLIN_TOKEN": TOKEN})
        for value in ("0", "-1", "nan", "inf"):
            with self.subTest(timeout=value), self.assertRaises(mcp.ClientError):
                mcp.make_settings(mcp.build_parser().parse_args(["ping", "--timeout", value]), {"JOPLIN_TOKEN": TOKEN})

    def test_unknown_config_key(self):
        path = Path(self.tmp.name) / "config.json"
        path.write_text('{"unexpected":1}')
        p, result = self.cli("ping", "--config", str(path))
        self.assertEqual(p.returncode, 2)
        self.assertEqual(result["error"]["kind"], "input")

    def test_file_and_stdin_arguments(self):
        payload = {"body": "中文\n'quotes' \"double\" $HOME `literal` 😀", "id": "note"}
        path = Path(self.tmp.name) / "args.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        for args, stdin in ((["--args-file", str(path)], None), (["--stdin"], json.dumps(payload, ensure_ascii=False))):
            with self.subTest(args=args):
                p, result = self.cli("call", "update_note", *args, stdin=stdin)
                self.assertEqual(p.returncode, 0)
                self.assertEqual(result["data"]["arguments"], payload)

    def test_invalid_arguments_rejected_before_network(self):
        for value in ("[]", "null", "{bad"):
            with self.subTest(value=value):
                p, result = self.cli("call", "create_note", "--args", value)
                self.assertEqual(p.returncode, 2)
                self.assertFalse(result["ok"])
        self.assertEqual(self.server.state["requests"], [])

    def test_http_errors_redact_token(self):
        for status in (401, 403, 404, 302, 500):
            with self.subTest(status=status):
                self.server.state["mode"] = "http-%s" % status
                p, result = self.cli("ping")
                self.assertEqual(p.returncode, 1)
                self.assertEqual(result["error"]["http_status"], status)
                self.assertNotIn(TOKEN, p.stdout + p.stderr)
                self.assertNotIn("unit-token", p.stdout + p.stderr)

    def test_protocol_failures(self):
        for mode in ("invalid-json", "empty", "sse", "wrong-id", "missing-result", "no-tools-capability"):
            with self.subTest(mode=mode):
                self.server.state["mode"] = mode
                p, result = self.cli("ping")
                self.assertEqual(p.returncode, 1)
                self.assertEqual(result["error"]["kind"], "protocol")

    def test_rpc_error(self):
        self.server.state["mode"] = "rpc-error"
        p, result = self.cli("ping")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(result["error"]["rpc_code"], -32602)

    def test_tool_error_http_200(self):
        p, result = self.cli("call", "fail")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(result["error"]["kind"], "tool")
        self.assertNotIn(TOKEN, p.stdout)

    def test_timeout_is_not_retried(self):
        self.server.state["mode"] = "timeout"
        p, result = self.cli("ping", "--timeout", "0.03")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(result["error"]["kind"], "transport")
        self.assertEqual(len(self.server.state["requests"]), 1)

    def test_write_timeout_marks_unknown(self):
        p, result = self.cli("call", "write_timeout", "--timeout", "0.1")
        self.assertEqual(p.returncode, 1)
        self.assertTrue(result["error"]["outcome_unknown"])
        self.assertEqual(sum(r["body"]["method"] == "tools/call" for r in self.server.state["requests"]), 1)

    def test_plain_and_multiple_content(self):
        p, result = self.cli("call", "plain")
        self.assertEqual(result["data"], "plain response")
        p, result = self.cli("call", "multi")
        self.assertEqual(result["data"][0], {"x": 1})
        self.assertEqual(result["data"][1]["type"], "resource_link")
        self.assertEqual(result["mcp_metadata"]["_meta"], {"x": 2})

    def test_image_saved_and_no_base64_in_output(self):
        path = Path(self.tmp.name) / "image.png"
        p, result = self.cli("image", "resource", "--resolution", "high", "--output", str(path))
        self.assertEqual(p.returncode, 0)
        self.assertEqual(path.read_bytes(), PNG)
        self.assertEqual(result["artifacts"][0]["bytes"], len(PNG))
        self.assertNotIn(base64.b64encode(PNG).decode(), p.stdout)
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_generic_image_and_raw_mode(self):
        p, result = self.cli("call", "read_image", "--artifacts-dir", self.tmp.name)
        self.assertEqual(Path(result["artifacts"][0]["path"]).read_bytes(), PNG)
        p, result = self.cli("call", "read_image", "--raw")
        self.assertEqual(result["result"]["content"][0]["data"], base64.b64encode(PNG).decode())
        self.assertNotIn("artifacts", result)

    def test_image_never_overwrites(self):
        path = Path(self.tmp.name) / "existing.png"
        path.write_bytes(b"original")
        p, result = self.cli("image", "resource", "--output", str(path))
        self.assertEqual(p.returncode, 1)
        self.assertEqual(result["error"]["kind"], "output")
        self.assertEqual(path.read_bytes(), b"original")

    def test_raw_image_output_combination_rejected(self):
        p, result = self.cli("image", "resource", "--raw", "--output", str(Path(self.tmp.name) / "a.png"))
        self.assertEqual(p.returncode, 2)
        self.assertEqual(self.server.state["requests"], [])

    def test_invalid_image_data(self):
        writer = mcp.ResultWriter(self.tmp.name)
        for item in ({"type": "image", "mimeType": "image/png", "data": "bad!"}, {"type": "image", "mimeType": "image/png", "data": ""}, {"type": "image", "mimeType": "text/plain", "data": "YQ=="}):
            with self.subTest(item=item), self.assertRaises(mcp.ClientError):
                writer.format("read_image", {"content": [item]})

    def test_batch_one_initialization_and_connection(self):
        calls = [{"tool": "read_note", "arguments": {"id": str(i)}} for i in range(4)]
        p, result = self.cli("batch", "--stdin", stdin=json.dumps(calls))
        self.assertEqual(p.returncode, 0)
        self.assertEqual(result["summary"], {"requested": 4, "completed": 4, "failed": 0, "skipped": 0, "transactional": False})
        requests = self.server.state["requests"]
        self.assertEqual(sum(r["body"]["method"] == "initialize" for r in requests), 1)
        self.assertEqual(len({r["port"] for r in requests}), 1)

    def test_batch_failure_stop_and_continue(self):
        calls = [{"tool": "read_note", "arguments": {}}, {"tool": "fail"}, {"tool": "list_tags", "label": "last"}]
        for continued in (False, True):
            with self.subTest(continued=continued):
                args = ["batch", "--stdin"] + (["--continue-on-error"] if continued else [])
                p, result = self.cli(*args, stdin=json.dumps(calls))
                self.assertEqual(p.returncode, 1)
                self.assertEqual(result["summary"]["failed"], 1)
                self.assertEqual(result["summary"]["skipped"], 0 if continued else 1)
                self.assertNotIn(TOKEN, p.stdout)
                if continued:
                    self.assertEqual(result["results"][-1]["label"], "last")

    def test_entire_batch_validated_before_writes(self):
        for calls in ([], [{"tool": "create_note"}, {"tool": "read_note", "arguments": []}], [{"tool": "create_note", "unknown": 1}]):
            with self.subTest(calls=calls):
                p, result = self.cli("batch", "--stdin", stdin=json.dumps(calls))
                self.assertEqual(p.returncode, 2)
        self.assertEqual(self.server.state["requests"], [])

    def test_batch_file(self):
        path = Path(self.tmp.name) / "batch.json"
        path.write_text('[{"tool":"list_tags"}]')
        p, result = self.cli("batch", "--file", str(path))
        self.assertEqual(p.returncode, 0)
        self.assertEqual(result["summary"]["completed"], 1)

    def test_response_size_limit(self):
        with mock.patch.object(mcp, "MAX_RESPONSE_BYTES", 8), self.assertRaises(mcp.ClientError) as caught:
            self.client.initialize()
        self.assertEqual(caught.exception.kind, "protocol")

    def test_nonstandard_json_constants_rejected(self):
        for value in ('{"limit":NaN}', '{"limit":Infinity}', '{"limit":-Infinity}'):
            with self.subTest(value=value):
                p, result = self.cli("call", "search_notes", "--args", value)
                self.assertEqual(p.returncode, 2)
                self.assertEqual(result["error"]["kind"], "input")
        self.assertEqual(self.server.state["requests"], [])

    def test_utf16_surrogate_chunk_output_and_input(self):
        rendered = mcp.dump_json({"body": "中文 \ud83d"})
        self.assertEqual(json.loads(rendered)["body"], "中文 \ud83d")
        # A lone code unit is serialized safely in HTTP request arguments too.
        result = self.client.call("read_note", {"text": "\ud83d"})
        self.assertEqual(json.loads(result["content"][0]["text"])["arguments"]["text"], "\ud83d")

    def test_unsupported_protocol_rejected_before_tool_calls(self):
        self.server.state["mode"] = "unsupported-protocol"
        p, result = self.cli("call", "create_note", "--args", '{"title":"test"}')
        self.assertEqual(p.returncode, 1)
        self.assertEqual(result["error"]["kind"], "protocol")
        self.assertEqual(result["error"]["protocol_version"], "2099-01-01")
        self.assertEqual([r["body"]["method"] for r in self.server.state["requests"]], ["initialize"])
        self.assertNotIn("outcome_unknown", result["error"])

    def test_write_http_failure_classification(self):
        for status in (408, 500, 502, 503, 401, 403, 404):
            with self.subTest(status=status):
                self.server.state["mode"] = "tool-http-%s" % status
                p, result = self.cli("call", "create_note", "--args", '{"title":"test"}')
                self.assertEqual(p.returncode, 1)
                self.assertEqual(result["error"]["http_status"], status)
                self.assertEqual(result["error"].get("outcome_unknown", False), status == 408 or status >= 500)

    def test_read_http_failure_has_no_write_uncertainty(self):
        self.server.state["mode"] = "tool-http-500"
        p, result = self.cli("call", "read_note")
        self.assertEqual(p.returncode, 1)
        self.assertNotIn("outcome_unknown", result["error"])

    def test_write_rpc_failure_classification_and_key_redaction(self):
        for code in (-32700, -32600, -32601, -32602, -32603, -32000):
            with self.subTest(code=code):
                self.server.state["mode"] = "tool-rpc-%s" % code
                p, result = self.cli("call", "create_note")
                self.assertEqual(p.returncode, 1)
                self.assertEqual(result["error"].get("outcome_unknown", False), code in (-32603, -32000))
                self.assertEqual(result["error"]["rpc_data"], {"[REDACTED]": "diagnostic"})
                self.assertNotIn(TOKEN, p.stdout)

    def test_malformed_write_content_marks_unknown(self):
        p, result = self.cli("call", "malformed")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(result["error"]["kind"], "protocol")
        self.assertTrue(result["error"]["outcome_unknown"])
        self.assertEqual(result["error"]["tool"], "malformed")
        self.assertEqual(sum(r["body"]["method"] == "tools/call" for r in self.server.state["requests"]), 1)

    def test_batch_malformed_content_stops_before_later_write(self):
        p, result = self.cli("batch", "--stdin", stdin='[{"tool":"malformed"},{"tool":"create_note"}]')
        self.assertEqual(p.returncode, 1)
        self.assertTrue(result["results"][0]["error"]["outcome_unknown"])
        self.assertEqual(result["summary"]["skipped"], 1)
        self.assertEqual(sum(r["body"]["method"] == "tools/call" for r in self.server.state["requests"]), 1)

    def test_local_output_failure_preserves_server_success(self):
        # A directory cannot be created over an existing file.
        artifacts = Path(self.tmp.name) / "not-a-directory"
        artifacts.write_bytes(b"keep")
        p, result = self.cli("call", "write_image", "--artifacts-dir", str(artifacts))
        self.assertEqual(p.returncode, 1)
        self.assertEqual(result["error"]["kind"], "output")
        self.assertTrue(result["error"]["server_succeeded"])
        self.assertNotIn("outcome_unknown", result["error"])
        self.assertEqual(artifacts.read_bytes(), b"keep")
        self.assertEqual(sum(r["body"]["method"] == "tools/call" for r in self.server.state["requests"]), 1)

    def test_batch_local_output_failure_preserves_server_success(self):
        artifacts = Path(self.tmp.name) / "not-a-directory"
        artifacts.write_bytes(b"keep")
        p, result = self.cli("batch", "--stdin", "--artifacts-dir", str(artifacts),
                             stdin='[{"tool":"write_image"},{"tool":"create_note"}]')
        self.assertEqual(p.returncode, 1)
        self.assertTrue(result["results"][0]["error"]["server_succeeded"])
        self.assertEqual(result["summary"]["skipped"], 1)

    def test_http_token_redacted_before_truncation(self):
        self.server.state["mode"] = "truncated-token"
        p, result = self.cli("call", "read_note")
        self.assertEqual(p.returncode, 1)
        self.assertIn("[REDACTED]", result["error"]["server_message"])
        self.assertNotIn("unit-token", p.stdout)

    def test_config_token_redacted_even_when_configuration_invalid(self):
        config = Path(self.tmp.name) / "config.json"
        config.write_text(json.dumps({"token": TOKEN, TOKEN: "unexpected key"}), encoding="utf-8")
        p, result = self.cli("ping", "--config", str(config), env_update={"JOPLIN_TOKEN": None})
        self.assertEqual(p.returncode, 2)
        self.assertIn("[REDACTED]", result["error"]["message"])
        self.assertNotIn(TOKEN, p.stdout)
        self.assertEqual(self.server.state["requests"], [])

    def test_overflow_numbers_rejected_before_batch_writes(self):
        for number in ("1e999", "-1e999"):
            with self.subTest(number=number):
                payload = '[{"tool":"create_note"},{"tool":"read_note","arguments":{"limit":%s}}]' % number
                p, result = self.cli("batch", "--stdin", stdin=payload)
                self.assertEqual(p.returncode, 2)
                self.assertEqual(result["error"]["kind"], "input")
                self.assertEqual(self.server.state["requests"], [])

    def test_batch_serialization_preflight_for_every_item(self):
        args = mcp.build_parser().parse_args(["batch", "--file", "unused.json"])
        calls = [{"tool": "create_note"}, {"tool": "read_note", "arguments": {"limit": float("inf")}}]
        with mock.patch.object(mcp, "read_json_file", return_value=calls):
            with self.assertRaises(mcp.ClientError) as caught:
                mcp.load_batch(args)
        self.assertEqual(caught.exception.kind, "input")
        self.assertEqual(caught.exception.details["index"], 1)
        self.assertEqual(self.server.state["requests"], [])

    def test_finite_numbers_still_supported(self):
        self.assertEqual(mcp.json_loads('{"large":1e308,"tiny":1e-300}'), {"large": 1e308, "tiny": 1e-300})

    def test_cli_json_pipes_use_utf8_even_with_ascii_environment(self):
        payload = {"body": "中文 😀"}
        p, result = self.cli("call", "future_tool", "--stdin", stdin=json.dumps(payload, ensure_ascii=False),
                             env_update={"PYTHONIOENCODING": "ascii"})
        self.assertEqual(p.returncode, 0)
        self.assertEqual(result["data"]["arguments"], payload)
        self.assertEqual(result["data"]["body"], "中文 😀")

    def test_invalid_rpc_ids_rejected(self):
        for mode in ("boolean-id", "fractional-id"):
            with self.subTest(mode=mode):
                self.server.state["mode"] = mode
                p, result = self.cli("ping")
                self.assertEqual(p.returncode, 1)
                self.assertEqual(result["error"]["kind"], "protocol")

    def test_malformed_tool_responses_keep_json_and_write_uncertainty(self):
        for mode in ("bad-mime", "bad-is-error", "bad-content-type", "both-result-and-error",
                     "invalid-rpc-error", "deep-json", "duplicate-response-keys", "truncated-http"):
            with self.subTest(mode=mode):
                self.server.state = {"mode": mode, "requests": []}
                p, result = self.cli("call", "create_note")
                self.assertEqual(p.returncode, 1)
                self.assertEqual(result["error"]["kind"], "protocol")
                self.assertTrue(result["error"]["outcome_unknown"])
                self.assertEqual(p.stderr, "")
                self.assertEqual(sum(r["body"]["method"] == "tools/call" for r in self.server.state["requests"]), 1)

    def test_raw_mode_still_rejects_invalid_content_shapes(self):
        self.server.state["mode"] = "bad-mime"
        p, result = self.cli("call", "create_note", "--raw")
        self.assertEqual(p.returncode, 1)
        self.assertTrue(result["error"]["outcome_unknown"])

    def test_malformed_batch_keeps_previous_success_and_skips_later_write(self):
        p, result = self.cli("batch", "--stdin", stdin='[{"tool":"list_tags"},{"tool":"malformed"},{"tool":"create_note"}]')
        self.assertEqual(p.returncode, 1)
        self.assertTrue(result["results"][0]["ok"])
        self.assertTrue(result["results"][1]["error"]["outcome_unknown"])
        self.assertEqual(result["summary"], {"requested": 3, "completed": 1, "failed": 1, "skipped": 1, "transactional": False})

    def test_invalid_discovery_responses_return_structured_errors(self):
        for mode in ("bad-description", "bad-schema", "duplicate-tools", "bad-cursor",
                     "invalid-tools-capability", "invalid-server-info", "invalid-session"):
            with self.subTest(mode=mode):
                self.server.state["mode"] = mode
                p, result = self.cli("doctor")
                self.assertEqual(p.returncode, 1)
                self.assertEqual(result["error"]["kind"], "protocol")
                self.assertEqual(p.stderr, "")

    def test_tool_pagination_bounded_even_with_unique_cursors(self):
        self.server.state["mode"] = "endless-pages"
        with mock.patch.object(mcp, "MAX_TOOL_PAGES", 3):
            with self.assertRaises(mcp.ClientError) as caught:
                self.client.tools()
        self.assertEqual(caught.exception.kind, "protocol")
        self.assertEqual(sum(r["body"]["method"] == "tools/list" for r in self.server.state["requests"]), 3)

    def test_tool_count_bounded(self):
        with mock.patch.object(mcp, "MAX_TOOLS", 2):
            with self.assertRaises(mcp.ClientError) as caught:
                self.client.tools()
        self.assertEqual(caught.exception.kind, "protocol")

    def test_failed_initialization_notification_resets_state(self):
        self.server.state["mode"] = "notification-error"
        with self.assertRaises(mcp.ClientError):
            self.client.call("create_note", {})
        self.assertFalse(self.client.initialized)
        self.assertIsNone(self.client.session_id)
        self.assertIsNone(self.client.connection)
        self.assertFalse(any(r["body"]["method"] == "tools/call" for r in self.server.state["requests"]))

    def test_duplicate_keys_rejected_before_any_batch_write(self):
        p, result = self.cli("batch", "--stdin", stdin='[{"tool":"create_note"},{"tool":"read_note","arguments":{"id":"first","id":"second"}}]')
        self.assertEqual(p.returncode, 2)
        self.assertEqual(result["error"]["kind"], "input")
        self.assertEqual(self.server.state["requests"], [])

    def test_deep_input_rejected_before_network_without_traceback(self):
        for depth in (mcp.MAX_JSON_DEPTH + 2, 1500):
            with self.subTest(depth=depth):
                payload = '{"nested":' + '[' * depth + '0' + ']' * depth + '}'
                p, result = self.cli("call", "create_note", "--stdin", stdin=payload)
                self.assertEqual(p.returncode, 2)
                self.assertEqual(result["error"]["kind"], "input")
                self.assertEqual(p.stderr, "")
                self.assertEqual(self.server.state["requests"], [])

    def test_escaped_tokens_and_overlapping_secrets_redacted(self):
        self.server.state["mode"] = "escaped-token"
        p, result = self.cli("call", "read_note")
        self.assertEqual(p.returncode, 1)
        self.assertIn("[REDACTED]", result["error"]["server_message"])
        self.assertNotIn("unit-token", p.stdout)
        self.assertEqual(mcp.redact("abcdef abc", ["abc", "abcdef"]), "[REDACTED] [REDACTED]")
        self.assertEqual(mcp.redact("\ud83d", ["\ud83d"]), "[REDACTED]")

    def test_invalid_selected_settings_do_not_fall_back_silently(self):
        for config in ({"url": False}, {"artifacts_dir": False}, {"timeout": 10 ** 400}):
            with self.subTest(config=config):
                path = Path(self.tmp.name) / "config.json"
                path.write_text(json.dumps(config), encoding="utf-8")
                p, result = self.cli("ping", "--config", str(path), env_update={"JOPLIN_MCP_URL": None})
                self.assertEqual(p.returncode, 2)
                self.assertEqual(result["error"]["kind"], "input")
        self.assertEqual(self.server.state["requests"], [])

    def test_invalid_urls_rejected_before_network(self):
        for suffix in (":0/mcp", ":41184/\npath", ":41184/中文", ":41184/mcp?x=a b"):
            with self.subTest(suffix=suffix):
                p, result = self.cli("ping", "--url", "http://127.0.0.1" + suffix)
                self.assertEqual(p.returncode, 2)
                self.assertEqual(result["error"]["kind"], "input")
        self.assertEqual(self.server.state["requests"], [])

    def test_surrogate_token_rejected_without_diagnostic_crash(self):
        path = Path(self.tmp.name) / "config.json"
        path.write_text('{"token":"\\ud83d"}', encoding="utf-8")
        p, result = self.cli("ping", "--config", str(path), env_update={"JOPLIN_TOKEN": None})
        self.assertEqual(p.returncode, 2)
        self.assertEqual(result["error"]["kind"], "input")
        self.assertEqual(p.stderr, "")
        self.assertEqual(self.server.state["requests"], [])

    def test_failed_image_write_removes_only_new_partial_file(self):
        path = Path(self.tmp.name) / "partial.png"
        writer = mcp.ResultWriter()
        stream = mock.MagicMock()
        stream.__enter__.return_value = stream
        stream.write.side_effect = OSError("disk full")
        real_fdopen = os.fdopen

        def broken_stream(fd, mode):
            wrapped = real_fdopen(fd, mode)
            stream.__exit__.side_effect = lambda *args: wrapped.close()
            return stream

        with mock.patch.object(mcp.os, "fdopen", side_effect=broken_stream):
            with self.assertRaises(mcp.ClientError) as caught:
                writer._save_image({"mimeType": "image/png", "data": base64.b64encode(PNG).decode()}, str(path))
        self.assertEqual(caught.exception.kind, "output")
        self.assertFalse(path.exists())

    def test_null_image_path_reports_local_output_failure(self):
        # OS argv cannot carry NUL; a JSON configuration can.
        path = Path(self.tmp.name) / "config.json"
        path.write_text(json.dumps({"artifacts_dir": self.tmp.name + '\0bad'}), encoding="utf-8")
        p, result = self.cli("call", "write_image", "--config", str(path))
        self.assertEqual(p.returncode, 1)
        self.assertEqual(result["error"]["kind"], "output")
        self.assertTrue(result["error"]["server_succeeded"])

    def test_huge_integer_rejected_consistently_before_network(self):
        payload = '{"limit":' + '9' * (mcp.MAX_INTEGER_DIGITS + 1) + '}'
        p, result = self.cli("call", "create_note", "--stdin", stdin=payload)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(result["error"]["kind"], "input")
        self.assertEqual(p.stderr, "")
        self.assertEqual(self.server.state["requests"], [])
        with self.assertRaises(mcp.ClientError):
            mcp.encode_json({"limit": mcp.MAX_JSON_INTEGER})

    def test_tool_error_after_partial_write_marks_unknown_without_retry(self):
        p, result = self.cli("call", "partial_failure")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(result["error"]["kind"], "tool")
        self.assertTrue(result["error"]["outcome_unknown"])
        self.assertEqual(self.server.state["mutation_count"], 1)
        self.assertEqual(sum(r["body"]["method"] == "tools/call" for r in self.server.state["requests"]), 1)

    def test_read_tool_error_has_no_write_uncertainty(self):
        self.server.state["mode"] = "tool-error"
        p, result = self.cli("call", "read_note")
        self.assertEqual(p.returncode, 1)
        self.assertEqual(result["error"]["kind"], "tool")
        self.assertNotIn("outcome_unknown", result["error"])

    def test_help_and_version_need_no_credentials(self):
        for arg in ("--help", "--version"):
            p = subprocess.run([sys.executable, str(SCRIPT), arg], text=True, capture_output=True, env={}, timeout=5)
            self.assertEqual(p.returncode, 0)
            self.assertIn("rick", p.stdout.lower())
        self.assertEqual(self.server.state["requests"], [])


if __name__ == "__main__":
    unittest.main()
