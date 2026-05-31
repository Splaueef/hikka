# ©️ Dan Gazizullin, 2021-2023
# This file is a part of Hikka Userbot
# 🌐 https://github.com/Splaueef/hikka
# You can redistribute it and/or modify it under the terms of the GNU AGPLv3
# 🔑 https://www.gnu.org/licenses/agpl-3.0.html

import asyncio
import contextlib
import logging
import os
import pathlib
import shlex
import subprocess
import sys
import time
import typing

import git
from git import GitCommandError, Repo
from hikkatl.extensions.html import CUSTOM_EMOJIS
from hikkatl.tl.functions.messages import (
    GetDialogFiltersRequest,
    UpdateDialogFilterRequest,
)
from hikkatl.tl.types import DialogFilter, Message

from .. import loader, main, utils, version
from .._internal import restart
from ..inline.types import InlineCall

logger = logging.getLogger(__name__)


@loader.tds
class UpdaterMod(loader.Module):
    """Updates itself"""

    strings = {
        "name": "Updater",
        "external_add_usage": (
            "🚫 <b>Usage:</b> <code>.updatesvcadd &lt;name&gt; &lt;repo_url&gt; "
            "&lt;path&gt; [branch] | [update command]</code>"
        ),
        "external_added": "✅ <b>External updater</b> <code>{}</code> <b>saved.</b>",
        "external_removed": "🗑 <b>External updater</b> <code>{}</code> <b>removed.</b>",
        "external_not_found": "🚫 <b>External updater</b> <code>{}</code> <b>not found.</b>",
        "external_empty": "🚸 <b>No external updaters configured.</b>",
        "external_list_item": (
            "▫️ <code>{name}</code> → <code>{path}</code> "
            '(<code>{branch}</code>)\n   <a href="{repo}">repo</a> | '
            "<code>{command}</code>"
        ),
        "external_list": "🔧 <b>External updaters:</b>\n\n{}",
        "external_checking": "🕗 <b>Checking external updater(s)...</b>",
        "external_no_updates": "✔️ <b>No external service updates found.</b>",
        "external_updated": (
            "🔄 <b>Updated</b> <code>{name}</code>\n"
            "<code>{old}</code> → <code>{new}</code>\n"
            "<b>Exit code:</b> <code>{code}</code>{output}"
        ),
        "external_cloned": (
            "📥 <b>Cloned and initialized</b> <code>{name}</code>\n"
            "<code>{new}</code>\n<b>Exit code:</b> <code>{code}</code>{output}"
        ),
        "external_failed": (
            "🚫 <b>External updater</b> <code>{name}</code> "
            "<b>failed:</b> <code>{error}</code>"
        ),
        "external_output": "\n\n<b>Output:</b>\n<code>{}</code>",
        "external_default_command": "git reset --hard origin/{branch} && git pull --quiet",
        "external_interval_doc": (
            "Seconds between automatic checks of external services. "
            "0 disables automatic checks"
        ),
        "_cmd_doc_updatesvcadd": (
            "<name> <repo_url> <path> [branch] | [update command] - Add external "
            "git repository updater"
        ),
        "_cmd_doc_updatesvcdel": "<name> - Remove external updater",
        "_cmd_doc_updatesvclist": "List configured external updaters",
        "_cmd_doc_updatesvc": "[name] - Check and update external services",
    }

    def __init__(self):
        self.config = loader.ModuleConfig(
            loader.ConfigValue(
                "GIT_ORIGIN_URL",
                "https://github.com/Splaueef/hikka",
                lambda: self.strings("origin_cfg_doc"),
                validator=loader.validators.Link(),
            ),
            loader.ConfigValue(
                "EXTERNAL_UPDATE_INTERVAL",
                0,
                lambda: self.strings("external_interval_doc"),
                validator=loader.validators.Integer(minimum=0),
            ),
        )

    def _get_external_services(self) -> typing.List[dict]:
        services = self.get("external_services", [])
        return services if isinstance(services, list) else []

    def _set_external_services(self, services: typing.List[dict]):
        self.set("external_services", services)

    def _find_external_service(self, name: str) -> typing.Optional[dict]:
        name = name.casefold()
        return next(
            (
                service
                for service in self._get_external_services()
                if service.get("name", "").casefold() == name
            ),
            None,
        )

    def _default_external_command(self, branch: str) -> str:
        return self.strings("external_default_command").format(branch=branch)

    def _format_external_output(self, stdout: str, stderr: str) -> str:
        output = "\n".join(filter(None, [stdout.strip(), stderr.strip()])).strip()
        if not output:
            return ""

        return self.strings("external_output").format(
            utils.escape_html(output[-1800:])
        )

    @loader.command()
    async def updatesvcadd(self, message: Message):
        args = utils.get_args_raw(message)
        before_command, _, command = args.partition("|")

        try:
            parts = shlex.split(before_command)
        except ValueError:
            await utils.answer(message, self.strings("external_add_usage"))
            return

        if len(parts) < 3:
            await utils.answer(message, self.strings("external_add_usage"))
            return

        name, repo_url, path, *rest = parts
        branch = rest[0] if rest else "main"
        command = command.strip() or self._default_external_command(branch)

        services = [
            service
            for service in self._get_external_services()
            if service.get("name", "").casefold() != name.casefold()
        ]
        services.append(
            {
                "name": name,
                "repo_url": repo_url,
                "path": path,
                "branch": branch,
                "command": command,
            }
        )
        self._set_external_services(services)

        await utils.answer(
            message,
            self.strings("external_added").format(utils.escape_html(name)),
        )

    @loader.command()
    async def updatesvcdel(self, message: Message):
        name = utils.get_args_raw(message).strip()
        if not name:
            await utils.answer(message, self.strings("external_add_usage"))
            return

        services = self._get_external_services()
        filtered = [
            service
            for service in services
            if service.get("name", "").casefold() != name.casefold()
        ]

        if len(filtered) == len(services):
            await utils.answer(
                message,
                self.strings("external_not_found").format(utils.escape_html(name)),
            )
            return

        self._set_external_services(filtered)
        await utils.answer(
            message,
            self.strings("external_removed").format(utils.escape_html(name)),
        )

    @loader.command()
    async def updatesvclist(self, message: Message):
        services = self._get_external_services()
        if not services:
            await utils.answer(message, self.strings("external_empty"))
            return

        await utils.answer(
            message,
            self.strings("external_list").format(
                "\n".join(
                    self.strings("external_list_item").format(
                        name=utils.escape_html(service.get("name", "n/a")),
                        path=utils.escape_html(service.get("path", "n/a")),
                        branch=utils.escape_html(service.get("branch", "main")),
                        repo=utils.escape_html(service.get("repo_url", "")),
                        command=utils.escape_html(service.get("command", "")),
                    )
                    for service in services
                )
            ),
        )

    async def _run_external_update(self, service: dict) -> dict:
        name = service.get("name", "n/a")
        repo_url = service.get("repo_url")
        raw_path = service.get("path", "")
        path = pathlib.Path(raw_path).expanduser()
        branch = service.get("branch") or "main"
        command = service.get("command") or self._default_external_command(branch)

        if not repo_url or not raw_path:
            raise ValueError("repo_url and path are required")

        cloned = False
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                Repo.clone_from,
                repo_url,
                str(path),
                branch=branch,
            )
            cloned = True

        repo = Repo(str(path))

        try:
            origin = repo.remote("origin")
        except ValueError:
            origin = repo.create_remote("origin", repo_url)

        await asyncio.to_thread(repo.git.remote, "set-url", "origin", repo_url)
        await asyncio.to_thread(origin.fetch)
        remote_ref = f"origin/{branch}"
        remote_commit = repo.commit(remote_ref).hexsha
        local_commit = repo.head.commit.hexsha

        if local_commit == remote_commit and not cloned:
            return {"name": name, "updated": False}

        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        return {
            "name": name,
            "updated": True,
            "cloned": cloned,
            "old": local_commit,
            "new": remote_commit,
            "code": process.returncode,
            "stdout": stdout.decode(errors="replace"),
            "stderr": stderr.decode(errors="replace"),
        }

    @loader.command()
    async def updatesvc(self, message: Message):
        args = utils.get_args_raw(message).strip()
        services = self._get_external_services()

        if args:
            service = self._find_external_service(args)
            if service is None:
                await utils.answer(
                    message,
                    self.strings("external_not_found").format(utils.escape_html(args)),
                )
                return

            services = [service]

        if not services:
            await utils.answer(message, self.strings("external_empty"))
            return

        message = await utils.answer(message, self.strings("external_checking"))
        results = []

        for service in services:
            try:
                result = await self._run_external_update(service)
            except Exception as e:
                logger.exception("External updater %s failed", service.get("name"))
                results.append(
                    self.strings("external_failed").format(
                        name=utils.escape_html(service.get("name", "n/a")),
                        error=utils.escape_html(str(e)),
                    )
                )
                continue

            if not result["updated"]:
                continue

            output = self._format_external_output(result["stdout"], result["stderr"])
            template = self.strings(
                "external_cloned" if result.get("cloned") else "external_updated"
            )
            results.append(
                template.format(
                    name=utils.escape_html(result["name"]),
                    old=result["old"][:8],
                    new=result["new"][:8],
                    code=result["code"],
                    output=output,
                )
            )

        await utils.answer(
            message,
            "\n\n".join(results) if results else self.strings("external_no_updates"),
        )

    @loader.command()
    async def restart(self, message: Message):
        args = utils.get_args_raw(message)
        secure_boot = any(trigger in args for trigger in {"--secure-boot", "-sb"})
        try:
            if (
                "-f" in args
                or not self.inline.init_complete
                or not await self.inline.form(
                    message=message,
                    text=self.strings(
                        "secure_boot_confirm" if secure_boot else "restart_confirm"
                    ),
                    reply_markup=[
                        {
                            "text": self.strings("btn_restart"),
                            "callback": self.inline_restart,
                            "args": (secure_boot,),
                        },
                        {"text": self.strings("cancel"), "action": "close"},
                    ],
                )
            ):
                raise
        except Exception:
            await self.restart_common(message, secure_boot)

    async def inline_restart(self, call: InlineCall, secure_boot: bool = False):
        await self.restart_common(call, secure_boot=secure_boot)

    async def process_restart_message(self, msg_obj: typing.Union[InlineCall, Message]):
        self.set(
            "selfupdatemsg",
            (
                msg_obj.inline_message_id
                if hasattr(msg_obj, "inline_message_id")
                else f"{utils.get_chat_id(msg_obj)}:{msg_obj.id}"
            ),
        )

    async def restart_common(
        self,
        msg_obj: typing.Union[InlineCall, Message],
        secure_boot: bool = False,
    ):
        if (
            hasattr(msg_obj, "form")
            and isinstance(msg_obj.form, dict)
            and "uid" in msg_obj.form
            and msg_obj.form["uid"] in self.inline._units
            and "message" in self.inline._units[msg_obj.form["uid"]]
        ):
            message = self.inline._units[msg_obj.form["uid"]]["message"]
        else:
            message = msg_obj

        if secure_boot:
            self._db.set(loader.__name__, "secure_boot", True)

        msg_obj = await utils.answer(
            msg_obj,
            self.strings("restarting_caption").format(
                utils.get_platform_emoji()
                if self._client.hikka_me.premium
                and CUSTOM_EMOJIS
                and isinstance(msg_obj, Message)
                else "Hikka"
            ),
        )

        await self.process_restart_message(msg_obj)

        self.set("restart_ts", time.time())

        await self._db.remote_force_save()

        if "LAVHOST" in os.environ:
            os.system("lavhost restart")
            return

        with contextlib.suppress(Exception):
            await main.hikka.web.stop()

        handler = logging.getLogger().handlers[0]
        handler.setLevel(logging.CRITICAL)

        for client in self.allclients:
            # Terminate main loop of all running clients
            # Won't work if not all clients are ready
            if client is not message.client:
                await client.disconnect()

        await message.client.disconnect()
        restart()

    async def download_common(self):
        try:
            repo = Repo(os.path.dirname(utils.get_base_dir()))
            origin = repo.remote("origin")
            r = origin.pull()
            new_commit = repo.head.commit
            for info in r:
                if info.old_commit:
                    for d in new_commit.diff(info.old_commit):
                        if d.b_path == "requirements.txt":
                            return True
            return False
        except git.exc.InvalidGitRepositoryError:
            repo = Repo.init(os.path.dirname(utils.get_base_dir()))
            origin = repo.create_remote("origin", self.config["GIT_ORIGIN_URL"])
            origin.fetch()
            repo.create_head("main", origin.refs.main)
            repo.heads.main.set_tracking_branch(origin.refs.main)
            repo.heads.main.checkout(True)
            return False

    @staticmethod
    def req_common():
        # Now we have downloaded new code, install requirements
        logger.debug("Installing new requirements...")
        try:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "-r",
                    os.path.join(
                        os.path.dirname(utils.get_base_dir()),
                        "requirements.txt",
                    )
                ],
                check=True,
            )
        except subprocess.CalledProcessError:
            logger.exception("Req install failed")

    @loader.command()
    async def update(self, message: Message):
        try:
            args = utils.get_args_raw(message)
            current = utils.get_git_hash()
            upcoming = next(
                git.Repo().iter_commits(f"origin/{version.branch}", max_count=1)
            ).hexsha
            if (
                "-f" in args
                or not self.inline.init_complete
                or not await self.inline.form(
                    message=message,
                    text=(
                        self.strings("update_confirm").format(
                            current, current[:8], upcoming, upcoming[:8]
                        )
                        if upcoming != current
                        else self.strings("no_update")
                    ),
                    reply_markup=[
                        {
                            "text": self.strings("btn_update"),
                            "callback": self.inline_update,
                        },
                        {"text": self.strings("cancel"), "action": "close"},
                    ],
                )
            ):
                raise
        except Exception:
            await self.inline_update(message)

    async def inline_update(
        self,
        msg_obj: typing.Union[InlineCall, Message],
        hard: bool = False,
    ):
        # We don't really care about asyncio at this point, as we are shutting down
        if hard:
            os.system(f"cd {utils.get_base_dir()} && cd .. && git reset --hard HEAD")

        try:
            if "LAVHOST" in os.environ:
                msg_obj = await utils.answer(
                    msg_obj,
                    self.strings("lavhost_update").format(
                        "</b><emoji document_id=5192756799647785066>✌️</emoji><emoji"
                        " document_id=5193117564015747203>✌️</emoji><emoji"
                        " document_id=5195050806105087456>✌️</emoji><emoji"
                        " document_id=5195457642587233944>✌️</emoji><b>"
                        if self._client.hikka_me.premium
                        and CUSTOM_EMOJIS
                        and isinstance(msg_obj, Message)
                        else "lavHost"
                    ),
                )
                await self.process_restart_message(msg_obj)
                os.system("lavhost update")
                return

            with contextlib.suppress(Exception):
                msg_obj = await utils.answer(msg_obj, self.strings("downloading"))

            req_update = await self.download_common()

            with contextlib.suppress(Exception):
                msg_obj = await utils.answer(msg_obj, self.strings("installing"))

            if req_update:
                self.req_common()

            await self.restart_common(msg_obj)
        except GitCommandError:
            if not hard:
                await self.inline_update(msg_obj, True)
                return

            logger.critical("Got update loop. Update manually via .terminal")

    @loader.loop(interval=60, autostart=True)
    async def external_update_poller(self):
        interval = self.config["EXTERNAL_UPDATE_INTERVAL"]
        if not interval or not self._get_external_services():
            return

        if time.time() - self.get("last_external_update_check", 0) < interval:
            return

        self.set("last_external_update_check", time.time())

        for service in self._get_external_services():
            try:
                result = await self._run_external_update(service)
            except Exception:
                logger.exception("External updater %s failed", service.get("name"))
                continue

            if result["updated"]:
                logger.info(
                    "External updater %s moved from %s to %s with exit code %s",
                    result["name"],
                    result["old"],
                    result["new"],
                    result["code"],
                )

    @loader.command()
    async def source(self, message: Message):
        await utils.answer(
            message,
            self.strings("source").format(self.config["GIT_ORIGIN_URL"]),
        )

    async def client_ready(self):
        if self.get("selfupdatemsg") is not None:
            try:
                await self.update_complete()
            except Exception:
                logger.exception("Failed to complete update!")

        if self.get("do_not_create", False):
            return

        try:
            await self._add_folder()
        except Exception:
            logger.exception("Failed to add folder!")

        self.set("do_not_create", True)

    async def _add_folder(self):
        folders = await self._client(GetDialogFiltersRequest())

        if any(getattr(folder, "title", None) == "hikka" for folder in folders):
            return

        try:
            folder_id = (
                max(
                    folders,
                    key=lambda x: x.id,
                ).id
                + 1
            )
        except ValueError:
            folder_id = 2

        try:
            await self._client(
                UpdateDialogFilterRequest(
                    folder_id,
                    DialogFilter(
                        folder_id,
                        title="hikka",
                        pinned_peers=(
                            [
                                await self._client.get_input_entity(
                                    self._client.loader.inline.bot_id
                                )
                            ]
                            if self._client.loader.inline.init_complete
                            else []
                        ),
                        include_peers=[
                            await self._client.get_input_entity(dialog.entity)
                            async for dialog in self._client.iter_dialogs(
                                None,
                                ignore_migrated=True,
                            )
                            if dialog.name
                            in {
                                "hikka-logs",
                                "hikka-onload",
                                "hikka-assets",
                                "hikka-backups",
                                "hikka-acc-switcher",
                                "silent-tags",
                            }
                            and dialog.is_channel
                            and (
                                dialog.entity.participants_count == 1
                                or dialog.entity.participants_count == 2
                                and dialog.name in {"hikka-logs", "silent-tags"}
                            )
                            or (
                                self._client.loader.inline.init_complete
                                and dialog.entity.id
                                == self._client.loader.inline.bot_id
                            )
                            or dialog.entity.id
                            in [
                                1554874075,
                                1697279580,
                                1679998924,
                            ]  # official hikka chats
                        ],
                        emoticon="🐱",
                        exclude_peers=[],
                        contacts=False,
                        non_contacts=False,
                        groups=False,
                        broadcasts=False,
                        bots=False,
                        exclude_muted=False,
                        exclude_read=False,
                        exclude_archived=False,
                    ),
                )
            )
        except Exception:
            logger.critical(
                "Can't create Hikka folder. Possible reasons are:\n"
                "- User reached the limit of folders in Telegram\n"
                "- User got floodwait\n"
                "Ignoring error and adding folder addition to ignore list"
            )

    async def update_complete(self):
        logger.debug("Self update successful! Edit message")
        start = self.get("restart_ts")
        try:
            took = round(time.time() - start)
        except Exception:
            took = "n/a"

        msg = self.strings("success").format(utils.ascii_face(), took)
        ms = self.get("selfupdatemsg")

        if ":" in str(ms):
            chat_id, message_id = ms.split(":")
            chat_id, message_id = int(chat_id), int(message_id)
            await self._client.edit_message(chat_id, message_id, msg)
            return

        await self.inline.bot.edit_message_text(
            inline_message_id=ms,
            text=self.inline.sanitise_text(msg),
        )

    async def full_restart_complete(self, secure_boot: bool = False):
        start = self.get("restart_ts")

        try:
            took = round(time.time() - start)
        except Exception:
            took = "n/a"

        self.set("restart_ts", None)

        ms = self.get("selfupdatemsg")
        msg = self.strings(
            "secure_boot_complete" if secure_boot else "full_success"
        ).format(utils.ascii_face(), took)

        if ms is None:
            return

        self.set("selfupdatemsg", None)

        if ":" in str(ms):
            chat_id, message_id = ms.split(":")
            chat_id, message_id = int(chat_id), int(message_id)
            await self._client.edit_message(chat_id, message_id, msg)
            await asyncio.sleep(60)
            await self._client.delete_messages(chat_id, message_id)
            return

        await self.inline.bot.edit_message_text(
            inline_message_id=ms,
            text=self.inline.sanitise_text(msg),
        )
