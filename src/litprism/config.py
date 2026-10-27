"""配置加载：YAML 文件 + 环境变量覆盖 + 合理默认值。"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML 缺失时给出明确指引
    yaml = None  # type: ignore[assignment]


# --------------------------------------------------------------------------
# 两个根目录：只读资源 vs 可写数据
# --------------------------------------------------------------------------
def resolve_roots(
    *,
    frozen: bool,
    meipass: str | Path | None = None,
    localappdata: str | Path | None = None,
    home: str | Path | None = None,
    source_root: Path | None = None,
) -> tuple[Path, Path]:
    """返回 ``(BUNDLE_ROOT, APP_ROOT)``。

    **为什么要分两个根**（这是打包后最容易踩的坑）：

    * PyInstaller onefile 模式下 ``__file__`` 位于 ``sys._MEIPASS``
      —— 那是**每次启动都会重建、退出后可能被清理的临时解包目录**。
      若把它当作项目根，知识库会建在 ``%TEMP%\\data`` 里，
      用户装了软件用一周，数据可能哪天就没了。
    * 同时，包内资源（``config.example.yaml``、``web/static``）确实只在
      ``_MEIPASS`` 里可读，不能写到那里去（重启即丢）。

    所以：**读资源走 BUNDLE_ROOT，写数据走 APP_ROOT**。
    源码运行时两者都是项目根，行为与之前完全一致。

    可写根优先级：``LITPRISM_HOME`` 环境变量 > ``%LOCALAPPDATA%\\LitPrism``
    > ``~/.litprism``（非 Windows 或变量缺失时）。
    """
    if not frozen:
        root = Path(source_root) if source_root else Path(__file__).resolve().parents[2]
        override = os.environ.get("LITPRISM_HOME")
        return root, Path(override) if override else root

    bundle = Path(meipass) if meipass else Path(sys.executable).parent
    override = os.environ.get("LITPRISM_HOME")
    if override:
        app = Path(override)
    elif localappdata:
        app = Path(localappdata) / "LitPrism"
    else:
        app = (Path(home) if home else Path.home()) / ".litprism"
    return bundle, app


BUNDLE_ROOT, APP_ROOT = resolve_roots(
    frozen=bool(getattr(sys, "frozen", False)),
    meipass=getattr(sys, "_MEIPASS", None),
    localappdata=os.environ.get("LOCALAPPDATA"),
)

# 向后兼容别名：原先所有相对路径都以「项目根」为基准
PROJECT_ROOT = APP_ROOT


# --------------------------------------------------------------------------
# 配置数据类
# --------------------------------------------------------------------------
@dataclass
class LLMConfig:
    provider: str = "mock"          # openai | deepseek | ollama | mock
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    temperature: float = 0.1
    max_tokens: int = 4096
    model_max_output_tokens: int | None = None
    model_window_tokens: int | None = None
    automatic_output: bool = False
    timeout: int = 120
    json_mode: bool = True
    review_max_tokens: int = 4096
    cross_review_max_tokens: int = 8192
    review_read_timeout: int = 60

    @property
    def effective_base_url(self) -> str:
        if self.provider == "deepseek" and "deepseek" not in self.base_url:
            return "https://api.deepseek.com/v1"
        if self.provider == "ollama" and self.base_url.startswith("https://api.openai"):
            return "http://localhost:11434/v1"
        return self.base_url.rstrip("/")

    @property
    def effective_model(self) -> str:
        if self.provider == "deepseek" and self.model == "gpt-4o-mini":
            return "deepseek-chat"
        if self.provider == "ollama" and self.model == "gpt-4o-mini":
            return "qwen2.5:7b"
        return self.model


@dataclass
class SourceConfig:
    enabled: bool = True
    mailto: str = ""
    api_key: str = ""


@dataclass
class SourcesConfig:
    crossref: SourceConfig = field(default_factory=lambda: SourceConfig(mailto="your_email@example.com"))
    openalex: SourceConfig = field(default_factory=lambda: SourceConfig(mailto="your_email@example.com"))
    arxiv: SourceConfig = field(default_factory=SourceConfig)
    semantic_scholar: SourceConfig = field(default_factory=lambda: SourceConfig(enabled=False))
    unpaywall: SourceConfig = field(default_factory=lambda: SourceConfig(enabled=False))

    def get(self, name: str) -> SourceConfig:
        return getattr(self, name, SourceConfig(enabled=False))


@dataclass
class VerificationConfig:
    verified_threshold: float = 0.92
    likely_threshold: float = 0.82
    require_year_match: bool = False
    max_candidates: int = 5


@dataclass
class RetrievalConfig:
    chunk_size: int = 900
    chunk_overlap: int = 150
    top_k: int = 8
    min_score: float = 0.0


@dataclass
class QAConfig:
    strict_citation_check: bool = True
    refuse_when_no_evidence: bool = True
    evidence_anchor_trial: bool = False  # Isolated opt-in trial; no settings UI.


@dataclass
class RecommendConfig:
    default_limit: int = 8
    strategies: list[str] = field(default_factory=lambda: ["openalex_related", "keyword", "local"])
    auto_import: bool = False


@dataclass
class ExportConfig:
    notes_dir: str = "./notes"
    formats: list[str] = field(default_factory=lambda: ["markdown"])


@dataclass
class ContextConfig:
    """上下文管理配置。

    参数取「校准值」而非「定律」—— 参考实现的经验起点，本项目实测后再标定。
    """

    window_tokens: int = 32000          # 模型上下文窗口
    step_tokens: int = 30000            # 增长门控步长（绝对值，与窗口解耦）
    retain_tokens: int | None = None    # 保留预算（绝对）；与 retain_ratio 互斥
    retain_ratio: float | None = None   # 保留预算（窗口比例）；与 retain_tokens 互斥
    nudge_ratio: float = 0.75           # 压力层：温和触发
    emergency_ratio: float = 0.95       # 压力层：紧急触发 + 硬截断
    output_reserve_tokens: int = 4096   # 输出预留
    overflow_retries: int = 1           # 窗口溢出后的恢复重试上限
    summary_max_ratio: float = 0.6      # 收缩验证：摘要不得超过被遮蔽内容的这个比例
    recent_turns_protected: int = 5     # 最近 N 条消息受保护，不参与压缩
    cjk_chars_per_token: float = 1.5    # CJK 估算系数（4 字符/token 对中文严重低估）
    latin_chars_per_token: float = 4.0  # 拉丁文估算系数
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def effective_retain_tokens(self) -> int:
        """保留预算的实际取值（token）。"""
        if self.retain_tokens is not None:
            return self.retain_tokens
        if self.retain_ratio is not None:
            return int(self.window_tokens * self.retain_ratio)
        # 默认：保留 30% 窗口
        return int(self.window_tokens * 0.30)

    @property
    def available_tokens(self) -> int:
        return self.window_tokens - self.output_reserve_tokens


@dataclass
class MemoryConfig:
    """记忆系统配置。一期只做显式写入。"""

    resident_token_budget: int = 500    # 常驻层硬上限 —— 防止偏好记忆挤压检索片段预算
    resident_max_items: int = 12        # 常驻条数上限
    allow_implicit: bool = False        # 一期固定 False：不做隐式推断
    retrieval_top_k: int = 5            # 按需层检索条数


@dataclass
class McpConfig:
    """MCP Server 配置。

    **默认只读** —— 写入需显式开启。MCP 让外部客户端能读写知识库，
    默认放开写入的风险远大于收益。
    """

    enabled: bool = False               # 是否在应用启动时一并拉起 MCP Server
    readonly: bool = True               # 只读模式：不暴露写工具
    server_id: str = "local"            # 审计日志里标识来源


@dataclass
class WebSearchConfig:
    """联网搜索配置（两级证据模型的 Tier 2）。

    Key 走 SecretStore 加密存储，配置文件里只存 ref。
    provider=none 表示禁用联网。
    """

    provider: str = "tavily"            # tavily | searxng | none
    api_key_ref: str = ""               # SecretStore 引用，不存明文
    searxng_url: str = "http://localhost:8888"
    max_results: int = 5


@dataclass
class AppConfig:
    name: str = "文献棱镜 LitPrism"
    data_dir: str = "./data"
    version: str = ""


@dataclass
class Config:
    app: AppConfig = field(default_factory=AppConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    verification: VerificationConfig = field(default_factory=VerificationConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    qa: QAConfig = field(default_factory=QAConfig)
    recommend: RecommendConfig = field(default_factory=RecommendConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    mcp: McpConfig = field(default_factory=McpConfig)
    websearch: WebSearchConfig = field(default_factory=WebSearchConfig)
    config_path: Path | None = None
    # 载入期校验产生的警告（未知键等）—— 不阻断启动，但要在 UI 上提示一次
    config_warnings: list[str] = field(default_factory=list)

    # ---- 便捷属性 ----
    @property
    def data_path(self) -> Path:
        p = Path(self.app.data_dir)
        if not p.is_absolute():
            p = (PROJECT_ROOT / p).resolve()
        return p

    @property
    def db_path(self) -> Path:
        return self.data_path / "litprism.db"

    @property
    def cache_path(self) -> Path:
        return self.data_path / "cache"

    @property
    def notes_path(self) -> Path:
        p = Path(self.export.notes_dir)
        if not p.is_absolute():
            p = (self.data_path / p).resolve()
        return p

    def ensure_dirs(self) -> None:
        for d in (self.data_path, self.cache_path, self.notes_path):
            d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# 加载逻辑
# --------------------------------------------------------------------------
def _merge_dict(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_dict(out[k], v)
        else:
            out[k] = v
    return out


def _from_dict(raw: dict) -> Config:
    raw = raw or {}
    cfg = Config()
    if "app" in raw:
        cfg.app = AppConfig(**{k: v for k, v in raw["app"].items() if k in AppConfig.__dataclass_fields__})
    if "llm" in raw:
        cfg.llm = LLMConfig(**{k: v for k, v in raw["llm"].items() if k in LLMConfig.__dataclass_fields__})
    if "sources" in raw:
        sc = SourcesConfig()
        for name, sub in raw["sources"].items():
            cur = sc.get(name)
            for k, v in (sub or {}).items():
                if hasattr(cur, k):
                    setattr(cur, k, v)
        cfg.sources = sc
    for key, cls in (
        ("verification", VerificationConfig),
        ("retrieval", RetrievalConfig),
        ("qa", QAConfig),
        ("recommend", RecommendConfig),
        ("export", ExportConfig),
        ("context", ContextConfig),
        ("memory", MemoryConfig),
        ("mcp", McpConfig),
        ("websearch", WebSearchConfig),
    ):
        if key in raw:
            sub = raw[key] or {}
            known = {k: v for k, v in sub.items() if k in cls.__dataclass_fields__}
            unknown = {k: v for k, v in sub.items() if k not in cls.__dataclass_fields__}
            obj = cls(**known)
            # 未知键不丢弃（保留在 extra 里）—— 便于 check_config 报告
            if unknown and hasattr(obj, "extra"):
                obj.extra.update(unknown)
            setattr(cfg, key, obj)
    return cfg


def _apply_env(cfg: Config) -> Config:
    env = os.environ
    if env.get("LITPRISM_BASE_URL"):
        cfg.llm.base_url = env["LITPRISM_BASE_URL"]
    if env.get("LITPRISM_API_KEY"):
        cfg.llm.api_key = env["LITPRISM_API_KEY"]
    if env.get("LITPRISM_MODEL"):
        cfg.llm.model = env["LITPRISM_MODEL"]
    if env.get("LITPRISM_PROVIDER"):
        cfg.llm.provider = env["LITPRISM_PROVIDER"]
    if env.get("LITPRISM_DATA_DIR"):
        cfg.app.data_dir = env["LITPRISM_DATA_DIR"]
    mailto = env.get("LITPRISM_MAILTO")
    if mailto:
        cfg.sources.crossref.mailto = mailto
        cfg.sources.openalex.mailto = mailto
    if env.get("S2_API_KEY"):
        cfg.sources.semantic_scholar.api_key = env["S2_API_KEY"]
        cfg.sources.semantic_scholar.enabled = True

    # 常见第三方 Key 自动探测，省去手填
    if not cfg.llm.api_key:
        for var, provider, url, model in (
            ("OPENAI_API_KEY", "openai", "https://api.openai.com/v1", None),
            ("DEEPSEEK_API_KEY", "deepseek", "https://api.deepseek.com/v1", "deepseek-chat"),
            ("DASHSCOPE_API_KEY", "openai", "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus"),
            ("MOONSHOT_API_KEY", "openai", "https://api.moonshot.cn/v1", "moonshot-v1-8k"),
            ("ZHIPU_API_KEY", "openai", "https://open.bigmodel.cn/api/paas/v4", "glm-4-flash"),
        ):
            if env.get(var):
                cfg.llm.provider = provider
                cfg.llm.api_key = env[var]
                cfg.llm.base_url = url
                if model:
                    cfg.llm.model = model
                break
    return cfg


def _validate(cfg: Config) -> None:
    """载入期 fail-fast 校验。

    **区分两类**（配置设计的核心判断）：

    * **错误** → 抛异常。会让系统进入自相矛盾状态的组合必须当场拒绝，
      而不是等到运行中才发现"压缩了还是超窗口"。
    * **警告** → 收集到 ``cfg.config_warnings``。未知键、可选字段畸形，
      静默丢弃 + 一次提示，不阻断启动。
    """
    from .invariants import ContextConfigView, check_config

    if type(cfg.qa.evidence_anchor_trial) is not bool:
        raise ValueError('原文锚点试验开关必须为布尔值。')
    if any(not isinstance(v, int) or isinstance(v, bool) for v in (cfg.llm.max_tokens, cfg.context.window_tokens, cfg.context.output_reserve_tokens, cfg.retrieval.top_k)):
        raise ValueError('窗口、输出预算和召回数必须是整数。')
    view = ContextConfigView(
        window_tokens=cfg.context.window_tokens,
        step_tokens=cfg.context.step_tokens,
        retain_tokens=cfg.context.retain_tokens,
        retain_ratio=cfg.context.retain_ratio,
        nudge_ratio=cfg.context.nudge_ratio,
        emergency_ratio=cfg.context.emergency_ratio,
        output_reserve_tokens=cfg.context.output_reserve_tokens,
        overflow_retries=cfg.context.overflow_retries,
        extra=cfg.context.extra,
    )
    result = check_config(view)
    cfg.config_warnings = [str(v) for v in result.warnings]
    if not result.ok:
        detail = "\n  ".join(str(v) for v in result.errors)
        raise ValueError(f"上下文管理配置不合法：\n  {detail}")
    if cfg.llm.max_tokens <= 0 or cfg.context.output_reserve_tokens <= 0:
        raise ValueError('最大输出和输出预留必须为正。')
    for key in ('timeout', 'review_max_tokens', 'cross_review_max_tokens', 'review_read_timeout'):
        value = getattr(cfg.llm, key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f'llm.{key} 必须为正整数。')
    for key in ('model_max_output_tokens', 'model_window_tokens'):
        value = getattr(cfg.llm, key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise ValueError(f'llm.{key} 必须为空或正整数。')
    if type(cfg.llm.automatic_output) is not bool:
        raise ValueError('llm.automatic_output 必须为布尔值。')
    if not 1 <= cfg.retrieval.top_k <= 50:
        raise ValueError('返回片段数必须在 1–50 之间。')

    if cfg.memory.resident_token_budget <= 0:
        raise ValueError(
            f"memory.resident_token_budget 必须为正，收到 {cfg.memory.resident_token_budget}。"
            "常驻记忆无上限增长会持续挤压检索片段的预算。"
        )


def examples_config_path() -> Path | None:
    """返回**只读**的配置模板路径（包内优先，其次开发目录）。"""
    for cand in (
        BUNDLE_ROOT / "config" / "config.example.yaml",
        APP_ROOT / "config" / "config.example.yaml",
    ):
        if cand.exists():
            return cand
    return None


def ensure_user_config() -> Path | None:
    """确保存在**用户配置文件** ``config/config.yaml``（位于可写根）。

    首次运行时从 ``config.example.yaml`` 复制一份。

    为什么必须这样做：设置界面会把改动写回配置文件。如果写的是
    ``config.example.yaml``（被 git 跟踪的模板），用户的每次改设置都会污染仓库。
    有了这一层，写回永远落在 gitignore 的 ``config.yaml`` 上。

    打包后同理但更严格：模板在 ``BUNDLE_ROOT``（``_MEIPASS``，只读且重启即丢），
    用户配置必须落在 ``APP_ROOT``，否则设置改了等于没改。
    """
    user = APP_ROOT / "config" / "config.yaml"
    if user.exists():
        return user
    example = examples_config_path()
    if example is None:
        return None
    try:
        user.parent.mkdir(parents=True, exist_ok=True)
        user.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        return user
    except OSError:
        return None


def load_config(path: str | Path | None = None) -> Config:
    """加载配置。

    查找顺序：显式路径 → 环境变量 LITPRISM_CONFIG → ./config/config.yaml
    → ./config/config.example.yaml → 内置默认值。
    """
    # An explicit path must never silently open another workspace's settings.
    requested = path if path is not None else os.environ.get('LITPRISM_CONFIG')
    if (path is not None or requested) and (not requested or not Path(requested).is_file()):
        raise FileNotFoundError(f'指定配置文件不存在或不是文件：{requested}')
    # 首次运行：把 example 复制成 config.yaml，后续读写都以它为准
    if not requested:
        ensure_user_config()

    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    if os.environ.get("LITPRISM_CONFIG"):
        candidates.append(Path(os.environ["LITPRISM_CONFIG"]))
    candidates += [
        APP_ROOT / "config" / "config.yaml",
        Path.cwd() / "config" / "config.yaml",
    ]
    example = examples_config_path()
    if example is not None:
        candidates.append(example)

    raw: dict[str, Any] = {}
    used: Path | None = None
    for cand in candidates:
        if cand.exists():
            if yaml is None:
                raise RuntimeError("缺少 PyYAML，请先执行：python -m pip install PyYAML")
            with open(cand, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            used = cand
            break

    cfg = _from_dict(raw)
    cfg = _apply_env(cfg)
    cfg.config_path = used
    cfg.ensure_dirs()
    _validate(cfg)
    return cfg


def patch_config(path: str | Path, patch: dict[str, Any]) -> dict[str, Any]:
    """把 ``patch`` 深合并进配置文件，**校验通过才落盘**。

    设计取舍
    --------
    * **先校验后写盘** —— 非法配置绝不能落地，否则下次启动直接崩。
    * **只合并不覆盖** —— 未提及的键原样保留。
    * **拒绝写 api_key** —— 密钥走 :mod:`litprism.secrets`，不进配置文件。
    * **注释会丢失** —— yaml 库不保留注释。这是设置界面的固有代价，
      需要保留注释的用户可以手动编辑配置文件。
    """
    p = Path(path)
    # 若目标是被 git 跟踪的模板（或包内只读模板），改写到用户配置文件，
    # 避免污染仓库，也避免写进 _MEIPASS 那种重启即丢的临时目录
    example = examples_config_path()
    if example is not None and p.resolve() == example.resolve():
        user = ensure_user_config()
        if user is not None:
            p = user

    raw: dict[str, Any] = {}
    if p.exists():
        try:
            loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
            raw = loaded if isinstance(loaded, dict) else {}
        except (yaml.YAMLError, OSError):
            raw = {}

    if isinstance(patch.get("llm"), dict) and "api_key" in patch["llm"]:
        raise ValueError(
            "API Key 不写入配置文件。请在「设置 → 模型」里通过提供商配置写入，"
            "它会被加密存入本地密钥库。"
        )

    _deep_merge(raw, patch)

    # 用合并后的内容走一遍完整校验（不应用环境变量，校验的是文件本身）
    probe = _from_dict(raw)
    _validate(probe)

    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(p)
    return raw


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> None:
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def config_to_dict(cfg: Config) -> dict:
    d = asdict(cfg)
    d.pop("config_path", None)
    if d.get("llm", {}).get("api_key"):
        d["llm"]["api_key"] = "***"
    return d
