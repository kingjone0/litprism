# AGENTS.md

Codex 从这里读项目约定。**完整交接文档是 `AGENT.md`（单数），先读它。**

## 这个目录是什么

`E:/codex-litprism` 是 `E:/aicoding/paper-research-agent` 的 **git worktree**，分支 `codex-work`。
两个目录共用同一个 `.git`，所以这里的工作可以正常 commit / diff / merge 回 `main`。

**不要**在这里复制源码另起一份，也不要删除或重建这个目录。

## 开工前必读

1. `AGENT.md` —— 进度、铁律、已知坑（它的信息是"实测过的现状"，不是设计意图）
2. `docs/实现计划.md` —— 分阶段计划与已完成项

## 这个项目的一条底线

卖点不是"解读"，是"**不编**"。三层防幻觉（多源核验真实性 / RAG+引用回溯 / 导入守卫）
是这个产品的核心价值，任何削弱它们的改动都是错方向。

## 环境

- Python：`C:/Users/18434/.workbuddy/binaries/python/envs/default/Scripts/python.exe`
  （**必须用全路径**，系统的 `python` 里没有 pytest）
- 测试：使用上述 Python 执行 `-m pytest tests` —— 当前 **639 passed / 1 skipped**；测试进程须清除 `LITPRISM_HOME`，改动前后都要跑
  - ⚠️ **不要加 `-q`**：pyproject 的 `addopts` 已有 `-q`，再传一次变 `-qq`，汇总行被吞掉、看起来像"没跑"，但退出码是 0
- 不要假设 `node_modules` / `packaging/dist` 已生成，需要时自己生成（当前不做打包）：
  - `npm install`（桌面外壳用，约 400MB）
  - `python packaging/build.py`（打 sidecar exe，约 2.5 分钟）

## 运行数据必须隔离

知识库和窗口缓存是**按机器**存的，不是按目录。在本目录跑应用时必须指定独立数据根，
否则会和主仓共用一个 SQLite：

```bash
LITPRISM_HOME=E:/codex-litprism-data
```

## 怎么验证（重要）

这是个 **GUI 应用**，而 Codex 是终端环境，**不要试图去开窗口验证**。有效的验证路径是：

```powershell
# 1. Python 侧单测（仅当前测试进程，不修改用户全局环境）
Remove-Item Env:LITPRISM_HOME -ErrorAction SilentlyContinue
& 'C:/Users/18434/.workbuddy/binaries/python/envs/default/Scripts/python.exe' -m pytest tests

# 2. 直接起后台，用 curl 断言 API
$env:LITPRISM_HOME='E:/codex-litprism-data'
$env:PYTHONPATH='src'
& 'C:/Users/18434/.workbuddy/binaries/python/envs/default/Scripts/python.exe' -m litprism.sidecar --port 8899
# 输出一行 LITPRISM_READY {...}；在另一终端验证，ping 免 token
curl.exe -s --noproxy '*' http://127.0.0.1:8899/api/ping
```

`curl` 打本机端口**必须加 `--noproxy '*'`**，否则会被系统代理劫持、看起来像"服务没起来"。

## 已知坑（踩过，别重复）

- **排查前先验证观测手段**：uvicorn 的访问日志默认写 stdout，而 sidecar 的 stdout 是握手管道。
  曾经因此两次误判"前端零请求"。规则：先 `curl` 一发，确认日志里能看到，再谈现象。
- **onefile 是两段进程**（启动器 + 真进程）。先杀启动器会让真进程成孤儿占着端口，
  必须先用握手行里的 `pid` 杀。
- **本机 Chromium 的 GPU 进程必崩**（0xC0000005），桌面壳必须关 GPU 与沙箱；
  跑 electron 前要 `env -u ELECTRON_RUN_AS_NODE`（宿主环境设了这个变量）。

## 提交

改完**不要自行 commit**，把改动讲清楚，等人确认。用户明确要求“提交”即授权本轮提交，无需重复确认；不包含 push 或打包。

## 当前接手边界（2026-10-18）

- 当前 schema v5。新增来源快照、人工审核与语义复核记录；不要为检查配置而打开用户主知识库触发迁移。测试和真实服务验收使用独立子数据根。
- 回复来源链接与右栏审核已接通。机器支持不等于人工确认；待审、冲突、撤销和来源失效不能作为正常结论进入正文、对比或导出。
- 长论文按物理页保留原文定位，复杂表格无法恢复时明确待核对；旧文献需重新导入，不能自动重写旧块 ID。
- 有界语义复核覆盖解读六字段及对比，不能将其保证扩展到术语、问答或 Skill。严格 JSON、来源白名单、预算上限、失败保留待审均不能削弱。
- 真实固定样本 18 条两轮与 5 个差异控制例通过有限门槛；fastText 全流程 22 条中 19 条机器支持、3 条待审，整篇仍 `passed=false`。优先处理相邻原文不足、误报与成本，不能宣称长论文整体语义已通过。
- 不增加本地 embedding、不装模型、不建向量库；F3 与打包继续暂停。扩大付费调用前先明确验收目标和调用预算，实测入口及边界见 `docs/有界复核与短论文实测-2026-10-18.md`。
- 密钥仅走 SecretStore；不得写入源码、文档、日志或提交。`_selftest.*` 下的 PDF、原始调用、数据库与截图仅留本地，不能强制加入 Git。
- UI 兼容性使用 `tools/check_workflows.py` 的无窗口流程验证；语义审核和撤销也须覆盖。不要启动可见桌面窗口。
