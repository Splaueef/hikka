"""Exercise terminal-script methods without importing optional Telegram dependencies."""

import ast
import asyncio
import contextlib
import os
from pathlib import Path
import re
import shlex
import signal
import tempfile
import time
import typing
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "hikka/modules/terminal.py"
METHODS = {
    "_split_pipeline",
    "_parse_script_interval",
    "_parse_terminal_script",
    "_parse_watch_script",
    "_validate_script_pipeline",
    "_script_format_vars",
    "_script_shell",
    "_execute_script",
    "_script_read_file_delta",
    "_script_delta_reader_command",
    "_run_script_pipeline",
}
tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
module = next(
    node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TerminalMod"
)
selected = [node for node in module.body if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in METHODS]
isolated = ast.Module(
    body=[
        ast.ClassDef(
            name="ScriptMethods",
            bases=[],
            keywords=[],
            body=selected,
            decorator_list=[],
        )
    ],
    type_ignores=[],
)
ast.fix_missing_locations(isolated)
namespace = {
    "asyncio": asyncio,
    "contextlib": contextlib,
    "os": os,
    "re": re,
    "shlex": shlex,
    "signal": signal,
    "time": time,
    "typing": typing,
    "SCRIPT_VARIABLE_RE": re.compile(r"(?<!\\)\$(script|event|file|path|watched)\b"),
    "redact_sensitive_text": lambda text: text,
    "clean_terminal_output": lambda text: text,
    "logger": type("Logger", (), {"exception": lambda *args: None})(),
}
exec(compile(isolated, str(SOURCE), "exec"), namespace)


class TerminalScriptsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mod = namespace["ScriptMethods"]()
        self.mod.config = {
            "SCRIPTS_OUTPUT_BYTES": 4096,
            "SCRIPTS_COMMAND_TIMEOUT": 1,
        }
        self.mod._script_status = {}
        self.mod._script_read_offsets = {}
        self.mod._shell = lambda: "/bin/sh"
        self.mod._get_cwd = lambda: tempfile.gettempdir()
        self.mod._resolve_path = lambda path: os.path.abspath(path)

    def test_parser_preserves_quoted_separators_and_rejects_empty_stages(self):
        self.assertEqual(
            self.mod._split_pipeline('run "printf \'a |> b\'" |> notify me'),
            ['run "printf \'a |> b\'"', "notify me"],
        )
        script = self.mod._parse_terminal_script(
            'watch file:/tmp/a.log on:change -> cat $file |> notify me'
        )
        self.assertEqual(script["event"], "change")
        self.assertEqual(script["type"], "watch")
        for source in (
            "on time:every 3min -> notify telegram",
            "on time:every 3min -> date |>",
            "on time:every 0s -> notify me",
            'on time:every 3min -> run "unclosed',
        ):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.mod._parse_terminal_script(source)

    def test_variable_substitution_is_whole_name_and_quotes_shell_args(self):
        result = self.mod._script_format_vars(
            "printf '%s' $file $path $filename",
            {"file": "/tmp/a b;hi", "path": "/tmp/next"},
            quote=True,
        )
        self.assertEqual(result, "printf '%s' '/tmp/a b;hi' /tmp/next $filename")

    async def test_shell_reports_failure_and_times_out(self):
        with self.assertRaisesRegex(RuntimeError, "shell exited 7"):
            await self.mod._script_shell("echo bad >&2; exit 7", {})
        started = time.monotonic()
        with self.assertRaises(asyncio.TimeoutError):
            await self.mod._script_shell("sleep 5", {})
        self.assertLess(time.monotonic() - started, 3)

    async def test_event_failure_does_not_escape_executor(self):
        async def fail(*_args):
            raise ValueError("bad event")

        self.mod._run_script_pipeline = fail
        self.assertFalse(await self.mod._execute_script("demo", {}, {"event": "change"}))
        self.assertEqual(self.mod._script_status["demo"]["error"], "bad event")

    async def test_watcher_reads_only_appended_data_and_test_reads_full_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log with spaces.txt"
            path.write_text("existing\n", encoding="utf-8")
            self.mod._script_read_offsets["log"] = {str(path): path.stat().st_size}
            path.write_text("existing\nadded\n", encoding="utf-8")
            outputs = []
            self.mod._script_notify = (
                lambda _stage, _variables, payload: self._capture(outputs, payload)
            )
            script = {"pipeline": "cat $file |> notify me"}
            variables = {"file": str(path), "event": "change"}
            await self.mod._run_script_pipeline("log", script, variables)
            self.assertEqual(outputs, ["added\n"])
            variables["event"] = "test"
            await self.mod._run_script_pipeline("log", script, variables)
            self.assertEqual(outputs[-1], "existing\nadded\n")

    @staticmethod
    async def _capture(outputs, payload):
        outputs.append(payload)


if __name__ == "__main__":
    unittest.main()
