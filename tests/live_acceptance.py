#!/usr/bin/env python3
"""Explicit live acceptance: all 11 Joplin MCP tools, isolated test writes."""
import argparse
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "joplin_mcp.py"
spec = importlib.util.spec_from_file_location("rick_joplin_live", SCRIPT)
mcp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mcp)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="JSON acceptance report (no note bodies/tokens)")
    parser.add_argument("--url")
    args = parser.parse_args()
    settings_args = mcp.build_parser().parse_args(["doctor"] + (["--url", args.url] if args.url else []))
    settings = mcp.make_settings(settings_args)
    client = mcp.McpClient(settings)
    report_fd = None
    try:
        target = Path(args.output).expanduser().absolute()
        target.parent.mkdir(parents=True, exist_ok=True)
        report_fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        report_stream = os.fdopen(report_fd, "w", encoding="utf-8")
    except (OSError, ValueError, RuntimeError) as exc:
        if report_fd is not None:
            try:
                os.close(report_fd)
            except OSError:
                pass
            target.unlink(missing_ok=True)
        print(mcp.dump_json({"ok": False, "error": "Cannot create a new acceptance report: " + str(exc)}))
        client.close()
        return 2
    marker = "rick-joplin-" + uuid.uuid4().hex[:10]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tag_title = marker + "-tag"
    owned_notes = []
    folders = []
    tools_passed = set()
    errors = []
    report = {
        "started_utc": datetime.now(timezone.utc).isoformat(), "python": sys.version.split()[0],
        "client_version": mcp.VERSION, "endpoint": settings["endpoint"],
        "steps": [], "cleanup": {},
    }
    temporary = tempfile.TemporaryDirectory(prefix="rick-joplin-live-")
    writer = mcp.ResultWriter(temporary.name)

    def call(step, tool, arguments, predicate=None):
        start = time.perf_counter()
        try:
            formatted = writer.format(tool, client.call(tool, arguments))
            data = formatted["data"]
            # Record returned IDs before assertions so an unexpected response
            # cannot bypass cleanup of an already-created test item.
            if tool == "create_note" and isinstance(data, dict) and isinstance(data.get("id"), str):
                owned_notes.append(data["id"])
            elif tool == "create_notebook" and isinstance(data, dict) and isinstance(data.get("id"), str):
                folders.append(data)
            if predicate:
                require(predicate(data), "Unexpected result for " + step)
            tools_passed.add(tool)
            report["steps"].append({"name": step, "tool": tool, "ok": True, "elapsed_ms": round((time.perf_counter() - start) * 1000, 2)})
            return data
        except Exception as exc:
            detail = mcp.redact(str(exc), [settings["token"]])
            report["steps"].append({"name": step, "tool": tool, "ok": False, "error": detail})
            raise

    def cli(step, arguments, stdin=None, check=None):
        start = time.perf_counter()
        env = os.environ.copy()
        env["JOPLIN_TOKEN"] = settings["token"]
        env["JOPLIN_MCP_URL"] = settings["endpoint"]
        proc = subprocess.run([sys.executable, str(SCRIPT), *arguments], input=stdin, text=True, encoding="utf-8", capture_output=True, env=env, timeout=40)
        result = json.loads(proc.stdout)
        require(proc.returncode == 0 and result.get("ok"), "CLI failed for " + step)
        if check:
            require(check(result), "Unexpected CLI result for " + step)
        report["steps"].append({"name": step, "ok": True, "elapsed_ms": round((time.perf_counter() - start) * 1000, 2)})
        return result

    try:
        cli("CLI doctor", ["doctor"], check=lambda r: r["data"]["missing_expected_tools"] == [] and r["data"]["disabled_tools"] == [])
        discovered = client.tools()
        require(set(mcp.EXPECTED_TOOLS) <= {t["name"] for t in discovered}, "Missing expected tools")
        report["server"] = client.server_info
        report["protocol_version"] = client.protocol_version
        report["discovered_tools"] = [t["name"] for t in discovered]
        call("List notebooks", "list_notebooks", {}, lambda d: isinstance(d.get("notebooks"), list))
        root = call("Create isolated test notebook", "create_notebook", {"title": "rick-joplin 验收 " + stamp + " " + marker}, lambda d: bool(d.get("id")))
        child = call("Create nested test notebook", "create_notebook", {"title": "子笔记本", "parent_id": root["id"]}, lambda d: d.get("parent_id") == root["id"])
        body = "alpha\n中文 😀\n唯一原文\nrepeat repeat\n"
        note = call("Create note", "create_note", {"title": marker + " normal", "body": body, "notebook_id": root["id"]}, lambda d: d.get("notebook_id") == root["id"])
        todo = call("Create to-do", "create_note", {"title": marker + " todo", "body": "待办验收", "notebook_id": root["id"], "is_todo": True}, lambda d: bool(d.get("id")))
        call("Read full note", "read_note", {"id": note["id"]}, lambda d: d.get("body") == body and d.get("notebook_id") == root["id"] and "tags" in d and "updated_time" in d)
        call("Read first segment", "read_note", {"id": note["id"], "offset": 0, "max_chars": 5}, lambda d: d.get("body") == "alpha" and d.get("has_more"))
        call("Read next segment", "read_note", {"id": note["id"], "offset": 5, "max_chars": 1}, lambda d: d.get("body") == "\n")
        # Deliberately split the emoji's UTF-16 pair through the actual CLI.
        cli("Read UTF-16 surrogate segment", ["call", "read_note", "--args", json.dumps({"id": note["id"], "offset": 9, "max_chars": 1})], check=lambda r: r["data"]["body"] == "\ud83d")
        # The desktop search index is updated asynchronously after Note.save.
        search_start = time.perf_counter()
        search_attempts = 0
        while True:
            search_attempts += 1
            searched = writer.format("search_notes", client.call("search_notes", {"query": 'title:"' + marker + '"', "limit": 100}))["data"]
            if note["id"] in {r["id"] for r in searched.get("results", [])}:
                tools_passed.add("search_notes")
                report["steps"].append({"name": "Keyword search after index refresh", "tool": "search_notes", "ok": True, "attempts": search_attempts, "elapsed_ms": round((time.perf_counter() - search_start) * 1000, 2)})
                break
            require(time.perf_counter() - search_start < 30, "New test note was not found by keyword search after 30 seconds")
            time.sleep(0.5)
        call("Append and prepend", "update_note", {"id": note["id"], "append": "后缀", "prepend": "前缀\n"})
        call("Verify append and prepend", "read_note", {"id": note["id"]}, lambda d: d.get("body") == "前缀\n" + body + "后缀")
        call("Unique replace", "update_note", {"id": note["id"], "replace_text": {"find": "唯一原文", "replace": "唯一新文"}})
        call("Verify unique replace", "read_note", {"id": note["id"]}, lambda d: "唯一新文" in d.get("body", "") and "唯一原文" not in d.get("body", ""))
        try:
            client.call("update_note", {"id": note["id"], "replace_text": {"find": "repeat", "replace": "bad"}})
        except mcp.ClientError as exc:
            require(exc.kind == "tool", "Ambiguous replace must be a tool error")
            report["steps"].append({"name": "Ambiguous replacement rejected", "ok": True})
        else:
            raise AssertionError("Ambiguous replacement was unexpectedly accepted")
        call("Move and rename note", "update_note", {"id": note["id"], "notebook_id": child["id"], "title": marker + " moved"})
        call("Verify move", "read_note", {"id": note["id"]}, lambda d: d.get("notebook_id") == child["id"] and d.get("title") == marker + " moved")
        replacement = "完整重写 中文\n包含引号 ' \" 与代码 $HOME `literal` 😀"
        input_file = Path(temporary.name) / "update.json"
        input_file.write_text(json.dumps({"id": note["id"], "body": replacement}, ensure_ascii=False), encoding="utf-8")
        cli("CLI JSON file update", ["call", "update_note", "--args-file", str(input_file)])
        call("Verify full rewrite", "read_note", {"id": note["id"]}, lambda d: d.get("body") == replacement)
        cli("CLI stdin update", ["call", "update_note", "--stdin"], stdin=json.dumps({"id": note["id"], "append": "\nstdin追加"}, ensure_ascii=False))
        call("Verify stdin update", "read_note", {"id": note["id"]}, lambda d: d.get("body") == replacement + "\nstdin追加")
        call("Complete to-do", "update_note", {"id": todo["id"], "todo_completed": True})
        call("Verify completed to-do", "read_note", {"id": todo["id"]}, lambda d: d.get("is_todo") and d.get("todo_completed"))
        call("Reopen to-do", "update_note", {"id": todo["id"], "todo_completed": False})
        call("Verify reopened to-do", "read_note", {"id": todo["id"]}, lambda d: d.get("is_todo") and not d.get("todo_completed"))
        call("Add isolated tag", "manage_tags", {"note_id": note["id"], "add": [tag_title]}, lambda d: tag_title in d.get("tags", []))
        call("List tags", "list_tags", {}, lambda d: tag_title in {t["title"] for t in d.get("tags", [])})
        call("Verify note tags", "read_note", {"id": note["id"]}, lambda d: tag_title in d.get("tags", []))
        call("Remove isolated tag", "manage_tags", {"note_id": note["id"], "remove": [tag_title]}, lambda d: tag_title not in d.get("tags", []))
        semantic = call("Semantic search using existing index", "semantic_search_notes", {"query": "服务性能优化", "relevance": "normal"}, lambda d: bool(d.get("results")) and all("score" in r and "chunk_text" in r and "note_id" in r for r in d["results"]))
        # Existing notes are only read. Their content is not included in the report.
        folder_id = semantic["results"][0]["notebook_id"]
        call("Notebook-scoped semantic search", "semantic_search_notes", {"query": "服务性能优化", "notebook_id": folder_id, "relevance": "normal"}, lambda d: bool(d.get("results")) and all(r["notebook_id"] == folder_id for r in d["results"]))
        candidates = call("Find existing image note", "search_notes", {"query": "resource:image/png", "limit": 10}, lambda d: bool(d.get("results")))
        image_id = None
        for candidate in candidates["results"]:
            existing = call("Read existing image reference", "read_note", {"id": candidate["id"]})
            body_text = existing.get("body", "")
            patterns = (r'!\[[^\]]*\]\(:/([a-f0-9]{32})', r'<img\b[^>]*src=[\"\x27]:/([a-f0-9]{32})')
            for pattern in patterns:
                match = re.search(pattern, body_text, re.I)
                if match:
                    image_id = match.group(1)
                    break
            if image_id:
                break
        require(image_id, "No existing image reference found for read_image acceptance")
        image = call("Read actual image", "read_image", {"id": image_id, "resolution": "high"}, lambda d: d.get("type") == "image" and d.get("bytes", 0) > 0)
        content = Path(image["path"]).read_bytes()
        require(content.startswith((b"\x89PNG", b"\xff\xd8\xff", b"RIFF", b"GIF")), "Unexpected image file signature")
        image_cli_path = Path(temporary.name) / "cli-image.png"
        cli("CLI image output", ["image", image_id, "--output", str(image_cli_path)], check=lambda r: Path(r["artifacts"][0]["path"]).is_file())
        batch_calls = [{"tool": "read_note", "arguments": {"id": note["id"]}}, {"tool": "list_tags", "arguments": {}}]
        cli("CLI batch", ["batch", "--stdin"], stdin=json.dumps(batch_calls), check=lambda r: r["summary"]["completed"] == 2 and r["summary"]["failed"] == 0)
        call("Verify test notebook counts", "list_notebooks", {}, lambda d: any(f["id"] == child["id"] and f["note_count"] >= 1 for f in d["notebooks"]))
    except Exception as exc:
        errors.append(mcp.redact(str(exc), [settings["token"]]))
    finally:
        trashed = []
        cleanup_errors = []
        owned_folder_ids = {f["id"] for f in folders}
        for note_id in owned_notes:
            try:
                existing = writer.format("read_note", client.call("read_note", {"id": note_id}))["data"]
                require(isinstance(existing.get("title"), str) and existing["title"].startswith(marker + " ")
                        and existing.get("notebook_id") in owned_folder_ids,
                        "Refusing to clean a note without confirmed test ownership")
            except Exception as exc:
                cleanup_errors.append(mcp.redact(str(exc), [settings["token"]]))
                continue
            try:
                client.call("manage_tags", {"note_id": note_id, "remove": [tag_title]})
            except Exception as exc:
                # Tag cleanup failure must not prevent trashing our test note.
                cleanup_errors.append(mcp.redact(str(exc), [settings["token"]]))
            try:
                call("Trash own test note", "delete_note", {"id": note_id}, lambda d: d.get("trashed") is True)
                trashed.append(note_id)
                try:
                    client.call("read_note", {"id": note_id})
                except mcp.ClientError as exc:
                    require(exc.kind == "tool", "Trashed note must be excluded from read_note")
                else:
                    raise AssertionError("Trashed test note still readable via read_note")
            except Exception as exc:
                cleanup_errors.append(mcp.redact(str(exc), [settings["token"]]))
        report["cleanup"] = {"test_notes_trashed": trashed, "empty_notebooks_retained": folders, "reason": "Official MCP has no delete_notebook tool", "errors": cleanup_errors}
        if folders:
            try:
                final = writer.format("list_notebooks", client.call("list_notebooks", {}))["data"]
                owned_folder_ids = {f["id"] for f in folders}
                counts = {f["id"]: f["note_count"] for f in final["notebooks"] if f["id"] in owned_folder_ids}
                report["cleanup"]["remaining_note_counts"] = counts
                require(set(counts) == owned_folder_ids and all(count == 0 for count in counts.values()),
                        "Test notebooks are missing or not empty after cleanup")
            except Exception as exc:
                cleanup_errors.append(mcp.redact(str(exc), [settings["token"]]))
        report["passed_tools"] = sorted(tools_passed)
        report["missing_tool_acceptance"] = sorted(set(mcp.EXPECTED_TOOLS) - tools_passed)
        report["errors"] = errors
        report["ok"] = not errors and not cleanup_errors and not report["missing_tool_acceptance"]
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        try:
            report_stream.write(mcp.dump_json(report) + "\n")
            report_stream.flush()
            print(mcp.dump_json({"ok": report["ok"], "report": str(target), "passed_tool_count": len(tools_passed), "step_count": len(report["steps"]), "errors": errors, "cleanup_errors": cleanup_errors}))
        except (OSError, ValueError) as exc:
            report["ok"] = False
            print(mcp.dump_json({"ok": False, "error": "Cannot write acceptance report: " + str(exc)}))
        finally:
            try:
                report_stream.close()
            finally:
                try:
                    client.close()
                finally:
                    temporary.cleanup()
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
