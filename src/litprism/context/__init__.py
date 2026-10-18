"""上下文管理内核。

模块划分
--------
meter       测量服务 —— 所有决策的**唯一定价来源**（不做第二套估算）
policy      触发 + 选择（三层触发 / 受保护区 / 安全切口）
summarizer  摘要生成 + 收缩验证
seam        提交事务（单一写入口 + 前置不变量）
restore     可逆性（L1 按块还原 / 还原到文件）
telemetry   可观测性（三层投影 / 压缩账本）

核心判据（见 docs/design/上下文管理与记忆系统设计.md）：
**在「新增量」上动手，而不是在「窗口位置」上动手。**
"""

from __future__ import annotations

__all__ = ["meter", "policy", "summarizer", "seam", "restore", "telemetry"]
