# AGENT.md · 文献棱镜 LitPrism 交接速查

> **给接手的 agent 看的第一份文档。**
> 这里记录的是**实测过的现状**，不是设计意图。凡标「推断」的都是未验证的，请先验证再行动。
> 最后更新：2026-10-18（553 项测试通过；CommandCode DeepSeek V4.1 Flash 真实短任务通过；提供方兼容性补修完成并纳入提交，未重新打包，F3 暂停）

---

## 0. 三句话读懂这个项目

1. **它是什么**：面向个人学术研究的论文知识解读 Agent —— 导入 PDF → 结构化解读（研究问题/方法/实验/结论/创新点/局限）→ 多轮问答（带引用）→ 跨论文对比 → 文献推荐。
2. **它真正的卖点不是"解读"，是"不编"**：三层防幻觉（多源核验文献真实性、检索增强问答+引用回溯、导入守卫）。任何改动只要削弱这三层，就是错的方向。
3. **它现在到哪了**：A–E 已有内核与主要界面流程，但不能视为全部验收完成；2026-10-18 修复审查发现的可信度与接线缺口，547 项测试及真实本地 API 离线流程通过。真实模型长会话/学术质量、部分体验仍待验收，D3 暂缓、F3 暂停、W1–W3 未实现。

---

## 1. 环境与命令（照抄即可）

```bash
# ⚠️ 系统 python 没有 pytest / fastapi，必须用这个 venv
PY="C:/Users/18434/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
cd E:/aicoding/paper-research-agent

# 测试（当前：553 passed；路径单测要求测试进程不带 LITPRISM_HOME）
$PY -m pytest tests -q

# CLI
$PY run.py init
$PY run.py list
$PY run.py add sample_papers/ --verify
$PY run.py ask "这些论文在方法论上的核心差异是什么？"
$PY run.py serve            # http://127.0.0.1:8765

# 打包 sidecar（单文件 exe，约 139MB，耗时 ~2.5min）
# ⚠️ 必须用这个脚本，别直接调 pyinstaller —— 否则产物会落到 ./dist 而不是 packaging/dist
$PY packaging/build.py
./packaging/dist/litprism-sidecar.exe --port 8899

# 桌面外壳（Electron）
npm install                                  # 首次：装 @tauri-apps/cli + electron
env -u ELECTRON_RUN_AS_NODE ./node_modules/electron/dist/electron.exe desktop-electron/main.js
#   ↑ 必须去掉 ELECTRON_RUN_AS_NODE（宿主设的），否则 electron 以纯 Node 运行
# 自证模式：加 LITPRISM_SELFTEST=1，页面会把 DOM 摘要与截图写到 _selftest.json/.png

# Tauri 版（当前环境白屏，仅备选，见 §5.3）
npx tauri build --debug --no-bundle
#  → stdout: LITPRISM_READY {"port": 8899, "token": "...", "pid": null}
#  → 所有 /api/* 必须带 X-LitPrism-Token 头，缺了返回 401

# 本机代理会劫持 localhost 之外的解析，测本地端口务必加 --noproxy '*'
curl -s --noproxy '*' -H "X-LitPrism-Token: $T" http://127.0.0.1:8899/api/status
```

**当前没有真实 LLM Key 时会自动用 mock 模型**（输出标注 `mock`，不冒充真实结论）。Tavily 搜索 Key 已存在 SecretStore（实测有效），但联网检索代码尚未实现。

---

## 2. 文件地图（只列有判断价值的）

| 文件 | 职责 | 为什么重要 |
|---|---|---|
| `src/litprism/schema.py` | 22 张业务表 + `user_version` 版本化迁移（当前 **v3**） | 单一事实来源；迁移必须幂等、老库无损 |
| `src/litprism/refs.py` | 固定宽度 ref（`m00123`） | 容忍非规范输入、输出永远规范、区间写反自动纠正 |
| `src/litprism/invariants.py` | 9 条核心不变量 + 失败码闭集 | 配置 fail-fast 的判定表 |
| `src/litprism/verify.py` | **五态核验**（见 §4.1） | 防幻觉第 1 层 |
| `src/litprism/whitelist.py` | 人工确认放行（白名单**并存**于自动结论） | 与 verify 结论不覆盖，可审计 |
| `src/litprism/ingest.py` | PDF/DOCX/HTML/TXT/MD/TeX → 章节识别 → 分块（带页码） | 页码不准 = 引用回溯失效 |
| `src/litprism/extract.py` | 结构化解读 | 缺失字段留空，不补全 |
| `src/litprism/agents.py` | 问答 / 对比 / 推荐三个 Agent | 证据不足必须拒答 |
| `src/litprism/genui.py` + `web/static/lit-ui.js` | `lit-ui` 围栏协议 + 8 组件 | **模型给的字段一律被数据库覆盖** |
| `src/litprism/context/*` | meter / policy / summarizer / seam / restore / telemetry | 上下文压缩内核，6 个模块各管一件事 |
| `src/litprism/memory/*` | store / strategy / view / guards | 三条硬边界可执行，不靠提示词 |
| `src/litprism/skills/*` | manifest / validate / registry / runner | 仅声明式；目录内 .py/.js/.sh 一律不执行 |
| `src/litprism/mcp/*` | tools / audit / server | 工具逻辑与 SDK 解耦，测试不依赖 MCP SDK |
| `src/litprism/sidecar.py` | Tauri sidecar 握手 + token 中间件 | 端口在本机，token 是最小访问控制 |
| `src/litprism/secrets.py` | API Key 加密存储（DPAPI → AES-GCM → Base64 降级） | **Key 绝不进配置文件** |

---

## 3. 进度矩阵（截至 2026-10-18）

### 最新功能缺口复查

提供方兼容性补修已提交 `cd40344`，提交前 553 项测试通过。后续离线探针复现了正文无引用仍可导出、对比来源归属未绑定、Skill 范围未执行、长文覆盖不足、聊天 top_k 配置被覆盖及术语回收隔离遗漏。详见 [剩余功能缺口分析](docs/剩余功能缺口分析-2026-10-18.md)，复现工具 `tools/audit_remaining_gaps.py`。本次分析未修改业务逻辑；优先补证据、范围与长文覆盖，不继续 F3。

### 真实提供方增量

CommandCode 的 `deepseek/deepseek-v4.1-flash` 已通过真实短问答、追问、解读/术语与对比（仅合成资料）。Key 经用户授权存入独立数据根的 DPAPI SecretStore，未切换默认模型。发现并修复输出截断提示、对比返回类型及 UI 术语读取，553 项测试和非空术语 UI 流程通过；这些补修已纳入本轮提交。详见功能补齐进度末节。

### 当前增量：先补功能，暂停打包

本轮按真实性与引用守卫优先补齐，然后接通导入、分组、对比、Skill、推荐、导出、回收站、偏好与上下文查看。完整修复映射、验证边界与后续顺序见 [功能补齐进度](docs/功能补齐进度-2026-10-18.md)。

- 551 项测试通过；真实 FastAPI + 临时 SQLite + 无窗口 Chromium 离线流程通过。
- 模型桩验证调用与守卫，不代表真实模型语义/联网源已经验收。
- 功能补齐已提交 `d5f0305`；随后修复 UI 同步、术语残留与下载状态，并通过 551 项测试和两套无窗口 UI 验收。旧 sidecar 不包含这些源码改动；不继续 F3。
- 下列历史完成记录按当时口径保留；当前以本节及补齐进度为准，不能沿用旧“A–E 全部完成”的结论。

### 本轮增量：研究工作台与多轮问答接线

- 界面资源实际位于 `src/litprism/web/static/`（下表的 `web/static/` 是简称）。
- Electron 保留原生窗口标题栏，页面工具栏移除仿 macOS 三色圆点；菜单默认隐藏，可按 Alt 调出。
- 默认两栏：左侧研究导航 + 中间阅读/问答；右侧「证据与详情」按需展开，默认页签为来源。
  小于 1180px 时详情面板浮于工作区，可关闭，不再强制隐藏导致无法查看。
- Ctrl+B 切换导航、Ctrl+Alt+B 切换详情、Ctrl+K 搜索知识库；主题与布局偏好保存在本机窗口缓存。
- 文献概览与列表、固定输入区、统一 SVG 图标；引用错误仍明确展示（包括重开会话后的历史记录）。
- `context/dialogue.py` 接入聊天：只注入可见历史与活跃压缩摘要，按 Meter 估算的剩余预算裁减；
  偏好经 LayeredStrategy 与强制守卫注入，历史 P 编号不沿用，回答只校验本轮检索原文。
  无召回仍直接拒答；TurnView 冻结历史 ref、偏好 id 与原文 chunk id，支持审计。
- 明确追问的检索会补充最近的问题，属于启发式指代处理，尚未做真实模型的长对话效果评估。
- 新增来源数据使用 GenUI 从数据库回填原文与页码；界面统计如实标注会话可见内容估算，不冒充完整模型请求用量。
- 本轮验证：改前 515 项 / 改后 523 项；headless 前端交互、深浅主题、1000×660 布局通过，未开应用窗口。
  源码 sidecar 使用 `E:/codex-litprism-data`，curl `--noproxy '*'` 验证 ping、鉴权 401/200、页面与资源 200；已回收测试进程。
- 可复用前端验收：`node tools/check_workbench.cjs`（需可解析 `playwright/test` 和已安装 Chrome；
  无窗口、临时独立浏览器配置、拦截 API 使用示例数据；可设 `LITPRISM_UI_OUTPUT` 保存截图）。
- 原有打包产物不会自动更新。需 `python packaging/build.py` 重新生成 sidecar，才会把本轮 Python 与静态资源带入桌面打包版。

### 提交与重新打包（2026-10-17）

- 工作台与多轮接线已提交：`225fa1b`（`codex-work`）。
- 已使用指定 Python 执行 `packaging/build.py`；产物 `packaging/dist/litprism-sidecar.exe`，145,518,180 字节（138.8 MiB）。
  SHA256：`0ea62000ae5d5879be988a23cbac382b554d01b784ad97aab18b263e63a4029b`。
- exe 验收：真实 PID 握手、先确认 ping 访问日志可观测、鉴权 401/200；页面 200，四个 CSS/JS 文件与当前源码逐字匹配；
  mock 模式空库问答明确拒答、新会话与消息重启后保留、用户配置与 SQLite 位于独立测试根；真实进程和启动器均回收，端口不再监听。
- 测试数据根：`E:/codex-litprism-data/package-smoke-20261017-235652`，未使用日常知识库；未打开 GUI。
- 本次只重新生成 sidecar，不是完整桌面安装包。下一阶段仍为 **F3：electron-builder + NSIS**。
  当前 `package.json` 的 build/dev 命令及 `packaging/build.py` 的结束提示仍指向 Tauri，F3 需统一成 Electron 发布流程；不据该提示重新切回 Tauri。

| 阶段 | 状态 | 证据 |
|---|---|---|
| A 数据结构与不变量 | ✅ 完成 | 老库无损升级到 v2、新库 v3 一次建全、迁移幂等 |
| B 上下文管理内核 | ✅ 完成 | 真实会话 22→34 条消息端到端压缩，省 13854 token/轮 |
| C 记忆系统 | ✅ 完成 | 三条硬边界有测试；去重/预算/轮次 View 实测 |
| D1 五态核验+白名单 | ✅ 完成 | 白名单 0ms 命中；not_found/likely/unchecked 被导入守卫拒绝 |
| D2 Skill 框架 | ✅ 完成 | 空状态正常；非法 Skill 报 5 错 1 警；`run_shell` 被拒 |
| D3 扫描件转写 | ⏸ **暂缓**（用户决定放弃） | 需要真实多模态 Key 才能实测 |
| D4 交付物管理 | ✅ 完成 | 导出必带「引用来源」；状态 draft→exported，final 不降级 |
| D5 MCP Server | ✅ 完成 | 只读模式注册 8 tool（write 不注册）；改 readonly 后 9 个 |
| E1–E7 界面 | ✅ 完成 | 三栏 + 设置弹窗 + 自动压缩 + 会话删除 + 模型目录增量加入 |
| F0 sidecar 握手 | ✅ 完成 | 200/401/401；静态资源不设防 |
| F1 PyInstaller spike | ✅ **完成** | 三项验证全过，见 §5.1 |
| F2 外壳工程 | ✅ **完成（用 Electron）** | 窗口渲染 + 数据加载 + 进程回收均已实测，见 §5.3 |
| W1–W3 联网检索 | 📐 仅设计 | `docs/联网检索与知识库扩充设计.md`，Key 已备 |

---

## 4. 不能破坏的设计铁律

改任何东西之前，先确认没有踩到下面这些。它们不是风格偏好，是产品存在的前提。

### 4.1 核验必须区分 `not_found` 与 `unchecked`

| 判定 | 触发 | 能否入库 |
|---|---|---|
| `verified` ✅ | 相似度 ≥0.92 或 DOI 精确命中 | 可 |
| `likely` ⚠️ | 相似度 ∈[0.82,0.92) | 需人工确认 |
| `not_found` ❌ | 数据源**正常响应**但无匹配 | 禁止 |
| `unchecked` ❔ | 数据源**全部请求失败** | 禁止 |
| `manual_confirmed` 🔵 | 白名单人工放行 | 可 |

把网络故障判成 `not_found`，会把真实论文诬为编造 —— 这比漏检严重。
标题相似度**必须对词序敏感**（`"Attention Is All You Need"` vs `"Is Attention All You Need?"` 得 0.91）。

### 4.2 问答只基于检索到的原文片段

无召回 → **直接拒答**，不让模型"合理推测"。回答里的 `[P# p.N]` 逐条校验：chunk 必须存在、页码必须与该 chunk 实际页一致。

### 4.3 记忆层不得承载事实

三条硬边界是**代码**不是提示词：记忆不能作事实依据、不能作引用来源、事实性内容不能写进记忆（必须走核验知识库）。第三条堵的是"绕过核验"的口子。

### 4.4 GenUI 的字段以数据库为准

模型输出的 `title` / `verify_level` / `page` 一律被数据库值覆盖。框架在模型没写围栏时**自动补** `citation-list` —— 让"不列来源"在结构上不可能发生。

### 4.5 证据分层（Tier1 文献 / Tier2 网页）

已设计未实现：`[P#]` 与 `[W#]` 编号空间独立、存储字段分开、校验规则不同（`[W#]` 的 URL 必须在**本次抓取集合**内）。网络证据只能作背景/线索，不能作学术结论依据。

### 4.6 其它容易踩的

- `config.yaml` 是用户配置（gitignore）；`config.example.yaml` 是模板，**改设置绝不能写回模板**。
- `patch_config` 先校验后落盘、只合并不覆盖、**拒绝写 api_key**。
- 交付物导出必须带「引用来源」段；没有引用清单的交付物不可追溯。
- Skill 只能是声明式的；`citation_required` 时引用校验由框架跑，Skill 无法移除。

---

## 5. 已知缺陷与坑

### 5.1 frozen 路径错乱 —— 已修复（保留记录，这是最容易复发的一类坑）

**曾经的现象（实测）**：`/api/status` 返回 `"db_path": "...\\AppData\\Local\\Temp\\data\\litprism.db"`。

**根因**：`PROJECT_ROOT = Path(__file__).resolve().parents[2]`。
onefile 模式下 `__file__` 在 `_MEIPASS`（每次启动重建、退出后可能被清理的临时解包目录）→
所有相对路径（`./data`、`config/`）都落到 `%TEMP%`。**用户用一周，数据可能哪天就没了。**

**连带问题**：`config/` 没打进包（spec 的 `datas` 只有 `web/static`）→
`ensure_user_config()` 找不到模板 → 用户配置永远生成不出来 → 设置界面改了没有落点。

**修法**：`config.py` 引入两个根，并抽出纯函数 `resolve_roots()` 便于测试：

| 根 | 源码模式 | frozen 模式 | 用途 |
|---|---|---|---|
| `BUNDLE_ROOT` | 项目根 | `sys._MEIPASS` | **只读**：配置模板、`web/static` |
| `APP_ROOT` | 项目根 | `%LOCALAPPDATA%\\LitPrism`（或 `LITPRISM_HOME`） | **可写**：`data/`、`config/config.yaml`、密钥库 |

`PROJECT_ROOT` 保留为 `APP_ROOT` 的别名（源码模式行为完全不变）。
`patch_config` 的守卫改为按 `examples_config_path()` 判定，防写进包内模板。

**验收（实测，全部通过）**：

```
db_path        → C:\Users\18434\AppData\Local\LitPrism\data\litprism.db   ✅
用户配置       → %LOCALAPPDATA%\LitPrism\config\config.yaml（6423 B）      ✅
GET /          → 200（页面骨架；token 只守 /api）                          ✅
GET /api/status 无 token → 401                                            ✅
PDF 解析       → BERT 16 页 / 61 块                                        ✅
重启后         → papers=1 chunks=61 仍在                                  ✅
握手行         → "pid": 80036（原先写死 null，已改成真实 PID）              ✅
```

**同时定的规矩**：
- token 只守 `/api/*`，不守页面骨架与静态资源 —— token 保护的是**数据**，挡住外壳既不增加安全又妨碍排障。
- 打包统一用 `python packaging/build.py`，产物固定落 `packaging/dist/`；直接调 pyinstaller 会生成到 `./dist`，容易验证到旧的那一个。
- 新增 `tests/test_paths.py`（10 项）把这条规则锁死：模板只读、用户配置可写、`LITPRISM_HOME` 优先。

**留给 F2 的伏笔**：Tauri 前端 origin 是 `tauri://localhost`，请求 `127.0.0.1:PORT` 属跨源，
需要给 sidecar 加 CORS（或走 Tauri 的 HTTP 插件代理）。这一条**尚未实测，属推断**。

### 5.3 外壳选型：Tauri 白屏 → 改用 Electron（已定案）

**结论先行**：外壳用 **`desktop-electron/main.js`**（约 150 行）。Tauri 工程保留在
`src-tauri/` 作为备选，但**当前环境跑不起来**。

**根因（实测，有日志）**：这台机器上 Chromium 的 **GPU 进程反复以 `0xC0000005`
（访问违例）崩溃**，最终 `FATAL: GPU process isn't usable. Goodbye.`。
Tauri 的 WebView2 与 Electron 用的是同一个 Chromium 底座，所以两者都白屏 ——
**这不是框架问题，是环境问题**。Electron 加上
`--disable-gpu / --disable-gpu-compositing / --disable-gpu-sandbox / --no-sandbox` +
`app.disableHardwareAcceleration()` 后正常渲染。

**最小判定实验怎么做的**（值得复用）：把内嵌页面换成一行 `PROBE-OK` 的 HTML，
重建后再看。仍然全黑 ⇒ 排除了「我们的页面/资源路径有问题」，锁定为 webview 本身。

**Electron 端的两个坑（都已修，别再踩）**：

| 坑 | 症状 | 修法 |
|---|---|---|
| `spawn` 直接跑 `.bat` | `spawn EINVAL`，backend 起不来 | Windows 下经 `cmd /d /s /c` 调用 |
| 先杀 onefile 启动器 | 应用退了 **sidecar 还活着** | 先用**握手行里的真实 pid** 杀，再收启动器（启动器一死，`/T` 就枚举不到子进程了） |

**为什么窗口直接 `loadURL(后端地址)` 而不是内嵌前端**：Python 侧本来就用 FastAPI
提供整套 UI（`run.py serve` 同一套），窗口加载它就是**同源**页面 —— 不需要 CORS、
不需要注入脚本、不需要把前端打进壳里。token 走 URL hash（`#t=…`，不发给服务器、不进 Referer）。
少一层就少一类坑，这是折腾 Tauri 一整轮换来的判断。

**Tauri 侧的失败清单**（若将来要回来修，别重复试）：`navigate()` 返回 `Ok` 但 URL 不变；
主线程再试一样；`WebviewUrl::External` 建窗时 `on_navigation` 放行（日志 `→ true`）仍 `about:blank`；
运行期建窗同样 `about:blank`；`additionalBrowserArgs` 在 conf 里不生效（调试端口没开）。
**注意**：以上都是在 GPU 已崩的前提下测的，加上 `--disable-gpu` 类参数后 Tauri 是否可用，
**没有验证过**，不要据此断言"Tauri 不可用"。

**调试工具**（都在仓库里，别再手搓）：
- `LITPRISM_SELFTEST=1` + Electron → 页面把自己 DOM 摘要与截图写到 `_selftest.json/.png`。
  **截图会被上层窗口遮挡，靠"拍屏幕"判断渲染状态不可靠**——这条弯路走过好几轮。
- `packaging/_debug_sidecar.bat` —— 让外壳跑**源码版** sidecar（改 Python 不用等 2 分钟打包）。
  纯 ASCII + CRLF，cmd.exe 按 GBK 读，中文注释会把整行读坏。
- `tools/window_shot.py` —— 抓窗口截图（仅当自证不可用时）。WebView2 内容 `PrintWindow`
  抓不到（全白）；`SetForegroundWindow` 会被前台锁定拒绝。
- 跑 Electron 前必须 `env -u ELECTRON_RUN_AS_NODE`：宿主（WorkBuddy）设了这个变量，
  否则 `electron.exe` 以纯 Node 身份运行，`require('electron').app` 是 undefined。

### 5.2 其它

| 项 | 说明 |
|---|---|
| 图谱 | 右栏以**列表**呈现，未接图库 |
| 全局搜索/命令面板 | 缺（Ctrl+K 目前只聚焦知识库搜索） |
| 跨语言检索 | 靠内置 ~200 组中英词表 + 可选 LLM 改写，冷僻术语召回不足 |
| 章节识别 | 基于标题词典，非标准结构论文退化为单节 |
| 跨论文关联 | TF-IDF 词法相似度，只是**线索**，实质判断要用 `compare` |
| `mcp` 包 | 清华镜像缺，需阿里云源 |
| 本地端口测试 | 必须 `--noproxy '*'`，否则代理会劫持 |

---

## 6. 下一步（建议顺序）

1. ~~修 §5.1 的 frozen 路径~~ ✅ 已完成（2026-10-17）。
2. ~~F2 外壳工程~~ ✅ 已完成（Electron，见 §5.3）。
3. **F3 安装包**：`electron-builder`（NSIS）。要带 `packaging/dist/litprism-sidecar.exe` 作为 extraResource。
   **注意**：本机 Chromium GPU 进程会崩，安装包里的启动参数必须保留 §5.3 那几个开关。
4. **W1**：web_search provider（tavily/searxng）+ 正文提取 + 双通道提示词 + **双通道引用校验** + `web-source-list` 组件 + 对话「联网补充」开关。
5. **W2**：答后入库编排（URL 论文特征检测 → 入库卡片 → 核验 → OA 下载 → ingest，**必须用户确认**）。
6. **W3**：证据不足自动建议联网 + `schema v4` 的 `web_citations` 字段。

**排序理由**：外壳越早撞上真实桌面环境的问题越好；W1 没有真实 Key 之前只能验证协议，做完也是"待实测"。

---

## 7. 文档索引

| 文档 | 内容 |
|---|---|
| `README.md` | 用户向：功能、命令、三层防幻觉说明 |
| `docs/方案定稿.md` | **总入口**，列出下面各专题文档 |
| `docs/设计文档.md` | 已实现内核的技术细节 |
| `docs/Skill框架设计.md` | Skill 清单规范 / 能力表 / 执行契约 |
| `docs/上下文管理与记忆系统设计.md` | 测量/触发/选择/摘要/恢复 + 9 条不变量；三层记忆 |
| `docs/回复展现层设计.md` | `lit-ui` 围栏 + 白名单组件 + 四级降级 |
| `docs/界面与扩展设计.md` | 三栏布局 / 设置七分区 / GenUI |
| `docs/联网检索与知识库扩充设计.md` | 两级证据模型（**待确认**，含 5 个开放问题） |
| `docs/实现计划.md` | 阶段 A–F 明细 + **每阶段的实测记录与抓出的缺陷** |

---

## 8. 接手自检清单

```bash
$PY -m pytest tests -q                       # 期望 523 passed；测试进程清除 LITPRISM_HOME
$PY run.py init                              # 期望 papers=3 chunks=389（示例库）
$PY packaging/build.py                       # 约 2min，产物固定落 packaging/dist/
./packaging/dist/litprism-sidecar.exe --port 8899 &
curl -s --noproxy '*' -H "X-LitPrism-Token: <握手行里的 token>" http://127.0.0.1:8899/api/status
#   db_path 必须落在 %LOCALAPPDATA%\LitPrism\data\ —— 若出现 %TEMP%，说明 §5.1 的坑复发了
```

若测试数、库内论文数与本文件不符，说明状态已前进 —— **以实测为准，顺手更新本文件**。
