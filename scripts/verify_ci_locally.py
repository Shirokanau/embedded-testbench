"""在本地按顺序执行 CI 中的每一步并核对结果。

步骤、命令和期望的退出码都取自 .github/workflows/ci.yml。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _find_dir(name: str) -> Path | None:
    """从仓库根目录向上查找名为 name 的目录。

    这些依赖目录可能位于仓库外（monorepo 的另一层，或者上一级的共享环境），
    所以逐级向上找，找不到就返回 None。也可以直接用环境变量指定。
    """
    override = os.environ.get(f"{name.strip('.').upper()}_DIR")
    if override:
        p = Path(override).expanduser()
        if p.is_dir():
            return p

    for base in [ROOT, *ROOT.parents]:
        candidate = base / name
        if candidate.is_dir():
            return candidate
    return None


# 存放 pytest / ruff / ziglang 的目录（仓库外的共享依赖目录）
PYLIBS = _find_dir(".pylibs")

steps: list[tuple[str, list[str], dict[str, str]]] = []

base_env = dict(os.environ)
base_env["PYTHONIOENCODING"] = "utf-8"
_path_parts = [str(ROOT / "src")]
if PYLIBS is not None:
    _path_parts.append(str(PYLIBS))
base_env["PYTHONPATH"] = os.pathsep.join(_path_parts)


def add(name: str, cmd: list[str], **env: str) -> None:
    e = dict(base_env)
    e.update(env)
    steps.append((name, cmd, e))


# 1) C 编译（对应 CI 的 firmware 任务，带 -Wall -Wextra -Werror）
zig = (PYLIBS / "ziglang" / "zig.exe") if PYLIBS is not None else None
if zig is not None and zig.exists():
    cc_cmd = [str(zig), "cc"]
    cc_env_value = f"{zig} cc"      # 与 test_c_conformance 的 CC 解析保持一致
else:
    cc_cmd = ["gcc"]
    cc_env_value = "gcc"

zigcache = ROOT / "firmware" / "build" / ".zig-cache"
zigcache.mkdir(parents=True, exist_ok=True)
add("C 编译（-Wall -Wextra -Werror）",
    [*cc_cmd, "-std=c99", "-O2", "-Wall", "-Wextra", "-Werror",
     "-o", str(ROOT / "firmware" / "build" / "crc_cli_ci"), str(ROOT / "firmware" / "crc.c")],
    ZIG_GLOBAL_CACHE_DIR=str(zigcache), ZIG_LOCAL_CACHE_DIR=str(zigcache))

# 2) C 标准向量冒烟（对应 CI 的 firmware 冒烟步骤）
exe = ROOT / "firmware" / "build" / "crc_cli_ci"
steps.append(("C 标准向量 CRC-16", [str(exe), "crc16", "313233343536373839"], dict(base_env)))
steps.append(("C 标准向量 CRC-8", [str(exe), "crc8", "313233343536373839"], dict(base_env)))

# 3) pytest（对应 CI 的 test 任务）
add("pytest 全量", [sys.executable, "-m", "pytest", "tests", "-q", "--no-header",
                    "-p", "no:cacheprovider"], CC=cc_env_value)

# 4) 测试台正常场景（对应 CI 的 testbench 任务）
add("测试台 run（期望退出码 0）",
    [sys.executable, "-m", "embedded_testbench", "run", "--out", "reports"],
    PYTHONPATH=str(ROOT / "src"))

# 5) 测试台故障注入（对应 CI 的 canary，期望非 0）
add("测试台 canary（期望非 0）",
    [sys.executable, "-m", "embedded_testbench", "run",
     "--out", "reports/canary", "--selftest-fail"],
    PYTHONPATH=str(ROOT / "src"))

# 6) ruff 静态检查（对应 CI 的 lint 任务）
# 找不到可用的 ruff 就跳过并说明。
def _find_ruff() -> str | None:
    """返回一个可以正常执行的 ruff 路径。

    查找顺序：``RUFF`` 环境变量 → PATH → 当前解释器环境。
    ruff 的 wheel 把二进制装在 ``<target>/bin/ruff``，不在 target 根目录，
    所以两个位置都要找。只检查文件存在不够，还要试运行一次，
    避免拿到损坏或架构不符的可执行文件。
    """
    candidates: list[str] = []

    override = os.environ.get("RUFF", "").strip()
    if override:
        candidates.append(override)

    w = shutil.which("ruff")
    if w:
        candidates.append(w)

    roots = [Path(sys.prefix) / "Scripts", Path(sys.prefix) / "bin"]
    if PYLIBS is not None:
        roots += [PYLIBS, PYLIBS / "bin"]
    for base in roots:
        for name in ("ruff.exe", "ruff"):
            candidates.append(str(base / name))

    for c in candidates:
        if not Path(c).exists():
            continue
        try:
            r = subprocess.run([c, "--version"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60)
            if r.returncode == 0:
                return c
        except (OSError, subprocess.SubprocessError):
            continue
    return None


_ruff = _find_ruff()
if _ruff:
    add("ruff 静态检查", [_ruff, "check", "src", "tests"])
else:
    print("[跳过] ruff 静态检查（本机没有可用的 ruff；CI 上会跑）")

EXPECT = {
    "C 标准向量 CRC-16": ("4b37", 0),
    "C 标准向量 CRC-8": ("a1", 0),
    "测试台 run（期望退出码 0）": (None, 0),
    "测试台 canary（期望非 0）": (None, 1),   # 必须失败
}

failures = 0
for name, cmd, env in steps:
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env,
                          cwd=str(ROOT), timeout=900)
    want_stdout, want_code = EXPECT.get(name, (None, 0))
    ok = proc.returncode == want_code
    if want_stdout is not None:
        ok = ok and proc.stdout.strip().splitlines()[-1].strip() == want_stdout

    status = "OK  " if ok else "FAIL"
    if not ok:
        failures += 1
    exp = f"exit={want_code}" + (f" out={want_stdout}" if want_stdout else "")
    print(f"[{status}] {name}  (实际 exit={proc.returncode}, 期望 {exp})")

    if not ok:
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-12:]
        for line in tail:
            print(f"        | {line}")

print()
print("=" * 60)
print("CI 本地复现：全部通过" if failures == 0 else f"CI 本地复现：{failures} 步不通过")
print("=" * 60)
sys.exit(1 if failures else 0)
