"""API Key 的安全存储。

为什么不能明文放 config.yaml
----------------------------
配置文件会被复制、备份、误传到 Git。明文 Key 一旦泄露就是真实损失。

存储策略（按可用性降级）
------------------------
1. **Windows DPAPI**（首选）—— 用当前 Windows 用户凭据加密。
   密文只在这台机器、这个用户下能解，换机器或换用户都解不开。
   **不需要额外管理主密钥** —— 这正是它的价值。
2. **本地随机密钥 + AES-GCM**（非 Windows）—— 主密钥放在权限受限的文件里。
3. **Base64 混淆**（兜底）—— **不是加密**，会在状态里明确标注 `weak`，
   让用户知道风险，而不是误以为安全。

存储位置：``<数据目录>/secrets.json``（已加入 .gitignore）
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import uuid
from pathlib import Path
from typing import Any

PREFIX_DPAPI = "dpapi:"
PREFIX_AES = "aesgcm:"
PREFIX_WEAK = "weak:"


def _try_win32crypt() -> Any:
    try:
        import win32crypt  # type: ignore

        return win32crypt
    except Exception:  # noqa: BLE001
        return None


def _try_cryptography() -> Any:
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore

        return AESGCM
    except Exception:  # noqa: BLE001
        return None


class SecretStore:
    """本地密钥存储。接口只暴露 ref，不暴露明文。"""

    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "secrets.json"
        self._key_path = self.dir / ".secrets.key"
        self._data: dict[str, str] = self._load()

    # ------------------------------------------------------------------
    @property
    def backend(self) -> str:
        if _try_win32crypt() is not None:
            return "dpapi"
        if _try_cryptography() is not None:
            return "aesgcm"
        return "weak"

    def describe(self) -> dict:
        info = {
            "backend": self.backend,
            "path": str(self.path),
            "count": len(self._data),
        }
        if self.backend == "dpapi":
            info["note"] = "使用 Windows DPAPI 加密，绑定当前用户与本机。"
        elif self.backend == "aesgcm":
            info["note"] = "使用本地 AES-GCM 加密，主密钥存放于同目录受限文件。"
        else:
            info["note"] = (
                "⚠️ 未找到可用的加密库，当前仅为 Base64 混淆，**不是加密**。"
                "请安装 cryptography 或 pywin32 后重新保存密钥。"
            )
        return info

    # ------------------------------------------------------------------
    def set(self, name: str, plaintext: str) -> str:
        """保存明文，返回 ref（可写入 providers.api_key_ref）。"""
        if not plaintext:
            raise ValueError("密钥不能为空")
        token = self._encrypt(plaintext)
        ref = f"{name}_{uuid.uuid4().hex[:8]}"
        self._data[ref] = token
        self._save()
        return ref

    def get(self, ref: str | None) -> str | None:
        if not ref:
            return None
        token = self._data.get(ref)
        if token is None:
            return None
        try:
            return self._decrypt(token)
        except Exception:  # noqa: BLE001
            return None

    def delete(self, ref: str | None) -> bool:
        if not ref or ref not in self._data:
            return False
        del self._data[ref]
        self._save()
        return True

    def has(self, ref: str | None) -> bool:
        return bool(ref) and ref in self._data

    def mask(self, ref: str | None) -> str:
        """给界面用的掩码文本 —— 绝不返回明文。"""
        if not self.has(ref):
            return "未配置"
        plain = self.get(ref) or ""
        if len(plain) <= 8:
            return "已配置"
        return f"{plain[:4]}…{plain[-4:]}"

    # ------------------------------------------------------------------
    # 加密后端
    # ------------------------------------------------------------------
    def _encrypt(self, plaintext: str) -> str:
        wc = _try_win32crypt()
        if wc is not None:
            blob = wc.CryptProtectData(
                plaintext.encode("utf-8"), "litprism", None, None, None, 0
            )
            return PREFIX_DPAPI + base64.b64encode(blob).decode()

        AESGCM = _try_cryptography()
        if AESGCM is not None:
            key = self._aes_key()
            nonce = os.urandom(12)
            ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), b"litprism")
            return PREFIX_AES + base64.b64encode(nonce + ct).decode()

        return PREFIX_WEAK + base64.b64encode(plaintext.encode("utf-8")).decode()

    def _decrypt(self, token: str) -> str:
        if token.startswith(PREFIX_DPAPI):
            wc = _try_win32crypt()
            if wc is None:
                raise RuntimeError("缺少 pywin32，无法解密 DPAPI 密文")
            blob = base64.b64decode(token[len(PREFIX_DPAPI):])
            return wc.CryptUnprotectData(blob, None, None, None, 0)[1].decode("utf-8")

        if token.startswith(PREFIX_AES):
            AESGCM = _try_cryptography()
            if AESGCM is None:
                raise RuntimeError("缺少 cryptography，无法解密 AES-GCM 密文")
            raw = base64.b64decode(token[len(PREFIX_AES):])
            nonce, ct = raw[:12], raw[12:]
            return AESGCM(self._aes_key()).decrypt(nonce, ct, b"litprism").decode("utf-8")

        if token.startswith(PREFIX_WEAK):
            return base64.b64decode(token[len(PREFIX_WEAK):]).decode("utf-8")

        # 兼容历史：无前缀视为 weak
        return base64.b64decode(token).decode("utf-8")

    def _aes_key(self) -> bytes:
        if self._key_path.exists():
            return base64.b64decode(self._key_path.read_text(encoding="utf-8").strip())
        key = secrets.token_bytes(32)
        self._key_path.write_text(base64.b64encode(key).decode(), encoding="utf-8")
        try:  # 尽力收紧权限（Windows 上不一定完全生效）
            os.chmod(self._key_path, 0o600)
        except Exception:  # noqa: BLE001
            pass
        return key

    # ------------------------------------------------------------------
    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
