# -*- coding: utf-8 -*-
"""适配器：把 TraceKit 接到 PocketFlow（或任何 Node/Flow 抽象）上。

PocketFlow 的 Node 契约是 ``prep(shared) -> exec(prep_res) -> post(shared, prep_res, exec_res)``，
外层通过 ``node.run(shared)`` 驱动。所以最小侵入的接入方式是
**包一层代理**，而不是去改它的源码。
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from .trace import Span, TraceRecorder

__all__ = ["instrument_node", "instrument_flow", "instrument_callable"]


def instrument_node(node: Any, recorder: TraceRecorder, name: Optional[str] = None) -> Any:
    """给单个节点套上 Trace 采集（返回一个行为一致的代理对象）。

    支持两种形态：

    * PocketFlow 风格：对象实现了 ``run(shared)``；
    * 函数风格：对象本身就是 callable，接受 ``shared`` 并返回新值或就地修改。
    """
    node_name = name or getattr(node, "name", None) or type(node).__name__

    class _TracedNode:
        """代理模式：保留原对象的全部属性（duck typing），只拦截 run/__call__。"""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, item: str) -> Any:
            return getattr(self._inner, item)

        def run(self, shared: Any) -> Any:
            with recorder.span(node_name, inputs=_preview(shared)) as sp:
                result = self._inner.run(shared)
                # PocketFlow 节点通过 shared 传值、run 只返回 action，
                # 因此输出摘要取 run 之后的 shared 快照，回放 diff 才有意义。
                # 必须走 set_outputs：span() 退出时只持久化 _pending_outputs。
                sp.set_outputs(_preview(shared))
                return result

        def __call__(self, *args, **kwargs):
            with recorder.span(node_name, inputs=_preview(args[0] if args else kwargs)) as sp:
                result = self._inner(*args, **kwargs)
                sp.set_outputs(_preview(result))
                return result

    return _TracedNode(node)


def instrument_flow(nodes: Any, recorder: TraceRecorder) -> Any:
    """给一串节点按顺序套上采集，保持原有顺序与引用。"""
    traced = [instrument_node(n, recorder) for n in nodes]
    try:
        head = traced[0]
        cur = head
        for nxt in traced[1:]:
            setattr(cur, "next", nxt) if hasattr(cur, "next") else None
            cur = nxt
        return head
    except Exception:  # pragma: no cover - 非链式结构时直接返回列表
        return traced


def instrument_callable(fn: Callable, recorder: TraceRecorder, name: Optional[str] = None) -> Callable:
    """给普通函数套上采集（不想改造对象结构时用这个）。"""

    def wrapper(*args, **kwargs):
        with recorder.span(name or getattr(fn, "__name__", "fn"), inputs=_preview(kwargs or args)) as sp:
            result = fn(*args, **kwargs)
            sp.set_outputs(_preview(result))
            return result

    wrapper.__wrapped__ = fn  # type: ignore[attr-defined]
    return wrapper


def _preview(obj: Any, limit: int = 300) -> Any:
    """给 span 的输入/输出做安全预览：超长截断，不可序列化的退化为 repr。"""
    try:
        import json

        json.dumps(obj, ensure_ascii=False, default=str)
        candidate = obj
    except Exception:  # noqa: BLE001
        candidate = repr(obj)
    text = repr(candidate)
    return candidate if len(text) <= limit else {"preview": text[:limit] + "…"}
