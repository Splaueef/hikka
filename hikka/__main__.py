"""Entry point. Checks for user and starts main script"""

# ©️ Dan Gazizullin, 2021-2023
# This file is a part of Hikka Userbot
# 🌐 https://github.com/Splaueef/hikka
# You can redistribute it and/or modify it under the terms of the GNU AGPLv3
# 🔑 https://www.gnu.org/licenses/agpl-3.0.html

import getpass
import importlib.util
import os
import subprocess
import sys

REQUIREMENTS_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "requirements.txt")
)

if (
    getpass.getuser() == "root"
    and "--root" not in " ".join(sys.argv)
    and all(trigger not in os.environ for trigger in {"DOCKER", "GOORM"})
):
    print("🚫" * 15)
    print("You attempted to run Hikka on behalf of root user")
    print("Please, create a new user and restart script")
    print("If this action was intentional, pass --root argument instead")
    print("🚫" * 15)
    print()
    print("Type force_insecure to ignore this warning")
    if input("> ").lower() != "force_insecure":
        sys.exit(1)


def deps():
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "uninstall",
            "-y",
            "telethon",
            "telethon-mod",
            "hikka-tl",
            "hikka-tl-new",
            "pyrogram",
            "pyrofork",
            "hikka-pyro",
            "hikka-pyro-new",
            "tgcrypto",
            "tgcrypto-pyrofork",
        ],
        check=False,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--upgrade",
            "-q",
            "--disable-pip-version-check",
            "--no-warn-script-location",
            "-r",
            REQUIREMENTS_PATH,
        ],
        check=True,
    )


def restart():
    from ._internal import restart as _restart

    _restart()


if sys.version_info < (3, 11, 0):
    print("🚫 Error: you must use at least Python version 3.11.0")
elif __package__ != "hikka":  # In case they did python __main__.py
    print("🚫 Error: you cannot run this as a script; you must execute as a package")
else:
    try:
        import telethon
    except Exception:
        pass
    else:
        try:
            import telethon  # noqa: F811

            if tuple(map(int, telethon.__version__.split("."))) < (1, 44, 0):
                raise ImportError

            if importlib.util.find_spec("hikkatl") is not None:
                # Remove the retired fork after an in-place update. Its package
                # name differs from Telethon, so pip would otherwise keep both.
                raise ImportError

            import pyrogram

            if tuple(map(int, pyrogram.__version__.split("."))) < (2, 3, 69):
                raise ImportError

            if pyrogram.raw.all.layer < 220:
                raise ImportError

            if importlib.util.find_spec("hikkapyro") is not None:
                raise ImportError
        except ImportError:
            print("🔄 Installing dependencies...")
            deps()
            restart()

    try:
        from . import log

        log.init()

        from . import main
    except ImportError as e:
        print(f"{str(e)}\n🔄 Attempting dependencies installation... Just wait ⏱")
        deps()
        restart()

    if "HIKKA_DO_NOT_RESTART" in os.environ:
        del os.environ["HIKKA_DO_NOT_RESTART"]

    main.hikka.main()  # Execute main function
