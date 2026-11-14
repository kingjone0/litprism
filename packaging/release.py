"""一条命令产出可交付安装包，并自带校验。

    python packaging/release.py                 # 打 sidecar + 冒烟 + 安装包 + 校验
    python packaging/release.py --skip-smoke    # 跳过冒烟（环境不允许起进程时）
    python packaging/release.py --skip-installer

为什么要有这个脚本：产物落后代码已经发生过两次（10-19→10-29 停在 v10，代码已 v15）。
手工三步（build.py → electron-builder → 上传）迟早会再错一次，所以把"打完之后到底对不对"
也写进来：冒烟要建出当前 schema 的库、安装包里嵌的 sidecar 必须和 dist 里是同一个文件。

产物一律落在 `packaging/dist/` 与 `packaging/installer/`（都已 gitignore）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PKG = Path(__file__).resolve().parent
ROOT = PKG.parent
DIST = PKG / "dist"
INSTALLER_DIR = PKG / "installer"
SIDECAR = DIST / "litprism-sidecar.exe"
UNPACKED_SIDECAR = INSTALLER_DIR / "win-unpacked" / "resources" / "litprism-sidecar.exe"
MANIFEST = INSTALLER_DIR / "release-manifest.json"
SCHEMA_PY = ROOT / "src"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def git_state() -> dict:
    def run(*args: str) -> str:
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True,
                                  timeout=10).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    return {"commit": run("rev-parse", "--short", "HEAD"),
            "dirty": bool(run("status", "--porcelain"))}


def code_schema_version() -> int:
    """读源码里的 SCHEMA_VERSION（不依赖当前工作目录是否装了这个包）。"""
    sys.path.insert(0, str(SCHEMA_PY))
    from litprism.schema import SCHEMA_VERSION  # noqa: PLC0415
    return int(SCHEMA_VERSION)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def smoke_sidecar() -> dict:
    """起打包件、等它就绪、看它建出来的库是不是当前 schema。"""
    want = code_schema_version()
    port = _free_port()
    home = Path(tempfile.mkdtemp(prefix="litprism-release-smoke-"))
    env = dict(os.environ, LOCALAPPDATA=str(home))
    started = time.monotonic()
    proc = subprocess.Popen([str(SIDECAR), "--port", str(port), "--no-token"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        status = None
        for _ in range(40):
            if proc.poll() is not None:
                raise RuntimeError(f"sidecar 启动即退出（code={proc.returncode}）")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2) as resp:
                    status = json.loads(resp.read().decode("utf-8"))
                break
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(0.5)
        if status is None:
            raise RuntimeError("sidecar 20 秒内没有响应 /api/status")

        db_path = Path(str((status.get("stats") or {}).get("db_path") or ""))
        if not db_path.exists():
            raise RuntimeError(f"/api/status 给的库路径不存在：{db_path}")
        # 冒烟时 LOCALAPPDATA 本身就在临时目录里，所以不能拿"是否在 Temp 下"当判据。
        # 真正要防的是：frozen 忘了拆 BUNDLE_ROOT/APP_ROOT，库落进 _MEIxxxx 解包目录。
        expected_root = home / "LitPrism"
        resolved = db_path.resolve()
        if expected_root.resolve() not in resolved.parents:
            raise RuntimeError(f"库没有落在 LOCALAPPDATA 下：{db_path}")
        if any(part.upper().startswith("_MEI") for part in resolved.parts):
            raise RuntimeError(f"库落进 PyInstaller 解包目录（frozen 路径回退）：{db_path}")

        with sqlite3.connect(db_path) as conn:
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            has_role = any(row[1] == "section_role" for row in conn.execute("PRAGMA table_info(chunks)"))
        if version != want:
            raise RuntimeError(f"打包件建库 v{version}，源码是 v{want} —— 产物落后于代码")
        return {"schema": version, "db_path": str(db_path), "section_role": has_role,
                "seconds": round(time.monotonic() - started, 1)}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(home, ignore_errors=True)


def build_sidecar() -> None:
    print("== 1/3 打 sidecar ==", flush=True)
    rc = subprocess.run([sys.executable, str(PKG / "build.py")], cwd=str(ROOT)).returncode
    if rc != 0 or not SIDECAR.exists():
        raise SystemExit("sidecar 构建失败")


def build_installer() -> None:
    print("== 3/3 打安装包 ==", flush=True)
    builder = ROOT / "node_modules" / ".bin" / "electron-builder.cmd"
    if not builder.exists():
        raise SystemExit(f"找不到 electron-builder：{builder}（先 npm install）")
    rc = subprocess.run(["cmd", "/c", str(builder), "--win"], cwd=str(ROOT)).returncode
    if rc != 0:
        raise SystemExit("安装包构建失败")


def verify(schema: int) -> dict:
    if not UNPACKED_SIDECAR.exists():
        raise SystemExit(f"安装包里没有 sidecar：{UNPACKED_SIDECAR}")
    if UNPACKED_SIDECAR.stat().st_size != SIDECAR.stat().st_size:
        raise SystemExit("安装包内 sidecar 与 dist 体积不一致")
    if sha256(UNPACKED_SIDECAR) != sha256(SIDECAR):
        raise SystemExit("安装包内 sidecar 与 dist 内容不一致（打包时用了旧产物）")

    installers = sorted(INSTALLER_DIR.glob("LitPrism-Setup-*.exe"), key=lambda p: p.stat().st_mtime)
    if not installers:
        raise SystemExit("没有生成安装包")
    setup = installers[-1]
    if setup.stat().st_mtime < SIDECAR.stat().st_mtime - 5:
        raise SystemExit(f"安装包比 sidecar 旧：{setup.name}")

    manifest = {
        "git": git_state(),
        "schema": schema,
        "sidecar": {"path": str(SIDECAR), "bytes": SIDECAR.stat().st_size, "sha256": sha256(SIDECAR)},
        "installer": {"path": str(setup), "bytes": setup.stat().st_size},
        "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="一条命令产出安装包并校验")
    parser.add_argument("--skip-smoke", action="store_true", help="不启动打包件做冒烟")
    parser.add_argument("--skip-installer", action="store_true", help="只打 sidecar")
    args = parser.parse_args()

    state = git_state()
    print(f"提交 {state['commit'] or '?'}{'（工作区有未提交改动）' if state['dirty'] else ''}")

    build_sidecar()

    smoke = None
    if not args.skip_smoke:
        print("== 2/3 冒烟（起打包件看它建什么库）==", flush=True)
        smoke = smoke_sidecar()
        print(f"   schema v{smoke['schema']}、section_role={'有' if smoke['section_role'] else '缺'}、{smoke['seconds']}s")

    if args.skip_installer:
        print("\n跳过安装包；sidecar 已就绪。")
        return 0

    build_installer()
    manifest = verify(code_schema_version())

    print("\n== 产物 ==")
    print(f"  sidecar   {manifest['sidecar']['bytes'] / 1048576:.1f} MB  {manifest['sidecar']['sha256'][:12]}")
    print(f"  安装包    {manifest['installer']['bytes'] / 1048576:.1f} MB  {Path(manifest['installer']['path']).name}")
    print(f"  清单      {MANIFEST}")
    print("校验通过：内嵌 sidecar 与 dist 同一份，schema 与源码一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
