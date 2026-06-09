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
import fnmatch
import logging
import os
import pty
import re
import shlex
import shutil
import tempfile
import time
import typing
import uuid
from pathlib import Path

import hikkatl

from .. import loader, utils

logger = logging.getLogger(__name__)

ANSI_ESCAPE_RE = re.compile(
    r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1B\\))"
)
SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(token|api[_-]?key|secret|password|passwd|pass|authorization)"
    r"(\s*[:=]\s*|\s+)([^\s&;]+)"
)
BEARER_TOKEN_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{12,}")
TELEGRAM_BOT_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")


def redact_sensitive_text(text: str) -> str:
    """Mask common secrets before persisting or sending terminal text."""
    text = str(text)
    text = TELEGRAM_BOT_TOKEN_RE.sub("<redacted-token>", text)
    text = BEARER_TOKEN_RE.sub(r"\1<redacted>", text)
    return SENSITIVE_VALUE_RE.sub(r"\1\2<redacted>", text)


def clean_terminal_output(text: str) -> str:
    """Remove terminal control sequences that render poorly in Telegram."""
    return ANSI_ESCAPE_RE.sub("", text).replace("\r\n", "\n").replace("\r", "\n")


def split_text_for_telegram(text: str, limit: int = 3500) -> typing.List[str]:
    """Split terminal output into Telegram-safe chunks preserving line breaks."""
    text = str(text)
    if not text:
        return [""]

    chunks = []
    current = ""

    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]

        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line

    if current or not chunks:
        chunks.append(current)

    return chunks


def format_output_page(title: str, body: str, page: int, total: int) -> str:
    suffix = f" <code>{page}/{total}</code>" if total > 1 else ""
    body = utils.escape_html(body) or " "
    return f"{title}{suffix}\n<blockquote>{body}</blockquote>"


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
                await func(clean_terminal_output(data.decode(errors="replace")))
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
                await func(clean_terminal_output(data.decode(errors="replace")))
            break

        data += dat

        if last_task:
            last_task.cancel()

        last_task = asyncio.ensure_future(sleep_for_task(func, data, delay))


async def sleep_for_task(func: callable, data: bytes, delay: float):
    await asyncio.sleep(delay)
    await func(clean_terminal_output(data.decode(errors="replace")))


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

    def _redact(self, text: str) -> str:
        if not self.config.get("REDACT_SECRETS", True):
            return text

        return redact_sensitive_text(text)

    async def redraw(self):
        command = self._redact(self.command)
        text = self.strings("running").format(utils.escape_html(command))  # fmt: skip

        if self.cwd:
            text += self.strings("cwd").format(utils.escape_html(self.cwd))

        if self.rc is not None:
            text += self.strings("finished").format(utils.escape_html(str(self.rc)))

        stdout_text = self._redact(self.stdout)
        stdout = utils.escape_html(stdout_text[max(len(stdout_text) - 2048, 0) :])
        text += self.strings("stdout")
        text += self.strings("quote").format(stdout or " ")
        stderr_text = self._redact(self.stderr)
        stderr = utils.escape_html(stderr_text[max(len(stderr_text) - 1024, 0) :])
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
        await self.send_full_output()

    def _full_output(self) -> typing.Tuple[str, str]:
        if self.rc not in (None, 0) and self.stderr:
            return self.strings("stderr"), self.stderr

        return self.strings("stdout"), self.stdout

    async def send_full_output(self):
        label, output = self._full_output()
        if not output:
            return

        preview_limit = 2048 if label == self.strings("stdout") else 1024
        if len(output) <= preview_limit:
            return

        command = self._redact(self.command)
        command = command if len(command) <= 256 else f"{command[:253]}..."
        output = self._redact(output)
        title = (
            "<emoji document_id=5472111548572900003>📼</emoji> "
            f"<b>Full terminal output:</b> <code>{utils.escape_html(command)}</code>"
        )
        chunks = split_text_for_telegram(output)
        for index, chunk in enumerate(chunks, 1):
            await self.message.client.send_message(
                self.message.peer_id,
                format_output_page(title, chunk, index, len(chunks)),
                reply_to=getattr(self.request_message, "id", None),
                link_preview=False,
            )

    def update_process(self, process, input_writer=None):
        pass


class SudoMessageEditor(MessageEditor):
    # Let's just hope these are safe to parse
    PASS_REQ = "[sudo] password for"
    WRONG_PASS = r"\[sudo\] password for (.*): Sorry, try again\."
    TOO_MANY_TRIES = (r"\[sudo\] password for (.*): sudo: [0-9]+ incorrect password attempts")  # fmt: skip
    GENERIC_AUTH_PROMPTS = (
        re.compile(
            r"(?im)(?:^|[\r\n])[^\r\n]{0,160}(?:password|passphrase|pin|otp|verification code|2fa)[^\r\n:]{0,160}:\s*$"
        ),
        re.compile(
            r"(?im)(?:^|[\r\n])[^\r\n]{0,160}(?:continue connecting|yes/no(?:/\[fingerprint\])?)[^\r\n?]{0,160}\?\s*$"
        ),
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

        command = "<code>" + utils.escape_html(self._redact(self.command)) + "</code>"
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

        if (
            is_stderr
            and len(lines) > 1
            and (
                re.fullmatch(self.TOO_MANY_TRIES, lastline) and self.state in {1, 3, 4}
            )
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

        stdout = self._redact(self.stdout)
        stderr = self._redact(self.stderr)
        if self.rc is None:
            text = self.strings("quote").format(
                utils.escape_html(stdout[max(len(stdout) - 4095, 0) :]) or " "
            )
        elif self.rc == 0:
            text = self.strings("quote").format(
                utils.escape_html(stdout[max(len(stdout) - 4090, 0) :]) or " "
            )
        else:
            text = self.strings("quote").format(
                utils.escape_html(stderr[max(len(stderr) - 4095, 0) :]) or " "
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
            "Legacy option kept for compatibility; script output is split into messages"
        ),
        "max_copy_file_size_cfg": (
            "Maximum file size in bytes for .t.cp without --force. 0 disables the limit"
        ),
        "max_paste_file_size_cfg": (
            "Maximum file size in bytes for .t-ps without --force. 0 disables the limit"
        ),
        "redact_secrets_cfg": "Mask common tokens/passwords in terminal output and history",
        "scripts_ignore_cfg": "Comma-separated patterns ignored by terminal directory watchers",
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
        "history_cleared": (
            "<emoji document_id=5314250708508220914>✅</emoji> <b>Terminal history cleared</b>"
        ),
        "copy_usage": (
            "<emoji document_id=5472111548572900003>📁</emoji> <b>Usage:</b> "
            "<code>.t.cp [-md|--md] [-p|--preview] [-f|--force] [--name name] &lt;path&gt;</code>"
        ),
        "copy_not_found": (
            "<emoji document_id=5210952531676504517>🚫</emoji> <b>File not found:</b> "
            "<code>{}</code>"
        ),
        "copy_is_dir": (
            "<emoji document_id=5210952531676504517>🚫</emoji> <b>This is a directory, not a file:</b> "
            "<code>{}</code>"
        ),
        "copy_sent": (
            "<emoji document_id=5472111548572900003>📁</emoji> <b>File from terminal:</b> "
            "<code>{}</code>"
        ),
        "copy_sent_md": (
            "<emoji document_id=5472111548572900003>📁</emoji> <b>Markdown file from terminal:</b> "
            "<code>{}</code>"
        ),
        "copy_not_text": (
            "<emoji document_id=5210952531676504517>🚫</emoji> <b>File does not look like readable text:</b> "
            "<code>{}</code>"
        ),
        "copy_too_large": (
            "<emoji document_id=5210952531676504517>🚫</emoji> <b>File is too large:</b> "
            "<code>{}</code> <b>(&gt; {} bytes). Use</b> <code>--force</code> <b>to send anyway.</b>"
        ),
        "copy_preview": (
            "<emoji document_id=5472111548572900003>📁</emoji> <b>File preview:</b> "
            "<code>{}</code>\n<b>Size:</b> <code>{}</code> <b>bytes</b>\n<blockquote>{}</blockquote>"
        ),
        "paste_usage": (
            "<emoji document_id=5472111548572900003>📁</emoji> <b>Reply to a Telegram file with</b> "
            "<code>.t-ps [-f|--force] [path]</code>"
        ),
        "paste_saved": (
            "<emoji document_id=5314250708508220914>✅</emoji> <b>File saved to:</b> "
            "<code>{}</code>"
        ),
        "paste_no_filename": (
            "<emoji document_id=5210952531676504517>🚫</emoji> <b>Could not determine file name. "
            "Use</b> <code>.t-ps &lt;path&gt;</code>"
        ),
        "paste_exists": (
            "<emoji document_id=5210952531676504517>🚫</emoji> <b>File already exists:</b> "
            "<code>{}</code> <b>Use</b> <code>--force</code> <b>to overwrite.</b>"
        ),
        "paste_too_large": (
            "<emoji document_id=5210952531676504517>🚫</emoji> <b>Telegram file is too large:</b> "
            "<code>{}</code> <b>(&gt; {} bytes). Use</b> <code>--force</code> <b>to save anyway.</b>"
        ),
        "terminal_mode_usage": (
            "<emoji document_id=5472111548572900003>⌨️</emoji> <b>Usage:</b> "
            "<code>.t-rg &lt;time|forever|off|status&gt;</code>"
        ),
        "terminal_mode_enabled": (
            "<emoji document_id=5314250708508220914>✅</emoji> <b>Terminal mode enabled for {}</b>\n"
            "<b>Now unknown dot-commands in this chat will be executed as shell commands.</b>"
        ),
        "terminal_mode_disabled": (
            "<emoji document_id=5314250708508220914>✅</emoji> <b>Terminal mode disabled in this chat</b>"
        ),
        "terminal_mode_status_on": (
            "<emoji document_id=5472111548572900003>⌨️</emoji> <b>Terminal mode is enabled in this chat for {}</b>"
        ),
        "terminal_mode_status_forever": (
            "<emoji document_id=5472111548572900003>⌨️</emoji> <b>Terminal mode is enabled in this chat permanently</b>"
        ),
        "terminal_mode_forever": "forever",
        "terminal_mode_status_off": (
            "<emoji document_id=5472111548572900003>⌨️</emoji> <b>Terminal mode is disabled in this chat</b>"
        ),
        "terminal_mode_invalid_time": (
            "<emoji document_id=5210952531676504517>🚫</emoji> <b>Specify time like</b> "
            "<code>30m</code>, <code>2h</code>, <code>1d</code>, <code>forever</code> <b>or</b> <code>off</code>"
        ),
        "script_usage": (
            "<emoji document_id=5472111548572900003>🤖</emoji> <b>Terminal scripts</b>\n"
            "<code>.ts add &lt;name&gt; = watch dir:/path on:change -> cat $file |> notify telegram -100123</code>\n"
            '<code>.ts add &lt;name&gt; = on time:every 5min -> run "date" |> notify me</code>\n'
            "<code>.ts list|show|test|start|stop|pause|del &lt;name|all&gt;</code>"
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
        "scripts_bulk_updated": (
            "<emoji document_id=5314250708508220914>✅</emoji> <b>{} scripts updated</b>"
        ),
        "script_tested": (
            "<emoji document_id=5314250708508220914>✅</emoji> <b>Script</b> "
            "<code>{}</code> <b>test run started</b>"
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
        "_cmd_doc_apt": "[apt arguments] - Shorthand for '.terminal apt'",
        "_cmd_doc_cd": "[path] - Change persistent terminal directory",
        "_cmd_doc_history": (
            "[clear|search <text>|-n N] - Show/manage terminal command history. Use .t !N to rerun an entry"
        ),
        "_cmd_doc_pwd": "Show persistent terminal directory",
        "_cmd_doc_tcp": (
            "[-md|--md] [-p|--preview] [-f|--force] [--name name] <path> - Send a file "
            "from the current terminal directory to Telegram (alias: .t.cp)"
        ),
        "_cmd_doc_tps": (
            "[-f|--force] [path] - Save a replied Telegram file to the current terminal directory (alias: .t-ps)"
        ),
        "_cmd_doc_terminal": (
            "<command> - Execute shell command (alias: .t). Use !N to rerun history item N"
        ),
        "_cmd_doc_terminalmode": (
            "<time|forever|off|status> - Enable terminal mode in current chat (alias: .t-rg). "
            "Unknown dot-commands will be executed as shell commands"
        ),
        "_cmd_doc_termscript": (
            "add|list|show|test|start|stop|pause|del - Manage saved background terminal scripts (alias: .ts)"
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
            loader.ConfigValue(
                "MAX_COPY_FILE_SIZE",
                25 * 1024 * 1024,
                lambda: self.strings("max_copy_file_size_cfg"),
                validator=loader.validators.Integer(minimum=0),
            ),
            loader.ConfigValue(
                "MAX_PASTE_FILE_SIZE",
                25 * 1024 * 1024,
                lambda: self.strings("max_paste_file_size_cfg"),
                validator=loader.validators.Integer(minimum=0),
            ),
            loader.ConfigValue(
                "REDACT_SECRETS",
                True,
                lambda: self.strings("redact_secrets_cfg"),
                validator=loader.validators.Boolean(),
            ),
            loader.ConfigValue(
                "SCRIPTS_IGNORE_PATTERNS",
                [".git", "node_modules", "venv", "__pycache__"],
                lambda: self.strings("scripts_ignore_cfg"),
                validator=loader.validators.Series(validator=loader.validators.String()),
            ),
        )
        self.activecmds = {}
        self.activeinputs = {}
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
    def _parse_cli_args(raw: str) -> typing.List[str]:
        try:
            return shlex.split(raw)
        except ValueError:
            return [raw] if raw else []

    @staticmethod
    def _pop_flag(args: typing.List[str], *flags: str) -> bool:
        found = False
        for flag in flags:
            while flag in args:
                args.remove(flag)
                found = True
        return found

    @staticmethod
    def _pop_option(args: typing.List[str], *flags: str) -> typing.Optional[str]:
        for index, arg in enumerate(list(args)):
            for flag in flags:
                if arg == flag:
                    args.pop(index)
                    return args.pop(index) if index < len(args) else None

                prefix = f"{flag}="
                if arg.startswith(prefix):
                    args.pop(index)
                    return arg[len(prefix) :]

        return None

    @staticmethod
    def _human_file_name(path: str) -> str:
        return os.path.basename(path.rstrip(os.path.sep)) or os.path.basename(path)

    @staticmethod
    def _markdown_file_name(path: str, custom_name: typing.Optional[str] = None) -> str:
        if custom_name:
            name = os.path.basename(custom_name)
            return name if name.endswith(".md") else f"{name}.md"

        original = Path(path)
        return f"{original.name}.md" if not original.suffix else original.with_suffix(".md").name

    @staticmethod
    def _looks_like_text(path: str, sample_size: int = 8192) -> bool:
        try:
            with open(path, "rb") as file:
                sample = file.read(sample_size)
        except OSError:
            return False

        if b"\x00" in sample:
            return False

        try:
            sample.decode("utf-8")
        except UnicodeDecodeError:
            try:
                sample.decode()
            except UnicodeDecodeError:
                return False

        return True

    @staticmethod
    def _read_text_preview(path: str, limit: int = 2048) -> typing.Optional[str]:
        try:
            with open(path, "rb") as file:
                sample = file.read(limit + 1)
        except OSError:
            return None

        if b"\x00" in sample:
            return None

        try:
            text = sample.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = sample.decode()
            except UnicodeDecodeError:
                return None

        if len(text) > limit:
            text = f"{text[:limit]}…"

        return text

    def _file_size_allowed(self, size: int, config_key: str, force: bool) -> bool:
        limit = self.config[config_key]
        return force or not limit or size <= limit

    def _is_secret_command(self, cmd: str) -> bool:
        return redact_sensitive_text(cmd) != cmd


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

    @staticmethod
    def _ssh_option_consumes_argument(option: str) -> bool:
        return option in {
            "B",
            "b",
            "c",
            "D",
            "E",
            "e",
            "F",
            "I",
            "i",
            "J",
            "L",
            "l",
            "m",
            "O",
            "o",
            "p",
            "Q",
            "R",
            "S",
            "W",
            "w",
        }

    @classmethod
    def _ssh_destination_index(
        cls,
        argv: typing.List[str],
    ) -> typing.Tuple[typing.Optional[int], bool, bool]:
        has_tty_option = False
        skip_autocd = False
        index = 1

        while index < len(argv):
            token = argv[index]

            if token == "--":
                return (
                    (index + 1 if index + 1 < len(argv) else None),
                    has_tty_option,
                    skip_autocd,
                )

            if not token.startswith("-") or token == "-":
                return index, has_tty_option, skip_autocd

            if token in {"-t", "-tt", "-T"}:
                has_tty_option = True
                skip_autocd = skip_autocd or token == "-T"
                index += 1
                continue

            if token.startswith("--"):
                index += 1
                continue

            offset = 1
            while offset < len(token):
                option = token[offset]
                if option in {"N", "T", "f", "s"}:
                    skip_autocd = True

                if cls._ssh_option_consumes_argument(option):
                    if option in {"O", "W", "w"}:
                        skip_autocd = True
                    if offset == len(token) - 1:
                        index += 1
                    break

                offset += 1

            index += 1

        return None, has_tty_option, skip_autocd

    @classmethod
    def _prepare_ssh_autocd(cls, cmd: str, cwd: str) -> str:
        try:
            argv = shlex.split(cmd)
        except ValueError:
            return cmd

        if not argv or os.path.basename(argv[0]) != "ssh":
            return cmd

        destination_index, has_tty_option, skip_autocd = cls._ssh_destination_index(
            argv
        )
        if (
            destination_index is None
            or skip_autocd
            or destination_index + 1 < len(argv)
        ):
            return cmd

        remote_command = f"cd {shlex.quote(cwd)} && exec ${{SHELL:-/bin/sh}} -l"
        prepared = list(argv)
        if not has_tty_option:
            prepared.insert(destination_index, "-t")

        prepared.append(remote_command)
        return shlex.join(prepared)

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
        if not limit or self._is_secret_command(cmd):
            return

        history = self._get_history()
        if history and history[-1] == cmd:
            return

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

    @staticmethod
    def _is_forever_terminal_mode(expires: typing.Optional[int]) -> bool:
        return isinstance(expires, int) and expires <= 0

    def _is_terminal_mode_enabled(self, message: hikkatl.tl.types.Message) -> bool:
        state = self._terminal_mode_state()
        chat_id = str(utils.get_chat_id(message))
        expires = state.get(chat_id)

        if expires is None:
            return False

        if self._is_forever_terminal_mode(expires):
            return True

        if expires <= time.time():
            state.pop(chat_id, None)
            self.set("terminal_mode", state)
            return False

        return True

    def _set_terminal_mode(self, message: hikkatl.tl.types.Message, duration: int):
        state = self._terminal_mode_state()
        state[str(utils.get_chat_id(message))] = (
            0 if duration <= 0 else int(time.time() + duration)
        )
        self.set("terminal_mode", state)

    @staticmethod
    def _has_shell_separator(command: str) -> bool:
        return bool(re.search(r"(?:\r?\n|&&|\|\||;|\|)", command))

    async def _run_terminal_mode_shell_builtin(
        self,
        message: hikkatl.tl.types.Message,
        builtin: str,
        args: str,
    ) -> bool:
        if (
            not self._is_terminal_mode_enabled(message)
            or not self._has_shell_separator(args)
        ):
            return False

        command = f"{builtin} {args}".strip()
        self._add_history(command)
        if self._write_to_active_input(message, command):
            return True

        await self.run_command(message, command)
        return True

    def _disable_terminal_mode(self, message: hikkatl.tl.types.Message):
        state = self._terminal_mode_state()
        state.pop(str(utils.get_chat_id(message)), None)
        self.set("terminal_mode", state)

    def _write_to_active_input(
        self,
        message: hikkatl.tl.types.Message,
        command: str,
    ) -> bool:
        active_input = self.activeinputs.get(str(utils.get_chat_id(message)))
        if not active_input or active_input["process"].returncode is not None:
            return False

        active_input["writer"]((command + "\n").encode())
        return True

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

    def _script_path_ignored(self, path: Path) -> bool:
        patterns = self.config["SCRIPTS_IGNORE_PATTERNS"]
        path_text = str(path)
        return any(
            fnmatch.fnmatch(path.name, pattern) or fnmatch.fnmatch(path_text, pattern)
            for pattern in patterns
        )

    def _iter_script_paths(self, target: Path) -> typing.Iterable[Path]:
        if not target.is_dir():
            return

        for root, dirs, files in os.walk(target):
            root_path = Path(root)
            dirs[:] = [
                directory
                for directory in dirs
                if not self._script_path_ignored(root_path / directory)
            ]
            for file_name in files:
                path = root_path / file_name
                if not self._script_path_ignored(path):
                    yield path

    def _script_snapshot(
        self, script: dict
    ) -> typing.Dict[str, typing.Tuple[int, int]]:
        target = Path(script["path"])
        paths = []
        if script["kind"] == "file" and not self._script_path_ignored(target):
            paths = [target]
        elif target.is_dir():
            paths = list(self._iter_script_paths(target))

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
        return clean_terminal_output(output)

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

        script_name = str(variables.get("script", ""))
        if len(script_name) > 128:
            script_name = f"{script_name[:125]}..."
        title = (
            "<emoji document_id=5472111548572900003>🤖</emoji> "
            f"<b>Terminal script</b> <code>{utils.escape_html(script_name)}</code>"
        )
        if variables.get("event"):
            title += f" <b>event:</b> <code>{utils.escape_html(str(variables['event']))}</code>"
        if variables.get("file"):
            file_name = str(variables["file"])
            if len(file_name) > 256:
                file_name = f"{file_name[:253]}..."
            title += f"\n<b>File:</b> <code>{utils.escape_html(file_name)}</code>"

        chunks = split_text_for_telegram(message)
        for index, chunk in enumerate(chunks, 1):
            await self._client.send_message(
                chat,
                format_output_page(title, chunk, index, len(chunks)),
                link_preview=False,
            )

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
        """add|list|show|test|start|stop|pause|del - Manage saved background terminal scripts (alias: .ts)"""
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
        managed_actions = {
            "show",
            "test",
            "start",
            "stop",
            "pause",
            "del",
            "delete",
            "rm",
        }
        if action in managed_actions and not name:
            await utils.answer(message, self.strings("script_usage"))
            return

        if action in {"start", "stop", "pause"} and name == "all":
            enabled = action == "start"
            for script_name, script in scripts.items():
                script["enabled"] = enabled
                if enabled:
                    self._start_script(script_name, script)
                else:
                    self._stop_script(script_name)
            self._save_scripts(scripts)
            await utils.answer(
                message,
                self.strings("scripts_bulk_updated").format(len(scripts)),
            )
            return

        if action in managed_actions and name not in scripts:
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

        if action == "test":
            asyncio.ensure_future(
                self._run_script_pipeline(
                    name,
                    scripts[name],
                    {"script": name, "event": "test", "file": "", "path": ""},
                )
            )
            await utils.answer(
                message,
                self.strings("script_tested").format(utils.escape_html(name)),
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

        if action in {"stop", "pause"}:
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

    @staticmethod
    def _message_file_name(message: hikkatl.tl.types.Message) -> typing.Optional[str]:
        file = getattr(message, "file", None)
        name = getattr(file, "name", None)
        if name:
            return os.path.basename(name)

        document = getattr(message, "document", None)
        for attr in getattr(document, "attributes", []) or []:
            file_name = getattr(attr, "file_name", None)
            if file_name:
                return os.path.basename(file_name)

        return None

    @loader.command(alias="t.cp")
    async def tcpcmd(self, message):
        """[-md|--md] [-p|--preview] [-f|--force] [--name name] <path> - Send a file from terminal"""
        raw_path = utils.get_args_raw(message).strip()
        if not raw_path:
            await utils.answer(message, self.strings("copy_usage"))
            return

        args = self._parse_cli_args(raw_path)
        as_markdown = self._pop_flag(args, "-md", "--md")
        preview = self._pop_flag(args, "-p", "--preview")
        force = self._pop_flag(args, "-f", "--force")
        custom_name = self._pop_option(args, "--name")

        if not args:
            await utils.answer(message, self.strings("copy_usage"))
            return

        path = self._resolve_path(args[0])
        display_path = os.path.relpath(path, self._get_cwd())

        if not os.path.exists(path):
            await utils.answer(
                message,
                self.strings("copy_not_found").format(utils.escape_html(path)),
            )
            return

        if not os.path.isfile(path):
            await utils.answer(
                message,
                self.strings("copy_is_dir").format(utils.escape_html(path)),
            )
            return

        size = os.path.getsize(path)
        if not self._file_size_allowed(size, "MAX_COPY_FILE_SIZE", force):
            await utils.answer(
                message,
                self.strings("copy_too_large").format(
                    utils.escape_html(path),
                    self.config["MAX_COPY_FILE_SIZE"],
                ),
            )
            return

        sent_path = display_path if not display_path.startswith("..") else path

        if preview:
            preview_text = self._read_text_preview(path)
            if preview_text is None:
                await utils.answer(
                    message,
                    self.strings("copy_not_text").format(utils.escape_html(path)),
                )
                return

            await utils.answer(
                message,
                self.strings("copy_preview").format(
                    utils.escape_html(sent_path),
                    size,
                    utils.escape_html(preview_text),
                ),
            )
            return

        if not as_markdown:
            await utils.answer_file(
                message,
                path,
                caption=self.strings("copy_sent").format(utils.escape_html(sent_path)),
            )
            return

        if not self._looks_like_text(path):
            await utils.answer(
                message,
                self.strings("copy_not_text").format(utils.escape_html(path)),
            )
            return

        md_name = self._markdown_file_name(path, custom_name)
        sent_dir = os.path.dirname(sent_path)
        md_display = os.path.join(sent_dir, md_name) if sent_dir else md_name
        with tempfile.TemporaryDirectory() as temp_dir:
            md_path = os.path.join(temp_dir, md_name)
            shutil.copyfile(path, md_path)
            await utils.answer_file(
                message,
                md_path,
                caption=self.strings("copy_sent_md").format(
                    utils.escape_html(md_display)
                ),
            )

    @loader.command(alias="t-ps")
    async def tpscmd(self, message):
        """[-f|--force] [path] - Save a replied Telegram file to the current terminal directory (alias: .t-ps)"""
        reply = await message.get_reply_message()
        if not reply or not getattr(reply, "media", None):
            await utils.answer(message, self.strings("paste_usage"))
            return

        args = self._parse_cli_args(utils.get_args_raw(message).strip())
        force = self._pop_flag(args, "-f", "--force")

        if args:
            destination = self._resolve_path(args[0])
        else:
            file_name = self._message_file_name(reply)
            if not file_name:
                await utils.answer(message, self.strings("paste_no_filename"))
                return
            destination = self._resolve_path(file_name)

        if destination.endswith(os.path.sep) or os.path.isdir(destination):
            file_name = self._message_file_name(reply)
            if not file_name:
                await utils.answer(message, self.strings("paste_no_filename"))
                return
            destination = os.path.join(destination, file_name)

        file_size = getattr(getattr(reply, "file", None), "size", None)
        if (
            file_size is not None
            and not self._file_size_allowed(file_size, "MAX_PASTE_FILE_SIZE", force)
        ):
            await utils.answer(
                message,
                self.strings("paste_too_large").format(
                    file_size,
                    self.config["MAX_PASTE_FILE_SIZE"],
                ),
            )
            return

        if os.path.exists(destination) and not force:
            await utils.answer(
                message,
                self.strings("paste_exists").format(utils.escape_html(destination)),
            )
            return

        os.makedirs(os.path.dirname(destination) or self._get_cwd(), exist_ok=True)
        data = await reply.download_media(bytes)
        data = data if isinstance(data, bytes) else bytes(data)

        if not self._file_size_allowed(len(data), "MAX_PASTE_FILE_SIZE", force):
            await utils.answer(
                message,
                self.strings("paste_too_large").format(
                    len(data),
                    self.config["MAX_PASTE_FILE_SIZE"],
                ),
            )
            return

        with open(destination, "wb") as file:
            file.write(data)

        await utils.answer(
            message,
            self.strings("paste_saved").format(utils.escape_html(destination)),
        )

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
        if self._write_to_active_input(message, cmd):
            return

        await self.run_command(message, cmd)

    @loader.command(alias="t-rg")
    async def terminalmodecmd(self, message):
        """<time|off|status> - Enable terminal mode in current chat (alias: .t-rg)"""
        args = utils.get_args_raw(message).lower().split()

        if not args:
            await utils.answer(message, self.strings("terminal_mode_usage"))
            return

        if args[0] in {"status", "state", "info", "статус"}:
            state = self._terminal_mode_state()
            expires = state.get(str(utils.get_chat_id(message)))
            if self._is_forever_terminal_mode(expires):
                await utils.answer(
                    message,
                    self.strings("terminal_mode_status_forever"),
                )
            elif expires and expires > time.time():
                await utils.answer(
                    message,
                    self.strings("terminal_mode_status_on").format(
                        utils.escape_html(
                            self._format_duration(int(expires - time.time()))
                        )
                    ),
                )
            else:
                await utils.answer(message, self.strings("terminal_mode_status_off"))
            return

        if args[0] in {"off", "disable", "stop", "0", "вимкнути"}:
            self._disable_terminal_mode(message)
            await utils.answer(message, self.strings("terminal_mode_disabled"))
            return

        forever = args[0] in {
            "on",
            "forever",
            "permanent",
            "always",
            "inf",
            "infinite",
            "назавжди",
            "постійно",
        }
        duration = 0 if forever else self._extract_time(args)
        if not forever and not duration:
            await utils.answer(message, self.strings("terminal_mode_invalid_time"))
            return

        self._set_terminal_mode(message, duration)
        await utils.answer(
            message,
            self.strings("terminal_mode_enabled").format(
                self.strings("terminal_mode_forever")
                if forever
                else utils.escape_html(self._format_duration(duration))
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

        if self._write_to_active_input(message, command):
            self._add_history(command)
            return

        self._add_history(command)
        await self.run_command(message, command)

    @loader.command(alias="a")
    async def aptcmd(self, message):
        """[apt arguments] - Shorthand for '.terminal apt'"""
        cmd = utils.get_args_raw(message).strip()
        if await self._run_terminal_mode_shell_builtin(message, "apt", cmd):
            return

        if not cmd:
            await utils.answer(message, self.strings("empty_command").format("apt"))
            return

        args = self._parse_cli_args(cmd)
        apt_cmd = ["apt"] + args
        if args and args[0] in {
            "install",
            "remove",
            "purge",
            "autoremove",
            "upgrade",
            "full-upgrade",
        }:
            apt_cmd.append("-y")

        cwd = self._get_cwd()
        command = shlex.join(apt_cmd)
        await self.run_command(
            message,
            command if os.geteuid() == 0 else f"sudo -S {command}",
            RawMessageEditor(
                message,
                command,
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
        args = utils.get_args_raw(message)
        if await self._run_terminal_mode_shell_builtin(message, "cd", args):
            return

        path = self._resolve_path(args)

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
        args = utils.get_args_raw(message)
        if await self._run_terminal_mode_shell_builtin(message, "pwd", args):
            return

        await utils.answer(
            message,
            self.strings("pwd").format(utils.escape_html(self._get_cwd())),
        )

    @loader.command()
    async def historycmd(self, message):
        """[clear|search <text>|-n N] - Show/manage terminal command history"""
        raw_args = utils.get_args_raw(message).strip()
        if await self._run_terminal_mode_shell_builtin(message, "history", raw_args):
            return

        args = self._parse_cli_args(raw_args)
        history = self._get_history()

        if args and args[0] == "clear":
            self.set("history", [])
            await utils.answer(message, self.strings("history_cleared"))
            return

        if args and args[0] == "search":
            query = " ".join(args[1:]).lower()
            history = [cmd for cmd in history if query in cmd.lower()] if query else []
        elif args and args[0] in {"-n", "--limit"}:
            try:
                limit = int(args[1])
            except (IndexError, ValueError):
                limit = len(history)
            history = history[-limit:] if limit > 0 else []

        if not history:
            await utils.answer(message, self.strings("history_empty"))
            return

        await utils.answer(
            message,
            self.strings("history").format(
                "\n".join(
                    self.strings("history_item").format(
                        index,
                        utils.escape_html(redact_sensitive_text(cmd)),
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
        cmd = self._prepare_ssh_autocd(cmd, cwd)
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
        chat_id = str(utils.get_chat_id(message))
        if input_writer is not None:
            self.activeinputs[chat_id] = {"process": sproc, "writer": input_writer}

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
            active_input = self.activeinputs.get(str(utils.get_chat_id(message)))
            if active_input and active_input["process"] is sproc:
                self.activeinputs.pop(str(utils.get_chat_id(message)), None)

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
