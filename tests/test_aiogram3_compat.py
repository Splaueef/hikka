"""Smoke tests for Hikka's modern Bot API compatibility helpers."""

import datetime
import types
import unittest
from unittest import mock

with mock.patch("sys.argv", ["hikka-tests"]):
    import hikka.main  # noqa: F401 - mirror the application's bootstrap order
from aiogram.types import InlineKeyboardMarkup
from telethon.tl.types import Updates

from hikka.compat.pyroproxy import PyroProxyClient
from hikka.inline.types import InlineCall
from hikka.inline.utils import Utils


class Aiogram3CompatTest(unittest.IsolatedAsyncioTestCase):
    def test_pyrofork_proxy_round_trip(self):
        proxy = object.__new__(PyroProxyClient)
        source = Updates(
            updates=[],
            users=[],
            chats=[],
            date=datetime.datetime.now(datetime.timezone.utc),
            seq=1,
        )

        pyrogram_update = proxy._tl2pyro(source)
        telethon_update = proxy._pyro2tl(pyrogram_update)

        self.assertEqual(telethon_update.seq, 1)
        self.assertIsInstance(telethon_update, Updates)

    def test_markup_uses_aiogram3_model(self):
        manager = object.__new__(Utils)
        manager._units = {}
        manager._custom_map = {}

        markup = manager.generate_markup(
            [[{"text": "Confirm", "data": "confirm"}]]
        )

        self.assertIsInstance(markup, InlineKeyboardMarkup)
        self.assertEqual(markup.inline_keyboard[0][0].text, "Confirm")
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "confirm")

    async def test_inline_call_delegates_to_bound_update(self):
        answered = []

        async def answer(*args, **kwargs):
            answered.append((args, kwargs))

        update = types.SimpleNamespace(
            id="callback-id",
            from_user=types.SimpleNamespace(id=1),
            message=None,
            inline_message_id="inline-id",
            chat_instance="chat-instance",
            data="confirm",
            game_short_name=None,
            answer=answer,
        )
        manager = types.SimpleNamespace(_units={"unit": {}})

        call = InlineCall(update, manager, "unit")
        await call.answer("Done", show_alert=True)

        self.assertEqual(call.id, "callback-id")
        self.assertEqual(answered, [(("Done",), {"show_alert": True})])


if __name__ == "__main__":
    unittest.main()
