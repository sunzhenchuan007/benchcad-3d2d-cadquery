# 本地可见文本 token 统计

episode 主指标为 `usage_metric: local_visible_text`，汇总在 `local_visible_text`；
`local_usage_records` 按调用尝试顺序保存记录。`local_visible_text.usage_complete` 只表示本地可见文本记录完整，
不表示真实 API 用量或账单完整。旧顶层 `usage_records`、`usage_complete` 仍属于 provider 诊断，
`provider_usage` 使用原有 `summarize_usage` 汇总；两套数值不应混用。

使用可选依赖 `local-accounting`（固定 `tiktoken==0.12.0`）。编码明确为 `o200k_base`，
仅是未经目标模型验证的 **PROXY**，不是模型实际 tokenizer 的保证。
每条记录保存编码、版本、模型及 `model_encoding_verified: false`。
加载器只读取已有词表缓存并校验 SHA-256，按 `TIKTOKEN_CACHE_DIR`、`DATA_GYM_CACHE_DIR`、
系统临时目录下 `data-gym-cache` 的顺序选择缓存目录；不下载、不修复缓存。
缺库、版本不符、缓存缺失或损坏均明确报错，不回退到字符数除以 4。

计数方法：每次实际请求构建 payload 后，将 `instructions` 和每个消息文本 part **分别编码后求和**。
全部历史每次重发都重新计数，包括先前 assistant 回复、执行反馈和重复图片。
输出对每个可见文本／拒绝文本 part 单独计数；用于展示而插入的连接换行不算此次输出 token，
若它随历史再次发送，则属于下一次输入。特殊 token 外观的字符串按普通文本编码。
图片 URL、base64 和图片内容不计文本 token；记录 `image_count`，`image_tokens` 始终为 null。
文本 part 内的普通 URL 仍属于文本。

空字符串或仅空白的系统指令在调用前拒绝，避免 relay 注入大量默认指令。
`system_prompt=None` 仍使用 episode 的明确默认指令，并完整计数。
未完成／拒绝响应的可见文本照常保存、计数和纳入后续历史，但禁止执行。
传输超时、worker 失败、响应解析失败尽量保留发送前输入估算；是否被服务端处理未知，
输出为 null。缺记录不会当作零；汇总只提供已知小计，完整性为 false。
ReplayModel 不产生真实请求，本地用量保持未知。

隐藏推理、服务端隐藏指令、API 消息封装、缓存命中、图片 token、总计费 token 和实际费用均未知。
客户端只能计数自己实际发送及收到的可见文本，无法判断 relay 是否另加内容。

可向 `run_episode(..., rates=...)` 或 `summarize_local_usage` 显式传入
`input_per_million`、`output_per_million`，以及 `model`、`date`、`source_url`。
费率必须有限且非负；模型须与已知请求／响应模型 ID 严格一致（不猜测别名映射）。
不内置或推断价格，不在线验证来源。仅记录完整时生成：

`visible_text_reference_cost_usd = (本地输入 tokens × 输入费率 + 本地输出 tokens × 输出费率) / 1_000_000`

这是可见文本参考成本，**不是实际账单**，不应用缓存折扣；费率缺失或记录不完整时为 null。
原 `summarize_usage` 的 provider 费用诊断接口保留，episode 不将它当作主成本。

Token 专项离线测试：`.venv/Scripts/python.exe -B -m unittest discover -s tests -p test_token_accounting.py -v`。
API worker 全部模拟，不使用真实凭据或网络；无词表缓存时真实 tokenizer 用例跳过，
其余用模拟编码器检验计数边界、累计、费用和 transport／episode 集成。
