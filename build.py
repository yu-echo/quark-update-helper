# -*- coding: utf-8 -*-
"""打包入口：先修补 CPython 3.10.0 的 dis 已知 bug，再调用 PyInstaller"""
import dis

_orig = dis._get_const_info


def _safe_get_const_info(arg, const_list):
    try:
        return _orig(arg, const_list)
    except (IndexError, TypeError):
        return None, None


dis._get_const_info = _safe_get_const_info

from PyInstaller.__main__ import run  # noqa: E402

run([
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", "QuarkWatcher",
    "--icon", "app.ico",
    "--add-data", "app.ico;.",
    "--collect-all", "customtkinter",
    "--collect-submodules", "qrcode",
    "--hidden-import", "qrcode.image.pil",
    "--hidden-import", "PIL.Image",
    "gui.py",
])
