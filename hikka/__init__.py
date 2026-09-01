"""Just a placeholder to do relative imports"""
# ©️ Dan Gazizullin, 2021-2023
# This file is a part of Hikka Userbot
# 🌐 https://github.com/Splaueef/hikka
# You can redistribute it and/or modify it under the terms of the GNU AGPLv3
# 🔑 https://www.gnu.org/licenses/agpl-3.0.html

# Do not delete this file, it will cause errors.

__author__ = "Dan Gazizullin"
__contact__ = "rotkranz@pm.me"
__copyright__ = "Copyright 2022, Dan Gazizullin"
__credits__ = ["LonamiWebs", "penn5"]
__license__ = "AGPLv3"
__maintainer__ = "developer"
__status__ = "Production"

# Hikka historically used a Telethon fork with its own custom-emoji HTML tags.
# Install the small compatibility layer as soon as the package is imported so
# core modules and third-party modules share the same parser.
try:
    from .compat.telethon_html import install as _install_telethon_html_compat
except ModuleNotFoundError as error:
    # Dependencies may not be installed yet. ``hikka.__main__`` will install
    # them and restart the process before importing the rest of the core.
    if not error.name.startswith("telethon"):
        raise
else:
    _install_telethon_html_compat()
