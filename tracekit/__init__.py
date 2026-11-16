# -*- coding: utf-8 -*-
"""TraceKit —— 为极简 Agent 框架（如 PocketFlow）补齐可观测与评测层。

三个卖点：

* **Trace**：Run 级借助落盘，JSONL + summary.json，零依赖；
* **Replay**：两次运行按 ``节点::阶段`` 对齐做结构化 diff，改了 prompt 立刻知道哪个节点变了；
* **Evals**：人类可读的回归评测套件，CI 里一条命令跑完并给出通过率门禁。

外加两个实用件：确定性降级装饰器 ``@resilient``，以及 Node/Flow 的无侵入适配 ``instrument_node``。
"""

from .trace import (
    RunSummary,
    Span,
    TraceRecorder,
    digest,
    get_default_recorder,
    set_default_recorder,
)
from .replay import LoadedRun, RunDiff, SpanDiff, diff_runs, list_runs, load_run
from .fallback import DegradationLog, resilient, get_degradation_log, reset_degradation_log
from .evalkit import Case, EvalSuite, Report
from .adapters import instrument_callable, instrument_node

__version__ = "0.1.0"

__all__ = [
    "TraceRecorder",
    "Span",
    "RunSummary",
    "digest",
    "get_default_recorder",
    "set_default_recorder",
    "LoadedRun",
    "RunDiff",
    "SpanDiff",
    "diff_runs",
    "list_runs",
    "load_run",
    "resilient",
    "DegradationLog",
    "get_degradation_log",
    "reset_degradation_log",
    "Case",
    "EvalSuite",
    "Report",
    "instrument_node",
    "instrument_callable",
    "__version__",
]
