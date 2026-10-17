# 用三条命令准备、检查和运行

本项目用自己的 API Agent 驱动 CadQuery。下载仓库后，在 Linux 主机准备一次环境，再检查，最后运行指定的 STEP；无需安装 Codex CLI 或 SOLIDWORKS。

这不是零前提的“一键安装”：主机必须已有 Python 3.11 或更新版本（含 `venv`）、Docker，以及可以访问本机 Docker 的非 root 用户。首次准备需要联网下载依赖和镜像。脚本不会安装 Docker、调用 sudo、修改系统权限，也不会自动开始付费测试。请预留足够内存给主机和其他服务；默认每个 CAD 容器上限为 4 GiB、1 CPU，并不是整台主机只需要 4 GiB。

## 1. 准备环境

在仓库根目录执行：

```bash
python3 tools/benchcad.py setup
```

这条命令创建项目 `.venv`，安装控制程序及本地 token 统计依赖，下载并校验词表，构建 `benchcad-cadquery:local` 镜像。镜像构建只使用 `sandbox/`，不会把仓库数据、API Key 或运行日志打进镜像。

重复执行会复用 `.venv` 和有效词表，重新检查依赖并构建镜像；不会删除已有运行。准备阶段不需要 API Key。此入口固定连接本机 Docker，不使用远程 Docker 上下文或已有 Docker 登录配置。

## 2. 检查环境与完整流程

```bash
python3 tools/benchcad.py check
```

先检查 Linux、权限、词表和容器依赖，再用一个固定零件及固定代码，走通出图、冻结 DXF/PNG、独立工作目录重建 STEP 的流程。结果保存在新建的 `.runs/doctor-日期-随机编号/` 和 `.runs/smoke-日期-随机编号/`。

检查不调用模型 API，不消耗模型 token。固定代码已经知道样例尺寸，因此检查成功只说明程序流程能运行，不能当作 AI 的重建能力或 benchmark 分数。检查失败时先保留目录并排查，下一次检查会创建新目录。

## 3. 运行自己的 case

在当前终端安全设置 `OPENAI_API_KEY`，不要写进命令参数、仓库文件或提交到 Git。可使用交互输入：

```bash
read -rsp 'API Key: ' OPENAI_API_KEY; echo
export OPENAI_API_KEY
python3 tools/benchcad.py run \
  --step /data/cases/case_013/model.step \
  --model YOUR_MODEL_ID \
  --base-url https://api.openai.com/v1 \
  --run-dir .runs/case013-first \
  --reconstruct
unset OPENAI_API_KEY
```

将 STEP、模型 ID 和网址替换为自己的配置。接口必须兼容 Responses API，模型须支持图片输入；不会自动替换模型。只有这一步会调用付费 API。Key 留在可信的控制程序侧，不传给 CAD 容器。

`--run-dir` 必须是尚不存在的目录，防止覆盖记录。`--reconstruct` 表示出图成功后启动新的重建上下文；重建 Agent 只拿到冻结的 DXF、PNG 和工具说明，不拿原始 STEP 或出图对话。省略它则只运行出图。

默认每阶段最多 30 个模型决策回复、1800 秒；每次执行最多 300 秒。回复格式错误也占决策轮，传输失败另计。到上限仍未有效提交就记录未完成。`submit` 执行或最终校验失败会结束该阶段，不自动继续修复。

需要调整参数时：

```bash
.venv/bin/python -m benchcad_cadquery.cli run --help
```

也可以显式传入 `--config config.example.toml`；配置文件不放 Key。当前入口有开发试验预算上限，不能靠增大参数无限运行。

## 看结果

先看运行目录中的 `summary.json`，再看 `drawing/episode/episode.json` 和可选的 `reconstruction/episode/episode.json`。成功提交的文件位于各阶段 `bundle/`，不是工作目录里任意一个碰巧存在的文件。

记录的内容与局限见 [LOGGING.md](LOGGING.md)。启动检查、模型成功提交、几何评分是不同的验收步骤；不要把环境检查或产出文件当作满分。剖切与碰撞评分接口的空值表示尚未评测。
