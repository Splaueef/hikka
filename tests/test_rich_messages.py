"""Regression tests for Rich inline results and terminal output."""

import types
import unittest
from unittest import mock

with mock.patch("sys.argv", ["hikka-tests"]):
    import hikka.main  # noqa: F401 - application bootstrap order

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from hikka.inline.form import Form
from hikka.inline.rich import RichMessageError
from hikka.inline.utils import Utils
from hikka.modules.terminal import MessageEditor


class RichMessageTest(unittest.IsolatedAsyncioTestCase):
    def _form(self):
        manager = object.__new__(Form)
        manager._token = "test-token"
        manager._units = {
            "unit-id": {
                "uid": "unit-id",
                "type": "form",
                "rich": True,
                "rich_active": False,
                "rich_text": "<h3>Rich heading</h3>",
                "text": "<b>Fallback</b>",
            }
        }
        manager._error_events = {}
        manager.generate_markup = lambda _: InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="Next", callback_data="next")]
            ]
        )
        return manager

    async def test_native_form_sends_rich_inline_content(self):
        manager = self._form()
        query = types.SimpleNamespace(id="query-id", query="unit-id")
        with mock.patch(
            "hikka.inline.form.call_rich_api", new_callable=mock.AsyncMock
        ) as api:
            await manager._form_inline_handler(query)

        self.assertEqual(api.call_args.args[:2], ("test-token", "answerInlineQuery"))
        result = api.call_args.args[2]["results"][0]
        self.assertEqual(
            result["input_message_content"]["rich_message"]["html"],
            "<h3>Rich heading</h3>",
        )
        self.assertEqual(
            result["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
            "next",
        )
        self.assertTrue(manager._units["unit-id"]["rich_active"])

    async def test_rejected_rich_result_uses_html_inline_content(self):
        manager = self._form()
        query = types.SimpleNamespace(
            id="query-id", query="unit-id", answer=mock.AsyncMock()
        )
        with mock.patch(
            "hikka.inline.form.call_rich_api",
            new_callable=mock.AsyncMock,
            side_effect=RichMessageError("Unsupported"),
        ):
            await manager._form_inline_handler(query)

        result = query.answer.call_args.args[0][0]
        self.assertEqual(result.input_message_content.message_text, "<b>Fallback</b>")
        self.assertFalse(manager._units["unit-id"]["rich_active"])

    async def test_rich_form_edit_preserves_full_terminal_content(self):
        manager = object.__new__(Utils)
        manager._token = "test-token"
        manager._units = {
            "unit-id": {
                "rich_active": True,
                "rich_text": "<h3>Full result</h3>",
                "text": "<b>Preview</b>",
                "buttons": [],
            }
        }
        manager._validate_markup = lambda _: []
        manager.generate_markup = lambda _: None
        manager.bot = types.SimpleNamespace(edit_message_text=mock.AsyncMock())
        with mock.patch(
            "hikka.inline.utils.call_rich_api", new_callable=mock.AsyncMock
        ) as api:
            result = await manager._edit_unit(
                text="<b>Preview</b>",
                unit_id="unit-id",
                inline_message_id="inline-id",
            )

        self.assertTrue(result)
        self.assertEqual(
            api.call_args.args[2]["rich_message"]["html"],
            "<h3>Full result</h3>",
        )
        manager.bot.edit_message_text.assert_not_awaited()

    def test_terminal_rich_result_escapes_output_and_redacts_secrets(self):
        editor = object.__new__(MessageEditor)
        editor.command = "echo '<unsafe>'"
        editor.stdout = "result <unsafe> password=mysecret"
        editor.stderr = ""
        editor.cwd = "/tmp"
        editor.rc = 0
        editor.config = {"REDACT_SECRETS": True}

        html = editor._rich_final_html()
        self.assertIn("<h3>⌨️ Terminal</h3>", html)
        self.assertIn("<pre>result &lt;unsafe&gt; password=&lt;redacted&gt;</pre>", html)
        self.assertNotIn("mysecret", html)


if __name__ == "__main__":
    unittest.main()
