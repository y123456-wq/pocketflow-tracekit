# -*- coding: utf-8 -*-
"""确定性降级（graceful degradation）：让 Agent 在外部依赖失效时仍然可用。

Agent 应用最常见的翻车方式不是逻辑错，而是**外部依赖不可用**：
LLM 超时、限流、返回了没法解析的东西、API Key 没配。
生产里这些都不该让整个流程崩掉，而应该：

1. 先重试有限次（带退避）；
2. 仍然失败就切到确定性的兜底策略；
3. 把"降级了"这件事作为 span 明确记录下来——**静默降级比崩溃更可怕**。

本模块提供两个 API：

* ``@resilient(...)`` 装饰器：给任意节点的执行函数套上重试 + 降级 + 记账；
* ``deterministic_route``：没有模型时也能走通的确定性分支。
"""

from __future__ import annotations

import functools
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .trace import Span, get_default_recorder

__all__ = ["resilient", "DegradationLog", "get_degradation_log", "reset_degradation_log"]


@dataclass
class DegradationEvent:
    """一次降级事件。"""

    node: str
    reason: str
    strategy: str
    attempts: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return {"node": self.node, "reason": self.reason, "strategy": self.strategy, "attempts": self.attempts}


class DegradationLog:
    """进程内的降级账本：方便断言"这条链路降级了几次、为什么"。"""

    def __init__(self) -> None:
        self._events: List[DegradationEvent] = []
        self._lock = threading.Lock()

    def add(self, event: DegradationEvent) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> List[DegradationEvent]:
        with self._lock:
            return list(self._events)

    def for_node(self, node: str) -> List[DegradationEvent]:
        return [e for e in self.events if e.node == node]

    def count(self) -> int:
        return len(self.events)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()


_LOG_LOCK = threading.Lock()
_LOG: Optional[DegradationLog] = None


def get_degradation_log() -> DegradationLog:
    global _LOG
    with _LOG_LOCK:
        if _LOG is None:
            _LOG = DegradationLog()
        return _LOG


def reset_degradation_log() -> None:
    global _LOG
    with _LOG_LOCK:
        _LOG = DegradationLog()


def resilient(
    fallback: Any = None,
    retries: int = 0,
    backoff: float = 0.0,
    node: Optional[str] = None,
    strategy: str = "static-fallback",
    on_error: Optional[Callable[[Exception, int], Any]] = None,
    record: bool = True,
) -> Callable:
    """把异常、重试、降级三件事收敛到一个装饰器里。

    Args:
        fallback: 兜底返回值。可以是常量，也可以是
            ``fn(exc, attempts)`` 形式的 callable（想返回带原因的报文时用）。
        retries: 额外重试次数（总共最多尝试 retries + 1 次）。
        backoff: 每次重试前的等待秒数，建议 > 0 以免打到限流的 API 上。
        node: 覆盖 Trace 里的节点名，默认用函数名。
        strategy: 降级策略标记，会写进 span metadata，便于事后分类统计。
        on_error: 每次失败都会调用（记录自定义埋点时用），返回值被忽略。
        record: 是否写入 Trace。关掉可用于纯业务逻辑，避免污染观测数据。

    Returns:
        装饰后的函数。原异常不会向上抛，取而代之的是兜底值。
        这是刻意的设计取舍：**节点失败不该让整个 Flow 停摆**，
        但每一次降级都会落到 Trace 和降级账本里，事后可审计。
    """

    def decorator(fn: Callable) -> Callable:
        name = node or getattr(fn, "__name__", "anonymous")

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            attempts = 0
            last_exc: Optional[Exception] = None
            for attempt in range(retries + 1):
                attempts = attempt + 1
                try:
                    return fn(*args, **kwargs)
                except Exception as exc:  # noqa: BLE001 - 有意兜住任意异常
                    last_exc = exc
                    if on_error is not None:
                        on_error(exc, attempts)
                    if attempt < retries and backoff > 0:
                        time.sleep(backoff)

            # 注意：except 结束后 `exc` 会被 Python 自动删除，这里必须用 last_exc
            value = fallback(last_exc, attempts) if callable(fallback) else fallback
            reason = f"{type(last_exc).__name__}: {last_exc}"

            if record:
                try:
                    recorder = get_default_recorder()
                    recorder.mark_degraded(name, reason, outputs=value)
                except Exception:  # pragma: no cover - 观测失败不能影响业务
                    pass
            get_degradation_log().add(
                DegradationEvent(node=name, reason=reason, strategy=strategy, attempts=attempts)
            )
            return value

        wrapper.__tracekit_node__ = name  # type: ignore[attr-defined]
        wrapper.__tracekit_fallback__ = fallback  # type: ignore[attr-defined]
        return wrapper

    return decorator


def span_from_degradation(event: DegradationEvent, run_id: str = "") -> Span:
    """把降级事件转成标准 Span，方便和正常 spans 放在同一条时间线里看。"""
    return Span(
        run_id=run_id,
        span_id="degraded",
        node=event.node,
        phase="fallback",
        status="degraded",
        metadata={"reason": event.reason, "strategy": event.strategy},
    )
