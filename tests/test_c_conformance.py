"""跨语言一致性测试：C 实现与 Python 实现必须逐字节相同。

上位机（Python）和设备固件（C）如果对同一帧算出不同的校验值，
现象是偶发丢帧、两边单独测都正常，属于联调时较难定位的问题。

这里把两个实现对同一批输入逐字节比对：

    输入 → C 算一遍 → Python 算一遍 → 结果必须相同

输入集合是固定的（见 _corpus()）：8 个边界输入，加上 16 种长度的
随机数据；随机数用固定种子，结果可复现。

编译器可替换
------------
默认用 ``zig cc``（zig 自带 C 编译器，可通过 pip 安装，跨平台）。
有 gcc/clang 时用 ``CC`` 环境变量指定：

    CC=gcc pytest tests/test_c_conformance.py

找不到编译器时整组跳过。CI 上没有 C 工具链不应该让测试失败，
但会明确标记为 skipped，不会当成通过。
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
from pathlib import Path

import pytest

from embedded_testbench.protocol.crc import Crc16, crc8_maxim, crc16_modbus
from embedded_testbench.protocol.dbus import (
    CHANNEL_COUNT,
    CHANNEL_MID,
    encode_dbus,
)
from embedded_testbench.protocol.framing import FrameEncoder

REPO_ROOT = Path(__file__).resolve().parents[1]
C_SOURCE = REPO_ROOT / "firmware" / "crc.c"
BUILD_DIR = REPO_ROOT / "firmware" / "build"


def _find_zig() -> str | None:
    """找 zig（可能在 .pylibs 里，也可能在 PATH 上）。"""
    exe = shutil.which("zig")
    if exe:
        return exe
    try:
        import ziglang  # noqa: PLC0415

        cand = Path(ziglang.__file__).parent / "zig.exe"
        if cand.exists():
            return str(cand)
        cand = Path(ziglang.__file__).parent / "zig"
        if cand.exists():
            return str(cand)
    except ImportError:
        pass
    return None


def _resolve_compiler() -> tuple[str, list[str]] | None:
    """返回 (编译器路径, 编译参数前缀)。

    解析顺序：``CC`` 环境变量 → zig → gcc/clang/cc。

    ``CC`` 支持带参数的写法（如 ``CC="zig cc"`` 或 ``CC="ccache gcc"``）。
    ``zig`` 本身是个多命令驱动器，编译 C 需要 ``zig cc`` 子命令；
    直接把 ``CC=zig`` 当成单纯的可执行文件会调用失败，所以这里要拆开处理。
    """
    env_cc = os.environ.get("CC", "").strip()
    if env_cc:
        parts = env_cc.split()
        exe, args = parts[0], parts[1:]
        resolved = shutil.which(exe) or (exe if Path(exe).exists() else None)
        if resolved is None:
            return None
        # CC 指向 zig 但没给子命令时，自动补 cc
        if "zig" in Path(resolved).name.lower() and not args:
            args = ["cc"]
        return resolved, args

    zig = _find_zig()
    if zig:
        return zig, ["cc"]

    for name in ("gcc", "clang", "cc"):
        exe = shutil.which(name)
        if exe:
            return exe, []
    return None


@pytest.fixture(scope="module")
def crc_cli() -> str:
    """编译 C 源码，返回可执行文件路径。"""
    resolved = _resolve_compiler()
    if resolved is None:
        pytest.skip("未找到 C 编译器（可 pip install ziglang，或设置 CC 环境变量）")
    compiler, prefix = resolved

    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    exe = BUILD_DIR / ("crc_cli.exe" if os.name == "nt" else "crc_cli")

    # zig 需要一个可写的缓存目录；沙箱下默认位置可能不可写
    env = dict(os.environ)
    if "zig" in Path(compiler).name.lower():
        cache = BUILD_DIR / ".zig-cache"
        cache.mkdir(parents=True, exist_ok=True)
        env["ZIG_GLOBAL_CACHE_DIR"] = str(cache)
        env["ZIG_LOCAL_CACHE_DIR"] = str(cache)

    cmd = [compiler, *prefix, "-O2", "-o", str(exe), str(C_SOURCE)]
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, timeout=300,
    )
    if proc.returncode != 0:
        pytest.fail(
            f"C 编译失败（{compiler}）：\n{proc.stdout}\n{proc.stderr}"
        )
    assert exe.exists(), "编译未产出可执行文件"
    return str(exe)


def _run_c(exe: str, *args: str) -> str:
    # 显式指定 encoding：Windows 默认用 GBK 解码子进程输出，
    # 遇到非 GBK 字节会抛 UnicodeDecodeError。
    proc = subprocess.run(
        [exe, *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"C 程序返回 {proc.returncode}: {proc.stderr}")
    return proc.stdout.strip()


def _hex(data: bytes) -> str:
    return data.hex()


# 覆盖边界的输入集合（固定种子 -> 结果可复现）
def _corpus() -> list[bytes]:
    cases: list[bytes] = [
        b"",                       # 空输入
        b"\x00",                   # 单字节 0
        b"\xff",                   # 单字节 1
        b"123456789",              # CRC 标准向量
        bytes(range(256)),         # 全字节范围
        b"\x00" * 64,              # 全 0
        b"\xff" * 64,              # 全 1
        bytes([0x00, 0xFF] * 32),  # 交替
    ]
    rng = random.Random(20261008)  # 固定种子
    for n in (1, 2, 3, 7, 8, 15, 16, 17, 31, 32, 33, 63, 64, 100, 200, 255):
        cases.append(bytes(rng.randrange(256) for _ in range(n)))
    return cases


CORPUS = _corpus()


# ── CRC-16 一致性 ────────────────────────────────────────────

@pytest.mark.parametrize("data", CORPUS, ids=lambda d: f"len{len(d)}")
def test_crc16_matches_python(crc_cli: str, data: bytes):
    """C 与 Python 的 CRC-16 必须完全一致。"""
    c_value = int(_run_c(crc_cli, "crc16", _hex(data)), 16)
    py_value = crc16_modbus(data)
    assert c_value == py_value, (
        f"CRC-16 不一致：C=0x{c_value:04X} Python=0x{py_value:04X} "
        f"（输入 {len(data)} 字节）"
    )


@pytest.mark.parametrize("data", CORPUS[:8], ids=lambda d: f"len{len(d)}")
def test_crc16_byte_order_matches(crc_cli: str, data: bytes):
    """线上字节序（低字节在前）两端必须一致。"""
    c_hex = _run_c(crc_cli, "crc16b", _hex(data))
    py_hex = Crc16().update(data).digest().hex()
    assert c_hex == py_hex, f"CRC 字节序不一致：C={c_hex} Python={py_hex}"


# ── CRC-8 一致性 ─────────────────────────────────────────────

@pytest.mark.parametrize("data", CORPUS, ids=lambda d: f"len{len(d)}")
def test_crc8_matches_python(crc_cli: str, data: bytes):
    c_value = int(_run_c(crc_cli, "crc8", _hex(data)), 16)
    py_value = crc8_maxim(data)
    assert c_value == py_value, (
        f"CRC-8 不一致：C=0x{c_value:02X} Python=0x{py_value:02X}"
    )


# ── 11 位打包一致性 ──────────────────────────────────────────

@pytest.mark.parametrize(
    "values",
    [
        [CHANNEL_MID] * 18,
        [0] * 18,
        [0x07FF] * 18,
        [364, 1684, 1024, 0, 2047] + [1024] * 13,
        list(range(0, 18 * 100, 100)),
    ],
    ids=["mid", "zero", "max", "mixed", "ramp"],
)
def test_channel_packing_matches_python(crc_cli: str, values: list[int]):
    """11 位打包必须逐字节一致——否则设备与上位机解出的通道不同。"""
    c_hex = _run_c(crc_cli, "pack", ",".join(str(v) for v in values))

    # Python 侧：编码后再取反（C 的 pack 不取反，与 invert_level=False 对应）
    py_bytes = bytearray()
    bit = 0
    buf = bytearray(25)
    for v in values:
        v &= 0x07FF
        for b in range(11):
            if (v >> b) & 1:
                buf[bit >> 3] |= 1 << (bit & 7)
            bit += 1
    py_bytes = bytes(buf)

    assert c_hex == py_bytes.hex(), (
        f"11 位打包不一致：\n  C      = {c_hex}\n  Python = {py_bytes.hex()}"
    )


def test_packed_frame_matches_python(crc_cli: str):
    """完整走一遍：C 打包 + C 算 CRC，与 Python 的 encode_dbus 一致。

    注意通道总数是 **16 比例通道 + 2 开关通道 = 18 个值**。
    只喂 16 个会让两边长度不同（C 少打 2 个值，尾部字节为 0），
    所以跨语言比对必须把输入规模也对齐，不能只对算法。
    """
    channels = [364, 1684, 1024, 1500] + [CHANNEL_MID] * (CHANNEL_COUNT - 4)
    switches = [CHANNEL_MID, CHANNEL_MID]
    all_values = channels + switches
    assert len(all_values) == 18, "16 比例通道 + 2 开关通道"

    c_hex = _run_c(crc_cli, "pack", ",".join(str(v) for v in all_values))
    py_hex = encode_dbus(channels, switches, invert_level=False).hex()
    assert c_hex == py_hex


# ── 反相 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("data", CORPUS[:8], ids=lambda d: f"len{len(d)}")
def test_invert_matches_python(crc_cli: str, data: bytes):
    """电平反相（软件 ^0xFF）两端必须一致。"""
    c_hex = _run_c(crc_cli, "invert", _hex(data))
    from embedded_testbench.protocol.dbus import invert

    assert c_hex == invert(data).hex()


# ── 整帧 ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "payload",
    [b"", b"\x01", b"hello world", bytes(range(32)), b"\xff" * 100],
    ids=lambda p: f"len{len(p)}",
)
def test_frame_encoding_matches_python(crc_cli: str, payload: bytes):
    """整帧编码（帧头+长度+载荷+CRC）两端一致。

    同时覆盖 CRC 算法、字节序，以及 CRC 的覆盖范围
    （长度字段是否参与计算）。
    """
    c_hex = _run_c(crc_cli, "frame", _hex(payload))
    py_hex = FrameEncoder.encode(payload).hex()
    assert c_hex == py_hex, (
        f"整帧编码不一致：\n  C      = {c_hex}\n  Python = {py_hex}"
    )


def test_cli_reports_usage_on_bad_args(crc_cli: str):
    """参数不合法时返回非 0 退出码，便于 CI 判定。"""
    proc = subprocess.run(
        [crc_cli], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30,
    )
    assert proc.returncode != 0
