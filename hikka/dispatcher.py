"""Processes incoming events and dispatches them to appropriate handlers"""

#    Friendly Telegram (telegram userbot)
#    Copyright (C) 2018-2022 The Authors

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

import asyncio
import collections
import contextlib
import copy
import inspect
import logging
import re
import sys
import traceback
import typing
import weakref

from telethon import events
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.types import Message

from . import main, security, utils
from .database import Database
from .loader import Modules
from .tl_cache import CustomTelegramClient

logger = logging.getLogger(__name__)

# Keys for layout switch
ru_keys = 'ёйцукенгшщзхъфывапролджэячсмитьбю.Ё"№;%:?ЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭ/ЯЧСМИТЬБЮ,'
en_keys = "`qwertyuiop[]asdfghjkl;'zxcvbnm,./~@#$%^&QWERTYUIOP{}ASDFGHJKL:\"|ZXCVBNM<>?"
ALL_TAGS = [
    "no_commands",
    "only_commands",
    "out",
    "in",
    "only_messages",
    "editable",
    "no_media",
    "only_media",
    "only_photos",
    "only_videos",
    "only_audios",
    "only_docs",
    "only_stickers",
    "only_inline",
    "only_channels",
    "only_groups",
    "only_pm",
    "no_pm",
    "no_channels",
    "no_groups",
    "no_inline",
    "no_stickers",
    "no_docs",
    "no_audios",
    "no_videos",
    "no_photos",
    "no_forwards",
    "no_reply",
    "no_mention",
    "mention",
    "only_reply",
    "only_forwards",
    "startswith",
    "endswith",
    "contains",
    "regex",
    "filter",
    "from_id",
    "chat_id",
    "thumb_url",
    "alias",
    "aliases",
]

# Built once instead of on every message
LAYOUT_SWITCH = str.maketrans(ru_keys + en_keys, en_keys + ru_keys)


def _get_chat_id_tag(func) -> int:
    return (
        func.chat_id
        if not str(func.chat_id).startswith("-100")
        else int(str(func.chat_id)[4:])
    )


# Checks of watcher tags, called with the message and the watcher. A tag fails
# when its check returns a falsy value. Defined once at import time, so
# checking a watcher doesn't create a closure for every tag
TAG_CHECKS = {
    "out": lambda m, func: getattr(m, "out", True),
    "in": lambda m, func: not getattr(m, "out", True),
    "only_messages": lambda m, func: isinstance(m, Message),
    "editable": (
        lambda m, func: not getattr(m, "out", False)
        and not getattr(m, "fwd_from", False)
        and not getattr(m, "sticker", False)
        and not getattr(m, "via_bot_id", False)
    ),
    "no_media": lambda m, func: (
        not isinstance(m, Message) or not getattr(m, "media", False)
    ),
    "only_media": lambda m, func: (
        isinstance(m, Message) and getattr(m, "media", False)
    ),
    "only_photos": lambda m, func: utils.mime_type(m).startswith("image/"),
    "only_videos": lambda m, func: utils.mime_type(m).startswith("video/"),
    "only_audios": lambda m, func: utils.mime_type(m).startswith("audio/"),
    "only_docs": lambda m, func: getattr(m, "document", False),
    "only_stickers": lambda m, func: getattr(m, "sticker", False),
    "only_inline": lambda m, func: getattr(m, "via_bot_id", False),
    "only_channels": lambda m, func: (
        getattr(m, "is_channel", False) and not getattr(m, "is_group", False)
    ),
    "only_groups": (
        lambda m, func: getattr(m, "is_group", False)
        or not getattr(m, "private", False)
        and not getattr(m, "is_channel", False)
    ),
    "only_pm": lambda m, func: getattr(m, "private", False),
    "no_pm": lambda m, func: not getattr(m, "private", False),
    "no_channels": lambda m, func: not getattr(m, "is_channel", False),
    "no_groups": (
        lambda m, func: not getattr(m, "is_group", False)
        or getattr(m, "private", False)
        or getattr(m, "is_channel", False)
    ),
    "no_inline": lambda m, func: not getattr(m, "via_bot_id", False),
    "no_stickers": lambda m, func: not getattr(m, "sticker", False),
    "no_docs": lambda m, func: not getattr(m, "document", False),
    "no_audios": lambda m, func: not utils.mime_type(m).startswith("audio/"),
    "no_videos": lambda m, func: not utils.mime_type(m).startswith("video/"),
    "no_photos": lambda m, func: not utils.mime_type(m).startswith("image/"),
    "no_forwards": lambda m, func: not getattr(m, "fwd_from", False),
    "no_reply": lambda m, func: not getattr(m, "reply_to_msg_id", False),
    "no_mention": lambda m, func: not getattr(m, "mentioned", False),
    "mention": lambda m, func: getattr(m, "mentioned", False),
    "only_reply": lambda m, func: getattr(m, "reply_to_msg_id", False),
    "only_forwards": lambda m, func: getattr(m, "fwd_from", False),
    "startswith": lambda m, func: (
        isinstance(m, Message) and m.raw_text.startswith(func.startswith)
    ),
    "endswith": lambda m, func: (
        isinstance(m, Message) and m.raw_text.endswith(func.endswith)
    ),
    "contains": lambda m, func: (
        isinstance(m, Message) and func.contains in m.raw_text
    ),
    "regex": lambda m, func: (
        isinstance(m, Message) and re.search(func.regex, m.raw_text)
    ),
    "filter": lambda m, func: callable(func.filter) and func.filter(m),
    "from_id": lambda m, func: getattr(m, "sender_id", None) == func.from_id,
    "chat_id": lambda m, func: utils.get_chat_id(m) == _get_chat_id_tag(func),
}

_tag_checks_cache = weakref.WeakKeyDictionary()


def _get_tag_checks(func: callable) -> typing.Tuple[typing.Tuple[str, callable], ...]:
    """Tags set on the handler with their checks, collected once per handler"""
    key = getattr(func, "__func__", func)
    try:
        return _tag_checks_cache[key]
    except (KeyError, TypeError):
        pass

    checks = tuple(
        (tag, check) for tag, check in TAG_CHECKS.items() if getattr(func, tag, False)
    )

    with contextlib.suppress(TypeError):
        _tag_checks_cache[key] = checks

    return checks


def _decrement_ratelimit(delay, data, key, severity):
    def inner():
        data[key] = max(0, data[key] - severity)

    asyncio.get_event_loop().call_later(delay, inner)


class CommandDispatcher:
    def __init__(
        self,
        modules: Modules,
        client: CustomTelegramClient,
        db: Database,
    ):
        self._modules = modules
        self._client = client
        self.client = client
        self._db = db

        self._ratelimit_storage_user = collections.defaultdict(int)
        self._ratelimit_storage_chat = collections.defaultdict(int)
        self._ratelimit_max_user = db.get(__name__, "ratelimit_max_user", 30)
        self._ratelimit_max_chat = db.get(__name__, "ratelimit_max_chat", 100)

        self.security = security.SecurityManager(client, db)

        self.check_security = self.security.check
        self._me = self._client.hikka_me.id
        self._cached_usernames = [
            (
                self._client.hikka_me.username.lower()
                if self._client.hikka_me.username
                else str(self._client.hikka_me.id)
            )
        ]

        self._cached_usernames.extend(
            getattr(self._client.hikka_me, "usernames", None) or []
        )

        self.raw_handlers = []

    async def _handle_ratelimit(self, message: Message, func: callable) -> bool:
        if await self.security.check(message, security.OWNER):
            return True

        func = getattr(func, "__func__", func)
        ret = True
        chat = self._ratelimit_storage_chat[message.chat_id]

        if message.sender_id:
            user = self._ratelimit_storage_user[message.sender_id]
            severity = (5 if getattr(func, "ratelimit", False) else 2) * (
                (user + chat) // 30 + 1
            )
            user += severity
            self._ratelimit_storage_user[message.sender_id] = user
            if user > self._ratelimit_max_user:
                ret = False
            else:
                self._ratelimit_storage_chat[message.chat_id] = chat

            _decrement_ratelimit(
                self._ratelimit_max_user * severity,
                self._ratelimit_storage_user,
                message.sender_id,
                severity,
            )
        else:
            severity = (5 if getattr(func, "ratelimit", False) else 2) * (
                chat // 15 + 1
            )

        chat += severity

        if chat > self._ratelimit_max_chat:
            ret = False

        _decrement_ratelimit(
            self._ratelimit_max_chat * severity,
            self._ratelimit_storage_chat,
            message.chat_id,
            severity,
        )

        return ret

    def _handle_grep(self, message: Message) -> Message:
        # Allow escaping grep with double stick
        if "||grep" in message.text or "|| grep" in message.text:
            message.raw_text = re.sub(r"\|\| ?grep", "| grep", message.raw_text)
            message.text = re.sub(r"\|\| ?grep", "| grep", message.text)
            message.message = re.sub(r"\|\| ?grep", "| grep", message.message)
            return message

        grep = False
        if not re.search(r".+\| ?grep (.+)", message.raw_text):
            return message

        grep = re.search(r".+\| ?grep (.+)", message.raw_text).group(1)
        message.text = re.sub(r"\| ?grep.+", "", message.text)
        message.raw_text = re.sub(r"\| ?grep.+", "", message.raw_text)
        message.message = re.sub(r"\| ?grep.+", "", message.message)

        ungrep = False

        if re.search(r"-v (.+)", grep):
            ungrep = re.search(r"-v (.+)", grep).group(1)
            grep = re.sub(r"(.+) -v .+", r"\g<1>", grep)

        grep = utils.escape_html(grep).strip() if grep else False
        ungrep = utils.escape_html(ungrep).strip() if ungrep else False

        old_edit = message.edit
        old_reply = message.reply
        old_respond = message.respond

        def process_text(text: str) -> str:
            nonlocal grep, ungrep
            res = []

            for line in text.split("\n"):
                if (
                    grep
                    and grep in utils.remove_html(line)
                    and (not ungrep or ungrep not in utils.remove_html(line))
                ):
                    res.append(
                        utils.remove_html(line, escape=True).replace(
                            grep, f"<u>{grep}</u>"
                        )
                    )

                if not grep and ungrep and ungrep not in utils.remove_html(line):
                    res.append(utils.remove_html(line, escape=True))

            cont = (
                (f"contain <b>{grep}</b>" if grep else "")
                + (" and" if grep and ungrep else "")
                + ((" do not contain <b>" + ungrep + "</b>") if ungrep else "")
            )

            if res:
                text = f"<i>💬 Lines that {cont}:</i>\n" + "\n".join(res)
            else:
                text = f"💬 <i>No lines that {cont}</i>"

            return text

        async def my_edit(text, *args, **kwargs):
            text = process_text(text)
            kwargs["parse_mode"] = "HTML"
            return await old_edit(text, *args, **kwargs)

        async def my_reply(text, *args, **kwargs):
            text = process_text(text)
            kwargs["parse_mode"] = "HTML"
            return await old_reply(text, *args, **kwargs)

        async def my_respond(text, *args, **kwargs):
            text = process_text(text)
            kwargs["parse_mode"] = "HTML"
            kwargs.setdefault("reply_to", utils.get_topic(message))
            return await old_respond(text, *args, **kwargs)

        message.edit = my_edit
        message.reply = my_reply
        message.respond = my_respond
        message.hikka_grepped = True

        return message

    async def _handle_command(
        self,
        event: typing.Union[events.NewMessage, events.MessageDeleted],
        watcher: bool = False,
    ) -> typing.Union[bool, typing.Tuple[Message, str, str, callable]]:
        if not hasattr(event, "message") or not hasattr(event.message, "message"):
            return False

        if not event.message.message:
            return False

        prefix = self._db.get(main.__name__, "command_prefix", False) or "."
        change = LAYOUT_SWITCH

        # Most messages are not commands, so skip them before the costly
        # censoring of the whole message object
        if not event.message.message.startswith(
            (prefix, str.translate(prefix, change))
        ):
            return False

        message = utils.censor(event.message)

        if (
            message.out
            and len(message.message) > 2
            and (
                message.message.startswith(prefix * 2)
                and any(s != prefix for s in message.message)
                or message.message.startswith(str.translate(prefix * 2, change))
                and any(s != str.translate(prefix, change) for s in message.message)
            )
        ):
            # Allow escaping commands using .'s
            if not watcher:
                await message.edit(
                    message.message[1:],
                    parse_mode=lambda s: (
                        s,
                        utils.relocate_entities(message.entities, -1, message.message)
                        or (),
                    ),
                )
            return False

        if (
            event.message.message.startswith(str.translate(prefix, change))
            and str.translate(prefix, change) != prefix
        ):
            message.message = str.translate(message.message, change)
            message.text = str.translate(message.text, change)
        elif not event.message.message.startswith(prefix):
            return False

        if (
            event.sticker
            or event.dice
            or event.audio
            or event.via_bot_id
            or getattr(event, "reactions", False)
        ):
            return False

        blacklist_chats = self._db.get(main.__name__, "blacklist_chats", [])
        whitelist_chats = self._db.get(main.__name__, "whitelist_chats", [])
        whitelist_modules = self._db.get(main.__name__, "whitelist_modules", [])

        if utils.get_chat_id(message) in blacklist_chats or (
            whitelist_chats and utils.get_chat_id(message) not in whitelist_chats
        ):
            return False

        if not message.message or len(message.message) == 1:
            return False  # Message is just the prefix

        initiator = getattr(event, "sender_id", 0)

        command = message.message[1:].strip().split(maxsplit=1)[0]
        tag = command.split("@", maxsplit=1)

        if len(tag) == 2:
            if tag[1] == "me":
                if not message.out:
                    return False
            elif tag[1].lower() not in self._cached_usernames:
                return False
        elif (
            event.out
            or event.mentioned
            and event.message is not None
            and event.message.message is not None
            and not any(
                f"@{username}" not in command.lower()
                for username in self._cached_usernames
            )
        ):
            pass
        elif (
            not event.is_private
            and not self._db.get(main.__name__, "no_nickname", False)
            and command not in self._db.get(main.__name__, "nonickcmds", [])
            and initiator not in self._db.get(main.__name__, "nonickusers", [])
            and not self.security.check_tsec(initiator, command)
            and utils.get_chat_id(event)
            not in self._db.get(main.__name__, "nonickchats", [])
        ):
            return False

        txt, func = self._modules.dispatch(tag[0])

        if (
            not func
            or not await self._handle_ratelimit(message, func)
            or not await self.security.check(
                message,
                func,
                usernames=self._cached_usernames,
            )
        ):
            return False

        if message.is_channel and message.edit_date and not message.is_group:
            editor = await self.security.get_channel_editor(message)
            if editor is not None and editor != self._client.tg_id:
                logger.debug("Ignoring edit in channel")
                return False

        if (
            message.is_channel
            and message.is_group
            and message.chat.title.startswith("hikka-")
            and message.chat.title != "hikka-logs"
        ):
            if not watcher:
                logger.warning("Ignoring message in datachat \\ logging chat")
            return False

        message.message = prefix + txt + message.message[len(prefix + command) :]

        if (
            f"{str(utils.get_chat_id(message))}.{func.__self__.__module__}"
            in blacklist_chats
            or whitelist_modules
            and f"{utils.get_chat_id(message)}.{func.__self__.__module__}"
            not in whitelist_modules
        ):
            return False

        if await self._handle_tags(event, func):
            return False

        if self._db.get(main.__name__, "grep", False) and not watcher:
            message = self._handle_grep(message)

        return message, prefix, txt, func

    async def handle_raw(self, event: events.Raw):
        """Handle raw events."""
        for handler in self.raw_handlers:
            if isinstance(event, tuple(handler.updates)):
                try:
                    await handler(event)
                except Exception as e:
                    logger.exception("Error in raw handler %s: %s", handler.id, e)

    async def handle_command(
        self,
        event: typing.Union[events.NewMessage, events.MessageDeleted],
    ):
        """Handle all commands"""
        message = await self._handle_command(event)
        if not message:
            return

        message, _, _, func = message

        asyncio.ensure_future(
            self.future_dispatcher(
                func,
                message,
                self.command_exc,
            )
        )

    async def command_exc(self, _, message: Message):
        """Handle command exceptions."""
        exc = sys.exc_info()[1]
        logger.exception("Command failed", extra={"stack": inspect.stack()})
        if isinstance(exc, RPCError):
            if isinstance(exc, FloodWaitError):
                hours = exc.seconds // 3600
                minutes = (exc.seconds % 3600) // 60
                seconds = exc.seconds % 60
                hours = f"{hours} hours, " if hours else ""
                minutes = f"{minutes} minutes, " if minutes else ""
                seconds = f"{seconds} seconds" if seconds else ""
                fw_time = f"{hours}{minutes}{seconds}"
                txt = (
                    self._client.loader.lookup("translations")
                    .strings("fw_error")
                    .format(
                        utils.escape_html(message.message),
                        fw_time,
                        type(exc.request).__name__,
                    )
                )
            else:
                txt = (
                    "<emoji document_id=5877477244938489129>🚫</emoji> <b>Call"
                    f" </b><code>{utils.escape_html(message.message)}</code><b> failed"
                    " due to RPC (Telegram) error:</b>"
                    f" <code>{utils.escape_html(str(exc))}</code>"
                )
                txt = (
                    self._client.loader.lookup("translations")
                    .strings("rpc_error")
                    .format(
                        utils.escape_html(message.message),
                        utils.escape_html(str(exc)),
                    )
                )
        else:
            if not self._db.get(main.__name__, "inlinelogs", True):
                txt = (
                    "<emoji document_id=5877477244938489129>🚫</emoji><b> Call</b>"
                    f" <code>{utils.escape_html(message.message)}</code><b>"
                    " failed!</b>"
                )
            else:
                exc = "\n".join(traceback.format_exc().splitlines()[1:])
                txt = (
                    "<emoji document_id=5877477244938489129>🚫</emoji><b> Call</b>"
                    f" <code>{utils.escape_html(message.message)}</code><b>"
                    " failed!</b>\n\n<b>🧾"
                    f" Logs:</b>\n<code>{utils.escape_html(exc)}</code>"
                )

        with contextlib.suppress(Exception):
            await (message.edit if message.out else message.reply)(txt)

    async def watcher_exc(self, *_):
        logger.exception("Error running watcher", extra={"stack": inspect.stack()})

    async def _handle_tags(
        self,
        event: typing.Union[events.NewMessage, events.MessageDeleted],
        func: callable,
    ) -> bool:
        return bool(await self._handle_tags_ext(event, func))

    async def _handle_tags_ext(
        self,
        event: typing.Union[events.NewMessage, events.MessageDeleted],
        func: callable,
        command_cache: typing.Optional[dict] = None,
    ) -> str:
        """
        Handle tags.
        :param event: The event to handle.
        :param func: The function to handle.
        :param command_cache: Dict to remember whether the event is a command,
            shared by all watchers of one event
        :return: The reason for the tag to fail.
        """
        m = event if isinstance(event, Message) else getattr(event, "message", event)

        no_commands = getattr(func, "no_commands", False)
        only_commands = getattr(func, "only_commands", False)
        if no_commands or only_commands:
            if command_cache is None:
                command_cache = {}

            if "is_command" not in command_cache:
                command_cache["is_command"] = bool(
                    await self._handle_command(event, watcher=True)
                )

            if no_commands and command_cache["is_command"]:
                return "no_commands"

            if only_commands and not command_cache["is_command"]:
                return "only_commands"

        return next(
            (tag for tag, check in _get_tag_checks(func) if not check(m, func)),
            None,
        )

    async def handle_incoming(
        self,
        event: typing.Union[events.NewMessage, events.MessageDeleted],
    ):
        """Handle all incoming messages"""
        message = utils.censor(getattr(event, "message", event))

        blacklist_chats = self._db.get(main.__name__, "blacklist_chats", [])
        whitelist_chats = self._db.get(main.__name__, "whitelist_chats", [])
        whitelist_modules = self._db.get(main.__name__, "whitelist_modules", [])

        chat_id = utils.get_chat_id(message)
        if chat_id in blacklist_chats or (
            whitelist_chats and chat_id not in whitelist_chats
        ):
            logger.debug("Message is blacklisted")
            return

        bl = self._db.get(main.__name__, "disabled_watchers", {})
        is_message = isinstance(message, Message)
        command_cache = {}
        placeholders_set = False

        for func in self._modules.watchers:
            modname = str(func.__self__.__class__.strings["name"])
            module_key = f"{chat_id}.{func.__self__.__module__}"

            if (
                modname in bl
                and is_message
                and (
                    "*" in bl[modname]
                    or chat_id in bl[modname]
                    or "only_chats" in bl[modname]
                    and message.is_private
                    or "only_pm" in bl[modname]
                    and not message.is_private
                    or "out" in bl[modname]
                    and not message.out
                    or "in" in bl[modname]
                    and message.out
                )
                or module_key in blacklist_chats
                or whitelist_modules
                and module_key not in whitelist_modules
            ):
                logger.debug(
                    "Ignored watcher of module %s because it is disabled in this chat",
                    modname,
                )
                continue

            if reason := await self._handle_tags_ext(event, func, command_cache):
                logger.debug(
                    "Ignored watcher of module %s because of %s",
                    modname,
                    reason,
                )
                continue

            # Avoid weird AttributeErrors in weird dochub modules by settings placeholder
            # of attributes
            if not placeholders_set:
                placeholders_set = True
                for placeholder in {"text", "raw_text", "out"}:
                    try:
                        if not hasattr(message, placeholder):
                            setattr(message, placeholder, "")
                    except UnicodeDecodeError:
                        pass

            # Run watcher via ensure_future so in case user has a lot
            # of watchers with long actions, they can run simultaneously
            asyncio.ensure_future(
                self.future_dispatcher(
                    func,
                    message,
                    self.watcher_exc,
                )
            )

    async def future_dispatcher(
        self,
        func: callable,
        message: Message,
        exception_handler: callable,
        *args,
    ):
        # Will be used to determine, which client caused logging messages
        # parsed via inspect.stack()
        _hikka_client_id_logging_tag = copy.copy(self.client.tg_id)  # noqa: F841
        try:
            await func(message)
        except Exception as e:
            await exception_handler(e, message, *args)
