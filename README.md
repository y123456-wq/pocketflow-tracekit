# TraceKit

**给极简 Agent 框架（PocketFlow 及其同类）补齐可观测与评测层。**

> Based on nothing, built for [PocketFlow](https://github.com/The-Pocket/PocketFlow) 的缺口。
> PocketFlow 只有 100 行、零依赖，但它官方文档明确写了：**不提供 tracing、evals、guardrails**。
> TraceKit 补的就是这三样的一半：Run 级 Trace 落盘 + 结构化回放 diff + CI 里的回归评测门禁，
> 以及一个确定性降级装饰器。全部零依赖（只用标准库）。

```bash
python -m tracekit demo            # 跑一遍示例流程，落盘两次 Trace 并对比
python -m tracekit ls              # 列出所有历史运行
python -m tracekit show <run_id>   # 打印某次运行的时间线
python -m tracekit diff <a> <b>    # 对比两次运行：改了 prompt 之后到底哪个节点的输出变了
python -m tracekit eval            # 跑回归评测套件（失败退出码非 0，可直接进 CI）
```

## 为什么需要它

写过 Agent 的人一定遇到过这几个场景：

| 场景 | 没有 TraceKit | 有 TraceKit |
|---|---|---|
| 改了一句 prompt，答案变差了 | 凭感觉，"好像上次不是这样的" | `python -m tracekit diff 上次 这次`，精确定位到 `draft` 节点的输出指纹变了 |
| LLM 超时当晚，第二天没人知道 | 日志里翻 | 时间线里直接标出 `status=degraded` 与原因，降级次数进 summary |
| 想确认某个 corner case 没回归 | 手动点一遍 | 一条 Case + 几条断言，CI 每次都跑 |
| 想给别人复现一个 bug | 讲不清楚 | 把 `runs/<run_id>/` 整个目录发过去，别人 `show` 一下就看到全链路 |

## 五分钟上手

### 1. 采集 Trace

```python
from tracekit import TraceRecorder
from tracekit.demo import build_flow

rec = TraceRecorder(run_dir="runs", name="research-brief", metadata={"git": "a1b2c3"})
with rec:
    shared = {"question": "什么是 RAG？"}
    build_flow().run(shared, recorder=rec)
summary = rec.finish()
print(summary.status, summary.span_count, summary.degraded_count)
```

也可以手工标记粒度：

```python
with rec.span("retrieve", inputs=query) as sp:
    docs = vectorstore.search(query)
    sp.set_outputs({"hits": len(docs)})
```

产物：

```
runs/<run_id>/
├── trace.jsonl     每行一个 span，流式写入，方便增量分析
└── summary.json    汇总：span 数、错误数、降级数、重试数、总耗时
```

### 2. 回放对比

```python
from tracekit import load_run, diff_runs

report = diff_runs(load_run("基准run_id"), load_run("本次run_id"))
print(report.render())
```

对齐策略是按 `节点::阶段` 键值，比较**状态码、输出指纹、报错信息**，
刻意不比较时间戳和耗时——那两个每次跑都不同，只会产生噪音。

实测输出（`python -m tracekit demo --broken`）：

```
❌ 发现 2 处差异：34fd6ebb244f → f1ba3f076fc1
   受影响节点：draft, verify

   - draft::exec：输出内容变化（基准='{"action": "default", "state": {"draft": "结论：RAG（检索增强生成）…' 本次='{"action": "default", "state": {"draft": "【修订版】RAG（…')
   - verify::exec：输出内容变化（…）
```

### 3. 确定性降级

```python
from tracekit import resilient

@resilient(
    fallback=lambda exc, attempts: f"[降级答案] 第 {attempts} 次尝试失败：{exc}",
    retries=2, backoff=0.5,
)
def call_llm(prompt):
    return client.chat(prompt)   # 超时/限流/解析失败都不会让整个 Flow 停摆
```

三条设计取舍写在代码注释里：**节点失败不该停摆**，但每一次降级都会落进
Trace（`status=degraded` + reason）和降级账本里，**静默降级比崩溃更可怕**。

### 4. 回归评测

```python
from tracekit import EvalSuite, contains, not_contains

suite = EvalSuite(name="qa-flow", runner=my_agent)
suite.case("命中RAG时答到要点", "RAG 是什么", contains("检索增强生成"))
suite.case("不知道时不能编", "收益率如何", contains("无法回答"), not_contains("我认为"))
suite.case("空输入不该崩", "", contains("请输入"))

report = suite.run()
print(report.render())     # 通过率 / 每条不满足的断言
suite.save(report, "reports/latest.json")
```

内置套件覆盖了 Agent 应用最容易翻车的几类：说服力幻觉、泄漏内部堆栈、空输入崩溃、
LLM 挂掉后无兜底、答案超长。跑起来是 5/5 通过：

```
评测套件：tracekit-demo-flow
用例 5 个，通过 5，失败 0，通过率 100%
```

## 怎么接到真正的 PocketFlow 上

PocketFlow 的 Node 契约是 `prep(shared) → exec(...) → post(...)`，外层靠 `node.run(shared)` 驱动。
最小侵入的接入方式是**包一层代理**，不去改它的源码：

```python
from pocketflow import Node, Flow          # 换成真实框架即可
from tracekit import TraceRecorder, instrument_node

rec = TraceRecorder(run_dir="runs", name="real-flow")
traced = instrument_node(MyNode(), rec)    # 行为一致，多了 Trace
```

仓库里的 `tracekit/demo.py` 用同样的 `Node / Flow` 契约实现了一个四节点示例
（`classify → retrieve → draft → verify`），所以把 `build_flow` 里的节点换成真实节点就能平滑迁移。

## 设计取舍

| 决定 | 理由 |
|---|---|
| 不用 OpenTelemetry | 要给 100 行零依赖的框架加观测，SDK 本身比框架还重；这里用 JSONL 就够了，需要时再做 exporter |
| 输入/输出存指纹而非原文 | 原文会让 Trace 迅速膨胀且 diff 全是噪音；存 sha256 + 截断预览，够回答问题又不失控 |
| diff 不比耗时 | 耗时必然抖动，比了只会制造假警报；要做性能回归请单独记录和做统计检验 |
| span 要显式 `set_outputs` | shared state 里通常塞着整个对话历史，全量落盘既浪费也让对比失去焦点 |
| 评测用数据断言而非 pytest | 产品同学也写得动；结果可序列化，能和 Trace 一样按版本归档后 diff |
| 降级不抛异常 | Agent 的优雅降级就是要能继续往下跑；同时必须把"降级了"显式记录下来 |

## 目录结构

```
pocketflow-tracekit/
├── tracekit/
│   ├── trace.py           # 采集：Span / TraceRecorder / 输入指纹
│   ├── replay.py          # 回放：LoadedRun / diff_runs / 结构化差异
│   ├── fallback.py        # 降级：@resilient 装饰器 + 降级账本
│   ├── evalkit.py         # 评测：Case / Check / EvalSuite / Report
│   ├── adapters.py        # 适配：把任意 Node/Flow 无侵入接进 Trace
│   ├── demo.py            # 示例：PocketFlow 契约的四节点研究流程
│   ├── examples_suite.py  # 内置回归用例（含幻觉/降级/崩溃场景）
│   └── cli.py             # 命令行：demo / ls / show / diff / eval
├── tests/                 # 36 个单元测试
└── .github/workflows/regression.yml
```

## 测试

```bash
python -m unittest discover -s tests        # 36 个用例全绿
python -m tracekit eval                     # 5 个回归用例通过率 100%
```

## 路线图

- [ ] OpenTelemetry exporter：把 span 转成标准 OTLP，接到 Jaeger / Grafana
- [ ] HTML 时间线报告：把 `trace.jsonl` 渲染成可分享的单页火焰图
- [ ] prompt 版本登记：把 git commit + prompt hash 自动写进 summary metadata
- [ ] 评测并发执行与 pass@k 统计
- [ ] guardrails：基于 span 输出指纹做成本/延迟/毒性拦截

## License

MIT
