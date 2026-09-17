"""峰谷价格模型：内置默认值 + 可被在线刷新覆盖的 PriceTable。

默认数据来源：https://api-docs.deepseek.com/quick_start/pricing
在线刷新逻辑在 price_source.py；本文件只负责规则计算与本地缓存。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

CN_TZ = timezone(timedelta(hours=8), "CST")
UTC = timezone.utc

PRICE_SOURCE_URL = "https://api-docs.deepseek.com/quick_start/pricing"
CACHE_VERSION = 1

# --------------------------------------------------------------------------- #
# 内置默认值（在线刷新失败时用这套）
# --------------------------------------------------------------------------- #
# 峰时：UTC 周一至周五 01:00-04:00 与 06:00-10:00
# 换算成北京时间 = 周一至周五 09:00-12:00、14:00-18:00
DEFAULT_PEAK_WINDOWS_UTC: tuple[tuple[int, int, int, int], ...] = (
    (1, 0, 4, 0), (6, 0, 10, 0),
)
DEFAULT_PEAK_WEEKDAYS_UTC: tuple[int, ...] = (0, 1, 2, 3, 4)  # 周一 = 0

# 峰时单价，单位：美元 / 百万 tokens，依次为
# (输入·缓存命中, 输入·缓存未命中, 输出)
DEFAULT_PEAK_PRICES: dict[str, tuple[float, float, float]] = {
    "deepseek-flash": (0.006, 0.30, 1.20),
    "deepseek-v4-pro": (0.044, 1.32, 3.96),
}
DEFAULT_OFF_PEAK_RATIO = 0.5

# 公告：2026-09-14 12:00（北京时间）起，deepseek-v4-pro 统一路由到 Flash 并按 Flash 计价
DEFAULT_FOLD_AT = datetime(2026, 9, 14, 12, 0, tzinfo=CN_TZ)


@dataclass(frozen=True)
class Price:
    """一组单价，顺序为 (输入·缓存命中, 输入·缓存未命中, 输出)。"""

    cache_hit: float
    cache_miss: float
    output: float

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.cache_hit, self.cache_miss, self.output)


@dataclass
class FoldRule:
    """某个模型在某个时刻之后，按另一个模型的价格计费。"""

    model: str
    into: str
    at: datetime


@dataclass
class PriceTable:
    models: tuple[str, ...]
    peak_prices: dict[str, tuple[float, float, float]]
    off_peak_ratio: float
    peak_windows_utc: tuple[tuple[int, int, int, int], ...]
    peak_weekdays_utc: tuple[int, ...]
    fold: FoldRule | None = None
    source: str = "内置默认值"
    fetched_at: str | None = None
    notes: dict[str, str] = field(default_factory=dict)

    # ---- 峰谷判定 ------------------------------------------------------- #
    def is_peak(self, moment: datetime) -> bool:
        u = moment.astimezone(UTC)
        if u.weekday() not in self.peak_weekdays_utc:
            return False
        minutes = u.hour * 60 + u.minute
        return any(
            start_h * 60 + start_m <= minutes < end_h * 60 + end_m
            for start_h, start_m, end_h, end_m in self.peak_windows_utc
        )

    def next_switch(self, moment: datetime) -> tuple[datetime | None, bool]:
        """返回下一个峰谷切换时刻，以及切换后是否为峰时。"""
        cursor = moment.astimezone(UTC).replace(second=0, microsecond=0)
        current_is_peak = self.is_peak(cursor)
        for _ in range(60 * 24 * 8):
            cursor += timedelta(minutes=1)
            if self.is_peak(cursor) != current_is_peak:
                return cursor.astimezone(CN_TZ), not current_is_peak
        return None, current_is_peak

    def peak_segments(self, day: datetime, step_minutes: int = 5) -> list[tuple[int, int]]:
        """某北京日期内 00:00-24:00 的峰时区间，单位为"当日第几分钟"。"""
        start = datetime(day.year, day.month, day.day, tzinfo=CN_TZ)
        segments: list[tuple[int, int]] = []
        segment_start: int | None = None
        for minute in range(0, 24 * 60 + 1, step_minutes):
            peak = self.is_peak(start + timedelta(minutes=minute))
            if peak and segment_start is None:
                segment_start = minute
            elif not peak and segment_start is not None:
                segments.append((segment_start, minute))
                segment_start = None
        if segment_start is not None:
            segments.append((segment_start, 24 * 60))
        return segments

    # ---- 价格 ----------------------------------------------------------- #
    def billing_key(self, model: str, when: datetime) -> str:
        """某个模型在某时刻实际按哪个模型的单价计费。"""
        fold = self.fold
        if fold and model == fold.model and when.astimezone(CN_TZ) >= fold.at:
            if fold.into in self.peak_prices:
                return fold.into
        return model

    def is_folded(self, model: str, when: datetime) -> bool:
        return self.billing_key(model, when) != model

    def prices_for(self, model: str, when: datetime) -> dict[str, Price]:
        peak = self.peak_prices.get(self.billing_key(model, when))
        if peak is None:
            raise KeyError(f"未知模型：{model}")
        off_peak = tuple(round(value * self.off_peak_ratio, 6) for value in peak)
        return {"peak": Price(*peak), "off_peak": Price(*off_peak)}

    # ---- 序列化 --------------------------------------------------------- #
    def to_dict(self) -> dict:
        return {
            "version": CACHE_VERSION,
            "models": list(self.models),
            "peak_prices": {k: list(v) for k, v in self.peak_prices.items()},
            "off_peak_ratio": self.off_peak_ratio,
            "peak_windows_utc": [list(w) for w in self.peak_windows_utc],
            "peak_weekdays_utc": list(self.peak_weekdays_utc),
            "fold": ({"model": self.fold.model, "into": self.fold.into,
                      "at": self.fold.at.isoformat()} if self.fold else None),
            "source": self.source,
            "fetched_at": self.fetched_at,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PriceTable":
        fold_data = data.get("fold")
        fold = None
        if fold_data:
            fold = FoldRule(str(fold_data["model"]), str(fold_data["into"]),
                            datetime.fromisoformat(fold_data["at"]))
        return cls(
            models=tuple(str(m) for m in data["models"]),
            peak_prices={str(k): tuple(float(x) for x in v)
                         for k, v in data["peak_prices"].items()},
            off_peak_ratio=float(data.get("off_peak_ratio", DEFAULT_OFF_PEAK_RATIO)),
            peak_windows_utc=tuple(tuple(int(x) for x in w)
                                   for w in data["peak_windows_utc"]),
            peak_weekdays_utc=tuple(int(d) for d in data["peak_weekdays_utc"]),
            fold=fold,
            source=str(data.get("source", "本地缓存")),
            fetched_at=data.get("fetched_at"),
            notes={str(k): str(v) for k, v in (data.get("notes") or {}).items()},
        )


def default_table() -> PriceTable:
    return PriceTable(
        models=tuple(DEFAULT_PEAK_PRICES.keys()),
        peak_prices=dict(DEFAULT_PEAK_PRICES),
        off_peak_ratio=DEFAULT_OFF_PEAK_RATIO,
        peak_windows_utc=DEFAULT_PEAK_WINDOWS_UTC,
        peak_weekdays_utc=DEFAULT_PEAK_WEEKDAYS_UTC,
        fold=FoldRule("deepseek-v4-pro", "deepseek-flash", DEFAULT_FOLD_AT),
        source="内置默认值",
    )


# --------------------------------------------------------------------------- #
# 当前生效的价格表
# --------------------------------------------------------------------------- #
_ACTIVE: PriceTable = default_table()


def current() -> PriceTable:
    return _ACTIVE


def activate(table: PriceTable) -> None:
    global _ACTIVE
    _ACTIVE = table


def load_cache(path: Path) -> PriceTable | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if int(data.get("version", 0)) != CACHE_VERSION:
            return None
        return PriceTable.from_dict(data)
    except Exception:
        return None


def save_cache(path: Path, table: PriceTable) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(table.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8")


# --------------------------------------------------------------------------- #
# 便捷函数（界面直接调用，永远作用于当前生效的价格表）
# --------------------------------------------------------------------------- #
def to_cn(moment: datetime) -> datetime:
    return moment.astimezone(CN_TZ)


def is_peak(moment: datetime) -> bool:
    return _ACTIVE.is_peak(moment)


def next_switch(moment: datetime) -> tuple[datetime | None, bool]:
    return _ACTIVE.next_switch(moment)


def prices_for(model: str, when: datetime) -> dict[str, Price]:
    return _ACTIVE.prices_for(model, when)


def peak_segments(day: datetime, step_minutes: int = 5) -> list[tuple[int, int]]:
    return _ACTIVE.peak_segments(day, step_minutes)


def models() -> tuple[str, ...]:
    return _ACTIVE.models


def minute_of_day(moment: datetime) -> float:
    cn = to_cn(moment)
    return cn.hour * 60 + cn.minute + cn.second / 60


def format_duration(delta: timedelta) -> str:
    total = max(0, int(delta.total_seconds()))
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    if days:
        return f"{days}天 {hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def short_name(model: str) -> str:
    """deepseek-v4-pro -> V4 Pro（界面上省地方用）。"""
    text = model.replace("deepseek-", "").replace("-", " ").strip()
    return " ".join(word.upper() if word[:1].isdigit() else word.capitalize()
                    for word in text.split())
