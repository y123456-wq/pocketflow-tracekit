# -*- coding: utf-8 -*-
"""适配器回归测试。

历史缺陷：instrument_node / instrument_callable 曾直接给 ``sp.outputs`` 赋值，
而 ``TraceRecorder.span()`` 退出时只持久化 ``_pending_outputs``，导致节点输出
被覆盖成空摘要、回放 diff 失真。这里用断言锁住"输出必须被记录"。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tracekit import TraceRecorder, instrument_callable, instrument_node  # noqa: E402


class _FakeNode:
    """PocketFlow 风格节点：run(shared) 就地改状态并返回 action。"""

    name = "FakeNode"

    def __init__(self):
        self.extra = "kept"

    def run(self, shared):
        shared["seen"] = True
        return "default"


class TestInstrumentNode(unittest.TestCase):
    def test_records_non_empty_outputs(self):
        rec = TraceRecorder(run_dir=None, name="t")
        node = instrument_node(_FakeNode(), rec)
        shared = {}
        action = node.run(shared)
        self.assertEqual(action, "default")
        self.assertTrue(shared["seen"])
        self.assertEqual(len(rec.spans), 1)
        out = rec.spans[0].outputs
        self.assertTrue(out, "节点输出摘要不能为空（回归：曾被 span 退出覆盖）")
        self.assertIn("sha256", out)

    def test_proxy_preserves_attributes(self):
        rec = TraceRecorder(run_dir=None, name="t")
        node = instrument_node(_FakeNode(), rec)
        self.assertEqual(node.extra, "kept")
        self.assertEqual(node.name, "FakeNode")


class TestInstrumentCallable(unittest.TestCase):
    def test_records_non_empty_outputs(self):
        rec = TraceRecorder(run_dir=None, name="t")

        def add(a, b):
            return a + b

        wrapped = instrument_callable(add, rec, name="add")
        self.assertEqual(wrapped(2, 3), 5)
        self.assertEqual(len(rec.spans), 1)
        self.assertTrue(rec.spans[0].outputs, "函数输出摘要不能为空")
        self.assertEqual(rec.spans[0].node, "add")


class TestDeltaSemantics(unittest.TestCase):
    def test_diff_pinpoints_only_changed_node(self):
        """全局字段（variant）变化不应让所有节点都被标记；只有真正改输出的节点才算。"""
        import tempfile

        from tracekit import diff_runs, load_run

        class TouchNode:
            name = "TouchNode"

            def run(self, shared):
                shared["query"] = shared["q"]
                return "default"

        class AnswerNode:
            name = "AnswerNode"

            def run(self, shared):
                shared["answer"] = "[v2]" if shared["variant"] == "v2" else "[v1]"
                return "default"

        with tempfile.TemporaryDirectory() as td:
            ids = []
            for variant in ("v1", "v2"):
                rec = TraceRecorder(run_dir=td, name=variant)
                shared = {"q": "hi", "variant": variant}
                for n in (TouchNode(), AnswerNode()):
                    instrument_node(n, rec).run(shared)
                ids.append(rec.finish().run_id)
            d = diff_runs(load_run(ids[0], td), load_run(ids[1], td))
            self.assertEqual(d.changed_nodes, ["AnswerNode"])


if __name__ == "__main__":
    unittest.main(verbosity=2)