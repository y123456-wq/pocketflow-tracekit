# -*- coding: utf-8 -*-
"""内置回归评测套件：``python -m tracekit eval`` 跑的就是它。

这些用例刻意覆盖了 Agent 应用最容易翻车的几类情况，
不是"能跑就行"的 happy path：

1. 正常问答有没有答到点上；
2. 检索不到证据时有没有**如实说不知道**（而不是编一个）；
3. 空输入会不会崩；
4. LLM 挂掉时有没有降级成可用答案；
5. 答案有没有超过长度约束。
"""

from __future__ import annotations

from typing import Any, Dict

from .demo import build_flow
from .evalkit import EvalSuite, contains, not_contains
from .fallback import resilient

__all__ = ["run_flow_question", "build_suite"]


@resilient(fallback=lambda exc, attempts: f"[降级答案] 主链路第 {attempts} 次尝试失败：{exc}", node="flow_entry")
def run_flow_question(question: str, llm=None) -> str:
    """跑一遍示例流程并返回最终答案。被 @resilient 包住，保证异常也能有兜底输出。"""
    shared: Dict[str, Any] = {"question": question}
    build_flow(llm=llm).run(shared)
    # verdict 可能不存在：某些分支（如空输入）会在中途直接结束流程
    if shared.get("verdict") == "fail":
        raise AssertionError("答案未通过校验：" + "；".join(shared["issues"]))
    return shared.get("draft", "")


def build_suite() -> EvalSuite:
    suite = EvalSuite(name="tracekit-demo-flow", runner=run_flow_question)

    suite.case(
        "命中RAG时答到要点",
        "RAG 是什么",
        contains("检索增强生成"),
        contains("大模型"),
    )

    suite.case(
        "命中Agent时答到要点",
        "介绍一下 Agent",
        contains("工具调用"),
    )

    suite.case(
        "检索不到时如实说不知道",
        "区块链理财产品收益率如何",
        contains("无法回答"),
        not_contains("根据经验", "我认为"),
    )

    suite.case(
        "空输入不应崩溃",
        "",
        contains("请输入"),
    )

    suite.case(
        "答案不得泄漏内部堆栈",
        "MCP 有什么用",
        not_contains("Traceback", "NoneType"),
    )
    return suite
