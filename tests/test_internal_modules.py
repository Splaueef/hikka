"""Regression tests for safety fixes in Hikka's internal modules."""

import asyncio
import io
import json
import pathlib
import tempfile
import unittest
import zipfile
from unittest import mock

from git import Repo

with mock.patch("sys.argv", ["hikka-tests"]):
    import hikka.main  # noqa: F401 - mirror the application's bootstrap order

from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, SendReactionRequest

from hikka.modules.api_protection import CONSTRUCTORS, APIRatelimiterMod
from hikka.modules.hikka_backup import HikkaBackupMod
from hikka.modules.loader import LoaderMod
from hikka.modules.terminal import (
    OUTPUT_TRUNCATED_MARKER,
    TerminalMod,
    append_limited_output,
)
from hikka.modules.updater import LEGACY_EXTERNAL_UPDATE_COMMAND, UpdaterMod


class APILimiterTest(unittest.TestCase):
    def test_telethon_request_classes_are_indexed(self):
        self.assertEqual(CONSTRUCTORS["joinChannel"], JoinChannelRequest.CONSTRUCTOR_ID)
        self.assertEqual(
            CONSTRUCTORS["importChatInvite"], ImportChatInviteRequest.CONSTRUCTOR_ID
        )
        self.assertEqual(
            CONSTRUCTORS["sendReaction"], SendReactionRequest.CONSTRUCTOR_ID
        )

    def test_configured_forbidden_methods_reach_the_client(self):
        module = object.__new__(APIRatelimiterMod)
        module.config = {"forbidden_methods": ["joinChannel", "sendReaction"]}
        module._client = mock.Mock()

        module._apply_forbidden_methods()

        module._client.forbid_constructors.assert_called_once_with(
            [
                JoinChannelRequest.CONSTRUCTOR_ID,
                SendReactionRequest.CONSTRUCTOR_ID,
            ]
        )


class BackupValidationTest(unittest.TestCase):
    @staticmethod
    def _archive(*members: tuple[str, bytes]) -> bytes:
        result = io.BytesIO()
        with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in members:
                archive.writestr(name, data)
        return result.getvalue()

    def test_database_backup_requires_an_object_root(self):
        with self.assertRaisesRegex(ValueError, "root must be an object"):
            HikkaBackupMod._decode_database_backup(b"[]")

        self.assertEqual(
            HikkaBackupMod._decode_database_backup(b'{"owner": 1}'),
            {"owner": 1},
        )

    def test_redis_clear_removes_current_and_legacy_keys(self):
        module = object.__new__(HikkaBackupMod)
        redis_client = mock.Mock()
        module._redis = mock.Mock(return_value=redis_client)
        module._redis_key = mock.Mock(return_value="hikka:db:1:primary")
        module._redis_legacy_key = mock.Mock(return_value="1")

        module._redis_clear_sync()

        redis_client.delete.assert_called_once_with("hikka:db:1:primary", "1")

    def test_redis_save_rejects_an_unrestorable_oversized_backup(self):
        module = object.__new__(HikkaBackupMod)
        module._db = {"large": "payload"}
        module._redis = mock.Mock()

        with (
            mock.patch("hikka.modules.hikka_backup.MAX_DATABASE_BACKUP_SIZE", 5),
            self.assertRaisesRegex(ValueError, "too large"),
        ):
            module._redis_save_sync()

        module._redis.assert_not_called()

    def test_module_archive_is_validated_before_extraction(self):
        metadata = json.dumps({"example": "https://example.com/example.py"}).encode()
        payload = self._archive(
            ("example.py", b"print('safe')\n"),
            ("db_mods.json", metadata),
        )

        db_mods, files = HikkaBackupMod._decode_module_archive(payload)

        self.assertEqual(db_mods, {"example": "https://example.com/example.py"})
        self.assertEqual(files, {"example.py": b"print('safe')\n"})

    def test_created_module_archive_round_trips_through_validator(self):
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            mock.patch(
                "hikka.modules.hikka_backup.loader.LOADED_MODULES_DIR",
                temporary_directory,
            ),
        ):
            module_path = pathlib.Path(temporary_directory) / "example1.py"
            module_path.write_text("print('safe')\n", encoding="utf-8")
            payload, local_modules = HikkaBackupMod._build_module_archive(
                {"example": "https://example.com/example.py"},
                1,
            )

        db_mods, files = HikkaBackupMod._decode_module_archive(payload)

        self.assertEqual(local_modules, 1)
        self.assertEqual(db_mods, {"example": "https://example.com/example.py"})
        self.assertEqual(files, {"example1.py": b"print('safe')\n"})

    def test_module_archive_rejects_path_traversal(self):
        payload = self._archive(
            ("../unsafe.py", b"pass\n"),
            ("db_mods.json", b"{}"),
        )

        with self.assertRaisesRegex(ValueError, "Unsafe module backup member"):
            HikkaBackupMod._decode_module_archive(payload)


class LoaderCacheTest(unittest.TestCase):
    def test_cache_metrics_count_links_not_metadata_fields(self):
        module = object.__new__(LoaderMod)
        module._links_cache = {
            "primary": {"exp": 1, "data": ["one", "two"]},
            "secondary": {"exp": 1, "data": ["three"]},
        }

        self.assertEqual(module.inspect_cache(), 3)
        self.assertEqual(module.flush_cache(), 3)
        self.assertEqual(module._links_cache, {})

    def test_clear_modules_removes_files_and_directories(self):
        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            mock.patch(
                "hikka.modules.loader.loader.LOADED_MODULES_DIR",
                temporary_directory,
            ),
        ):
            root = pathlib.Path(temporary_directory)
            (root / "module.py").write_text("pass\n", encoding="utf-8")
            nested = root / "cache"
            nested.mkdir()
            (nested / "data").write_text("cached", encoding="utf-8")

            LoaderMod._clear_loaded_modules()

            self.assertEqual(list(root.iterdir()), [])


class TerminalOutputTest(unittest.TestCase):
    def test_terminal_output_keeps_a_bounded_recent_tail(self):
        limit = len(OUTPUT_TRUNCATED_MARKER) + 8
        output = bytearray(b"old-output" * 10)

        append_limited_output(output, b"-new-output", limit)

        self.assertEqual(len(output), limit)
        self.assertTrue(output.startswith(OUTPUT_TRUNCATED_MARKER))
        self.assertTrue(output.endswith(b"w-output"))


class TerminalProcessTest(unittest.IsolatedAsyncioTestCase):
    async def test_stop_process_terminates_the_process_group(self):
        process = await asyncio.create_subprocess_shell(
            "sleep 30 & wait",
            start_new_session=True,
        )

        await TerminalMod._stop_process(process)

        self.assertIsNotNone(process.returncode)


class ExternalUpdaterTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _module(timeout: int = 10) -> UpdaterMod:
        module = object.__new__(UpdaterMod)
        module.config = {"EXTERNAL_UPDATE_TIMEOUT": timeout}
        module.strings = lambda key: UpdaterMod.strings[key]
        return module

    def test_compare_urls_do_not_expose_credentials_or_queries(self):
        module = self._module()

        url = module._get_compare_url(
            "https://user:secret@example.com/project.git?token=secret#fragment",
            "a" * 40,
            "b" * 40,
        )

        self.assertEqual(
            url,
            "https://example.com/project/compare/aaaaaaaaaaaa...bbbbbbbbbbbb",
        )

    def test_legacy_destructive_command_is_migrated(self):
        module = self._module()
        services = [
            {
                "name": "site",
                "branch": "main",
                "command": LEGACY_EXTERNAL_UPDATE_COMMAND.format(branch="main"),
            }
        ]
        saved = []
        module.get = lambda key, default=None: (
            services if key == "external_services" else default
        )
        module.set = lambda key, value: saved.append((key, value))

        result = module._get_external_services()

        self.assertEqual(result[0]["command"], module._default_external_command("main"))
        self.assertNotIn("reset --hard", result[0]["command"])
        self.assertEqual(saved, [("external_services", result)])

    async def test_default_update_fast_forwards_without_resetting_files(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = pathlib.Path(temporary_directory)
            origin_path = root / "origin.git"
            seed_path = root / "seed"
            target_path = root / "target"

            Repo.init(origin_path, bare=True)
            seed = Repo.init(seed_path)
            with seed.config_writer() as config:
                config.set_value("user", "name", "Hikka Tests")
                config.set_value("user", "email", "tests@example.com")
            seed.git.checkout("-b", "main")
            source = seed_path / "version.txt"
            source.write_text("one\n", encoding="utf-8")
            seed.index.add(["version.txt"])
            seed.index.commit("initial")
            seed.create_remote("origin", str(origin_path)).push("main:main")

            Repo.clone_from(str(origin_path), target_path, branch="main")
            source.write_text("two\n", encoding="utf-8")
            seed.index.add(["version.txt"])
            remote_commit = seed.index.commit("update").hexsha
            seed.remote("origin").push("main:main")

            service = {
                "name": "site",
                "path": str(target_path),
                "repo_url": str(origin_path),
                "branch": "main",
                "command": module._default_external_command("main"),
            }
            result = await module._run_external_update(service)

            self.assertTrue(result["updated"])
            self.assertEqual(result["new"], remote_commit)
            self.assertEqual((target_path / "version.txt").read_text(), "two\n")

            (target_path / "version.txt").write_text("local\n", encoding="utf-8")
            source.write_text("three\n", encoding="utf-8")
            seed.index.add(["version.txt"])
            seed.index.commit("second update")
            seed.remote("origin").push("main:main")

            with self.assertRaisesRegex(ValueError, "local changes present"):
                await module._run_external_update(service)

            self.assertEqual((target_path / "version.txt").read_text(), "local\n")


if __name__ == "__main__":
    unittest.main()
