# 剖切操作转 JSON：已实现的范围与接入方式

这个模块把**按协议记录的绘图操作**还原成最终视图和剖切面 JSON。它也能转换现有 `Drawing.save()` 写出的 `.state.json`，但这种结果只是 AI 可修改的声明，不能当成已经核实的剖切证据。

当前主流程已接入绘图工具自动日志、提交捕获和最终几何核验，再生成评分 JSON，见 [三项指标](METRICS.md)。以下说明的是仍保留的旧离线转换器：它不能从任意 Python 源码、终端输出或 OCP 调用中完整恢复剖切操作，也不能独立产生可信分数。

## 1. 输入和输出

输入有两种，必须明确选择：

| 输入 | 可以得到什么 | 得不到什么 |
| --- | --- | --- |
| `benchcad-operation-event/1` JSONL | 按顺序重放创建、修改、删除，得到最终仍存在的视图和剖面，以及事件索引 | 未经过记录接口的操作；几何真实性 |
| 现有 `benchcad-cadquery-working-state/1` | 读取最终保存的普通视图、全剖面的坐标和保留侧 | 中间修改次数、失败操作、真实历史；状态与 DXF 是否一致 |

输出目录包含：

```text
converted/
  sections.json
  raw-operations.jsonl       # 或 raw-working-state.json，原始字节完整保留
```

输出目录必须不存在，程序拒绝覆盖旧记录。`sections.json` 保存原始输入的 SHA256 和大小。原始日志可能含模型输出或本地信息，必须留在私有运行目录，不直接上传到公开仓库。

在 Linux/POSIX 上，输出目录从创建时请求 `0700`，内部文件请求 `0600`（系统 umask 可进一步收紧）；Windows 的 mode 参数不能等同于 ACL 隔离。不存在或空的操作日志会转换失败，不会生成一个冒充成功的空结果。

输出中的每个剖面包含 `view_id`、`plane.point_mm`、单位法向量 `plane.normal`、`keep_side` 和证据索引。根字段包含 `case_id`、`input_sha256`、`units: mm` 和 `coordinate_frame: source_step`。输入长度可为 mm、cm、m、in；候选剖面位置统一换算为毫米，原始单位另保留在 `source.units`。不支持任意变换坐标系：调用方必须先转换到输入 STEP 的坐标系。

`views` 中保留的剖面也使用 `point_mm`；不会出现根字段写毫米、视图内仍用米的混合表示。

`keep_side = -1` 表示保留满足 `normal · (X - point) <= 0` 的一侧；`+1` 表示另一侧。法向量归一化但不翻转符号，以免改变保留侧。剖切平面法向量与纸面视图的观察方向不是同一个字段。

## 2. 与现有专家 GT 如何对应

已参考现有网页的 `draft.js`、`position.js` 和 `policy.js`。当前专家导出版本为 **`benchcad-section-position-draft/5`**，不是简单的一组固定平面。

| 专家 GT 的表达 | 候选 JSON 提供的信息 | 未来 scorer 的工作 |
| --- | --- | --- |
| `contains_axis` | 实际剖面的 `point_mm`、`normal` | 判断平面是否包含专家目标轴线 |
| `crosses_axis_at_station` | 同上 | 判断平面与轴的交点是否在要求的位置容差内 |
| `contains_lines` | 同上 | 判断平面是否同时包含两条目标线 |
| `angle_rule: unrestricted` | 仍记录实际法向量 | 不增加角度约束 |
| `angle_rule: plane_normal` | 实际法向量 | 按专家角度容差匹配；相反法向量表示相同无限平面 |
| `requirement: required / optional / one_of` | 实际完成的视图和剖面集合 | 分别处理必选、可选、组内替代要求 |
| `view_policy.reference_schemes` | 视图集合 | 在完整性和证据合格后再比较数量；当前不评分 |

**专家写的是要求，候选写的是实际结果。两份 JSON 不应长得一样，更不能按文本、数组顺序、视图名称或 DXF handle 判断相等。** 专家轴的编号不需要出现在 AI 输出里。匹配在 evaluator 一侧完成，专家轴数据不发送给 AI。

`prepare_comparison(candidate, expert)` 已实现配对接口检查：同一 case、同一输入 STEP SHA256、毫米、源坐标系，以及已知位置/角度规则。它返回 `compatible_inputs_matching_not_implemented`，分数为 `null`，证据资格为 `false`。这是接口兼容检查，不是完整 GT 审核或几何匹配。旧版 GT 需要先用现有标注工具迁移到 v5；程序不猜测旧字段含义。

v5 没有显式 `coordinate_frame` 字段；这里按照标注网页的现有定义，将其解释为输入 STEP 坐标系。另存、旋转或重新导出的 STEP 即使外形相同，只要哈希不同，也需要显式注册变换/对应关系，不能自动认定匹配。

当前候选实现只支持普通投影和单平面全剖。全剖沿用 GT 的 `view.type: full_section`、`category: major_views`。半剖、阶梯剖、局部剖需要记录多个平面、切除范围或其他几何，当前会明确拒绝，不会压成一个虚假的全剖面；GT 中相关要求可保留，不能据此声称当前候选已完整覆盖。

## 3. 事件协议

每行一个 JSON 对象，公共字段如下：

```json
{"schema":"benchcad-operation-event/1","seq":2,"operation":"view.created","round":1,"payload":{"view_id":"A_A","view":{"kind":"full_section","plane":{"point":[0,0,0],"normal":[0,1,0],"keep_side":-1}}}}
```

`seq` 从 1 连续递增。`round` 可省略；提供时必须为正整数且不能倒退。标识符使用 1–80 个字母、数字、下划线、点或连字符，首字符须为字母或数字。显示用中文标签不作为对象标识符。

第一条必须是：

```json
{"schema":"benchcad-operation-event/1","seq":1,"operation":"log.started","payload":{"case_id":"case_demo","source_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","units":"mm","coordinate_frame":"source_step","producer":"tool_adapter","coverage":"structured_calls_only"}}
```

上面的哈希和几何全部是合成示例，不代表任何真实 case。

| operation | payload | 含义 |
| --- | --- | --- |
| `view.created` | `view_id`, `view` | 创建新 ID，初始 revision 为 1 |
| `view.updated` | `view_id`, `expected_revision`, 完整 `view` | 替换视图声明，revision 加 1；不是部分字段合并 |
| `view.deleted` | `view_id`, `expected_revision` | 删除视图；被其他视图引用时拒绝 |
| `artifact.exported` | `dxf_sha256` | 绑定一次最终导出，必须是最后一个事件 |

`view` 包含 `kind: projection/full_section`，可包含 `parent_view_id`。全剖必须有 `plane.point`、`plane.normal`、`plane.keep_side`。已删除的 ID 不可复用，重新创建应使用新 ID；这样旧事件永远不会指向另一个对象。

输入检查包括非有限数值、零向量、错误引用、循环依赖、旧 revision、重复 JSON 键、未知字段/操作、乱序事件。JSONL 最大 16 MiB、10,000 个事件。它是轻量元数据转换，不需要重新执行 CAD；它不负责分析大段自然语言或推测代码意图。

`producer` 允许 `agent_declaration`、`tool_adapter`、`controller_capture`、`working_state_adapter`；这些都是**来源声明**，不是可信等级。`coverage` 为 `structured_calls_only` 或 `working_state_snapshot`。程序不接受输入自报 `verified: true`。

## 4. 现在就能调用

现有工作状态转换：

```sh
python tools/operations_to_json.py run/workspace/output/drawing.state.json \
  --working-state --case-id case_demo \
  --dxf run/bundle/drawing.dxf --output-dir run/section-conversion
```

结构化日志转换：

```sh
python tools/operations_to_json.py run/operations.jsonl \
  --dxf run/bundle/drawing.dxf --output-dir run/section-conversion
```

省略 `--dxf` 时不检查文件哈希。`matches_frozen_bytes` 仅表示事件声明的哈希与提供的 DXF 文件字节一致；它不能证明该文件真实含有声明的剖面。哈希不一致会记录 `mismatch`，不会悄悄改写声明。

CLI 的终端摘要也显示 `dxf_binding` 和 `scoring_eligible: false`。哈希不一致时仍保留转换结果供审查，所以退出码 0 只表示“转换完成”，不表示“文件一致”或“可以计分”。下游必须检查这些字段，不能只看退出码。

## 5. 如何接到受控绘图工具

`OperationRecorder` 已有实际 JSONL 写入接口；`drawing_view_created()` 可直接接现有 `Drawing.projection()` / `Drawing.full_section()` 返回的元数据。以下示例展示调用关系，**不意味着当前容器与控制程序已经建立了该信任边界**：

```python
from benchcad_runtime.operation_log import OperationRecorder

recorder = OperationRecorder(controller_log_path, {
    "case_id": case_id, "source_sha256": input_sha256,
    "units": "mm", "coordinate_frame": "source_step",
    "producer": "tool_adapter", "coverage": "structured_calls_only",
})

# 在工具成功返回后记录；操作失败留在原执行日志，不虚构成功事件。
view = drawing.full_section("A_A", point=(0, 0, 0), normal=(0, 1, 0))
recorder.drawing_view_created(view, round_number=round_number)

# 删除成功后记录相应 ID/revision。
drawing.delete_view("A_A")
recorder.append("view.deleted", {"view_id": "A_A", "expected_revision": 1},
                round_number=round_number)

# 最终成功导出且冻结后，由控制程序计算 frozen_dxf_sha256。
recorder.append("artifact.exported", {"dxf_sha256": frozen_dxf_sha256},
                round_number=round_number)
```

`controller_log_path` 必须位于 AI 不能修改的记录区。把 recorder 放进 AI 的 Python 里并写到工作目录，只能得到自报日志；只读 Python 工具代码也不足以防止同一解释器中的绕过或篡改。未来若要可靠操作覆盖，需要由控制程序接收结构化请求、运行受控工具、捕获结果并写记录，或在提交时由独立程序检查实际 CAD 状态。

现有 SolidWorks 版本使用原生文档的最终视图快照，并对不能恢复的剖切语义保留空值。这一原则在这里保留，但不复用 SOLIDWORKS 的 COM 对象、视图矩阵接口或文档格式。

## 6. 哪些工作还没完成

1. 在主循环/受控工具服务接入 recorder；现在只有可调用模块与转换命令。
2. 从任意 CadQuery/OCP/ezdxf 代码获得完整、不可伪造的剖切记录。当前不支持，也不承诺仅靠日志文本就能解决。
3. 几何核验与最终 DXF 一致性检查。当前所有候选均为 `unverified_declaration`，无论来源标签写什么。
4. 半剖、阶梯剖、局部剖的完整几何协议。
5. 依据专家 GT 的位置/角度/要求做匹配和打分；碰撞评分也未实现。

这些缺项均不妨碍保存和分析已经记录的操作，但不能把“JSON 成功生成”报告成“专业剖切评分已经可用”。

## 7. 离线检查

```sh
python -m unittest discover -s tests -p test_operation_log.py -v
```

测试使用合成几何和临时文件，不读取真实专家答案、运行日志、API 配置或 CAD 文件。
