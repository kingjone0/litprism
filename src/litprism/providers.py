"""模型提供商与模型目录。

设计要点
--------
* **API Key 不落配置文件** —— 存在 :mod:`litprism.secrets`（DPAPI 加密），
  ``providers.api_key_ref`` 只存引用。
* **模型目录是"可选项清单"** —— 从提供商 ``/models`` 拉取后，用户勾选哪些
  暴露给 chat 的模型选择器。``providers.chat_model`` 是当前选中的那一个。
* **拉取失败要说清原因** —— 是网络不通、Key 无效、还是该提供商不支持列举模型，
  三种情况的处理方式完全不同。

预设
----
内置几个常见提供商的地址与协议，新建时选预设即可，不必手填。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .secrets import SecretStore

PROTOCOLS = {
    "openai": "OpenAI Chat Completions",
    "anthropic": "Anthropic Messages",
}

PRESETS: list[dict] = [
    {
        "key": "openai",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "protocol": "openai",
    },
    {
        "key": "deepseek",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "protocol": "openai",
    },
    {
        "key": "dashscope",
        "name": "阿里云百炼",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "protocol": "openai",
    },
    {
        "key": "moonshot",
        "name": "Moonshot",
        "base_url": "https://api.moonshot.cn/v1",
        "protocol": "openai",
    },
    {
        "key": "zhipu",
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "protocol": "openai",
    },
    {
        "key": "anthropic",
        "name": "Anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "protocol": "anthropic",
    },
    {
        "key": "ollama",
        "name": "Ollama（本地）",
        "base_url": "http://localhost:11434/v1",
        "protocol": "openai",
    },
]

DEFAULT_MODELS = {
    "openai": [("gpt-4o", "GPT-4o"), ("gpt-4o-mini", "GPT-4o mini")],
    "deepseek": [("deepseek-chat", "DeepSeek Chat"), ("deepseek-reasoner", "DeepSeek Reasoner")],
    "anthropic": [("claude-sonnet-4-5", "Claude Sonnet 4.5")],
    "ollama": [("qwen2.5:7b", "Qwen2.5 7B")],
}

# 常见模型的上下文窗口（token）。仅作**填写提示**，不参与任何判定 ——
# 以用户填写的值为准，因为同名模型的窗口也可能随版本变化。
KNOWN_WINDOWS: list[tuple[str, int]] = [
    ("gpt-4o", 128_000),
    ("gpt-4.1", 1_000_000),
    ("gpt-5", 400_000),
    ("o3", 200_000),
    ("deepseek-chat", 128_000),
    ("deepseek-reasoner", 128_000),
    ("claude", 200_000),
    ("qwen", 128_000),
    ("qwen-long", 1_000_000),
    ("glm-4", 128_000),
    ("moonshot", 128_000),
    ("kimi", 200_000),
    ("gemini", 1_000_000),
    ("llama", 128_000),
]


def guess_window(model_id: str) -> int | None:
    """按模型名猜一个窗口，仅用于界面预填。

    **按前缀长度降序匹配** —— 否则 ``qwen``（128k）会先于 ``qwen-long``（1M）命中，
    给出错误的预填值。越具体的名字必须优先。
    """
    m = (model_id or "").lower()
    if not m:
        return None
    for prefix, window in sorted(KNOWN_WINDOWS, key=lambda kv: -len(kv[0])):
        if m.startswith(prefix) or prefix in m:
            return window
    return None


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _as_int(value: Any) -> int | None:
    """把界面上传来的窗口值转成 int；空值/非法值一律视为未配置。"""
    if value is None or value == "":
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


@dataclass
class ProviderModel:
    model_id: str
    display_name: str = ""
    enabled: bool = True
    sort_order: int = 0
    window_tokens: int | None = None
    max_output_tokens: int | None = None
    api_limits: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.display_name or self.model_id

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "display_name": self.label,
            "enabled": self.enabled,
            "sort_order": self.sort_order,
            "window_tokens": self.window_tokens,
            "max_output_tokens": self.max_output_tokens,
            'api_limits': self.api_limits,
            "suggested_window": guess_window(self.model_id),
        }


@dataclass
class Provider:
    id: str
    name: str
    base_url: str
    protocol: str = "openai"
    api_key_ref: str | None = None
    api_key_mask: str = "未配置"
    chat_model: str | None = None
    multimodal: bool = False
    is_custom: bool = True
    status: str = "unconfigured"
    status_note: str = ""
    checked_at: str = ""
    window_tokens: int | None = None
    models: list[ProviderModel] = field(default_factory=list)

    @property
    def has_key(self) -> bool:
        return self.api_key_mask not in ("未配置", "")

    @property
    def status_label(self) -> str:
        return {
            "ok": "已连接",
            "unconfigured": "未配置",
            "failed": "连接失败",
            "unknown": "未测试",
        }.get(self.status, self.status)

    def to_dict(self) -> dict:
        from .model_limits import confirmed_limits
        def model_dict(m):
            item = m.to_dict()
            item['defaults'] = confirmed_limits(self.base_url, m.model_id)
            return item
        return {
            "id": self.id,
            "name": self.name,
            "base_url": self.base_url,
            "protocol": self.protocol,
            "protocol_label": PROTOCOLS.get(self.protocol, self.protocol),
            "api_key_mask": self.api_key_mask,
            "has_key": self.has_key,
            "chat_model": self.chat_model,
            "multimodal": self.multimodal,
            "is_custom": self.is_custom,
            "status": self.status,
            "status_label": self.status_label,
            "status_note": self.status_note,
            "checked_at": self.checked_at,
            "window_tokens": self.window_tokens,
            "models": [model_dict(m) for m in self.models],
            "enabled_models": [model_dict(m) for m in self.models if m.enabled],
        }


class ProviderError(ValueError):
    """提供商操作失败。消息必须可操作。"""


def _positive_limit(value):
    if value is None or value == '':
        return None
    if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit() or not 0 < int(value) < 2**63:
        raise ProviderError('模型最大输出能力必须为正整数，留空表示继承。')
    return int(value)


def _validate_limits(model):
    ceiling, window = model.get('max_output_tokens'), model.get('window_tokens')
    if ceiling and window and ceiling > window:
        raise ProviderError('模型最大输出不能超过上下文窗口。')


def api_capabilities(item: dict) -> dict:
    """Only unambiguous capacity fields; max_tokens alone is ambiguous."""
    def read(mapping, keys):
        for key in keys:
            value = mapping.get(key)
            if type(value) is int and 0 < value < 2**63:
                return value
        return None
    top = item.get('top_provider')
    top = top if isinstance(top, dict) else {}
    return {'window_tokens': read(item, ('context_length', 'context_window', 'contextWindow'))
            or read(top, ('context_length',)),
            'max_output_tokens': read(item, ('max_output_tokens', 'max_completion_tokens'))
            or read(top, ('max_completion_tokens', 'max_output_tokens'))}


class ProviderStore:
    def __init__(self, kb: Any, data_dir: str | None = None, secrets_store: SecretStore | None = None):
        self.kb = kb
        self.secrets = secrets_store or SecretStore(data_dir or kb.cfg.data_path)

    # ------------------------------------------------------------------
    def list(self) -> list[Provider]:
        rows = self.kb._con.execute(
            "SELECT * FROM providers ORDER BY sort_order, created_at"
        ).fetchall()
        return [self._row_to_provider(r) for r in rows]

    def get(self, pid: str) -> Provider | None:
        row = self.kb._con.execute("SELECT * FROM providers WHERE id = ?", (pid,)).fetchone()
        return self._row_to_provider(row) if row else None

    # ------------------------------------------------------------------
    def create(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str = "",
        protocol: str = "openai",
        chat_model: str | None = None,
        preset: str | None = None,
    ) -> Provider:
        if not (name or "").strip():
            raise ProviderError("显示名称不能为空")
        if not (base_url or "").strip():
            raise ProviderError("API 地址不能为空")
        if protocol not in PROTOCOLS:
            raise ProviderError(f"未知协议 {protocol!r}。可用：{list(PROTOCOLS)}")

        pid = f"pv_{uuid.uuid4().hex[:10]}"
        key_ref = self.secrets.set(f"provider_{pid}", api_key) if api_key.strip() else None
        now = _now()

        with self.kb.tx() as con:
            con.execute(
                "INSERT INTO providers (id, name, base_url, api_key_ref, chat_model, multimodal,"
                " is_custom, status, protocol, sort_order, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    pid, name.strip(), base_url.strip().rstrip("/"), key_ref,
                    chat_model, 1 if protocol == "openai" else 0,
                    0 if preset else 1, "unknown", protocol, 0, now, now,
                ),
            )

        # 预置默认模型目录（用户可再「获取可用模型」覆盖）
        seeds = DEFAULT_MODELS.get(preset or "", [])
        if seeds:
            self.set_models(pid, [{"model_id": m, "display_name": d, "enabled": True} for m, d in seeds])
            if not chat_model:
                self.set_active_model(pid, seeds[0][0])

        return self.get(pid)  # type: ignore[return-value]

    def update(
        self,
        pid: str,
        *,
        name: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        protocol: str | None = None,
        chat_model: str | None = None,
        multimodal: bool | None = None,
    ) -> Provider:
        p = self.get(pid)
        if p is None:
            raise ProviderError(f"提供商 {pid} 不存在")

        sets, vals = [], []
        if name is not None:
            if not name.strip():
                raise ProviderError("显示名称不能为空")
            sets.append("name = ?")
            vals.append(name.strip())
        if base_url is not None:
            if not base_url.strip():
                raise ProviderError("API 地址不能为空")
            sets.append("base_url = ?")
            vals.append(base_url.strip().rstrip("/"))
        if protocol is not None:
            if protocol not in PROTOCOLS:
                raise ProviderError(f"未知协议 {protocol!r}")
            sets.append("protocol = ?")
            vals.append(protocol)
        if chat_model is not None:
            sets.append("chat_model = ?")
            vals.append(chat_model or None)
        if multimodal is not None:
            sets.append("multimodal = ?")
            vals.append(1 if multimodal else 0)
        if api_key:  # 空字符串表示"不改"，避免误清空
            ref = self.secrets.set(f"provider_{pid}", api_key)
            if p.api_key_ref:
                self.secrets.delete(p.api_key_ref)
            sets.append("api_key_ref = ?")
            vals.append(ref)

        if sets:
            sets.append("updated_at = ?")
            vals.extend([_now(), pid])
            with self.kb.tx() as con:
                con.execute(f"UPDATE providers SET {', '.join(sets)} WHERE id = ?", vals)
        return self.get(pid)  # type: ignore[return-value]

    def delete(self, pid: str) -> bool:
        p = self.get(pid)
        if p is None:
            return False
        if p.api_key_ref:
            self.secrets.delete(p.api_key_ref)
        with self.kb.tx() as con:
            con.execute('DELETE FROM provider_model_capabilities WHERE provider_id=?', (pid,))
            con.execute("DELETE FROM provider_models WHERE provider_id = ?", (pid,))
            cur = con.execute("DELETE FROM providers WHERE id = ?", (pid,))
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 模型目录
    # ------------------------------------------------------------------
    def set_models(self, pid: str, models: list[dict]) -> int:
        """整体替换模型目录。``models`` 每项含 model_id / display_name / enabled。"""
        cleaned: list[dict] = []
        seen: set[str] = set()
        for i, m in enumerate(models or []):
            mid = str((m or {}).get("model_id") or "").strip()
            if not mid or mid in seen:
                continue
            seen.add(mid)
            cleaned.append(
                {
                    "model_id": mid,
                    "display_name": str(m.get("display_name") or mid).strip(),
                    "enabled": 1 if m.get("enabled", True) else 0,
                    "sort_order": i,
                    "window_tokens": _as_int(m.get("window_tokens")),
                    "max_output_tokens": _positive_limit(m.get('max_output_tokens')),
                }
            )

        now = _now()
        for m in cleaned:
            _validate_limits(m)
        with self.kb.tx() as con:
            con.execute("DELETE FROM provider_models WHERE provider_id = ?", (pid,))
            for m in cleaned:
                con.execute(
                    "INSERT INTO provider_models (id, provider_id, model_id, display_name,"
                    " enabled, sort_order, window_tokens, max_output_tokens, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        f"pm_{uuid.uuid4().hex[:10]}", pid, m["model_id"],
                        m["display_name"], m["enabled"], m["sort_order"],
                        m["window_tokens"], m['max_output_tokens'], now,
                    ),
                )
        return len(cleaned)

    def add_models(self, pid: str, models: list[dict]) -> dict:
        """**增量**加入模型目录。

        与 :meth:`set_models` 的区别：已存在的条目**原样保留**（包括用户的
        enabled 勾选与显示名），只插入新的。这对应「拉取可用模型 → 勾选
        要加入哪些」的交互 —— 整体替换会把用户已调好的目录冲掉。
        """
        p = self.get(pid)
        if p is None:
            raise ProviderError(f"提供商 {pid} 不存在")

        existing = {m.model_id for m in p.models}
        for m in models or []:
            _validate_limits({**m, 'window_tokens': _as_int(m.get('window_tokens')),
                              'max_output_tokens': _positive_limit(m.get('max_output_tokens'))})
        now = _now()
        added: list[str] = []
        skipped: list[str] = []

        with self.kb.tx() as con:
            for i, m in enumerate(models or []):
                mid = str((m or {}).get("model_id") or "").strip()
                if not mid:
                    continue
                if mid in existing:
                    skipped.append(mid)
                    continue
                existing.add(mid)
                con.execute(
                    "INSERT INTO provider_models (id, provider_id, model_id, display_name,"
                    " enabled, sort_order, window_tokens, max_output_tokens, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        f"pm_{uuid.uuid4().hex[:10]}", pid, mid,
                        str(m.get("display_name") or mid).strip(), 1, 1000 + i,
                        _as_int(m.get("window_tokens")), _positive_limit(m.get('max_output_tokens')), now,
                    ),
                )
                added.append(mid)
        return {"added": added, "skipped": skipped}

    def fetch_models(self, pid: str) -> list[dict]:
        """从提供商的 ``/models`` 拉取可用模型。

        失败时抛 :class:`ProviderError`，**消息要区分原因** ——
        Key 无效和网络不通的处理方式完全不同。
        """
        p = self.get(pid)
        if p is None:
            raise ProviderError(f"提供商 {pid} 不存在")

        import requests

        key = self.secrets.get(p.api_key_ref)
        url = f"{p.base_url.rstrip('/')}/models"
        headers = {"Accept": "application/json"}
        if key:
            if p.protocol == "anthropic":
                headers["x-api-key"] = key
                headers["anthropic-version"] = "2023-06-01"
            else:
                headers["Authorization"] = f"Bearer {key}"

        try:
            resp = requests.get(url, headers=headers, timeout=20)
        except requests.RequestException as e:
            raise ProviderError(
                f"无法连接 {url}：{type(e).__name__}。"
                "请检查 API 地址是否正确、网络是否可达（本地服务需先启动）。"
            ) from e

        if resp.status_code in (401, 403):
            raise ProviderError(
                f"认证失败（HTTP {resp.status_code}）。API Key 可能无效或权限不足。"
            )
        if resp.status_code == 404:
            raise ProviderError(
                "该提供商未提供 /models 接口（HTTP 404）。"
                "请在下方手动添加模型 ID。"
            )
        if resp.status_code >= 400:
            raise ProviderError(f"拉取失败（HTTP {resp.status_code}）：{resp.text[:200]}")

        try:
            data = resp.json()
        except ValueError as e:
            raise ProviderError("返回内容不是合法 JSON，可能 API 地址指向了错误的路径。") from e

        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            items = data if isinstance(data, list) else []

        out: list[dict] = []
        for it in items:
            if isinstance(it, str):
                out.append({"model_id": it, "display_name": it})
                continue
            if not isinstance(it, dict):
                continue
            mid = it.get("id") or it.get("name") or it.get("model")
            if not mid:
                continue
            out.append(
                {
                    "model_id": str(mid),
                    "display_name": str(it.get("display_name") or it.get("name") or mid),
                    **api_capabilities(it),
                }
            )
        out.sort(key=lambda x: x["model_id"])

        with self.kb.tx() as con:
            for item in out:
                con.execute('''INSERT INTO provider_model_capabilities VALUES (?,?,?,?,?)
                    ON CONFLICT(provider_id,model_id) DO UPDATE SET window_tokens=excluded.window_tokens,
                    max_output_tokens=excluded.max_output_tokens,checked_at=excluded.checked_at''',
                    (pid, item['model_id'], item.get('window_tokens'), item.get('max_output_tokens'), _now()))
            con.execute(
                "UPDATE providers SET status = 'ok', status_note = ?, checked_at = ?,"
                " updated_at = ? WHERE id = ?",
                (f"拉取到 {len(out)} 个模型", _now(), _now(), pid),
            )
        return out

    def test(self, pid: str) -> dict:
        """连通性测试：拉一次 /models 看结果。"""
        try:
            models = self.fetch_models(pid)
            return {"ok": True, "model_count": len(models), "message": f"连接正常，{len(models)} 个模型"}
        except ProviderError as e:
            with self.kb.tx() as con:
                con.execute(
                    "UPDATE providers SET status = 'failed', status_note = ?, checked_at = ? WHERE id = ?",
                    (str(e)[:500], _now(), pid),
                )
            return {"ok": False, "message": str(e)}

    # ------------------------------------------------------------------
    # 当前生效
    # ------------------------------------------------------------------
    def get_active(self) -> tuple[Provider | None, str | None]:
        """当前 chat 使用的 (provider, model_id)。"""
        row = self.kb._con.execute(
            "SELECT value FROM meta WHERE key = 'active_provider'"
        ).fetchone()
        if not row or not row["value"]:
            return None, None
        p = self.get(row["value"])
        if p is None:
            return None, None
        model = p.chat_model
        if not model:
            enabled = [m for m in p.models if m.enabled]
            model = enabled[0].model_id if enabled else None
        return p, model

    def set_active_model(self, pid: str, model_id: str) -> bool:
        p = self.get(pid)
        if p is None:
            return False
        with self.kb.tx() as con:
            con.execute(
                "UPDATE providers SET chat_model = ?, updated_at = ? WHERE id = ?",
                (model_id, _now(), pid),
            )
            con.execute(
                "INSERT INTO meta (key, value) VALUES ('active_provider', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (pid,),
            )
        return True

    def active_window(self) -> int | None:
        """当前生效模型的上下文窗口。

        查找顺序：模型级 → 提供商级 → ``None``（调用方回落到全局配置）。
        ``None`` 和"0"必须区分开 —— 前者是"没配"，后者是非法值，
        都不能悄悄当成某个默认窗口用。
        """
        provider, model_id = self.get_active()
        if provider is None:
            return None
        return self.active_limits().get('window_tokens')

    def active_limits(self) -> dict:
        """当前生效模型的窗口与最大输出。

        优先级：**API 实测值 > 库里存的 > 用户确认白名单 > 提供商级**。
        白名单只在前两者都缺时兜底 —— 它不该掩盖"根本没拉到能力"这件事。
        """
        from .model_limits import confirmed_limits
        provider, model_id = self.get_active()
        if provider is None:
            return {'capability_missing': 'no_active_model'}
        limits, sources = {}, {}
        for model in provider.models:
            if model.model_id == model_id:
                for key in ('window_tokens', 'max_output_tokens'):
                    if model.api_limits.get(key):
                        limits[key] = model.api_limits[key]
                        sources[key] = 'api'
                    elif getattr(model, key) is not None:
                        limits[key] = getattr(model, key)
                        sources[key] = 'stored'
                break
        for key, value in confirmed_limits(provider.base_url, model_id or '').items():
            if key.endswith('_tokens') and key not in limits:
                limits[key] = value
                sources[key] = 'user_confirmed'
        if 'window_tokens' not in limits and provider.window_tokens:
            limits['window_tokens'] = provider.window_tokens
            sources['window_tokens'] = 'provider'
        limits['selected_model'] = model_id
        limits['capability_sources'] = sources
        if 'window_tokens' not in limits:
            # 已选模型但没有任何窗口来源：多半是从没成功刷新过模型目录。
            limits['capability_missing'] = 'not_fetched'
        return limits

    def chat_options(self) -> list[dict]:
        """暴露给 chat 的模型选择器：所有启用的模型，按提供商分组。"""
        out = []
        for p in self.list():
            for m in p.models:
                if not m.enabled:
                    continue
                out.append(
                    {
                        "provider_id": p.id,
                        "provider_name": p.name,
                        "model_id": m.model_id,
                        "display_name": m.label,
                        "label": f"{p.name} · {m.label}",
                        "status": p.status,
                    }
                )
        return out

    # ------------------------------------------------------------------
    def _row_to_provider(self, row: Any) -> Provider:
        d = dict(row)
        pid = d["id"]
        capabilities = {r['model_id']: dict(r) for r in self.kb._con.execute(
            'SELECT model_id,window_tokens,max_output_tokens,checked_at FROM provider_model_capabilities WHERE provider_id=?', (pid,))}
        model_rows = [
            dict(r)
            for r in self.kb._con.execute(
                "SELECT * FROM provider_models WHERE provider_id = ? ORDER BY sort_order",
                (pid,),
            ).fetchall()
        ]
        models = [
            ProviderModel(
                model_id=m["model_id"],
                display_name=m.get("display_name") or m["model_id"],
                enabled=bool(m.get("enabled", 1)),
                sort_order=int(m.get("sort_order") or 0),
                window_tokens=m.get("window_tokens"),
                max_output_tokens=m.get('max_output_tokens'),
                api_limits=capabilities.get(m['model_id'], {}),
            )
            for m in model_rows
        ]
        ref = d.get("api_key_ref")
        return Provider(
            id=pid,
            name=d["name"],
            base_url=d["base_url"],
            protocol=d.get("protocol") or "openai",
            api_key_ref=ref,
            api_key_mask=self.secrets.mask(ref),
            chat_model=d.get("chat_model"),
            multimodal=bool(d.get("multimodal")),
            is_custom=bool(d.get("is_custom", 1)),
            status=d.get("status") or "unconfigured",
            status_note=d.get("status_note") or "",
            checked_at=d.get("checked_at") or "",
            window_tokens=d.get("window_tokens"),
            models=models,
        )


def format_providers(providers: list[Provider]) -> str:
    if not providers:
        return "（未配置任何提供商）"
    lines = []
    for p in providers:
        enabled = [m for m in p.models if m.enabled]
        lines.append(
            f"  ● {p.name:<14} {p.protocol:<10} {p.status_label:<6} "
            f"模型 {len(enabled)}/{len(p.models)}  当前 {p.chat_model or '—'}"
        )
        lines.append(f"      {p.base_url}   Key: {p.api_key_mask}")
    return "\n".join(lines)


def dump_presets() -> list[dict]:
    return PRESETS


def apply_active_window(cfg: Any, kb: Any, *, require_active: bool = False) -> dict:
    """把**当前生效模型**的窗口写进 ``cfg.context``，返回解析结果。

    为什么必须在这里做：`context.window_tokens` 原本是全局 32000，
    与模型无关。换 128k/1M 的模型不会自动放大，长论文就永远进不了
    「全文驻留」模式 —— 缓存前缀根本建立不起来。

    解析顺序：API 实测 → 库里存的 → 用户确认白名单 → 提供商级 → 全局配置。
    ``require_active=True`` 时，没选模型直接抛错（真实模型入口必须显式选模）；
    其余情况回落配置值，但会把 ``capability_missing`` 带回去，供界面提示。
    """
    configured = int(cfg.context.window_tokens)
    resolved: int | None = None
    source = "config"
    try:
        limits = ProviderStore(kb, cfg.data_path).active_limits()
        resolved = limits.get('window_tokens')
    except Exception:  # noqa: BLE001 - 提供商表缺失/损坏都不该阻断分析
        resolved = None
        limits = {}

    if limits.get('capability_missing') == 'no_active_model' and require_active:
        from .providers import ProviderError
        raise ProviderError('尚未选择模型：请先在设置里选择提供商与模型，再开始真实调用。')

    if resolved and resolved != configured:
        cfg.context.window_tokens = resolved
        source = "model"
    elif resolved:
        source = "model"

    ceiling = limits.get('max_output_tokens', cfg.llm.model_max_output_tokens if 'selected_model' not in limits else None)
    cfg.llm.model_max_output_tokens = ceiling
    cfg.llm.model_window_tokens = cfg.context.window_tokens
    cfg.llm.automatic_output = bool(ceiling)
    if ceiling:
        cfg.llm.max_tokens = min(ceiling, cfg.context.window_tokens - 1)
        cfg.context.output_reserve_tokens = cfg.llm.max_tokens
        cfg.llm.review_read_timeout = 300
        if cfg.llm.timeout == 120:
            cfg.llm.timeout = 300
    effective = min(cfg.llm.max_tokens, cfg.context.output_reserve_tokens)
    cfg.llm.max_tokens = effective
    return {
        "window": int(cfg.context.window_tokens),
        "configured": configured,
        "source": source,
        "available": int(cfg.context.available_tokens),
        'max_output_tokens': ceiling, 'output_budget_tokens': effective,
        'capability_source': limits.get('source', 'configured'),
        'capability_sources': limits.get('capability_sources', {}),
        'output_mode': 'automatic' if ceiling else 'legacy_unknown_capability',
        'capability_missing': limits.get('capability_missing'),
    }
