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

from hikka import database, dispatcher, loader, log, security, tl_cache, utils
from hikka.inline import core as inline_core
from hikka.types import CacheRecordEntity, CacheRecordPerms


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


    def test_broken_change_is_rolled_back_to_deep_revision(self):
        db = _database()
        db.set("Module", "nested", {"value": 1})

        db["Module"]["nested"]["value"] = object()
        with self.assertRaises(RuntimeError):
            db.save()

        self.assertEqual(db["Module"]["nested"], {"value": 1})

    def test_revisions_keep_the_newest_snapshots(self):
        db = _database()
        with mock.patch.object(database.time, "time", side_effect=range(0, 1000, 10)):
            for value in range(20):
                db.set("Module", "key", value)

        self.assertEqual(len(db._revisions), 5)
        self.assertEqual(json.loads(db._revisions[-1])["Module"]["key"], 19)

    def test_file_is_written_compact(self):
        db = _database()
        db.set("Module", "key", {"a": 1})

        self.assertNotIn("\n", db._db_file.read_text())

    def test_circular_value_is_not_serializable(self):
        value = []
        value.append(value)

        self.assertFalse(utils.is_serializable(value))
        with self.assertRaises(RuntimeError):
            _database().set("Module", "key", value)


class CensorTest(unittest.TestCase):
    def test_default_censoring_is_done_once(self):
        obj = types.SimpleNamespace(phone="380000000000")
        utils.censor(obj)
        self.assertEqual(obj.phone, "redacted_12_chars")

        obj.phone = "123"
        utils.censor(obj)
        self.assertEqual(obj.phone, "123")

        utils.censor(obj, ["phone"], "x")
        self.assertEqual(obj.phone, "x")


class ChannelEditorTest(unittest.IsolatedAsyncioTestCase):
    async def test_admin_log_is_requested_once_per_message(self):
        requests = []

        async def iter_admin_log(*args, **kwargs):
            requests.append((args, kwargs))
            for user_id, message_id in ((7, 5), (8, 5)):
                yield types.SimpleNamespace(
                    user_id=user_id,
                    action=types.SimpleNamespace(
                        prev_message=types.SimpleNamespace(id=message_id)
                    ),
                )

        client = _Client()
        client.iter_admin_log = iter_admin_log
        manager = security.SecurityManager(client, _database())
        message = types.SimpleNamespace(id=5, chat_id=-1001234)

        self.assertEqual(await manager.get_channel_editor(message), 7)
        self.assertEqual(await manager.get_channel_editor(message), 7)
        self.assertEqual(len(requests), 1)


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


class LogCallerTest(unittest.TestCase):
    def test_caller_is_found_without_inspecting_stack(self):
        def log_from_client():
            _hikka_client_id_logging_tag = 42  # noqa: F841
            return nested()

        def nested():
            return log._find_logging_client()

        with mock.patch.object(log.inspect, "stack") as stack:
            self.assertEqual(log_from_client(), 42)
            self.assertIsNone(nested())

        stack.assert_not_called()


class LogBufferTest(unittest.TestCase):
    def _record(self, *args, exc_info=None):
        return log.logging.LogRecord(
            "hikka.test", log.logging.ERROR, __file__, 1, "value %s", args, exc_info
        )

    def test_buffered_record_drops_heavy_references(self):
        try:
            raise ValueError("boom")
        except ValueError:
            exc_info = log.sys.exc_info()

        record = self._record(object(), exc_info=exc_info)
        record.stack = ["frame"]
        log._lighten(record)

        self.assertIsNone(record.args)
        self.assertTrue(record.msg.startswith("value <object"))
        self.assertIsNone(record.exc_info)
        self.assertIn("ValueError: boom", record.exc_text)
        self.assertFalse(hasattr(record, "stack"))
        self.assertIn("ValueError: boom", log._main_formatter.format(record))

    def test_plain_arguments_are_kept_lazy(self):
        record = self._record("text")
        log._lighten(record)

        self.assertEqual(record.args, ("text",))
        self.assertEqual(record.getMessage(), "value text")


class InlineCleanerTest(unittest.IsolatedAsyncioTestCase):
    async def test_units_without_ttl_expire(self):
        manager = object.__new__(inline_core.InlineManager)
        manager._markup_ttl = 100
        manager._units = {"form": {}, "fresh": {"ttl": 10_000}}

        with mock.patch.object(
            inline_core.time, "time", side_effect=[1000, 1200]
        ), mock.patch.object(
            inline_core.asyncio, "sleep", side_effect=[None, asyncio.CancelledError]
        ):
            with self.assertRaises(asyncio.CancelledError):
                await manager._cleaner()

        self.assertEqual(list(manager._units), ["fresh"])


class EntityCachePurgeTest(unittest.TestCase):
    def test_old_records_are_purged(self):
        client = object.__new__(tl_cache.CustomTelegramClient)
        client._hikka_next_cache_purge = 0
        old = CacheRecordEntity(1, object(), 0)
        old.ts = 0
        new = CacheRecordEntity(2, object(), 0)
        old_perms = CacheRecordPerms(1, 1, object(), 0)
        old_perms.ts = 0
        client._hikka_entity_cache = {1: old, "@old": old, 2: new}
        client._hikka_perms_cache = {1: {1: old_perms}, 2: {2: new}}
        client._hikka_fullchannel_cache = {}
        client._hikka_fulluser_cache = {}

        client._purge_caches()

        self.assertEqual(client._hikka_entity_cache, {2: new})
        self.assertEqual(client._hikka_perms_cache, {2: {2: new}})

        client._hikka_entity_cache[3] = old
        client._purge_caches()
        self.assertIn(3, client._hikka_entity_cache)


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
