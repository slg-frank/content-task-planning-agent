# 内容任务规划 Agent

本项目用于实现一个帮助运营人员在预算和截止时间内完成内容活动方案的 Agent。项目按照 `task.md` 分阶段开发，每个阶段必须通过独立测试和全量回归测试后才能进入下一阶段。

当前状态：阶段 12（安全检查与 GitHub 交付）的提交前检查已完成。干净环境安装、CLI 冒烟、8 项交付检查和阶段 01～12 共 145 项回归测试全部通过；推送状态与最终 SHA 以交付回复为准。

## 环境要求

- Python 3.11 或更高版本
- Git
- Windows PowerShell、macOS Terminal 或 Linux shell

## 安装

在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .
```

在 macOS 或 Linux 上，将激活命令替换为：

```bash
source .venv/bin/activate
```

离线 Mock 模式不需要环境变量。真实模型模式需要在仓库外设置 `OPENAI_API_KEY` 和 `OPENAI_MODEL`，变量名称记录在 `.env.example`；真实密钥不得提交到仓库。未配置真实模型时应显式选择 Mock，系统不会静默切换模式。

## 运行

查看 CLI 帮助：

```powershell
content-agent --help
```

也可以直接使用模块入口：

```powershell
python -m content_agent.cli --help
python -m content_agent.cli --version
```

正常运行（以下命令使用离线、确定性的 Mock 模式）：

```powershell
python -m content_agent.cli run --topic "秋季新品内容活动" --budget 20 --deadline "2099-12-31T23:59:59+08:00" --task-id demo-normal --runtime-dir runtime
```

预算为 10 时，标准方案 12 超出预算，CLI 会改选并重新估算费用为 8 的经济方案：

```powershell
python -m content_agent.cli run --topic "秋季新品内容活动" --budget 10 --deadline "2099-12-31T23:59:59+08:00" --task-id demo-replan --runtime-dir runtime
```

测试一次脚本工具失败并观察自动重试：

```powershell
python -m content_agent.cli run --topic "秋季新品内容活动" --budget 20 --deadline "2099-12-31T23:59:59+08:00" --task-id demo-retry --runtime-dir runtime --failure-injection '{"script_generation":1}'
```

也可将输入保存为 UTF-8 JSON 文件，例如 `task-input.json`：

```json
{
  "topic": "秋季新品内容活动",
  "budget": "20",
  "deadline": "2099-12-31T23:59:59+08:00",
  "task_id": "demo-json",
  "model_mode": "mock",
  "runtime_dir": "runtime",
  "state_dir": "runtime/state",
  "trace_dir": "runtime/traces",
  "json_output": "runtime/demo-json-result.json"
}
```

然后执行：

```powershell
python -m content_agent.cli run --input task-input.json
```

命令行参数会覆盖 JSON 中的同名任务字段。`--state-dir`、`--trace-dir`、`--max-steps` 和 `--max-tool-calls` 可单独配置。重复执行相同任务 ID 和相同关键输入会直接返回原结果，不会追加 Trace、重复计费或重复保存。

真实模型模式不会静默降级，需要先在仓库外设置 `OPENAI_API_KEY` 和 `OPENAI_MODEL`，再增加 `--model-mode real`。离线 Mock 根据执行状态和工具观察确定下一动作，用于验证工程流程，不代表真实模型决策质量。

标准输出始终保留可解析的完整 JSON；人类可读摘要写入标准错误，便于脚本只消费标准输出。退出码如下：

| 退出码 | 含义 |
|---:|---|
| `0` | 已完成 |
| `2` | 命令行或任务输入错误 |
| `3` | 未完成或任务输入冲突 |
| `4` | 模型配置或系统错误 |

## 测试

运行阶段 01 测试：

```powershell
python -m pytest tests/test_stage_01_structure.py -v
```

运行阶段 02 测试：

```powershell
python -m pytest tests/test_stage_02_validation.py -v
```

运行阶段 03 测试：

```powershell
python -m pytest tests/test_stage_03_tools.py -v
```

运行阶段 04 测试：

```powershell
python -m pytest tests/test_stage_04_state_trace.py -v
```

运行阶段 05 测试：

```powershell
python -m pytest tests/test_stage_05_model_adapter.py -v
```

运行阶段 06 测试：

```powershell
python -m pytest tests/test_stage_06_react_loop.py -v
```

运行阶段 07 测试：

```powershell
python -m pytest tests/test_stage_07_constraints.py -v
```

运行阶段 08 测试：

```powershell
python -m pytest tests/test_stage_08_failure_recovery.py -v
```

运行阶段 09 测试：

```powershell
python -m pytest tests/test_stage_09_idempotency.py -v
```

运行阶段 10 测试：

```powershell
python -m pytest tests/test_stage_10_cli.py -v
```

运行阶段 11 验收测试：

```powershell
python -m pytest tests/test_acceptance.py -v
```

运行阶段 12 交付检查：

```powershell
python -m pytest tests/test_delivery.py -v
```

运行全部回归测试：

```powershell
python -m pytest tests/ -v
```

测试结果按阶段留档在 `examples/test-results/`。

## 验收材料

阶段 11 提供以下真实 CLI 运行记录：

| 场景 | 目录 | 核心证据 |
|---|---|---|
| 正常完成 | `examples/normal/` | 完成状态、标准方案、保存结果与完整 Trace |
| 缺预算 | `examples/missing_input/` | 退出码 2、结构化错误、空 Trace、无状态和方案 |
| 工具失败 | `examples/tool_failure/` | 脚本失败记录、基于 `retryable` 的重试、素材结果保留 |
| 预算超限 | `examples/budget_exceeded/` | 标准方案 12、预算缺口 2、改选并重新估算经济方案 8 |
| 重复执行 | `examples/repeated_execution/` | 两次输出相同，状态、Trace、方案内容与修改时间不变 |

每个目录包含输入、CLI 输出、运行记录和 Trace；成功场景还包含最终状态与保存方案。缺输入场景的 `trace.jsonl` 按实际结果为空，`run-record.json` 明确记录未创建运行目录。临时绝对路径被替换为 `${RUNTIME_ROOT}`，该归一化范围记录在每组运行记录中。

重新生成五组材料：

```powershell
python examples/generate_stage_11_examples.py
python -m pytest tests/test_acceptance.py -v
```

验收点到场景的映射见 `examples/acceptance-matrix.json`。

## 评审入口

- GitHub 仓库：[slg-frank/content-task-planning-agent](https://github.com/slg-frank/content-task-planning-agent)
- 原始考核要求：`readme.txt`
- 分阶段开发计划：`task.md`
- 项目需求：`docs/01-requirements.md`
- 方案设计：`docs/02-solution.md`
- 测试与评估：`docs/03-evaluation.md`
- AI 使用说明：`docs/04-ai-usage.md`
- 项目复盘：`docs/05-retrospective.md`
- 阶段测试：`tests/`
- 输入、输出、Trace 和测试记录：`examples/`

## 项目结构

```text
src/content_agent/   项目源代码
tests/               可执行测试
examples/            示例与测试结果
data/                可复现的合成素材、费用和耗时数据
docs/                需求、方案、评估、AI 使用与复盘文档
```

## 当前限制

- 默认 Mock 使用确定性、观察驱动策略，只证明 CLI、ReAct、工具、状态和 Trace 链路，不证明真实模型质量。
- 真实模型需要用户自行提供有效的 API 配置；自动测试不发送真实网络请求。
- 失败步骤自动续跑仍作为加分能力保留，默认重复未完成任务返回原状态。
- 当前为单机 JSON 文件存储，不提供并发锁、图形界面、线上部署或多 Agent 协作。
- 阶段 11 运行材料使用离线 Mock，不包含真实模型质量评估。
- 项目自身许可证尚未选择；公开仓库不等同于授权复用。

`task.md` 规定的阶段 01～12 核心开发与提交前门禁均已完成。

## 依赖与许可证

项目支持 Python 3.11 及以上版本。运行依赖为 OpenAI Python SDK `>=2.0,<3.0`，用于显式选择的真实模型模式；离线 Mock 不发送网络请求。测试依赖为 pytest `>=8.0,<10.0`。

阶段 12 核对环境中的实际版本与许可证如下：

| 组件 | 核对版本 | 许可证 |
|---|---:|---|
| OpenAI Python SDK | 2.54.0 | Apache-2.0 |
| pytest | 9.2.0 | MIT |
| setuptools | 82.0.1 | MIT |

项目自身许可证目前未选择。公开仓库并不自动授予复制、修改或再分发权；如需开放复用，应由仓库所有者在后续明确选择许可证。

## GitHub 交付

目标公开仓库为 `https://github.com/slg-frank/content-task-planning-agent`。安全扫描、干净环境安装、交付测试和全量回归均已通过。最终 commit SHA 以 `examples/test-results/stage-12.txt` 的说明和本次交付回复为准。
