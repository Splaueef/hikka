# ©️ Dan Gazizullin, 2021-2023
# This file is a part of Hikka Userbot
# 🌐 https://github.com/Splaueef/hikka
# You can redistribute it and/or modify it under the terms of the GNU AGPLv3
# 🔑 https://www.gnu.org/licenses/agpl-3.0.html

import contextlib
import datetime
import logging

from hikkatl.tl.functions.channels import GetFullChannelRequest
from hikkatl.tl.functions.messages import GetFullChatRequest
from hikkatl.tl.types import (
    Channel,
    ChannelParticipantCreator,
    ChannelParticipantsAdmins,
    Chat,
    ChatParticipantAdmin,
    ChatParticipantCreator,
    Message,
    User,
)

from .. import loader, utils

logger = logging.getLogger(__name__)


@loader.tds
class ChatInfoMod(loader.Module):
    """Показує основну інформацію про поточну групу."""

    strings = {
        "name": "ChatInfo",
        "private_not_allowed": (
            "<b>Цю команду потрібно виконувати в групі або каналі.</b>"
        ),
        "header": "<b>Інформація про чат</b>",
        "title": "<b>Назва:</b> <code>{}</code>",
        "chat_id": "<b>ID:</b> <code>{}</code>",
        "username": "<b>Юзернейм:</b> @{}",
        "link": '<b>Посилання:</b> <a href="{}">{}</a>',
        "chat_type": "<b>Тип:</b> {}",
        "participants": "<b>Учасників:</b> <code>{}</code>",
        "owner": "<b>Власник:</b> {}",
        "admins": "<b>Адміни:</b> {}",
        "admins_more": " та ще <code>{}</code>",
        "activity": (
            "<b>Активність:</b> <code>{}</code> повідомлень за останні 24 години"
        ),
        "last_message": "<b>Останнє повідомлення:</b> <code>{}</code>",
        "about": "<b>Опис:</b> {}",
        "megagroup": "супергрупа",
        "broadcast": "канал",
        "gigagroup": "гігагрупа",
        "group": "група",
        "unknown": "чат",
    }

    @staticmethod
    def _format_count(value: int) -> str:
        return f"{value:,}".replace(",", " ")

    @staticmethod
    def _format_user(user: User) -> str:
        parts = [getattr(user, "first_name", None), getattr(user, "last_name", None)]
        name = " ".join(part for part in parts if part).strip()

        if not name:
            name = getattr(user, "username", None) or str(user.id)

        name = utils.escape_html(name)
        username = getattr(user, "username", None)
        suffix = f" (@{utils.escape_html(username)})" if username else ""
        return f'<a href="tg://user?id={user.id}">{name}</a>{suffix}'

    @staticmethod
    def _format_datetime(value: datetime.datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=datetime.timezone.utc)

        return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    def _chat_type(self, entity) -> str:
        if isinstance(entity, Channel):
            if getattr(entity, "gigagroup", False):
                return self.strings("gigagroup")

            if getattr(entity, "megagroup", False):
                return self.strings("megagroup")

            if getattr(entity, "broadcast", False):
                return self.strings("broadcast")

        if isinstance(entity, Chat):
            return self.strings("group")

        return self.strings("unknown")

    async def _get_full_chat(self, entity, chat_id: int):
        with contextlib.suppress(Exception):
            if isinstance(entity, Channel):
                return await self._client(GetFullChannelRequest(channel=entity))

            if isinstance(entity, Chat):
                return await self._client(GetFullChatRequest(chat_id))

        return None

    async def _get_admins(self, entity, full_chat) -> tuple:
        owner = None
        admins = []

        if isinstance(entity, Channel):
            with contextlib.suppress(Exception):
                async for user in self._client.iter_participants(
                    entity,
                    filter=ChannelParticipantsAdmins(),
                ):
                    participant = getattr(user, "participant", None)
                    if isinstance(participant, ChannelParticipantCreator):
                        owner = user
                    else:
                        admins.append(user)

            return owner, admins

        if not full_chat:
            return owner, admins

        participants = getattr(
            getattr(full_chat.full_chat, "participants", None),
            "participants",
            [],
        )
        users = {user.id: user for user in getattr(full_chat, "users", [])}

        for participant in participants:
            user = users.get(getattr(participant, "user_id", None))
            if not user:
                continue

            if isinstance(participant, ChatParticipantCreator):
                owner = user
            elif isinstance(participant, ChatParticipantAdmin):
                admins.append(user)

        return owner, admins

    async def _get_activity(self, entity) -> tuple:
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=1
        )
        count = 0
        last_message_date = None

        with contextlib.suppress(Exception):
            async for msg in self._client.iter_messages(entity, limit=500):
                if not getattr(msg, "date", None):
                    continue

                msg_date = msg.date
                if msg_date.tzinfo is None:
                    msg_date = msg_date.replace(tzinfo=datetime.timezone.utc)

                if last_message_date is None:
                    last_message_date = msg_date

                if msg_date < since:
                    break

                count += 1

            return count, last_message_date

        return None, None

    @loader.command(alias="chat")
    async def чатcmd(self, message: Message):
        """Показати основну інформацію про поточну групу або канал."""
        if not getattr(message, "is_group", False) and not getattr(
            message,
            "is_channel",
            False,
        ):
            await utils.answer(message, self.strings("private_not_allowed"))
            return

        entity = await message.get_chat()
        chat_id = utils.get_chat_id(message)
        full_chat = await self._get_full_chat(entity, chat_id)
        owner, admins = await self._get_admins(entity, full_chat)
        activity_count, last_message_date = await self._get_activity(entity)

        rows = [self.strings("header")]

        title = getattr(entity, "title", None)
        if title:
            rows.append(self.strings("title").format(utils.escape_html(title)))

        if chat_id:
            rows.append(self.strings("chat_id").format(chat_id))

        username = getattr(entity, "username", None)
        if username:
            escaped_username = utils.escape_html(username)
            rows.append(self.strings("username").format(escaped_username))
            rows.append(
                self.strings("link").format(
                    f"https://t.me/{username}",
                    f"t.me/{escaped_username}",
                )
            )

        rows.append(self.strings("chat_type").format(self._chat_type(entity)))

        participants_count = getattr(entity, "participants_count", None) or getattr(
            getattr(full_chat, "full_chat", None),
            "participants_count",
            None,
        )
        if participants_count is not None:
            rows.append(
                self.strings("participants").format(
                    self._format_count(participants_count),
                )
            )

        if owner:
            rows.append(self.strings("owner").format(self._format_user(owner)))

        if admins:
            shown_admins = admins[:10]
            admins_line = ", ".join(self._format_user(admin) for admin in shown_admins)
            if len(admins) > len(shown_admins):
                admins_line += self.strings("admins_more").format(
                    self._format_count(len(admins) - len(shown_admins)),
                )

            rows.append(self.strings("admins").format(admins_line))

        if activity_count is not None:
            rows.append(
                self.strings("activity").format(self._format_count(activity_count))
            )

        if last_message_date:
            rows.append(
                self.strings("last_message").format(
                    self._format_datetime(last_message_date),
                )
            )

        about = getattr(getattr(full_chat, "full_chat", None), "about", None)
        if about:
            rows.append(self.strings("about").format(utils.escape_html(about)))

        await utils.answer(message, "\n".join(rows))
