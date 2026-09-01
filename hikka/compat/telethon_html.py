"""Compatibility helpers for Hikka's legacy custom-emoji HTML syntax."""

import re
from typing import Iterable

from telethon.extensions import html


_LEGACY_OPEN_TAG = re.compile(
    (
        r"<emoji\s+document_id\s*=\s*"
        r"(?:\"(?P<double>\d+)\"|'(?P<single>\d+)'|(?P<plain>\d+))\s*>"
    ),
    flags=re.IGNORECASE,
)
_TELETHON_OPEN_TAG = re.compile(
    r'<tg-emoji\s+emoji-id="(?P<document_id>\d+)">',
    flags=re.IGNORECASE,
)

_native_parse = html.parse
_native_unparse = html.unparse


def _to_telethon_html(value: str) -> str:
    def replace(match: re.Match) -> str:
        document_id = next(group for group in match.groups() if group is not None)
        return f'<tg-emoji emoji-id="{document_id}">'

    return _LEGACY_OPEN_TAG.sub(replace, value).replace("</emoji>", "</tg-emoji>")


def _to_legacy_html(value: str) -> str:
    value = _TELETHON_OPEN_TAG.sub(
        lambda match: f'<emoji document_id="{match.group("document_id")}">',
        value,
    )
    return value.replace("</tg-emoji>", "</emoji>")


def parse(value: str):
    """Parse both current Telethon and historical Hikka custom-emoji tags."""
    if not value:
        return _native_parse(value)

    if getattr(html, "CUSTOM_EMOJIS", True):
        value = _to_telethon_html(value)

    return _native_parse(value)


def unparse(text: str, entities: Iterable) -> str:
    """Preserve Hikka's public HTML format when converting entities to text."""
    value = _native_unparse(text, entities)
    if getattr(html, "CUSTOM_EMOJIS", True):
        return _to_legacy_html(value)

    return _TELETHON_OPEN_TAG.sub("", value).replace("</tg-emoji>", "")


def install() -> None:
    """Install the parser patch once for the process."""
    if getattr(html, "_HIKKA_COMPAT_INSTALLED", False):
        return

    html.CUSTOM_EMOJIS = True
    html.parse = parse
    html.unparse = unparse
    html._HIKKA_COMPAT_INSTALLED = True
