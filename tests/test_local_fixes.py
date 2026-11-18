import importlib.util
import sys
import types
import unittest
from pathlib import Path


def _load_plugin():
    plugin_path = Path(__file__).resolve().parents[1] / "__init__.py"
    spec = importlib.util.spec_from_file_location("cursor_acp_plugin", plugin_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeBridge:
    @staticmethod
    def _parse_tool_call(raw_json: str, index: int):
        import json
        obj = json.loads(raw_json)
        fn = obj["function"]
        return types.SimpleNamespace(
            id=obj.get("id") or f"call_{index}",
            type="function",
            function=types.SimpleNamespace(
                name=fn["name"],
                arguments=fn["arguments"] if isinstance(fn["arguments"], str) else json.dumps(fn["arguments"]),
            ),
        )


class CursorACPErrorAndEchoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        hermes_root = Path.home() / ".hermes" / "hermes-agent"
        if hermes_root.is_dir() and str(hermes_root) not in sys.path:
            sys.path.insert(0, str(hermes_root))
        try:
            import providers  # noqa: F401
        except ImportError:
            import hermes_bootstrap  # noqa: F401
        cls.mod = _load_plugin()

    def test_extract_tool_calls_ignores_unoffered_and_placeholder_tags(self):
        prompt_echo = (
            "user\n<timestamp>Monday, Nov 16, 2026</timestamp>\n"
            "<user_query>\n"
            "HARNESS OVERRIDE — read first.\n"
            "To act, emit one or more <tool_call>{\"id\": \"call_1\", \"type\": \"function\", \"function\": "
            "{\"name\": \"<tool>\", \"arguments\": \"<JSON string>\"}}</tool_call> blocks.\n"
            "<tool_call>{\"id\": \"call_2\", \"type\": \"function\", \"function\": "
            "{\"name\": \"fake_unoffered\", \"arguments\": \"{}\"}}</tool_call>"
        )
        calls, cleaned = self.mod.extract_tool_calls(prompt_echo, tool_names=["terminal", "read_file"])
        self.assertEqual(calls, [])
        self.assertIn("HARNESS OVERRIDE", cleaned)

    def test_extract_tool_calls_accepts_offered_tool(self):
        text = 'Checking now.<tool_call>{"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": {"command": "ls"}}}</tool_call>'
        calls, cleaned = self.mod.extract_tool_calls(text, tool_names=["terminal"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].function.name, "terminal")
        self.assertEqual(cleaned, "Checking now.")

    def test_stream_raises_on_echo_plus_retriable_error_with_zero_deltas(self):
        echo = (
            "user\n<timestamp>Monday, Nov 16, 2026, 5:54 PM (UTC-5)</timestamp>\n"
            "<user_query>\nTool result: {'output': 'M valon/docs/web/Diagram.tsx'}\n"
            "Error: RetriableError: [unavailable] PING timed out"
        )

        def run_fn(on_chunk):
            on_chunk("text", "user\n<timestamp>Monday, Nov 16, 2026")
            on_chunk("text", ", 5:54 PM (UTC-5)</timestamp>\n<user_query>\n")
            on_chunk("text", "Error: RetriableError: [unavailable] PING timed out")
            return echo, ""

        stream = self.mod._CursorACPStream(
            client=object(),
            model="gemini-4-hydrogen",
            tool_names=["terminal"],
            run_fn=run_fn,
        )
        emitted = []
        with self.assertRaises(ConnectionError) as ctx:
            for chunk in stream:
                emitted.append(chunk)
        self.assertEqual(emitted, [])
        self.assertIn("RetriableError", str(ctx.exception))

    def test_stream_emits_normal_text_and_tool_call(self):
        call_tag = '<tool_call>{"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": {"command": "pwd"}}}</tool_call>'
        full = "Let me check." + call_tag

        def run_fn(on_chunk):
            on_chunk("text", "Let me check.")
            on_chunk("text", call_tag)
            return full, ""

        stream = self.mod._CursorACPStream(
            client=object(),
            model="gemini-4-hydrogen",
            tool_names=["terminal"],
            run_fn=run_fn,
        )
        chunks = list(stream)
        content = "".join(c.choices[0].delta.content for c in chunks if c.choices and c.choices[0].delta.content)
        reasoning = "".join(c.choices[0].delta.reasoning for c in chunks if c.choices and c.choices[0].delta.reasoning)
        tool_calls = [tc for c in chunks if c.choices for tc in (c.choices[0].delta.tool_calls or [])]
        self.assertEqual(content, "")
        self.assertEqual(reasoning, "Let me check.")
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0].function.name, "terminal")

    def test_stream_strips_leading_assistant_label_and_emits_final_answer(self):
        def run_fn(on_chunk):
            on_chunk("text", "Assistant:\n")
            on_chunk("text", "All checks are green.")
            return "Assistant:\nAll checks are green.", ""

        stream = self.mod._CursorACPStream(
            client=object(),
            model="gemini-4-hydrogen",
            tool_names=["terminal"],
            run_fn=run_fn,
        )
        chunks = list(stream)
        content = "".join(c.choices[0].delta.content for c in chunks if c.choices and c.choices[0].delta.content)
        self.assertEqual(content, "All checks are green.")

    def test_stream_allows_literal_user_reply(self):
        def run_fn(on_chunk):
            on_chunk("text", "user")
            return "user", ""

        stream = self.mod._CursorACPStream(
            client=object(),
            model="gemini-4-hydrogen",
            tool_names=[],
            run_fn=run_fn,
        )
        chunks = list(stream)
        content = "".join(c.choices[0].delta.content for c in chunks if c.choices and c.choices[0].delta.content)
        self.assertEqual(content, "user")

    def test_extract_content_and_images_parses_data_and_anthropic_blocks(self):
        content = [
            {"type": "text", "text": "What is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
            {"type": "input_image", "image_url": "data:image/jpeg;base64,REVm"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/webp", "data": "R0hJ"}},
            {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
        ]
        text, images = self.mod._extract_content_and_images(content)
        self.assertIn("What is this?", text)
        self.assertIn("[Image: image/png", text)
        self.assertIn("[Image URL: https://example.com/a.png]", text)
        self.assertEqual(images, [
            {"type": "image", "mimeType": "image/png", "data": "QUJD"},
            {"type": "image", "mimeType": "image/jpeg", "data": "REVm"},
            {"type": "image", "mimeType": "image/webp", "data": "R0hJ"},
        ])
        # Different image bytes produce different rendered digests for warm-session matching.
        text2, _ = self.mod._extract_content_and_images([
            {"type": "text", "text": "What is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,WFla"}},
        ])
        self.assertNotEqual(text, text2)

    def test_cursor_acp_vision_profile_capabilities_and_native_routing(self):
        from agent.image_routing import decide_image_input_mode
        from tools.vision_tools import _native_tool_result_images

        self.assertTrue(self.mod.cursor_acp.supports_vision)
        for model in ("gemini-4-hydrogen", "claude-opus-5-5", "gemini-4-hydrogen[reasoning_effort=high]"):
            self.assertEqual(decide_image_input_mode("cursor-acp", model, {}), "native")
            self.assertTrue(_native_tool_result_images("cursor-acp", model, {}))

    def test_run_prompt_sends_acp_image_blocks_fresh_and_warm(self):
        import queue
        import tempfile
        from collections import deque

        with tempfile.TemporaryDirectory() as tmp:
            cls = self.mod._make_client_class()
            client = cls(acp_cwd=tmp)
            sent_prompts = []

            class _FakeProc:
                def poll(self):
                    return None
                def terminate(self):
                    pass
                def wait(self, timeout=None):
                    pass
                def kill(self):
                    pass

            def fake_open_warm(**kw):
                return self.mod._WarmACPSession(
                    proc=_FakeProc(),
                    inbox=queue.Queue(),
                    stderr_tail=deque(),
                    stderr_pump=None,
                    request_ids=iter(range(1, 100)),
                    session_id="sess-1",
                    config_key=kw["config_key"],
                )

            client._open_warm_session = fake_open_warm

            def fake_request(sess, method, params, **kw):
                if method == "session/prompt":
                    sent_prompts.append(params["prompt"])
                    kw["text_parts"].append("looks good")
                return {}

            client._session_request = fake_request
            msg1 = {"role": "user", "content": [
                {"type": "text", "text": "describe"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
            ]}
            resp = client.chat.completions.create(model="gemini-4-hydrogen", messages=[msg1])
            self.assertEqual(resp.choices[0].message.content, "looks good")
            self.assertEqual(len(sent_prompts), 1)
            self.assertEqual(sent_prompts[0][0]["type"], "text")
            self.assertEqual(sent_prompts[0][1], {"type": "image", "mimeType": "image/png", "data": "QUJD"})

            # Turn 2 on warm session should send ONLY the new delta image.
            msg2 = {"role": "assistant", "content": "looks good"}
            msg3 = {"role": "tool", "name": "vision_analyze", "content": [
                {"type": "text", "text": "crop"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,REVm"}},
            ]}
            resp2 = client.chat.completions.create(model="gemini-4-hydrogen", messages=[msg1, msg2, msg3])
            self.assertEqual(resp2.choices[0].message.content, "looks good")
            self.assertEqual(len(sent_prompts), 2)
            self.assertEqual(sent_prompts[1][1], {"type": "image", "mimeType": "image/jpeg", "data": "REVm"})
            self.mod._ACP_POOL.close_all()

    def test_async_create_and_acreate_with_progress(self):
        import asyncio
        import tempfile
        from agent.auxiliary_client import _acreate_with_progress, _to_async_client, aux_progress_hook

        with tempfile.TemporaryDirectory() as tmp:
            cls = self.mod._make_client_class()
            client = cls(acp_cwd=tmp)
            client._run_prompt = lambda prompt_text, **kw: ("async ok", "")

            async_client, _ = _to_async_client(client, "gemini-4-hydrogen", is_vision=True)
            self.assertIs(async_client, client)

            async def _run():
                resp = await _acreate_with_progress(
                    async_client, {"model": "gemini-4-hydrogen", "messages": [{"role": "user", "content": "hi"}]},
                    task="vision", provider="cursor-acp",
                )
                self.assertEqual(resp.choices[0].message.content, "async ok")

                def _run_stream(prompt_text, **kw):
                    if kw.get("on_chunk"):
                        kw["on_chunk"]("text", "chunked ok")
                    return "chunked ok", ""

                client._run_prompt = _run_stream
                ticks = []
                with aux_progress_hook(lambda: ticks.append(1)):
                    resp_streamed = await _acreate_with_progress(
                        async_client, {"model": "gemini-4-hydrogen", "messages": [{"role": "user", "content": "hi"}]},
                        task="vision", provider="cursor-acp",
                    )
                self.assertEqual(resp_streamed.choices[0].message.content, "chunked ok")
                self.assertTrue(ticks)

            asyncio.run(_run())

    def test_extract_tool_calls_strips_tool_result_echoes_but_keeps_prose(self):
        text = (
            'Tool result:\n{"output": "noise"}\n\n'
            'Tool result (terminal):\n{"output": "more noise"}\n'
            'Checking now.<tool_call>{"id": "call_1", "type": "function", '
            '"function": {"name": "terminal", "arguments": {"command": "ls"}}}</tool_call>'
        )
        calls, cleaned = self.mod.extract_tool_calls(text, tool_names=["terminal"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].function.name, "terminal")
        self.assertEqual(cleaned, "Checking now.")

    def test_stream_strips_echoed_tool_results_before_tool_calls(self):
        call_tag = '<tool_call>{"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": {"command": "pwd"}}}</tool_call>'
        echo = 'Tool result:\n{"output": "big blob"}\n\nTool result (read_file):\n{"content": "abc"}\n'
        full = echo + call_tag

        def run_fn(on_chunk):
            on_chunk("text", 'Tool result:\n{"output": "big blob"}\n')
            on_chunk("text", 'Tool result (read_file):\n{"content": "abc"}\n')
            on_chunk("text", call_tag)
            return full, ""

        stream = self.mod._CursorACPStream(
            client=object(),
            model="gemini-4-hydrogen",
            tool_names=["terminal"],
            run_fn=run_fn,
        )
        chunks = list(stream)
        content = "".join(c.choices[0].delta.content for c in chunks if c.choices and c.choices[0].delta.content)
        tool_calls = [tc for c in chunks if c.choices for tc in (c.choices[0].delta.tool_calls or [])]
        self.assertEqual(content, "")
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0].function.name, "terminal")

    def test_stream_raises_on_pure_tool_result_echo_without_calls(self):
        echo = 'Tool result:\n{"output": "only echo"}'

        def run_fn(on_chunk):
            on_chunk("text", echo)
            return echo, ""

        stream = self.mod._CursorACPStream(
            client=object(),
            model="gemini-4-hydrogen",
            tool_names=["terminal"],
            run_fn=run_fn,
        )
        emitted = []
        with self.assertRaises(ConnectionError):
            for chunk in stream:
                emitted.append(chunk)
        self.assertEqual(emitted, [])

    def test_extract_tool_calls_normalizes_literal_newlines_and_rejects_invalid_arguments_json(self):
        import json
        # Literal newline inside arguments JSON string is losslessly normalized.
        ctrl_inner = '{"command": "echo a\necho b"}'
        text_ctrl = f'<tool_call>{json.dumps({"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": ctrl_inner}})}</tool_call>'
        calls, cleaned, errors = self.mod._extract_tool_calls_with_errors(text_ctrl, tool_names=["terminal"])
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0].function.arguments), {"command": "echo a\necho b"})
        self.assertEqual(cleaned, "")

        # Unescaped quote / invalid escape inside arguments string must be rejected, not emitted.
        bad_inner = '{"command": "curl -H "Authorization: Bearer ***" https://example.com"}'
        text_bad = f'<tool_call>{json.dumps({"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": bad_inner}})}</tool_call>'
        calls_bad, _cleaned_bad, errors_bad = self.mod._extract_tool_calls_with_errors(text_bad, tool_names=["terminal"])
        self.assertEqual(calls_bad, [])
        self.assertTrue(errors_bad)
        self.assertIn("terminal:", errors_bad[0])

    def test_run_prompt_nudges_once_on_malformed_tool_call_json_and_recovers(self):
        import json
        import queue
        import tempfile
        from collections import deque

        with tempfile.TemporaryDirectory() as tmp:
            cls = self.mod._make_client_class()
            client = cls(acp_cwd=tmp)
            sent_prompts = []

            class _FakeProc:
                def poll(self):
                    return None
                def terminate(self):
                    pass
                def wait(self, timeout=None):
                    pass
                def kill(self):
                    pass

            client._open_warm_session = lambda **kw: self.mod._WarmACPSession(
                proc=_FakeProc(), inbox=queue.Queue(), stderr_tail=deque(),
                stderr_pump=None, request_ids=iter(range(1, 100)),
                session_id="sess-bad-json", config_key=kw["config_key"],
            )

            bad_inner = '{"command": "curl -H "Authorization: Bearer ***""}'
            bad_reply = f'<tool_call>{json.dumps({"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": bad_inner}})}</tool_call>'
            good_reply = '<tool_call>{"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": {"command": "echo ok"}}}</tool_call>'

            def fake_request(sess, method, params, **kw):
                if method == "session/prompt":
                    sent_prompts.append(params["prompt"][0]["text"])
                    kw["text_parts"].append(bad_reply if len(sent_prompts) == 1 else good_reply)
                return {}

            client._session_request = fake_request
            resp = client.chat.completions.create(
                model="gemini-4-hydrogen",
                messages=[{"role": "user", "content": "run it"}],
                tools=[{"type": "function", "function": {"name": "terminal", "parameters": {}}}],
            )
            self.assertEqual(len(sent_prompts), 2)
            self.assertIn("invalid JSON", sent_prompts[1])
            self.assertEqual(resp.choices[0].finish_reason, "tool_calls")
            self.assertEqual(json.loads(resp.choices[0].message.tool_calls[0].function.arguments), {"command": "echo ok"})

            # If the retry is still malformed, raise ConnectionError rather than emitting broken JSON.
            sent_prompts.clear()
            client._session_request = lambda sess, method, params, **kw: (
                (sent_prompts.append(params["prompt"][0]["text"]) or kw["text_parts"].append(bad_reply))
                if method == "session/prompt" else None
            ) or {}
            with self.assertRaises(ConnectionError):
                client.chat.completions.create(
                    model="gemini-4-hydrogen",
                    messages=[{"role": "user", "content": "run it again"}],
                    tools=[{"type": "function", "function": {"name": "terminal", "parameters": {}}}],
                )
            self.mod._ACP_POOL.close_all()

    def test_stream_raises_on_malformed_tool_call_json(self):
        import json
        bad_inner = '{"command": "echo "hi""}'
        bad_tag = f'<tool_call>{json.dumps({"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": bad_inner}})}</tool_call>'
        stream = self.mod._CursorACPStream(
            client=object(),
            model="gemini-4-hydrogen",
            tool_names=["terminal"],
            run_fn=lambda on_chunk: (bad_tag, ""),
        )
        with self.assertRaises(ConnectionError):
            list(stream)

    def test_extract_tool_calls_accepts_call_ordinal_and_variant_tags(self):
        import json
        payload = json.dumps({
            "id": "call_1",
            "type": "function",
            "function": {"name": "terminal", "arguments": json.dumps({"command": "echo ok"})},
        })
        for open_tag, close_tag in (
            ("<call:1>", "</tool_call>"),
            ("<call:2>", "</call:2>"),
            ("<tool_call:1>", "</tool_call:1>"),
            ("<toolcall>", "</toolcall>"),
            ("<function_call>", "</function_call>"),
        ):
            with self.subTest(open_tag=open_tag, close_tag=close_tag):
                calls, cleaned, errors = self.mod._extract_tool_calls_with_errors(
                    f"{open_tag}{payload}{close_tag}", tool_names=["terminal"]
                )
                self.assertEqual(errors, [])
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].function.name, "terminal")
                self.assertEqual(cleaned, "")

    def test_stream_extracts_call_ordinal_tag_without_leaking_blob(self):
        import json
        payload = json.dumps({
            "id": "call_1",
            "type": "function",
            "function": {"name": "terminal", "arguments": json.dumps({"command": "pwd"})},
        })
        blob = f"<call:1>{payload}</tool_call>"
        stream = self.mod._CursorACPStream(
            client=object(),
            model="gemini-4-hydrogen",
            tool_names=["terminal"],
            run_fn=lambda on_chunk: (on_chunk("text", "<call:1>"), on_chunk("text", f"{payload}</tool_call>")) and (blob, ""),
        )
        chunks = list(stream)
        content = "".join(c.choices[0].delta.content for c in chunks if c.choices and c.choices[0].delta.content)
        tool_calls = [tc for c in chunks if c.choices for tc in (c.choices[0].delta.tool_calls or [])]
        self.assertEqual(content, "")
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0].function.name, "terminal")

    def test_extract_tool_calls_reports_malformed_json_inside_call_ordinal_tag(self):
        bad = '<call:1>{"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": {bad}}</call:1>'
        calls, _cleaned, errors = self.mod._extract_tool_calls_with_errors(bad, tool_names=["terminal"])
        self.assertEqual(calls, [])
        self.assertTrue(errors)

    def test_extract_tool_calls_reports_unclosed_tool_call_json_when_tools_offered(self):
        truncated = '<tool_call>{"id": "call_1", "type": "function", "function": {"name": "patch", "arguments": "{\"new_string\":'
        calls, _cleaned, errors = self.mod._extract_tool_calls_with_errors(truncated, tool_names=["patch"])
        self.assertEqual(calls, [])
        self.assertTrue(errors)

    def test_stream_raises_on_unclosed_tool_call_plus_non_retriable_error(self):
        loop_chunk = '<tool_call>{"id": "call_1", "type": "function", "function": {"name": "patch", "arguments": "{\"new_string\":\"    await page.hover'
        err_chunk = "\n\nError: NonRetriableError: Agent Looping Detected The model got stuck in a repeating response pattern, so this turn was stopped."
        full = loop_chunk + loop_chunk + err_chunk

        def run_fn(on_chunk):
            on_chunk("text", loop_chunk)
            on_chunk("text", loop_chunk)
            on_chunk("text", err_chunk)
            return full, ""

        stream = self.mod._CursorACPStream(
            client=object(),
            model="gemini-4-hydrogen",
            tool_names=["patch"],
            run_fn=run_fn,
        )
        emitted = []
        with self.assertRaises(ConnectionError) as ctx:
            for chunk in stream:
                emitted.append(chunk)
        self.assertEqual(emitted, [])
        self.assertIn("NonRetriableError", str(ctx.exception))

    def test_stream_strips_localized_user_timestamp_wrapper_before_tool_call(self):
        call_tag = '<tool_call>{"id": "call_1", "type": "function", "function": {"name": "read_file", "arguments": {"path": "/tmp/a"}}}</tool_call>'
        wrapper = "ユーザー\n<timestamp>Tuesday, Nov 17, 2026, 4:11 PM (UTC-5)</timestamp>\n<user_query>"
        full = wrapper + call_tag

        def run_fn(on_chunk):
            on_chunk("text", "ユーザー")
            on_chunk("text", "\n<timestamp>Tuesday, Nov 17, 2026, 4:11 PM (UTC-5)</timestamp>\n<user_query>")
            on_chunk("text", call_tag)
            return full, ""

        stream = self.mod._CursorACPStream(
            client=object(),
            model="gemini-4-hydrogen",
            tool_names=["read_file"],
            run_fn=run_fn,
        )
        chunks = list(stream)
        content = "".join(c.choices[0].delta.content for c in chunks if c.choices and c.choices[0].delta.content)
        tool_calls = [tc for c in chunks if c.choices for tc in (c.choices[0].delta.tool_calls or [])]
        self.assertEqual(content, "")
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0].function.name, "read_file")

    def test_stream_raises_on_pure_localized_wrapper_echo(self):
        wrapper = "ユーザー\n<timestamp>Tuesday, Nov 17, 2026, 4:11 PM (UTC-5)</timestamp>\n<user_query>"

        def run_fn(on_chunk):
            on_chunk("text", "ユーザー")
            on_chunk("text", "\n<timestamp>Tuesday, Nov 17, 2026, 4:11 PM (UTC-5)</timestamp>\n<user_query>")
            return wrapper, ""

        stream = self.mod._CursorACPStream(
            client=object(),
            model="gemini-4-hydrogen",
            tool_names=["read_file"],
            run_fn=run_fn,
        )
        emitted = []
        with self.assertRaises(ConnectionError):
            for chunk in stream:
                emitted.append(chunk)
        self.assertEqual(emitted, [])

    def test_stream_allows_short_first_line_followed_by_normal_prose(self):
        def run_fn(on_chunk):
            on_chunk("text", "Yes")
            on_chunk("text", "\nI checked it.")
            return "Yes\nI checked it.", ""

        stream = self.mod._CursorACPStream(
            client=object(),
            model="gemini-4-hydrogen",
            tool_names=[],
            run_fn=run_fn,
        )
        chunks = list(stream)
        content = "".join(c.choices[0].delta.content for c in chunks if c.choices and c.choices[0].delta.content)
        self.assertEqual(content, "Yes\nI checked it.")

    def test_prompts_use_turn_tags_without_assistant_role_labels(self):
        messages = [
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "name": "terminal", "content": "ok"},
        ]
        prompt = self.mod._format_prompt(messages, None, None)
        self.assertNotIn("Assistant:", prompt)
        self.assertNotIn("Now continue as the Assistant", prompt)
        self.assertIn('<turn role="user">', prompt)
        self.assertIn('<turn role="tool" name="terminal">', prompt)
        delta = self.mod._format_delta_prompt(messages[1:], ["terminal"])
        self.assertNotIn("Assistant:", delta)
        self.assertNotIn("Now continue as the Assistant", delta)
        self.assertIn('<turn role="assistant">', delta)
        self.assertIn('<turn role="tool" name="terminal">', delta)
        self.assertIn("Continue the response.", delta)

    def test_extract_tool_calls_strips_assistant_prefixes_outside_fences(self):
        text = (
            "Assistant:\nChecking now.\n\n"
            "Assistant: Let's run it.\n"
            "```text\nAssistant: keep inside fence\n```\n"
            '<tool_call>{"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}</tool_call>'
        )
        calls, cleaned = self.mod.extract_tool_calls(text, tool_names=["terminal"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            cleaned,
            "Checking now.\n\nLet's run it.\n```text\nAssistant: keep inside fence\n```",
        )

    def test_stream_strips_leading_and_mid_reply_assistant_prefixes(self):
        def run_fn(on_chunk):
            chunks = [
                "Ass",
                "istant:\nChecking now.\n\n",
                "Assistant: ",
                "Next step.\n",
                "```\nAssistant: inside fence\n```\nDone.",
            ]
            for ch in chunks:
                on_chunk("text", ch)
            return "".join(chunks), ""

        for tool_names in ([], ["terminal"]):
            stream = self.mod._CursorACPStream(
                client=object(),
                model="gemini-4-hydrogen",
                tool_names=tool_names,
                run_fn=run_fn,
            )
            out = list(stream)
            text = "".join(c.choices[0].delta.content or "" for c in out if c.choices)
            self.assertEqual(
                text,
                "Checking now.\n\nNext step.\n```\nAssistant: inside fence\n```\nDone.",
            )

    def test_canned_greeting_glitch_detection_and_pool_eviction(self):
        import queue
        import tempfile
        from collections import deque

        self.assertTrue(self.mod._is_canned_greeting_glitch(
            "Hey! What can I help you with?",
            "**Planning**\nI will summarize the result.",
            messages=[{"role": "user", "content": "thanks!"}],
        ))
        self.assertTrue(self.mod._is_canned_greeting_glitch(
            "Hey! What can I help you with?",
            "",
            messages=[{"role": "tool", "content": "ok"}],
        ))
        self.assertTrue(self.mod._is_canned_greeting_glitch(
            "Hey! What can I help you with?",
            "",
            messages=[{"role": "user", "content": "merge both PR and pull\n[voice-web: page open]"}],
        ))
        self.assertFalse(self.mod._is_canned_greeting_glitch(
            "Hey! What can I help you with?",
            "",
            messages=[{"role": "user", "content": "hi"}],
        ))

        with tempfile.TemporaryDirectory() as tmp:
            cls = self.mod._make_client_class()
            client = cls(acp_cwd=tmp)
            terminated = []

            class _FakeProc:
                def poll(self):
                    return None
                def terminate(self):
                    terminated.append(True)
                def wait(self, timeout=None):
                    pass
                def kill(self):
                    pass

            def fake_open_warm(**kw):
                return self.mod._WarmACPSession(
                    proc=_FakeProc(),
                    inbox=queue.Queue(),
                    stderr_tail=deque(maxlen=40),
                    stderr_pump=None,
                    request_ids=iter(range(1, 100)),
                    session_id="s1",
                    config_key=kw["config_key"],
                )

            client._open_warm_session = fake_open_warm
            def fake_session_request(sess, method, params, **kw):
                if method == "session/prompt":
                    kw["reasoning_parts"].append("**Thinking**\nDone with the fix.")
                    kw["text_parts"].append("Hey! What can I help you with?")
                return {}

            client._session_request = fake_session_request
            self.mod._ACP_POOL.close_all()
            try:
                with self.assertRaises(ConnectionError):
                    client._run_prompt(
                        "full prompt",
                        timeout_seconds=5,
                        model="gemini-4-hydrogen",
                        tool_names=["terminal"],
                        messages=[{"role": "tool", "content": "ok"}],
                        tools_signature="sig",
                    )
                self.assertEqual(len(self.mod._ACP_POOL._sessions), 0)
                self.assertTrue(terminated)
            finally:
                self.mod._ACP_POOL.close_all()

    def test_stream_holds_canned_greeting_until_done_when_no_tools(self):
        def run_fn_fail(on_chunk):
            on_chunk("text", "Hey! What can I help you with?")
            raise ConnectionError("Cursor ACP emitted a canned greeting instead of an assistant response.")

        stream = self.mod._CursorACPStream(
            client=object(),
            model="gemini-4-hydrogen",
            tool_names=[],
            run_fn=run_fn_fail,
        )
        emitted = []
        with self.assertRaises(ConnectionError):
            for chunk in stream:
                emitted.append(chunk)
        self.assertEqual(emitted, [])

        def run_fn_ok(on_chunk):
            on_chunk("text", "Hey! What can I help you with?")
            return "Hey! What can I help you with?", ""

        stream_ok = self.mod._CursorACPStream(
            client=object(),
            model="gemini-4-hydrogen",
            tool_names=[],
            run_fn=run_fn_ok,
        )
        chunks = list(stream_ok)
        content = "".join(c.choices[0].delta.content for c in chunks if c.choices and c.choices[0].delta.content)
        self.assertEqual(content, "Hey! What can I help you with?")

    def test_extract_tool_calls_accepts_namespaced_call_tags(self):
        import json

        cmd_blob = '<call:default_api:terminal>{"command":"for p in 1908 1899 1890; do echo $p; done"}</call:default_api:terminal>'
        calls, cleaned, errors = self.mod._extract_tool_calls_with_errors(
            cmd_blob, tool_names=["terminal"]
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].function.name, "terminal")
        self.assertEqual(json.loads(calls[0].function.arguments), {"command": "for p in 1908 1899 1890; do echo $p; done"})
        self.assertEqual(cleaned, "")

        # A tool whose own argument is "name" must not be misclassified as a flat envelope.
        name_arg_blob = '<call:default_api:skill_view>{"name": "ptal"}</call:default_api:skill_view>'
        calls_sv, cleaned_sv, errors_sv = self.mod._extract_tool_calls_with_errors(
            name_arg_blob, tool_names=["skill_view"]
        )
        self.assertEqual(errors_sv, [])
        self.assertEqual(len(calls_sv), 1)
        self.assertEqual(calls_sv[0].function.name, "skill_view")
        self.assertEqual(json.loads(calls_sv[0].function.arguments), {"name": "ptal"})
        self.assertEqual(cleaned_sv, "")

        # Named tag wrapping a full OpenAI envelope still uses the envelope.
        env_payload = json.dumps({
            "id": "c1",
            "type": "function",
            "function": {"name": "terminal", "arguments": json.dumps({"command": "pwd"})},
        })
        env_blob = f'<call:terminal>{env_payload}</call:terminal>'
        calls_env, cleaned_env, errors_env = self.mod._extract_tool_calls_with_errors(
            env_blob, tool_names=["terminal"]
        )
        self.assertEqual(errors_env, [])
        self.assertEqual(len(calls_env), 1)
        self.assertEqual(calls_env[0].id, "c1")
        self.assertEqual(calls_env[0].function.name, "terminal")
        self.assertEqual(cleaned_env, "")

        # Self-closing named tag and unoffered tool tag.
        self_closing = '<call:default_api:terminal/>'
        calls_sc, cleaned_sc, errors_sc = self.mod._extract_tool_calls_with_errors(
            self_closing, tool_names=["terminal"]
        )
        self.assertEqual(errors_sc, [])
        self.assertEqual(len(calls_sc), 1)
        self.assertEqual(calls_sc[0].function.name, "terminal")
        self.assertEqual(json.loads(calls_sc[0].function.arguments), {})
        self.assertEqual(cleaned_sc, "")

        unoffered = '<call:default_api:not_offered>{"x": 1}</call:default_api:not_offered>'
        calls_uo, cleaned_uo, errors_uo = self.mod._extract_tool_calls_with_errors(
            unoffered, tool_names=["terminal"]
        )
        self.assertEqual(errors_uo, [])
        self.assertEqual(calls_uo, [])
        self.assertEqual(cleaned_uo, unoffered)

    def test_stream_extracts_namespaced_call_tag_without_leaking(self):
        import json

        blob = '<call:default_api:terminal>{"command": "echo ok"}</call:default_api:terminal>'
        stream = self.mod._CursorACPStream(
            client=object(),
            model="gemini-4-hydrogen",
            tool_names=["terminal"],
            run_fn=lambda on_chunk: (on_chunk("text", "Checking. "), on_chunk("text", blob)) and (f"Checking. {blob}", ""),
        )
        chunks = list(stream)
        content = "".join(c.choices[0].delta.content for c in chunks if c.choices and c.choices[0].delta.content)
        tool_calls = [tc for c in chunks if c.choices for tc in (c.choices[0].delta.tool_calls or [])]
        self.assertEqual(content, "")
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0].function.name, "terminal")
        self.assertEqual(json.loads(tool_calls[0].function.arguments), {"command": "echo ok"})


if __name__ == "__main__":
    unittest.main()
