# -*- coding: utf-8 -*-
"""内置示例：一个 PocketFlow 风格的极简研究流程，用来演示 TraceKit 的产出。

节点契约与 PocketFlow 保持一致（``run(shared) -> action``），
所以这里的代码**原样替换成真实的 pocketflow.Node / Flow 也能跑**，
只要换成 ``from pocketflow import Node, Flow`` 并把 ``build_flow`` 里的内容改掉即可。

四个节点：``classify → retrieve → draft → verify``，
全部确定性，不需要任何 API Key，随时可跑。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

__all__ = ["Node", "Flow", "build_flow", "FLOW_INPUT", "CORPUS"]

# 一份内置的"知识库"，避免演示时依赖外部检索。
CORPUS: Dict[str, str] = {
    "rag": "RAG（检索增强生成）先检索相关文档片段，再把片段拼进上下文交给大模型生成答案。",
    "agent": "Agent 通过工具调用来扩展能力：先推理要做什么，再调用工具，再用工具返回值继续推理。",
    "mcp": "MCP（Model Context Protocol）把工具、资源以标准化协议暴露给任意 AI 客户端调用。",
    "trace": "Trace 记录一次运行的每个节点输入输出与耗时，用于排查问题和做回归对比。",
}

FLOW_INPUT: Dict[str, Any] = {"question": "什么是 RAG，它和 Agent 有什么关系？"}


class Node:
    """极简节点：``fn(shared) -> action``。函数名就是节点名。"""

    def __init__(self, name: str, fn: Callable[[Dict[str, Any]], Optional[str]]) -> None:
        self.name = name
        self.fn = fn
        self.successors: Dict[Optional[str], "Node"] = {}

    def next(self, node: "Node", action: Optional[str] = "default") -> "Node":
        self.successors[action] = node
        return node

    def run(self, shared: Dict[str, Any]) -> Optional[str]:
        return self.fn(shared)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Node {self.name}>"


class Flow:
    """按 action 在节点之间跳转的执行器。"""

    def __init__(self, start: Node) -> None:
        self.start = start

    def run(self, shared: Dict[str, Any], recorder: Any = None, max_steps: int = 20) -> Dict[str, Any]:
        cur: Optional[Node] = self.start
        steps = 0
        while cur is not None and steps < max_steps:
            steps += 1
            if recorder is not None:
                with recorder.span(cur.name, inputs=_thin(shared)) as sp:
                    action = cur.run(shared)
                    # 抓取"这一跳之后"的状态摘要：既有动作，也有产出内容指纹，
                    # 这样 prompt 一改导致措辞变化，diff 就能精确定位到 draft 节点。
                    sp.set_outputs({"action": action, "state": _thin(shared)})
            else:  # pragma: no cover - 不带 recorder 时纯跑业务逻辑
                action = cur.run(shared)
            cur = _next_node(cur, action)

        return shared


def _next_node(cur: Node, action: Optional[str]) -> Optional[Node]:
    """按 action 找后继节点；``done`` / ``None`` 表示流程终止。"""
    if action is None or action == "done":
        return None
    return cur.successors.get(action) or cur.successors.get("default")


def _thin(shared: Dict[str, Any]) -> Dict[str, Any]:
    """spans 里只存关键字段，避免整份 shared 状态污染 Trace。

    取舍说明：证据列表只存条数（内容通常很长且不参与回归判定），
    而 draft / question / verdict 这些"决定行为是否变了"的字段要存文本，
    否则 diff 只能发现"节点跑过了"，发现不了"答案变了"。
    """
    keep = ("question", "intent", "evidence", "draft", "verdict", "issues")
    out: Dict[str, Any] = {}
    for k, v in shared.items():
        if k not in keep:
            continue
        if k == "evidence":
            out[k] = f"{len(v)} 条"
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# 四个节点的业务逻辑
# ---------------------------------------------------------------------------
def _classify(shared: Dict[str, Any]) -> Optional[str]:
    q = (shared.get("question") or "").lower()
    if not q.strip():
        shared["intent"] = "empty"
        shared["draft"] = "请输入一个非空的问题。"
        return "done"
    if any(k in q for k in ("rag", "检索")):
        shared["intent"] = "rag"
    elif any(k in q for k in ("agent", "智能体")):
        shared["intent"] = "agent"
    elif any(k in q for k in ("mcp", "协议")):
        shared["intent"] = "mcp"
    else:
        shared["intent"] = "unknown"
    return "default"


def _retrieve(shared: Dict[str, Any]) -> Optional[str]:
    q = (shared.get("question") or "").lower()
    hits: List[str] = [text for key, text in CORPUS.items() if key in q or _zh_hit(key, q)]
    if shared["intent"] == "unknown":
        hits = []
    if hits:
        shared["evidence"] = hits[:3]
        return "default"
    # 没检索到就不硬编答案——这是 Agent 应用最容易犯的错
    shared["evidence"] = []
    shared["draft"] = "根据现有资料无法回答该问题，没有检索到可靠证据。"
    return "done"


def _zh_hit(key: str, question: str) -> bool:
    mapping = {"rag": "检索", "agent": "智能体", "mcp": "协议", "trace": "链路"}
    return mapping.get(key, key) in question


def _make_draft(mutate: bool, llm: Optional[Callable[[str, List[str]], str]] = None):
    def _draft(shared: Dict[str, Any]) -> Optional[str]:
        evidence = shared.get("evidence") or []
        if llm is not None:
            shared["draft"] = llm(shared["question"], evidence)
            return "default"
        body = " ".join(evidence)
        if mutate:
            # 模拟"改了一次 prompt"：措辞变了，但事实内容没变
            shared["draft"] = f"【修订版】{body}（已按新 prompt 重新组织语序。）"
        else:
            shared["draft"] = f"结论：{body}"
        return "default"

    return _draft


def _verify(shared: Dict[str, Any]) -> Optional[str]:
    draft = shared.get("draft") or ""
    issues: List[str] = []
    if not draft.strip():
        issues.append("答案为空")
    elif "无法回答" not in draft:
        if not any(snippet[:12] in draft for snippet in shared.get("evidence", [])):
            issues.append("答案疑似未引用任何证据片段")
        if len(draft) > 500:
            issues.append("答案过长，超过 500 字上限")
    shared["issues"] = issues
    shared["verdict"] = "pass" if not issues else "fail"
    return "default"


def build_flow(mutate: bool = False, llm: Optional[Callable[[str, List[str]], str]] = None) -> Flow:
    """组装示例流程。``mutate=True`` 会轻微改写草稿措辞，用来演示 diff。"""
    classify = Node("classify", _classify)
    retrieve = Node("retrieve", _retrieve)
    draft = Node("draft", _make_draft(mutate, llm))
    verify = Node("verify", _verify)

    classify.next(retrieve)
    retrieve.next(draft).next(verify)
    return Flow(classify)
