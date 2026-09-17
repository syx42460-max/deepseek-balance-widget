"""账本：按余额差值记账，并把相邻的连续扣费合并成"一次会话"。

DeepSeek 没有提供按对话（request/response）的账单接口，
`/user/balance` 只能看到总余额，所以这里的做法是：

1. 每次刷新余额，和上一次的数值比较，差额就是这段时间的消耗；
2. 相邻两次扣费间隔小于「会话合并窗口」（默认 15 分钟）时，算作同一次会话，
   金额累加、时长延伸；超过窗口就收尾成一条独立记录；
3. 余额变多则记为充值。

所以账本记录的是"一段连续的用量区间"，可以近似看成一次对话；
想要精确切分，可以在账本窗口点「结算当前会话」手动断开。
"""

from __future__ import annotations

import csv
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

LEDGER_VERSION = 1
EPSILON = 0.0001          # 小于这个金额的波动直接忽略（四舍五入噪声）

PERIOD_LABELS = {"peak": "峰时", "off_peak": "谷时", "mixed": "混合"}
KIND_LABELS = {"auto": "自动", "manual": "手动", "topup": "充值"}


@dataclass
class Entry:
    id: str
    start: str
    end: str
    amount: float
    currency: str = "CNY"
    period: str = "off_peak"
    kind: str = "auto"
    note: str = ""

    @property
    def start_at(self) -> datetime:
        return datetime.fromisoformat(self.start)

    @property
    def end_at(self) -> datetime:
        return datetime.fromisoformat(self.end)

    @property
    def seconds(self) -> int:
        return max(0, int((self.end_at - self.start_at).total_seconds()))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Entry":
        return cls(
            id=str(data["id"]),
            start=str(data["start"]),
            end=str(data["end"]),
            amount=float(data["amount"]),
            currency=str(data.get("currency", "CNY")),
            period=str(data.get("period", "off_peak")),
            kind=str(data.get("kind", "auto")),
            note=str(data.get("note", "")),
        )


@dataclass
class OpenSession:
    """正在进行、还没收尾的会话。"""

    id: str
    start: str
    last_seen: str
    amount: float = 0.0
    currency: str = "CNY"
    period: str = "off_peak"
    events: int = 0
    note: str = ""

    @property
    def start_at(self) -> datetime:
        return datetime.fromisoformat(self.start)

    @property
    def last_seen_at(self) -> datetime:
        return datetime.fromisoformat(self.last_seen)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "OpenSession":
        return cls(
            id=str(data["id"]),
            start=str(data["start"]),
            last_seen=str(data.get("last_seen", data["start"])),
            amount=float(data.get("amount", 0.0)),
            currency=str(data.get("currency", "CNY")),
            period=str(data.get("period", "off_peak")),
            events=int(data.get("events", 0)),
            note=str(data.get("note", "")),
        )

    def to_entry(self, note: str = "") -> Entry:
        return Entry(
            id=self.id,
            start=self.start,
            end=self.last_seen,
            amount=round(self.amount, 6),
            currency=self.currency,
            period=self.period,
            kind="auto",
            note=note or self.note,
        )


class Ledger:
    def __init__(self, path: Path, session_minutes: int = 15) -> None:
        self.path = Path(path)
        self.session_minutes = max(1, int(session_minutes))
        self.entries: list[Entry] = []
        self.open: OpenSession | None = None
        self.last_total: float | None = None
        self.last_total_at: str | None = None
        self.revision = 0
        self.load()

    # ---- 存取 ----------------------------------------------------------- #
    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return
        self.entries = [Entry.from_dict(item) for item in data.get("entries", [])]
        open_data = data.get("open")
        self.open = OpenSession.from_dict(open_data) if open_data else None
        total = data.get("last_total")
        self.last_total = float(total) if total is not None else None
        self.last_total_at = data.get("last_total_at")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": LEDGER_VERSION,
            "entries": [entry.to_dict() for entry in self.entries],
            "open": self.open.to_dict() if self.open else None,
            "last_total": self.last_total,
            "last_total_at": self.last_total_at,
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    # ---- 记账 ----------------------------------------------------------- #
    @property
    def window(self) -> timedelta:
        return timedelta(minutes=self.session_minutes)

    def record_spend(self, amount: float, when: datetime, period: str,
                     note: str = "", currency: str = "CNY") -> str:
        """记一笔消耗，返回 'new' 或 'merged'。"""
        if amount <= EPSILON:
            return "ignored"
        stamp = when.isoformat(timespec="seconds")
        if (self.open is not None
                and when - self.open.last_seen_at <= self.window
                and self.open.currency == currency):
            self.open.amount = round(self.open.amount + amount, 6)
            self.open.last_seen = stamp
            self.open.events += 1
            if period != self.open.period:
                self.open.period = "mixed"
            if note:
                self.open.note = note
            result = "merged"
        else:
            self.close_open(when)
            self.open = OpenSession(id=uuid.uuid4().hex[:12], start=stamp, last_seen=stamp,
                                    amount=round(amount, 6), period=period, events=1,
                                    currency=currency, note=note)
            result = "new"
        self.revision += 1
        self.save()
        return result

    def record_topup(self, amount: float, when: datetime) -> Entry | None:
        if amount <= EPSILON:
            return None
        self.close_open(when)
        stamp = when.isoformat(timespec="seconds")
        entry = Entry(id=uuid.uuid4().hex[:12], start=stamp, end=stamp,
                      amount=round(amount, 6), kind="topup", note="余额增加")
        self.entries.append(entry)
        self.revision += 1
        self.save()
        return entry

    def add_manual(self, amount: float, when: datetime, note: str = "") -> Entry:
        stamp = when.isoformat(timespec="seconds")
        entry = Entry(id=uuid.uuid4().hex[:12], start=stamp, end=stamp,
                      amount=round(abs(amount), 6), kind="manual",
                      period="mixed", note=note or "手动记录")
        self.entries.append(entry)
        self.revision += 1
        self.save()
        return entry

    def close_open(self, when: datetime, note: str = "") -> Entry | None:
        if self.open is None or self.open.amount <= EPSILON:
            self.open = None
            return None
        entry = self.open.to_entry(note)
        self.entries.append(entry)
        self.open = None
        self.revision += 1
        self.save()
        return entry

    def settle(self, when: datetime | None = None) -> Entry | None:
        return self.close_open(when or datetime.now(), note="手动结算")

    def close_stale(self, now: datetime) -> bool:
        """会话空闲超过窗口就自动收尾。返回是否有收尾发生。"""
        if self.open is not None and now - self.open.last_seen_at > self.window:
            self.close_open(now)
            return True
        return False

    def note_total(self, total: float, when: datetime) -> None:
        self.last_total = float(total)
        self.last_total_at = when.isoformat(timespec="seconds")

    # ---- 统计 ----------------------------------------------------------- #
    def summary(self, now: datetime | None = None) -> dict:
        now = now or datetime.now()
        today = now.date()
        week_start = today - timedelta(days=today.weekday())
        result = {
            "today": 0.0, "week": 0.0, "month": 0.0, "total": 0.0,
            "count": 0, "topped_up": 0.0,
        }
        for entry in self.entries:
            if entry.kind == "topup":
                result["topped_up"] += entry.amount
                continue
            start = entry.start_at
            result["total"] += entry.amount
            result["count"] += 1
            if start.date() == today:
                result["today"] += entry.amount
            if start.date() >= week_start:
                result["week"] += entry.amount
            if (start.year, start.month) == (now.year, now.month):
                result["month"] += entry.amount
        if self.open is not None:
            result["total"] += self.open.amount
            if self.open.start_at.date() == today:
                result["today"] += self.open.amount
            if self.open.start_at.date() >= week_start:
                result["week"] += self.open.amount
            if (self.open.start_at.year, self.open.start_at.month) == (now.year, now.month):
                result["month"] += self.open.amount
        return result

    def recent(self, limit: int = 300) -> list[dict]:
        """给界面用的列表，最新的在最前面，进行中的会话排在最上面。"""
        rows: list[dict] = []
        if self.open is not None and self.open.amount > EPSILON:
            rows.append({
                "id": self.open.id,
                "start": self.open.start,
                "seconds": int((self.open.last_seen_at - self.open.start_at).total_seconds()),
                "amount": self.open.amount,
                "period": self.open.period,
                "kind": "auto",
                "note": "进行中",
                "open": True,
            })
        for entry in reversed(self.entries[-limit:]):
            rows.append({
                "id": entry.id,
                "start": entry.start,
                "seconds": entry.seconds,
                "amount": entry.amount,
                "period": entry.period,
                "kind": entry.kind,
                "note": entry.note,
                "open": False,
            })
        return rows[:limit]

    # ---- 导出 / 清空 ----------------------------------------------------- #
    def export_csv(self, path: Path) -> int:
        path = Path(path)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["开始时间", "结束时间", "时长(秒)", "金额", "币种",
                             "时段", "类型", "备注"])
            for entry in self.entries:
                writer.writerow([
                    entry.start.replace("T", " "), entry.end.replace("T", " "),
                    entry.seconds, f"{entry.amount:.6f}".rstrip("0").rstrip("."),
                    entry.currency, PERIOD_LABELS.get(entry.period, entry.period),
                    KIND_LABELS.get(entry.kind, entry.kind), entry.note,
                ])
        return len(self.entries)

    def backup(self) -> Path | None:
        if not self.path.exists():
            return None
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"ledger.backup-{stamp}.json")
        target.write_bytes(self.path.read_bytes())
        return target

    def clear(self) -> int:
        removed = len(self.entries)
        self.entries = []
        self.open = None
        self.revision += 1
        self.save()
        return removed
