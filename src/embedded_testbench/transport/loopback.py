"""虚拟串口：让上位机测试框架在没有真实硬件时也能端到端跑通。

为什么需要它
------------
自动化测试要被 CI 跑起来，就不能依赖"插着板子"。业界常见做法是
把设备抽象成接口，测试时用**假的设备**替换真设备：

* 真实场景：``SerialTransport("COM3", 100000)``
* 测试/CI：``LoopbackTransport()`` —— 一个内存里的环形缓冲，
  上位机写进去的字节，设备端能读到；设备端写的，上位机也能读到。

关键在于**测试用的传输层和真实传输层实现同一个接口**，所以
测试覆盖的是真实的解析/超时/重传逻辑，而不是一堆 mock。

另外这里实现了**故障注入**（fault injection）——可以按需制造
丢字节、坏 CRC、延迟、半包等异常。这是测试框架比"手点一遍"强的地方：
异常场景可以稳定复现，而不是靠运气碰。
"""

from __future__ import annotations

import threading
import time
from typing import Protocol

__all__ = ["Transport", "LoopbackTransport", "FaultConfig"]


class Transport(Protocol):
    """串口传输层接口——真实串口与虚拟串口共用。"""

    def write(self, data: bytes) -> int: ...
    def read(self, size: int = 1) -> bytes: ...
    def close(self) -> None: ...


class FaultConfig:
    """故障注入配置。

    每一项都是一类真实出现过的现场问题：

    * ``drop_bytes`` —— 模拟 DMA 丢帧/缓冲区溢出导致的字节丢失
    * ``corrupt_every`` —— 每 N 帧翻转 1 个 bit，模拟电磁干扰
    * ``truncate_every`` —— 每 N 帧只发一半，模拟半包后连接断开
    * ``delay_s`` —— 每帧延迟，用于测试超时逻辑
    """

    def __init__(
        self,
        drop_bytes: int = 0,
        corrupt_every: int = 0,
        truncate_every: int = 0,
        delay_s: float = 0.0,
        duplicate_every: int = 0,
    ) -> None:
        self.drop_bytes = drop_bytes
        self.corrupt_every = corrupt_every
        self.truncate_every = truncate_every
        self.delay_s = delay_s
        self.duplicate_every = duplicate_every

    @property
    def active(self) -> bool:
        return any((
            self.drop_bytes, self.corrupt_every, self.truncate_every,
            self.delay_s, self.duplicate_every,
        ))


class LoopbackTransport:
    """内存回环串口，线程安全。

    一端是"上位机"（测试框架），另一端是"设备"（模拟器）。
    两个方向各有独立缓冲，行为贴近真实串口：写进去的字节不会
    凭空消失，读不到就是空。
    """

    def __init__(self, faults: FaultConfig | None = None, mtu: int = 64) -> None:
        self._to_device = bytearray()
        self._to_host = bytearray()
        self._lock = threading.Lock()
        self._closed = False
        # mtu 模拟串口一次中断最多搬多少字节——真实串口不会一次给你一整帧
        self._mtu = mtu
        self.faults = faults or FaultConfig()
        self._frame_count = 0
        self.written_bytes = 0
        self.read_bytes = 0

    # ── 上位机侧 ────────────────────────────────────────────────
    def write(self, data: bytes) -> int:
        """上位机 -> 设备。会在这里施加故障。"""
        if self._closed:
            raise RuntimeError("transport 已关闭")

        payload = self._apply_faults(data)
        with self._lock:
            self._to_device.extend(payload)
            self.written_bytes += len(payload)
        if self.faults.delay_s:
            time.sleep(self.faults.delay_s)
        return len(payload)

    def read(self, size: int = 1) -> bytes:
        """上位机 <- 设备。一次最多读 mtu 字节。"""
        if self._closed:
            raise RuntimeError("transport 已关闭")
        with self._lock:
            n = min(size, self._mtu, len(self._to_host))
            if n <= 0:
                return b""
            out = bytes(self._to_host[:n])
            del self._to_host[:n]
            self.read_bytes += len(out)
            return out

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._to_host)

    # ── 设备侧（供模拟器使用）───────────────────────────────────
    def device_read(self, size: int = 256) -> bytes:
        with self._lock:
            n = min(size, len(self._to_device))
            if n <= 0:
                return b""
            out = bytes(self._to_device[:n])
            del self._to_device[:n]
            return out

    def device_write(self, data: bytes) -> int:
        with self._lock:
            self._to_host.extend(data)
        return len(data)

    def close(self) -> None:
        self._closed = True

    # ── 故障注入 ────────────────────────────────────────────────
    def _apply_faults(self, data: bytes) -> bytes:
        f = self.faults
        if not f.active:
            return data

        self._frame_count += 1
        n = self._frame_count
        out = data

        if f.drop_bytes and n % 2 == 0:
            out = out[: max(0, len(out) - f.drop_bytes)]

        if f.truncate_every and n % f.truncate_every == 0:
            out = out[: max(1, len(out) // 2)]

        if f.corrupt_every and n % f.corrupt_every == 0 and out:
            mutated = bytearray(out)
            # 翻转中间字节的一个 bit——模拟线上干扰
            mid = len(mutated) // 2
            mutated[mid] ^= 0x01
            out = bytes(mutated)

        if f.duplicate_every and n % f.duplicate_every == 0:
            out = out + out

        return out
