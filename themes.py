"""主题配色：几套预设 + 支持自定义主色。

挂件和设置面板、账本窗口都从这里取色。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

DEFAULT_KEY = "ocean"


def _rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16))


def mix(base: str, other: str, amount: float) -> str:
    """把 base 向 other 混合 amount（0=全 base，1=全 other）。"""
    amount = max(0.0, min(1.0, amount))
    channels = (round(a + (b - a) * amount) for a, b in zip(_rgb(base), _rgb(other)))
    return "#%02x%02x%02x" % tuple(channels)


def is_hex_color(value: str) -> bool:
    return bool(re.fullmatch(r"#[0-9a-fA-F]{6}", value or ""))


@dataclass(frozen=True)
class Theme:
    key: str
    name: str
    card: str          # 卡片底色
    border: str        # 卡片描边
    accent: str        # 主色（余额数字、输入中状态）
    bar_off: str       # 谷时段底色
    bar_peak: str      # 峰时段底色
    fg: str = "#e9edf5"
    dim: str = "#8b93a5"
    muted: str = "#5c6474"
    good: str = "#3ddc97"
    warn: str = "#f5b544"
    bad: str = "#ff6b6b"

    # ---- 派生色（改底色时自动跟着变） --------------------------------- #
    @property
    def card_hover(self) -> str:
        return mix(self.card, "#ffffff", 0.05)

    @property
    def badge_off(self) -> str:
        return mix(self.card, self.good, 0.16)

    @property
    def badge_peak(self) -> str:
        return mix(self.card, self.warn, 0.18)

    @property
    def divider(self) -> str:
        return mix(self.card, "#ffffff", 0.09)

    @property
    def slash(self) -> str:
        return mix(self.card, "#ffffff", 0.17)

    @property
    def input_bg(self) -> str:
        return mix(self.card, "#000000", 0.40)

    @property
    def button_bg(self) -> str:
        return mix(self.card, "#ffffff", 0.10)

    @property
    def button_active(self) -> str:
        return mix(self.card, "#ffffff", 0.18)

    # ---- 靠边收起后的底色（用来标识峰谷） ----------------------------- #
    @property
    def dock_idle(self) -> str:
        """谷时：绿色底。"""
        return mix(self.good, "#04140d", 0.45)

    @property
    def dock_peak(self) -> str:
        """峰时：橙色底。"""
        return "#d97706"

    @property
    def dock_error(self) -> str:
        return mix(self.bad, "#1a0505", 0.45)


PRESETS: dict[str, Theme] = {
    "ocean": Theme(
        key="ocean", name="深海蓝（默认）",
        card="#191d25", border="#2d3543", accent="#5aa2ff",
        bar_off="#1b3b32", bar_peak="#4a3a14",
    ),
    "graphite": Theme(
        key="graphite", name="石墨灰",
        card="#1c1c1e", border="#333338", accent="#cfd4dd",
        bar_off="#2a3832", bar_peak="#4a4030",
    ),
    "forest": Theme(
        key="forest", name="墨绿",
        card="#13201b", border="#25372f", accent="#45d6a0",
        bar_off="#17372c", bar_peak="#45401a",
    ),
    "violet": Theme(
        key="violet", name="紫罗兰",
        card="#1b1926", border="#322d45", accent="#a78bfa",
        bar_off="#1f3350", bar_peak="#493a12",
    ),
    "ember": Theme(
        key="ember", name="暖砂",
        card="#221d18", border="#3a3229", accent="#f0a35e",
        bar_off="#2f3a2a", bar_peak="#4d3a17",
    ),
    "ink": Theme(
        key="ink", name="纯黑",
        card="#0e0f11", border="#24262b", accent="#7fd1ff",
        bar_off="#16302a", bar_peak="#3d3011",
    ),
}

_CURRENT: Theme = PRESETS[DEFAULT_KEY]


def get(key: str) -> Theme:
    return PRESETS.get(key, PRESETS[DEFAULT_KEY])


def current() -> Theme:
    return _CURRENT


def set_current(theme: Theme) -> None:
    global _CURRENT
    _CURRENT = theme


def resolve(config) -> Theme:
    """按配置解析出主题：预设 + 可选的自定义主色覆盖。"""
    theme = get(str(config["theme"]))
    accent = str(config["accent_color"] or "").strip()
    if is_hex_color(accent):
        theme = replace(theme, accent=accent.lower())
    return theme


def choices() -> list[tuple[str, str]]:
    return [(key, theme.name) for key, theme in PRESETS.items()]
