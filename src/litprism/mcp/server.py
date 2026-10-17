"""MCP Server 适配层（stdio 传输）。

**薄适配层** —— 只做三件事：把 tools 注册进 SDK、把结果序列化、记审计。
所有业务逻辑都在 ``tools.py`` 里，不依赖 SDK。

依赖：``mcp >= 2.0``（使用高层 ``MCPServer`` 的装饰器 API）

启动::

    python -m litprism.mcp.server

客户端配置（Claude Desktop / 其他 MCP 客户端）::

    {
      "mcpServers": {
        "litprism": {
          "command": "python",
          "args": ["-m", "litprism.mcp.server"]
        }
      }
    }

安全
----
* 默认**只读**。``import_paper`` 需在配置里显式设 ``mcp.readonly: false`` 才可用。
* 只读模式下**不注册**写工具 —— 客户端根本看不到它，避免反复试错。
* 所有调用记入 ``mcp_audit``。
* 经 MCP 写入的文献**同样走核验流程** —— 不接受无法确认真实性的记录。
"""

from __future__ import annotations

from typing import Any

from ..config import load_config
from ..db import KnowledgeBase
from ..llm import build_llm
from .audit import AuditLog
from .tools import RESOURCES, TOOLS, LitPrismTools, describe_tools, dump_schema

SERVER_NAME = "litprism"
SERVER_VERSION = "1.0.0"

SERVER_INSTRUCTIONS = (
    "文献棱镜 LitPrism —— 个人学术文献知识库。\n"
    "所有论文都经过权威数据库核验；引用请使用 search_passages 返回的 chunk_id 与页码。\n"
    "核验状态为「未核到」的文献不得作为引用来源。"
)

# ---------------------------------------------------------------------------
# 工具输入 schema（文档与自检用；SDK 从函数签名推断实际 schema）
# ---------------------------------------------------------------------------
TOOL_SCHEMAS: dict[str, dict] = {
    "search_papers": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "关键词（匹配标题、显示名、作者）"},
            "limit": {"type": "integer", "description": "返回条数上限", "default": 10},
            "verified_only": {"type": "boolean", "description": "只返回可引用的文献", "default": False},
        },
    },
    "get_paper": {
        "type": "object",
        "properties": {
            "paper_id": {"type": "string", "description": "论文 id"},
            "title": {"type": "string", "description": "按标题片段查找（paper_id 为空时生效）"},
        },
    },
    "search_passages": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索词"},
            "top_k": {"type": "integer", "description": "返回片段数", "default": 8},
            "paper_id": {"type": "string", "description": "限定在某篇论文内检索"},
        },
        "required": ["query"],
    },
    "verify_paper": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "论文标题"},
            "doi": {"type": "string", "description": "DOI（最可靠）"},
        },
    },
    "list_collections": {"type": "object", "properties": {}},
    "get_graph": {
        "type": "object",
        "properties": {"threshold": {"type": "number", "description": "关联强度阈值", "default": 0.25}},
    },
    "compare_papers": {
        "type": "object",
        "properties": {
            "paper_ids": {"type": "array", "items": {"type": "string"}, "description": "至少两篇论文的 id"},
        },
        "required": ["paper_ids"],
    },
    "ask": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "问题"},
            "top_k": {"type": "integer", "description": "检索片段数", "default": 8},
        },
        "required": ["question"],
    },
    "import_paper": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "论文标题"},
            "doi": {"type": "string", "description": "DOI"},
            "confirm_reason": {"type": "string", "description": "人工确认依据（核验未通过时使用，会留痕）"},
        },
    },
}


def _mcp_import() -> Any:
    """延迟导入 SDK，给出可操作的错误。"""
    try:
        from mcp.server import MCPServer
    except ImportError as e:  # pragma: no cover - 环境相关
        raise RuntimeError(
            "未安装 MCP SDK（需要 >= 2.0），无法启动 MCP Server。\n"
            "安装：pip install 'mcp>=2.0'\n"
            "若默认镜像没有该包，可换源："
            "pip install mcp -i https://mirrors.aliyun.com/pypi/simple/\n"
            f"（原始错误：{e}）"
        ) from e
    return MCPServer


def build_server(kb: KnowledgeBase | None = None, cfg: Any = None, *, llm: Any = None) -> Any:
    """构造 MCP Server 实例。"""
    MCPServer = _mcp_import()

    cfg = cfg or load_config()
    kb = kb or KnowledgeBase(cfg)
    readonly = bool(getattr(getattr(cfg, "mcp", None), "readonly", True))
    llm = llm if llm is not None else _try_llm(cfg)

    impl = LitPrismTools(kb, cfg, llm=llm, readonly=readonly)
    audit = AuditLog(kb, getattr(getattr(cfg, "mcp", None), "server_id", "local"))

    server = MCPServer(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        instructions=SERVER_INSTRUCTIONS,
    )

    def _guarded(name: str, args: dict, fn: Any) -> dict:
        """统一包一层：审计 + 异常转可读错误。"""
        try:
            result = fn()
        except TypeError as e:
            audit.record(name, args, ok=False, error=f"参数错误：{e}")
            return {"ok": False, "error": f"参数错误：{e}"}
        except Exception as e:  # noqa: BLE001
            audit.record(name, args, ok=False, error=f"{type(e).__name__}: {e}")
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        audit.record(name, args, ok=bool(result.get("ok")))
        return result

    # ---------------- 只读工具 ----------------
    @server.tool(
        name="search_papers",
        description=TOOLS["search_papers"]["description"],
    )
    def search_papers(query: str = "", limit: int = 10, verified_only: bool = False) -> dict:
        args = {"query": query, "limit": limit, "verified_only": verified_only}
        return _guarded("search_papers", args,
                        lambda: impl.search_papers(query, limit, verified_only=verified_only))

    @server.tool(name="get_paper", description=TOOLS["get_paper"]["description"])
    def get_paper(paper_id: str = "", title: str = "") -> dict:
        args = {"paper_id": paper_id, "title": title}
        return _guarded("get_paper", args, lambda: impl.get_paper(paper_id, title))

    @server.tool(name="search_passages", description=TOOLS["search_passages"]["description"])
    def search_passages(query: str, top_k: int = 8, paper_id: str = "") -> dict:
        args = {"query": query, "top_k": top_k, "paper_id": paper_id}
        return _guarded("search_passages", args, lambda: impl.search_passages(query, top_k, paper_id))

    @server.tool(name="verify_paper", description=TOOLS["verify_paper"]["description"])
    def verify_paper(title: str = "", doi: str = "") -> dict:
        args = {"title": title, "doi": doi}
        return _guarded("verify_paper", args, lambda: impl.verify_paper(title, doi))

    @server.tool(name="list_collections", description=TOOLS["list_collections"]["description"])
    def list_collections() -> dict:
        return _guarded("list_collections", {}, impl.list_collections)

    @server.tool(name="get_graph", description=TOOLS["get_graph"]["description"])
    def get_graph(threshold: float = 0.25) -> dict:
        args = {"threshold": threshold}
        return _guarded("get_graph", args, lambda: impl.get_graph(threshold))

    @server.tool(name="compare_papers", description=TOOLS["compare_papers"]["description"])
    def compare_papers(paper_ids: list[str]) -> dict:
        args = {"paper_ids": paper_ids}
        return _guarded("compare_papers", args, lambda: impl.compare_papers(paper_ids))

    @server.tool(name="ask", description=TOOLS["ask"]["description"])
    def ask(question: str, top_k: int = 8) -> dict:
        args = {"question": question, "top_k": top_k}
        return _guarded("ask", args, lambda: impl.ask(question, top_k))

    # ---------------- 写工具（只读模式下不注册）----------------
    if not readonly:
        @server.tool(name="import_paper", description=TOOLS["import_paper"]["description"])
        def import_paper(title: str = "", doi: str = "", confirm_reason: str = "") -> dict:
            args = {"title": title, "doi": doi, "confirm_reason": confirm_reason}
            return _guarded("import_paper", args,
                            lambda: impl.import_paper(title, doi, confirm_reason))

    # ---------------- 资源 ----------------
    @server.resource(
        "litprism://library",
        name="知识库概览",
        description=RESOURCES["litprism://library"]["description"],
        mime_type="text/markdown",
    )
    def library() -> str:
        try:
            return impl.resource_library()
        except Exception as e:  # noqa: BLE001
            return f"读取失败：{type(e).__name__}: {e}"

    @server.resource(
        "litprism://paper/{paper_id}",
        name="论文解读",
        description=RESOURCES["litprism://paper/{id}"]["description"],
        mime_type="text/markdown",
    )
    def paper(paper_id: str) -> str:
        try:
            return impl.resource_paper(paper_id)
        except Exception as e:  # noqa: BLE001
            return f"读取失败：{type(e).__name__}: {e}"

    # 便于测试与排查
    server._litprism_impl = impl
    server._litprism_audit = audit
    server._litprism_readonly = readonly
    return server


def _try_llm(cfg: Any) -> Any:
    try:
        return build_llm(cfg)
    except Exception:  # noqa: BLE001 - 没有模型时 ask 会给出可读错误
        return None


def serve(kb: KnowledgeBase | None = None, cfg: Any = None) -> None:
    """阻塞运行 stdio 服务。"""
    server = build_server(kb, cfg)
    server.run(transport="stdio")


def main() -> int:
    """CLI 入口：``python -m litprism.mcp.server``"""
    import sys

    try:
        cfg = load_config()
    except Exception as e:  # noqa: BLE001
        print(f"配置载入失败：{e}", file=sys.stderr)
        return 2

    readonly = bool(getattr(getattr(cfg, "mcp", None), "readonly", True))
    print(
        f"[litprism-mcp] stdio 服务启动  v{SERVER_VERSION}  "
        f"模式={'只读' if readonly else '读写'}",
        file=sys.stderr,
    )
    print(describe_tools(), file=sys.stderr)

    try:
        serve(cfg=cfg)
    except RuntimeError as e:
        print(f"[litprism-mcp] {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
