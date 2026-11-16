# -*- coding: utf-8 -*-
"""回放 diff、确定性降级与回归评测套件测试。"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tracekit.demo import build_flow  # noqa: E402
from tracekit.evalkit import (  # noqa: E402
    EvalSuite,
    contains,
    json_field,
    not_contains,
)
from tracekit.fallback import get_degradation_log, reset_degradation_log, resilient  # noqa: E402
from tracekit.replay import diff_runs, list_runs, load_run  # noqa: E402
from tracekit.trace import TraceRecorder, set_default_recorder  # noqa: E402


def _record_flow(tmp: str, name: str, **flow_kwargs) -> str:
    rec = TraceRecorder(run_dir=tmp, name=name)
    with rec:
        shared = {"question": "什么是 RAG，它和 Agent 有什么关系？"}
        build_flow(**flow_kwargs).run(shared, recorder=rec)
    rec.finish()
    return rec.run_id


class TestReplayDiff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_identical_runs(self):
        a = _record_flow(self.tmp, "a")
        b = _record_flow(self.tmp, "b")
        report = diff_runs(load_run(a, self.tmp), load_run(b, self.tmp))
        self.assertTrue(report.identical, report.render())

    def test_detects_draft_change_and_locates_node(self):
        a = _record_flow(self.tmp, "base")
        b = _record_flow(self.tmp, "changed", mutate=True)
        report = diff_runs(load_run(a, self.tmp), load_run(b, self.tmp))
        self.assertFalse(report.identical)
        self.assertIn("draft", report.changed_nodes)
        self.assertIn("❌", report.render())

    def test_detects_missing_node(self):
        full: TraceRecorder = None
        base_id = _record_flow(self.tmp, "full")
        partial = TraceRecorder(run_dir=self.tmp, name="partial")
        with partial:
            with partial.span("classify"):
                pass
        partial.finish()
        report = diff_runs(load_run(base_id, self.tmp), load_run(partial.run_id, self.tmp))
        self.assertFalse(report.identical)
        self.assertTrue(any(d.kind == "only-in-a" for d in report.diffs))

    def test_detects_status_change(self):
        r1 = TraceRecorder(run_dir=self.tmp, name="ok")
        with r1.span("step"):
            pass
        r1.finish()
        r2 = TraceRecorder(run_dir=self.tmp, name="bad")
        try:
            with r2.span("step"):
                raise RuntimeError("下游超时")
        except RuntimeError:
            pass
        r2.finish()
        report = diff_runs(load_run(r1.run_id, self.tmp), load_run(r2.run_id, self.tmp))
        self.assertTrue(any(d.kind == "status" for d in report.diffs))

    def test_report_is_serializable(self):
        a = _record_flow(self.tmp, "a")
        b = _record_flow(self.tmp, "b", mutate=True)
        report = diff_runs(load_run(a, self.tmp), load_run(b, self.tmp))
        text = json.dumps(report.to_dict(), ensure_ascii=False)
        self.assertIn("changed_nodes", text)

    def test_list_runs_ordered_by_time(self):
        first = _record_flow(self.tmp, "first")
        time.sleep(0.01)
        second = _record_flow(self.tmp, "second")
        ids = [r["run_id"] for r in list_runs(self.tmp)]
        self.assertEqual(ids.index(first) < ids.index(second), True)

    def test_load_missing_run_is_empty_not_crash(self):
        self.assertEqual(len(load_run("nonexistent", self.tmp)), 0)


class TestResilient(unittest.TestCase):
    def setUp(self):
        reset_degradation_log()
        set_default_recorder(TraceRecorder(run_dir=None, name="t"))

    def tearDown(self):
        reset_degradation_log()
        set_default_recorder(None)

    def test_success_path_unchanged(self):
        @resilient(fallback="兜底")
        def ok_fn():
            return "正常结果"

        self.assertEqual(ok_fn(), "正常结果")
        self.assertEqual(get_degradation_log().count(), 0)

    def test_falls_back_on_error(self):
        @resilient(fallback="兜底答案")
        def bad_fn():
            raise ConnectionError("LLM 超时")

        self.assertEqual(bad_fn(), "兜底答案")
        self.assertEqual(get_degradation_log().count(), 1)
        self.assertIn("LLM 超时", get_degradation_log().events[0].reason)

    def test_retries_then_falls_back(self):
        calls = {"n": 0}

        @resilient(fallback="兜底", retries=2)
        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ValueError("not yet")
            return "第三次成功"

        self.assertEqual(flaky(), "第三次成功")
        self.assertEqual(calls["n"], 3)

    def test_callable_fallback_receives_exception(self):
        @resilient(fallback=lambda exc, attempts: f"降级({type(exc).__name__}@{attempts})")
        def bad():
            raise KeyError("missing")

        self.assertEqual(bad(), "降级(KeyError@1)")

    def test_degradation_is_recorded_in_trace(self):
        rec = TraceRecorder(run_dir=None, name="with-trace")
        set_default_recorder(rec)

        @resilient(fallback="兜底")
        def bad():
            raise RuntimeError("x")

        bad()
        self.assertEqual(rec.summary.degraded_count, 1)

    def test_record_can_be_disabled(self):
        set_default_recorder(None)
        rec = TraceRecorder(run_dir=None, name="silent")
        set_default_recorder(rec)

        @resilient(fallback="兜底", record=False)
        def bad():
            raise RuntimeError("x")

        self.assertEqual(bad(), "兜底")
        self.assertEqual(rec.summary.degraded_count, 0)


class TestEvalSuite(unittest.TestCase):
    def _suite(self) -> EvalSuite:
        def runner(q: str) -> str:
            if not q:
                raise ValueError("空输入")
            return {"rag": "RAG 是检索增强生成", "other": "根据现有资料无法回答该问题"}.get(q, "未知")

        suite = EvalSuite(name="demo", runner=runner)
        suite.case("命中", "rag", contains("检索增强生成"))
        suite.case("兜底", "other", contains("无法回答"))
        suite.case("崩溃", "", contains("随便"))
        return suite

    def test_report_counts(self):
        report = self._suite().run()
        self.assertEqual(report.total, 3)
        self.assertEqual(report.passed_count, 2)
        self.assertEqual(report.failed_count, 1)
        self.assertFalse(report.ok)

    def test_failing_case_exposes_error(self):
        report = self._suite().run()
        crashed = next(r for r in report.results if r.name == "崩溃")
        self.assertIn("ValueError", crashed.error)

    def test_pass_rate_and_render(self):
        report = self._suite().run()
        self.assertAlmostEqual(report.pass_rate, 2 / 3)
        text = report.render()
        self.assertIn("通过率", text)
        self.assertIn("✗", text)

    def test_report_serializable_and_savable(self):
        suite = self._suite()
        report = suite.run()
        path = os.path.join(tempfile.mkdtemp(), "nested", "report.json")
        suite.save(report, path)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["total"], 3)

    def test_checks_behave(self):
        self.assertTrue(contains("abc")("xxabcxx"))
        self.assertFalse(contains("zzz")("xxabcxx"))
        self.assertTrue(contains("ABC")("abc"))
        self.assertTrue(not_contains("Traceback")("正常输出"))
        self.assertFalse(not_contains("Traceback")("Traceback boom"))
        self.assertTrue(json_field("a.b", 1)({"a": {"b": 1}}))
        self.assertFalse(json_field("a.b", 2)({"a": {"b": 1}}))

    def test_requires_runner(self):
        with self.assertRaises(ValueError):
            EvalSuite(name="x").run()


class TestEndToEndDemo(unittest.TestCase):
    """把 demo 流程当成一条真实业务链路跑一遍。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_flow_produces_evidence_based_answer(self):
        rec = TraceRecorder(run_dir=self.tmp, name="e2e")
        shared = {"question": "RAG 是什么"}
        with rec:
            build_flow().run(shared, recorder=rec)
        rec.finish()
        self.assertEqual(shared["verdict"], "pass")
        self.assertIn("检索增强生成", shared["draft"])
        self.assertEqual(rec.summary.span_count, 4)  # classify → retrieve → draft → verify

    def test_flow_admits_ignorance(self):
        shared = {"question": "量子永生怎么实现"}
        build_flow().run(shared)
        self.assertIn("无法回答", shared["draft"])

    def test_llm_injection(self):
        calls = []

        def fake_llm(question, evidence):
            calls.append(question)
            return f"LLM 生成：{question}（引用 {len(evidence)} 条）"

        shared = {"question": "RAG 是什么"}
        build_flow(llm=fake_llm).run(shared)
        self.assertIn("LLM 生成", shared["draft"])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
