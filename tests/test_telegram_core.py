"""Regression tests for the maintained Telegram client migration."""

import unittest

import hikka  # noqa: F401 - installs the HTML compatibility layer
import pyrogram
from telethon import events
from telethon.extensions import html
from telethon.tl import alltlobjects
from telethon.tl.types import (
    DocumentAttributeVideo,
    MessageEntityCustomEmoji,
    MessageMediaDocument,
)


class TelegramCoreTest(unittest.TestCase):
    def setUp(self):
        self.custom_emojis = html.CUSTOM_EMOJIS

    def tearDown(self):
        html.CUSTOM_EMOJIS = self.custom_emojis

    def test_current_layer_contains_ephemeral_round_media_flags(self):
        self.assertGreaterEqual(alltlobjects.LAYER, 227)
        self.assertGreaterEqual(pyrogram.raw.all.layer, 220)

        media = MessageMediaDocument(round=True, ttl_seconds=0)
        attribute = DocumentAttributeVideo(
            duration=1,
            w=384,
            h=384,
            round_message=True,
        )

        self.assertEqual(media.ttl_seconds, 0)
        self.assertTrue(media.round)
        self.assertTrue(attribute.round_message)

    def test_new_message_builder_keeps_both_directions(self):
        builder = events.NewMessage()

        self.assertIsNone(builder.incoming)
        self.assertIsNone(builder.outgoing)

    def test_legacy_custom_emoji_html_round_trip(self):
        source = '<b>Hello</b> <emoji document_id="5377399247589088543">🔥</emoji>'

        text, entities = html.parse(source)

        custom_emoji = next(
            entity
            for entity in entities
            if isinstance(entity, MessageEntityCustomEmoji)
        )
        self.assertEqual(custom_emoji.document_id, 5377399247589088543)
        self.assertIn(
            '<emoji document_id="5377399247589088543">',
            html.unparse(text, entities),
        )

    def test_custom_emoji_entities_can_be_disabled(self):
        html.CUSTOM_EMOJIS = False

        text, entities = html.parse(
            '<emoji document_id="5377399247589088543">🔥</emoji>'
        )

        self.assertEqual(text, "🔥")
        self.assertFalse(
            any(isinstance(entity, MessageEntityCustomEmoji) for entity in entities)
        )


if __name__ == "__main__":
    unittest.main()
