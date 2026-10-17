"""MCP 适配层。

架构
----
``tools.py``    工具与资源的**纯实现**，只依赖内核库，不依赖 MCP SDK
``audit.py``    调用审计（谁在什么时候调了什么、成功与否）
``server.py``   薄适配层：把 tools 注册进 SDK，stdio 传输

这样分层的好处：**测试不依赖 MCP SDK**，SDK 升级或不可用都不影响内核逻辑的验证。

安全约束
--------
* 默认**只读**。写入工具（``import_paper``）需显式开启 ``readonly=false``。
* 所有调用记入 ``mcp_audit``。
* 经 MCP 写入的数据**强制走核验流程** —— 不接受未核验的文献。
"""

from __future__ import annotations

__all__ = ["tools", "audit", "server"]
