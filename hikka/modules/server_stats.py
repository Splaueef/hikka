# ©️ Dan Gazizullin, 2021-2023
# This file is a part of Hikka Userbot
# 🌐 https://github.com/Splaueef/hikka
# You can redistribute it and/or modify it under the terms of the GNU AGPLv3
# 🔑 https://www.gnu.org/licenses/agpl-3.0.html

import contextlib
import logging
import statistics
import time
from collections import deque
from datetime import datetime

import psutil
from hikkatl.tl.types import Message

from .. import loader, utils

logger = logging.getLogger(__name__)

PERIODS = {
    "хв": 60,
    "хвилина": 60,
    "minute": 60,
    "min": 60,
    "год": 60 * 60,
    "година": 60 * 60,
    "hour": 60 * 60,
    "day": 24 * 60 * 60,
    "день": 24 * 60 * 60,
    "month": 31 * 24 * 60 * 60,
    "місяць": 31 * 24 * 60 * 60,
}


@loader.tds
class ServerStats(loader.Module):
    """Стежить за статистикою сервера, на якому працює бот."""

    strings = {
        "name": "ServerStats",
        "no_data": (
            "<b>📊 Даних статистики ще немає. Зачекайте до наступного заміру.</b>"
        ),
        "usage": (
            "<b>Вкажіть період:</b> <code>хв</code>, <code>година</code>, "
            "<code>день</code> або <code>місяць</code>"
        ),
        "stats": (
            "<b>📊 Статистика сервера за {}</b>\n"
            "<code>Заміри:</code> {}\n"
            "<code>Період:</code> {}\n\n"
            "<b>RAM:</b> {} / {} ({})\n"
            "<b>MEM:</b> {} / {} ({})\n"
            "<b>CPU:</b> avg {} | max {}\n"
            "<b>Ядра:</b>\n{}"
        ),
        "alert": (
            "<b>⚠️ Ядро CPU завантажене на 100%</b>\n"
            "<code>Час:</code> {}\n"
            "<code>Ядро:</code> #{}\n"
            "<code>Навантаження:</code> {}\n"
            "<code>Процес:</code> {}"
        ),
    }

    def __init__(self):
        self._samples = deque()
        self._last_alert = {}
        self.config = loader.ModuleConfig(
            loader.ConfigValue(
                "collect_stats",
                True,
                "Collect server statistics in the background",
                validator=loader.validators.Boolean(),
            ),
            loader.ConfigValue(
                "alert_on_full_core",
                True,
                "Notify owner when any CPU core reaches 100% load",
                validator=loader.validators.Boolean(),
            ),
            loader.ConfigValue(
                "alert_cooldown",
                300,
                "Minimal seconds between alerts for the same CPU core",
                validator=loader.validators.Integer(minimum=30),
            ),
        )

    async def client_ready(self):
        psutil.cpu_percent(interval=None, percpu=True)
        for process in psutil.process_iter():
            with contextlib.suppress(psutil.Error):
                process.cpu_percent(interval=None)

    def _sample(self):
        vm = psutil.virtual_memory()
        swap = psutil.swap_memory()
        cpu_total = psutil.cpu_percent(interval=None)
        cpu_cores = psutil.cpu_percent(interval=None, percpu=True)
        top_process = self._top_process()
        return {
            "ts": time.time(),
            "ram_used": vm.used,
            "ram_total": vm.total,
            "ram_percent": vm.percent,
            "mem_used": swap.used,
            "mem_total": swap.total,
            "mem_percent": swap.percent,
            "cpu_total": cpu_total,
            "cpu_cores": cpu_cores,
            "top_process": top_process,
        }

    @staticmethod
    def _size(value: float) -> str:
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if value < 1024 or unit == "TB":
                return f"{value:.1f} {unit}"
            value /= 1024

    @staticmethod
    def _percent(value: float) -> str:
        return f"{value:.1f}%"

    def _top_process(self) -> str:
        top = None
        for process in psutil.process_iter(["pid", "name", "cmdline"]):
            with contextlib.suppress(psutil.Error):
                cpu = process.cpu_percent(interval=None)
                if top is None or cpu > top["cpu"]:
                    top = {
                        "pid": process.info["pid"],
                        "name": process.info["name"] or "unknown",
                        "cmdline": " ".join(process.info.get("cmdline") or []),
                        "cpu": cpu,
                    }

        if not top:
            return "unknown"

        command = top["cmdline"] or top["name"]
        if len(command) > 80:
            command = command[:77] + "..."

        return utils.escape_html(
            f'{top["name"]} (pid {top["pid"]}, cpu {top["cpu"]:.1f}%) — {command}'
        )

    async def _alert_full_cores(self, sample):
        if not self.config["alert_on_full_core"]:
            return

        now = time.time()
        for core, load in enumerate(sample["cpu_cores"]):
            if (
                load < 100
                or now - self._last_alert.get(core, 0) < self.config["alert_cooldown"]
            ):
                continue

            self._last_alert[core] = now
            await self.inline.bot.send_message(
                self.tg_id,
                self.strings("alert").format(
                    datetime.fromtimestamp(sample["ts"]).strftime("%Y-%m-%d %H:%M:%S"),
                    core,
                    self._percent(load),
                    sample["top_process"],
                ),
            )

    @loader.loop(interval=5, autostart=True)
    async def stats_collector(self):
        if not self.config["collect_stats"]:
            return

        try:
            sample = self._sample()
            self._samples.append(sample)
            oldest = time.time() - PERIODS["month"]
            while self._samples and self._samples[0]["ts"] < oldest:
                self._samples.popleft()
            await self._alert_full_cores(sample)
        except Exception:
            logger.exception("Failed to collect server statistics")

    def _render_stats(self, period_name: str, seconds: int) -> str:
        since = time.time() - seconds
        samples = [sample for sample in self._samples if sample["ts"] >= since]
        if not samples:
            return self.strings("no_data")

        cpu_total = [sample["cpu_total"] for sample in samples]
        cores = max(len(sample["cpu_cores"]) for sample in samples)
        core_lines = []
        for core in range(cores):
            values = [
                sample["cpu_cores"][core]
                for sample in samples
                if core < len(sample["cpu_cores"])
            ]
            core_lines.append(
                f"<code>#{core}</code>: avg {self._percent(statistics.fmean(values))} | "
                f"max {self._percent(max(values))}"
            )

        latest = samples[-1]
        return self.strings("stats").format(
            period_name,
            len(samples),
            f'{datetime.fromtimestamp(samples[0]["ts"]).strftime("%Y-%m-%d %H:%M:%S")} — '
            f'{datetime.fromtimestamp(samples[-1]["ts"]).strftime("%Y-%m-%d %H:%M:%S")}',
            self._size(latest["ram_used"]),
            self._size(latest["ram_total"]),
            self._percent(
                statistics.fmean(sample["ram_percent"] for sample in samples)
            ),
            self._size(latest["mem_used"]),
            self._size(latest["mem_total"]),
            self._percent(
                statistics.fmean(sample["mem_percent"] for sample in samples)
            ),
            self._percent(statistics.fmean(cpu_total)),
            self._percent(max(cpu_total)),
            "\n".join(core_lines),
        )

    @loader.command()
    async def serverstats(self, message: Message):
        """[хв|година|день|місяць] - показати статистику сервера"""
        period = (utils.get_args_raw(message) or "хв").strip().lower()
        if period not in PERIODS:
            await utils.answer(message, self.strings("usage"))
            return

        await utils.answer(message, self._render_stats(period, PERIODS[period]))
