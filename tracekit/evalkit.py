# -*- coding: utf-8 -*-
"""轻量回归评测套件：不装 pytest / 不上云也能跑的人类可读回归测试。

为什么不用 pytest？pytest 测的是代码单元，而 Agent 应用要测的是
**端到端行为**：同一个输入，跑完整个 Flow，产出的答案是否还满足约束。
这类用例需要：可读性（产品同学也能写）、可存档（每次运行结果能 diff）、
以及 CI 门禁（失败非零退出）。

所以这里的抽象是：

    Case(name, input, checks=[...])   →   跑一遍 →   Report(pass/fail + 明细)

且 Report 是可 JSON 序列化的，可以当 Trace 一样归档。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

__all__ = [
    "Check",
    "contains",
    "not_contains",
    "regex_matches",
    "json_field",
    "max_latency_ms",
    "Case",
    "EvalSuite",
    "CaseResult",
    "Report",
]


@dataclass
class Check:
    """一个断言。做成数据而不是函数，是为了让结果可以被序列化、被 diff。"""

    name: str
    predicate: Callable[[Any], bool]
    hint: str = ""

    def __call__(self, output: Any) -> bool:
        try:
            return bool(self.predicate(output))
        except Exception:  # noqa: BLE001 - 断言自身出错应算失败，而非崩掉整个套件
            return False


def contains(*substrings: str, case_sensitive: bool = False) -> Check:
    """输出必须包含全部给定子串（常用于校验关键事实有没有被"幻觉掉"）。"""

    def _pred(output: Any) -> bool:
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
        hay = text if case_sensitive else text.lower()
        return all(s if case_sensitive else s.lower() in hay for s in substrings)

    label = "包含 " + " / ".join(substrings)
    return Check(name=label, predicate=_pred, hint=label)


def not_contains(*substrings: str) -> Check:
    """输出不得包含给定子串（常用于校验有没有泄漏 prompt / 内部报错堆栈）。"""

    def _pred(output: Any) -> bool:
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
        return not any(s.lower() in text.lower() for s in substrings)

    label = "不包含 " + " / ".join(substrings)
    return Check(name=label, predicate=_pred, hint=label)


def regex_matches(pattern: str) -> Check:
    import re

    rx = re.compile(pattern)

    def _pred(output: Any) -> bool:
        text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
        return rx.search(text) is not None

    return Check(name=f"匹配正则 {pattern}", predicate=_pred, hint=pattern)


def json_field(path: str, expected: Any) -> Check:
    """按 ``a.b.c`` 路径取结构化输出里的字段并断言相等。"""

    def _pred(output: Any) -> bool:
        cur = output if isinstance(output, dict) else {"value": output}
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return False
        return cur == expected

    return Check(name=f"{path} == {expected!r}", predicate=_pred, hint=path)


def max_latency_ms(limit_ms: float) -> Check:
    """耗时上限。注意这时 input 应为 ``{"latency_ms": ..., "output": ...}``。"""
    return Check(
        name=f"耗时 ≤ {limit_ms}ms",
        predicate=lambda record: float(record.get("latency_ms", 1e9)) <= limit_ms,
        hint="latency",
    )


@dataclass
class Case:
    """一个评测用例。"""

    name: str
    input: Any
    checks: Sequence[Check] = field(default_factory=list)
    tags: Sequence[str] = field(default_factory=tuple)
    timeout_seconds: float = 30.0


@dataclass
class CaseResult:
    name: str
    passed: bool
    latency_ms: float = 0.0
    failures: List[str] = field(default_factory=list)
    error: Optional[str] = None
    output_preview: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Report:
    """整次评测的结果，可直接 JSON 序列化后归档。"""

    suite: str
    results: List[CaseResult]
    started_at: str = ""
    duration_ms: float = 0.0

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed_count(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed_count(self) -> int:
        return self.total - self.passed_count

    @property
    def pass_rate(self) -> float:
        return (self.passed_count / self.total) if self.total else 0.0

    @property
    def ok(self) -> bool:
        return self.failed_count == 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "suite": self.suite,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "total": self.total,
            "passed": self.passed_count,
            "failed": self.failed_count,
            "pass_rate": round(self.pass_rate, 4),
            "ok": self.ok,
            "results": [r.to_dict() for r in self.results],
        }

    def render(self) -> str:
        lines = [
            f"评测套件：{self.suite}",
            f"用例 {self.total} 个，通过 {self.passed_count}，失败 {self.failed_count}，"
            f"通过率 {self.pass_rate:.0%}",
            "",
        ]
        for r in self.results:
            flag = "✓" if r.passed else "✗"
            lines.append(f" {flag} {r.name}  ({r.latency_ms:.1f}ms)")
            if r.error:
                lines.append(f"     └─ 执行异常：{r.error}")
            for f in r.failures:
                lines.append(f"     └─ 未通过：{f}")
        return "\n".join(lines)


class EvalSuite:
    """一组用例 + 一个被测对象（这里是任意 callable）。

    用例跑完之后 :meth:`run` 返回 :class:`Report`，可直接用于 CI 断言，
    也可以存成 JSON，和 Trace 一样按版本归档后做 diff。
    """

    def __init__(self, name: str = "suite", runner: Optional[Callable[[Any], Any]] = None) -> None:
        self.name = name
        self.runner = runner
        self.cases: List[Case] = []

    def add(self, case: Case) -> "EvalSuite":
        self.cases.append(case)
        return self

    def case(
        self,
        name: str,
        input: Any,
        *checks: Check,
        tags: Sequence[str] = (),
    ) -> "EvalSuite":
        """链式注册用例，比手写 dataclass 更顺手。"""
        return self.add(Case(name=name, input=input, checks=list(checks), tags=tuple(tags)))

    def run(self, runner: Optional[Callable[[Any], Any]] = None) -> Report:
        fn = runner or self.runner
        if fn is None:
            raise ValueError("未提供 runner：请给 EvalSuite 传入被测函数，或在 run() 里指定。")

        from datetime import datetime, timezone

        t0 = time.perf_counter()
        results: List[CaseResult] = []
        for case in self.cases:
            started = time.perf_counter()
            error = None
            output: Any = None
            try:
                output = fn(case.input)
            except Exception as exc:  # noqa: BLE001 - 用例崩溃要变成失败项，而不是中断套件
                error = f"{type(exc).__name__}: {exc}"
            latency = (time.perf_counter() - started) * 1000

            failures: List[str] = []
            if error:
                failures.append("执行异常")
            else:
                for check in case.checks:
                    if not check(output):
                        failures.append(check.name)

            preview = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
            results.append(
                CaseResult(
                    name=case.name,
                    passed=not failures,
                    latency_ms=round(latency, 2),
                    failures=failures,
                    error=error,
                    output_preview=preview[:300],
                )
            )

        return Report(
            suite=self.name,
            results=results,
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            duration_ms=round((time.perf_counter() - t0) * 1000, 2),
        )

    def save(self, report: Report, path: str) -> str:
        """把报告落盘，方便跟历史版本 diff。"""
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report.to_dict(), f, ensure_ascii=False, indent=2)
        return path
