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
import time
import typing
import zipfile
from pathlib import Path

import redis

from hikkatl.tl.types import Message

from .. import loader, main, utils
from ..inline.types import BotInlineCall

logger = logging.getLogger(__name__)

MAX_MODULE_BACKUP_FILES = 200
MAX_MODULE_BACKUP_TOTAL_SIZE = 25 * 1024 * 1024
MAX_MODULE_BACKUP_FILE_SIZE = 2 * 1024 * 1024


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
        return redis.Redis.from_url(
            self._normalize_redis_uri(self.config["redis_uri"]),
            password=password,
            decode_responses=False,
        )

    def _redis_save_sync(self) -> int:
        payload = json.dumps(self._db, ensure_ascii=True)
        client = self._redis()
        client.set(self._redis_key(), payload)
        return len(payload.encode())

    def _redis_load_sync(self) -> typing.Optional[dict]:
        client = self._redis()
        payload = client.get(self._redis_key()) or client.get(self._redis_legacy_key())
        if not payload:
            return None

        if isinstance(payload, bytes):
            payload = payload.decode()

        return json.loads(payload)

    def _redis_clear_sync(self) -> None:
        self._redis().delete(self._redis_key())

    def _redis_check_sync(self) -> int:
        client = self._redis()
        client.ping()
        payload = client.get(self._redis_key()) or client.get(self._redis_legacy_key())
        return len(payload or b"")

    async def _save_to_redis(self) -> int:
        return await utils.run_sync(self._redis_save_sync)

    async def _load_from_redis(self) -> typing.Optional[dict]:
        return await utils.run_sync(self._redis_load_sync)

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

    @loader.loop(interval=1, autostart=True)
    async def handler(self):
        try:
            if self.get("period") == "disabled":
                raise loader.StopLoop

            if not self.get("period"):
                await asyncio.sleep(3)
                return

            if not self.get("last_backup"):
                self.set("last_backup", round(time.time()))
                await asyncio.sleep(self.get("period"))
                return

            await asyncio.sleep(
                max(0, self.get("last_backup") + self.get("period") - time.time())
            )

            await self._save_to_redis()
            self.set("last_backup", round(time.time()))
        except loader.StopLoop:
            raise
        except Exception:
            logger.exception("HikkaBackup failed")
            await asyncio.sleep(60)

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
            raise RuntimeError("Attempted to restore broken database")

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

        file = await reply.download_media(bytes)
        try:
            decoded_text = json.loads(file.decode())
        except Exception:
            logger.exception("Unable to decode database backup")
            await utils.answer(message, self.strings("invalid_backup"))
            return

        with contextlib.suppress(KeyError):
            decoded_text["hikka.inline"].pop("bot_token")

        if not self._db.process_db_autofix(decoded_text):
            raise RuntimeError("Attempted to restore broken database")

        self._db.clear()
        self._db.update(**decoded_text)
        self._db.save()

        await utils.answer(message, self.strings("db_restored"))
        await self.invoke("restart", "-f", peer=message.peer_id)

    @loader.command()
    async def backupmods(self, message: Message):
        mods_quantity = len(self.lookup("Loader").get("loaded_modules", {}))

        result = io.BytesIO()
        result.name = "mods.zip"

        db_mods = json.dumps(self.lookup("Loader").get("loaded_modules", {})).encode()

        with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as zipf:
            for root, _, files in os.walk(loader.LOADED_MODULES_DIR):
                for file in files:
                    if file.endswith(f"{self.tg_id}.py"):
                        with open(os.path.join(root, file), "rb") as f:
                            zipf.writestr(file, f.read())
                            mods_quantity += 1

            zipf.writestr("db_mods.json", db_mods)

        archive = io.BytesIO(result.getvalue())
        archive.name = f"mods-{datetime.datetime.now():%d-%m-%Y-%H-%M}.zip"

        await utils.answer_file(
            message,
            archive,
            caption=self.strings("modules_backup").format(
                mods_quantity,
                utils.escape_html(self.get_prefix()),
            ),
        )

    @staticmethod
    def _safe_module_backup_infos(
        zf: zipfile.ZipFile,
    ) -> typing.List[zipfile.ZipInfo]:
        infos = zf.infolist()
        if len(infos) > MAX_MODULE_BACKUP_FILES:
            raise ValueError("Too many files in modules backup")

        total_size = 0
        safe_infos = []
        for info in infos:
            path = Path(info.filename)
            if info.is_dir() or path.name == "db_mods.json":
                continue

            if path.name != info.filename or path.suffix != ".py":
                raise ValueError(f"Unsafe module backup member: {info.filename}")

            total_size += info.file_size
            if (
                info.file_size > MAX_MODULE_BACKUP_FILE_SIZE
                or total_size > MAX_MODULE_BACKUP_TOTAL_SIZE
            ):
                raise ValueError("Modules backup is too large")

            safe_infos.append(info)

        return safe_infos

    @loader.command()
    async def restoremods(self, message: Message):
        if not (reply := await message.get_reply_message()) or not reply.media:
            await utils.answer(message, self.strings("reply_to_file"))
            return

        file = await reply.download_media(bytes)
        try:
            decoded_text = json.loads(file.decode())
        except Exception:
            try:
                file = io.BytesIO(file)
                file.name = "mods.zip"

                with zipfile.ZipFile(file) as zf:
                    module_infos = self._safe_module_backup_infos(zf)
                    with zf.open("db_mods.json", "r") as modules:
                        db_mods = json.loads(modules.read().decode())
                        if isinstance(db_mods, dict) and all(
                            (
                                isinstance(key, str)
                                and isinstance(value, str)
                                and utils.check_url(value)
                            )
                            for key, value in db_mods.items()
                        ):
                            self.lookup("Loader").set("loaded_modules", db_mods)

                    for info in module_infos:
                        path = loader.LOADED_MODULES_PATH / Path(info.filename).name
                        with zf.open(info, "r") as module:
                            path.write_bytes(module.read())
            except Exception:
                logger.exception("Unable to restore modules")
                await utils.answer(message, self.strings("invalid_backup"))
                return
        else:
            if not isinstance(decoded_text, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in decoded_text.items()
            ):
                raise RuntimeError("Invalid backup")

            self.lookup("Loader").set("loaded_modules", decoded_text)

        await utils.answer(message, self.strings("mods_restored"))
        await self.invoke("restart", "-f", peer=message.peer_id)
