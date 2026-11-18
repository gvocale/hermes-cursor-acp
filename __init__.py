"""Cursor subscription model provider for Hermes, via the Cursor CLI ACP server.

Cursor sells its models through an agent harness, not a chat-completions API, so this
plugin reuses Hermes' built-in ACP shim (the same one ``copilot-acp`` uses) against
``agent acp``. Hermes' tools are bridged as ``<tool_call>`` text and executed by Hermes;
Cursor's own permission requests are cancelled by the shim. When a model reaches for a
native Cursor tool anyway, the fence refuses it and the same action is rerouted to the
matching Hermes tool, so the user is not told to "fix permissions".

Isolation: Cursor runs in an empty private workspace and is switched to ``ask`` mode
(no edits, no command execution) so it cannot act on the repo by itself.

Models: pick by bare name (``claude-opus-5-5``, ``gpt-5.6-sol``, ``composer-2.5``) or by
the full advertised id (``claude-opus-5-5[context=300k,effort=high,fast=false]``).
"""

from __future__ import annotations

import asyncio
import atexit
from collections import deque
import contextlib
import errno
import hashlib
import inspect
import json
import logging
import os
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

CURSOR_MARKER_BASE_URL = "acp://cursor"
_TOOL_CALL_TAG_NAME = r"(?:tool_?call(?::\d+)?|call(?::\d+)?|function_call(?::\d+)?)"
_TOOL_CALL_OPEN_PATTERN = rf"<(?:{_TOOL_CALL_TAG_NAME})>"
_TOOL_CALL_CLOSE_PATTERN = rf"</(?:{_TOOL_CALL_TAG_NAME})>"
_TOOL_CALL_RE = re.compile(_TOOL_CALL_OPEN_PATTERN, re.IGNORECASE)
_TOOL_CALL_CLOSE_RE = re.compile(rf"\s*{_TOOL_CALL_CLOSE_PATTERN}", re.IGNORECASE)
_TOOL_CALL_CLOSE_SEARCH_RE = re.compile(_TOOL_CALL_CLOSE_PATTERN, re.IGNORECASE)
# A JSON object that opens a line, optionally inside a ``` fence: an untagged tool call.
_UNTAGGED_CALL_RE = re.compile(r"(?m)^[ \t]*(?:```[a-zA-Z]*[ \t]*\n[ \t]*)?\{")
_FENCE_CLOSE_RE = re.compile(r"\s*```")
_PROMPT_TOOL_ECHO_RE = re.compile(r"(?m)^Tool result(?: \([^\n)]+\))?:\s*\r?\n")
_PROMPT_ECHO_BOUNDARY_RE = re.compile(
    rf"(?m)^(?:Tool result(?: \([^\n)]+\))?:|{_TOOL_CALL_OPEN_PATTERN})",
    re.IGNORECASE,
)
_ASSISTANT_LINE_PREFIX_RE = re.compile(r"^[ \t]*Assistant:[ \t]*(?:\r?\n)?")
_FENCE_LINE_RE = re.compile(r"^[ \t]*(?:```|~~~)")
_ROLE_LINE_TOKEN_RE = re.compile(r"^[^\s<>{}()\[\].,;!?:'\"`]{1,32}$")
_HARNESS_WRAPPER_ECHO_RE = re.compile(
    r"^\s*(?:[^\s<>{}()\[\].,;!?:'\"`]{1,32}\r?\n)?"
    r"(?:<timestamp>(?:.*?</timestamp>|[^\n]*)(?:\s*<user_query>)?|<user_query>)\s*",
    re.DOTALL,
)
_DEFAULT_MODE = "agent"  # measured: ask mode made gpt-5.6-sol refuse the bridge 2/5; the deny list is the real fence
# Cursor's harness prompt pushes its native tools (Shell, Read, Edit...). Those are
# cancelled by the shim, so the model must route every action through Hermes instead.
_HARNESS_OVERRIDE = (
    "HARNESS OVERRIDE — read first.\n"
    "You are the language model behind a different agent (Hermes). Every built-in Cursor tool "
    "(Shell, Read, Grep, Glob, Edit, Write, Search, MCP, todo) is DISABLED in this session and any "
    "call to one is automatically cancelled. Never call them.\n"
    "To act, emit one or more <tool_call>{\"id\": \"call_1\", \"type\": \"function\", \"function\": "
    "{\"name\": \"<tool>\", \"arguments\": \"<JSON string>\"}}</tool_call> blocks "
    "as plain text using ONLY the tools listed below, then stop and wait: Hermes runs them and "
    "sends back the results in the next turn. If no tool is needed, answer directly.\n\n"
)
def _resolve_cursor_command(command: str | None = None) -> str:
    """Prefer Cursor's unique executable name over the shared ``agent`` name.

    Grok also installs ``agent``. Keep explicit paths authoritative and retain
    legacy Cursor installs, without letting a PATH collision hide the catalog.
    """
    override = command or next((os.environ[name].strip() for name in
        ("HERMES_CURSOR_ACP_COMMAND", "CURSOR_AGENT_PATH") if os.environ.get(name, "").strip()), "")
    if override:
        return os.path.expanduser(override)
    for name in ("cursor-agent", "agent"):
        found = shutil.which(name)
        if found:
            return found
        local = Path.home() / ".local" / "bin" / name
        if local.is_file() and os.access(local, os.X_OK):
            return str(local)
    return "cursor-agent"


def _workspace_dir() -> str:
    """Empty, private cwd so Cursor never reads repo rules or files on its own."""
    override = os.environ.get("HERMES_CURSOR_ACP_CWD", "").strip()
    if override:
        path = Path(override).expanduser()
    else:
        try:
            from hermes_constants import get_hermes_home

            base = Path(get_hermes_home())
        except Exception:
            base = Path.home() / ".hermes"
        path = base / "cache" / "cursor-acp-workspace"
    path.mkdir(parents=True, exist_ok=True)
    _write_deny_permissions(path)
    return str(path.resolve())


# Cursor's harness tools run inside the CLI, not through the ACP client, so the shim's
# permission cancel does not stop them: in ask mode the native Read tool reads ANY
# absolute path on disk and ships the bytes to Cursor. A project-level deny list in the
# workspace is what actually blocks them (verified: Read returns "Permission denied").
_DENY_PERMISSIONS = {
    "permissions": {
        "allow": [],
        "deny": [
            "Read(**)", "Read(/**)", "Write(**)", "Write(/**)",
            "Shell(*)", "WebFetch(*)", "Mcp(*:*)",
        ],
    }
}


def _write_deny_permissions(workspace: Path) -> None:
    """(Re)write ``<workspace>/.cursor/cli.json`` every time so drift can't loosen it.

    If a pre-existing custom ``.cursor/cli.json`` is present, preserve a backup at
    ``.cursor/cli.json.bak`` before writing the deny policy.
    """
    import json
    cfg = workspace / ".cursor" / "cli.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(_DENY_PERMISSIONS, indent=2) + "\n"
    if cfg.exists():
        existing = cfg.read_text(encoding="utf-8")
        if existing != body:
            bak = workspace / ".cursor" / "cli.json.bak"
            if not bak.exists():
                bak.write_text(existing, encoding="utf-8")
            cfg.write_text(body, encoding="utf-8")
    else:
        cfg.write_text(body, encoding="utf-8")


def _bare(model_id: str) -> str:
    return str(model_id or "").split("[", 1)[0].strip()


_ROLE_LABELS = {"system": "System", "user": "User", "assistant": "Assistant", "tool": "Tool result"}
_DATA_IMAGE_URL_RE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+)(?:;[^;,=]+=[^;,]+)*;base64,(.+)$", re.DOTALL)
_STREAM_END = object()


def _parse_data_image_url(url: str) -> dict[str, str] | None:
    """Convert a ``data:image/<type>;base64,<b64>`` URL into an ACP ``image`` block."""
    if not isinstance(url, str):
        return None
    m = _DATA_IMAGE_URL_RE.match(url.strip())
    if not m:
        return None
    mime_type, data = m.group(1).lower(), "".join(m.group(2).split())
    if not data:
        return None
    return {"type": "image", "mimeType": mime_type, "data": data}


def _extract_content_and_images(content: Any) -> tuple[str, list[dict[str, str]]]:
    """Extract rendered text and ACP ``image`` blocks from OpenAI/Anthropic message content."""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content.strip(), []
    if isinstance(content, list):
        text_parts: list[str] = []
        images: list[dict[str, str]] = []
        for p in content:
            if isinstance(p, str):
                if s := p.strip():
                    text_parts.append(s)
                continue
            if not isinstance(p, dict):
                continue
            ptype = str(p.get("type") or "").strip().lower()
            if ptype in ("image_url", "input_image"):
                raw_img = p.get("image_url")
                url = raw_img.get("url", "") if isinstance(raw_img, dict) else (raw_img if isinstance(raw_img, str) else "")
                if url:
                    if block := _parse_data_image_url(url):
                        images.append(block)
                        digest = hashlib.sha256(block["data"].encode("ascii", errors="ignore")).hexdigest()[:12]
                        text_parts.append(f"[Image: {block['mimeType']} {digest}]")
                    else:
                        text_parts.append(f"[Image URL: {url.strip()}]")
                continue
            if ptype == "image" and isinstance(p.get("source"), dict):
                src = p["source"]
                if src.get("type") == "base64" and src.get("data") and src.get("media_type"):
                    block = {"type": "image", "mimeType": str(src["media_type"]).lower(), "data": str(src["data"]).strip()}
                    images.append(block)
                    digest = hashlib.sha256(block["data"].encode("ascii", errors="ignore")).hexdigest()[:12]
                    text_parts.append(f"[Image: {block['mimeType']} {digest}]")
                    continue
            if p.get("text"):
                if s := str(p.get("text", "")).strip():
                    text_parts.append(s)
        return "\n".join(text_parts).strip(), images
    if isinstance(content, dict) and "text" in content:
        return str(content["text"]).strip(), []
    return str(content).strip(), []


def _render_content(content: Any) -> str:
    return _extract_content_and_images(content)[0]


def _extract_images_from_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    images: list[dict[str, str]] = []
    for m in messages:
        if isinstance(m, dict):
            images.extend(_extract_content_and_images(m.get("content"))[1])
    return images


def _render_message(msg: dict[str, Any]) -> str:
    """Render one transcript message, including the assistant's prior tool calls so the model
    sees its own <tool_call> history (the parent shim drops them, which breaks multi-step turns)."""
    role = str(msg.get("role") or "context").lower()
    parts = [_render_content(msg.get("content"))]
    for call in msg.get("tool_calls") or []:
        fn = (call.get("function") if isinstance(call, dict) else getattr(call, "function", None)) or {}
        name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", "")
        args = fn.get("arguments") if isinstance(fn, dict) else getattr(fn, "arguments", "{}")
        try:
            args_obj = json.loads(args) if isinstance(args, str) else args
        except Exception:
            args_obj = args
        parts.append("<tool_call>" + json.dumps(
            {"id": (call.get("id") if isinstance(call, dict) else getattr(call, "id", None)) or "call_1",
             "type": "function", "function": {"name": name, "arguments": args if isinstance(args, str)
                                               else json.dumps(args_obj, ensure_ascii=False)}},
            ensure_ascii=False) + "</tool_call>")
    attrs = f'role="{role}"'
    if role == "tool" and msg.get("name"):
        tool_name = msg["name"]
        attrs += f' name="{tool_name}"'
    body = "\n".join(p for p in parts if p)
    return f"<turn {attrs}>\n{body}\n</turn>" if body else ""


def _format_prompt(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, tool_choice: Any) -> str:
    """Hermes conversation -> one Cursor prompt. Tool contract up front AND a reminder at the
    end: recency is what keeps GPT/Composer from reaching for Cursor's own tools."""
    from agent.acp_openai_bridge import render_tool_bridge_sections, tool_specs_from_openai_tools

    sections = [_HARNESS_OVERRIDE.strip(), *render_tool_bridge_sections(tools, tool_choice)]
    transcript = [r for r in (_render_message(m) for m in messages if isinstance(m, dict)) if r]
    if transcript:
        sections.append("Conversation so far:\n\n" + "\n\n".join(transcript))
    names = [s["name"] for s in tool_specs_from_openai_tools(tools)]
    if names:
        sections.append(
            "Respond to the latest turn. The tools you CAN use are exactly: " + ", ".join(names) + ". "
            "They are real and wired: Hermes executes them on the user's machine when you emit "
            "<tool_call>{\"id\": \"call_1\", \"type\": \"function\", \"function\": {\"name\": \"<tool>\", "
            "\"arguments\": \"<JSON string>\"}}</tool_call>. If the task needs one, "
            "emit the block now and stop. Do not claim a tool is unavailable, and do not use Cursor's "
            "built-in tools; they are refused.")
    else:
        sections.append("Respond directly to the latest turn. No tools are available this turn.")
    return "\n\n".join(s.strip() for s in sections if s and s.strip())


def _format_delta_prompt(delta_messages: list[dict[str, Any]], tool_names: list[str]) -> str:
    """Format only the new messages since the last turn on a warm ACP session."""
    transcript = [r for r in (_render_message(m) for m in delta_messages if isinstance(m, dict)) if r]
    sections: list[str] = []
    if transcript:
        sections.append("\n\n".join(transcript))
    if tool_names:
        sections.append(
            "Continue the response. The tools you CAN use are exactly: " + ", ".join(tool_names) + ". "
            "Emit <tool_call>{\"id\": \"call_1\", \"type\": \"function\", \"function\": {\"name\": \"<tool>\", "
            "\"arguments\": \"<JSON string>\"}}</tool_call> if another tool is needed, or answer directly. "
            "Do not use Cursor's built-in tools; they are refused.")
    else:
        sections.append("Continue the response directly.")
    return "\n\n".join(s.strip() for s in sections if s and s.strip())


def _validate_and_normalize_arguments(raw_args: Any) -> tuple[str | None, str | None]:
    """Validate ``function.arguments`` as JSON; return ``(valid_json_str, None)`` or ``(None, error)``.

    Accepts a dict/list (serialized cleanly) or a JSON string. If a string has unescaped
    control characters (e.g. literal newlines), ``strict=False`` normalizes it losslessly.
    Invalid escapes or unescaped quotes are rejected so Hermes never misclassifies a
    syntax error as ``finish_reason=\"length\"`` truncation.
    """
    if raw_args is None:
        return "{}", None
    if not isinstance(raw_args, str):
        return json.dumps(raw_args, ensure_ascii=False), None
    stripped = raw_args.strip()
    if not stripped:
        return "{}", None
    try:
        json.loads(stripped)
        return stripped, None
    except json.JSONDecodeError as err:
        try:
            parsed = json.loads(stripped, strict=False)
            return json.dumps(parsed, ensure_ascii=False), None
        except (json.JSONDecodeError, TypeError, ValueError):
            return None, f"{err.msg} (char {err.pos})"


def _decode_call(
    obj: Any,
    index: int,
    tool_names: set[str] | None = None,
    *,
    errors: list[str] | None = None,
) -> Any:
    """One decoded JSON object -> a Hermes tool call, or None when it is not one.

    Accepts the flat {"name", "arguments"} shape models commonly emit; Hermes' parser only
    accepts {"function": {...}} and dropped those calls silently. With ``tool_names`` the
    call must name a tool Hermes offered this turn, and ``arguments`` must be valid JSON.
    """
    from agent.acp_openai_bridge import _parse_tool_call

    if not isinstance(obj, dict):
        return None
    if "function" not in obj and isinstance(obj.get("name"), str):
        obj = {"id": obj.get("id"), "type": "function",
               "function": {"name": obj["name"], "arguments": obj.get("arguments", {})}}
    fn = obj.get("function")
    if not isinstance(fn, dict):
        return None
    name = fn.get("name")
    if not isinstance(name, str) or not name or name == "<tool>":
        return None
    if tool_names is not None and name not in tool_names:
        return None
    norm_args, err = _validate_and_normalize_arguments(fn.get("arguments", "{}"))
    if norm_args is None:
        if errors is not None:
            errors.append(f"{name}: {err}")
        return None
    normalized_obj = {**obj, "function": {**fn, "arguments": norm_args}}
    return _parse_tool_call(json.dumps(normalized_obj), index)


def _strip_assistant_prefixes(text: str) -> str:
    """Remove leaked line-start ``Assistant:`` role prefixes outside fenced code blocks."""
    if not text or "Assistant:" not in text:
        return text
    out: list[str] = []
    in_fence = False
    for line in text.splitlines(keepends=True):
        if _FENCE_LINE_RE.match(line):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence:
            stripped = _ASSISTANT_LINE_PREFIX_RE.sub("", line, count=1)
            if stripped != line:
                if stripped:
                    out.append(stripped)
                continue
        out.append(line)
    return "".join(out)


def _strip_prompt_echoes(text: str) -> str:
    """Remove synthetic harness wrapper headers, `Tool result:` blocks, and leaked `Assistant:` prefixes."""
    if not text:
        return text
    text = _HARNESS_WRAPPER_ECHO_RE.sub("", text, count=1)
    if "Tool result" not in text:
        text = _strip_assistant_prefixes(text)
        return text.strip() if text != text.lstrip() else text
    decoder = json.JSONDecoder()
    pieces: list[str] = []
    cursor = 0
    for m in _PROMPT_TOOL_ECHO_RE.finditer(text):
        if m.start() < cursor:
            continue
        pos = m.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos < len(text) and text[pos] == "{":
            try:
                _, end = decoder.raw_decode(text, pos)
            except ValueError:
                nxt = _PROMPT_ECHO_BOUNDARY_RE.search(text, pos)
                end = nxt.start() if nxt else len(text)
        else:
            nxt = _PROMPT_ECHO_BOUNDARY_RE.search(text, m.end())
            end = nxt.start() if nxt else len(text)
        if m.start() > cursor:
            pieces.append(text[cursor:m.start()])
        cursor = end
    if cursor == 0:
        return _strip_assistant_prefixes(text)
    if cursor < len(text):
        pieces.append(text[cursor:])
    return _strip_assistant_prefixes("".join(pieces)).strip()


def _extract_tool_calls_with_errors(
    text: str, tool_names: list[str] | None = None
) -> tuple[list[Any], str, list[str]]:
    """Like ``extract_tool_calls``, also returning JSON syntax errors from malformed blocks."""
    if not isinstance(text, str):
        return [], "", []
    text = _strip_prompt_echoes(text)
    if not _TOOL_CALL_RE.search(text) and not tool_names:
        return [], text.strip(), []
    offered = set(tool_names) if tool_names is not None else None
    decoder = json.JSONDecoder()
    calls: list[Any] = []
    spans: list[tuple[int, int]] = []
    errors: list[str] = []
    for tag in _TOOL_CALL_RE.finditer(text):
        pos = tag.end()
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text) or text[pos] != "{":
            continue
        try:
            obj, end = decoder.raw_decode(text, pos)
        except ValueError as err:
            if tool_names or _TOOL_CALL_CLOSE_SEARCH_RE.search(text, pos):
                errors.append(f"{tag.group(0)} JSON error: {err}")
            continue
        close = _TOOL_CALL_CLOSE_RE.match(text, end)
        span_end = close.end() if close else end
        call = _decode_call(obj, len(calls) + 1, offered, errors=errors)
        if call is not None:
            calls.append(call)
            spans.append((tag.start(), span_end))
    if not calls and tool_names:
        offered, pos = set(tool_names), 0
        for opener in _UNTAGGED_CALL_RE.finditer(text):
            if opener.start() < pos:
                continue
            try:
                obj, end = decoder.raw_decode(text, opener.end() - 1)
            except ValueError:
                continue
            call = _decode_call(obj, len(calls) + 1, offered, errors=errors)
            if call is not None:
                close = _FENCE_CLOSE_RE.match(text, end) if "`" in opener.group(0) else None
                pos = close.end() if close else end
                calls.append(call)
                spans.append((opener.start(), pos))
    cleaned, cursor = [], 0
    for start, end in spans:
        cleaned.append(text[cursor:start])
        cursor = end
    cleaned.append(text[cursor:])
    return calls, "".join(cleaned).strip(), errors


def extract_tool_calls(text: str, tool_names: list[str] | None = None) -> tuple[list[Any], str]:
    """Pull every well-formed ``<tool_call>{json}</tool_call>`` block out of ``text``.

    Hermes' bridge regex pairs the FIRST ``<tool_call>`` with the first ``</tool_call>``, so
    when a model mentions the tag in prose before the real block ("emitting a `<tool_call>`
    now: <tool_call>{...}</tool_call>") the span is invalid JSON and the call is silently
    dropped. Hermes' parser also accepts ONLY the {"function": {...}} shape, silently dropping
    the flat {"name", "arguments"} shape models commonly emit. Measured: 4 of 4 composer-2.5
    'misses' in one run were real calls lost to these two defects. Each tag occurrence is
    decoded independently and both shapes are accepted.

    Some models (measured: glm-5p3) emit the call JSON with no tags at all, and it was shown
    to the user as the answer. When nothing was tagged and ``tool_names`` is given, a JSON
    object that opens a line and names an offered tool is taken as the call.

    ⚡ Bolt Optimization: Uses pre-compiled regexes and module-level imports
    to reduce per-call execution time, as this function is run on every generation chunk.
    """
    calls, cleaned, _ = _extract_tool_calls_with_errors(text, tool_names)
    return calls, cleaned


# Cursor reports every native tool attempt on the ACP stream as a ``tool_call`` update, with its
# input, even when the deny list refuses it. kind -> (Hermes tool, rawInput -> arguments).
_NATIVE_KIND_TO_HERMES = {
    "execute": ("terminal", lambda raw: {"command": raw["command"]}),
    "read": ("read_file", lambda raw: {"path": raw["path"]}),
    "search": ("search_files", lambda raw: {"pattern": raw["pattern"], **({"path": raw["path"]} if raw.get("path") else {})}),
}
_NATIVE_REFUSED_NUDGE = (
    "Your built-in tool call was refused, as the instructions said it would be. Nothing ran, and "
    "no permission setting will change that. Re-issue the same action now as a "
    "<tool_call>{{\"id\": \"call_1\", \"type\": \"function\", \"function\": {{\"name\": \"<tool>\", "
    "\"arguments\": \"<JSON string>\"}}}}</tool_call> text block using one of: {names}. "
    "Output only the block."
)
_MALFORMED_TOOL_CALL_NUDGE = (
    "Your <tool_call> had invalid JSON ({errors}), so nothing ran. Re-issue the same action now as valid JSON "
    "using one of: {names}. You may pass \"arguments\" as a plain JSON object (e.g. {{\"command\": \"...\"}}) "
    "instead of an escaped JSON string. Output only the <tool_call>{{...}}</tool_call> block."
)


def translate_native_calls(attempts: list[dict[str, Any]], tool_names: list[str] | None) -> list[dict[str, Any]]:
    """Refused native Cursor tool attempts -> the same action as Hermes tool calls.

    A model that reaches for Cursor's Shell/Read/Grep/MCP instead of the text bridge gets a
    denial from the fence and reports "blocked by permissions" to the user. The intent is on
    the ACP stream, so run it through Hermes instead (its approvals and sandbox apply). Only
    tools Hermes offered this turn are emitted; a repeated attempt is emitted once.
    """
    offered = set(tool_names or ())
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for attempt in attempts:
        raw = attempt.get("rawInput") or {}
        try:
            if attempt.get("kind") in _NATIVE_KIND_TO_HERMES:
                name, build = _NATIVE_KIND_TO_HERMES[attempt["kind"]]
                args = build(raw)
            else:  # MCP: {"providerIdentifier": "hub", "toolName": "bos_tabs", "args": {...}}
                server, tool = str(raw.get("providerIdentifier") or ""), str(raw["toolName"])
                name = next((n for n in (f"mcp_{server}_{tool}", f"mcp__{server}__{tool}", f"{server}_{tool}", tool)
                             if n in offered), "")
                args = raw.get("args") or {}
        except (KeyError, TypeError):
            continue
        key = (name, json.dumps(args, sort_keys=True, default=str))
        if name not in offered or key in seen:
            continue
        seen.add(key)
        out.append({"id": f"call_{len(out) + 1}", "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False, default=str)}})
    return out


def _set_model_with_retry(request: Any, session_id: str, model_id: str, attempts: int = 3) -> None:
    """Cursor intermittently answers set_model with 'Internal error' when several sessions start
    at once (measured 2 of 16 under 6-way parallelism). Retry before failing the turn."""
    import time

    for attempt in range(1, attempts + 1):
        try:
            request("session/set_model", {"sessionId": session_id, "modelId": model_id})
            return
        except RuntimeError as exc:
            if attempt == attempts or "Internal error" not in str(exc):
                raise
            time.sleep(0.5 * attempt)


_EFFORT_ALIASES = {"minimal": "low", "low": "low", "medium": "medium", "high": "high",
                   "xhigh": "xhigh", "max": "xhigh"}


def normalize_effort(reasoning_effort: Any = None, extra_body: Any = None) -> str | None:
    """Hermes reasoning request -> a Cursor ``reasoning_effort`` value, or None to keep the default.

    Reads the top-level ``reasoning_effort`` first, then ``extra_body.reasoning.effort``.
    ``none``/disabled and unknown strings give None.
    """
    raw = reasoning_effort
    if not raw and isinstance(extra_body, dict):
        reasoning = extra_body.get("reasoning")
        if isinstance(reasoning, dict):
            if reasoning.get("enabled") is False:
                return None
            raw = reasoning.get("effort")
    return _EFFORT_ALIASES.get(str(raw or "").strip().lower())


class _InitializeCapsStdin:
    """Wraps a child's stdin and adds ``_meta.parameterizedModelPicker`` to the ``initialize`` request.

    Without it ``agent acp`` lists model variants (``model[reasoning_effort=high]``) and rejects
    a separate effort. The base client sends a fixed initialize payload, so the capability is
    added here, on the plugin's own child pipe, without touching Hermes core.
    """

    def __init__(self, inner: Any):
        self._inner = inner
        self._done = False

    def write(self, data: str) -> Any:
        if not self._done and '"initialize"' in data:
            try:
                msg = json.loads(data)
                if msg.get("method") == "initialize":
                    caps = msg.setdefault("params", {}).setdefault("clientCapabilities", {})
                    caps.setdefault("_meta", {})["parameterizedModelPicker"] = True
                    data = json.dumps(msg) + "\n"
                    self._done = True
            except Exception:
                pass
        return self._inner.write(data)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def resolve_cursor_model(requested: str | None, available: list[dict[str, Any]]) -> str | None:
    """Map a Hermes model string onto one advertised Cursor modelId.

    Order: exact id -> exact display name -> bare id (text before ``[``).
    Returns None when nothing matches, so the session default is kept.
    """
    want = str(requested or "").strip()
    if not want or want in {"cursor", "cursor-acp", "cursor-agent", "cursor-cli", "default"}:
        return None
    ids = [str(m.get("modelId") or "") for m in available if isinstance(m, dict)]
    if want in ids:
        return want
    if "[" in want:
        return None
    for m in available:
        if isinstance(m, dict) and str(m.get("name") or "").strip() == want:
            return str(m.get("modelId"))
    for mid in ids:
        if _bare(mid) == _bare(want):
            return mid
    return None


class _WarmACPSession:
    """Live ``agent acp`` process + session state reusable across tool-loop turns."""

    def __init__(self, *, proc: subprocess.Popen[str], inbox: queue.Queue[dict[str, Any]],
                 stderr_tail: deque[str], stderr_pump: threading.Thread, request_ids: Any,
                 session_id: str, config_key: tuple[Any, ...], sent_rendered: list[str] | None = None):
        self.proc = proc
        self.inbox = inbox
        self.stderr_tail = stderr_tail
        self.stderr_pump = stderr_pump
        self.request_ids = request_ids
        self.session_id = session_id
        self.config_key = config_key
        self.sent_rendered = list(sent_rendered or [])
        self.last_used_at = time.monotonic()


def _terminate_proc(proc: subprocess.Popen[str]) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        with contextlib.suppress(Exception):
            proc.kill()


class _ACPSessionPool:
    """Module-level pool of idle warm ``agent acp`` sessions keyed by config + message prefix."""

    def __init__(self, max_sessions: int = 8, idle_ttl_seconds: float = 600.0):
        self._max_sessions = max_sessions
        self._idle_ttl = idle_ttl_seconds
        self._lock = threading.Lock()
        self._sessions: list[_WarmACPSession] = []

    def _prune_locked(self, now: float) -> list[_WarmACPSession]:
        keep: list[_WarmACPSession] = []
        evict: list[_WarmACPSession] = []
        for s in self._sessions:
            if s.proc.poll() is not None or (now - s.last_used_at) > self._idle_ttl:
                evict.append(s)
            else:
                keep.append(s)
        self._sessions = keep
        return evict

    def checkout(self, config_key: tuple[Any, ...], rendered: list[str],
                 messages: list[dict[str, Any]]) -> _WarmACPSession | None:
        now = time.monotonic()
        with self._lock:
            evict = self._prune_locked(now)
            match_idx: int | None = None
            best_len = -1
            for idx, s in enumerate(self._sessions):
                if s.config_key != config_key:
                    continue
                n = len(s.sent_rendered)
                # Expect: messages[:n] == prior sent, messages[n] == assistant reply, messages[n+1:] == new delta
                if n > 0 and len(messages) > n + 1 and rendered[:n] == s.sent_rendered:
                    if str((messages[n] or {}).get("role") or "").lower() == "assistant" and n > best_len:
                        best_len = n
                        match_idx = idx
            picked = self._sessions.pop(match_idx) if match_idx is not None else None
        for s in evict:
            _terminate_proc(s.proc)
        return picked

    def checkin(self, sess: _WarmACPSession) -> None:
        if sess.proc.poll() is not None:
            _terminate_proc(sess.proc)
            return
        now = time.monotonic()
        sess.last_used_at = now
        evict: list[_WarmACPSession] = []
        with self._lock:
            evict.extend(self._prune_locked(now))
            while len(self._sessions) >= self._max_sessions:
                oldest_idx = min(range(len(self._sessions)), key=lambda i: self._sessions[i].last_used_at)
                evict.append(self._sessions.pop(oldest_idx))
            self._sessions.append(sess)
        for s in evict:
            _terminate_proc(s.proc)

    def close_all(self) -> None:
        with self._lock:
            sessions, self._sessions = self._sessions, []
        for s in sessions:
            _terminate_proc(s.proc)


_ACP_POOL = _ACPSessionPool()
atexit.register(_ACP_POOL.close_all)


_POTENTIAL_CALL_LINE_RE = re.compile(
    r"(?m)^[ \t]*(?:```|\{|Tool result(?:[ :(]|$)|Error:\s*(?:Non)?RetriableError:)"
)
_STREAM_ECHO_PREFIXES = (
    "Tool result:",
    "Tool result (",
)
_ECHO_PREFIXES = (
    "Assistant:",
    "user\n<timestamp>",
    "user\r\n<timestamp>",
    "user\n<user_query>",
    "user\r\n<user_query>",
    "<timestamp>",
    "<user_query>",
    "HARNESS OVERRIDE",
    "Error: RetriableError:",
    "Error: NonRetriableError:",
)
_ACP_ERROR_LINE_RE = re.compile(r"(?m)^Error:\s*((?:Non)?RetriableError):\s*(.+)$")


def _echo_prefix_state(stripped: str) -> tuple[bool, bool]:
    """Return ``(definite_echo, possible_prefix)`` for the stripped start of an ACP response."""
    if not stripped:
        return False, True
    all_prefixes = _ECHO_PREFIXES + _STREAM_ECHO_PREFIXES
    if any(stripped.startswith(p) for p in all_prefixes):
        return True, False
    if any(p.startswith(stripped) for p in all_prefixes):
        return False, True
    # Localized or variant role label on line 1 (e.g. 'ユーザー\n<timestamp>...') before harness tags.
    if "\n" not in stripped:
        if _ROLE_LINE_TOKEN_RE.match(stripped.rstrip("\r")):
            return False, True
        return False, False
    first_line, rest = stripped.split("\n", 1)
    if _ROLE_LINE_TOKEN_RE.match(first_line.rstrip("\r")):
        rest_stripped = rest.lstrip()
        if not rest_stripped:
            return False, True
        if any(rest_stripped.startswith(tag) for tag in ("<timestamp>", "<user_query>")):
            return True, False
        if any(tag.startswith(rest_stripped) for tag in ("<timestamp>", "<user_query>")):
            return False, True
    return False, False


def _detect_acp_error(text: str) -> str | None:
    """Return a retryable error message when ``agent acp`` echoes its prompt wrapper or emits a terminal error."""
    if not isinstance(text, str) or not text:
        return None
    m = _ACP_ERROR_LINE_RE.search(text)
    if m:
        return f"Cursor ACP upstream error: {m.group(1)}: {m.group(2).strip()}"
    after_wrapper = _HARNESS_WRAPPER_ECHO_RE.sub("", text, count=1).lstrip()
    stripped = text.lstrip()
    if stripped.startswith("HARNESS OVERRIDE") or after_wrapper.startswith("HARNESS OVERRIDE"):
        return "Cursor ACP echoed its prompt wrapper instead of returning an assistant response."
    if after_wrapper != stripped and not after_wrapper:
        return "Cursor ACP echoed its prompt wrapper instead of returning an assistant response."
    return None


def _safe_prefix_end(buf: str, emitted_len: int) -> int:
    """End index up to which ``buf[emitted_len:]`` is guaranteed not to start a tool call."""
    tail = buf[emitted_len:]
    if not tail:
        return emitted_len
    cut = len(tail)
    lt = tail.find("<")
    if lt != -1:
        cut = min(cut, lt)
    m = _POTENTIAL_CALL_LINE_RE.search(tail)
    if m:
        cut = min(cut, m.start())
    # Hold back trailing '<' or newline-only suffix that could open a line-start '{'/'```'.
    if cut == len(tail) and tail.endswith(("\n", "`")):
        cut = max(0, cut - 1)
    return emitted_len + cut


class _CursorACPStream:
    """OpenAI-compatible streaming iterator backed by a live ACP ``session/prompt`` thread."""

    def __init__(self, *, client: Any, model: str, tool_names: list[str], run_fn: Any):
        from types import SimpleNamespace

        self._SimpleNamespace = SimpleNamespace
        self._client = client
        self._model = model or "cursor-acp"
        self._tool_names = tool_names
        self._q: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._pending_chunks: deque[Any] = deque()
        self._closed = False
        self._done = False
        self._raw_text = ""
        self._emitted_len = 0
        self._emitted_text = ""
        self._at_line_start = True
        self._in_fence = False
        self._started_text = False
        self._echo_cleared = False
        self._run_fn = run_fn
        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._worker.start()

    def _drain_stream_pieces(self) -> str:
        """Consume safe prefix from ``self._raw_text[self._emitted_len:]``, stripping leaked ``Assistant:`` lines."""
        out: list[str] = []
        while self._emitted_len < len(self._raw_text):
            tail = self._raw_text[self._emitted_len:]
            if self._at_line_start and not self._in_fence:
                no_indent = tail.lstrip(" \t")
                if not no_indent:
                    break
                if "Assistant:".startswith(no_indent) or no_indent == "Assistant:":
                    break
                m = _ASSISTANT_LINE_PREFIX_RE.match(tail)
                if m:
                    # If the match ended at 'Assistant:' or 'Assistant:   ' without a newline/body, wait for next char.
                    if m.end() == len(tail) and not tail.endswith("\n"):
                        break
                    self._emitted_len += m.end()
                    self._at_line_start = m.group(0).endswith("\n")
                    continue
            if self._tool_names:
                safe_end = _safe_prefix_end(self._raw_text, self._emitted_len)
            else:
                nl = tail.find("\n")
                if nl != -1:
                    safe_end = self._emitted_len + nl + 1
                elif tail.endswith(("\n", "`")):
                    safe_end = self._emitted_len + max(0, len(tail) - 1)
                else:
                    safe_end = len(self._raw_text)
            if safe_end <= self._emitted_len:
                break
            # Stop at the first newline so we re-check _at_line_start / _in_fence on the next line.
            chunk = self._raw_text[self._emitted_len:safe_end]
            nl = chunk.find("\n")
            if nl != -1:
                chunk = chunk[:nl + 1]
            if self._at_line_start and _FENCE_LINE_RE.match(chunk):
                if chunk.endswith("\n"):
                    self._in_fence = not self._in_fence
                else:
                    break
            self._emitted_len += len(chunk)
            self._at_line_start = chunk.endswith("\n")
            out.append(chunk)
        piece = "".join(out)
        if piece:
            self._emitted_text += piece
        return piece

    def _run_worker(self) -> None:
        def _on_chunk(kind: str, text: str) -> None:
            if not self._closed and text:
                self._q.put((kind, text))

        try:
            full_text, full_reasoning = self._run_fn(_on_chunk)
            if not self._closed:
                self._q.put(("done", (full_text, full_reasoning)))
        except Exception as exc:
            if not self._closed:
                self._q.put(("error", exc))

    def _make_chunk(self, *, content: str | None = None, reasoning: str | None = None,
                    tool_calls: list[Any] | None = None, finish_reason: str | None = None,
                    choices_present: bool = True) -> Any:
        NS = self._SimpleNamespace
        if not choices_present:
            return NS(choices=[], model=self._model, usage=None)
        delta = NS(
            role="assistant",
            content=content,
            tool_calls=tool_calls,
            reasoning_content=reasoning,
            reasoning=reasoning,
        )
        return NS(
            choices=[NS(index=0, delta=delta, finish_reason=finish_reason)],
            model=self._model,
            usage=None,
        )

    def __iter__(self) -> "_CursorACPStream":
        return self

    def __aiter__(self) -> "_CursorACPStream":
        return self

    def _next_or_sentinel(self) -> Any:
        try:
            return self.__next__()
        except StopIteration:
            return _STREAM_END

    async def __anext__(self) -> Any:
        item = await asyncio.to_thread(self._next_or_sentinel)
        if item is _STREAM_END:
            raise StopAsyncIteration
        return item

    async def aclose(self) -> None:
        self.close()

    def __next__(self) -> Any:
        if self._pending_chunks:
            return self._pending_chunks.popleft()
        if self._closed or self._done:
            raise StopIteration
        while True:
            if self._closed:
                raise StopIteration
            try:
                kind, payload = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            if kind == "thought":
                return self._make_chunk(reasoning=payload)
            if kind == "text":
                self._raw_text += payload
                if not self._started_text:
                    stripped = self._raw_text.lstrip()
                    if not stripped:
                        continue
                    self._emitted_len = len(self._raw_text) - len(stripped)
                    self._started_text = True
                if not self._echo_cleared:
                    definite, possible = _echo_prefix_state(self._raw_text[self._emitted_len:])
                    if definite or possible:
                        continue
                    self._echo_cleared = True
                if not self._tool_names:
                    piece = self._drain_stream_pieces()
                    if piece:
                        return self._make_chunk(content=piece)
                    continue
                # Tools are offered: hold text until done so pre-tool scratchpad
                # never leaks into user-facing assistant.delta before <tool_call>.
                continue
            if kind == "error":
                self._done = True
                raise payload
            if kind == "done":
                self._done = True
                full_text, _full_reasoning = payload
                if (acp_err := _detect_acp_error(full_text)) is not None:
                    raise ConnectionError(acp_err)
                tool_calls, cleaned, call_errors = _extract_tool_calls_with_errors(full_text, self._tool_names)
                if not tool_calls and call_errors:
                    raise ConnectionError(
                        f"Cursor ACP emitted malformed tool_call JSON ({'; '.join(call_errors[:3])})."
                    )
                if not tool_calls and not cleaned and _strip_prompt_echoes(full_text) != full_text:
                    raise ConnectionError("Cursor ACP echoed its prompt tool results instead of returning an assistant response.")
                already = self._emitted_text
                if already and cleaned.startswith(already):
                    rem = cleaned[len(already):]
                elif already:
                    rem = ""
                else:
                    rem = cleaned
                tc_deltas = None
                if tool_calls:
                    NS = self._SimpleNamespace
                    tc_deltas = [
                        NS(
                            index=idx,
                            id=getattr(tc, "id", None),
                            type=getattr(tc, "type", "function"),
                            function=NS(
                                name=getattr(tc.function, "name", None),
                                arguments=getattr(tc.function, "arguments", None),
                            ),
                        )
                        for idx, tc in enumerate(tool_calls)
                    ]
                finish_reason = "tool_calls" if tool_calls else "stop"
                if tool_calls:
                    self._pending_chunks.append(self._make_chunk(
                        content=None,
                        reasoning=rem or None,
                        tool_calls=tc_deltas,
                        finish_reason=finish_reason,
                    ))
                else:
                    self._pending_chunks.append(self._make_chunk(
                        content=rem or None,
                        tool_calls=None,
                        finish_reason=finish_reason,
                    ))
                self._pending_chunks.append(self._make_chunk(choices_present=False))
                return self._pending_chunks.popleft()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self._client.cancel()


def _make_client_class():
    from agent.copilot_acp_client import CopilotACPClient

    class CursorACPClient(CopilotACPClient):
        """CopilotACPClient pointed at ``agent acp`` with name-based model selection, streaming, and warm session reuse."""

        def __init__(self, *, command: str | None = None, args: list[str] | None = None,
                     acp_cwd: str | None = None, **kwargs: Any):
            kwargs.pop("acp_command", None)
            kwargs.pop("acp_args", None)
            kwargs.setdefault("api_key", "cursor-acp")
            kwargs.setdefault("base_url", CURSOR_MARKER_BASE_URL)
            cwd = acp_cwd or _workspace_dir()
            _write_deny_permissions(Path(cwd))
            self._native = threading.local()
            super().__init__(
                acp_command=_resolve_cursor_command(command),
                acp_args=list((args if args is not None else
                               shlex.split(os.environ.get("HERMES_CURSOR_ACP_ARGS", ""))) or ["acp"]),
                acp_cwd=cwd,
                **kwargs,
            )

        def _spawn(self):
            try:
                proc = super()._spawn()
            except RuntimeError as exc:
                if "Could not start" in str(exc):
                    raise RuntimeError(
                        f"Could not start the Cursor CLI '{self._acp_command}'. Install it with "
                        "`curl https://cursor.com/install -fsS | bash`, run `agent login`, or set "
                        "HERMES_CURSOR_ACP_COMMAND.") from exc
                raise
            if proc.stdin is not None and not isinstance(proc.stdin, _InitializeCapsStdin):
                proc.stdin = _InitializeCapsStdin(proc.stdin)
            return proc

        def cancel(self) -> None:
            """Stop every live ``agent acp`` child now.

            Hermes calls this when the stale-call watchdog or an interrupt aborts a request. The
            ACP pipes are stdio, so socket force-close does nothing; killing the child makes the
            blocked request fail at once (retryable) instead of leaking until the next retry.
            """
            with self._active_process_lock:
                procs = tuple(self._active_processes)
            for proc in procs:
                self._terminate_process(proc)

        @staticmethod
        def _caller_is_coroutine() -> bool:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return False
            frame = inspect.currentframe()
            caller = frame.f_back.f_back if frame and frame.f_back else None
            return bool(caller and (caller.f_code.co_flags & (inspect.CO_COROUTINE | inspect.CO_ASYNC_GENERATOR)))

        def _create_chat_completion(self, *, model: str | None = None, messages: list[dict[str, Any]] | None = None,
                                    timeout: Any = None, tools: list[dict[str, Any]] | None = None,
                                    tool_choice: Any = None, stream: bool = False,
                                    reasoning_effort: Any = None, extra_body: Any = None, **extra: Any) -> Any:
            call_kwargs = {
                "model": model, "messages": messages, "timeout": timeout, "tools": tools,
                "tool_choice": tool_choice, "stream": stream, "reasoning_effort": reasoning_effort,
                "extra_body": extra_body, **extra,
            }
            if self._caller_is_coroutine():
                return self._acreate_chat_completion(**call_kwargs)
            return self._create_chat_completion_sync(**call_kwargs)

        async def _acreate_chat_completion(self, **kwargs: Any) -> Any:
            if kwargs.get("stream"):
                return self._create_chat_completion_sync(**kwargs)
            return await asyncio.to_thread(self._create_chat_completion_sync, **kwargs)

        def _create_chat_completion_sync(self, *, model: str | None = None, messages: list[dict[str, Any]] | None = None,
                                         timeout: Any = None, tools: list[dict[str, Any]] | None = None,
                                         tool_choice: Any = None, stream: bool = False,
                                         reasoning_effort: Any = None, extra_body: Any = None, **_: Any) -> Any:
            from types import SimpleNamespace

            from agent.acp_openai_bridge import tool_specs_from_openai_tools
            from agent.copilot_acp_client import _effective_timeout

            msgs = messages or []
            names = [s["name"] for s in tool_specs_from_openai_tools(tools)]
            effort = normalize_effort(reasoning_effort, extra_body)
            timeout_seconds = _effective_timeout(timeout)
            tools_sig = json.dumps(
                {"tools": tool_specs_from_openai_tools(tools), "tool_choice": tool_choice},
                sort_keys=True, ensure_ascii=False, default=str,
            )

            if stream:
                def _run_for_stream(on_chunk):
                    prompt_text = _format_prompt(msgs, tools, tool_choice)
                    return self._run_prompt(
                        prompt_text, timeout_seconds=timeout_seconds, model=model, tool_names=names,
                        effort=effort, messages=msgs, tools_signature=tools_sig, on_chunk=on_chunk,
                        prompt_images=_extract_images_from_messages(msgs),
                    )

                return _CursorACPStream(client=self, model=model or "cursor-acp", tool_names=names, run_fn=_run_for_stream)

            prompt_text = _format_prompt(msgs, tools, tool_choice)
            response_text, reasoning = self._run_prompt(
                prompt_text, timeout_seconds=timeout_seconds, model=model, tool_names=names,
                effort=effort, messages=msgs, tools_signature=tools_sig,
                prompt_images=_extract_images_from_messages(msgs),
            )
            tool_calls, cleaned = extract_tool_calls(response_text, names)
            if tool_calls and cleaned:
                reasoning = f"{reasoning}\n\n{cleaned}".strip() if reasoning else cleaned
                cleaned = ""
            message = SimpleNamespace(content=cleaned, tool_calls=tool_calls, reasoning=reasoning or None,
                                      reasoning_content=reasoning or None, reasoning_details=None)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=message, finish_reason="tool_calls" if tool_calls else "stop")],
                usage=None, model=model or "cursor-acp")

        def list_models(self, *, timeout_seconds: float = 15.0) -> list[str]:
            with self._session(timeout_seconds, allow_file_requests=False) as (session, _):
                seen: list[str] = []
                for m in (session.get("models") or {}).get("availableModels") or []:
                    model_id = m.get("modelId") if isinstance(m, dict) else None
                    if isinstance(model_id, str) and model_id.strip() and model_id not in seen:
                        seen.append(model_id)
                return seen

        def _native_attempts(self) -> dict[str, dict[str, Any]]:
            """Native Cursor tool attempts seen on this thread's ACP stream, by toolCallId."""
            if not hasattr(self._native, "calls"):
                self._native.calls = {}
            return self._native.calls

        def _handle_server_message(self, msg: dict[str, Any], **kwargs: Any) -> bool:
            # The parent drops ``tool_call`` updates. They are the only record that the model
            # reached for a native Cursor tool, and they carry the input it wanted to run.
            if msg.get("method") == "session/update":
                update = (msg.get("params") or {}).get("update") or {}
                su = str(update.get("sessionUpdate") or "").strip()
                if su in ("tool_call", "tool_call_update") and update.get("toolCallId"):
                    entry = self._native_attempts().setdefault(str(update["toolCallId"]), {})
                    entry.update({k: update[k] for k in ("kind", "title", "rawInput") if update.get(k)})
                on_chunk = getattr(self._native, "on_chunk", None)
                if on_chunk is not None and su in ("agent_message_chunk", "agent_thought_chunk"):
                    content = update.get("content") or {}
                    chunk_text = str(content.get("text") or "") if isinstance(content, dict) else ""
                    if chunk_text:
                        on_chunk("text" if su == "agent_message_chunk" else "thought", chunk_text)
            return super()._handle_server_message(msg, **kwargs)

        def _session_request(self, sess: _WarmACPSession, method: str, params: dict[str, Any],
                             *, deadline: float, text_parts: list[str] | None = None,
                             reasoning_parts: list[str] | None = None,
                             allow_file_requests: bool = True) -> Any:
            request_id = next(sess.request_ids)
            proc = sess.proc
            try:
                proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}) + "\n")
                proc.stdin.flush()
            except OSError as exc:
                if not isinstance(exc, BrokenPipeError) and exc.errno != errno.EINVAL:
                    raise
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
                if proc.poll() is None:
                    raise
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    msg = sess.inbox.get(timeout=0.1)
                except queue.Empty:
                    continue
                if self._handle_server_message(
                    msg, process=proc, cwd=self._acp_cwd, text_parts=text_parts,
                    reasoning_parts=reasoning_parts, allow_file_requests=allow_file_requests,
                ) or msg.get("id") != request_id:
                    continue
                if "error" in msg:
                    raise RuntimeError(self._request_error(method, msg.get("error") or {}))
                return msg.get("result")
            if proc.poll() is not None:
                sess.stderr_pump.join(timeout=1.0)
                stderr_text = "\n".join(sess.stderr_tail).strip()
                if (vendor_error := self.early_exit_error(stderr_text)) is not None:
                    raise RuntimeError(vendor_error)
                raise RuntimeError(f"{self.label} process exited early: {stderr_text or f'exit code {proc.returncode}'}")
            raise TimeoutError(f"Timed out waiting for {self.label} response to {method}.")

        def _open_warm_session(self, *, timeout_seconds: float, model: str | None,
                               effort: str | None, mode: str, config_key: tuple[Any, ...]) -> _WarmACPSession:
            from agent.acp_client import _INITIALIZE_PARAMS

            proc = self._spawn()
            inbox: queue.Queue[dict[str, Any]] = queue.Queue()
            stderr_tail: deque[str] = deque(maxlen=40)

            def _decode(line: str) -> dict[str, Any]:
                try:
                    return json.loads(line)
                except Exception:
                    return {"raw": line.rstrip("\n")}

            def _pump(stream: Any, sink: Any) -> None:
                for line in stream or ():
                    sink(line)

            threading.Thread(target=_pump, args=(proc.stdout, lambda line: inbox.put(_decode(line))), daemon=True).start()
            stderr_pump = threading.Thread(
                target=_pump, args=(proc.stderr, lambda line: stderr_tail.append(line.rstrip("\n"))), daemon=True)
            stderr_pump.start()
            sess = _WarmACPSession(
                proc=proc, inbox=inbox, stderr_tail=stderr_tail, stderr_pump=stderr_pump,
                request_ids=iter(range(1, 1 << 62)), session_id="", config_key=config_key,
            )
            deadline = time.monotonic() + timeout_seconds
            try:
                self._session_request(sess, "initialize", _INITIALIZE_PARAMS, deadline=deadline)
                session = self._session_request(sess, "session/new", {"cwd": self._acp_cwd, "mcpServers": []}, deadline=deadline) or {}
                session_id = str(session.get("sessionId") or "").strip()
                if not session_id:
                    raise RuntimeError(f"{self.label} did not return a sessionId.")
                sess.session_id = session_id
                available = (session.get("models") or {}).get("availableModels") or []
                model_id = resolve_cursor_model(model, available)
                if model and model_id is None and str(model).strip() not in {"cursor", "cursor-acp", "cursor-agent", "cursor-cli", "default"}:
                    raise RuntimeError(
                        f"Cursor does not offer model {model!r} on this account. "
                        "Run `agent models` to see the list.")
                if model_id:
                    _set_model_with_retry(
                        lambda m, p: self._session_request(sess, m, p, deadline=deadline),
                        session_id, model_id,
                    )
                if effort and "[" not in (model_id or ""):
                    try:
                        self._session_request(
                            sess, "session/set_config_option",
                            {"sessionId": session_id, "configId": "reasoning_effort", "value": effort},
                            deadline=deadline,
                        )
                    except Exception as exc:
                        logger.warning("Cursor ACP reasoning_effort=%s not applied for %s: %s", effort, model_id, exc)
                if mode:
                    try:
                        self._session_request(sess, "session/set_mode", {"sessionId": session_id, "modeId": mode}, deadline=deadline)
                    except Exception as exc:
                        logger.warning("Cursor ACP set_mode(%s) failed: %s", mode, exc)
                return sess
            except Exception:
                self._release_process(proc)
                raise

        def _run_prompt_turn(self, *, ask: Any, model: str | None, tool_names: list[str] | None,
                             effective_prompt: str, effective_images: list[dict[str, str]] | None = None,
                             on_reusable: Any = None) -> tuple[str, str]:
            native = self._native_attempts()
            native.clear()
            text, reasoning = ask(effective_prompt, images=effective_images)
            if (acp_err := _detect_acp_error(text)) is not None:
                raise ConnectionError(acp_err)
            if tool_names:
                parsed_calls, _cleaned, call_errors = _extract_tool_calls_with_errors(text, tool_names)
                if parsed_calls:
                    if on_reusable is not None:
                        on_reusable()
                    return text, reasoning
                if call_errors:
                    logger.warning("cursor-acp: model=%s emitted malformed <tool_call> JSON (%s); re-prompting once",
                                   model, "; ".join(call_errors[:3]))
                    retry_text, retry_reasoning = ask(
                        _MALFORMED_TOOL_CALL_NUDGE.format(
                            errors="; ".join(call_errors[:3]),
                            names=", ".join(tool_names),
                        )
                    )
                    if (acp_err := _detect_acp_error(retry_text)) is not None:
                        raise ConnectionError(acp_err)
                    retry_calls, _retry_cleaned, retry_errors = _extract_tool_calls_with_errors(retry_text, tool_names)
                    if retry_calls:
                        if on_reusable is not None:
                            on_reusable()
                        return retry_text, reasoning + retry_reasoning
                    raise ConnectionError(
                        f"Cursor ACP model={model} emitted malformed tool_call JSON after retry "
                        f"({'; '.join((retry_errors or call_errors)[:3])})."
                    )
            if not tool_names or not native:
                if on_reusable is not None:
                    on_reusable()
                return text, reasoning
            kinds = sorted({str(a.get("title") or a.get("kind") or "?") for a in native.values()})
            rerouted = translate_native_calls(list(native.values()), tool_names)
            if rerouted:
                logger.info("cursor-acp: model=%s used %d refused native tool call(s) %s; rerouted to Hermes: %s",
                            model, len(native), kinds, [c["function"]["name"] for c in rerouted])
                if on_reusable is not None:
                    on_reusable()
                return "".join(f"<tool_call>{json.dumps(c, ensure_ascii=False)}</tool_call>" for c in rerouted), reasoning
            logger.info("cursor-acp: model=%s used refused native tool call(s) %s with no Hermes equivalent; "
                        "re-prompting once", model, kinds)
            retry_text, retry_reasoning = ask(_NATIVE_REFUSED_NUDGE.format(names=", ".join(tool_names)))
            if (acp_err := _detect_acp_error(retry_text)) is not None:
                raise ConnectionError(acp_err)
            if on_reusable is not None:
                on_reusable()
            if extract_tool_calls(retry_text, tool_names)[0]:
                return retry_text, reasoning + retry_reasoning
            return text, reasoning

        def _run_prompt(self, prompt_text: str, *, timeout_seconds: float, model: str | None = None,
                        tool_names: list[str] | None = None, effort: str | None = None,
                        messages: list[dict[str, Any]] | None = None, tools_signature: str | None = None,
                        on_chunk: Any = None, prompt_images: list[dict[str, str]] | None = None) -> tuple[str, str]:
            mode = os.environ.get("HERMES_CURSOR_ACP_MODE", _DEFAULT_MODE).strip()
            if "_session" in self.__dict__:
                self._native.on_chunk = on_chunk
                try:
                    with self._session(timeout_seconds) as (session, _request):
                        session_id = str(session.get("sessionId") or "").strip()
                        available = (session.get("models") or {}).get("availableModels") or []
                        model_id = resolve_cursor_model(model, available)
                        if model and model_id is None and str(model).strip() not in {"cursor", "cursor-acp", "cursor-agent", "cursor-cli", "default"}:
                            raise RuntimeError(
                                f"Cursor does not offer model {model!r} on this account. "
                                "Check /model in Hermes or run `cursor-agent models` to inspect Cursor availability.")
                        if model_id:
                            _set_model_with_retry(_request, session_id, model_id)
                        if effort and "[" not in (model_id or ""):
                            try:
                                _request("session/set_config_option", {"sessionId": session_id, "configId": "reasoning_effort", "value": effort})
                            except Exception as exc:
                                logger.warning("Cursor ACP reasoning_effort=%s not applied for %s: %s", effort, model_id, exc)
                        if mode:
                            try:
                                _request("session/set_mode", {"sessionId": session_id, "modeId": mode})
                            except Exception as exc:
                                logger.warning("Cursor ACP set_mode(%s) failed: %s", mode, exc)

                        def ask(text: str, images: list[dict[str, str]] | None = None) -> tuple[str, str]:
                            text_parts: list[str] = []
                            reasoning_parts: list[str] = []
                            prompt_blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
                            if images:
                                prompt_blocks.extend(images)
                            _request(
                                "session/prompt",
                                {"sessionId": session_id, "prompt": prompt_blocks},
                                text_parts=text_parts, reasoning_parts=reasoning_parts,
                            )
                            return "".join(text_parts), "".join(reasoning_parts)

                        return self._run_prompt_turn(
                            ask=ask, model=model, tool_names=tool_names,
                            effective_prompt=prompt_text, effective_images=list(prompt_images or ()),
                        )
                finally:
                    self._native.on_chunk = None

            config_key = (
                self._acp_command, tuple(self._acp_args), self._acp_cwd,
                _bare(model or ""), effort or "", mode, tools_signature or "",
            )
            msgs = messages or []
            rendered = [_render_message(m) for m in msgs if isinstance(m, dict)] if msgs else []
            sess = _ACP_POOL.checkout(config_key, rendered, msgs) if msgs else None
            if sess is not None:
                with self._active_process_lock:
                    self._active_processes.add(sess.proc)
                    self.is_closed = False
                delta_msgs = msgs[len(sess.sent_rendered) + 1:]
                effective_prompt = _format_delta_prompt(delta_msgs, list(tool_names or ()))
                effective_images = _extract_images_from_messages(delta_msgs)
            else:
                sess = self._open_warm_session(
                    timeout_seconds=timeout_seconds, model=model, effort=effort, mode=mode, config_key=config_key,
                )
                effective_prompt = prompt_text
                effective_images = list(prompt_images or ())

            deadline = time.monotonic() + timeout_seconds
            self._native.on_chunk = on_chunk
            reusable = False
            try:
                def ask(text: str, images: list[dict[str, str]] | None = None) -> tuple[str, str]:
                    text_parts: list[str] = []
                    reasoning_parts: list[str] = []
                    prompt_blocks: list[dict[str, Any]] = [{"type": "text", "text": text}]
                    if images:
                        prompt_blocks.extend(images)
                    self._session_request(
                        sess, "session/prompt",
                        {"sessionId": sess.session_id, "prompt": prompt_blocks},
                        deadline=deadline, text_parts=text_parts, reasoning_parts=reasoning_parts,
                    )
                    return "".join(text_parts), "".join(reasoning_parts)

                def _mark_reusable() -> None:
                    nonlocal reusable
                    reusable = bool(msgs) and sess.proc.poll() is None

                return self._run_prompt_turn(
                    ask=ask, model=model, tool_names=tool_names,
                    effective_prompt=effective_prompt, effective_images=effective_images,
                    on_reusable=_mark_reusable,
                )
            finally:
                self._native.on_chunk = None
                if reusable:
                    with self._active_process_lock:
                        self._active_processes.discard(sess.proc)
                    sess.sent_rendered = rendered
                    _ACP_POOL.checkin(sess)
                else:
                    self._release_process(sess.proc)

    return CursorACPClient


class CursorACPProfile(ProviderProfile):
    """Cursor subscription via ``agent acp`` (external process, no REST endpoint)."""

    def create_client(self, **client_kwargs: Any) -> Any:
        return _make_client_class()(**client_kwargs)

    def fetch_models(self, *, api_key: str | None = None, base_url: str | None = None,
                     timeout: float = 30.0) -> list[str] | None:
        try:
            from hermes_cli.auth import resolve_external_process_provider_credentials

            creds = resolve_external_process_provider_credentials(self.name)
            client = self.create_client(command=creds.get("command"), args=creds.get("args"))
            try:
                models = client.list_models(timeout_seconds=timeout)
                if not models:
                    raise RuntimeError("Cursor ACP returned an empty model catalog")
                return models
            finally:
                client.close()
        except Exception as exc:
            logger.warning("cursor-acp model discovery failed: %s. Check the Cursor CLI and login; "
                           "no static model list will be substituted.", exc)
            return []

    def get_model_context_length(self, model: str) -> int | None:
        return 200_000


_VISION_MODEL_PREFIXES = (
    "claude-", "gemini-", "gpt-5", "grok-", "composer-", "auto-smart", "o1", "o3", "o4",
)


class _CursorModelCapabilities(dict):
    """Capability map for Cursor models, stripping ``[...]`` parameter suffixes."""

    def __init__(self) -> None:
        super().__init__()

    @staticmethod
    def _caps_for(key: Any) -> dict[str, Any] | None:
        bare = _bare(str(key or "")).lower()
        if any(bare.startswith(prefix) for prefix in _VISION_MODEL_PREFIXES):
            return {"supports_vision": True}
        return None

    def get(self, key: Any, default: Any = None) -> Any:
        if (hit := super().get(key)) is not None:
            return hit
        return caps if (caps := self._caps_for(key)) is not None else default

    def __contains__(self, key: object) -> bool:
        return super().__contains__(key) or self._caps_for(key) is not None

    def __getitem__(self, key: Any) -> Any:
        if super().__contains__(key):
            return super().__getitem__(key)
        if (caps := self._caps_for(key)) is not None:
            return caps
        raise KeyError(key)


cursor_acp = CursorACPProfile(
    name="cursor-acp",
    aliases=("cursor-agent", "cursor-cli"),
    display_name="Cursor Subscription (ACP)",
    description="Use your Cursor subscription models via the Cursor CLI (`agent acp`)",
    api_mode="chat_completions",
    env_vars=(),
    base_url=CURSOR_MARKER_BASE_URL,
    auth_type="external_process",
    supports_streaming=True,
    supports_vision=True,
    model_capabilities=_CursorModelCapabilities(),
    process_command=_resolve_cursor_command(),
    process_args=("acp",),
    process_command_env_vars=("HERMES_CURSOR_ACP_COMMAND", "CURSOR_AGENT_PATH"),
    process_args_env_var="HERMES_CURSOR_ACP_ARGS",
    default_aux_model="composer-2.5",
    fallback_models=(),
)

register_provider(cursor_acp)
