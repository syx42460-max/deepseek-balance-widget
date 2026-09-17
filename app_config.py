"""配置读写、API key 加密存储（Windows DPAPI）与开机自启。"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Any

APP_NAME = "DeepSeekBalanceWidget"
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_PATH = CONFIG_DIR / "widget.log"
PRICING_CACHE_PATH = CONFIG_DIR / "pricing.json"
LEDGER_PATH = CONFIG_DIR / "ledger.json"


def log_exception(context: str, error: BaseException) -> None:
    """把异常写进 %APPDATA%\\DeepSeekBalanceWidget\\widget.log，方便排查。"""
    try:
        import traceback

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {context}\n")
            traceback.print_exception(type(error), error, error.__traceback__, file=handle)
    except Exception:
        pass

DEFAULTS: dict[str, Any] = {
    "api_key": "",            # 明文留空，实际存 api_key_blob（DPAPI 加密后 base64）
    "api_key_blob": "",
    "refresh_minutes": 10,
    "opacity": 1.0,
    "topmost": True,
    "autostart": False,
    "low_balance_alert": 5.0,
    "position": None,         # [x, y]
    "snap_margin": 24,
    "pricing_auto_refresh": True,   # 是否按间隔自动刷新峰谷价格
    "pricing_refresh_hours": 24,    # 自动刷新间隔（小时）
    "pricing_last_refresh": "",     # 上次成功的刷新时间（ISO 字符串）
    "theme": "ocean",               # 主题配色
    "accent_color": "",             # 自定义主色（#rrggbb），空则用主题自带
    "ledger_session_minutes": 15,   # 会话合并窗口：间隔小于它算同一次对话
    "prices_collapsed": False,      # 是否折叠峰谷价格区
    "docked": False,                # 是否收起成靠边竖条
    "dock_side": "right",           # 竖条贴哪一侧：left / right
    "dock_offset": 0,               # 竖条中心的纵向位置
}

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "DeepSeekBalanceWidget"


# --------------------------------------------------------------------------- #
# Windows DPAPI：把 key 用当前用户的凭据加密，换台机器/换个用户都解不开
# --------------------------------------------------------------------------- #
class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_CRYPT32 = ctypes.WinDLL("crypt32", use_last_error=True) if sys.platform == "win32" else None
_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True) if sys.platform == "win32" else None


def _make_blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data, len(data))
    blob = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _blob_bytes(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def protect(text: str) -> str:
    """加密成 base64 字符串；DPAPI 不可用时降级为明文标记。"""
    if not text:
        return ""
    if _CRYPT32 is None:
        return "plain:" + base64.b64encode(text.encode("utf-8")).decode("ascii")
    blob_in, _keep = _make_blob(text.encode("utf-8"))
    blob_out = _DataBlob()
    ok = _CRYPT32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    )
    if not ok:
        raise OSError(ctypes.get_last_error(), "CryptProtectData 失败")
    try:
        return "dpapi:" + base64.b64encode(_blob_bytes(blob_out)).decode("ascii")
    finally:
        _KERNEL32.LocalFree(blob_out.pbData)


def unprotect(token: str) -> str:
    if not token:
        return ""
    if token.startswith("plain:"):
        return base64.b64decode(token[6:]).decode("utf-8")
    if token.startswith("dpapi:"):
        if _CRYPT32 is None:
            return ""
        data = base64.b64decode(token[6:])
        blob_in, _keep = _make_blob(data)
        blob_out = _DataBlob()
        ok = _CRYPT32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        )
        if not ok:
            raise OSError(ctypes.get_last_error(), "CryptUnprotectData 失败")
        try:
            return _blob_bytes(blob_out).decode("utf-8")
        finally:
            _KERNEL32.LocalFree(blob_out.pbData)
    return ""


# --------------------------------------------------------------------------- #
# 配置对象
# --------------------------------------------------------------------------- #
class Config:
    def __init__(self) -> None:
        self.data: dict[str, Any] = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        if not CONFIG_PATH.exists():
            return
        try:
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(stored, dict):
            for key, value in stored.items():
                self.data[key] = value

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        payload = {key: self.data.get(key, value) for key, value in DEFAULTS.items()}
        CONFIG_PATH.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def __getitem__(self, key: str) -> Any:
        return self.data.get(key, DEFAULTS.get(key))

    def __setitem__(self, key: str, value: Any) -> None:
        self.data[key] = value

    # ---- API key ---------------------------------------------------------- #
    @property
    def api_key(self) -> str:
        try:
            key = unprotect(str(self["api_key_blob"] or ""))
        except Exception:
            key = ""
        return key or str(self["api_key"] or "")

    @api_key.setter
    def api_key(self, value: str) -> None:
        value = (value or "").strip()
        try:
            self["api_key_blob"] = protect(value) if value else ""
            self["api_key"] = ""
        except Exception:
            self["api_key"] = value
            self["api_key_blob"] = ""


# --------------------------------------------------------------------------- #
# 开机自启（HKCU\...\Run）
# --------------------------------------------------------------------------- #
def autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    script = Path(__file__).resolve().parent / "main.py"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{interpreter}" "{script}"'


def is_autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as handle:
            value, _ = winreg.QueryValueEx(handle, RUN_VALUE)
            return bool(value)
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    if sys.platform != "win32":
        return
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as handle:
        if enabled:
            winreg.SetValueEx(handle, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(handle, RUN_VALUE)
            except FileNotFoundError:
                pass


# --------------------------------------------------------------------------- #
# 屏幕可用区域
# --------------------------------------------------------------------------- #
class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


def work_area() -> tuple[int, int, int, int]:
    """返回主屏工作区 (left, top, width, height)，已扣除任务栏。"""
    try:
        rect = _Rect()
        SPI_GETWORKAREA = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        pass
    return 0, 0, 1920, 1080


def default_position(width: int, height: int, margin: int = 24) -> tuple[int, int]:
    left, top, screen_w, screen_h = work_area()
    return left + screen_w - width - margin, top + screen_h - height - margin
