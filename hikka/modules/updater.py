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
import urllib.parse

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
            "🚫 <b>Usage:</b> <code>.updatesvcadd &lt;name&gt; &lt;path&gt; "
            "[script]</code> <b>or</b> <code>.updatesvcadd &lt;name&gt; "
            "&lt;repo_url&gt; &lt;path&gt; [branch] | [update command]</code>"
        ),
        "external_added": "✅ <b>External updater</b> <code>{}</code> <b>saved.</b>",
        "external_removed": "🗑 <b>External updater</b> <code>{}</code> <b>removed.</b>",
        "external_not_found": (
            "🚫 <b>External updater</b> <code>{}</code> <b>not found.</b>"
        ),
        "external_empty": "🚸 <b>No external updaters configured.</b>",
        "external_list_item": (
            "▫️ <code>{name}</code> → <code>{path}</code> "
            "(<code>{branch}</code>)\n   {repo} | <code>{command}</code>"
        ),
        "external_list": "🔧 <b>External updaters:</b>\n\n{}",
        "external_checking": "🕗 <b>Checking external updater(s)...</b>",
        "external_no_updates": "✔️ <b>No external service updates found.</b>",
        "external_info_checking": "🕗 <b>Collecting external updater info...</b>",
        "external_info": "ℹ️ <b>External updater info:</b>\n\n{}",
        "external_info_item": (
            "▫️ <b>{name}</b>\n"
            "   <b>Path:</b> <code>{path}</code>\n"
            "   <b>Branch:</b> <code>{branch}</code> → <code>{tracking}</code>\n"
            "   <b>Local:</b> <code>{local}</code>\n"
            "   <b>Remote:</b> <code>{remote}</code>\n"
            "   <b>Compare:</b> {compare}\n"
            "   <b>Status:</b> {status}\n"
            "   <b>Command:</b> <code>{command}</code>"
        ),
        "external_info_item_missing": (
            "▫️ <b>{name}</b>\n"
            "   <b>Path:</b> <code>{path}</code>\n"
            "   <b>Status:</b> not cloned yet\n"
            "   <b>Repository:</b> {repo}\n"
            "   <b>Branch:</b> <code>{branch}</code>\n"
            "   <b>Command:</b> <code>{command}</code>"
        ),
        "external_info_status_behind": "behind by <code>{}</code> commit(s)",
        "external_info_status_ahead": "ahead by <code>{}</code> commit(s)",
        "external_info_status_diverged": (
            "diverged: ahead by <code>{}</code>, behind by <code>{}</code> commit(s)"
        ),
        "external_info_status_current": "up-to-date",
        "external_info_status_dirty": "local changes present",
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
        "external_default_command": (
            "git reset --hard origin/{branch} && git pull --quiet"
        ),
        "external_interval_doc": (
            "Seconds between automatic checks of external services. "
            "0 disables automatic checks"
        ),
        "_cmd_doc_updatesvcadd": (
            "<name> <path> [script] or <name> <repo_url> <path> [branch] | "
            "[update command] - Add external git repository updater"
        ),
        "_cmd_doc_updatesvcdel": "<name> - Remove external updater",
        "_cmd_doc_updatesvclist": "List configured external updaters",
        "_cmd_doc_updatesvcinfo": "[name] - Show current external updater status",
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

    def _looks_like_git_url(self, value: str) -> bool:
        if pathlib.Path(value).expanduser().exists():
            return False

        return (
            "://" in value
            or value.startswith(("git@", "ssh://"))
            or value.endswith(".git")
        )

    def _get_tracking_branch(self, repo: Repo, branch: str = None) -> str:
        if branch:
            return f"origin/{branch}"

        try:
            tracking_branch = repo.active_branch.tracking_branch()
        except TypeError:
            tracking_branch = None

        if tracking_branch is not None:
            return tracking_branch.name

        try:
            branch = repo.active_branch.name
        except TypeError as e:
            raise ValueError("Unable to detect active branch") from e

        return f"origin/{branch}"

    def _get_service_repo(self, path: pathlib.Path) -> Repo:
        return Repo(str(path), search_parent_directories=True)

    def _get_origin_url(self, repo: Repo) -> typing.Optional[str]:
        try:
            return next(repo.remote("origin").urls)
        except (StopIteration, ValueError):
            return None

    def _normalize_compare_base_url(self, repo_url: str) -> typing.Optional[str]:
        if not repo_url:
            return None

        repo_url = repo_url.strip()
        if repo_url.startswith("git@") and ":" in repo_url:
            host, path = repo_url[4:].split(":", 1)
            repo_url = f"https://{host}/{path}"
        elif repo_url.startswith("ssh://git@"):
            parsed = urllib.parse.urlparse(repo_url)
            repo_url = f"https://{parsed.hostname}/{parsed.path.lstrip('/')}"

        if repo_url.endswith(".git"):
            repo_url = repo_url[:-4]

        parsed = urllib.parse.urlparse(repo_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None

        return urllib.parse.urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path, "", "", "")
        )

    def _get_compare_url(
        self,
        repo_url: typing.Optional[str],
        old: str,
        new: str,
    ) -> typing.Optional[str]:
        base_url = self._normalize_compare_base_url(repo_url or "")
        if not base_url or not old or not new:
            return None

        return f"{base_url}/compare/{old[:12]}...{new[:12]}"

    def _format_external_repo_link(self, repo_url: typing.Optional[str]) -> str:
        base_url = self._normalize_compare_base_url(repo_url or "")
        if base_url:
            return f'<a href="{utils.escape_html(base_url)}">repo</a>'

        return utils.escape_html(repo_url or "n/a")

    def _format_external_status(self, status: dict) -> str:
        ahead = status.get("ahead", 0)
        behind = status.get("behind", 0)
        parts = []

        if ahead and behind:
            parts.append(
                self.strings("external_info_status_diverged").format(ahead, behind)
            )
        elif behind:
            parts.append(self.strings("external_info_status_behind").format(behind))
        elif ahead:
            parts.append(self.strings("external_info_status_ahead").format(ahead))
        else:
            parts.append(self.strings("external_info_status_current"))

        if status.get("dirty"):
            parts.append(self.strings("external_info_status_dirty"))

        return ", ".join(parts)

    async def _get_external_status(self, service: dict, fetch: bool = True) -> dict:
        name = service.get("name", "n/a")
        repo_url = service.get("repo_url")
        raw_path = service.get("path", "")
        path = pathlib.Path(raw_path).expanduser()
        branch = service.get("branch")
        command = service.get("command") or self._default_external_command(
            branch or "main"
        )

        if not raw_path:
            raise ValueError("path is required")

        if not path.exists():
            return {
                "name": name,
                "path": raw_path,
                "repo_url": repo_url,
                "branch": branch or "main",
                "command": command,
                "exists": False,
            }

        repo = self._get_service_repo(path)

        try:
            origin = repo.remote("origin")
        except ValueError as e:
            if not repo_url:
                raise ValueError("origin remote is not configured") from e

            origin = repo.create_remote("origin", repo_url)

        if repo_url:
            await asyncio.to_thread(repo.git.remote, "set-url", "origin", repo_url)

        if fetch:
            await asyncio.to_thread(origin.fetch)

        origin_url = repo_url or self._get_origin_url(repo)
        tracking_branch = self._get_tracking_branch(repo, branch)
        branch = tracking_branch.rsplit("/", 1)[-1]
        remote_commit = repo.commit(tracking_branch).hexsha
        local_commit = repo.head.commit.hexsha

        ahead = behind = 0
        with contextlib.suppress(Exception):
            ahead, behind = map(
                int,
                repo.git.rev_list(
                    "--left-right",
                    "--count",
                    f"{local_commit}...{tracking_branch}",
                ).split(),
            )

        return {
            "name": name,
            "path": raw_path,
            "repo_url": origin_url,
            "branch": branch,
            "tracking_branch": tracking_branch,
            "command": command,
            "exists": True,
            "local": local_commit,
            "remote": remote_commit,
            "ahead": ahead,
            "behind": behind,
            "dirty": repo.is_dirty(untracked_files=True),
            "compare_url": self._get_compare_url(
                origin_url, local_commit, remote_commit
            ),
        }

    def _format_external_output(self, stdout: str, stderr: str) -> str:
        output = "\n".join(filter(None, [stdout.strip(), stderr.strip()])).strip()
        if not output:
            return ""

        return self.strings("external_output").format(utils.escape_html(output[-1800:]))

    @loader.command()
    async def updatesvcadd(self, message: Message):
        args = utils.get_args_raw(message)
        before_command, _, command = args.partition("|")

        try:
            parts = shlex.split(before_command)
        except ValueError:
            await utils.answer(message, self.strings("external_add_usage"))
            return

        if len(parts) < 2:
            await utils.answer(message, self.strings("external_add_usage"))
            return

        name, first_arg, *rest = parts

        if self._looks_like_git_url(first_arg):
            if not rest:
                await utils.answer(message, self.strings("external_add_usage"))
                return

            repo_url = first_arg
            path, *rest = rest
            branch = rest[0] if rest else "main"
            command = command.strip() or self._default_external_command(branch)
        else:
            repo_url = None
            path = first_arg
            branch = None
            command = command.strip() or " ".join(rest).strip()

            if not command:
                await utils.answer(message, self.strings("external_add_usage"))
                return

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
                        branch=utils.escape_html(service.get("branch") or "auto"),
                        repo=(
                            self._format_external_repo_link(service.get("repo_url"))
                            if service.get("repo_url")
                            else "<code>auto repo</code>"
                        ),
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
        branch = service.get("branch")

        if not raw_path:
            raise ValueError("path is required")

        cloned = False
        if not path.exists():
            if not repo_url:
                raise ValueError("path does not exist and repo_url is not configured")

            path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                Repo.clone_from,
                repo_url,
                str(path),
                branch=branch or "main",
            )
            cloned = True

        status = await self._get_external_status(service)
        command = status["command"]
        local_commit = status["local"]
        remote_commit = status["remote"]

        if local_commit == remote_commit and not cloned:
            return {
                "name": name,
                "updated": False,
                "compare_url": status.get("compare_url"),
            }

        repo = self._get_service_repo(path)
        process = await asyncio.create_subprocess_shell(
            command,
            cwd=str(repo.working_tree_dir or path),
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
            "compare_url": status.get("compare_url"),
        }

    @loader.command()
    async def updatesvcinfo(self, message: Message):
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

        message = await utils.answer(message, self.strings("external_info_checking"))
        results = []

        for service in services:
            try:
                status = await self._get_external_status(service)
            except Exception as e:
                logger.exception(
                    "External updater %s status check failed",
                    service.get("name"),
                )
                results.append(
                    self.strings("external_failed").format(
                        name=utils.escape_html(service.get("name", "n/a")),
                        error=utils.escape_html(str(e)),
                    )
                )
                continue

            if not status["exists"]:
                results.append(
                    self.strings("external_info_item_missing").format(
                        name=utils.escape_html(status["name"]),
                        path=utils.escape_html(status["path"]),
                        repo=self._format_external_repo_link(status.get("repo_url")),
                        branch=utils.escape_html(status["branch"]),
                        command=utils.escape_html(status["command"]),
                    )
                )
                continue

            compare_url = status.get("compare_url")
            compare = (
                f'<a href="{utils.escape_html(compare_url)}">compare</a>'
                if compare_url
                else "<code>n/a</code>"
            )

            results.append(
                self.strings("external_info_item").format(
                    name=utils.escape_html(status["name"]),
                    path=utils.escape_html(status["path"]),
                    branch=utils.escape_html(status["branch"]),
                    tracking=utils.escape_html(status["tracking_branch"]),
                    local=status["local"][:12],
                    remote=status["remote"][:12],
                    compare=compare,
                    status=self._format_external_status(status),
                    command=utils.escape_html(status["command"]),
                )
            )

        await utils.answer(
            message,
            self.strings("external_info").format("\n\n".join(results)),
        )

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
                    ),
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
