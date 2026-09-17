"""DeepSeek 余额挂件 安装 / 卸载程序。

设计目标：
- 只装到当前用户目录（%LOCALAPPDATA%\\Programs\\DeepSeekBalanceWidget），
  不需要管理员权限，不写 HKLM；
- 安装后创建开始菜单快捷方式，可选桌面快捷方式与开机自启；
- 注册到「应用和功能」，卸载时清理快捷方式、自启项和注册表；
- 配置与账本存在 %APPDATA%\\DeepSeekBalanceWidget，卸载默认保留（可选删除）。

打包：
    python -m PyInstaller --onefile --noconsole --add-data "dist/DeepSeekBalanceWidget.exe;payload"
        --icon app.ico installer.py
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import winreg
from pathlib import Path
from tkinter import filedialog

APP_NAME = "DeepSeek 余额挂件"
APP_ID = "DeepSeekBalanceWidget"
VERSION = "1.4.0"
EXE_NAME = "DeepSeekBalanceWidget.exe"
UNINSTALLER_NAME = "Uninstall.exe"
PUBLISHER = "自建"
RUN_VALUE = "DeepSeekBalanceWidget"

CARD = "#191d25"
CARD_LINE = "#2d3543"
FG = "#e9edf5"
DIM = "#8b93a5"
MUTED = "#5c6474"
ACCENT = "#5aa2ff"
INPUT_BG = "#11151c"
BTN_BG = "#232b38"
BTN_ACTIVE = "#2e3849"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
UNINSTALL_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}"


# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


def known_folder(folder_id: str) -> Path | None:
    """用 SHGetKnownFolderPath 取系统目录（桌面可能被 OneDrive 重定向）。"""
    try:
        ole32 = ctypes.windll.ole32
        shell32 = ctypes.windll.shell32
        guid = _GUID()
        if ole32.CLSIDFromString(ctypes.c_wchar_p(folder_id), ctypes.byref(guid)) != 0:
            return None
        out = ctypes.c_wchar_p()
        if shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None,
                                        ctypes.byref(out)) != 0:
            return None
        try:
            return Path(out.value)
        finally:
            ole32.CoTaskMemFree(out)
    except Exception:
        return None


def desktop_dir() -> Path:
    return (known_folder("{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}")
            or Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop")


def start_menu_dir() -> Path:
    return (known_folder("{A77F5D77-2E2B-44C3-A6A2-ABA601054A51}")
            or Path(os.environ.get("APPDATA", ""))
            / "Microsoft/Windows/Start Menu/Programs")


def default_install_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "Programs" / APP_ID


def config_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / APP_ID


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def payload_path() -> Path:
    """待安装的主程序：打包后在 _MEIPASS/payload，开发时在 dist/。"""
    bundled = resource_path(f"payload/{EXE_NAME}")
    if bundled.exists():
        return bundled
    return Path(__file__).resolve().parent / "dist" / EXE_NAME


def icon_path() -> Path | None:
    for candidate in (resource_path("app.ico"),
                      Path(__file__).resolve().parent / "app.ico"):
        if candidate.exists():
            return candidate
    return None


def uninstaller_source() -> Path | None:
    """卸载程序来源：优先用配套的精简卸载器，没有就退回复制自己。"""
    bundled = resource_path(f"uninstaller/{UNINSTALLER_NAME}")
    if bundled.exists():
        return bundled
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return None


# --------------------------------------------------------------------------- #
# 系统操作
# --------------------------------------------------------------------------- #
def create_shortcut(link: Path, target: Path, workdir: Path, icon: Path | None,
                    description: str) -> None:
    """用 WScript.Shell 建 .lnk（PowerShell 一行搞定，避免额外依赖）。"""
    def q(value: Path | str) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(" + q(link) + ");"
        "$s.TargetPath=" + q(target) + ";"
        "$s.WorkingDirectory=" + q(workdir) + ";"
        "$s.Description=" + q(description) + ";"
        + ("$s.IconLocation=" + q(str(icon)) + ";" if icon else "")
        + "$s.WindowStyle=1;$s.Save()"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   check=True, creationflags=subprocess.CREATE_NO_WINDOW,
                   capture_output=True)


def write_uninstall_entry(install_dir: Path, size_kb: int) -> None:
    exe = install_dir / EXE_NAME
    uninstaller = install_dir / UNINSTALLER_NAME
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, VERSION)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(exe))
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(install_dir))
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, size_kb)
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ,
                          f'"{uninstaller}" --uninstall')
        winreg.SetValueEx(key, "QuietUninstallString", 0, winreg.REG_SZ,
                          f'"{uninstaller}" --uninstall --silent')


def remove_uninstall_entry() -> None:
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError:
        pass


def set_autostart(exe: Path, enabled: bool) -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, f'"{exe}"')
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


def autostart_target() -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return str(winreg.QueryValueEx(key, RUN_VALUE)[0])
    except OSError:
        return ""


def process_ids(name: str) -> list[int]:
    # tasklist 输出是 OEM 代码页，这里按字节取回再解码，避免 UTF-8 解码炸掉
    result = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV",
                             "/NH"], capture_output=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    output = decode_console(result.stdout)
    ids: list[int] = []
    for line in output.splitlines():
        parts = [cell.strip('"') for cell in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == name.lower():
            try:
                ids.append(int(parts[1]))
            except ValueError:
                pass
    return ids


def decode_console(data: bytes | None) -> str:
    if not data:
        return ""
    for encoding in ("mbcs", "utf-8", "gbk"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def stop_running(install_dir: Path) -> bool:
    """结束正在运行的挂件（安装/卸载前必须先关掉，否则文件被占用）。"""
    stopped = False
    for pid in process_ids(EXE_NAME):
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        stopped = True
    if stopped:
        time.sleep(1.0)
    return stopped


def directory_size_kb(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            total += item.stat().st_size
    return max(1, total // 1024)


# --------------------------------------------------------------------------- #
# 已安装检测：支持"覆盖更新"
# --------------------------------------------------------------------------- #
def installed_info() -> dict | None:
    """读注册表里上一次安装的信息（安装到别处或手动拷贝的可能没有）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            return {
                "version": str(winreg.QueryValueEx(key, "DisplayVersion")[0]),
                "location": str(winreg.QueryValueEx(key, "InstallLocation")[0]),
            }
    except OSError:
        return None


def running_exe_paths() -> list[Path]:
    """正在运行的挂件 exe 路径（用来找出"没走安装程序、直接拷贝"的那些）。"""
    script = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
              f"(Get-Process -Name '{Path(EXE_NAME).stem}' -ErrorAction SilentlyContinue)"
              ".Path | Sort-Object -Unique")
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                             script], capture_output=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    paths: list[Path] = []
    for line in decode_console(result.stdout).splitlines():
        candidate = line.strip()
        if candidate and Path(candidate).exists():
            paths.append(Path(candidate))
    return paths


def detect_existing() -> dict | None:
    """找出已有的安装位置，供"覆盖更新"使用。"""
    info = installed_info()
    running = running_exe_paths()
    location: Path | None = None
    version: str | None = None
    if info and info.get("location") and Path(info["location"]).exists():
        location = Path(info["location"])
        version = info.get("version")
    if location is None and running:
        location = running[0].parent
    if location is None:
        fallback = default_install_dir()
        if (fallback / EXE_NAME).exists():
            location = fallback
    if location is None:
        return None
    # 优先读 exe 自己的版本资源：绿色版（直接拷贝的）也能认出真实版本，
    # 注册表只作为兜底（个别机器上注册表视图不一致）
    version = exe_version(location / EXE_NAME) or version
    return {
        "location": location,
        "version": version,
        "running": bool(running),
        "running_path": str(running[0]) if running else "",
    }


def exe_version(path: Path) -> str | None:
    """从 exe 的版本资源里读版本号。"""
    if not Path(path).exists():
        return None
    script = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
              f"(Get-Item -LiteralPath '{str(path).replace("'", "''")}')"
              ".VersionInfo.FileVersion")
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                             script], capture_output=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    value = decode_console(result.stdout).strip()
    return value or None


def schedule_delete(path: Path) -> None:
    """删不掉的文件（自身）登记到重启后删除。"""
    MOVEFILE_DELAY_UNTIL_REBOOT = 0x4
    ctypes.windll.kernel32.MoveFileExW(ctypes.c_wchar_p(str(path)), None,
                                       MOVEFILE_DELAY_UNTIL_REBOOT)


def link_target(link: Path) -> str:
    """读 .lnk 指向的目标（用来判断快捷方式是不是我们装的）。"""
    if not link.exists():
        return ""
    script = ("[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
              "$s=(New-Object -ComObject WScript.Shell).CreateShortcut("
              + "'" + str(link).replace("'", "''") + "');$s.TargetPath")
    result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                             script], capture_output=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    return decode_console(result.stdout).strip()


# --------------------------------------------------------------------------- #
# 安装 / 卸载
# --------------------------------------------------------------------------- #
def install(install_dir: Path, desktop_shortcut: bool, autostart: bool,
            run_after: bool, log, restart_if_running: bool = True) -> None:
    payload = payload_path()
    if not payload.exists():
        raise FileNotFoundError(f"安装包内没有找到主程序：{payload}")

    was_running = bool(process_ids(EXE_NAME))
    log("关闭正在运行的挂件…")
    if stop_running(install_dir):
        log("已结束旧实例。")

    log(f"创建目录 {install_dir}")
    install_dir.mkdir(parents=True, exist_ok=True)

    target = install_dir / EXE_NAME
    log("释放主程序…")
    shutil.copy2(payload, target)

    icon = icon_path()
    if icon:
        shutil.copy2(icon, install_dir / "app.ico")

    uninstaller = uninstaller_source()
    if uninstaller is not None:
        log("写入卸载程序…")
        shutil.copy2(uninstaller, install_dir / UNINSTALLER_NAME)

    log("创建开始菜单快捷方式…")
    create_shortcut(start_menu_dir() / f"{APP_NAME}.lnk", target, install_dir,
                    target, APP_NAME)

    desktop_link = desktop_dir() / f"{APP_NAME}.lnk"
    if desktop_shortcut:
        log("创建桌面快捷方式…")
        create_shortcut(desktop_link, target, install_dir, target, APP_NAME)
    else:
        try:
            desktop_link.unlink(missing_ok=True)
        except OSError:
            pass

    log("设置开机自启…" if autostart else "关闭开机自启…")
    set_autostart(target, autostart)

    log("注册到「应用和功能」…")
    write_uninstall_entry(install_dir, directory_size_kb(install_dir))

    if run_after or (restart_if_running and was_running):
        log("启动挂件…")
        subprocess.Popen([str(target)], cwd=str(install_dir),
                         creationflags=subprocess.CREATE_NO_WINDOW)

    log("安装完成。")


def uninstall(install_dir: Path, remove_config: bool, silent: bool, log) -> None:
    log("关闭正在运行的挂件…")
    if stop_running(install_dir):
        log("已结束挂件进程。")

    log("删除快捷方式…")
    for link in (start_menu_dir() / f"{APP_NAME}.lnk",
                 desktop_dir() / f"{APP_NAME}.lnk"):
        try:
            if link.exists() and str(install_dir).lower() in link_target(link).lower():
                link.unlink()
            elif link.exists() and silent:
                pass
        except OSError:
            pass

    log("清理开机自启项…")
    if str(install_dir).lower() in autostart_target().lower():
        set_autostart(install_dir / EXE_NAME, False)

    log("移除「应用和功能」条目…")
    remove_uninstall_entry()

    if remove_config:
        log("删除配置与账本数据…")
        shutil.rmtree(config_dir(), ignore_errors=True)

    log("删除程序文件…")
    self_path = Path(sys.executable).resolve() if getattr(sys, "frozen", False) else None
    # 正在运行的卸载程序自己删不掉：先改名挪到 %TEMP%，安装目录就能整个删掉
    if self_path is not None and install_dir in self_path.parents:
        temp_copy = Path(os.environ.get("TEMP", str(Path.home()))) / (
            f"ds-widget-uninstall-{os.getpid()}.tmp")
        try:
            os.replace(self_path, temp_copy)
            self_path = temp_copy
        except OSError:
            pass

    leftover: list[Path] = []
    if install_dir.exists():
        for item in sorted(install_dir.rglob("*"), reverse=True):
            try:
                if item.is_dir():
                    item.rmdir()
                else:
                    item.unlink()
            except OSError:
                leftover.append(item)
        try:
            install_dir.rmdir()
        except OSError:
            schedule_delete(install_dir)

    if self_path is not None and self_path.exists():
        try:
            self_path.unlink()
        except OSError:
            schedule_delete(self_path)

    log("卸载完成。" if not leftover else
        f"卸载完成，{len(leftover)} 个文件被占用，将在重启后清理。")


# --------------------------------------------------------------------------- #
# 界面
# --------------------------------------------------------------------------- #
def enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _button(parent, text, command, primary=False):
    return tk.Button(parent, text=text, command=command,
                     bg=ACCENT if primary else BTN_BG,
                     fg="#0b1220" if primary else FG,
                     activebackground="#7fb6ff" if primary else BTN_ACTIVE,
                     activeforeground="#0b1220" if primary else FG,
                     relief="flat", padx=16, pady=6, cursor="hand2")


class InstallerWindow:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} 安装程序")
        self.root.configure(bg=CARD)
        self.root.resizable(False, False)
        icon = icon_path()
        if icon:
            try:
                self.root.iconbitmap(default=str(icon))
            except tk.TclError:
                pass

        pad = {"padx": 16, "pady": 6}
        tk.Label(self.root, text=f"{APP_NAME}  {VERSION}", bg=CARD, fg=FG,
                 font=("Microsoft YaHei UI", 13, "bold"))\
            .grid(row=0, column=0, columnspan=2, sticky="w", padx=16, pady=(16, 0))
        tk.Label(self.root, text="余额 + 峰谷价格 + 账本，常驻桌面右下角。仅当前用户安装，无需管理员权限。",
                 bg=CARD, fg=DIM, font=("Microsoft YaHei UI", 9), justify="left",
                 wraplength=420)\
            .grid(row=1, column=0, columnspan=2, sticky="w", padx=16, pady=(2, 10))

        # ---- 覆盖更新检测 ------------------------------------------------ #
        self.existing = detect_existing()
        banner_text = "全新安装"
        banner_color = MUTED
        if self.existing:
            version = self.existing.get("version")
            version_text = f"v{version}" if version else "未知版本"
            banner_text = f"检测到已安装 {version_text}：{self.existing['location']}\n" \
                          "将覆盖更新，配置、API key 与账本都会保留。"
            if self.existing.get("running"):
                banner_text += "（挂件正在运行，更新时会自动关闭并重新启动）"
            banner_color = ACCENT
        self.banner = tk.Label(self.root, text=banner_text, bg=CARD, fg=banner_color,
                               anchor="w", justify="left", wraplength=430,
                               font=("Microsoft YaHei UI", 9))
        self.banner.grid(row=2, column=0, columnspan=3, sticky="w", padx=16, pady=(0, 6))

        tk.Label(self.root, text="安装位置", bg=CARD, fg=DIM)\
            .grid(row=3, column=0, sticky="w", **pad)
        self.dir_var = tk.StringVar(
            value=str(self.existing["location"]) if self.existing
            else str(default_install_dir()))
        tk.Entry(self.root, textvariable=self.dir_var, width=44, bg=INPUT_BG, fg=FG,
                 insertbackground=FG, relief="flat")\
            .grid(row=3, column=1, sticky="we", padx=(0, 6), pady=6)
        tk.Button(self.root, text="浏览…", command=self.browse, bg=BTN_BG, fg=FG,
                  activebackground=BTN_ACTIVE, activeforeground=FG, relief="flat",
                  padx=10).grid(row=3, column=2, padx=(0, 16), pady=6)

        desktop_link = desktop_dir() / f"{APP_NAME}.lnk"
        self.desktop_var = tk.BooleanVar(
            value=desktop_link.exists() if self.existing else True)
        self.autostart_var = tk.BooleanVar(
            value=(str(self.existing["location"]).lower() in autostart_target().lower()
                   if self.existing else True))
        self.run_var = tk.BooleanVar(value=True)
        for row, (text, var) in enumerate((
            ("创建桌面快捷方式", self.desktop_var),
            ("开机自动启动", self.autostart_var),
            ("安装完成后立即运行", self.run_var),
        ), start=4):
            tk.Checkbutton(self.root, text=text, variable=var, bg=CARD, fg=FG,
                           selectcolor=INPUT_BG, activebackground=CARD,
                           activeforeground=FG, anchor="w")\
                .grid(row=row, column=0, columnspan=3, sticky="w", padx=16, pady=1)

        self.status = tk.Label(self.root, text="准备就绪。", bg=CARD, fg=MUTED,
                               anchor="w", justify="left", wraplength=460,
                               font=("Microsoft YaHei UI", 9))
        self.status.grid(row=7, column=0, columnspan=3, sticky="w", padx=16, pady=(12, 0))

        buttons = tk.Frame(self.root, bg=CARD)
        buttons.grid(row=8, column=0, columnspan=3, sticky="e", padx=16, pady=16)
        self.install_button = _button(
            buttons, "覆盖更新" if self.existing else "安装", self.start_install,
            primary=True)
        self.install_button.pack(side="left", padx=4)
        self.cancel_button = _button(buttons, "取消", self.root.destroy)
        self.cancel_button.pack(side="left", padx=4)

        self.root.update_idletasks()
        self.center()

    def center(self) -> None:
        width, height = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        x = (self.root.winfo_screenwidth() - width) // 2
        y = (self.root.winfo_screenheight() - height) // 3
        self.root.geometry(f"+{max(0, x)}+{max(0, y)}")

    def browse(self) -> None:
        chosen = filedialog.askdirectory(parent=self.root, title="选择安装位置",
                                         initialdir=self.dir_var.get())
        if chosen:
            self.dir_var.set(chosen)

    def log(self, text: str) -> None:
        self.status.configure(text=text)
        self.root.update_idletasks()

    def start_install(self) -> None:
        self.install_button.configure(state="disabled")
        self.cancel_button.configure(state="disabled")
        install_dir = Path(self.dir_var.get())
        desktop = self.desktop_var.get()
        autostart = self.autostart_var.get()
        run_after = self.run_var.get()

        def worker() -> None:
            try:
                install(install_dir, desktop, autostart, run_after, self._log_threadsafe)
                self.root.after(0, lambda: self.finish(True, install_dir))
            except Exception as error:  # noqa: BLE001
                message = f"安装失败：{error}"
                self.root.after(0, lambda: self.finish(False, install_dir, message))

        threading.Thread(target=worker, daemon=True).start()

    def _log_threadsafe(self, text: str) -> None:
        self.root.after(0, lambda: self.log(text))

    def finish(self, ok: bool, install_dir: Path, message: str = "") -> None:
        if ok:
            action = "更新完成！" if self.existing else "安装完成！"
            self.status.configure(
                text=f"{action}\n程序目录：{install_dir}\n"
                     f"配置与账本：{config_dir()}\n"
                     f"可在「设置 → 应用 → 已安装的应用」里卸载。",
                fg=FG)
            self.install_button.configure(text="完成", state="normal",
                                          command=self.root.destroy)
            self.cancel_button.configure(text="关闭", state="normal")
        else:
            self.status.configure(text=message, fg="#ff6b6b")
            self.install_button.configure(state="normal")
            self.cancel_button.configure(state="normal")

    def run(self) -> None:
        self.root.mainloop()


class UninstallWindow:
    def __init__(self, install_dir: Path) -> None:
        self.install_dir = install_dir
        self.root = tk.Tk()
        self.root.title(f"卸载 {APP_NAME}")
        self.root.configure(bg=CARD)
        self.root.resizable(False, False)
        icon = icon_path()
        if icon:
            try:
                self.root.iconbitmap(default=str(icon))
            except tk.TclError:
                pass

        tk.Label(self.root, text=f"卸载 {APP_NAME}", bg=CARD, fg=FG,
                 font=("Microsoft YaHei UI", 13, "bold"))\
            .grid(row=0, column=0, sticky="w", padx=16, pady=(16, 6))
        tk.Label(self.root, text=f"程序目录：{install_dir}", bg=CARD, fg=DIM,
                 justify="left", wraplength=420, font=("Microsoft YaHei UI", 9))\
            .grid(row=1, column=0, sticky="w", padx=16)

        self.config_var = tk.BooleanVar(value=False)
        tk.Checkbutton(self.root, text="同时删除配置、API key 与账本数据（不可恢复）",
                       variable=self.config_var, bg=CARD, fg=FG, selectcolor=INPUT_BG,
                       activebackground=CARD, activeforeground=FG, anchor="w")\
            .grid(row=2, column=0, sticky="w", padx=16, pady=(10, 0))

        self.status = tk.Label(self.root, text="", bg=CARD, fg=MUTED, anchor="w",
                               justify="left", wraplength=440)
        self.status.grid(row=3, column=0, sticky="w", padx=16, pady=(12, 0))

        buttons = tk.Frame(self.root, bg=CARD)
        buttons.grid(row=4, column=0, sticky="e", padx=16, pady=16)
        self.ok = _button(buttons, "确认卸载", self.start, primary=True)
        self.ok.pack(side="left", padx=4)
        _button(buttons, "取消", self.root.destroy).pack(side="left", padx=4)
        self.root.update_idletasks()
        x = (self.root.winfo_screenwidth() - self.root.winfo_reqwidth()) // 2
        y = (self.root.winfo_screenheight() - self.root.winfo_reqheight()) // 3
        self.root.geometry(f"+{max(0, x)}+{max(0, y)}")

    def start(self) -> None:
        self.ok.configure(state="disabled")
        remove_config = self.config_var.get()
        install_dir = self.install_dir

        def worker() -> None:
            try:
                uninstall(install_dir, remove_config, False,
                          lambda text: self.root.after(0, lambda: self.status.configure(
                              text=text, fg=MUTED)))
                self.root.after(0, lambda: self.status.configure(
                    text="卸载完成，窗口即将关闭。", fg=FG))
                self.root.after(1200, self.root.destroy)
            except Exception as error:  # noqa: BLE001
                message = f"卸载失败：{error}"
                self.root.after(0, lambda: self.status.configure(text=message,
                                                                 fg="#ff6b6b"))
                self.root.after(0, lambda: self.ok.configure(state="normal"))

        threading.Thread(target=worker, daemon=True).start()

    def run(self) -> None:
        self.root.mainloop()


# --------------------------------------------------------------------------- #
def parse_args():
    parser = argparse.ArgumentParser(description=f"{APP_NAME} 安装程序")
    parser.add_argument("--uninstall", action="store_true", help="进入卸载模式")
    parser.add_argument("--silent", action="store_true", help="不显示界面")
    parser.add_argument("--dir", metavar="PATH", help="指定安装目录")
    parser.add_argument("--update", action="store_true",
                        help="覆盖更新到已安装（或正在运行）的位置")
    parser.add_argument("--no-desktop-shortcut", action="store_true")
    parser.add_argument("--no-autostart", action="store_true")
    parser.add_argument("--no-run", action="store_true")
    parser.add_argument("--remove-config", action="store_true",
                        help="卸载时同时删除配置与账本")
    parser.add_argument("--detect", action="store_true",
                        help="打印检测到的已安装信息后退出（排查用）")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.detect:
        print("注册表记录：", installed_info())
        print("正在运行的路径：", [str(p) for p in running_exe_paths()])
        print("检测结果：", detect_existing())
        return 0
    if args.dir:
        install_dir = Path(args.dir)
    elif (getattr(sys, "frozen", False)
          and Path(sys.executable).name.lower() == UNINSTALLER_NAME.lower()):
        # 被安装到目标目录里的 Uninstall.exe：自己所在的目录就是安装目录
        install_dir = Path(sys.executable).resolve().parent
    else:
        install_dir = default_install_dir()

    if args.update and not args.dir:
        existing = detect_existing()
        if existing is not None:
            install_dir = Path(existing["location"])
            print(f"检测到已有安装：{install_dir}"
                  + (f"（v{existing['version']}）" if existing.get("version") else ""))

    if args.silent:
        # 窗口模式下没有控制台，日志同时写文件，方便排查
        log_file = Path(os.environ.get("TEMP", str(Path.home()))) / "deepseek-setup.log"

        def printer(text: str) -> None:
            try:
                with log_file.open("a", encoding="utf-8") as handle:
                    handle.write(text + "\n")
            except OSError:
                pass

        try:
            if args.uninstall:
                uninstall(install_dir, args.remove_config, True, printer)
            else:
                install(install_dir, not args.no_desktop_shortcut,
                        not args.no_autostart, False, printer)
        except Exception as error:  # noqa: BLE001
            printer(f"失败：{error!r}（日志：{log_file}）")
            return 1
        return 0

    enable_dpi_awareness()
    if args.uninstall:
        UninstallWindow(install_dir).run()
    else:
        InstallerWindow().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
