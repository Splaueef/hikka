#    Friendly Telegram (telegram userbot)
#    Copyright (C) 2018-2019 The Authors

#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.

#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU Affero General Public License for more details.

#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see <https://www.gnu.org/licenses/>.

# ©️ Dan Gazizullin, 2021-2023
# This file is a part of Hikka Userbot
# 🌐 https://github.com/Splaueef/hikka
# You can redistribute it and/or modify it under the terms of the GNU AGPLv3
# 🔑 https://www.gnu.org/licenses/agpl-3.0.html

# meta developer: @bsolute

import asyncio
import contextlib
import logging
import os
import pty
import re
import shlex
import tempfile
import time
import typing
import uuid
from pathlib import Path

import hikkatl

from .. import loader, utils

logger = logging.getLogger(__name__)


def hash_msg(message):
    return f"{str(utils.get_chat_id(message))}/{str(message.id)}"


async def read_stream(func: callable, stream, delay: float):
    last_task = None
    data = b""
    while True:
        dat = await stream.read(1)

        if not dat:
            # EOF
            if last_task:
                # Send all pending data
                last_task.cancel()
                await func(data.decode(errors="replace"))
                # If there is no last task there is inherently no data, so theres no point sending a blank string
            break

        data += dat

        if last_task:
            last_task.cancel()

        last_task = asyncio.ensure_future(sleep_for_task(func, data, delay))


async def read_pty_stream(func: callable, fd: int, delay: float):
    loop = asyncio.get_running_loop()
    last_task = None
    data = b""

    while True:
        future = loop.create_future()

        def _read_ready():
            if not future.done():
                future.set_result(None)

        loop.add_reader(fd, _read_ready)
        try:
            await future
        finally:
            loop.remove_reader(fd)

        try:
            dat = os.read(fd, 1024)
        except OSError:
            dat = b""

        if not dat:
            if last_task:
                last_task.cancel()
                await func(data.decode(errors="replace"))
            break

        data += dat

        if last_task:
            last_task.cancel()

        last_task = asyncio.ensure_future(sleep_for_task(func, data, delay))


async def sleep_for_task(func: callable, data: bytes, delay: float):
    await asyncio.sleep(delay)
    await func(data.decode(errors="replace"))


class MessageEditor:
    def __init__(
        self,
        message: hikkatl.tl.types.Message,
        command: str,
        config,
        strings,
        request_message,
        cwd: typing.Optional[str] = None,
    ):
        self.message = message
        self.command = command
        self.stdout = ""
        self.stderr = ""
        self.rc = None
        self.redraws = 0
        self.config = config
        self.strings = strings
        self.request_message = request_message
        self.cwd = cwd

    async def update_stdout(self, stdout):
        self.stdout = stdout
        await self.redraw()

    async def update_stderr(self, stderr):
        self.stderr = stderr
        await self.redraw()

    async def redraw(self):
        text = self.strings("running").format(utils.escape_html(self.command))  # fmt: skip

        if self.cwd:
            text += self.strings("cwd").format(utils.escape_html(self.cwd))

        if self.rc is not None:
            text += self.strings("finished").format(utils.escape_html(str(self.rc)))

        stdout = utils.escape_html(self.stdout[max(len(self.stdout) - 2048, 0) :])
        text += self.strings("stdout")
        text += self.strings("quote").format(stdout or " ")
        stderr = utils.escape_html(self.stderr[max(len(self.stderr) - 1024, 0) :])
        text += (
            self.strings("stderr") + self.strings("quote").format(stderr)
            if stderr
            else ""
        )
        text += self.strings("end")

        with contextlib.suppress(hikkatl.errors.rpcerrorlist.MessageNotModifiedError):
            try:
                self.message = await utils.answer(self.message, text)
            except hikkatl.errors.rpcerrorlist.MessageTooLongError as e:
                logger.error(e)
                logger.error(text)
        # The message is never empty due to the template header

    async def cmd_ended(self, rc, cwd: typing.Optional[str] = None):
        self.rc = rc
        if cwd:
            self.cwd = cwd
        self.state = 4
        await self.redraw()

    def update_process(self, process, input_writer=None):
        pass


class SudoMessageEditor(MessageEditor):
    # Let's just hope these are safe to parse
    PASS_REQ = "[sudo] password for"
    WRONG_PASS = r"\[sudo\] password for (.*): Sorry, try again\."
    TOO_MANY_TRIES = (r"\[sudo\] password for (.*): sudo: [0-9]+ incorrect password attempts")  # fmt: skip
    GENERIC_AUTH_PROMPTS = (
        re.compile(r"(?im)(?:^|[\r\n])[^\r\n]{0,160}(?:password|passphrase|pin|otp|verification code|2fa)[^\r\n:]{0,160}:\s*$"),
        re.compile(r"(?im)(?:^|[\r\n])[^\r\n]{0,160}(?:continue connecting|yes/no(?:/\[fingerprint\])?)[^\r\n?]{0,160}\?\s*$"),
    )

    def __init__(
        self,
        message,
        command,
        config,
        strings,
        request_message,
        cwd=None,
        tg_id=None,
    ):
        super().__init__(message, command, config, strings, request_message, cwd)
        self._tg_id = tg_id
        self.process = None
        self._input_writer = None
        self.state = 0
        self.authmsg = None
        self._last_prompt = None
        self._auth_handler_registered = False

    def update_process(self, process, input_writer=None):
        logger.debug("got sproc obj %s", process)
        self.process = process
        self._input_writer = input_writer or self._write_to_process_stdin

    def _write_to_process_stdin(self, data: bytes):
        if self.process and self.process.stdin:
            self.process.stdin.write(data)

    @staticmethod
    def _last_nonempty_line(text: str) -> str:
        normalized = text.replace("\r", "\n")
        lines = [line for line in normalized.split("\n") if line.strip()]
        return lines[-1].strip() if lines else ""

    def _generic_prompt(self, output: str) -> typing.Optional[str]:
        tail = output[-500:]
        for pattern in self.GENERIC_AUTH_PROMPTS:
            match = pattern.search(tail)
            if match:
                return self._last_nonempty_line(match.group(0))

        return None

    async def _request_auth(self, prompt: str):
        self._last_prompt = prompt
        text = self.strings("auth_needed").format(self._tg_id)

        try:
            await utils.answer(self.message, text)
        except hikkatl.errors.rpcerrorlist.MessageNotModifiedError as e:
            logger.debug(e)

        command = "<code>" + utils.escape_html(self.command) + "</code>"
        self.authmsg = await self.message[0].client.send_message(
            "me",
            self.strings("auth_msg").format(
                utils.escape_html(prompt),
                command,
            ),
        )

        if self._auth_handler_registered:
            self.message[0].client.remove_event_handler(self.on_message_edited)

        self.message[0].client.add_event_handler(
            self.on_message_edited,
            hikkatl.events.messageedited.MessageEdited(chats=["me"]),
        )
        self._auth_handler_registered = True

    async def update_stderr(self, stderr):
        logger.debug("stderr update " + stderr)
        self.stderr = stderr
        await self._handle_output_update(stderr, is_stderr=True)

    async def update_stdout(self, stdout):
        self.stdout = stdout
        await self._handle_output_update(stdout, is_stderr=False)

    async def _handle_output_update(self, output: str, is_stderr: bool):
        lines = output.strip().split("\n")
        lastline = lines[-1] if lines else ""
        lastlines = lastline.rsplit(" ", 1)
        handled = False

        if (
            is_stderr
            and len(lines) > 1
            and re.fullmatch(self.WRONG_PASS, lines[-2])
            and lastlines[0] == self.PASS_REQ
            and self.state == 1
        ):
            logger.debug("switching state to 0")
            await self.authmsg.edit(self.strings("auth_failed"))
            self.state = 0
            handled = True
            await asyncio.sleep(2)
            await self.authmsg.delete()
            self.authmsg = None

        prompt = None
        if is_stderr and lastlines[0] == self.PASS_REQ:
            prompt = lastlines[1][:-1]
        else:
            prompt = self._generic_prompt(output)

        if prompt and self.state == 1:
            logger.debug("interactive auth prompt repeated after submitted response")
            if self.authmsg is not None:
                await self.authmsg.edit(self.strings("auth_failed"))
                await asyncio.sleep(2)
                await self.authmsg.delete()
                self.authmsg = None
            self.state = 0
            self._last_prompt = None

        if prompt and self.state != 1 and prompt != self._last_prompt:
            logger.debug("interactive auth prompt detected: %s", prompt)
            await self._request_auth(prompt)
            self.state = 0
            handled = True

        if is_stderr and len(lines) > 1 and (
            re.fullmatch(self.TOO_MANY_TRIES, lastline) and self.state in {1, 3, 4}
        ):
            logger.debug("password wrong lots of times")
            await utils.answer(self.message, self.strings("auth_locked"))
            if self.authmsg is not None:
                await self.authmsg.delete()
                self.authmsg = None
            self.state = 2
            handled = True

        if not handled:
            if not is_stderr and self.state != 2:
                self.state = 3  # Means that we got stdout only

            await self.redraw()

        logger.debug(self.state)

    async def on_message_edited(self, message):
        # Message contains sensitive information.
        if self.authmsg is None:
            return

        logger.debug("got message edit update in self %s", str(message.id))

        if hash_msg(message) == hash_msg(self.authmsg):
            # The user has provided interactive authentication. Send secret to stdin.
            try:
                self.authmsg = await utils.answer(message, self.strings("auth_ongoing"))
            except hikkatl.errors.rpcerrorlist.MessageNotModifiedError:
                # Try to clear personal info if the edit fails
                await message.delete()

            self.state = 1
            response = message.message.message.split("\n", 1)[0].encode() + b"\n"
            self._input_writer(response)


class RawMessageEditor(SudoMessageEditor):
    def __init__(
        self,
        message,
        command,
        config,
        strings,
        request_message,
        show_done=False,
        cwd=None,
        tg_id=None,
    ):
        super().__init__(message, command, config, strings, request_message, cwd, tg_id)
        self.show_done = show_done

    async def redraw(self):
        logger.debug(self.rc)

        if self.rc is None:
            text = self.strings("quote").format(
                utils.escape_html(self.stdout[max(len(self.stdout) - 4095, 0) :]) or " "
            )
        elif self.rc == 0:
            text = self.strings("quote").format(
                utils.escape_html(self.stdout[max(len(self.stdout) - 4090, 0) :]) or " "
            )
        else:
            text = self.strings("quote").format(
                utils.escape_html(self.stderr[max(len(self.stderr) - 4095, 0) :]) or " "
            )

        if self.rc is not None and self.show_done:
            text += "\n" + self.strings("done")

        logger.debug(text)

        with contextlib.suppress(
            hikkatl.errors.rpcerrorlist.MessageNotModifiedError,
            hikkatl.errors.rpcerrorlist.MessageEmptyError,
            ValueError,
        ):
            try:
                await utils.answer(self.message, text)
            except hikkatl.errors.rpcerrorlist.MessageTooLongError as e:
                logger.error(e)
                logger.error(text)


@loader.tds
class TerminalMod(loader.Module):
    """Runs commands"""

    strings = {
        "name": "Terminal",
        "fw_protect": "How long to wait in seconds between edits in commands",
        "timeout_cfg": "Maximum command runtime in seconds. 0 disables timeout",
        "interactive_tty_cfg": (
            "Run terminal commands in a pseudo-terminal so interactive password prompts work"
        ),
        "history_limit_cfg": (
            "How many terminal commands to keep in history. 0 disables history"
        ),
        "scripts_poll_cfg": "How often terminal scripts poll watched files, in seconds",
        "scripts_output_limit_cfg": (
            "Maximum characters from a script command to send to Telegram"
        ),
        "what_to_kill": (
            "<emoji document_id=5210952531676504517>🚫</emoji> <b>Reply to a terminal command to terminate it</b>"
        ),
        "kill_fail": (
            "<emoji document_id=5210952531676504517>🚫</emoji> <b>Could not kill process</b>"
        ),
        "killed": "<emoji document_id=5210952531676504517>🚫</emoji> <b>Killed</b>",
        "no_cmd": (
            "<emoji document_id=5210952531676504517>🚫</emoji> <b>No command is running in that message</b>"
        ),
        "running": (
            "<emoji document_id=5472111548572900003>⌨️</emoji><b> System call</b> <code>{}</code>"
        ),
        "cwd": "\n<b>📁 Directory:</b> <code>{}</code>",
        "finished": "\n<b>Exit code</b> <code>{}</code>",
        "stdout": "\n<b>📼 Stdout:</b>",
        "stderr": (
            "\n\n<b><emoji document_id=5210952531676504517>🚫</emoji> Stderr:</b>"
        ),
        "quote": "\n<blockquote>{}</blockquote>",
        "end": "",
        "auth_fail": (
            "<emoji document_id=5210952531676504517>🚫</emoji> <b>Authentication failed, please try again</b>"
        ),
        "auth_failed": (
            "<emoji document_id=5210952531676504517>🚫</emoji> <b>Authentication failed, please try again</b>"
        ),
        "auth_needed": (
            '<emoji document_id=5472308992514464048>🔐</emoji><a href="tg://user?id={}"> Interactive authentication required</a>'
        ),
        "auth_msg": (
            "<emoji document_id=5472308992514464048>🔐</emoji> <b>Please edit this message to the password for</b> <code>{}</code> <b>to run</b> <code>{}</code>"
        ),
        "auth_locked": (
            "<emoji document_id=5210952531676504517>🚫</emoji> <b>Authentication failed, please try again later</b>"
        ),
        "auth_ongoing": (
            "<emoji document_id=5213452215527677338>⏳</emoji> <b>Authenticating...</b>"
        ),
        "done": "<emoji document_id=5314250708508220914>✅</emoji> <b>Done</b>",
        "pwd": (
            "<emoji document_id=5472111548572900003>📁</emoji> <b>Current terminal directory:</b> <code>{}</code>"
        ),
        "cd": (
            "<emoji document_id=5472111548572900003>📁</emoji> <b>Directory changed to:</b> <code>{}</code>"
        ),
        "cd_error": (
            "<emoji document_id=5210952531676504517>🚫</emoji> <b>No such directory:</b> <code>{}</code>"
        ),
        "empty_command": (
            "<emoji document_id=5472111548572900003>⌨️</emoji> <b>Usage:</b> <code>.t &lt;command&gt;</code>\n<b>Current directory:</b> <code>{}</code>"
        ),
        "timeout": "\nCommand timed out after {} seconds and was terminated.",
        "history_empty": (
            "<emoji document_id=5472111548572900003>⌨️</emoji> <b>Terminal history is empty</b>"
        ),
        "history": (
            "<emoji document_id=5472111548572900003>⌨️</emoji> <b>Terminal history:</b>\n{}"
        ),
        "history_item": "<code>{}</code>. <code>{}</code>",
        "history_invalid": (
            "<emoji document_id=5210952531676504517>🚫</emoji> <b>No command with this history number</b>"
        ),
        "terminal_mode_usage": (
            "<emoji document_id=5472111548572900003>⌨️</emoji> <b>Usage:</b> "
            "<code>.t-rg &lt;time|off&gt;</code>"
        ),
        "terminal_mode_enabled": (
            "<emoji document_id=5314250708508220914>✅</emoji> <b>Terminal mode enabled for {}</b>\n"
            "<b>Now unknown dot-commands in this chat will be executed as shell commands.</b>"
        ),
        "terminal_mode_disabled": (
            "<emoji document_id=5314250708508220914>✅</emoji> <b>Terminal mode disabled in this chat</b>"
        ),
        "terminal_mode_invalid_time": (
            "<emoji document_id=5210952531676504517>🚫</emoji> <b>Specify time like</b> "
            "<code>30m</code>, <code>2h</code>, <code>1d</code> <b>or</b> <code>off</code>"
        ),
        "script_usage": (
            "<emoji document_id=5472111548572900003>🤖</emoji> <b>Terminal scripts</b>\n"
            "<code>.ts add &lt;name&gt; = watch dir:/path on:change -> cat $file |> notify telegram -100123</code>\n"
            '<code>.ts add &lt;name&gt; = on time:every 5min -> run "date" |> notify me</code>\n'
            "<code>.ts list|show|start|stop|del &lt;name&gt;</code>"
        ),
        "script_saved": (
            "<emoji document_id=5314250708508220914>✅</emoji> <b>Script</b> "
            "<code>{}</code> <b>saved and started</b>"
        ),
        "script_deleted": (
            "<emoji document_id=5314250708508220914>✅</emoji> <b>Script</b> "
            "<code>{}</code> <b>deleted</b>"
        ),
        "script_started": (
            "<emoji document_id=5314250708508220914>✅</emoji> <b>Script</b> "
            "<code>{}</code> <b>started</b>"
        ),
        "script_stopped": (
            "<emoji document_id=5314250708508220914>✅</emoji> <b>Script</b> "
            "<code>{}</code> <b>stopped</b>"
        ),
        "script_not_found": (
            "<emoji document_id=5210952531676504517>🚫</emoji> <b>Script not found:</b> "
            "<code>{}</code>"
        ),
        "script_invalid": (
            "<emoji document_id=5210952531676504517>🚫</emoji> <b>Invalid script:</b> "
            "<code>{}</code>"
        ),
        "scripts_empty": (
            "<emoji document_id=5472111548572900003>🤖</emoji> <b>No terminal scripts saved</b>"
        ),
        "scripts_list": (
            "<emoji document_id=5472111548572900003>🤖</emoji> <b>Terminal scripts:</b>\n{}"
        ),
        "script_item": "{} <code>{}</code> — <code>{}</code>",
        "script_show": (
            "<emoji document_id=5472111548572900003>🤖</emoji> <b>Script</b> "
            "<code>{}</code> <b>({})</b>\n<blockquote>{}</blockquote>"
        ),
        "_cmd_doc_apt": "Shorthand for '.terminal apt'",
        "_cmd_doc_cd": "[path] - Change persistent terminal directory",
        "_cmd_doc_history": (
            "Show terminal command history. Use .t !N to rerun an entry"
        ),
        "_cmd_doc_pwd": "Show persistent terminal directory",
        "_cmd_doc_terminal": (
            "<command> - Execute shell command (alias: .t). Use !N to rerun history item N"
        ),
        "_cmd_doc_terminalmode": (
            "<time|off> - Enable terminal mode in current chat (alias: .t-rg). "
            "Unknown dot-commands will be executed as shell commands"
        ),
        "_cmd_doc_termscript": (
            "add|list|show|start|stop|del - Manage saved background terminal scripts (alias: .ts)"
        ),
        "_cmd_doc_terminate": (
            "[-f to force kill] - Use in reply to send SIGTERM to a process"
        ),
        "_cls_doc": "Runs commands",
    }

    def __init__(self):
        self.config = loader.ModuleConfig(
            loader.ConfigValue(
                "FLOOD_WAIT_PROTECT",
                2,
                lambda: self.strings("fw_protect"),
                validator=loader.validators.Integer(minimum=0),
            ),
            loader.ConfigValue(
                "COMMAND_TIMEOUT",
                0,
                lambda: self.strings("timeout_cfg"),
                validator=loader.validators.Integer(minimum=0),
            ),
            loader.ConfigValue(
                "INTERACTIVE_TTY",
                True,
                lambda: self.strings("interactive_tty_cfg"),
                validator=loader.validators.Boolean(),
            ),
            loader.ConfigValue(
                "HISTORY_LIMIT",
                50,
                lambda: self.strings("history_limit_cfg"),
                validator=loader.validators.Integer(minimum=0),
            ),
            loader.ConfigValue(
                "SCRIPTS_POLL_INTERVAL",
                2,
                lambda: self.strings("scripts_poll_cfg"),
                validator=loader.validators.Integer(minimum=1),
            ),
            loader.ConfigValue(
                "SCRIPTS_OUTPUT_LIMIT",
                3500,
                lambda: self.strings("scripts_output_limit_cfg"),
                validator=loader.validators.Integer(minimum=256, maximum=4096),
            ),
        )
        self.activecmds = {}
        self._script_tasks = {}
        self._script_snapshots = {}

    def _default_cwd(self) -> str:
        return os.path.abspath(utils.get_base_dir())

    def _get_cwd(self) -> str:
        cwd = os.path.abspath(
            os.path.expandvars(os.path.expanduser(self.get("cwd", self._default_cwd())))
        )

        if not os.path.isdir(cwd):
            cwd = self._default_cwd()
            self.set("cwd", cwd)

        return cwd

    def _set_cwd(self, cwd: str) -> str:
        cwd = os.path.abspath(os.path.expandvars(os.path.expanduser(cwd)))
        self.set("cwd", cwd)
        return cwd

    def _resolve_path(self, path: str) -> str:
        path = os.path.expandvars(os.path.expanduser(path or "~"))
        return path if os.path.isabs(path) else os.path.join(self._get_cwd(), path)

    @staticmethod
    def _shell() -> str:
        shell = os.environ.get("SHELL")
        if shell and os.path.exists(shell):
            return shell

        return "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"

    @staticmethod
    def _append_sudo_stdin_switch(cmd: str) -> str:
        if len(cmd.split(" ")) <= 1 or cmd.split(" ")[0] != "sudo":
            return cmd

        needsswitch = True

        for word in cmd.split(" ", 1)[1].split(" "):
            if not word or word[0] != "-":
                break

            if word == "-S":
                needsswitch = False

        return (
            " ".join([cmd.split(" ", 1)[0], "-S", cmd.split(" ", 1)[1]])
            if needsswitch
            else cmd
        )

    @staticmethod
    def _wrap_command(cmd: str, cwd_file: str) -> str:
        return "\n".join(
            (
                cmd,
                "__hikka_terminal_rc=$?",
                f"pwd > {shlex.quote(cwd_file)}",
                "exit $__hikka_terminal_rc",
            )
        )

    def _read_tracked_cwd(self, cwd_file: str) -> typing.Optional[str]:
        try:
            with open(cwd_file, encoding="utf-8") as file:
                cwd = file.read().strip()
        except OSError:
            return None
        finally:
            with contextlib.suppress(OSError):
                os.remove(cwd_file)

        if cwd and os.path.isdir(cwd):
            return self._set_cwd(cwd)

        return None

    def _get_history(self) -> typing.List[str]:
        history = self.get("history", [])
        return history if isinstance(history, list) else []

    def _add_history(self, cmd: str):
        limit = self.config["HISTORY_LIMIT"]
        if not limit:
            return

        history = self._get_history()
        history.append(cmd)
        self.set("history", history[-limit:])

    def _history_entry(self, ref: str) -> typing.Optional[str]:
        if not ref.startswith("!") or not ref[1:].isdigit():
            return None

        history = self._get_history()
        index = int(ref[1:]) - 1
        if index < 0 or index >= len(history):
            return False

        return history[index]

    @staticmethod
    def _extract_time(args: typing.List[str]) -> int:
        for suffix, quantifier in [
            ("d", 24 * 60 * 60),
            ("h", 60 * 60),
            ("m", 60),
            ("s", 1),
        ]:
            duration = next(
                (
                    int(arg.rsplit(suffix, maxsplit=1)[0])
                    for arg in args
                    if arg.endswith(suffix)
                    and arg.rsplit(suffix, maxsplit=1)[0].isdigit()
                ),
                None,
            )
            if duration is not None:
                return duration * quantifier

        return 0

    @staticmethod
    def _format_duration(duration: int) -> str:
        if duration >= 24 * 60 * 60 and duration % (24 * 60 * 60) == 0:
            return f"{duration // (24 * 60 * 60)}d"

        if duration >= 60 * 60 and duration % (60 * 60) == 0:
            return f"{duration // (60 * 60)}h"

        if duration >= 60 and duration % 60 == 0:
            return f"{duration // 60}m"

        return f"{duration}s"

    def _terminal_mode_state(self) -> typing.Dict[str, int]:
        state = self.get("terminal_mode", {})
        return state if isinstance(state, dict) else {}

    def _is_terminal_mode_enabled(self, message: hikkatl.tl.types.Message) -> bool:
        state = self._terminal_mode_state()
        chat_id = str(utils.get_chat_id(message))
        expires = state.get(chat_id)

        if not expires:
            return False

        if expires <= time.time():
            state.pop(chat_id, None)
            self.set("terminal_mode", state)
            return False

        return True

    def _set_terminal_mode(self, message: hikkatl.tl.types.Message, duration: int):
        state = self._terminal_mode_state()
        state[str(utils.get_chat_id(message))] = int(time.time() + duration)
        self.set("terminal_mode", state)

    def _disable_terminal_mode(self, message: hikkatl.tl.types.Message):
        state = self._terminal_mode_state()
        state.pop(str(utils.get_chat_id(message)), None)
        self.set("terminal_mode", state)

    async def client_ready(self):
        self._restart_enabled_scripts()

    async def on_unload(self):
        self._stop_all_scripts()

    def _get_scripts(self) -> typing.Dict[str, dict]:
        scripts = self.get("scripts", {})
        return scripts if isinstance(scripts, dict) else {}

    def _save_scripts(self, scripts: typing.Dict[str, dict]):
        self.set("scripts", scripts)

    def _restart_enabled_scripts(self):
        self._stop_all_scripts()
        for name, script in self._get_scripts().items():
            if script.get("enabled", True):
                self._start_script(name, script)

    def _stop_all_scripts(self):
        for task in list(self._script_tasks.values()):
            task.cancel()
        self._script_tasks.clear()
        self._script_snapshots.clear()

    def _stop_script(self, name: str):
        task = self._script_tasks.pop(name, None)
        if task:
            task.cancel()
        self._script_snapshots.pop(name, None)

    def _start_script(self, name: str, script: dict):
        self._stop_script(name)
        self._script_tasks[name] = asyncio.ensure_future(
            self._script_loop(name, script)
        )

    @staticmethod
    def _split_pipeline(pipeline: str) -> typing.List[str]:
        return [
            part.strip()
            for part in re.split(r"\s*(?:\|>|\+)\s*", pipeline)
            if part.strip()
        ]

    @staticmethod
    def _parse_script_interval(value: str) -> int:
        value = value.strip().lower()
        match = re.fullmatch(
            r"(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hour|hours)?",
            value,
        )
        if not match:
            return 0

        amount = int(match.group(1))
        suffix = match.group(2) or "s"
        if suffix.startswith("h"):
            return amount * 60 * 60
        if suffix.startswith("m"):
            return amount * 60
        return amount

    def _parse_terminal_script(self, source: str) -> dict:
        source = source.strip()
        if "->" not in source:
            raise ValueError("missing -> action separator")

        head, pipeline = map(str.strip, source.split("->", 1))
        if not head or not pipeline:
            raise ValueError("empty trigger or action")

        lowered = head.lower()
        if lowered.startswith("watch "):
            return self._parse_watch_script(source, head, pipeline)

        if lowered.startswith("on time:every "):
            interval_text = head[len("on time:every ") :].strip()
            interval = self._parse_script_interval(interval_text)
            if not interval:
                raise ValueError("invalid time interval")
            return {
                "type": "time",
                "interval": interval,
                "pipeline": pipeline,
                "source": source,
            }

        if lowered.startswith("schedule every "):
            interval_text = head[len("schedule every ") :].strip()
            interval = self._parse_script_interval(interval_text)
            if not interval:
                raise ValueError("invalid time interval")
            return {
                "type": "time",
                "interval": interval,
                "pipeline": pipeline,
                "source": source,
            }

        raise ValueError(
            "supported triggers: watch file:/path, watch dir:/path, on time:every <interval>"
        )

    def _parse_watch_script(self, source: str, head: str, pipeline: str) -> dict:
        match = re.fullmatch(
            r"watch\s+(file|dir):(.+?)\s+on:([\w-]+)",
            head,
            flags=re.IGNORECASE,
        )
        if not match:
            raise ValueError("watch syntax: watch file:/path on:change -> ...")

        kind, path, event = match.groups()
        event = event.lower()
        if event not in {"change", "new-file", "delete", "any"}:
            raise ValueError("watch events: change, new-file, delete, any")

        return {
            "type": "watch",
            "kind": kind.lower(),
            "path": self._resolve_path(path.strip().strip("\"'")),
            "event": event,
            "pipeline": pipeline,
            "source": source,
        }

    def _script_snapshot(
        self, script: dict
    ) -> typing.Dict[str, typing.Tuple[int, int]]:
        target = Path(script["path"])
        paths = []
        if script["kind"] == "file":
            paths = [target]
        elif target.is_dir():
            paths = [path for path in target.rglob("*") if path.is_file()]

        snapshot = {}
        for path in paths:
            with contextlib.suppress(OSError):
                stat = path.stat()
                snapshot[str(path)] = (stat.st_mtime_ns, stat.st_size)
        return snapshot

    @staticmethod
    def _watch_changes(
        old: typing.Dict[str, typing.Tuple[int, int]],
        new: typing.Dict[str, typing.Tuple[int, int]],
        event: str,
    ) -> typing.List[typing.Tuple[str, str]]:
        changes = []
        for path, stat in new.items():
            if path not in old:
                changes.append(("new-file", path))
            elif old[path] != stat:
                changes.append(("change", path))

        for path in old:
            if path not in new:
                changes.append(("delete", path))

        if event == "any":
            return changes
        if event == "change":
            return [change for change in changes if change[0] in {"change", "new-file"}]
        return [change for change in changes if change[0] == event]

    def _script_format_vars(
        self, text: str, variables: dict, quote: bool = False
    ) -> str:
        for key, value in variables.items():
            value = str(value)
            text = text.replace(f"${key}", shlex.quote(value) if quote else value)
        return text

    async def _script_shell(
        self, command: str, variables: dict, stdin: str = ""
    ) -> str:
        command = self._script_format_vars(command, variables, quote=True)
        process = await asyncio.create_subprocess_exec(
            self._shell(),
            "-c",
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._get_cwd(),
        )
        stdout, stderr = await process.communicate(stdin.encode())
        output = stdout.decode(errors="replace")
        errors = stderr.decode(errors="replace")
        if process.returncode and errors:
            output += ("\n" if output else "") + errors
        return output[-int(self.config["SCRIPTS_OUTPUT_LIMIT"]) :]

    async def _script_notify(self, stage: str, variables: dict, payload: str):
        stage = self._script_format_vars(stage, variables)
        args = shlex.split(stage)
        if len(args) < 2:
            raise ValueError('notify syntax: notify telegram <chat> [msg:"text"]')

        chat = args[1]
        if chat.lower() in {"telegram", "tg"}:
            if len(args) < 3:
                raise ValueError("notify telegram requires chat id/user/channel")
            chat = args[2]

        if chat.lstrip("-").isdigit():
            chat = int(chat)

        message = payload.strip()
        for arg in args[2:]:
            if arg.startswith("msg:"):
                message = arg.split(":", 1)[1]
                break

        if not message:
            message = self.strings("done")

        await self._client.send_message(chat, message[:4096])

    async def _run_script_pipeline(self, name: str, script: dict, variables: dict):
        payload = ""
        for stage in self._split_pipeline(script["pipeline"]):
            if stage.startswith("notify "):
                await self._script_notify(stage, variables, payload)
                continue

            if stage.startswith("run "):
                command = stage[4:].strip()
                if (
                    len(command) >= 2
                    and command[0] == command[-1]
                    and command[0] in {"'", '"'}
                ):
                    command = command[1:-1]
            else:
                command = stage

            if command:
                payload = await self._script_shell(command, variables, payload)

    async def _script_loop(self, name: str, script: dict):
        try:
            if script["type"] == "watch":
                self._script_snapshots[name] = self._script_snapshot(script)
                while True:
                    await asyncio.sleep(int(self.config["SCRIPTS_POLL_INTERVAL"]))
                    old = self._script_snapshots.get(name, {})
                    new = self._script_snapshot(script)
                    self._script_snapshots[name] = new
                    for event, path in self._watch_changes(old, new, script["event"]):
                        await self._run_script_pipeline(
                            name,
                            script,
                            {
                                "script": name,
                                "event": event,
                                "file": path,
                                "path": path,
                                "watched": script["path"],
                            },
                        )
            elif script["type"] == "time":
                while True:
                    await asyncio.sleep(int(script["interval"]))
                    await self._run_script_pipeline(
                        name,
                        script,
                        {"script": name, "event": "time", "file": "", "path": ""},
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Terminal script %s failed", name)
            await asyncio.sleep(10)
            saved = self._get_scripts().get(name)
            if saved and saved.get("enabled", True):
                self._start_script(name, saved)

    @loader.command(alias="ts")
    async def termscriptcmd(self, message):
        """add|list|show|start|stop|del - Manage saved background terminal scripts (alias: .ts)"""
        args = utils.get_args_raw(message).strip()
        if not args:
            await utils.answer(message, self.strings("script_usage"))
            return

        action, _, rest = args.partition(" ")
        action = action.lower()
        scripts = self._get_scripts()

        if action == "list":
            if not scripts:
                await utils.answer(message, self.strings("scripts_empty"))
                return
            await utils.answer(
                message,
                self.strings("scripts_list").format(
                    "\n".join(
                        self.strings("script_item").format(
                            "🟢" if data.get("enabled", True) else "⚪️",
                            utils.escape_html(name),
                            utils.escape_html(data.get("source", "")),
                        )
                        for name, data in scripts.items()
                    )
                ),
            )
            return

        if action == "add":
            name, sep, source = rest.partition("=")
            name = name.strip()
            source = source.strip() if sep else ""
            if not name or not source:
                await utils.answer(message, self.strings("script_usage"))
                return
            try:
                script = self._parse_terminal_script(source)
            except ValueError as e:
                await utils.answer(
                    message,
                    self.strings("script_invalid").format(utils.escape_html(str(e))),
                )
                return
            script["enabled"] = True
            scripts[name] = script
            self._save_scripts(scripts)
            self._start_script(name, script)
            await utils.answer(
                message,
                self.strings("script_saved").format(utils.escape_html(name)),
            )
            return

        name = rest.strip()
        if action in {"show", "start", "stop", "del", "delete", "rm"} and not name:
            await utils.answer(message, self.strings("script_usage"))
            return
        if (
            action in {"show", "start", "stop", "del", "delete", "rm"}
            and name not in scripts
        ):
            await utils.answer(
                message,
                self.strings("script_not_found").format(utils.escape_html(name)),
            )
            return

        if action == "show":
            await utils.answer(
                message,
                self.strings("script_show").format(
                    utils.escape_html(name),
                    "enabled" if scripts[name].get("enabled", True) else "disabled",
                    utils.escape_html(scripts[name].get("source", "")),
                ),
            )
            return

        if action == "start":
            scripts[name]["enabled"] = True
            self._save_scripts(scripts)
            self._start_script(name, scripts[name])
            await utils.answer(
                message,
                self.strings("script_started").format(utils.escape_html(name)),
            )
            return

        if action == "stop":
            scripts[name]["enabled"] = False
            self._save_scripts(scripts)
            self._stop_script(name)
            await utils.answer(
                message,
                self.strings("script_stopped").format(utils.escape_html(name)),
            )
            return

        if action in {"del", "delete", "rm"}:
            scripts.pop(name)
            self._save_scripts(scripts)
            self._stop_script(name)
            await utils.answer(
                message,
                self.strings("script_deleted").format(utils.escape_html(name)),
            )
            return

        await utils.answer(message, self.strings("script_usage"))

    @loader.command(alias="t")
    async def terminalcmd(self, message):
        """<command> - Execute shell command (alias: .t)"""
        cmd = utils.get_args_raw(message)

        if not cmd:
            await utils.answer(
                message,
                self.strings("empty_command").format(
                    utils.escape_html(self._get_cwd())
                ),
            )
            return

        if cmd.startswith("!"):
            history_cmd = self._history_entry(cmd)
            if history_cmd is False:
                await utils.answer(message, self.strings("history_invalid"))
                return

            if history_cmd:
                cmd = history_cmd

        self._add_history(cmd)
        await self.run_command(message, cmd)

    @loader.command(alias="t-rg")
    async def terminalmodecmd(self, message):
        """<time|off> - Enable terminal mode in current chat (alias: .t-rg)"""
        args = utils.get_args_raw(message).lower().split()

        if not args:
            await utils.answer(message, self.strings("terminal_mode_usage"))
            return

        if args[0] in {"off", "disable", "stop", "0"}:
            self._disable_terminal_mode(message)
            await utils.answer(message, self.strings("terminal_mode_disabled"))
            return

        duration = self._extract_time(args)
        if not duration:
            await utils.answer(message, self.strings("terminal_mode_invalid_time"))
            return

        self._set_terminal_mode(message, duration)
        await utils.answer(
            message,
            self.strings("terminal_mode_enabled").format(
                utils.escape_html(self._format_duration(duration))
            ),
        )

    @loader.watcher(out=True, only_messages=True)
    async def terminal_mode_watcher(self, message):
        if not self._is_terminal_mode_enabled(message):
            return

        prefix = self.get_prefix()
        text = getattr(message, "raw_text", None) or getattr(message, "message", "")
        if not text or not text.startswith(prefix) or text.startswith(prefix * 2):
            return

        command = text[len(prefix) :].strip()
        if not command:
            return

        command_name = command.split(maxsplit=1)[0].split("@", maxsplit=1)[0]
        if self.allmodules.dispatch(command_name)[1]:
            return

        self._add_history(command)
        await self.run_command(message, command)

    @loader.command(alias="a")
    async def aptcmd(self, message):
        """Shorthand for '.terminal apt'"""
        cmd = utils.get_args_raw(message)
        cwd = self._get_cwd()
        await self.run_command(
            message,
            ("apt " if os.geteuid() == 0 else "sudo -S apt ") + cmd + " -y",
            RawMessageEditor(
                message,
                f"apt {cmd}",
                self.config,
                self.strings,
                message,
                True,
                cwd,
                self._tg_id,
            ),
        )

    @loader.command(alias="c")
    async def cdcmd(self, message):
        """[path] - Change persistent terminal directory"""
        path = self._resolve_path(utils.get_args_raw(message))

        if not os.path.isdir(path):
            await utils.answer(
                message,
                self.strings("cd_error").format(utils.escape_html(path)),
            )
            return

        await utils.answer(
            message,
            self.strings("cd").format(utils.escape_html(self._set_cwd(path))),
        )

    @loader.command(alias="cwd")
    async def pwdcmd(self, message):
        """Show persistent terminal directory"""
        await utils.answer(
            message,
            self.strings("pwd").format(utils.escape_html(self._get_cwd())),
        )

    @loader.command()
    async def historycmd(self, message):
        """Show terminal command history. Use .t !N to rerun an entry"""
        history = self._get_history()
        if not history:
            await utils.answer(message, self.strings("history_empty"))
            return

        await utils.answer(
            message,
            self.strings("history").format(
                "\n".join(
                    self.strings("history_item").format(
                        index,
                        utils.escape_html(cmd),
                    )
                    for index, cmd in enumerate(history, 1)
                )
            ),
        )

    async def run_command(
        self,
        message: hikkatl.tl.types.Message,
        cmd: str,
        editor: typing.Optional[MessageEditor] = None,
    ):
        cmd = self._append_sudo_stdin_switch(cmd)
        cwd = self._get_cwd()
        cwd_file = os.path.join(
            tempfile.gettempdir(),
            f"hikka-terminal-{uuid.uuid4().hex}.cwd",
        )

        master_fd = None
        slave_fd = None
        input_writer = None

        if self.config["INTERACTIVE_TTY"]:
            master_fd, slave_fd = pty.openpty()
            sproc = await asyncio.create_subprocess_exec(
                self._shell(),
                "-c",
                self._wrap_command(cmd, cwd_file),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=cwd,
            )
            os.close(slave_fd)
            slave_fd = None

            def input_writer(data: bytes):
                if master_fd is not None:
                    os.write(master_fd, data)
        else:
            sproc = await asyncio.create_subprocess_exec(
                self._shell(),
                "-c",
                self._wrap_command(cmd, cwd_file),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )

        if editor is None:
            editor = SudoMessageEditor(
                message,
                cmd,
                self.config,
                self.strings,
                message,
                cwd,
                self._tg_id,
            )

        if self.config["INTERACTIVE_TTY"]:
            readers = asyncio.gather(
                read_pty_stream(
                    editor.update_stdout,
                    master_fd,
                    self.config["FLOOD_WAIT_PROTECT"],
                )
            )
        else:
            readers = asyncio.gather(
                read_stream(
                    editor.update_stdout,
                    sproc.stdout,
                    self.config["FLOOD_WAIT_PROTECT"],
                ),
                read_stream(
                    editor.update_stderr,
                    sproc.stderr,
                    self.config["FLOOD_WAIT_PROTECT"],
                ),
            )

        editor.update_process(sproc, input_writer)

        self.activecmds[hash_msg(message)] = sproc

        await editor.redraw()

        timed_out = False
        try:
            timeout = self.config["COMMAND_TIMEOUT"]
            try:
                rc = (
                    await asyncio.wait_for(sproc.wait(), timeout=timeout)
                    if timeout
                    else await sproc.wait()
                )
            except asyncio.TimeoutError:
                timed_out = True
                sproc.terminate()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(sproc.wait(), timeout=5)

                if sproc.returncode is None:
                    sproc.kill()
                    await sproc.wait()

                rc = sproc.returncode

            await readers

            if timed_out:
                await editor.update_stderr(
                    editor.stderr + self.strings("timeout").format(timeout)
                )

            new_cwd = self._read_tracked_cwd(cwd_file)
            await editor.cmd_ended(rc, new_cwd)
        finally:
            if not readers.done():
                readers.cancel()

            if slave_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(slave_fd)

            if master_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(master_fd)

            with contextlib.suppress(OSError):
                os.remove(cwd_file)

            self.activecmds.pop(hash_msg(message), None)

    @loader.command(aliases=("kill", "stop"))
    async def terminatecmd(self, message):
        if not message.is_reply:
            await utils.answer(message, self.strings("what_to_kill"))
            return

        if hash_msg(await message.get_reply_message()) in self.activecmds:
            try:
                if "-f" not in utils.get_args_raw(message):
                    self.activecmds[
                        hash_msg(await message.get_reply_message())
                    ].terminate()
                else:
                    self.activecmds[hash_msg(await message.get_reply_message())].kill()
            except Exception:
                logger.exception("Killing process failed")
                await utils.answer(message, self.strings("kill_fail"))
            else:
                await utils.answer(message, self.strings("killed"))
        else:
            await utils.answer(message, self.strings("no_cmd"))
