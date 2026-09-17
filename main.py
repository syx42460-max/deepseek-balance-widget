"""DeepSeek 余额桌面挂件 —— 入口。

常用：
    python main.py                 启动挂件
    python main.py --set-key sk-xx 把 API key 加密写入本机配置
    python main.py --interval 15   把刷新间隔改成 15 分钟
    python main.py --reset-position
"""

from __future__ import annotations

import argparse
import ctypes
import queue
import sys
import threading
import tkinter as tk
from tkinter import colorchooser, ttk
from datetime import datetime, timedelta

import app_config
import ledger as ledger_module
import ledger_window
import price_source
import pricing
import themes
from app_config import Config, is_autostart_enabled, set_autostart
from deepseek_api import BalanceError, fetch_balance
from ui import ViewState, WidgetWindow

# 设置面板用的调色板（随主题变化，建面板时实时读取）
CARD = DIM = FG = MUTED = INPUT_BG = BTN_BG = BTN_ACTIVE = GOOD = BAD = BORDER = ""


def refresh_palette() -> None:
    global CARD, DIM, FG, MUTED, INPUT_BG, BTN_BG, BTN_ACTIVE, GOOD, BAD, BORDER
    theme = themes.current()
    CARD, DIM, FG, MUTED = theme.card, theme.dim, theme.fg, theme.muted
    INPUT_BG, BTN_BG, BTN_ACTIVE = theme.input_bg, theme.button_bg, theme.button_active
    GOOD, BAD, BORDER = theme.good, theme.bad, theme.border


def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def single_instance_guard():
    """返回互斥体句柄；已经在运行时返回 None。"""
    if sys.platform != "win32":
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, "DeepSeekBalanceWidget_SingleInstance")
    if not handle:
        return True
    ERROR_ALREADY_EXISTS = 183
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return None
    return handle


class Application:
    COMPACT_REFRESH_SECONDS = 30     # 收起状态（靠边小条 / 折叠峰谷区）下的刷新间隔

    def __init__(self, config: Config) -> None:
        self.config = config
        self.queue: queue.Queue = queue.Queue()
        self.balance = None
        self.error: str | None = None
        self.loading = False
        self.next_refresh = datetime.now()
        self.pricing_busy = False
        self.pricing_error: str | None = None
        self.pricing_message = ""
        self.pricing_loaded_from_cache = False
        self.next_pricing_refresh: datetime | None = None
        self.settings_dialog: "SettingsDialog | None" = None
        self.ledger_window = None
        self.ledger = ledger_module.Ledger(
            app_config.LEDGER_PATH,
            session_minutes=int(config["ledger_session_minutes"] or 15),
        )

        themes.set_current(themes.resolve(config))
        refresh_palette()
        self._load_pricing_cache()
        self.schedule_pricing_refresh()

        self.window = WidgetWindow(config, {
            "refresh": self.refresh_now,
            "refresh_pricing": self.refresh_pricing,
            "ledger": self.open_ledger,
            "toggle_prices": lambda: self.window.toggle_prices(),
            "dock": lambda: self.window.dock(),
            "expand_dock": lambda: self.window.undock(),
            "settings": self.open_settings,
            "quit": self.quit,
            "topmost": self.set_topmost,
            "autostart": self.set_autostart,
        })
        self.window.root.protocol("WM_DELETE_WINDOW", self.quit)
        # Tk 的默认回调异常会打到 stderr，而 pythonw 没有控制台，这里改成写日志
        self.window.root.report_callback_exception = (
            lambda exc_type, exc, tb: app_config.log_exception("tk-callback", exc))
        self.tick()

    # ------------------------------------------------------------------ #
    def tick(self) -> None:
        self._drain_queue()
        now = datetime.now()
        # 收起状态下刷新更勤：把下次刷新时间往前提，最多不超过当前间隔
        limit = now + self.refresh_interval()
        if self.next_refresh > limit:
            self.next_refresh = limit
        if not self.loading and now >= self.next_refresh:
            self.refresh_now()
        if self.ledger.close_stale(now):
            self._notify_ledger()
        if (not self.pricing_busy and self.next_pricing_refresh is not None
                and now >= self.next_pricing_refresh):
            self.refresh_pricing(silent=True)
        try:
            self.window.render(ViewState(
                now=now,
                balance=self.balance,
                error=self.error,
                pricing_error=self.pricing_error,
                loading=self.loading,
                low_balance_alert=float(self.config["low_balance_alert"] or 0),
            ))
        except Exception as error:  # 渲染异常不能让刷新循环死掉
            app_config.log_exception("render", error)
        self.window.root.after(1000, self.tick)

    def _drain_queue(self) -> None:
        while True:
            try:
                kind, payload = self.queue.get_nowait()
            except queue.Empty:
                return
            if kind == "ok":
                self._on_balance(payload)
                self.error = None
                self.loading = False
            elif kind == "error":
                self.error = str(payload)
                self.loading = False
            elif kind == "price_ok":
                self.pricing_busy = False
                self.pricing_error = None
                self._on_pricing_updated(payload)
            elif kind == "price_error":
                self.pricing_busy = False
                self.pricing_error = str(payload)
                self.pricing_message = f"刷新失败：{payload}"
                # 失败后 1 小时再试，避免每分钟重试把网络打爆
                self.next_pricing_refresh = (datetime.now() + timedelta(hours=1)
                                             if self.config["pricing_auto_refresh"] else None)

    # ------------------------------------------------------------------ #
    # 峰谷价格刷新
    # ------------------------------------------------------------------ #
    def _load_pricing_cache(self) -> None:
        table = pricing.load_cache(app_config.PRICING_CACHE_PATH)
        if table is not None:
            pricing.activate(table)
            self.pricing_loaded_from_cache = True

    def schedule_pricing_refresh(self) -> None:
        if not self.config["pricing_auto_refresh"]:
            self.next_pricing_refresh = None
            return
        hours = max(1, int(self.config["pricing_refresh_hours"] or 24))
        last = self.config["pricing_last_refresh"]
        try:
            last_at = datetime.fromisoformat(last) if last else None
        except ValueError:
            last_at = None
        if last_at is None:
            self.next_pricing_refresh = datetime.now()
        else:
            self.next_pricing_refresh = max(datetime.now(),
                                            last_at + timedelta(hours=hours))

    def refresh_pricing(self, silent: bool = False) -> None:
        """抓取官方价格页并替换当前价格表。"""
        if self.pricing_busy:
            return
        self.pricing_busy = True
        self.pricing_error = None
        if not silent:
            self.pricing_message = "正在刷新峰谷价格…"
        threading.Thread(target=self._pricing_worker, daemon=True).start()

    def _pricing_worker(self) -> None:
        try:
            self.queue.put(("price_ok", price_source.fetch_price_table()))
        except price_source.PriceSourceError as error:
            self.queue.put(("price_error", str(error)))
        except Exception as error:
            app_config.log_exception("pricing", error)
            self.queue.put(("price_error", f"未知错误：{error}"))

    def _on_pricing_updated(self, table: pricing.PriceTable) -> None:
        pricing.activate(table)
        try:
            pricing.save_cache(app_config.PRICING_CACHE_PATH, table)
        except Exception as error:
            app_config.log_exception("pricing-cache", error)
        self.config["pricing_last_refresh"] = datetime.now().isoformat(timespec="seconds")
        self.config.save()
        self.schedule_pricing_refresh()
        self.pricing_message = self.pricing_status_text()

    def pricing_status_text(self) -> str:
        table = pricing.current()
        if self.pricing_busy:
            return "正在刷新…"
        if self.pricing_error:
            return f"刷新失败：{self.pricing_error}"
        if table.fetched_at:
            return (f"官方文档 · {table.fetched_at[:16].replace('T', ' ')}"
                    f" · {len(table.models)} 个模型")
        return "当前使用内置默认价格"

    # ------------------------------------------------------------------ #
    def refresh_now(self) -> None:
        if self.loading:
            return
        self.loading = True
        self.error = None
        self.next_refresh = datetime.now() + self.refresh_interval()
        threading.Thread(target=self._fetch_worker, daemon=True).start()

    def refresh_interval(self) -> timedelta:
        """收起状态 30 秒一次，展开状态按设置里的分钟数。"""
        window = getattr(self, "window", None)
        if window is not None and window.is_compact():
            return timedelta(seconds=self.COMPACT_REFRESH_SECONDS)
        return timedelta(minutes=max(1, int(self.config["refresh_minutes"] or 10)))

    def _fetch_worker(self) -> None:
        try:
            self.queue.put(("ok", fetch_balance(self.config.api_key)))
        except BalanceError as error:
            self.queue.put(("error", str(error)))
        except Exception as error:  # 兜底，避免线程静默挂掉
            app_config.log_exception("fetch", error)
            self.queue.put(("error", f"未知错误：{error}"))

    # ------------------------------------------------------------------ #
    def set_topmost(self, enabled: bool) -> None:
        self.config["topmost"] = bool(enabled)
        self.config.save()
        self.window.apply_preferences()

    # ------------------------------------------------------------------ #
    # 账本
    # ------------------------------------------------------------------ #
    def _on_balance(self, balance) -> None:
        """余额到手后跟上次比较，差额记进账本。"""
        now = datetime.now()
        previous = self.balance.total if self.balance else self.ledger.last_total
        same_currency = self.balance is None or self.balance.currency == balance.currency
        if previous is not None and same_currency:
            delta = round(previous - balance.total, 6)
            if delta > ledger_module.EPSILON:
                period = "peak" if pricing.is_peak(now) else "off_peak"
                note = "" if self.balance is not None else "挂件未运行期间累计"
                self.ledger.record_spend(delta, now, period, note=note,
                                         currency=balance.currency)
                self._notify_ledger()
            elif delta < -ledger_module.EPSILON:
                self.ledger.record_topup(-delta, now)
                self._notify_ledger()
        self.balance = balance
        self.ledger.note_total(balance.total, now)
        self.ledger.save()

    def open_ledger(self) -> None:
        if self.ledger_window is not None:
            try:
                self.ledger_window.window.lift()
                return
            except tk.TclError:
                self.ledger_window = None
        self.ledger_window = ledger_window.LedgerWindow(self)

    def _notify_ledger(self) -> None:
        if self.ledger_window is not None:
            try:
                self.ledger_window.refresh()
            except tk.TclError:
                self.ledger_window = None

    def set_ledger_window_minutes(self, minutes: int) -> None:
        minutes = max(1, min(240, int(minutes)))
        self.config["ledger_session_minutes"] = minutes
        self.ledger.session_minutes = minutes
        self.config.save()

    # ------------------------------------------------------------------ #
    # 主题
    # ------------------------------------------------------------------ #
    def apply_theme(self, theme_key: str, accent: str | None = None) -> None:
        self.config["theme"] = theme_key
        if accent is not None:
            self.config["accent_color"] = accent
        self.config.save()
        theme = themes.resolve(self.config)
        themes.set_current(theme)
        refresh_palette()
        self.window.set_theme(theme)
        if self.ledger_window is not None:   # 账本窗口重开一次来换色
            self.ledger_window.close()
            self.open_ledger()

    def set_autostart(self, enabled: bool) -> None:
        try:
            set_autostart(bool(enabled))
        except Exception as error:
            self.error = f"设置开机自启失败：{error}"
        self.config["autostart"] = is_autostart_enabled()
        self.config.save()

    def open_settings(self) -> None:
        if self.settings_dialog is not None:
            try:
                self.settings_dialog.window.lift()
                return
            except tk.TclError:
                self.settings_dialog = None
        self.settings_dialog = SettingsDialog(self)

    def quit(self) -> None:
        self.config.save()
        self.window.destroy()

    def run(self) -> None:
        self.window.root.mainloop()


class SettingsDialog:
    def __init__(self, app: Application) -> None:
        self.app = app
        config = app.config
        self.window = tk.Toplevel(app.window.root)
        self.window.title("DeepSeek 挂件设置")
        self.window.configure(bg=CARD)
        self.window.resizable(False, False)
        self.window.attributes("-topmost", True)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        pad = {"padx": 14, "pady": 6}
        row = 0

        def label(text: str) -> None:
            nonlocal row
            tk.Label(self.window, text=text, bg=CARD, fg=DIM, anchor="w")\
                .grid(row=row, column=0, sticky="w", **pad)

        label("API key")
        key_text = tk.StringVar(value=config.api_key)
        self.key_var = key_text
        entry = tk.Entry(self.window, textvariable=key_text, width=40, show="•",
                         bg=INPUT_BG, fg=FG, insertbackground=FG, relief="flat")
        entry.grid(row=row, column=1, sticky="we", **pad)
        row += 1

        label("刷新间隔（分钟）")
        self.interval_var = tk.IntVar(value=int(config["refresh_minutes"]))
        tk.Spinbox(self.window, from_=1, to=1440, textvariable=self.interval_var,
                   width=8, bg=INPUT_BG, fg=FG, relief="flat",
                   buttonbackground=BTN_BG).grid(row=row, column=1, sticky="w", **pad)
        row += 1

        label("低余额提醒阈值（元）")
        self.threshold_var = tk.StringVar(value=str(config["low_balance_alert"]))
        tk.Entry(self.window, textvariable=self.threshold_var, width=10, bg=INPUT_BG,
                 fg=FG, insertbackground=FG, relief="flat")\
            .grid(row=row, column=1, sticky="w", **pad)
        row += 1

        label("窗口不透明度")
        self.opacity_var = tk.DoubleVar(value=float(config["opacity"]))
        tk.Scale(self.window, variable=self.opacity_var, from_=0.4, to=1.0,
                 resolution=0.05, orient="horizontal", bg=CARD, fg=DIM,
                 troughcolor=INPUT_BG, highlightthickness=0, length=180,
                 command=self._preview_opacity).grid(row=row, column=1, sticky="w", **pad)
        row += 1

        self.topmost_var = tk.BooleanVar(value=bool(config["topmost"]))
        self.autostart_var = tk.BooleanVar(value=bool(is_autostart_enabled()))
        for text, var in (("窗口置顶", self.topmost_var), ("开机自启", self.autostart_var)):
            tk.Checkbutton(self.window, text=text, variable=var, bg=CARD, fg=FG,
                           selectcolor=INPUT_BG, activebackground=CARD,
                           activeforeground=FG, anchor="w")\
                .grid(row=row, column=0, columnspan=2, sticky="w", **pad)
            row += 1

        # ---- 峰谷价格 -------------------------------------------------- #
        tk.Label(self.window, text="峰谷价格", bg=CARD, fg=FG, anchor="w",
                 font=("Microsoft YaHei UI", 10, "bold"))\
            .grid(row=row, column=0, columnspan=2, sticky="w", padx=14, pady=(14, 0))
        row += 1

        self.pricing_status_var = tk.StringVar(value=app.pricing_status_text())
        tk.Label(self.window, textvariable=self.pricing_status_var, bg=CARD, fg=MUTED,
                 anchor="w", justify="left", wraplength=340)\
            .grid(row=row, column=0, columnspan=2, sticky="w", padx=14, pady=(2, 0))
        row += 1

        pricing_row = tk.Frame(self.window, bg=CARD)
        pricing_row.grid(row=row, column=0, columnspan=2, sticky="w", padx=10, pady=6)
        tk.Button(pricing_row, text="立即刷新", command=self.refresh_pricing, bg=BTN_BG,
                  fg=FG, activebackground=BTN_ACTIVE, activeforeground=FG, relief="flat",
                  padx=10, pady=3).pack(side="left", padx=4)
        self.pricing_auto_var = tk.BooleanVar(value=bool(config["pricing_auto_refresh"]))
        tk.Checkbutton(pricing_row, text="自动刷新，每", variable=self.pricing_auto_var,
                       bg=CARD, fg=FG, selectcolor=INPUT_BG, activebackground=CARD,
                       activeforeground=FG).pack(side="left", padx=(6, 0))
        self.pricing_hours_var = tk.IntVar(value=int(config["pricing_refresh_hours"] or 24))
        tk.Spinbox(pricing_row, from_=1, to=168, textvariable=self.pricing_hours_var,
                   width=4, bg=INPUT_BG, fg=FG, relief="flat",
                   buttonbackground=BTN_BG).pack(side="left")
        tk.Label(pricing_row, text="小时", bg=CARD, fg=DIM).pack(side="left", padx=(3, 0))
        row += 1

        # ---- 外观：主题配色 --------------------------------------------- #
        self.theme_keys = [key for key, _ in themes.choices()]
        self.theme_names = [name for _, name in themes.choices()]
        self._accent = str(config["accent_color"] or "")

        tk.Label(self.window, text="外观", bg=CARD, fg=FG, anchor="w",
                 font=("Microsoft YaHei UI", 10, "bold"))\
            .grid(row=row, column=0, columnspan=2, sticky="w", padx=14, pady=(14, 0))
        row += 1

        style = ttk.Style(self.window)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Theme.TCombobox", fieldbackground=INPUT_BG, background=BTN_BG,
                        foreground=FG, arrowcolor=DIM, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER)
        style.map("Theme.TCombobox",
                  fieldbackground=[("readonly", INPUT_BG), ("disabled", INPUT_BG)],
                  background=[("readonly", BTN_BG), ("active", BTN_ACTIVE)],
                  foreground=[("readonly", FG), ("disabled", MUTED)],
                  selectbackground=[("readonly", INPUT_BG)],
                  selectforeground=[("readonly", FG)])
        self.window.option_add("*TCombobox*Listbox.background", INPUT_BG)
        self.window.option_add("*TCombobox*Listbox.foreground", FG)
        self.window.option_add("*TCombobox*Listbox.selectBackground", BTN_ACTIVE)

        appearance = tk.Frame(self.window, bg=CARD)
        appearance.grid(row=row, column=0, columnspan=2, sticky="w", padx=10, pady=6)
        tk.Label(appearance, text="主题配色", bg=CARD, fg=DIM).pack(side="left", padx=(4, 6))
        current_key = str(config["theme"])
        self.theme_var = tk.StringVar(
            value=self.theme_names[self.theme_keys.index(current_key)]
            if current_key in self.theme_keys else self.theme_names[0])
        combo = ttk.Combobox(appearance, textvariable=self.theme_var, state="readonly",
                             width=14, values=self.theme_names, style="Theme.TCombobox")
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", self._on_theme_change)
        tk.Button(appearance, text="自定义主色", command=self.pick_accent, bg=BTN_BG,
                  fg=FG, activebackground=BTN_ACTIVE, activeforeground=FG, relief="flat",
                  padx=8, pady=2).pack(side="left", padx=(8, 4))
        tk.Button(appearance, text="恢复默认色", command=self.reset_accent, bg=BTN_BG,
                  fg=FG, activebackground=BTN_ACTIVE, activeforeground=FG, relief="flat",
                  padx=8, pady=2).pack(side="left")
        row += 1

        self.accent_hint = tk.Label(
            self.window, bg=CARD, fg=MUTED, anchor="w", justify="left", wraplength=400,
            text=("主题会立刻应用到挂件；设置面板本身下次打开时换色。"
                  + (f"当前自定义主色：{self._accent}" if self._accent else "")))
        self.accent_hint.grid(row=row, column=0, columnspan=2, sticky="w",
                              padx=14, pady=(0, 2))
        row += 1

        # ---- 账本 -------------------------------------------------------- #
        tk.Label(self.window, text="账本", bg=CARD, fg=FG, anchor="w",
                 font=("Microsoft YaHei UI", 10, "bold"))\
            .grid(row=row, column=0, columnspan=2, sticky="w", padx=14, pady=(14, 0))
        row += 1

        ledger_row = tk.Frame(self.window, bg=CARD)
        ledger_row.grid(row=row, column=0, columnspan=2, sticky="w", padx=10, pady=6)
        tk.Button(ledger_row, text="打开账本", command=app.open_ledger, bg=BTN_BG,
                  fg=FG, activebackground=BTN_ACTIVE, activeforeground=FG, relief="flat",
                  padx=10, pady=3).pack(side="left", padx=(4, 10))
        tk.Label(ledger_row, text="会话合并窗口", bg=CARD, fg=DIM).pack(side="left")
        self.ledger_minutes_var = tk.IntVar(
            value=int(config["ledger_session_minutes"] or 15))
        tk.Spinbox(ledger_row, from_=1, to=240, textvariable=self.ledger_minutes_var,
                   width=5, bg=INPUT_BG, fg=FG, relief="flat",
                   buttonbackground=BTN_BG).pack(side="left", padx=(6, 3))
        tk.Label(ledger_row, text="分钟（相邻扣费在此间隔内算同一次对话）",
                 bg=CARD, fg=MUTED).pack(side="left")
        row += 1

        self.status = tk.Label(self.window, text="", bg=CARD, fg=MUTED, anchor="w")
        self.status.grid(row=row, column=0, columnspan=2, sticky="w", **pad)
        row += 1

        buttons = tk.Frame(self.window, bg=CARD)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", padx=14, pady=10)
        for text, command in (("测试连接", self.test_connection), ("取消", self.close),
                              ("保存", self.save)):
            tk.Button(buttons, text=text, command=command, bg=BTN_BG, fg=FG,
                      activebackground=BTN_ACTIVE, activeforeground=FG, relief="flat",
                      padx=12, pady=4).pack(side="left", padx=4)

        self._center()
        entry.focus_set()

    def _center(self) -> None:
        # 新建的 Toplevel 在布局完成前 winfo_width/height 还不可靠，
        # 这里强制刷新一次，并用请求尺寸兜底，避免窗口被放到屏幕外。
        self.window.update_idletasks()
        self.window.update()
        width = max(self.window.winfo_width(), self.window.winfo_reqwidth())
        height = max(self.window.winfo_height(), self.window.winfo_reqheight())
        parent = self.app.window.root
        left, top, screen_w, screen_h = app_config.work_area()
        right, bottom = left + screen_w, top + screen_h

        # 优先放在挂件左侧；放不下就放挂件右侧，再不行才夹到屏幕里
        candidates_x = (parent.winfo_x() - width - 16,
                        parent.winfo_x() + parent.winfo_width() + 16)
        x = next((value for value in candidates_x if left + 8 <= value
                  and value + width <= right - 8), candidates_x[0])

        # 优先和挂件顶部对齐；下方放不下就挪到挂件上方
        candidates_y = (parent.winfo_y() + 40, parent.winfo_y() - height - 12)
        y = next((value for value in candidates_y if top + 8 <= value
                  and value + height <= bottom - 8), candidates_y[0])

        x = max(left + 8, min(x, right - width - 8))
        y = max(top + 8, min(y, bottom - height - 8))
        self.window.geometry(f"+{x}+{y}")

    def _preview_opacity(self, _value=None) -> None:
        self.app.window.root.attributes("-alpha", self.opacity_var.get())

    # ---- 主题 ---------------------------------------------------------- #
    def _current_theme_key(self) -> str:
        try:
            return self.theme_keys[self.theme_names.index(self.theme_var.get())]
        except ValueError:
            return self.theme_keys[0]

    def _on_theme_change(self, _event=None) -> None:
        self.app.apply_theme(self._current_theme_key(), self._accent)

    def pick_accent(self) -> None:
        initial = self._accent or themes.current().accent
        chosen = colorchooser.askcolor(color=initial, parent=self.window,
                                       title="选择主色")[1]
        if not chosen:
            return
        self._accent = chosen.lower()
        self.accent_hint.configure(
            text=f"主题会立刻应用到挂件；当前自定义主色：{self._accent}")
        self.app.apply_theme(self._current_theme_key(), self._accent)

    def reset_accent(self) -> None:
        self._accent = ""
        self.accent_hint.configure(text="已恢复主题自带主色。")
        self.app.apply_theme(self._current_theme_key(), "")

    def refresh_pricing(self) -> None:
        self.app.refresh_pricing()
        self._poll_pricing()

    def _poll_pricing(self) -> None:
        """价格刷新跑在后台线程，这里在主线程里轮询状态。"""
        self.pricing_status_var.set(self.app.pricing_status_text())
        if self.app.pricing_busy:
            self.window.after(250, self._poll_pricing)

    def _set_status(self, text: str, color: str | None = None) -> None:
        self.status.configure(text=text, fg=color or MUTED)

    def test_connection(self) -> None:
        key = self.key_var.get().strip()
        self._set_status("正在测试…")
        self._test_result: tuple[str, object] | None = None

        def worker() -> None:
            try:
                self._test_result = ("ok", fetch_balance(key))
            except BalanceError as error:
                self._test_result = ("error", str(error))

        threading.Thread(target=worker, daemon=True).start()
        self._poll_test()

    def _poll_test(self) -> None:
        """在 Tk 主线程里轮询测试结果（tkinter 不允许跨线程调用）。"""
        if self._test_result is None:
            self.window.after(150, self._poll_test)
            return
        kind, payload = self._test_result
        self._test_result = None
        if kind == "ok":
            balance = payload
            self._set_status(
                f"连接成功，当前余额 {balance.symbol}{balance.total:,.2f}"
                f"（{balance.currency}）", GOOD)
        else:
            self._set_status(f"失败：{payload}", BAD)

    def save(self) -> None:
        config = self.app.config
        config.api_key = self.key_var.get().strip()
        try:
            config["refresh_minutes"] = max(1, int(self.interval_var.get()))
        except (tk.TclError, ValueError):
            config["refresh_minutes"] = 10
        try:
            config["low_balance_alert"] = max(0.0, float(self.threshold_var.get()))
        except ValueError:
            config["low_balance_alert"] = 5.0
        config["opacity"] = round(float(self.opacity_var.get()), 2)
        config["topmost"] = bool(self.topmost_var.get())
        config["pricing_auto_refresh"] = bool(self.pricing_auto_var.get())
        try:
            config["pricing_refresh_hours"] = max(1, min(168, int(self.pricing_hours_var.get())))
        except (tk.TclError, ValueError):
            config["pricing_refresh_hours"] = 24
        try:
            minutes = max(1, min(240, int(self.ledger_minutes_var.get())))
        except (tk.TclError, ValueError):
            minutes = 15
        self.app.set_ledger_window_minutes(minutes)
        config.save()
        self.app.set_autostart(self.autostart_var.get())
        self.app.window.apply_preferences()
        self.app.schedule_pricing_refresh()
        self.app.refresh_now()
        self.close()

    def close(self) -> None:
        self.app.window.root.attributes("-alpha", float(self.app.config["opacity"]))
        self.app.settings_dialog = None
        self.window.destroy()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="DeepSeek 余额桌面挂件")
    parser.add_argument("--set-key", metavar="KEY", help="把 API key 加密写入本机配置后退出")
    parser.add_argument("--interval", type=int, metavar="MIN", help="设置刷新间隔（分钟）")
    parser.add_argument("--threshold", type=float, metavar="CNY", help="设置低余额提醒阈值")
    parser.add_argument("--reset-position", action="store_true", help="恢复默认位置并退出")
    parser.add_argument("--print-config", action="store_true", help="打印配置路径后退出")
    parser.add_argument("--refresh-pricing", action="store_true",
                        help="抓取官方价格页、更新峰谷价格缓存后退出")
    parser.add_argument("--no-auto-pricing", action="store_true",
                        help="关闭峰谷价格的自动刷新")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    sys.excepthook = lambda exc_type, exc, tb: app_config.log_exception("unhandled", exc)

    if args.print_config:
        print(app_config.CONFIG_PATH)
        return 0

    config = Config()
    touched = False

    if args.set_key:
        config.api_key = args.set_key
        print(f"API key 已保存到 {app_config.CONFIG_PATH}")
        touched = True
    if args.interval:
        config["refresh_minutes"] = max(1, args.interval)
        touched = True
    if args.threshold is not None:
        config["low_balance_alert"] = max(0.0, args.threshold)
        touched = True
    if touched:
        config.save()
    if args.reset_position:
        config["position"] = None
        config.save()
        print("已恢复默认位置（右下角）")
        return 0

    if args.no_auto_pricing:
        config["pricing_auto_refresh"] = False
        config.save()
        print("已关闭峰谷价格自动刷新（仍可在设置里手动刷新）")
        return 0

    if args.refresh_pricing:
        try:
            table = price_source.fetch_price_table()
        except price_source.PriceSourceError as error:
            print(f"刷新失败：{error}")
            return 1
        pricing.save_cache(app_config.PRICING_CACHE_PATH, table)
        config["pricing_last_refresh"] = datetime.now().isoformat(timespec="seconds")
        config.save()
        print(f"已从官方文档更新峰谷价格（{len(table.models)} 个模型）")
        print(f"峰时（UTC）：{table.peak_windows_utc}，星期 {table.peak_weekdays_utc}，"
              f"谷时折扣 {table.off_peak_ratio}")
        for model, values in table.peak_prices.items():
            print(f"  {model}: 峰时 命中 {values[0]} / 未命中 {values[1]} / 输出 {values[2]}"
                  f"（美元每百万 tokens）")
        if table.fold:
            print(f"  {table.fold.model} 自 {table.fold.at:%Y-%m-%d %H:%M} 起按 "
                  f"{table.fold.into} 计价")
        return 0

    if not config.api_key:
        print("提示：尚未配置 API key，可在挂件右键菜单 → 设置 里填写。")

    enable_dpi_awareness()
    guard = single_instance_guard()
    if guard is None:
        print("挂件已在运行，本次启动忽略。")
        return 0

    app = Application(config)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
