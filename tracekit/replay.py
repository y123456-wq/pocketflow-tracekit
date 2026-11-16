# -*- coding: utf-8 -*-
"""Trace 回放与 diff：把"上一次跑得好好的"变成可以断言的事实。

单次 Trace 只能看热闹，真正有用的是**两次运行的结构化对比**：
改了一个 prompt、换了一个模型、升级了一个依赖之后，
到底是哪个节点的输出变了？这就是回归。

对比时不比对时间戳和耗时（每次跑必然不同），只对齐
``节点::阶段`` 这一键值，比较：状态码、输出指纹、报错信息。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .trace import RunSummary, Span, TraceRecorder

__all__ = ["SpanDiff", "RunDiff", "load_run", "diff_runs", "list_runs", "replay_summary"]


@dataclass
class SpanDiff:
    """单个节点的变化。"""

    key: str
    kind: str  # only-in-a | only-in-b | status | output | error
    expected: Any = None
    actual: Any = None

    def describe(self) -> str:
        kind_text = {
            "only-in-a": "基准运行中有、本次缺失",
            "only-in-b": "本次新增",
            "status": "执行状态变化",
            "output": "输出内容变化",
            "error": "报错信息变化",
        }[self.kind]
        return f"{self.key}：{kind_text}（基准={_short(self.expected)} 本次={_short(self.actual)}）"

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "kind": self.kind, "expected": self.expected, "actual": self.actual}


@dataclass
class RunDiff:
    """两次运行的整体差异。"""

    baseline_id: str
    candidate_id: str
    diffs: List[SpanDiff] = field(default_factory=list)
    baseline_summary: Optional[RunSummary] = None
    candidate_summary: Optional[RunSummary] = None

    @property
    def identical(self) -> bool:
        return not self.diffs

    @property
    def changed_nodes(self) -> List[str]:
        return sorted({d.key.split("::")[0] for d in self.diffs})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "baseline_id": self.baseline_id,
            "candidate_id": self.candidate_id,
            "identical": self.identical,
            "changed_nodes": self.changed_nodes,
            "diffs": [d.to_dict() for d in self.diffs],
        }

    def render(self) -> str:
        if self.identical:
            return f"✅ 两次运行完全一致（{self.baseline_id} vs {self.candidate_id}）"
        lines = [
            f"❌ 发现 {len(self.diffs)} 处差异：{self.baseline_id} → {self.candidate_id}",
            f"   受影响节点：{', '.join(self.changed_nodes)}",
            "",
        ]
        lines.extend("   - " + d.describe() for d in self.diffs)
        return "\n".join(lines)


def list_runs(run_dir: str = "runs") -> List[Dict[str, Any]]:
    """列出某个目录下所有已落盘的运行（按开始时间排序）。"""
    if not os.path.isdir(run_dir):
        return []
    out = []
    for rid in sorted(os.listdir(run_dir)):
        summary_path = os.path.join(run_dir, rid, "summary.json")
        if not os.path.isfile(summary_path):
            continue
        with open(summary_path, encoding="utf-8") as f:
            out.append(json.load(f))
    out.sort(key=lambda d: d.get("started_at", ""))
    return out


def load_run(run_id: str, run_dir: str = "runs") -> "LoadedRun":
    """读取一次运行的完整 Trace。"""
    return LoadedRun(run_id=run_id, run_dir=run_dir)


class LoadedRun:
    """一次运行的结构化视图（spans + summary）。"""

    def __init__(self, run_id: str, run_dir: str = "runs") -> None:
        self.run_id = run_id
        self.run_dir = run_dir
        base = os.path.join(run_dir, run_id)
        self.base = base
        trace_path = os.path.join(base, "trace.jsonl")
        self.spans: List[Span] = []
        if os.path.isfile(trace_path):
            with open(trace_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        self.spans.append(Span.from_dict(json.loads(line)))
        summary_path = os.path.join(base, "summary.json")
        self.summary: Optional[RunSummary] = None
        if os.path.isfile(summary_path):
            with open(summary_path, encoding="utf-8") as f:
                self.summary = RunSummary.from_dict(json.load(f))

    def __len__(self) -> int:
        return len(self.spans)

    def by_key(self) -> Dict[str, List[Span]]:
        """按 ``节点::阶段`` 分组，同一节点多轮调用也都能对齐。"""
        grouped: Dict[str, List[Span]] = {}
        for sp in self.spans:
            grouped.setdefault(sp.key, []).append(sp)
        return grouped

    def timeline(self) -> str:
        """人类可读的时间线，用于快速定位卡在哪一步。"""
        lines = [f"run {self.run_id}: {len(self.spans)} spans", ""]
        for sp in self.spans:
            flag = {"ok": " ", "error": "✗", "degraded": "!", "skipped": "-"}.get(sp.status, "?")
            lines.append(f" {flag} {sp.node:<24} {sp.phase:<10} {sp.duration_ms:>8.2f}ms")
            if sp.error:
                lines.append(f"     └─ {sp.error}")
        return "\n".join(lines)


def _short(value: Any, limit: int = 90) -> str:
    """把对比值压到一行：diff 输出的价值在于**快速看出变了什么**，不是全文。"""
    text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"


def diff_runs(baseline: LoadedRun, candidate: LoadedRun) -> RunDiff:
    """比较两次运行，返回结构化差异对象。

    Args:
        baseline: 基准运行（通常是已归档的、被认为"正确"的那次）。
        candidate: 本次运行。
    """
    result = RunDiff(
        baseline_id=baseline.run_id,
        candidate_id=candidate.run_id,
        baseline_summary=baseline.summary,
        candidate_summary=candidate.summary,
    )
    a = baseline.by_key()
    b = candidate.by_key()

    for key in sorted(set(a) | set(b)):
        a_spans, b_spans = a.get(key, []), b.get(key, [])
        if not b_spans:
            result.diffs.append(SpanDiff(key, "only-in-a", expected=len(a_spans)))
            continue
        if not a_spans:
            result.diffs.append(SpanDiff(key, "only-in-b", actual=len(b_spans)))
            continue
        if len(a_spans) != len(b_spans):
            result.diffs.append(SpanDiff(key, "only-in-a" if len(a_spans) > len(b_spans) else "only-in-b",
                                         expected=len(a_spans), actual=len(b_spans)))
        for i, pair in enumerate(zip(a_spans, b_spans), 1):
            sa, sb = pair
            suffix = key if len(a_spans) == 1 else f"{key}#{i}"
            if sa.status != sb.status:
                result.diffs.append(SpanDiff(suffix, "status", sa.status, sb.status))
            if sa.outputs.get("sha256") != sb.outputs.get("sha256"):
                result.diffs.append(SpanDiff(suffix, "output", sa.outputs.get("preview"), sb.outputs.get("preview")))
            if (sa.error or "") != (sb.error or ""):
                result.diffs.append(SpanDiff(suffix, "error", sa.error, sb.error))
    return result


def replay_summary(recorder: TraceRecorder) -> str:
    """对一个**刚跑完**的采集器生成时间线（不必先落盘再读回来）。"""
    temp = LoadedRun(recorder.run_id, recorder.run_dir or "")
    temp.spans = list(recorder.spans)
    temp.summary = recorder.summary
    return temp.timeline()
