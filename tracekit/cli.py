# -*- coding: utf-8 -*-
"""命令行入口：``python -m tracekit <command>``

    python -m tracekit demo              跑一遍内置示例流程并落盘 Trace
    python -m tracekit ls                列出所有历史运行
    python -m tracekit show <run_id>     打印某次运行的时间线
    python -m tracekit diff <a> <b>      对比两次运行（例如升级前后）
    python -m tracekit eval              跑内置回归评测套件（失败时退出码非 0，可直接进 CI）
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from .replay import diff_runs, list_runs, load_run
from .trace import TraceRecorder


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="tracekit", description="PocketFlow 的 Trace / Replay / Evals 工具箱")
    parser.add_argument("--runs", default="runs", help="Trace 落盘目录，默认 runs")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("ls", help="列出所有历史运行")
    p_show = sub.add_parser("show", help="打印某次运行的时间线")
    p_show.add_argument("run_id")
    p_diff = sub.add_parser("diff", help="对比两次运行")
    p_diff.add_argument("baseline")
    p_diff.add_argument("candidate")
    p_demo = sub.add_parser("demo", help="跑一遍内置示例流程")
    p_demo.add_argument("--broken", action="store_true", help="注入一次改动，演示 diff 能抓到什么")
    sub.add_parser("eval", help="跑内置回归评测套件")

    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1

    if args.command == "ls":
        rows = list_runs(args.runs)
        if not rows:
            print(f"目录 {args.runs} 下还没有任何运行记录。先跑：python -m tracekit demo")
            return 0
        print(f"{'run_id':<14}{'状态':<10}{'spans':>7}{'错误':>6}{'降级':>6}  名称")
        for r in rows:
            print(
                f"{r['run_id']:<14}{r.get('status',''):<10}{r.get('span_count',0):>7}"
                f"{r.get('error_count',0):>6}{r.get('degraded_count',0):>6}  {r.get('name','')}"
            )
        return 0

    if args.command == "show":
        run = load_run(args.run_id, args.runs)
        if not len(run):
            print(f"找不到运行 {args.run_id}（或该运行没有任何 span）")
            return 1
        print(run.timeline())
        return 0

    if args.command == "diff":
        baseline, candidate = load_run(args.baseline, args.runs), load_run(args.candidate, args.runs)
        report = diff_runs(baseline, candidate)
        print(report.render())
        return 0 if report.identical else 2

    if args.command == "demo":
        return _run_demo(args.runs, broken=args.broken)

    if args.command == "eval":
        from .examples_suite import build_suite  # 延迟导入，避免循环依赖

        report = build_suite().run()
        print(report.render())
        return 0 if report.ok else 1

    parser.print_help()
    return 1


def _run_demo(run_dir: str, broken: bool = False) -> int:
    """跑两次示例流程：一次基准、一次"改坏了"，用于演示 diff 的价值。"""
    from .demo import build_flow, FLOW_INPUT

    def once(tag: str, mutate: bool) -> str:
        recorder = TraceRecorder(run_dir=run_dir, name=f"demo-{tag}", metadata={"variant": tag})
        with recorder:
            shared = dict(FLOW_INPUT)
            flow = build_flow(mutate=mutate)
            flow.run(shared, recorder=recorder)
        recorder.finish()
        return recorder.run_id

    first = once("baseline", mutate=False)
    second = once("candidate", mutate=broken)
    print(f"基准运行：{first}")
    print(f"对照运行：{second}")
    print()
    print(load_run(second, run_dir).timeline())
    print()
    report = diff_runs(load_run(first, run_dir), load_run(second, run_dir))
    print(report.render())
    return 0 if report.identical else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
