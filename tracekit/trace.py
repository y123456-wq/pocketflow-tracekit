# -*- coding: utf-8 -*-
"""Run 级 Trace 采集：把一次 Agent/流程执行完整落盘，事后可回放、可对比。

PocketFlow 的核心抽象是 Node / Flow，它刻意不内置任何观测能力
（官方文档明确说明 tracing、evals、guardrails 不在核心里）。
TraceKit 补的就是这一层，设计上遵循三条原则：

1. **零依赖**：只用标准库，不引入 OpenTelemetry 的重量级依赖，
   因此对 PocketFlow 这种"100 行、零依赖"的框架侵入最小；
2. **可回放**：不只记录日志，而是记录每个节点的输入/输出摘要与时间线，
   使得同一次运行可以被结构化重放并与另一次运行做 diff；
3. **失败即数据**：异常、降级、重试全部作为一等公民写进 span，
   而不是被打进日志里丢掉。

产物格式（JSONL + JSON 摘要）：

    runs/<run_id>/trace.jsonl    每行一个 span，便于流式写入与增量分析
    runs/<run_id>/summary.json   整次运行的汇总指标
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

__all__ = ["Span", "RunSummary", "TraceRecorder", "get_default_recorder", "digest", "utc_now"]

TRACE_VERSION = "1.0"


def utc_now() -> str:
    """UTC 时间戳字符串（ISO-8601，带时区），避免不同机器时区不同导致的diff噪音。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def digest(obj: Any, max_chars: int = 4000) -> Dict[str, Any]:
    """给任意输入输出生成稳定摘要（长度 + sha256 + 截断预览）。

    回放时不需要比对完整内容（那会把 token 级抖动也算成差异），
    只需要一个稳定的指纹，因此在做 diff 之前会先做一次规范化。
    """
    try:
        text = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 - 任何对象都要能被摘要
        text = repr(obj)
    preview = text[:200]
    return {
        "chars": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "preview": preview if len(text) <= max_chars else preview + "…",
    }


@dataclass
class Span:
    """一次节点执行（或节点内的一个阶段）的记录。"""

    run_id: str
    span_id: str
    node: str
    phase: str = "exec"
    parent_id: Optional[str] = None
    started_at: str = ""
    ended_at: str = ""
    duration_ms: float = 0.0
    status: str = "ok"  # ok | error | degraded | skipped
    inputs: Dict[str, Any] = field(default_factory=dict)
    outputs: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    retries: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Span":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})

    @property
    def key(self) -> str:
        """回放 diff 时的对齐键：同一节点的同一阶段。"""
        return f"{self.node}::{self.phase}"

    def set_outputs(self, outputs: Any) -> None:
        """在 ``with recorder.span(...) as sp:`` 块内指定要持久化的输出摘要。

        之所以要显式指定而不是自动捕获全部状态，是因为 shared state 里通常塞着
        整个对话历史、整份证据列表，全量落盘既浪费也让 diff 全是噪音。
        """
        self._pending_outputs = outputs  # type: ignore[attr-defined]


@dataclass
class RunSummary:
    """一次运行的汇总指标，用于回归看板与 CI 门禁。"""

    run_id: str
    name: str
    started_at: str
    ended_at: str = ""
    duration_ms: float = 0.0
    span_count: int = 0
    error_count: int = 0
    degraded_count: int = 0
    retry_count: int = 0
    status: str = "running"  # running | ok | error | degraded
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunSummary":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in known})


class TraceRecorder:
    """Trace 采集器。

    Args:
        run_dir: 落盘根目录，默认 ``runs``。传 ``None`` 则只内存记录（测试常用）。
        name: 本次运行的可读名字。
        metadata: 附加信息（模型版本、git commit、环境等），会写进 summary。
    """

    def __init__(
        self,
        run_dir: Optional[str] = "runs",
        name: str = "run",
        metadata: Optional[Dict[str, Any]] = None,
        run_id: Optional[str] = None,
    ) -> None:
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.name = name
        self.run_dir = run_dir
        self.span_group_lock = threading.Lock()
        self.summary = RunSummary(
            run_id=self.run_id,
            name=name,
            started_at=utc_now(),
            metadata=dict(metadata or {}),
        )
        self.spans: List[Span] = []
        self._open: Dict[str, Span] = {}
        if run_dir:
            os.makedirs(os.path.join(run_dir, self.run_id), exist_ok=True)
            self._jsonl_path = os.path.join(run_dir, self.run_id, "trace.jsonl")
        else:
            self._jsonl_path = None

    # ---- 上下文管理器：最常用的采集方式 ---------------------------------
    @contextmanager
    def span(
        self,
        node: str,
        phase: str = "exec",
        inputs: Any = None,
        parent_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Iterator[Span]:
        """用 ``with recorder.span("Planner") as sp:`` 包住一段执行并自动计时。

        块内抛异常时不会吞掉异常，而是标记 span 为 error 后原样向上抛。
        """
        sp = self.start_span(node, phase, inputs, parent_id, metadata)
        try:
            yield sp
        except Exception as exc:  # noqa: BLE001 - 记录后必须继续向上抛
            self.end_span(sp, status="error", error=f"{type(exc).__name__}: {exc}")
            raise
        else:
            # 允许调用方在块内通过 ``sp.set_outputs(...)`` 指定要持久化的输出摘要
            self.end_span(sp, outputs=getattr(sp, "_pending_outputs", None), status=sp.status)

    def start_span(
        self,
        node: str,
        phase: str = "exec",
        inputs: Any = None,
        parent_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Span:
        sp = Span(
            run_id=self.run_id,
            span_id=uuid.uuid4().hex[:8],
            node=node,
            phase=phase,
            parent_id=parent_id,
            started_at=utc_now(),
            inputs=digest(inputs) if inputs is not None else {},
            metadata=dict(metadata or {}),
        )
        sp._t0 = time.perf_counter()  # type: ignore[attr-defined] - 不计入序列化字段
        with self.span_group_lock:
            self._open[sp.span_id] = sp
        return sp

    def end_span(
        self,
        span: Span,
        outputs: Any = None,
        status: str = "ok",
        error: Optional[str] = None,
        retries: int = 0,
    ) -> Span:
        span.ended_at = utc_now()
        span.duration_ms = round((time.perf_counter() - span._t0) * 1000, 3) if hasattr(span, "_t0") else 0.0
        span.status = status
        span.outputs = digest(outputs) if outputs is not None else {}
        span.error = error
        span.retries = retries
        self.record(span)
        with self.span_group_lock:
            self._open.pop(span.span_id, None)
        return span

    def record(self, span: Span) -> None:
        """落盘 + 内存留痕，并增量更新 summary。"""
        self.spans.append(span)
        s = self.summary
        s.span_count += 1
        if span.status == "error":
            s.error_count += 1
        elif span.status == "degraded":
            s.degraded_count += 1
        s.retry_count += span.retries
        if self._jsonl_path:
            with open(self._jsonl_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(span.to_dict(), ensure_ascii=False) + "\n")

    # ---- 便捷记账 -------------------------------------------------------
    def mark_degraded(self, node: str, reason: str, outputs: Any = None) -> Span:
        """记录一次降级：没崩，但走了备用路径。"""
        sp = Span(
            run_id=self.run_id,
            span_id=uuid.uuid4().hex[:8],
            node=node,
            phase="fallback",
            started_at=utc_now(),
            ended_at=utc_now(),
            status="degraded",
            metadata={"reason": reason},
            outputs=digest(outputs) if outputs is not None else {},
        )
        self.record(sp)
        return sp

    def finish(self, status: Optional[str] = None) -> RunSummary:
        """收尾：写 summary.json 并返回汇总结果。"""
        s = self.summary
        s.ended_at = utc_now()
        s.duration_ms = round((time.perf_counter() - self._t0) * 1000, 3) if hasattr(self, "_t0") else 0.0
        if status:
            s.status = status
        else:
            s.status = "error" if s.error_count else ("degraded" if s.degraded_count else "ok")
        if self.run_dir:
            path = os.path.join(self.run_dir, self.run_id, "summary.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {"trace_version": TRACE_VERSION, **s.to_dict()},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        return s

    # ---- 兼容 with TraceRecorder(...) as r: 写法 -------------------------
    def __enter__(self) -> "TraceRecorder":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.finish(status="error" if exc_type else None)
        return False



_DEFAULT_RECORDER: Optional[TraceRecorder] = None
_DEFAULT_LOCK = threading.Lock()


def get_default_recorder(run_dir: Optional[str] = "runs") -> TraceRecorder:
    """进程级默认采集器，便于装饰器等场景无侵入地记录。"""
    global _DEFAULT_RECORDER
    with _DEFAULT_LOCK:
        if _DEFAULT_RECORDER is None:
            _DEFAULT_RECORDER = TraceRecorder(run_dir=run_dir, name="default")
            _DEFAULT_RECORDER._t0 = time.perf_counter()  # type: ignore[attr-defined]
        return _DEFAULT_RECORDER


def set_default_recorder(recorder: Optional[TraceRecorder]) -> None:
    """替换（或清空）默认采集器，测试里用来隔离。"""
    global _DEFAULT_RECORDER
    with _DEFAULT_LOCK:
        _DEFAULT_RECORDER = recorder
