"""DeepSeek 余额接口封装（只依赖标准库）。"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime

BALANCE_URL = "https://api.deepseek.com/user/balance"


class BalanceError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class Balance:
    is_available: bool
    currency: str
    total: float
    granted: float
    topped_up: float
    fetched_at: datetime

    @property
    def symbol(self) -> str:
        return {"CNY": "¥", "USD": "$"}.get(self.currency, self.currency + " ")


def fetch_balance(api_key: str, timeout: float = 10.0, retries: int = 1) -> Balance:
    """拉取余额。失败抛 BalanceError，消息可以直接展示给用户。"""
    if not api_key or not api_key.strip():
        raise BalanceError("未配置 API key")

    request = urllib.request.Request(
        BALANCE_URL,
        headers={
            "Authorization": f"Bearer {api_key.strip()}",
            "Accept": "application/json",
            "User-Agent": "DeepSeekBalanceWidget/1.0",
        },
    )

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return _parse(payload)
        except urllib.error.HTTPError as error:
            raise BalanceError(_http_message(error), error.code) from error
        except urllib.error.URLError as error:
            last_error = error
            if attempt < retries:
                time.sleep(1.5)
                continue
            raise BalanceError("网络不可用或连接超时") from error
        except (TimeoutError, OSError) as error:
            last_error = error
            if attempt < retries:
                time.sleep(1.5)
                continue
            raise BalanceError("网络不可用或连接超时") from error
        except (ValueError, KeyError, TypeError) as error:
            raise BalanceError(f"接口返回格式异常：{error}") from error

    raise BalanceError(str(last_error) if last_error else "未知错误")


def _http_message(error: urllib.error.HTTPError) -> str:
    mapping = {
        400: "请求参数有误",
        401: "API key 无效或已失效",
        402: "账户余额不足",
        403: "该 key 无权访问此接口",
        429: "请求过于频繁，已被限流",
        500: "服务端错误",
        503: "服务暂时不可用",
    }
    base = mapping.get(error.code, f"HTTP {error.code}")
    try:
        detail = json.loads(error.read().decode("utf-8"))
        message = detail.get("error", {}).get("message")
        if message:
            base = f"{base}（{message}）"
    except Exception:  # 详情解析失败不影响主流程
        pass
    return base


def _parse(payload: dict) -> Balance:
    infos = payload.get("balance_infos") or []
    if not infos:
        raise BalanceError("接口未返回余额信息")
    info = infos[0]
    return Balance(
        is_available=bool(payload.get("is_available")),
        currency=str(info.get("currency", "CNY")),
        total=float(info.get("total_balance", 0)),
        granted=float(info.get("granted_balance", 0)),
        topped_up=float(info.get("topped_up_balance", 0)),
        fetched_at=datetime.now(),
    )
