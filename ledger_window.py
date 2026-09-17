"""账本窗口：统计 + 明细列表 + 结算/手动记账/导出/清空。"""

from __future__ import annotations

import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import themes
from ledger import KIND_LABELS, PERIOD_LABELS, Ledger
from ui import place_beside

COLUMNS = (
    ("start", "开始时间", 132, "w"),
    ("duration", "时长", 66, "e"),
    ("amount", "金额", 76, "e"),
    ("period", "时段", 54, "center"),
    ("kind", "类型", 54, "center"),
    ("note", "备注", 150, "w"),
)


def _duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}分{sec:02d}秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}时{minutes:02d}分"


class LedgerWindow:
    def __init__(self, app) -> None:
        self.app = app
        self.ledger: Ledger = app.ledger
        self.theme = themes.current()
        t = self.theme
        self._revision = -1

        self.window = tk.Toplevel(app.window.root)
        self.window.title("DeepSeek 账本")
        self.window.configure(bg=t.card)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.attributes("-topmost", bool(app.config["topmost"]))

        self.scale = max(1.0, self.window.winfo_fpixels("1i") / 96.0)

        header = tk.Frame(self.window, bg=t.card)
        header.pack(fill="x", padx=14, pady=(12, 4))

        self.summary_vars: dict[str, tk.StringVar] = {}
        for key, label in (("today", "今日"), ("week", "本周"),
                           ("month", "本月"), ("total", "累计")):
            box = tk.Frame(header, bg=t.card)
            box.pack(side="left", expand=True, fill="x")
            tk.Label(box, text=label, bg=t.card, fg=t.dim,
                     font=("Microsoft YaHei UI", 9)).pack(anchor="w")
            var = tk.StringVar(value="¥0.00")
            self.summary_vars[key] = var
            tk.Label(box, textvariable=var, bg=t.card, fg=t.fg,
                     font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")

        self.subtitle = tk.Label(self.window, bg=t.card, fg=t.muted, anchor="w",
                                 font=("Microsoft YaHei UI", 8))
        self.subtitle.pack(fill="x", padx=14)

        toolbar = tk.Frame(self.window, bg=t.card)
        toolbar.pack(fill="x", padx=10, pady=8)
        for text, command in (
            ("结算当前会话", self.settle),
            ("手动记一笔", self.manual),
            ("导出 CSV", self.export),
            ("清空", self.clear),
        ):
            tk.Button(toolbar, text=text, command=command, bg=t.button_bg, fg=t.fg,
                      activebackground=t.button_active, activeforeground=t.fg,
                      relief="flat", padx=10, pady=4)\
                .pack(side="left", padx=3)

        table = tk.Frame(self.window, bg=t.card)
        table.pack(fill="both", expand=True, padx=14, pady=(0, 6))

        style = ttk.Style(self.window)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Ledger.Treeview", background=t.input_bg,
                        fieldbackground=t.input_bg, foreground=t.fg,
                        bordercolor=t.border, rowheight=int(23 * self.scale))
        style.configure("Ledger.Treeview.Heading", background=t.button_bg,
                        foreground=t.dim, relief="flat",
                        font=("Microsoft YaHei UI", 9))
        style.map("Ledger.Treeview.Heading", background=[("active", t.button_active)])
        style.map("Ledger.Treeview", background=[("selected", t.button_bg)],
                  foreground=[("selected", t.fg)])
        style.configure("Ledger.Vertical.TScrollbar", background=t.button_bg,
                        troughcolor=t.card, arrowcolor=t.dim, bordercolor=t.card)

        self.tree = ttk.Treeview(table, columns=[c[0] for c in COLUMNS], show="headings",
                                 style="Ledger.Treeview", height=12)
        for key, title, width, anchor in COLUMNS:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=int(width * self.scale), anchor=anchor,
                             stretch=(key == "note"))
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview,
                               style="Ledger.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.tag_configure("open", foreground=t.accent)
        self.tree.tag_configure("topup", foreground=t.good)
        self.tree.tag_configure("odd", background=t.card_hover)

        tk.Label(self.window, bg=t.card, fg=t.muted, anchor="w", justify="left",
                 wraplength=int(560 * self.scale), font=("Microsoft YaHei UI", 8),
                 text=("余额接口只提供总额，没有按对话的账单：这里按每次刷新的余额差值记账，"
                       f"相邻扣费间隔小于 {self.ledger.session_minutes} 分钟视为同一次会话，"
                       f"可在设置里调整合并窗口。数据存于 {self.ledger.path}"))\
            .pack(fill="x", padx=14, pady=(0, 10))

        self.window.minsize(int(560 * self.scale), int(300 * self.scale))
        self.refresh(force=True)
        place_beside(self.app.window.root, self.window)
        self._tick()

    # ------------------------------------------------------------------ #
    def refresh(self, force: bool = False) -> None:
        summary = self.ledger.summary()
        currency = "¥"
        for key, var in self.summary_vars.items():
            var.set(f"{currency}{summary[key]:,.2f}")
        self.subtitle.configure(
            text=(f"共 {summary['count']} 条消耗记录 · 充值累计 "
                  f"{currency}{summary['topped_up']:,.2f} · 合并窗口 "
                  f"{self.ledger.session_minutes} 分钟"))
        if not force and self.ledger.revision == self._revision:
            return
        self._revision = self.ledger.revision
        self.tree.delete(*self.tree.get_children())
        for index, row in enumerate(self.ledger.recent()):
            start = datetime.fromisoformat(row["start"])
            tags = []
            if row["open"]:
                tags.append("open")
            elif row["kind"] == "topup":
                tags.append("topup")
            elif index % 2:
                tags.append("odd")
            self.tree.insert("", "end", values=(
                start.strftime("%m-%d %H:%M:%S"),
                _duration(row["seconds"]),
                f"{row['amount']:.4f}".rstrip("0").rstrip("."),
                PERIOD_LABELS.get(row["period"], row["period"]),
                KIND_LABELS.get(row["kind"], row["kind"]),
                row["note"],
            ), tags=tuple(tags))

    def _tick(self) -> None:
        try:
            self.refresh()
        except tk.TclError:
            return
        self.window.after(2000, self._tick)

    # ------------------------------------------------------------------ #
    def settle(self) -> None:
        entry = self.ledger.settle()
        if entry is None:
            messagebox.showinfo("账本", "当前没有进行中的会话。", parent=self.window)
        self.refresh(force=True)

    def manual(self) -> None:
        amount = _ManualDialog(self.window, self.theme, self.scale).result
        if amount is None:
            return
        value, note = amount
        self.ledger.add_manual(value, datetime.now(), note)
        self.refresh(force=True)

    def export(self) -> None:
        if not self.ledger.entries:
            messagebox.showinfo("账本", "还没有可导出的记录。", parent=self.window)
            return
        path = filedialog.asksaveasfilename(
            parent=self.window, title="导出账本",
            defaultextension=".csv", initialfile="deepseek-ledger.csv",
            filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")])
        if not path:
            return
        count = self.ledger.export_csv(Path(path))
        messagebox.showinfo("账本", f"已导出 {count} 条记录到\n{path}", parent=self.window)

    def clear(self) -> None:
        if not messagebox.askyesno(
                "清空账本",
                "确定清空所有记录吗？\n（会先自动备份一份 JSON 到同目录）",
                parent=self.window):
            return
        backup = self.ledger.backup()
        removed = self.ledger.clear()
        messagebox.showinfo(
            "账本",
            f"已清空 {removed} 条记录。" + (f"\n备份：{backup}" if backup else ""),
            parent=self.window)
        self.refresh(force=True)

    def close(self) -> None:
        self.app.ledger_window = None
        self.window.destroy()


class _ManualDialog:
    """手动记一笔的小弹窗（同步返回）。"""

    def __init__(self, parent, theme, scale: float) -> None:
        self.result: tuple[float, str] | None = None
        window = tk.Toplevel(parent)
        self.window = window
        window.title("手动记一笔")
        window.configure(bg=theme.card)
        window.resizable(False, False)
        window.transient(parent)

        tk.Label(window, text="金额（元）", bg=theme.card, fg=theme.dim)\
            .grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))
        self.amount_var = tk.StringVar()
        entry = tk.Entry(window, textvariable=self.amount_var, width=16, bg=theme.input_bg,
                         fg=theme.fg, insertbackground=theme.fg, relief="flat")
        entry.grid(row=0, column=1, sticky="we", padx=12, pady=(12, 4))

        tk.Label(window, text="备注", bg=theme.card, fg=theme.dim)\
            .grid(row=1, column=0, sticky="w", padx=12, pady=4)
        self.note_var = tk.StringVar(value="手动记录")
        tk.Entry(window, textvariable=self.note_var, width=24, bg=theme.input_bg,
                 fg=theme.fg, insertbackground=theme.fg, relief="flat")\
            .grid(row=1, column=1, sticky="we", padx=12, pady=4)

        buttons = tk.Frame(window, bg=theme.card)
        buttons.grid(row=2, column=0, columnspan=2, sticky="e", padx=12, pady=12)
        tk.Button(buttons, text="取消", command=self._cancel, bg=theme.button_bg,
                  fg=theme.fg, relief="flat", padx=12, pady=3).pack(side="left", padx=4)
        tk.Button(buttons, text="保存", command=self._save, bg=theme.button_bg,
                  fg=theme.fg, relief="flat", padx=12, pady=3).pack(side="left", padx=4)

        entry.focus_set()
        window.bind("<Return>", lambda _e: self._save())
        window.bind("<Escape>", lambda _e: self._cancel())
        place_beside(parent, window, offset_y=40)
        window.grab_set()
        window.wait_window()

    def _save(self) -> None:
        try:
            value = float(self.amount_var.get().strip())
        except ValueError:
            self.amount_var.set("")
            self.window.bell()
            return
        if value <= 0:
            self.window.bell()
            return
        self.result = (value, self.note_var.get().strip())
        self.window.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.window.destroy()
