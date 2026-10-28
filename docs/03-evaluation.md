# 03 测试与评估

## 阶段 01 预期

- 强制目录和文件存在。
- Python 包可以导入。
- CLI 帮助和版本命令可运行。
- README 包含安装、运行、测试和评审入口。
- `.env.example` 不包含真实密钥。

## 实际结果

2026-10-28 实际执行结果：

- `python -m pytest tests/test_stage_01_structure.py -v`：8 项通过，0 项失败，0 项跳过。
- `python -m pytest tests/ -v`：8 项通过，0 项失败，0 项跳过。
- 依赖安装、可编辑包安装、CLI 帮助和版本入口均验证成功。

完整记录位于 `examples/test-results/stage-01.txt`。

## 后续评估

业务场景、模型决策、失败处理和幂等性将在后续阶段加入测试和实际运行记录。

## 阶段 02 预期

- 合法输入被规范化为领域模型。
- 缺主题、预算或截止时间时返回稳定错误码。
- 非法、负数或非有限预算被拒绝。
- 非法、无时区或已过期的截止时间被拒绝。
- 非法任务 ID 被拒绝，缺省任务 ID 自动生成。
- 无效输入始终保持空步骤和零费用。

## 阶段 02 实际结果

- `python -m pytest tests/test_stage_02_validation.py -v`：26 项通过，0 项失败，0 项跳过。
- `python -m pytest tests/ -v`：34 项通过，0 项失败，0 项跳过。
- 已验证完整合法输入、全部必填字段缺失、预算边界、时间格式和时区、过期截止时间、任务 ID 及无副作用错误结果。

完整记录位于 `examples/test-results/stage-02.txt`。

## 阶段 03 预期

- 四类工具可独立成功调用。
- 工具费用和耗时与合成数据表一致。
- 相同输入产生可复现结果。
- 标准方案和经济方案分别估算为 12 和 8。
- 每种工具均可注入一次失败，并在下一次调用恢复。
- 部分成功明确记录已成功和未满足的部分。
- 方案只能保存在批准目录内，保存内容与输入一致。

## 阶段 03 实际结果

- `python -m pytest tests/test_stage_03_tools.py -v`：16 项通过，0 项失败，0 项跳过。
- `python -m pytest tests/ -v`：50 项通过，0 项失败，0 项跳过。
- 已验证四类工具正常调用、费用和耗时表、结果可复现、12/8 方案估算、每种工具一次性失败、部分成功、保存内容和目录边界。

完整记录位于 `examples/test-results/stage-03.txt`。

## 阶段 04 预期

- 合法状态迁移成功，非法迁移被拒绝。
- 失败步骤不能直接变成成功步骤。
- 成功工具结果可以持久化并重新加载。
- 费用、耗时和工具调用次数只累计一次。
- Trace 字段完整、顺序连续且可以重新读取。
- Trace 中不出现敏感键值或敏感环境变量内容。

## 阶段 04 实际结果

- `python -m pytest tests/test_stage_04_state_trace.py -v`：12 项通过，0 项失败，0 项跳过。
- `python -m pytest tests/ -v`：62 项通过，0 项失败，0 项跳过。
- 已验证合法与非法状态迁移、失败步骤保护、显式重试、跨实例持久化、累计值只记一次、Trace 顺序和敏感信息脱敏。

完整记录位于 `examples/test-results/stage-04.txt`。

## 阶段 05 预期

- Mock 模型返回合法且可复现的工具动作。
- 调用工具、改选方案、停止和完成动作均可解析。
- 未知动作、未知工具、缺失参数、未知参数和非法类型被拒绝。
- 非法模型输出不会触达工具执行函数。
- 真实模型使用严格结构化输出，并再次经过本地校验。
- 未配置密钥或模型名称时返回明确说明，Mock 模式仍可离线使用。

## 阶段 05 实际结果

- `python -m pytest tests/test_stage_05_model_adapter.py -v`：25 项通过，0 项失败，0 项跳过。
- `python -m pytest tests/ -v`：87 项通过，0 项失败，0 项跳过。
- 已安装并检查 OpenAI Python SDK 2.54.0，确认 `responses.create` 接受实现所用参数；检查未发送网络请求。
- 已验证动作解析、工具与参数白名单、非法输出隔离、真实配置错误提示、严格结构化输出请求和 Mock 离线入口。

完整记录位于 `examples/test-results/stage-05.txt`。真实适配器测试使用假客户端，不证明真实模型决策质量或账号 API 可用性。

## 阶段 06 预期

- 正常任务经模型动作循环完成四类工具调用并保存方案。
- Trace 同时体现模型动作、工具观察、简要依据和下一动作。
- 工具调用由循环中的模型动作触发，不由测试直接串行执行。
- 不同工具观察会导致不同的后续模型动作。
- 达到最大决策步数或工具调用次数时明确返回未完成。
- 完成时步骤状态、费用、耗时、调用次数、状态文件和 Trace 一致。

## 阶段 06 实际结果

- 首次测试收集发现模型包遗漏导出 `ModelError`，在任何阶段 06 行为测试运行前完成修复。
- `python -m pytest tests/test_stage_06_react_loop.py -v`：最终 6 项通过，0 项失败，0 项跳过。
- `python -m pytest tests/ -v`：93 项通过，0 项失败，0 项跳过。
- 已验证正常闭环、真实工具触发、状态与 Trace 一致、不同观察改变动作、两类执行上限以及提前完成保护。

完整记录位于 `examples/test-results/stage-06.txt`。

## 阶段 07 预期

- 预算 10、标准方案 12 时禁止直接保存。
- Agent 观察超预算后选择经济方案 8，并在重新估算后保存。
- 所有方案超预算时返回未完成和实际预算缺口。
- 标准方案超时后能够选择时长 60 秒的经济方案，或在仍不可行时停止。
- 超预算、超截止时间或未重新估算的方案不能绕过保存保护。
- 首次估算和替代方案估算分别保留，Trace 清楚体现重规划动作。

## 阶段 07 实际结果

- 相关测试冒烟首次发现约束字段需要同时存在于 `ExecutionState`；修正后阶段 03/04/06 相关回归 34 项全部通过，再开始阶段 07 专项测试。
- `python -m pytest tests/test_stage_07_constraints.py -v`：最终 7 项通过，0 项失败，0 项跳过。
- `python -m pytest tests/ -v`：100 项通过，0 项失败，0 项跳过。
- 已验证 12→8 预算重规划、重新估算、无可行预算方案、快速时间方案、无可行时间方案及保存硬保护。

完整记录位于 `examples/test-results/stage-07.txt`。

## 阶段 08 预期

- 注入一次脚本失败后，模型观察可重试标志并成功重试。
- 脚本失败和重试期间，已成功素材结果保持不变。
- 不可重试错误返回未完成和具体工具错误码，失败步骤保持 `failed`。
- 部分素材成功只将实际找到的素材传给脚本生成。
- 重复失败不会突破最大步骤或工具调用次数。
- 未预期工具异常转换成结构化失败并写入 Trace。
- Trace 包含失败摘要、下一动作和基于观察的简要决策依据。

## 阶段 08 实际结果

- `python -m pytest tests/test_stage_08_failure_recovery.py -v`：5 项通过，0 项失败，0 项跳过。
- `python -m pytest tests/ -v`：105 项通过，0 项失败，0 项跳过。
- 已验证一次失败后恢复、成功素材保留、不可重试停止、部分成功真实数据传递、重试调用上限和未预期异常结构化。

完整记录位于 `examples/test-results/stage-08.txt`。

## 阶段 09 预期

- 同一任务执行两次只产生一份保存方案。
- 第二次执行不增加模型调用、工具调用、费用或耗时。
- 第二次返回与首次相同的最终结果。
- 相同任务 ID 下主题、预算或截止时间变化时返回冲突。
- 重复或冲突请求不修改原状态、Trace 和保存方案。
- 等价预算与等价时区时间使用相同输入指纹。
- 未完成任务重复执行返回原状态，不自动重复已成功步骤。

## 阶段 09 实际结果

- `python -m pytest tests/test_stage_09_idempotency.py -v`：7 项通过，0 项失败，0 项跳过。
- `python -m pytest tests/ -v`：112 项通过，0 项失败，0 项跳过。
- 已验证完成任务零副作用重复执行、关键输入冲突、等价输入规范化、文件内容和修改时间不变，以及未完成任务安全返回原状态。

完整记录位于 `examples/test-results/stage-09.txt`。自动续跑未启用，属于核心交付后的可选增强。

## 阶段 10 预期

- CLI 直接参数和 JSON 文件输入均可完成正常任务。
- 缺预算或截止时间返回退出码 2、结构化错误且不创建运行目录。
- 预算超限通过实际 CLI 触发 12→8 重规划和第二次估算。
- 测试配置可注入工具失败，Trace 记录失败及基于观察的成功重试。
- 重复任务 ID 不修改状态、方案和 Trace。
- 标准输出及可选输出文件均为完整可解析 JSON，人类摘要不污染标准输出。
- 不可行任务返回退出码 3；四类退出码保持稳定。
- README 的核心命令无需修改源码即可运行。

## 阶段 10 实际结果

- `python -m pytest tests/test_stage_10_cli.py -v`：10 项通过，0 项失败，0 项跳过。
- `python -m pytest tests/ -v`：122 项通过，0 项失败，0 项跳过。
- 已验证直接参数与 JSON 文件输入、完整 JSON 标准输出、人类摘要分流、自定义状态/Trace 目录、预算 12→8 重规划、失败注入重试、重复任务零副作用，以及 `0/2/3/4` 四类退出码。
- 真实模式配置错误测试显式移除模型环境变量，不发送网络请求；Mock 结果不作为真实模型质量证据。

完整记录位于 `examples/test-results/stage-10.txt`。

## 阶段 11 验收用例与实际材料

五组材料均由 `python examples/generate_stage_11_examples.py` 在临时隔离目录实际调用 CLI 生成：

| 场景 | 材料 | 实际结果与证据 |
|---|---|---|
| 正常完成 | `examples/normal/` | 退出码 0，四类工具成功，标准方案费用 12，方案已保存 |
| 缺少预算 | `examples/missing_input/` | 退出码 2，错误码 `missing_budget`，无状态、无方案、空 Trace |
| 工具失败 | `examples/tool_failure/` | 脚本首次失败、第二次成功；素材步骤结果保持 3 条 |
| 预算超限 | `examples/budget_exceeded/` | 观察预算缺口 2 后选择 economy，再估算为 8 并保存 |
| 重复执行 | `examples/repeated_execution/` | 两次输出相同，仅一份方案，状态/方案/Trace 内容和修改时间不变 |

动态决策证据至少有两组：`examples/tool_failure/trace.jsonl` 根据 `injected_failure` 与 `retryable` 选择相同参数重试；`examples/budget_exceeded/trace.jsonl` 根据预算缺口选择 `economy` 并再次调用成本估算。完整覆盖关系见 `examples/acceptance-matrix.json`。

## 阶段 11 测试结果

- `python examples/generate_stage_11_examples.py`：实际调用 CLI 生成五组材料成功。
- `python -m pytest tests/test_acceptance.py -v`：15 项通过，0 项失败，0 项跳过。
- `python -m pytest tests/ -v`：137 项通过，0 项失败，0 项跳过。
- 已验证五组材料完整、JSON/JSONL 可解析、正常保存、缺输入零工具调用、失败后结果保留、预算动态重规划、重复执行零副作用、两组不同观察改变动作，以及五份文档的真实路径引用。

完整记录位于 `examples/test-results/stage-11.txt`。阶段 11 门禁通过；阶段 12 的安全检查与 GitHub 交付尚未执行。

## 阶段 12 预期

- 常见真实密钥、私钥、个人目录和项目本机绝对路径扫描无发现。
- `.env`、虚拟环境、缓存、字节码和 `runtime/` 均被 Git 忽略。
- 包元数据声明 Python 3.11+ 和 OpenAI SDK 运行依赖。
- 全新隔离环境可按 README 安装，CLI 正常场景可完成。
- 阶段 12 交付测试和阶段 01～12 全量回归无失败、无跳过。
- 提交文件清单不含运行缓存或本地配置，公开仓库推送成功。
- GitHub URL 和最终 commit SHA 有实际记录。

## 阶段 12 实际结果

- 常见密钥、GitHub 令牌、AWS 密钥、私钥标记、本机用户目录和项目绝对路径扫描均无发现。
- `.env`、`.venv`、缓存、字节码、构建目录和 `runtime/` 忽略规则验证通过；项目根目录不存在 `runtime/`。
- 新建 Python 3.13.13 虚拟环境，升级到 pip 26.2.2，按 README 安装依赖和可编辑项目成功。
- 隔离环境确认 `content-agent 0.1.0`、OpenAI SDK 2.54.0 和 pytest 9.2.0，并实际完成一次离线 CLI 正常任务。
- `.venv\Scripts\python.exe -m pytest tests/test_delivery.py -v`：8 项通过，0 项失败，0 项跳过。
- `.venv\Scripts\python.exe -m pytest tests/ -v`：145 项通过，0 项失败，0 项跳过。
- 依赖许可证核对：OpenAI SDK 为 Apache-2.0，pytest 与 setuptools 为 MIT；项目自身许可证未选择并已在 README 说明。
- 目标公开仓库：`https://github.com/slg-frank/content-task-planning-agent`。精确推送 SHA 在最终交付回复中记录，因为阶段记录本身属于该提交。

完整记录位于 `examples/test-results/stage-12.txt`。提交前门禁全部通过。
