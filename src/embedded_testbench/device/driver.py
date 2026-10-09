"""上位机驱动：像操作真实模块一样操作设备。

这一层是**被测系统与测试框架之间的契约**。测试用例只调用
``DbusA5Driver.read_channels()``，不关心底层是虚拟串口还是 COM3。

工程上这就是"面向接口编程"在测试里的价值——同一套测试用例，
开发阶段跑模拟器，实验室跑真板子，CI 跑模拟器，**一行不用改**。
"""

from __future__ import annotations

import time

from ..device.simulator import (
    CMD_ECHO,
    CMD_READ_CHANNELS,
    CMD_SELFTEST,
    CMD_VERSION,
)
from ..protocol.dbus import DbusFrame
from ..protocol.framing import FrameDecoder, FrameEncoder
from ..transport.loopback import Transport

__all__ = ["DbusA5Driver", "DeviceTimeout", "DeviceError"]


class DeviceTimeout(Exception):
    """在超时时间内没有拿到完整响应。"""


class DeviceError(Exception):
    """设备返回了错误码。"""

    def __init__(self, code: int, echoed: int = 0) -> None:
        super().__init__(f"设备错误码 0x{code:02X}（回显 0x{echoed:02X}）")
        self.code = code
        self.echoed = echoed


class DbusA5Driver:
    """DBUS-A5 模块的上位机协议驱动。"""

    def __init__(self, transport: Transport, default_timeout_s: float = 2.0) -> None:
        self.transport = transport
        self.default_timeout_s = default_timeout_s
        self._decoder = FrameDecoder()
        self.stats = {"tx_frames": 0, "rx_frames": 0, "timeouts": 0, "retries": 0}

    # ── 底层收发 ────────────────────────────────────────────────
    def _send(self, payload: bytes) -> None:
        self.transport.write(FrameEncoder.encode(payload))
        self.stats["tx_frames"] += 1

    def _recv_frame(self, timeout_s: float | None = None) -> bytes:
        """收一帧；超时抛 :class:`DeviceTimeout`。"""
        deadline = time.perf_counter() + (timeout_s or self.default_timeout_s)
        while time.perf_counter() < deadline:
            chunk = self.transport.read(64)
            if chunk:
                for frame, _error in self._decoder.feed(chunk):
                    if frame is not None:
                        self.stats["rx_frames"] += 1
                        return frame.payload
            else:
                time.sleep(0.001)
        self.stats["timeouts"] += 1
        raise DeviceTimeout(f"{timeout_s or self.default_timeout_s}s 内未收到响应")

    def request(self, payload: bytes, timeout_s: float | None = None,
                retries: int = 0) -> bytes:
        """发送并等待响应，可选重传。

        重传是真实链路的必备能力：串口没有 TCP 的重传，丢一帧
        就得靠应用层补。测试里会专门验证重传逻辑正确。
        """
        attempt = 0
        while True:
            self._decoder.reset()  # 丢弃上一轮的残留，避免错位
            self._send(payload)
            try:
                return self._recv_frame(timeout_s)
            except DeviceTimeout:
                if attempt >= retries:
                    raise
                attempt += 1
                self.stats["retries"] += 1

    # ── 业务接口 ────────────────────────────────────────────────
    def read_channels(self, timeout_s: float | None = None) -> DbusFrame:
        """读一帧遥控器通道数据，解析成 :class:`DbusFrame`。"""
        from ..protocol.dbus import DbusDecoder

        raw = self.request(bytes((CMD_READ_CHANNELS,)), timeout_s)
        if raw and raw[0] in (0xE0, 0xE1):
            raise DeviceError(raw[0], raw[1] if len(raw) > 1 else 0)
        return DbusDecoder().decode(raw)

    def get_version(self, timeout_s: float | None = None) -> tuple[int, int, int]:
        raw = self.request(bytes((CMD_VERSION,)), timeout_s)
        if len(raw) != 3:
            raise DeviceError(raw[0] if raw else -1)
        return (raw[0], raw[1], raw[2])

    def selftest(self, timeout_s: float | None = None) -> bool:
        raw = self.request(bytes((CMD_SELFTEST,)), timeout_s)
        return bool(raw and raw[0] == 0x00)

    def echo(self, data: bytes, timeout_s: float | None = None) -> bytes:
        return self.request(bytes((CMD_ECHO,)) + data, timeout_s)

    def send_unknown_command(self, code: int = 0x7F,
                             timeout_s: float | None = None) -> bytes:
        return self.request(bytes((code,)), timeout_s)
