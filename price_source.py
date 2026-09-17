"""从官方文档页抓取并解析峰谷价格。

官方没有提供价格查询 API，价格是写在文档页
https://api-docs.deepseek.com/quick_start/pricing 上的 HTML 表格，
所以这里用标准库 HTMLParser 把表格与脚注解析成 PriceTable。
解析失败时调用方会保留原有价格表，不影响挂件运行。
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from datetime import datetime
from html import unescape
from html.parser import HTMLParser

from pricing import CN_TZ, FoldRule, PRICE_SOURCE_URL, PriceTable

OFF_PEAK_LABELS = {"OFF-PEAK", "OFF PEAK", "OFFPEAK", "优惠时段", "谷时", "非高峰时段"}
PEAK_LABELS = {"PEAK", "标准时段", "高峰时段", "峰时"}
WEEKDAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}


class PriceSourceError(RuntimeError):
    """抓取或解析失败，消息可直接展示给用户。"""


class _PageParser(HTMLParser):
    """抽取页面里的所有表格（rows: list[list[str]]）和整页纯文本。"""

    SKIP_TAGS = {"script", "style", "noscript", "sup"}
    BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "table", "section"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self.text_parts: list[str] = []
        self._table_stack: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._skip_depth = 0

    # ---- 标签 ----------------------------------------------------------- #
    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "table":
            self._table_stack.append([])
        elif tag == "tr" and self._table_stack:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        if tag in self.BLOCK_TAGS:
            self.text_parts.append(" ")

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag in ("td", "th") and self._cell is not None:
            assert self._row is not None
            self._row.append(_squash(" ".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._table_stack:
                self._table_stack[-1].append(self._row)
            self._row = None
        elif tag == "table" and self._table_stack:
            table = self._table_stack.pop()
            if table:
                self.tables.append(table)
        if tag in self.BLOCK_TAGS:
            self.text_parts.append(" ")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._cell is not None:
            self._cell.append(data)
        self.text_parts.append(data)


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _parse_money(text: str) -> float | None:
    match = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _metric_key(label: str) -> str | None:
    upper = label.upper()
    if "CACHE HIT" in upper or ("缓存命中" in label and "未命中" not in label):
        return "hit"
    if "CACHE MISS" in upper or "未命中" in label:
        return "miss"
    if "OUTPUT" in upper or "输出" in label:
        return "output"
    return None


def _pick_pricing_table(tables):
    for rows in tables:
        flat = " ".join(" ".join(row) for row in rows).upper()
        if "PRICING" in flat and ("OFF-PEAK" in flat or "PEAK" in flat):
            return rows
    return None


def parse_price_page(html: str) -> PriceTable:
    """从价格页 HTML 中解析出 PriceTable；解析不出必要信息时抛 PriceSourceError。"""
    parser = _PageParser()
    parser.feed(html)
    full_text = _squash("".join(parser.text_parts))

    rows = _pick_pricing_table(parser.tables)
    if not rows:
        raise PriceSourceError("页面结构变了：没找到价格表")

    models: list[str] = []
    for row in rows:
        if row and row[0].strip().upper() == "MODEL":
            models = [cell for cell in row[1:] if cell.strip()]
            break
    if not models:
        raise PriceSourceError("页面结构变了：没找到模型名")

    # ---- 表格里的单价 ---------------------------------------------------- #
    collected: dict[str, dict[str, list[float]]] = {}
    current_metric: str | None = None
    for row in rows:
        if not row:
            continue
        period = None
        index = None
        for position, cell in enumerate(row):
            label = cell.strip().upper()
            if label in OFF_PEAK_LABELS:
                period, index = "off", position
                break
            if label in PEAK_LABELS:
                period, index = "peak", position
                break
        if period is None or index is None:
            continue
        label = " ".join(row[:index]).strip()
        if label:
            key = _metric_key(label)
            if key:
                current_metric = key
        if current_metric is None:
            continue
        values = [value for value in (_parse_money(cell) for cell in row[index + 1:])
                  if value is not None]
        if len(values) < len(models):
            continue
        collected.setdefault(current_metric, {})[period] = values[:len(models)]

    missing = [key for key in ("hit", "miss", "output")
               if "peak" not in collected.get(key, {})]
    if missing:
        raise PriceSourceError(f"页面结构变了：缺少 {', '.join(missing)} 的价格")

    peak_prices = {
        model: (
            collected["hit"]["peak"][i],
            collected["miss"]["peak"][i],
            collected["output"]["peak"][i],
        )
        for i, model in enumerate(models)
    }

    # ---- 谷时折扣比例（官方说明：谷时为峰时的一半，这里按实际数字算） ---- #
    ratios: list[float] = []
    for key in ("hit", "miss", "output"):
        off = collected[key].get("off")
        if not off:
            continue
        for i, peak_value in enumerate(collected[key]["peak"]):
            if peak_value:
                ratios.append(off[i] / peak_value)
    ratio = 0.5
    if ratios:
        ratios.sort()
        ratio = round(ratios[len(ratios) // 2], 4)

    # ---- 峰时时段与星期 -------------------------------------------------- #
    windows, weekdays = _parse_peak_schedule(full_text)

    # ---- 折价公告（V4 Pro 并入 Flash 之类） ----------------------------- #
    fold = _parse_fold_rule(full_text, models)

    notes = {key: value for key, value in _parse_notes(full_text).items()}

    return PriceTable(
        models=tuple(models),
        peak_prices=peak_prices,
        off_peak_ratio=ratio,
        peak_windows_utc=windows,
        peak_weekdays_utc=weekdays,
        fold=fold,
        source=PRICE_SOURCE_URL,
        fetched_at=datetime.now().isoformat(timespec="seconds"),
        notes=notes,
    )


def _parse_peak_schedule(text: str):
    """从脚注里解析峰时时段（UTC）与生效星期。"""
    match = re.search(r"(?:Peak hours are|峰时(?:段)?为|高峰时段为)(.{0,180}?)(?:\(|（|。|$)",
                      text, re.IGNORECASE)
    windows: list[tuple[int, int, int, int]] = []
    if match:
        for start_h, start_m, end_h, end_m in re.findall(
                r"(\d{1,2}):(\d{2})\s*[-–—~至]\s*(\d{1,2}):(\d{2})", match.group(1)):
            windows.append((int(start_h), int(start_m), int(end_h), int(end_m)))

    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    if match:
        fragment = match.group(1) + " " + text[match.end():match.end() + 60]
        span = re.search(r"([A-Za-z]+day)\s*(?:through|thru|to|-|–|—|~)\s*([A-Za-z]+day)",
                         fragment, re.IGNORECASE)
        if span and span.group(1).lower() in WEEKDAY_NAMES and span.group(2).lower() in WEEKDAY_NAMES:
            start = WEEKDAY_NAMES[span.group(1).lower()]
            end = WEEKDAY_NAMES[span.group(2).lower()]
            weekdays = tuple((start + offset) % 7
                             for offset in range((end - start) % 7 + 1))
        else:
            found = sorted({WEEKDAY_NAMES[name.lower()] for name in
                            re.findall(r"[A-Za-z]+day", fragment)
                            if name.lower() in WEEKDAY_NAMES})
            if found:
                weekdays = tuple(found)
        if "周一" in fragment and "周五" in fragment:
            weekdays = (0, 1, 2, 3, 4)

    if not windows:
        windows = [(1, 0, 4, 0), (6, 0, 10, 0)]
    return tuple(windows), weekdays


def _parse_fold_rule(text: str, models: list[str]) -> FoldRule | None:
    """解析"某模型从某时刻起改按另一模型计价"的公告。"""
    pattern = re.compile(
        r"(?:From\s+)?(\d{1,2}):(\d{2})\s*(?:Beijing Time|北京时间)?\s*(?:on\s+)?"
        r"([A-Za-z]+|\d{1,2})\s*月?\s*(\d{1,2})?,?\s*(\d{4})?\s*(?:起|，|,)?"
        r".{0,300}?billed at the\s+(.{0,60}?)price",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        hour, minute, month_token, day_token, year_token, target_text = match.groups()
        month = MONTH_NAMES.get(month_token.lower()) if month_token.isalpha() else None
        if month is None and month_token.isdigit():
            month = int(month_token)
        day = int(day_token) if day_token and day_token.isdigit() else None
        year = int(year_token) if year_token and year_token.isdigit() else None
        if not (month and day and year):
            continue
        source = None
        for candidate in re.findall(r"deepseek-[a-z0-9.\-]+", match.group(0), re.IGNORECASE):
            if candidate.lower() in models:
                source = candidate.lower()
                break
        if source is None:
            continue
        target_key = (target_text or "").lower()
        target = next((m for m in models
                       if m != source and target_key and
                       target_key.split()[0] in m.lower()), None)
        if target is None:
            target = next((m for m in models if "flash" in m), None)
        if target is None:
            continue
        return FoldRule(source, target, datetime(year, month, day, int(hour), int(minute),
                                                 tzinfo=CN_TZ))
    return None


def _parse_notes(text: str) -> dict[str, str]:
    notes: dict[str, str] = {}
    for index, note in enumerate(re.findall(r"\(\d\)\s*(.{0,300}?)(?=\(\d\)|$)", text)):
        notes[str(index + 1)] = _squash(note)[:300]
    return notes


def fetch_price_table(timeout: float = 15.0) -> PriceTable:
    """下载价格页并解析。失败抛 PriceSourceError。"""
    request = urllib.request.Request(
        PRICE_SOURCE_URL,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DeepSeekBalanceWidget/1.0",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            html = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as error:
        raise PriceSourceError(f"价格页返回 HTTP {error.code}") from error
    except Exception as error:
        raise PriceSourceError("无法访问官方价格页（网络不可用？）") from error

    table = parse_price_page(html)
    if not table.peak_prices:
        raise PriceSourceError("解析结果为空，已保留原价格")
    return table
