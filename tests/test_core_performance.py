"""Regression tests for the per-message hot paths and deferred database saves."""

import asyncio
import json
import pathlib
import tempfile
import types
import unittest
from unittest import mock

with mock.patch("sys.argv", ["hikka-tests"]):
    import hikka.main  # noqa: F401 - mirror the application's bootstrap order
from telethon.tl.types import Message, PeerChat, PeerUser

from hikka import database, dispatcher, loader


class _Client:
    tg_id = 1
    hikka_me = types.SimpleNamespace(id=1, username="me", usernames=None)


def _database() -> database.Database:
    db = database.Database(_Client())
    db._db_file = pathlib.Path(tempfile.mkdtemp()) / "config-1.json"
    return db


def _read(db: database.Database) -> dict:
    return json.loads(db._db_file.read_text())


class DatabaseSaveTest(unittest.IsolatedAsyncioTestCase):
    async def test_set_in_event_loop_writes_once_after_delay(self):
        db = _database()
        with mock.patch.object(database, "SAVE_DELAY", 0.01):
            for value in range(50):
                db.set("Stats", "count", value)

            self.assertFalse(db._db_file.exists())
            with mock.patch.object(db, "_write_file", wraps=db._write_file) as write:
                await asyncio.sleep(0.05)

        write.assert_called_once()
        self.assertEqual(_read(db)["Stats"]["count"], 49)

    async def test_flush_writes_pending_changes_immediately(self):
        db = _database()
        db.set("Module", "key", "value")
        database.flush_all()

        self.assertEqual(_read(db)["Module"]["key"], "value")
        self.assertIsNone(db._save_handle)
        self.assertFalse(db._db_file.with_name("config-1.json.tmp").exists())

    def test_set_without_event_loop_writes_immediately(self):
        db = _database()
        db.set("Module", "key", [1, 2])

        self.assertEqual(_read(db)["Module"]["key"], [1, 2])

    def test_write_falls_back_when_file_cannot_be_replaced(self):
        db = _database()
        with mock.patch.object(database.os, "replace", side_effect=OSError):
            db.set("Module", "key", 1)

        self.assertEqual(_read(db)["Module"]["key"], 1)
        self.assertFalse(db._db_file.with_name("config-1.json.tmp").exists())


class _Module:
    strings = {"name": "Test"}


def _watcher(**tags):
    calls = []

    async def watcher(message):
        calls.append(message)

    watcher.__self__ = _Module()
    watcher.__module__ = "hikka.modules.test"
    for tag, value in tags.items():
        setattr(watcher, tag, value)

    return watcher, calls


def _event(text: str, out: bool = False) -> types.SimpleNamespace:
    message = Message(
        id=1,
        peer_id=PeerChat(chat_id=10),
        date=None,
        message=text,
        out=out,
        from_id=PeerUser(2),
    )
    return types.SimpleNamespace(
        message=message,
        sticker=None,
        dice=None,
        audio=None,
        via_bot_id=None,
        out=out,
        mentioned=False,
        is_private=False,
    )


class DispatcherTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = _database()
        self.modules = types.SimpleNamespace(
            watchers=[],
            dispatch=lambda command: (command, None),
        )
        self.dispatcher = dispatcher.CommandDispatcher(
            self.modules,
            _Client(),
            self.db,
        )

    async def _dispatch(self, event):
        await self.dispatcher.handle_incoming(event)
        await asyncio.sleep(0)

    async def test_watcher_tags_are_applied(self):
        plain, plain_calls = _watcher()
        out_only, out_calls = _watcher(out=True)
        incoming, in_calls = _watcher(**{"in": True})
        contains, contains_calls = _watcher(contains="needle")
        self.modules.watchers = [plain, out_only, incoming, contains]

        await self._dispatch(_event("a needle here"))

        self.assertEqual(len(plain_calls), 1)
        self.assertEqual(out_calls, [])
        self.assertEqual(len(in_calls), 1)
        self.assertEqual(len(contains_calls), 1)

    async def test_command_check_runs_once_per_event(self):
        watchers = [_watcher(no_commands=True) for _ in range(5)]
        only_commands, only_calls = _watcher(only_commands=True)
        self.modules.watchers = [watcher for watcher, _ in watchers] + [
            only_commands
        ]

        with mock.patch.object(
            self.dispatcher,
            "_handle_command",
            wraps=self.dispatcher._handle_command,
        ) as handle_command:
            await self._dispatch(_event("hello"))

        handle_command.assert_called_once()
        self.assertTrue(all(len(calls) == 1 for _, calls in watchers))
        self.assertEqual(only_calls, [])

    async def test_plain_message_is_not_censored_by_command_handler(self):
        with mock.patch.object(dispatcher.utils, "censor") as censor:
            self.assertFalse(await self.dispatcher._handle_command(_event("hello")))

        censor.assert_not_called()


class LoaderDispatchTest(unittest.TestCase):
    def test_alias_scan_is_skipped_for_known_command(self):
        modules = object.__new__(loader.Modules)
        modules.commands = {"ping": object()}
        modules.aliases = {"p": "ping"}

        with mock.patch.object(loader.Modules, "find_alias") as find_alias:
            self.assertEqual(modules.dispatch("ping")[0], "ping")
            self.assertEqual(modules.dispatch("p")[0], "ping")

        find_alias.assert_not_called()


if __name__ == "__main__":
    unittest.main()
