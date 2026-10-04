#!/usr/bin/env python3
"""Rick Joplin: dependency-free HTTP MCP client (Python 3.9+)."""

import argparse
import base64
import binascii
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

VERSION = "1.0.2"
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = frozenset((PROTOCOL_VERSION,))
DEFAULT_URL = "http://127.0.0.1:41184/mcp"
EXPECTED_TOOLS = (
    "search_notes", "semantic_search_notes", "read_note", "read_image",
    "list_notebooks", "list_tags", "create_note", "update_note",
    "delete_note", "manage_tags", "create_notebook",
)
READ_TOOLS = frozenset((
    "search_notes", "semantic_search_notes", "read_note", "read_image",
    "list_notebooks", "list_tags",
))
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_JSON_DEPTH = 100
MAX_INTEGER_DIGITS = 4300
MAX_JSON_INTEGER = 10 ** MAX_INTEGER_DIGITS
MAX_TOOL_PAGES = 100
MAX_TOOLS = 10000


class ClientError(Exception):
    def __init__(self, kind, message, **details):
        super().__init__(message)
        self.kind = kind
        self.details = details

    def as_dict(self):
        return {"kind": self.kind, "message": str(self), **self.details}


def redact(value, secrets=()):
    """Redact configured tokens and token query parameters from diagnostics."""
    if isinstance(value, str):
        variants = set()
        for secret in secrets:
            if isinstance(secret, str) and secret:
                variants.update((secret,
                                 urlencode({"token": secret}, errors="surrogatepass")[6:],
                                 json.dumps(secret, ensure_ascii=True)[1:-1],
                                 json.dumps(secret, ensure_ascii=False)[1:-1]))
        # Replace longer variants first when old and current tokens overlap.
        for variant in sorted(variants, key=len, reverse=True):
            value = value.replace(variant, "[REDACTED]")
        return re.sub(r"([?&]token=)[^&\s\"<>]+", r"\1[REDACTED]", value, flags=re.I)
    if isinstance(value, dict):
        return {redact(key, secrets): redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    return value


def reject_constant(value):
    raise ValueError("Non-standard JSON constant: " + value)


def finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number is outside the finite float range")
    return number


def bounded_int(value):
    if len(value.lstrip("-")) > MAX_INTEGER_DIGITS:
        raise ValueError("JSON integer exceeds the %s-digit limit" % MAX_INTEGER_DIGITS)
    return int(value)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON object keys are not allowed")
        result[key] = value
    return result


def check_json_depth(value):
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, (dict, list, tuple)):
            if depth > MAX_JSON_DEPTH:
                raise ValueError("JSON nesting exceeds the %s-level limit" % MAX_JSON_DEPTH)
            children = item.values() if isinstance(item, dict) else item
            pending.extend((child, depth + 1) for child in children)
        elif isinstance(item, int) and abs(item) >= MAX_JSON_INTEGER:
            raise ValueError("JSON integer exceeds the %s-digit limit" % MAX_INTEGER_DIGITS)


def json_loads(value):
    try:
        result = json.loads(value, parse_constant=reject_constant, parse_int=bounded_int,
                            parse_float=finite_float, object_pairs_hook=unique_object)
    except RecursionError:
        raise ValueError("JSON nesting is too deep") from None
    check_json_depth(result)
    return result


def encode_json(value):
    try:
        check_json_depth(value)
        return json.dumps(value, ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ClientError("input", "Request parameters are not valid JSON") from None


def dump_json(value):
    # Joplin's character paging can split a UTF-16 surrogate pair. Preserve
    # that code unit as a JSON escape without breaking UTF-8 stdout.
    return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8", errors="backslashreplace").decode("utf-8")


def read_json_file(path):
    try:
        return json_loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError, RuntimeError) as exc:
        raise ClientError("input", "Cannot read JSON file: %s" % exc) from None


def first_value(*values):
    return next((value for value in values if value is not None and value != ""), None)


def make_settings(args, environ=None, secrets=None):
    env = os.environ if environ is None else environ
    config = {}
    if getattr(args, "config", None):
        config = read_json_file(args.config)
        if not isinstance(config, dict):
            raise ClientError("input", "Configuration must be a JSON object")
        if secrets is not None and isinstance(config.get("token"), str):
            secrets.append(config["token"])
        unknown = set(config) - {"url", "token", "timeout", "artifacts_dir"}
        if unknown:
            raise ClientError("input", "Unknown configuration keys: " + ", ".join(sorted(unknown)))
    url = first_value(getattr(args, "url", None), env.get("JOPLIN_MCP_URL"),
                      config.get("url"), env.get("JOPLIN_URL"))
    if url is None:
        url = DEFAULT_URL
    if not isinstance(url, str):
        raise ClientError("input", "MCP URL must be a string")
    if re.search(r"[\x00-\x20\x7f]", url):
        raise ClientError("input", "MCP URL must not contain whitespace or control characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ClientError("input", "Invalid MCP URL or port") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ClientError("input", "MCP URL must use http:// or https:// with a hostname")
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise ClientError("input", "Do not use URL userinfo or fragments in the MCP URL")
    if port == 0:
        raise ClientError("input", "MCP URL port must be between 1 and 65535")
    if not parts.path.isascii():
        raise ClientError("input", "Non-ASCII URL paths must be percent-encoded")
    query = parse_qsl(parts.query, keep_blank_values=True)
    url_tokens = [v for k, v in query if k == "token"]
    if secrets is not None:
        secrets.extend(url_tokens)
    token = first_value(env.get("JOPLIN_TOKEN"), config.get("token"),
                        url_tokens[-1] if url_tokens else None)
    if not isinstance(token, str) or not token:
        raise ClientError("configuration", "JOPLIN_TOKEN is missing or invalid. Export the Web Clipper token in this process, or use --config with a token.")
    try:
        token.encode("utf-8")
    except UnicodeError:
        raise ClientError("input", "Token must be valid UTF-8 text") from None
    path = parts.path.rstrip("/")
    if not path.endswith("/mcp"):
        path += "/mcp"
    path = path or "/mcp"
    query = [(k, v) for k, v in query if k != "token"] + [("token", token)]
    target = path + "?" + urlencode(query)
    endpoint = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    timeout = getattr(args, "timeout", None)
    if timeout is None:
        timeout = config.get("timeout", 30)
    try:
        if isinstance(timeout, bool):
            raise ValueError()
        timeout = float(timeout)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError()
    except (TypeError, ValueError, OverflowError):
        raise ClientError("input", "Timeout must be a finite positive number") from None
    artifacts_dir = first_value(getattr(args, "artifacts_dir", None), config.get("artifacts_dir"))
    if artifacts_dir is not None and not isinstance(artifacts_dir, str):
        raise ClientError("input", "artifacts_dir must be a path string")
    return {
        "scheme": parts.scheme, "host": parts.hostname,
        "port": port or (443 if parts.scheme == "https" else 80),
        "target": target, "endpoint": endpoint, "token": token,
        "timeout": timeout, "artifacts_dir": artifacts_dir,
    }


class McpClient:
    """One connection and initialization per process; no automatic retries."""
    def __init__(self, settings):
        self.settings = settings
        self.connection = None
        self.initialized = False
        self.counter = 0
        self.protocol_version = PROTOCOL_VERSION
        self.session_id = None
        self.server_info = {}
        self._tools = None

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def _connection(self):
        if self.connection is None:
            cls = http.client.HTTPSConnection if self.settings["scheme"] == "https" else http.client.HTTPConnection
            self.connection = cls(self.settings["host"], self.settings["port"], timeout=self.settings["timeout"])
        return self.connection

    def rpc(self, method, params=None, notification=False):
        self.counter += 1
        rid = self.counter
        payload = {"jsonrpc": "2.0", "method": method}
        if not notification:
            payload["id"] = rid
        if params is not None:
            payload["params"] = params
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.initialized:
            headers["MCP-Protocol-Version"] = self.protocol_version
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        encoded = encode_json(payload)
        try:
            conn = self._connection()
            conn.request("POST", self.settings["target"], encoded, headers)
            response = conn.getresponse()
            status = response.status
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            session = response.getheader("Mcp-Session-Id")
            if session:
                if not all(0x21 <= ord(char) <= 0x7e for char in session):
                    self.close()
                    raise ClientError("protocol", "Invalid MCP session id header")
                self.session_id = session
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                self.close()
                raise ClientError("protocol", "MCP response exceeds the 32 MiB limit")
            if response.length not in (None, 0):
                self.close()
                raise ClientError("protocol", "MCP HTTP response body was truncated")
        except (socket.timeout, TimeoutError) as exc:
            self.close()
            raise ClientError("transport", "MCP request timed out; it was not retried", reason=type(exc).__name__) from None
        except (OSError, http.client.HTTPException) as exc:
            self.close()
            raise ClientError("transport", "MCP connection failed; it was not retried", reason=str(exc)) from None
        if not 200 <= status < 300:
            message = {
                401: "Authentication failed: check JOPLIN_TOKEN",
                403: "Access refused: check the token and Settings > AI > Enable MCP server",
                404: "MCP endpoint not found: check the URL and Joplin version",
            }.get(status, "MCP HTTP request failed")
            if 300 <= status < 400:
                message = "MCP redirect refused; configure the final endpoint explicitly"
            details = {"http_status": status}
            if data:
                # Redact before truncation so a token crossing the boundary
                # cannot leave a partial credential in diagnostics.
                details["server_message"] = redact(data.decode("utf-8", errors="replace"), (self.settings["token"],))[:2000]
            raise ClientError("http", message, **details)
        if notification:
            return None
        if not data:
            raise ClientError("protocol", "Empty response to a JSON-RPC request")
        if content_type and content_type != "application/json":
            raise ClientError("protocol", "Expected Joplin's JSON HTTP response", content_type=content_type)
        try:
            result = json_loads(data)
        except (ValueError, UnicodeError):
            raise ClientError("protocol", "MCP returned invalid JSON") from None
        if (not isinstance(result, dict) or result.get("jsonrpc") != "2.0"
                or type(result.get("id")) is not int or result["id"] != rid):
            raise ClientError("protocol", "Invalid JSON-RPC response or mismatched request id")
        if ("result" in result) == ("error" in result):
            raise ClientError("protocol", "JSON-RPC response must contain exactly one of result or error")
        if "error" in result:
            error = result["error"]
            if (not isinstance(error, dict) or type(error.get("code")) is not int
                    or not isinstance(error.get("message"), str)):
                raise ClientError("protocol", "Invalid JSON-RPC error object")
            raise ClientError("rpc", error["message"], rpc_code=error["code"], rpc_data=error.get("data"))
        return result["result"]

    def initialize(self):
        if self.initialized:
            return
        info = self.rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "rick-joplin", "version": VERSION},
        })
        if not isinstance(info, dict) or not isinstance(info.get("protocolVersion"), str):
            raise ClientError("protocol", "Invalid MCP initialization response")
        if info["protocolVersion"] not in SUPPORTED_PROTOCOL_VERSIONS:
            raise ClientError("protocol", "Server selected an unsupported MCP protocol version",
                              protocol_version=info["protocolVersion"],
                              supported_versions=sorted(SUPPORTED_PROTOCOL_VERSIONS))
        capabilities = info.get("capabilities", {})
        if not isinstance(capabilities, dict) or not isinstance(capabilities.get("tools"), dict):
            raise ClientError("protocol", "Server does not advertise valid MCP tools capabilities")
        server_info = info.get("serverInfo")
        if (not isinstance(server_info, dict)
                or not isinstance(server_info.get("name"), str)
                or not isinstance(server_info.get("version"), str)):
            raise ClientError("protocol", "Invalid MCP server info")
        self.protocol_version = info["protocolVersion"]
        self.server_info = server_info
        self.initialized = True
        try:
            self.rpc("notifications/initialized", notification=True)
        except ClientError:
            self.initialized = False
            self.session_id = None
            self.close()
            raise

    def tools(self):
        self.initialize()
        if self._tools is None:
            tools = []
            cursor = None
            cursors = set()
            names = set()
            for _ in range(MAX_TOOL_PAGES):
                page = self.rpc("tools/list", {"cursor": cursor} if cursor else {})
                if not isinstance(page, dict) or not isinstance(page.get("tools"), list):
                    raise ClientError("protocol", "Invalid tools/list response")
                for tool in page["tools"]:
                    if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                            or not tool["name"].strip()
                            or ("description" in tool and not isinstance(tool["description"], str))
                            or not isinstance(tool.get("inputSchema"), dict)
                            or tool["inputSchema"].get("type") != "object"):
                        raise ClientError("protocol", "Invalid MCP tool definition")
                    if tool["name"] in names:
                        raise ClientError("protocol", "Duplicate MCP tool name")
                    names.add(tool["name"])
                    tools.append(tool)
                    if len(tools) > MAX_TOOLS:
                        raise ClientError("protocol", "MCP tool count exceeds the configured limit")
                cursor = page.get("nextCursor")
                if cursor is None:
                    break
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    raise ClientError("protocol", "Invalid or repeated tools/list cursor")
                cursors.add(cursor)
            else:
                raise ClientError("protocol", "MCP tool pagination exceeds the configured limit")
            self._tools = tools
        return self._tools

    def call(self, name, arguments):
        if not isinstance(name, str) or not name.strip() or not isinstance(arguments, dict):
            raise ClientError("input", "Tool name must be non-empty and arguments must be a JSON object")
        self.initialize()
        try:
            result = self.rpc("tools/call", {"name": name, "arguments": arguments})
            validate_tool_result(result)
        except ClientError as exc:
            uncertain = (exc.kind in ("transport", "protocol")
                         or (exc.kind == "http" and (exc.details.get("http_status", 0) >= 500
                                                   or exc.details.get("http_status") == 408))
                         or (exc.kind == "rpc" and exc.details.get("rpc_code")
                             not in (-32700, -32600, -32601, -32602)))
            if name not in READ_TOOLS and uncertain:
                exc.details["outcome_unknown"] = True
                exc.details["tool"] = name
            raise
        if result.get("isError"):
            texts = [item["text"] for item in result["content"] if item["type"] == "text"]
            details = {"tool": name}
            # A tool error can occur after a partial mutation. MCP does not
            # promise rollback, even when an error response arrives cleanly.
            if name not in READ_TOOLS:
                details["outcome_unknown"] = True
            raise ClientError("tool", "\n".join(texts) or "MCP tool failed", **details)
        return result


def validate_tool_result(result):
    if (not isinstance(result, dict) or not isinstance(result.get("content"), list)
            or ("isError" in result and not isinstance(result["isError"], bool))
            or ("structuredContent" in result and not isinstance(result["structuredContent"], dict))):
        raise ClientError("protocol", "Invalid tools/call result")
    for item in result["content"]:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str) or not item["type"]:
            raise ClientError("protocol", "Invalid MCP content item")
        if item["type"] == "text" and not isinstance(item.get("text"), str):
            raise ClientError("protocol", "Invalid text content")
        if item["type"] == "image" and (not isinstance(item.get("mimeType"), str)
                                        or not isinstance(item.get("data"), str)):
            raise ClientError("protocol", "Invalid image content")


def input_arguments(args):
    if getattr(args, "args_file", None):
        value = read_json_file(args.args_file)
    elif getattr(args, "stdin", False):
        try:
            value = json_loads(sys.stdin.read())
        except (ValueError, UnicodeError) as exc:
            raise ClientError("input", "stdin must contain valid JSON: %s" % exc) from None
    else:
        try:
            value = json_loads(getattr(args, "args", None) or "{}")
        except ValueError as exc:
            raise ClientError("input", "--args must contain valid JSON: %s" % exc) from None
    if not isinstance(value, dict):
        raise ClientError("input", "Tool arguments must be a JSON object")
    return value


class ResultWriter:
    def __init__(self, artifacts_dir=None):
        self.artifacts_dir = artifacts_dir
        self.directory = None

    def _save_image(self, item, output=None):
        mime = item.get("mimeType")
        extensions = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/webp": ".webp", "image/gif": ".gif", "image/svg+xml": ".svg", "image/bmp": ".bmp"}
        if not isinstance(mime, str) or mime not in extensions or not isinstance(item.get("data"), str):
            raise ClientError("protocol", "Unsupported or invalid image content", mime_type=mime)
        try:
            data = base64.b64decode(item["data"], validate=True)
        except (binascii.Error, ValueError):
            raise ClientError("protocol", "Invalid Base64 image data") from None
        if not data:
            raise ClientError("protocol", "Image data is empty")
        fd = None
        created_path = None
        try:
            if output:
                path = Path(output).expanduser().absolute()
                path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            else:
                if self.directory is None:
                    if self.artifacts_dir:
                        self.directory = Path(self.artifacts_dir).expanduser().absolute()
                        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                    else:
                        self.directory = Path(tempfile.mkdtemp(prefix="rick-joplin-images-"))
                fd, filename = tempfile.mkstemp(prefix="image-", suffix=extensions[mime], dir=str(self.directory))
                path = Path(filename)
            created_path = path
            stream = os.fdopen(fd, "wb")
            fd = None  # The stream now owns the descriptor.
            with stream:
                stream.write(data)
        except (OSError, ValueError, RuntimeError) as exc:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if created_path is not None:
                try:
                    created_path.unlink()
                except OSError:
                    pass
            raise ClientError("output", "Cannot save image (existing files are never overwritten): %s" % exc) from None
        return {"type": "image", "path": str(path), "mime_type": mime, "bytes": len(data)}

    def format(self, tool, result, raw=False, output=None):
        validate_tool_result(result)
        if raw:
            return {"ok": True, "tool": tool, "result": result}
        artifacts = []
        values = []
        image_count = sum(isinstance(item, dict) and item.get("type") == "image" for item in result["content"])
        if output and image_count != 1:
            raise ClientError("output", "--output requires exactly one image in the result")
        for item in result["content"]:
            if not isinstance(item, dict):
                raise ClientError("protocol", "Invalid MCP content item")
            if item.get("type") == "text":
                text = item.get("text", "")
                if not isinstance(text, str):
                    raise ClientError("protocol", "Invalid text content")
                try:
                    values.append(json_loads(text))
                except ValueError:
                    values.append(text)
            elif item.get("type") == "image":
                artifact = self._save_image(item, output)
                artifacts.append(artifact)
                values.append(artifact)
            else:
                # Preserve new content types rather than silently discarding them.
                values.append(item)
        data = values[0] if len(values) == 1 else values
        formatted = {"ok": True, "tool": tool, "data": data, "artifacts": artifacts}
        metadata = {k: v for k, v in result.items() if k not in ("content", "isError")}
        if metadata:
            formatted["mcp_metadata"] = metadata
        return formatted


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ClientError("input", message)


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    for flag, kwargs in (
        ("--url", {"help": "MCP endpoint; also accepts a Web Clipper base URL"}),
        ("--config", {"help": "JSON configuration file"}),
        ("--timeout", {"type": float, "help": "Request timeout in seconds (default 30)"}),
        ("--artifacts-dir", {"help": "Directory for decoded images"}),
        ("--raw", {"action": "store_true", "help": "Return raw tool results, including Base64 images"}),
    ):
        common.add_argument(flag, default=argparse.SUPPRESS, **kwargs)
    parser = Parser(description=__doc__, parents=[common])
    parser.add_argument("--version", action="version", version="rick-joplin " + VERSION)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    sub.add_parser("doctor", parents=[common], help="Check MCP connectivity, initialization and tools")
    sub.add_parser("ping", parents=[common], help="Initialize and ping MCP")
    tools = sub.add_parser("tools", parents=[common], help="Discover all tools")
    tools.add_argument("--full", action="store_true", help="Include full descriptions and input schemas")
    describe = sub.add_parser("describe", parents=[common], help="Get one tool's complete definition")
    describe.add_argument("tool")
    call = sub.add_parser("call", parents=[common], help="Call any MCP tool by name")
    call.add_argument("tool")
    source = call.add_mutually_exclusive_group()
    source.add_argument("--args", help="JSON object (default {})")
    source.add_argument("--args-file", help="Read a JSON object from a file")
    source.add_argument("--stdin", action="store_true", help="Read a JSON object from stdin")
    image = sub.add_parser("image", parents=[common], help="Call read_image and save the image")
    image.add_argument("id", help="Joplin image resource ID")
    image.add_argument("--resolution", choices=("low", "medium", "high"), default="medium")
    image.add_argument("--output", help="Save to this new file; never overwrite")
    batch = sub.add_parser("batch", parents=[common], help="Run calls in one initialized process")
    source = batch.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", help="JSON array of {tool, arguments, optional label}")
    source.add_argument("--stdin", action="store_true", help="Read the array from stdin")
    batch.add_argument("--continue-on-error", action="store_true", help="Run later calls after a failure")
    return parser


def load_batch(args):
    if args.file:
        calls = read_json_file(args.file)
    else:
        try:
            calls = json_loads(sys.stdin.read())
        except (ValueError, UnicodeError) as exc:
            raise ClientError("input", "Batch stdin must contain valid JSON: %s" % exc) from None
    if not isinstance(calls, list) or not calls:
        raise ClientError("input", "Batch input must be a non-empty JSON array")
    # Validate all items before any call is dispatched, including writes.
    for i, call in enumerate(calls):
        if not isinstance(call, dict) or set(call) - {"tool", "arguments", "label"}:
            raise ClientError("input", "Invalid batch item", index=i)
        if not isinstance(call.get("tool"), str) or not call["tool"].strip():
            raise ClientError("input", "Batch tool must be a non-empty string", index=i)
        if not isinstance(call.get("arguments", {}), dict):
            raise ClientError("input", "Batch arguments must be an object", index=i)
        if "label" in call and not isinstance(call["label"], str):
            raise ClientError("input", "Batch label must be a string", index=i)
        try:
            encode_json(call.get("arguments", {}))
        except ClientError as exc:
            exc.details["index"] = i
            raise
    return calls


def call_and_format(client, writer, tool, arguments, raw=False, output=None):
    result = client.call(tool, arguments)
    try:
        return writer.format(tool, result, raw, output)
    except ClientError as exc:
        exc.details["tool"] = tool
        if tool not in READ_TOOLS:
            if exc.kind == "protocol":
                exc.details["outcome_unknown"] = True
            else:
                # A valid successful tool response arrived; only local output
                # handling failed. Repeating the write would duplicate it.
                exc.details["server_succeeded"] = True
        raise


def run(args, client, writer, prepared=None):
    raw = getattr(args, "raw", False)
    if args.command == "doctor":
        client.initialize()
        client.rpc("ping", {})
        tools = client.tools()
        names = [t["name"] for t in tools]
        disabled = [t["name"] for t in tools if "(Disabled tool)" in t.get("description", "")]
        return {"ok": True, "data": {"endpoint": client.settings["endpoint"], "server": client.server_info,
                "protocol_version": client.protocol_version, "tool_count": len(tools),
                "enabled_tools": [n for n in names if n not in disabled], "disabled_tools": disabled,
                "missing_expected_tools": [n for n in EXPECTED_TOOLS if n not in names]}}, 0
    if args.command == "ping":
        client.initialize()
        return {"ok": True, "data": client.rpc("ping", {})}, 0
    if args.command == "tools":
        tools = client.tools()
        if not args.full:
            tools = [{"name": t["name"], "disabled": "(Disabled tool)" in t.get("description", ""),
                      "description": t.get("description", "")[:300]} for t in tools]
        return {"ok": True, "data": {"tools": tools, "total": len(tools)}}, 0
    if args.command == "describe":
        tool = next((t for t in client.tools() if t["name"] == args.tool), None)
        if tool is None:
            raise ClientError("input", "Tool not found in tools/list", tool=args.tool)
        return {"ok": True, "data": tool}, 0
    if args.command == "call":
        return call_and_format(client, writer, args.tool, prepared, raw), 0
    if args.command == "image":
        if raw and args.output:
            raise ClientError("input", "--raw cannot be combined with image --output")
        return call_and_format(client, writer, "read_image", {"id": args.id, "resolution": args.resolution}, raw, args.output), 0
    if args.command == "batch":
        results = []
        failed = 0
        for i, call in enumerate(prepared):
            try:
                result = call_and_format(client, writer, call["tool"], call.get("arguments", {}), raw)
            except ClientError as exc:
                result = {"ok": False, "tool": call["tool"], "error": exc.as_dict()}
                failed += 1
            result["index"] = i
            if "label" in call:
                result["label"] = call["label"]
            results.append(result)
            if not result["ok"] and not args.continue_on_error:
                break
        return {"ok": failed == 0, "results": results, "summary": {
            "requested": len(prepared), "completed": len(results) - failed, "failed": failed,
            "skipped": len(prepared) - len(results), "transactional": False,
        }}, 1 if failed else 0
    raise ClientError("input", "Unknown command")


def main(argv=None):
    client = None
    secrets = [os.environ.get("JOPLIN_TOKEN", "")]
    try:
        # JSON pipes use UTF-8 on every platform, including redirected Windows
        # streams. Leave file-like objects used by embedders untouched.
        for stream in (sys.stdin, sys.stdout):
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8")
        args = build_parser().parse_args(argv)
        prepared = input_arguments(args) if args.command == "call" else load_batch(args) if args.command == "batch" else None
        settings = make_settings(args, secrets=secrets)
        secrets.append(settings["token"])
        client = McpClient(settings)
        writer = ResultWriter(settings["artifacts_dir"])
        result, status = run(args, client, writer, prepared)
        # Errors nested in batch results must also redact credentials.
        if args.command == "batch":
            for item in result["results"]:
                if not item["ok"]:
                    item["error"] = redact(item["error"], secrets)
        print(dump_json(result))
        return status
    except ClientError as exc:
        print(dump_json({"ok": False, "error": redact(exc.as_dict(), secrets)}))
        return 2 if exc.kind in ("input", "configuration") else 1
    except (OSError, UnicodeError, ValueError) as exc:
        print(dump_json({"ok": False, "error": redact({"kind": "local", "message": str(exc)}, secrets)}))
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
