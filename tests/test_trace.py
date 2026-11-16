# -*- coding: utf-8 -*-
"""Trace 采集层测试。"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from tracekit.trace import (  # noqa: E402
    Span,
    TraceRecorder,
    digest,
    set_default_recorder,
)


class TestDigest(unittest.TestCase):
    def test_same_object_same_fingerprint(self):
        self.assertEqual(digest({"a": 1})["sha256"], digest({"a": 1})["sha256"])

    def test_key_order_does_not_matter(self):
        self.assertEqual(digest({"a": 1, "b": 2})["sha256"], digest({"b": 2, "a": 1})["sha256"])

    def test_different_object_different_fingerprint(self):
        self.assertNotEqual(digest({"a": 1})["sha256"], digest({"a": 2})["sha256"])

    def test_records_length(self):
        d = digest("x" * 100)
        self.assertGreaterEqual(d["chars"], 100)


class TestRecorderInMemory(unittest.TestCase):
    def setUp(self):
        self.rec = TraceRecorder(run_dir=None, name="mem")

    def test_span_records_timing_and_status(self):
        with self.rec.span("Planner", inputs={"q": "hi"}) as sp:
            pass
        self.assertEqual(len(self.rec.spans), 1)
        self.assertEqual(self.rec.spans[0].status, "ok")
        self.assertGreaterEqual(self.rec.spans[0].duration_ms, 0)

    def test_exception_is_recorded_and_reraised(self):
        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            with self.rec.span("Writer"):
                raise Boom("炸了")
        span = self.rec.spans[0]
        self.assertEqual(span.status, "error")
        self.assertIn("炸了", span.error)

    def test_set_outputs_is_digested(self):
        with self.rec.span("Draft") as sp:
            sp.set_outputs({"answer": "hello"})
        self.assertIn("sha256", self.rec.spans[0].outputs)

    def test_mark_degraded(self):
        self.rec.mark_degraded("Retriever", "向量库不可用", outputs="兜底结果")
        self.assertEqual(self.rec.spans[0].status, "degraded")
        self.assertEqual(self.rec.summary.degraded_count, 1)

    def test_summary_status_reflects_spans(self):
        with self.rec.span("A"):
            pass
        self.rec.mark_degraded("B", "reason")
        summary = self.rec.finish()
        self.assertEqual(summary.status, "degraded")
        self.assertEqual(summary.span_count, 2)

    def test_summary_error_status(self):
        with self.assertRaises(ValueError):
            with self.rec.span("A"):
                raise ValueError("x")
        summary = self.rec.finish()
        self.assertEqual(summary.status, "error")


class TestRecorderPersisted(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.rec = TraceRecorder(run_dir=self.tmp, name="persist", metadata={"git": "abc123"})

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        set_default_recorder(None)

    def test_files_are_written(self):
        with self.rec.span("A", inputs={"x": 1}) as sp:
            sp.set_outputs({"y": 2})
        summary = self.rec.finish()
        base = os.path.join(self.tmp, summary.run_id)
        self.assertTrue(os.path.isfile(os.path.join(base, "trace.jsonl")))
        self.assertTrue(os.path.isfile(os.path.join(base, "summary.json")))

    def test_jsonl_lines_are_valid_json(self):
        with self.rec.span("A"):
            pass
        self.rec.finish()
        path = os.path.join(self.tmp, self.rec.run_id, "trace.jsonl")
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(rows), 1)
        self.assertTrue(Span.from_dict(rows[0]).node == "A")

    def test_metadata_persisted(self):
        self.rec.finish()
        path = os.path.join(self.tmp, self.rec.run_id, "summary.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["metadata"]["git"], "abc123")
        self.assertIn("trace_version", data)

    def test_context_manager_form(self):
        with TraceRecorder(run_dir=self.tmp, name="cm") as rec:
            rec.mark_degraded("X", "r")
        path = os.path.join(self.tmp, rec.run_id, "summary.json")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["status"], "degraded")


if __name__ == "__main__":
    unittest.main(verbosity=2)
