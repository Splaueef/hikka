# ©️ Dan Gazizullin, 2021-2023
# This file is a part of Hikka Userbot
# 🌐 https://github.com/Splaueef/hikka
# You can redistribute it and/or modify it under the terms of the GNU AGPLv3
# 🔑 https://www.gnu.org/licenses/agpl-3.0.html

import asyncio
import contextlib
import datetime
import io
import json
import logging
import os
import re
import time
import typing
import zipfile
from pathlib import Path

import redis

from telethon.tl.types import Message

from .. import loader, main, utils
from ..inline.types import BotInlineCall

logger = logging.getLogger(__name__)

MAX_MODULE_BACKUP_FILES = 200
MAX_MODULE_BACKUP_TOTAL_SIZE = 25 * 1024 * 1024
MAX_MODULE_BACKUP_FILE_SIZE = 2 * 1024 * 1024
MAX_DATABASE_BACKUP_SIZE = 50 * 1024 * 1024


@loader.tds
class HikkaBackupMod(loader.Module):
    """Керує резервними копіями бази даних і модулів."""

    strings = {
        "name": "HikkaBackup",
        "invalid_backup": "<b>Invalid or unsafe backup file</b>",
        "redis_saved": (
            "<emoji document_id=5206607081334906820>✅</emoji> <b>Database backup saved to Redis</b>"
        ),
        "redis_loaded": (
            "<emoji document_id=5774134533590880843>🔄</emoji> <b>Database loaded from Redis, restarting...</b>"
        ),
        "redis_missing": (
            "<emoji document_id=5312383351217201533>🚫</emoji> <b>No Redis backup found</b>"
        ),
        "redis_cleared": (
            "<emoji document_id=5206607081334906820>✅</emoji> <b>Redis database cleared</b>"
        ),
        "redis_ok": (
            "<emoji document_id=5206607081334906820>✅</emoji> <b>Redis is available. Backup size: {size} bytes</b>"
        ),
        "redis_users_empty": (
            "<emoji document_id=5312383351217201533>🚫</emoji> <b>No Redis database backups found</b>"
        ),
        "redis_users": (
            "<emoji document_id=5431736674147114227>🗂</emoji> <b>Redis database backups:</b>\n\n{users}"
        ),
        "redis_user": "<code>{tg_id}</code> — <code>{key}</code> ({size} bytes)",
        "redis_error": (
            "<emoji document_id=5312383351217201533>🚫</emoji> <b>Redis error:</b> <code>{error}</code>"
        ),
    }

    def __init__(self):
        self.config = loader.ModuleConfig(
            loader.ConfigValue(
                "redis_uri",
                "127.0.0.1:6379",
                "Redis URI for database backups",
                validator=loader.validators.String(),
            ),
            loader.ConfigValue(
                "redis_password",
                "OOooOO",
                "Redis password for database backups",
                validator=loader.validators.Hidden(),
            ),
            loader.ConfigValue(
                "redis_timeout",
                10,
                "Redis connection and operation timeout, in seconds",
                validator=loader.validators.Integer(minimum=1, maximum=60),
            ),
        )

    @staticmethod
    def _normalize_redis_uri(uri: str) -> str:
        uri = (uri or "").strip()
        if "://" not in uri:
            uri = f"redis://{uri}"

        return uri

    def _redis_key(self) -> str:
        return main.get_database_key(self._client.tg_id)

    def _redis_legacy_key(self) -> str:
        return str(self._client.tg_id)

    def _redis(self) -> redis.Redis:
        password = self.config["redis_password"] or None
        timeout = int(self.config["redis_timeout"])
        return redis.Redis.from_url(
            self._normalize_redis_uri(self.config["redis_uri"]),
            password=password,
            decode_responses=False,
            socket_connect_timeout=timeout,
            socket_timeout=timeout,
        )

    @staticmethod
    def _decode_database_backup(payload: typing.Union[bytes, str]) -> dict:
        raw_payload = payload.encode() if isinstance(payload, str) else payload
        if len(raw_payload) > MAX_DATABASE_BACKUP_SIZE:
            raise ValueError("Database backup is too large")

        try:
            decoded = json.loads(raw_payload.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError("Database backup is not valid JSON") from e

        if not isinstance(decoded, dict):
            raise ValueError("Database backup root must be an object")

        return decoded

    def _redis_save_sync(self) -> int:
        payload = json.dumps(self._db, ensure_ascii=True)
        payload_size = len(payload.encode())
        if payload_size > MAX_DATABASE_BACKUP_SIZE:
            raise ValueError("Database backup is too large")

        client = self._redis()
        client.set(self._redis_key(), payload)
        return payload_size

    def _redis_load_sync(self) -> typing.Optional[dict]:
        client = self._redis()
        payload = client.get(self._redis_key()) or client.get(self._redis_legacy_key())
        if not payload:
            return None

        return self._decode_database_backup(payload)

    def _redis_clear_sync(self) -> None:
        self._redis().delete(self._redis_key(), self._redis_legacy_key())

    def _redis_check_sync(self) -> int:
        client = self._redis()
        client.ping()
        payload = client.get(self._redis_key()) or client.get(self._redis_legacy_key())
        return len(payload or b"")

    def _redis_users_sync(
        self,
    ) -> typing.List[typing.Dict[str, typing.Union[str, int]]]:
        client = self._redis()
        client.ping()
        users = {}

        for key in client.scan_iter(match="hikka:db:*"):
            key = key.decode() if isinstance(key, bytes) else key
            parts = key.split(":", 3)
            if len(parts) < 4:
                continue

            payload = client.get(key)
            users[key] = {
                "tg_id": parts[2],
                "key": key,
                "size": len(payload or b""),
            }

        for key in client.scan_iter():
            key = key.decode() if isinstance(key, bytes) else key
            if not re.fullmatch(r"\d+", key) or key in users:
                continue

            payload = client.get(key)
            users[key] = {
                "tg_id": key,
                "key": key,
                "size": len(payload or b""),
            }

        return sorted(
            users.values(), key=lambda item: (str(item["tg_id"]), str(item["key"]))
        )

    async def _save_to_redis(self) -> int:
        return await utils.run_sync(self._redis_save_sync)

    async def _load_from_redis(self) -> typing.Optional[dict]:
        return await utils.run_sync(self._redis_load_sync)

    async def _list_redis_users(
        self,
    ) -> typing.List[typing.Dict[str, typing.Union[str, int]]]:
        return await utils.run_sync(self._redis_users_sync)

    async def client_ready(self):
        if not self.get("period"):
            await self.inline.bot.send_photo(
                self.tg_id,
                photo="https://github.com/Splaueef/assets/raw/main/unit_alpha.png",
                caption=self.strings("period"),
                reply_markup=self.inline.generate_markup(
                    utils.chunks(
                        [
                            {
                                "text": f"🕰 {i} h",
                                "callback": self._set_backup_period,
                                "args": (i,),
                            }
                            for i in [1, 2, 4, 6, 8, 12, 24, 48, 168]
                        ],
                        3,
                    )
                    + [
                        [
                            {
                                "text": "🚫 Never",
                                "callback": self._set_backup_period,
                                "args": (0,),
                            }
                        ]
                    ]
                ),
            )

    async def _set_backup_period(self, call: BotInlineCall, value: int):
        if not value:
            self.set("period", "disabled")
            await call.answer(self.strings("never"), show_alert=True)
            await call.delete()
            return

        self.set("period", value * 60 * 60)
        self.set("last_backup", round(time.time()))

        await call.answer(self.strings("saved"), show_alert=True)
        await call.delete()

    @loader.command()
    async def set_backup_period(self, message: Message):
        if (
            not (args := utils.get_args_raw(message))
            or not args.isdigit()
            or int(args) not in range(200)
        ):
            await utils.answer(message, self.strings("invalid_args"))
            return

        if not int(args):
            self.set("period", "disabled")
            await utils.answer(message, f"<b>{self.strings('never')}</b>")
            return

        period = int(args) * 60 * 60
        self.set("period", period)
        self.set("last_backup", round(time.time()))
        await utils.answer(message, f"<b>{self.strings('saved')}</b>")

    @loader.loop(interval=60, autostart=True)
    async def handler(self):
        period = self.get("period")
        if not isinstance(period, (int, float)) or period <= 0:
            return

        now = time.time()
        last_backup = self.get("last_backup")
        if not isinstance(last_backup, (int, float)):
            self.set("last_backup", round(now))
            return

        if now < last_backup + period:
            return

        try:
            await self._save_to_redis()
            self.set("last_backup", round(time.time()))
        except Exception:
            logger.exception("HikkaBackup failed")

    @loader.command()
    async def backupdb(self, message: Message):
        try:
            await self._save_to_redis()
        except Exception as e:
            logger.exception("Unable to save database to Redis")
            await utils.answer(
                message,
                self.strings("redis_error").format(error=utils.escape_html(str(e))),
            )
            return

        await utils.answer(message, self.strings("redis_saved"))

    @loader.command()
    async def loaddb(self, message: Message):
        try:
            decoded_text = await self._load_from_redis()
        except Exception as e:
            logger.exception("Unable to load database from Redis")
            await utils.answer(
                message,
                self.strings("redis_error").format(error=utils.escape_html(str(e))),
            )
            return

        if not decoded_text:
            await utils.answer(message, self.strings("redis_missing"))
            return

        with contextlib.suppress(KeyError):
            decoded_text["hikka.inline"].pop("bot_token")

        if not self._db.process_db_autofix(decoded_text):
            await utils.answer(message, self.strings("invalid_backup"))
            return

        self._db.clear()
        self._db.update(**decoded_text)
        self._db.save()

        await utils.answer(message, self.strings("redis_loaded"))
        await self.invoke("restart", "-f", peer=message.peer_id)

    @loader.command()
    async def checkdb(self, message: Message):
        try:
            size = await utils.run_sync(self._redis_check_sync)
        except Exception as e:
            logger.exception("Unable to check Redis")
            await utils.answer(
                message,
                self.strings("redis_error").format(error=utils.escape_html(str(e))),
            )
            return

        await utils.answer(message, self.strings("redis_ok").format(size=size))

    @loader.command()
    async def listdb(self, message: Message):
        try:
            users = await self._list_redis_users()
        except Exception as e:
            logger.exception("Unable to list Redis database backups")
            await utils.answer(
                message,
                self.strings("redis_error").format(error=utils.escape_html(str(e))),
            )
            return

        if not users:
            await utils.answer(message, self.strings("redis_users_empty"))
            return

        await utils.answer(
            message,
            self.strings("redis_users").format(
                users="\n".join(
                    self.strings("redis_user").format(
                        tg_id=utils.escape_html(str(user["tg_id"])),
                        key=utils.escape_html(str(user["key"])),
                        size=user["size"],
                    )
                    for user in users
                )
            ),
        )

    @loader.command()
    async def cleardb(self, message: Message):
        try:
            await utils.run_sync(self._redis_clear_sync)
        except Exception as e:
            logger.exception("Unable to clear Redis")
            await utils.answer(
                message,
                self.strings("redis_error").format(error=utils.escape_html(str(e))),
            )
            return

        await utils.answer(message, self.strings("redis_cleared"))

    @loader.command()
    async def restoredb(self, message: Message):
        if not (reply := await message.get_reply_message()) or not reply.media:
            await utils.answer(
                message,
                self.strings("reply_to_file"),
            )
            return

        file_size = getattr(getattr(reply, "file", None), "size", None)
        if file_size is not None and file_size > MAX_DATABASE_BACKUP_SIZE:
            await utils.answer(message, self.strings("invalid_backup"))
            return

        file = await reply.download_media(bytes)
        try:
            decoded_text = self._decode_database_backup(file)
        except ValueError:
            logger.exception("Unable to decode database backup")
            await utils.answer(message, self.strings("invalid_backup"))
            return

        with contextlib.suppress(KeyError):
            decoded_text["hikka.inline"].pop("bot_token")

        if not self._db.process_db_autofix(decoded_text):
            await utils.answer(message, self.strings("invalid_backup"))
            return

        self._db.clear()
        self._db.update(**decoded_text)
        self._db.save()

        await utils.answer(message, self.strings("db_restored"))
        await self.invoke("restart", "-f", peer=message.peer_id)

    @loader.command()
    async def backupmods(self, message: Message):
        loaded_modules = self.lookup("Loader").get("loaded_modules", {})
        try:
            payload, local_modules = await asyncio.to_thread(
                self._build_module_archive,
                loaded_modules,
                self.tg_id,
            )
        except (OSError, ValueError, zipfile.BadZipFile):
            logger.exception("Unable to create modules backup")
            await utils.answer(message, self.strings("invalid_backup"))
            return

        archive = io.BytesIO(payload)
        archive.name = f"mods-{datetime.datetime.now():%d-%m-%Y-%H-%M}.zip"

        await utils.answer_file(
            message,
            archive,
            caption=self.strings("modules_backup").format(
                len(loaded_modules) + local_modules,
                utils.escape_html(self.get_prefix()),
            ),
        )

    @classmethod
    def _build_module_archive(cls, db_mods: dict, tg_id: int) -> tuple[bytes, int]:
        if not cls._valid_module_map(db_mods):
            raise ValueError("Invalid modules backup metadata")

        metadata = json.dumps(db_mods).encode()
        if len(metadata) > MAX_MODULE_BACKUP_FILE_SIZE:
            raise ValueError("Modules backup metadata is too large")

        result = io.BytesIO()
        total_size = len(metadata)
        file_count = 1
        local_modules = 0
        seen_names = {"db_mods.json"}

        with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
            for root, _, files in os.walk(loader.LOADED_MODULES_DIR):
                for name in files:
                    if not name.endswith(f"{tg_id}.py"):
                        continue
                    if name in seen_names:
                        raise ValueError(f"Duplicate module backup member: {name}")
                    if file_count >= MAX_MODULE_BACKUP_FILES:
                        raise ValueError("Too many files in modules backup")

                    path = os.path.join(root, name)
                    with open(path, "rb") as module_file:
                        data = module_file.read(MAX_MODULE_BACKUP_FILE_SIZE + 1)

                    size = len(data)
                    total_size += size
                    if (
                        size > MAX_MODULE_BACKUP_FILE_SIZE
                        or total_size > MAX_MODULE_BACKUP_TOTAL_SIZE
                    ):
                        raise ValueError("Modules backup is too large")

                    archive.writestr(name, data)
                    seen_names.add(name)
                    file_count += 1
                    local_modules += 1

            archive.writestr("db_mods.json", metadata)

        payload = result.getvalue()
        if len(payload) > MAX_MODULE_BACKUP_TOTAL_SIZE:
            raise ValueError("Modules backup archive is too large")

        return payload, local_modules

    @staticmethod
    def _safe_module_backup_infos(
        zf: zipfile.ZipFile,
    ) -> typing.List[zipfile.ZipInfo]:
        infos = zf.infolist()
        if len(infos) > MAX_MODULE_BACKUP_FILES:
            raise ValueError("Too many files in modules backup")

        total_size = 0
        safe_infos = []
        seen_names = set()
        for info in infos:
            path = Path(info.filename)
            if info.filename in seen_names:
                raise ValueError(f"Duplicate module backup member: {info.filename}")
            seen_names.add(info.filename)

            if info.is_dir():
                continue

            if (
                path.name != info.filename
                or "/" in info.filename
                or "\\" in info.filename
            ):
                raise ValueError(f"Unsafe module backup member: {info.filename}")

            total_size += info.file_size
            if (
                info.file_size > MAX_MODULE_BACKUP_FILE_SIZE
                or total_size > MAX_MODULE_BACKUP_TOTAL_SIZE
            ):
                raise ValueError("Modules backup is too large")

            if path.name == "db_mods.json":
                continue

            if path.suffix != ".py":
                raise ValueError(f"Unsafe module backup member: {info.filename}")

            safe_infos.append(info)

        if "db_mods.json" not in seen_names:
            raise ValueError("Modules backup metadata is missing")

        return safe_infos

    @staticmethod
    def _valid_module_map(value: typing.Any) -> bool:
        return isinstance(value, dict) and all(
            isinstance(key, str)
            and isinstance(module_url, str)
            and utils.check_url(module_url)
            for key, module_url in value.items()
        )

    @classmethod
    def _decode_module_archive(cls, payload: bytes) -> tuple[dict, dict[str, bytes]]:
        if len(payload) > MAX_MODULE_BACKUP_TOTAL_SIZE:
            raise ValueError("Modules backup archive is too large")

        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            module_infos = cls._safe_module_backup_infos(zf)
            with zf.open("db_mods.json", "r") as modules:
                db_mods = json.loads(modules.read().decode())

            if not cls._valid_module_map(db_mods):
                raise ValueError("Invalid modules backup metadata")

            module_files = {
                Path(info.filename).name: zf.read(info) for info in module_infos
            }

        return db_mods, module_files

    @staticmethod
    def _write_module_files(module_files: dict[str, bytes]) -> None:
        loader.LOADED_MODULES_PATH.mkdir(parents=True, exist_ok=True)
        for name, data in module_files.items():
            (loader.LOADED_MODULES_PATH / name).write_bytes(data)

    @loader.command()
    async def restoremods(self, message: Message):
        if not (reply := await message.get_reply_message()) or not reply.media:
            await utils.answer(message, self.strings("reply_to_file"))
            return

        file_size = getattr(getattr(reply, "file", None), "size", None)
        if file_size is not None and file_size > MAX_MODULE_BACKUP_TOTAL_SIZE:
            await utils.answer(message, self.strings("invalid_backup"))
            return

        file = await reply.download_media(bytes)
        decoded_text = None
        try:
            decoded_text = json.loads(file.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass

        if decoded_text is None:
            try:
                db_mods, module_files = await asyncio.to_thread(
                    self._decode_module_archive,
                    file,
                )
                await asyncio.to_thread(self._write_module_files, module_files)
            except Exception:
                logger.exception("Unable to restore modules")
                await utils.answer(message, self.strings("invalid_backup"))
                return
            self.lookup("Loader").set("loaded_modules", db_mods)
        else:
            if not self._valid_module_map(decoded_text):
                await utils.answer(message, self.strings("invalid_backup"))
                return

            self.lookup("Loader").set("loaded_modules", decoded_text)

        await utils.answer(message, self.strings("mods_restored"))
        await self.invoke("restart", "-f", peer=message.peer_id)
