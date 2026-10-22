# BenchCAD 3D2D CadQuery

工作台已支持按结果包自动导入多个 case 和同一 case 的多次运行。
[结果包与同步协议](docs/WORKBENCH_RESULTS.md)说明自动收尾、仅重试上传、发布白名单和同机部署。

当前是最小可用的开发试点：自定义 Responses API agent 在 Linux Docker 中
完成 STEP→DXF；可选第二个独立 episode 仅根据冻结 DXF+PNG 重建 STEP。
被测 agent 不是 Codex CLI。Windows 仅用于开发与离线单测。

简化入口见[部署、验收与运行](docs/QUICKSTART.zh-CN.md)，
完整日志的保存与分析见[日志说明](docs/LOGGING.md)。
通用出图、重建要求独立放在[任务定义](benchcad_cadquery/tasks/README.md)；
仓库只附带一个[真实 Case13 输入示例](examples/case13/README.md)，不包含专家答案和运行日志。
运行时用 `--step examples/case13/model.step` 选择它；参数沿用 `config.example.toml`。
[旧操作 JSON 转换](docs/OPERATION_LOG.md)仅提供未核实声明。当前工具日志在提交时冻结，经最终几何核验后用于专家评分；复杂视图和 SW 原生证据仍有覆盖限制。

[三项 Metric 中文设计](docs/METRICS.zh-CN.md)说明重建一致性、专家偏好和文字碰撞如何相乘，以及边缘接触、扣分和缺失证据处理。可下载 [HTML 讲解](docs/METRICS.zh-CN.html) 离线阅读。

Linux 主机需要 Python≥3.11、非 root 账户及本机 Docker 权限。
控制器只需 Pillow/tiktoken；CAD 依赖安装在 worker 镜像中。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[local-accounting]'
docker build -t benchcad-cadquery:local sandbox
docker build --build-arg CAD_IMAGE=benchcad-cadquery:local -t benchcad-cadquery:evaluator evaluation
export TIKTOKEN_CACHE_DIR="$PWD/.runs/tokenizer-cache"
.venv/bin/python tools/provision_tokenizer.py --cache-dir "$TIKTOKEN_CACHE_DIR"
.venv/bin/python -m benchcad_cadquery.cli doctor --run-dir .runs/doctor-001
.venv/bin/python -m benchcad_cadquery.cli smoke --run-dir .runs/smoke-001
```

构建/词表准备可联网，运行计数器只读已校验的离线词表。
`smoke` 是参数化板件的可信脚本回放，不是模型成绩；包含真实全剖、
原生尺寸、DXF 校验/渲染/冻结与独立重建流程。

部署后，在控制器环境设置 `OPENAI_API_KEY`、可选 `BASE_URL`
（兼容 `OPENAI_BASE_URL`），明确指定模型，无自动回退：

```sh
.venv/bin/python -m benchcad_cadquery.cli run \
  --step /data/input/model.step --run-dir .runs/run-001 \
  --model gpt-6-sol --image benchcad-cadquery:local --reconstruct \
  --iou-vendor /data/tools/benchcad-2-main
```

默认每阶段最多 60 个决策响应、7200 秒，每次代码执行最多 300 秒，
1 CPU/4096 MiB。可用 CLI 或显式 `--config config.example.toml` 调低预算。
每次 API 回复默认最多 500,000 个输出 token（包括推理），不是整阶段总量。
历史运行保留原预算；明确授权的开发续跑累计计算此前轮次和有效时间。
500K 是请求上限，不代表所有模型服务都支持。服务商拒绝时记录配置错误，
不静默降低上限或切换模型；需要明确设置服务商支持的参数。
2 小时是每阶段上限：出图和独立重建分别计时，评分使用单独的超时限制。
每个运行目录必须全新；每次代码执行使用全新容器。worker 无网络、
无 key、无主机 home/GT/Docker socket，系统只读，降权运行。
绘图与重建不共享上下文、代码、日志或 STEP。可修复的提交失败会在原有预算内反馈给模型。

结果见 `summary.json`、各阶段 `episode/`、`executions/`、`bundle/`。
冻结包只包含 DXF+PNG 或 STEP 及哈希 manifest；工作状态明确不可信。
主计量为 `local_visible_text`，不是完整计费 token；provider usage 仅诊断，
`actual_fee=null`。专家图纸重建基线关闭，IoU 在独立评分环境运行。
新增[三项评分](docs/METRICS.md)：重建一致性、专家偏好、标注碰撞均为 0～1，最终分数为三者乘积。
用 `--expert-json` 配置评测端专家 JSON；缺少专家文件、操作日志或几何核验时保留未评分，总分不补值。
配置 `--iou-vendor` 后，程序会自动衔接独立评测、合入评分并生成 `report.html`；不再需要人工分别执行评分和汇总。未配置时明确显示未启用评测；失败不伪造零分。算法与依赖准备见 [归一化 IoU](evaluation/README.md)。API 请求受剩余阶段时间限制。

离线测试：`python -m unittest discover -s tests -v`。本次单测不能证明
Docker 几何运行成功或文件访问隔离；服务端需实际 smoke 和 deny probes。
工具提供有限主视投影/全剖辅助；半剖、阶梯剖需真实 OCP 构造，未提供 helper。
不宣称工程完整、日志内容真实或对抗隔离已验证。
持久工作区尚无磁盘配额，部署时需专用限额文件系统；超时后的强制清理有
有限宽限时间，daemon 故障或控制器崩溃仍需运维核查。

详见 [中文八节 Agent 规范](docs/AGENT_SPEC.zh-CN.md)、
[英文规范](docs/AGENT_SPEC.md)、[英文安装说明](README.md)。
