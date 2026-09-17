"""挂件界面：无边框圆角卡片 + 自绘峰谷时间轴（纯 tkinter，无第三方依赖）。"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from tkinter import font as tkfont
import math
import time

import pricing
import themes
from themes import mix
from deepseek_api import Balance

# 设计尺寸（96 DPI 下的像素）；运行时会按屏幕 DPI 整体缩放
WIDTH, HEIGHT = 320, 340
PAD = 16

TRANSPARENT_KEY = "#ff00fe"   # 该颜色在 Windows 上会被抠成全透明
# 下面的颜色由 apply_theme() 按当前主题写入（模块级变量，绘制时实时读取）
CARD = "#191d25"
CARD_HOVER = "#1d222b"
BORDER = "#2d3543"
FG = "#e9edf5"
DIM = "#8b93a5"
MUTED = "#5c6474"
GREEN = "#3ddc97"
AMBER = "#f5b544"
RED = "#ff6b6b"
BLUE = "#5aa2ff"
BAR_BG = "#1b3b32"
BAR_PEAK = "#4a3a14"
BADGE_OFF_FILL = "#153729"
BADGE_PEAK_FILL = "#3a2f12"
DIVIDER = "#262d3a"
SLASH = "#39414f"

CURRENT_THEME: themes.Theme = themes.get(themes.DEFAULT_KEY)


def apply_theme(theme: themes.Theme) -> None:
    """把主题写进模块级颜色变量；调用方随后需要重建画布。"""
    global CURRENT_THEME, CARD, CARD_HOVER, BORDER, FG, DIM, MUTED
    global GREEN, AMBER, RED, BLUE, BAR_BG, BAR_PEAK
    global BADGE_OFF_FILL, BADGE_PEAK_FILL, DIVIDER, SLASH
    CURRENT_THEME = theme
    CARD = theme.card
    CARD_HOVER = theme.card_hover
    BORDER = theme.border
    FG = theme.fg
    DIM = theme.dim
    MUTED = theme.muted
    GREEN = theme.good
    AMBER = theme.warn
    RED = theme.bad
    BLUE = theme.accent
    BAR_BG = theme.bar_off
    BAR_PEAK = theme.bar_peak
    BADGE_OFF_FILL = theme.badge_off
    BADGE_PEAK_FILL = theme.badge_peak
    DIVIDER = theme.divider
    SLASH = theme.slash

METRICS = ("输入·命中", "输入·未命中", "输出")

# 折叠峰谷区之后的高度；靠边收起的竖条尺寸（都是 96 DPI 设计值）
# 折叠峰谷区之后的高度；靠边小条长宽比 1.5:1（都是 96 DPI 设计值）
COLLAPSED_HEIGHT = 150
DOCK_WIDTH, DOCK_HEIGHT = 69, 46
HOVER_EXPAND_DELAY = 1.0        # 悬停多久后展开
HOVER_COLLAPSE_DELAY = 0.35     # 鼠标挪开后多久收回
TEAR_OFF_DISTANCE = 24          # 把小条拖离边缘多少像素算"撕下来"


@dataclass
class ViewState:
    now: datetime
    balance: Balance | None = None
    error: str | None = None
    pricing_error: str | None = None
    loading: bool = False
    low_balance_alert: float = 5.0


def round_rect(canvas: tk.Canvas, x1, y1, x2, y2, radius, fill, tags=()):
    """圆角矩形：用单个多边形画。

    以前是"两个矩形 + 四个扇形"拼的，图元之间会留下 1px 的缝；挂件用的是
    颜色键透明（transparentcolor），缝里会露出桌面，看起来就是卡片边上有一圈
    细白线。换成一个多边形之后就没有接缝了。
    """
    radius = max(1, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    steps = 7
    points: list[float] = []
    for center_x, center_y, start in (
        (x1 + radius, y1 + radius, 180.0),   # 左上
        (x2 - radius, y1 + radius, 270.0),   # 右上
        (x2 - radius, y2 - radius, 0.0),     # 右下
        (x1 + radius, y2 - radius, 90.0),    # 左下
    ):
        for step in range(steps + 1):
            angle = math.radians(start + 90.0 * step / steps)
            points.append(center_x + radius * math.cos(angle))
            points.append(center_y + radius * math.sin(angle))
    return canvas.create_polygon(points, fill=fill, outline=fill, width=1,
                                 joinstyle="round", tags=tags)


def compact(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text or "0"


def place_beside(parent: tk.Misc, child: tk.Toplevel, offset_y: int = 40,
                 gap: int = 16) -> None:
    """把弹窗放在挂件旁边；放不下就换到另一侧，最后再夹进屏幕。"""
    import app_config

    child.update_idletasks()
    child.update()
    width = max(child.winfo_width(), child.winfo_reqwidth())
    height = max(child.winfo_height(), child.winfo_reqheight())
    left, top, screen_w, screen_h = app_config.work_area()
    right, bottom = left + screen_w, top + screen_h

    for value in (parent.winfo_x() - width - gap,
                  parent.winfo_x() + parent.winfo_width() + gap):
        if left + 8 <= value and value + width <= right - 8:
            x = value
            break
    else:
        x = parent.winfo_x() - width - gap

    for value in (parent.winfo_y() + offset_y, parent.winfo_y() - height - 12):
        if top + 8 <= value and value + height <= bottom - 8:
            y = value
            break
    else:
        y = parent.winfo_y() + offset_y

    x = max(left + 8, min(x, right - width - 8))
    y = max(top + 8, min(y, bottom - height - 8))
    child.geometry(f"+{x}+{y}")


class WidgetWindow:
    def __init__(self, config, actions: dict):
        self.config = config
        self.actions = actions
        self.ids: dict[str, int] = {}
        self.value_ids: dict[tuple[str, int], tuple[int, int, int]] = {}
        self._drag_origin: tuple[int, int] | None = None
        self._drag_start_pos: tuple[int, int] | None = None
        self._segments_key: tuple | None = None
        self._hovering = False
        self._last_state: ViewState | None = None

        apply_theme(themes.resolve(config))
        self.docked = bool(config["docked"])
        self.dock_side = str(config["dock_side"] or "right")
        self.prices_collapsed = bool(config["prices_collapsed"])
        self.auto_expanded = False        # 靠边后因悬停临时展开
        self._hover_since: float | None = None
        self._leave_since: float | None = None
        self._hover_armed = False         # 指针先离开过，悬停才生效
        self._pending_click: str | None = None   # 单击余额刷新（等双击判定）

        self.root = tk.Tk()
        self.root.title("DeepSeek 余额")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", bool(config["topmost"]))
        self.apply_alpha()
        try:
            self.root.attributes("-transparentcolor", TRANSPARENT_KEY)
        except tk.TclError:
            pass
        self.root.configure(bg=TRANSPARENT_KEY)

        # 按屏幕 DPI 整体缩放：布局在设计坐标系里画，最后统一 scale
        self.scale = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        self.design_height = self._design_height()
        self.width = int(round((DOCK_WIDTH if self.docked else WIDTH) * self.scale))
        self.height = int(round(self.design_height * self.scale))

        x, y = self._initial_position()
        # 自己记一份窗口位置：Tk 的 winfo_x/y 要等 ConfigureNotify 处理完才刷新，
        # 拖动松手那一刻读到的还是旧值，边缘吸附会判断错。
        self._window_pos: tuple[int, int] = (x, y)
        self.root.geometry(f"{self.width}x{self.height}+{x}+{y}")

        self.canvas = tk.Canvas(
            self.root, width=self.width, height=self.height,
            bg=TRANSPARENT_KEY, highlightthickness=0, bd=0,
        )
        self.canvas.pack()

        self._pick_fonts()
        self._build()
        self._apply_scale()
        self._bind_events()
        self.root.after(150, self._watch_pointer)

    def _design_height(self) -> int:
        if self._is_tab():
            return DOCK_HEIGHT
        return COLLAPSED_HEIGHT if self.prices_collapsed else HEIGHT

    def _is_tab(self) -> bool:
        """当前是不是"贴边小条"形态。"""
        return self.docked and not self.auto_expanded

    def is_compact(self) -> bool:
        """是否处于收起状态（靠边小条，或折叠了峰谷价格）。"""
        return self._is_tab() or self.prices_collapsed

    def _apply_scale(self) -> None:
        if abs(self.scale - 1.0) > 0.01:
            self.canvas.scale("all", 0, 0, self.scale, self.scale)
        self._bar = tuple(value * self.scale for value in self._design_bar)

    def rebuild(self) -> None:
        """换了主题色之后重画整块卡片（颜色是画上去的，改色要重画）。"""
        self.canvas.delete("all")
        self.ids.clear()
        self.value_ids.clear()
        self._segments_key = None
        self._build()
        self._apply_scale()
        if self._last_state is not None:
            self.render(self._last_state)

    def set_theme(self, theme: themes.Theme) -> None:
        apply_theme(theme)
        self.rebuild()

    # ------------------------------------------------------------------ #
    # 折叠 / 靠边收起
    # ------------------------------------------------------------------ #
    def _resize(self) -> None:
        """按当前形态重新计算窗口尺寸并重画。"""
        self.design_height = self._design_height()
        self.width = int(round((DOCK_WIDTH if self._is_tab() else WIDTH) * self.scale))
        self.height = int(round(self.design_height * self.scale))
        self.canvas.configure(width=self.width, height=self.height)
        self.rebuild()

    def _clamped_position(self, x: float, y: float) -> tuple[int, int]:
        import app_config

        left, top, screen_w, screen_h = app_config.work_area()
        x = min(max(int(x), left - 20), left + screen_w - 60)
        y = min(max(int(y), top), top + screen_h - 40)
        return x, y

    def set_prices_collapsed(self, collapsed: bool) -> None:
        collapsed = bool(collapsed)
        if collapsed == self.prices_collapsed:
            return
        # 顶边保持不动：峰谷价格往下展开、往上收起，余额数字不会跳位置
        top_edge = self._current_pos()[1]
        self.prices_collapsed = collapsed
        self._resize()
        x, y = self._fit_onscreen(self._current_pos()[0], top_edge)
        self._apply_geometry(x, y)
        self.config["prices_collapsed"] = collapsed
        self.config.save()

    def _fit_onscreen(self, x: float, y: float) -> tuple[int, int]:
        """把窗口整个约束在工作区内（展开时不能顶到屏幕外）。"""
        import app_config

        left, top, screen_w, screen_h = app_config.work_area()
        x = min(max(int(x), left), max(left, left + screen_w - self.width))
        y = min(max(int(y), top), max(top, top + screen_h - self.height))
        return x, y

    def toggle_prices(self) -> None:
        self.set_prices_collapsed(not self.prices_collapsed)

    def dock(self, side: str | None = None, remember_position: bool = True) -> None:
        """收起成靠边的小条；side 为空时自动选更近的一侧。"""
        import app_config

        if self.docked and not self.auto_expanded:
            return
        left, top, screen_w, screen_h = app_config.work_area()
        if side not in ("left", "right"):
            if self.docked:
                side = self.dock_side
            else:
                center = self._current_pos()[0] + self.width / 2
                side = "left" if center < left + screen_w / 2 else "right"
        if not self.docked:
            # 记住浮窗位置，取消靠边时还原到这里
            if remember_position:
                self.config["position"] = list(self._current_pos())
            self.config["dock_offset"] = int(self._current_pos()[1] + self.height / 2)
        self.config["dock_side"] = side
        self.dock_side = side
        self.docked = True
        self.auto_expanded = False
        self._hover_armed = False          # 刚收起时指针还在上面，先不展开
        self._hover_since = None
        self.config["docked"] = True
        self._resize()
        x, y = self._dock_position(left, top, screen_w, screen_h)
        self._apply_geometry(x, y)
        self.config.save()

    def undock(self) -> None:
        self._undock_to(None)

    def _undock_to(self, position: tuple[int, int] | None) -> None:
        """取消靠边；position 为空时回到"靠边之前"的位置。"""
        import app_config

        if not self.docked:
            return
        self.docked = False
        self.auto_expanded = False
        self._hover_armed = False
        self._hover_since = None
        self._leave_since = None
        self.config["docked"] = False
        self._resize()
        left, top, screen_w, screen_h = app_config.work_area()
        if position is not None:
            x, y = int(position[0]), int(position[1])
        else:
            saved = self.config["position"]
            x, y = (int(saved[0]), int(saved[1])) if (
                isinstance(saved, (list, tuple)) and len(saved) == 2) else (None, None)
        if x is None:
            margin = int(round(int(self.config["snap_margin"] or 24) * self.scale))
            x, y = app_config.default_position(self.width, self.height, margin)
        x, y = self._clamped_position(x, y)
        self._apply_geometry(x, y)
        self.config["position"] = [x, y]
        self.config.save()

    def _expand_from_dock(self) -> None:
        """靠边状态下临时展开（悬停触发），仍贴着原来那一侧。"""
        if not self.docked or self.auto_expanded:
            return
        self.auto_expanded = True
        self._leave_since = None
        self._resize()
        x, y = self._docked_panel_position()
        self._apply_geometry(x, y)

    def _collapse_to_dock(self) -> None:
        """把临时展开收回成小条。"""
        if not (self.docked and self.auto_expanded):
            return
        self.auto_expanded = False
        self._hover_since = None
        self._leave_since = None
        # 收回的触发条件就是"指针已经离开"，所以这里是"已武装"状态
        self._hover_armed = True
        self._resize()
        x, y = self._dock_position()
        self._apply_geometry(x, y)

    def _docked_panel_position(self) -> tuple[int, int]:
        """靠边展开时的位置：贴同一侧，纵向对齐小条中心。"""
        import app_config

        left, top, screen_w, screen_h = app_config.work_area()
        center = int(self.config["dock_offset"] or (top + screen_h // 2))
        x = left if self.dock_side == "left" else left + screen_w - self.width
        y = min(max(center - self.height // 2, top), top + screen_h - self.height)
        return x, y

    # ------------------------------------------------------------------ #
    # 鼠标悬停：停 1 秒展开，挪开自动收回
    # ------------------------------------------------------------------ #
    def _watch_pointer(self) -> None:
        try:
            if self.root.winfo_ismapped() and self.root.grab_current() is None:
                self._handle_hover(*self.root.winfo_pointerxy())
        except tk.TclError:
            return
        except Exception as error:      # 轮询绝不能因为一次异常就停掉
            import app_config

            app_config.log_exception("hover", error)
        self.root.after(150, self._watch_pointer)

    def _handle_hover(self, pointer_x: int, pointer_y: int) -> None:
        if self._drag_origin is not None:        # 拖动过程中不做悬停判断
            return
        left, top = self._current_pos()
        inside = (left <= pointer_x < left + self.width
                  and top <= pointer_y < top + self.height)
        now = time.monotonic()

        if self._is_tab():
            if not inside:
                self._hover_since = None
                self._hover_armed = True         # 指针离开过之后，悬停才算数
                return
            if not self._hover_armed:
                self._hover_since = None
                return
            self._hover_since = self._hover_since or now
            if now - self._hover_since >= HOVER_EXPAND_DELAY:
                self._hover_since = None
                self._expand_from_dock()
            return

        if self.docked and self.auto_expanded:
            if inside:
                self._leave_since = None
                return
            self._leave_since = self._leave_since or now
            if now - self._leave_since >= HOVER_COLLAPSE_DELAY:
                self._collapse_to_dock()

    def _balance_hit(self, x: float, y: float) -> bool:
        """点到余额数字那一块（双击折叠热区）。"""
        return (self.u(6) <= x <= self.u(WIDTH - 6)
                and self.u(34) <= y <= self.u(104))

    def apply_alpha(self) -> None:
        alpha = float(self.config["opacity"])
        self.root.attributes("-alpha", 1.0 if alpha > 0.99 else alpha)

    def px(self, points: float) -> int:
        """把设计用点数换成按 DPI 缩放后的像素字号（Tk 中用负数表示像素）。"""
        return -max(1, int(round(points * 4 / 3 * self.scale)))

    def u(self, value: float) -> float:
        return value * self.scale

    def _current_pos(self) -> tuple[int, int]:
        return self._window_pos or (self.root.winfo_x(), self.root.winfo_y())

    def _move_to(self, x: float, y: float) -> None:
        """只挪位置（尺寸不变）。"""
        position = (int(x), int(y))
        self._window_pos = position
        self.root.geometry(f"+{position[0]}+{position[1]}")

    def _apply_geometry(self, x: float, y: float) -> None:
        """尺寸和位置一起设。凡是调过 _resize() 之后都必须用它，
        否则画布已经变大、窗口还停在旧尺寸，看起来就像"显示不全"。"""
        position = (int(x), int(y))
        self._window_pos = position
        self.root.geometry(f"{self.width}x{self.height}+{position[0]}+{position[1]}")

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #
    def _pick_fonts(self) -> None:
        families = set(tkfont.families(self.root))
        ui = next((f for f in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI")
                   if f in families), "TkDefaultFont")
        mono = next((f for f in ("Cascadia Mono", "Consolas", "Courier New")
                     if f in families), "TkFixedFont")
        sym = next((f for f in ("Segoe UI Symbol", "Segoe UI", ui) if f in families), ui)
        self.f_title = (ui, self.px(10), "bold")
        self.f_btn = (sym, self.px(10))
        self.f_balance = (ui, self.px(27), "bold")
        self.f_small = (ui, self.px(9))
        self.f_tiny = (ui, self.px(7.5))
        self.f_section = (ui, self.px(9.5), "bold")
        self.f_badge = (ui, self.px(8.5), "bold")
        self.f_mono = (mono, self.px(8.5))
        self.f_mono_bold = (mono, self.px(8.5), "bold")
        self.f_mono_mid = (mono, self.px(7.5), "bold")
        self.f_mono_small = (mono, self.px(6.5), "bold")
        self.f_note = (ui, self.px(7))

    def model_font(self, model: str):
        """模型名太长时自动换小一号，避免两列标题挤在一起。"""
        if len(model) <= 14:
            return self.f_mono_bold
        if len(model) <= 17:
            return self.f_mono_mid
        return self.f_mono_small

    def _initial_position(self) -> tuple[int, int]:
        import app_config

        left, top, screen_w, screen_h = app_config.work_area()
        if self.docked:
            return self._dock_position(left, top, screen_w, screen_h)

        position = self.config["position"]
        margin = int(round(int(self.config["snap_margin"] or 24) * self.scale))
        if isinstance(position, (list, tuple)) and len(position) == 2:
            x = min(max(int(position[0]), left - 40),
                    left + screen_w - self.width + 40)
            y = min(max(int(position[1]), top - 10),
                    top + screen_h - self.height + 40)
            return x, y

        return app_config.default_position(self.width, self.height, margin)

    def _dock_position(self, left=None, top=None, screen_w=None, screen_h=None):
        """靠边竖条的坐标：横向贴边，纵向沿用上次的位置。"""
        if left is None:
            import app_config

            left, top, screen_w, screen_h = app_config.work_area()
        x = left if self.dock_side == "left" else left + screen_w - self.width
        offset = self.config["dock_offset"]
        try:
            y = int(offset) - self.height // 2 if offset else top + (screen_h - self.height) // 2
        except (TypeError, ValueError):
            y = top + (screen_h - self.height) // 2
        y = min(max(y, top), top + screen_h - self.height)
        return x, y

    # ------------------------------------------------------------------ #
    # 绘制骨架
    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        cv = self.canvas
        if self._is_tab():
            self._build_docked(cv)
            return

        height = self.design_height
        round_rect(cv, 0, 0, WIDTH, height, 15, BORDER, tags=("card_border",))
        round_rect(cv, 1, 1, WIDTH - 1, height - 1, 14, CARD, tags=("card",))

        self.ids["title"] = cv.create_text(
            PAD + 2, 12, text="DeepSeek 余额", anchor="nw", fill=FG, font=self.f_title,
            tags=("title",))

        self.ids["btn_close"] = cv.create_text(
            WIDTH - PAD, 20, text="✕", anchor="e", fill=DIM, font=self.f_btn,
            tags=("btn", "btn_close"))
        self.ids["btn_settings"] = cv.create_text(
            WIDTH - PAD - 24, 20, text="⚙", anchor="e", fill=DIM, font=self.f_btn,
            tags=("btn", "btn_settings"))
        self.ids["btn_ledger"] = cv.create_text(
            WIDTH - PAD - 48, 20, text="☰", anchor="e", fill=DIM, font=self.f_btn,
            tags=("btn", "btn_ledger"))
        self.ids["balance"] = cv.create_text(
            PAD, 40, text="--", anchor="nw", fill=FG, font=self.f_balance, tags=("balance",))
        self.ids["collapse_hint"] = cv.create_text(
            WIDTH - PAD, 60, text="▴", anchor="e", fill=DIM, font=self.f_small,
            tags=("collapse_hint",))
        self.ids["breakdown"] = cv.create_text(
            PAD + 2, 84, text="", anchor="nw", fill=DIM, font=self.f_small,
            tags=("breakdown",))

        self.ids["status_dot"] = cv.create_oval(
            PAD + 2, 109, PAD + 8, 115, fill=MUTED, outline="", tags=("status_dot",))
        self.ids["status"] = cv.create_text(
            PAD + 14, 112, text="正在获取…", anchor="w", fill=DIM, font=self.f_small,
            tags=("status",))
        self.ids["updated"] = cv.create_text(
            WIDTH - PAD, 112, text="", anchor="e", fill=MUTED, font=self.f_tiny,
            tags=("updated",))

        cv.create_line(PAD, 128, WIDTH - PAD, 128, fill=DIVIDER,
                       width=max(1, round(self.scale)))

        self._design_bar = (PAD, 168, WIDTH - PAD, 180)   # 展开时才真正用到
        self._columns = 0
        if self.prices_collapsed:
            # 折叠状态：留一行摘要（当前时段 + 倒计时）
            self.ids["summary"] = cv.create_text(
                PAD, 134, text="", anchor="nw", fill=GREEN, font=self.f_note,
                tags=("summary",))
            return

        self._build_prices(cv)

    def _build_docked(self, cv) -> None:
        """靠边收起后的小条：横排余额，底色用橙/绿标识峰谷。"""
        width, height = DOCK_WIDTH, DOCK_HEIGHT
        self._design_bar = (0, 0, 0, 0)      # 竖条形态没有时间轴
        self._columns = 0
        background = themes.current().dock_idle
        round_rect(cv, 0, 0, width, height, 13, mix(background, "#ffffff", 0.20),
                   tags=("card_border",))
        round_rect(cv, 1, 1, width - 1, height - 1, 12, background, tags=("card",))
        self.ids["dock_balance"] = cv.create_text(
            width / 2, height / 2 + 1, text="--", anchor="center", fill=FG,
            font=(self.f_balance[0], self.px(11), "bold"),
            tags=("dock_balance",))

    def _build_prices(self, cv) -> None:
        self.ids["section"] = cv.create_text(
            PAD, 140, text="峰谷价格", anchor="nw", fill=FG, font=self.f_section,
            tags=("section",))

        badge_w, badge_h = 92, 20
        badge_x2 = WIDTH - PAD
        badge_x1 = badge_x2 - badge_w
        round_rect(cv, badge_x1, 135, badge_x2, 135 + badge_h, badge_h / 2,
                   BADGE_OFF_FILL, tags=("badge_bg",))
        self.ids["badge_text"] = cv.create_text(
            (badge_x1 + badge_x2) / 2, 135 + badge_h / 2 + 1, text="谷时",
            fill=GREEN, font=self.f_badge, tags=("badge_text",))

        bar_x1, bar_x2 = PAD, WIDTH - PAD
        bar_y1, bar_y2 = 168, 180
        self._design_bar = (bar_x1, bar_y1, bar_x2, bar_y2)
        round_rect(cv, bar_x1, bar_y1, bar_x2, bar_y2, 6, BAR_BG, tags=("bar_bg",))

        self.ids["marker"] = cv.create_line(
            bar_x1, bar_y1 - 3, bar_x1, bar_y2 + 3, fill=FG,
            width=max(1, round(2 * self.scale)), tags=("marker",))
        self.ids["marker_dot"] = cv.create_oval(
            bar_x1 - 2, bar_y1 - 6, bar_x1 + 2, bar_y1 - 2, fill=FG, outline="",
            tags=("marker",))

        for frac, label, anchor in (
            (0.0, "00:00", "w"), (0.25, "06", "n"), (0.5, "12", "n"),
            (0.75, "18", "n"), (1.0, "24:00", "e"),
        ):
            x = bar_x1 + (bar_x2 - bar_x1) * frac
            cv.create_text(x, 184, text=label, anchor=("nw" if anchor == "w" else
                                                       ("ne" if anchor == "e" else "n")),
                           fill=MUTED, font=self.f_tiny)

        self.ids["period"] = cv.create_text(
            PAD, 208, text="", anchor="w", fill=FG, font=self.f_small, tags=("period",))
        self.ids["countdown"] = cv.create_text(
            WIDTH - PAD, 208, text="", anchor="e", fill=DIM, font=self.f_small,
            tags=("countdown",))

        cv.create_text(PAD, 230, text="USD/1M tok", anchor="w", fill=MUTED, font=self.f_note)

        self._col_centers = (139, 249)
        self._columns = len(self._col_centers)
        for index, model in enumerate(pricing.models()[:self._columns]):
            center = self._col_centers[index]
            self.ids[f"model_{index}"] = cv.create_text(
                center, 230, text=model, anchor="center", fill=FG,
                font=self.model_font(model), tags=(f"model_{index}",))

        for row, label in enumerate(METRICS):
            y = 252 + row * 18
            cv.create_text(PAD, y, text=label, anchor="w", fill=DIM, font=self.f_small)
            for index in range(self._columns):
                center = self._col_centers[index]
                left = cv.create_text(center - 6, y, text="", anchor="e",
                                      fill=FG, font=self.f_mono)
                slash = cv.create_text(center, y, text="/", anchor="center",
                                       fill=SLASH, font=self.f_mono)
                right = cv.create_text(center + 6, y, text="", anchor="w",
                                       fill=DIM, font=self.f_mono)
                self.value_ids[(index, row)] = (left, slash, right)

        self.ids["legend"] = cv.create_text(
            PAD, 306, text="", anchor="nw", fill=MUTED, font=self.f_note, tags=("legend",))
        self.ids["note"] = cv.create_text(
            PAD, 322, text="", anchor="nw", fill=MUTED, font=self.f_note, tags=("note",))

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def _bind_events(self) -> None:
        cv = self.canvas
        for tag, action in (
            ("btn_settings", self.actions["settings"]),
            ("btn_ledger", self.actions["ledger"]),
            ("btn_close", self.actions["quit"]),
        ):
            cv.tag_bind(tag, "<Button-1>", lambda _e, fn=action: (fn(), "break")[1])
            cv.tag_bind(tag, "<Enter>", lambda _e, t=tag: self._set_btn_fill(t, FG))
            cv.tag_bind(tag, "<Leave>", lambda _e, t=tag: self._set_btn_fill(t, DIM))

        cv.bind("<Button-1>", self._drag_start)
        cv.bind("<B1-Motion>", self._drag_move)
        cv.bind("<ButtonRelease-1>", self._drag_end)
        cv.bind("<Double-Button-1>", self._on_double_click)
        cv.bind("<Button-3>", self._show_menu)
        cv.bind("<Enter>", lambda _e: self._set_card_hover(True))
        cv.bind("<Leave>", lambda _e: self._set_card_hover(False))
        self.root.bind("<Configure>", self._on_configure)

        self._build_menu()

    def _on_configure(self, event) -> None:
        """窗口被外部移动/缩放时同步位置缓存。"""
        if event.widget is self.root:
            self._window_pos = (event.x, event.y)

    def _set_btn_fill(self, tag: str, color: str) -> None:
        self.canvas.itemconfigure(tag, fill=color)

    def _set_card_hover(self, hovering: bool) -> None:
        if hovering == self._hovering:
            return
        self._hovering = hovering
        self._apply_card_fill()

    def _apply_card_fill(self) -> None:
        """卡片底色：小条形态用峰谷色，普通形态用主题色。"""
        if self._is_tab():
            background, _ = self._dock_colors()
            fill = mix(background, "#ffffff", 0.10) if self._hovering else background
        else:
            fill = CARD_HOVER if self._hovering else CARD
        self.canvas.itemconfigure("card", fill=fill)
        self.canvas.itemconfigure("card_border", fill=mix(fill, "#ffffff", 0.20))

    def _dock_colors(self) -> tuple[str, str]:
        """小条底色与文字色：谷时绿 / 峰时橙 / 出错红。"""
        theme = themes.current()
        state = self._last_state
        if state is not None and state.error and state.balance is None:
            return theme.dock_error, FG
        moment = state.now if state is not None else datetime.now()
        return (theme.dock_peak if pricing.is_peak(moment) else theme.dock_idle), FG

    def _drag_start(self, event) -> None:
        current = self._current_pos()
        self._drag_origin = (event.x_root - current[0], event.y_root - current[1])
        self._drag_start_pos = current

    def _drag_move(self, event) -> None:
        if not self._drag_origin:
            return
        x = event.x_root - self._drag_origin[0]
        y = event.y_root - self._drag_origin[1]
        if self._is_tab():
            # 还贴着边：只允许上下滑动；拖离边缘就"撕下来"展开成浮窗
            edge_x = self._dock_position()[0]
            if abs(x - edge_x) <= self.u(TEAR_OFF_DISTANCE):
                x = edge_x
            else:
                self._tear_off(event)
                return
        if self._drag_start_pos:
            self._drag_moved = (abs(x - self._drag_start_pos[0])
                                + abs(y - self._drag_start_pos[1]))
        self._move_to(x, y)

    def _tear_off(self, event) -> None:
        """把小条拖离边缘：立刻展开成普通浮窗并继续跟随光标。"""
        self.docked = False
        self.auto_expanded = False
        self._hover_armed = False
        self._hover_since = None
        self.config["docked"] = False
        self._resize()
        self._drag_origin = (self.width // 2, self.u(22))   # 光标落在标题栏位置
        # 必须连尺寸一起设，否则窗口还停在小条大小，展开后的卡片显示不全
        self._apply_geometry(event.x_root - self._drag_origin[0],
                             event.y_root - self._drag_origin[1])

    def _drag_end(self, event) -> None:
        if not self._drag_origin:
            return
        self._drag_origin = None
        moved = getattr(self, "_drag_moved", 0)
        self._drag_moved = 0
        self._drag_start_pos = None
        if moved <= 4:
            # 没怎么移动 = 单击：点余额区域就是刷新
            if not self.docked and self._balance_hit(event.x, event.y):
                self._schedule_click_refresh()
            return
        if self.docked:
            # 靠边状态下拖动：贴着边就只上下挪，拖离边缘则取消靠边
            side = self._snap_to_edges(side_threshold=int(round(42 * self.scale)))
            if side:
                self.config["dock_offset"] = int(self._current_pos()[1]
                                                 + self.height / 2)
                self.config["dock_side"] = side
                self.dock_side = side
                if self.auto_expanded:
                    self._collapse_to_dock()
                else:
                    self._settle_dock()
            else:
                self._undock_to(self._current_pos())
            return
        side = self._snap_to_edges(side_threshold=int(round(42 * self.scale)))
        if side:
            # 拖到桌面左右边缘 -> 自动收起成小条
            self.dock(side, remember_position=False)
            return
        self.config["position"] = list(self._current_pos())
        self.config.save()

    def _settle_dock(self) -> None:
        """贴边拖动结束后：横向压回边缘，纵向别跑出屏幕。"""
        import app_config

        left, top, screen_w, screen_h = app_config.work_area()
        x, y = self._dock_position(left, top, screen_w, screen_h)
        self._apply_geometry(x, y)
        self.config["dock_offset"] = int(y + self.height / 2)
        self.config.save()

    def _on_double_click(self, event):
        if self._pending_click is not None:      # 双击就别刷新了
            try:
                self.root.after_cancel(self._pending_click)
            except (tk.TclError, ValueError):
                pass
            self._pending_click = None
        if self.docked:
            self.actions["expand_dock"]()
            return "break"
        if self._balance_hit(event.x, event.y):
            self.toggle_prices()
        else:
            self.actions["refresh"]()
        return "break"

    def _schedule_click_refresh(self) -> None:
        """延迟一点再刷新，给双击留出判定的时间（双击余额是折叠峰谷区）。"""
        if self._pending_click is not None:
            try:
                self.root.after_cancel(self._pending_click)
            except (tk.TclError, ValueError):
                pass
        self._pending_click = self.root.after(240, self._click_refresh)

    def _click_refresh(self) -> None:
        self._pending_click = None
        self.actions["refresh"]()

    def _snap_to_edges(self, threshold: int = 28, side_threshold: int | None = None):
        """靠近边缘就吸附；返回吸附到的那一侧（left/right），没有则 None。"""
        import app_config

        side_threshold = threshold if side_threshold is None else side_threshold
        left, top, screen_w, screen_h = app_config.work_area()
        x, y = self._current_pos()
        margin = int(round(int(self.config["snap_margin"] or 24) * self.scale))
        side = None
        # 用带符号的距离：窗口被拖到超出屏幕边缘时距离是负数，
        # 这种情况也要算"贴边"（之前用 abs() 会导致拖过头反而收不起来）
        if x - left <= side_threshold:
            x = left + margin
            side = "left"
        elif (left + screen_w) - (x + self.width) <= side_threshold:
            x = left + screen_w - self.width - margin
            side = "right"
        if y - top <= threshold:
            y = top + margin
        elif (top + screen_h) - (y + self.height) <= threshold:
            y = top + screen_h - self.height - margin
        x = min(max(x, left - 20), left + screen_w - 60)
        y = min(max(y, top), top + screen_h - 40)
        self._move_to(x, y)
        return side

    def reset_position(self) -> None:
        import app_config

        margin = int(round(int(self.config["snap_margin"] or 24) * self.scale))
        x, y = app_config.default_position(self.width, self.height, margin)
        self.root.geometry(f"+{x}+{y}")
        self._window_pos = (x, y)
        self.config["position"] = [x, y]
        self.config.save()

    # ------------------------------------------------------------------ #
    # 右键菜单
    # ------------------------------------------------------------------ #
    def _build_menu(self) -> None:
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="立即刷新", command=self.actions["refresh"])
        self.menu.add_command(label="刷新峰谷价格", command=self.actions["refresh_pricing"])
        self.menu.add_command(label="账本…", command=self.actions["ledger"])
        self.menu.add_command(label="设置…", command=self.actions["settings"])
        self.menu.add_separator()
        self.menu.add_command(label="收起峰谷价格", command=self.toggle_prices)
        self._idx_prices = self.menu.index("end")
        self.menu.add_command(label="靠边收起", command=lambda: self.actions["dock"]())
        self._idx_dock = self.menu.index("end")
        self.menu.add_separator()
        self.var_topmost = tk.BooleanVar(value=bool(self.config["topmost"]))
        self.var_autostart = tk.BooleanVar(value=bool(self.config["autostart"]))
        self.menu.add_checkbutton(label="窗口置顶", variable=self.var_topmost,
                                  command=lambda: self.actions["topmost"](self.var_topmost.get()))
        self.menu.add_checkbutton(label="开机自启", variable=self.var_autostart,
                                  command=lambda: self.actions["autostart"](self.var_autostart.get()))
        self.menu.add_command(label="回到右下角", command=self.reset_position)
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self.actions["quit"])

    def _show_menu(self, event) -> None:
        self.var_topmost.set(bool(self.config["topmost"]))
        self.var_autostart.set(bool(self.config["autostart"]))
        self.menu.entryconfigure(
            self._idx_prices,
            label="展开峰谷价格" if self.prices_collapsed else "收起峰谷价格")
        self.menu.entryconfigure(
            self._idx_dock,
            label="取消靠边收起" if self.docked else "靠边收起")
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def _draw_segments(self, segments: list[tuple[int, int]]) -> None:
        key = tuple(segments)
        if key == self._segments_key:
            return
        self._segments_key = key
        self.canvas.delete("seg")
        x1, y1, x2, y2 = self._bar
        span = x2 - x1
        for start, end in segments:
            sx = x1 + span * start / 1440
            ex = x1 + span * end / 1440
            round_rect(self.canvas, sx, y1, ex, y2, self.u(6), BAR_PEAK, tags=("seg",))
        self.canvas.tag_raise("marker")

    def render(self, state: ViewState) -> None:
        self._last_state = state
        if self._is_tab():
            self._render_docked(state)
            return
        cv = self.canvas
        now = state.now
        peak_now = pricing.is_peak(now)

        # ---- 余额 ------------------------------------------------------ #
        balance = state.balance
        if balance is None:
            cv.itemconfigure("balance", text="--", fill=MUTED)
            cv.itemconfigure("breakdown", text="等待首次获取…")
        else:
            low = balance.total <= state.low_balance_alert
            color = FG if not low else (RED if balance.total < state.low_balance_alert * 0.4
                                        else AMBER)
            cv.itemconfigure("balance", text=f"{balance.symbol}{balance.total:,.2f}", fill=color)
            cv.itemconfigure(
                "breakdown",
                text=f"充值 {balance.symbol}{balance.topped_up:,.2f}    "
                     f"赠送 {balance.symbol}{balance.granted:,.2f}",
            )

        # ---- 状态 ------------------------------------------------------ #
        if state.error:
            dot = AMBER if balance else RED
            text = "离线 · 显示上次数据" if balance else "获取失败"
        elif state.loading:
            dot, text = BLUE, "正在更新…"
        elif balance and not balance.is_available:
            dot, text = RED, "账户不可用"
        else:
            dot, text = GREEN, "账户可用"
        cv.itemconfigure("status_dot", fill=dot)
        cv.itemconfigure("status", text=text)
        stamp = (balance.fetched_at if balance else now).strftime("%H:%M:%S")
        cv.itemconfigure("updated", text=f"{stamp} 更新")

        active = pricing.current()
        discount = compact(active.off_peak_ratio * 10)
        switch_at, becomes_peak = pricing.next_switch(now)
        countdown = ""
        if switch_at is not None:
            delta = switch_at - pricing.to_cn(now)
            countdown = f"{'距峰时' if becomes_peak else '距谷时'} {pricing.format_duration(delta)}"

        # ---- 折叠状态：只留一行摘要 ------------------------------------ #
        if self.prices_collapsed:
            label = ("峰时 标准价" if peak_now else f"谷时 {discount} 折")
            cv.itemconfigure("collapse_hint", text="▾")
            cv.itemconfigure("summary",
                             text=f"{label} · {countdown}" if countdown else label,
                             fill=AMBER if peak_now else GREEN)
            return
        cv.itemconfigure("collapse_hint", text="▴")

        # ---- 峰谷时间轴 ------------------------------------------------ #
        self._draw_segments(pricing.peak_segments(now))
        bar_x1, bar_y1, bar_x2, bar_y2 = self._bar
        marker_x = bar_x1 + (bar_x2 - bar_x1) * pricing.minute_of_day(now) / 1440
        cv.coords("marker", marker_x, bar_y1 - 3, marker_x, bar_y2 + 3)
        cv.coords("marker_dot", marker_x - 2, bar_y1 - 6, marker_x + 2, bar_y1 - 2)

        if peak_now:
            cv.itemconfigure("badge_bg", fill=BADGE_PEAK_FILL)
            cv.itemconfigure("badge_text", text="峰时 · 标准价", fill=AMBER)
            cv.itemconfigure("period", text="当前处于峰时，按标准价计费", fill=AMBER)
        else:
            cv.itemconfigure("badge_bg", fill=BADGE_OFF_FILL)
            cv.itemconfigure("badge_text", text=f"谷时 · {discount} 折", fill=GREEN)
            cv.itemconfigure("period", text=f"当前处于谷时，按 {discount} 折计费", fill=GREEN)
        cv.itemconfigure("legend",
                         text=f"每列：左 谷时 / 右 峰时 · 谷时为峰时 {discount} 折")

        cv.itemconfigure("countdown", text=countdown)

        # ---- 价格表 ---------------------------------------------------- #
        display_models = active.models[:self._columns]
        for index in range(self._columns):
            if index >= len(display_models):
                cv.itemconfigure(f"model_{index}", text="")
                for row in range(len(METRICS)):
                    for item in self.value_ids[(index, row)]:
                        cv.itemconfigure(item, text="")
                continue
            model = display_models[index]
            cv.itemconfigure(f"model_{index}", text=model, font=self.model_font(model))
            table = active.prices_for(model, now)
            for row in range(len(METRICS)):
                off = table["off_peak"].as_tuple()[row]
                peak = table["peak"].as_tuple()[row]
                left, slash, right = self.value_ids[(index, row)]
                cv.itemconfigure(left, text=compact(off),
                                 fill=(MUTED if peak_now else GREEN))
                cv.itemconfigure(right, text=compact(peak),
                                 fill=(AMBER if peak_now else MUTED))

        # ---- 脚注 / 错误提示 ------------------------------------------- #
        note = ""
        note_color = MUTED
        if state.error:
            note = state.error if len(state.error) <= 46 else state.error[:45] + "…"
            note_color = RED if not balance else AMBER
        elif state.pricing_error:
            message = f"峰谷价格未更新：{state.pricing_error}"
            note = message if len(message) <= 46 else message[:45] + "…"
            note_color = AMBER
        elif active.fold and active.fold.model in active.models:
            fold = active.fold
            if pricing.to_cn(now) < fold.at:
                note = (f"注：{pricing.short_name(fold.model)} 自 {fold.at.month}/"
                        f"{fold.at.day} {fold.at:%H:%M} 起按 "
                        f"{pricing.short_name(fold.into)} 计价")
            else:
                note = (f"注：{pricing.short_name(fold.model)} 当前已按 "
                        f"{pricing.short_name(fold.into)} 单价计费")
        elif active.fetched_at:
            note = f"价格同步自官方文档 · {active.fetched_at[:10]}"
        else:
            note = "价格使用内置默认值"
        cv.itemconfigure("note", text=note, fill=note_color)
        cv.tag_raise("btn")

    def _render_docked(self, state: ViewState) -> None:
        """小条形态：横排余额，底色标识当前峰谷。"""
        cv = self.canvas
        balance = state.balance
        text = "--" if balance is None else f"{balance.symbol}{balance.total:,.2f}"
        cv.itemconfigure("dock_balance", text=text, fill=FG)
        self._apply_card_fill()

    # ------------------------------------------------------------------ #
    def apply_preferences(self) -> None:
        self.root.attributes("-topmost", bool(self.config["topmost"]))
        self.root.attributes("-alpha", float(self.config["opacity"]))

    def destroy(self) -> None:
        try:
            self.root.destroy()
        except tk.TclError:
            pass
