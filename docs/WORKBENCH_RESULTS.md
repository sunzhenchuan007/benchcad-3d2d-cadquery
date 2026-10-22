# 工作台结果包与自动同步

工作台按 `(case_number, run_id)` 导入记录。一个 case 对应一张卡片，图下列出它的多次运行。相同模型在不同 case 可以使用同一个 run ID；同一 case 的不同运行必须使用不同 ID。

## 完成后的流程

1. 控制程序结束出图、独立重建和评分，先保存本地报告。
2. 可信后处理从冻结产物生成图纸预览、精简视图操作和重建对比。预览生成不调用模型，不改评分。
3. 导出 `workbench-result/`，检查白名单、统计字段、敏感字符串、图片元数据和文件哈希。
4. 调用配置的同步命令；接收端再次检查，按 case/run 导入并重建工作台。
5. 发布适配器只同步审核后的静态文件，以新版本切换网站；旧版本保留。

模型、评分与发布是三个独立阶段。发布失败记录为 `pending_retry`，不改测试成绩；重试入口只接收已冻结的结果包，不接受模型配置或 CAD 输入。

## 结果包 v1

```text
workbench-result/
  manifest.json          必需：schema、case_number、run_id、文件大小与 SHA-256
  run.json               必需：白名单统计，不含原始运行配置
  view-operations.json   必需：最终保留视图，或明确的 unavailable/needs_review 状态
  drawing.png            可选：冻结 DXF 的 RGB 预览
  comparison.png         可选：原模型与重建模型的对比预览
  geometry.json          可选：已核对 IoU 的体素/表面显示缓存
```

`manifest.schema` 固定为 `benchcad-workbench-result/1`。额外文件、路径穿越、符号链接、篡改或超过大小限制的文件都拒绝导入。缺少的预览不会被伪造；没有评分时使用 `null`，不能写成零分。

`run.json` 包含 case 编号、标题、run ID、运行名称、模型、Agent、接入渠道、预算、阶段状态、时间、token 及统计口径、IoU 和重建分。真实费用未知时为 `null`。剖切、碰撞、综合分尚未实现，保留空值和试跑标记。CLI 的缓存输入已包含在输入总数中，推理输出已包含在输出总数中，不能重复相加；未知模型回复轮数保留 `null`。

`view-operations.json` 仅表达最终保留的已记录视图，包括投影类型、观察方向及剖切面的点、法向与保留侧。它不上传源代码或修改历史。工作状态可被模型修改，且任意手工图线未必被记录，因此该文件不能冒充完整可信的制图操作审计。状态缺失不等于零个视图。

`geometry.json` 只接受规定的数值和网格缓冲区；体素交并集必须复现本次原始 IoU。网站用自己的查看器模板生成 HTML，不接受结果包中的任意 HTML 或脚本。

结果包是不可变快照。重复导入同一包不新增卡片或运行；同一 case/run ID 对应不同内容会拒绝导入，避免覆盖历史。如需发布修订版，明确使用新的 run ID。不要直接修改已同步的包。

## 控制程序调用

自建 API 控制程序增加以下参数，其余模型、CAD 和评分参数沿用原运行方式：

```text
--case-number 5 --case-title "Case 05" --run-id trial-001
--workbench-config <controller-private>/sync-config.json
```

在保存本地报告后自动调用 `finalize_runtime()`。不指定 case 编号就不自动公开或猜测案例身份。工作台参数仅属于控制程序，不交给模型。后处理对比图使用单独受限执行器；后处理失败不改已保存成绩。

Codex CLI harness 在两阶段和评分结束后调用：

```python
from benchcad_runtime.workbench import finalize_cli

finalize_cli(report_dir, case_number=5, run_id="trial-001",
             title="Case 05", config=private_sync_config,
             comparison=comparison_png, geometry=display_geometry_json)
```

也可处理已有 CLI 结果；此命令只读取结构化汇总与冻结图纸，不读取登录文件、不发起模型运行：

```text
python tools/workbench_result.py export-cli --run-dir <report-dir> --case-number 5 --run-id trial-001 --comparison <comparison.png> --geometry <display.json> --config <private-sync-config>
```

## 同步命令与重试

同步配置由控制程序明确指定，不自动搜索认证文件。配置是本地私有文件，不能进入结果包或公开仓库。

```json
{
  "command": ["python", "<workbench>/sync_results.py", "--package", "{package}", "--publish-config", "<private>/publish-config.json"],
  "timeout_seconds": 1200
}
```

接收端 `sync_results.py --package ...` 验证、幂等导入和构建页面；不提供发布配置时只导入本地，不代表已经上线。发布配置的 `commands` 是可信的命令参数数组，依次执行审核、发布与验证；不经 shell。它不能来自 Agent 或结果包。

上传失败后，结果包保持不变，旁边的 `*-sync.json` 保存状态与尝试次数。自动收尾最多尝试三次；后续只执行：

```text
python tools/workbench_result.py retry --package <frozen-package> --config <private-sync-config> --attempts 3
```

接收端进程锁随进程退出自动释放。传输命令失败或超时都保留可重试状态，错误输出不复制到公开页面。同一包对同一目标同步成功后再次调用不上传；更换目标配置可以重新同步。

## 同一台服务器如何存放

可以将运行环境与网站放在同一台服务器，仍必须分开目录和权限：

```text
私有运行目录 ──可信导出──> 私有结果包队列 ──检查/构建──> 私有网站发布目录
                                                              │
                                                        原子切换发布版本
                                                              ↓
                                                        网站可访问目录
```

网站服务只读取发布版本；不能把运行根目录、日志目录或结果包队列作为网站目录。Agent 只获得阶段工作区，不能访问同步配置、专家参考或私有评测目录。

同机部署可用 `--site-root <private-release-staging>` 输出经过检查的工作台文件，再由本机发布命令切换版本；无需将原始数据传过公网。跨机部署仅将同一批审核文件交给上传适配器。同步协议与页面数据格式不因机器位置而改变。本次没有迁移现有运行服务。

## 发布边界与验证

只公开审核后的图片、精简 JSON、统计和显示网格。原始 CLI/API 日志、会话归档、源 STEP、账号资料、凭据、个人路径、服务器连接信息均不进入包。PNG 重新编码并检查元数据。图像中可见文字仍应人工抽查，文本扫描不是对图像像素的 OCR 审计。

测试覆盖重复导入、跨 case 同名运行、不可变冲突、文件篡改、额外日志、路径和凭据特征、PNG 元数据、超时和只重试同步。桌面浏览器还验证分组卡片、逐次运行产物、视图 JSON、时间/token 和 3D 配色。
