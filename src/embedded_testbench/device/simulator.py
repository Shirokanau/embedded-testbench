"""设备模拟器：用软件扮演一块真实的嵌入式板子。

它和上位机测试框架共用同一套传输层，所以：

    host  --write-->  LoopbackTransport  --device_read-->  simulator
    host  <--read---  LoopbackTransport  <--device_write--  simulator

跑起来就是一条完整的链路，唯一缺的是那根 USB 线。

模拟器实现了一个虚构但自洽的设备 **DBUS-A5 遥控接收模块**：

* 收到 ``0x01`` 命令 → 回一帧 18 字节 DBUS 数据（16 通道 + 2 开关）
* 收到 ``0x02`` 命令 → 回版本号
* 收到 ``0x03`` 命令 → 回自检结果（模拟一个可能失败的自检）
* 收到未知命令 → 回错误码 ``0xE0`` + 原命令（协议错误处理）

这个"命令 → 响应"的形状，和真实的 FAE/测试日常面对的模块
（温湿度模块、电机驱动、射频前端）完全一致。
"""

from __future__ import annotations

import threading
import time

from ..protocol.dbus import (
    CHANNEL_COUNT,
    CHANNEL_MID,
    encode_dbus,
)
from ..protocol.framing import FrameDecoder, FrameEncoder
from ..transport.loopback import LoopbackTransport

__all__ = ["DbusA5Simulator", "CMD_READ_CHANNELS", "CMD_VERSION", "CMD_SELFTEST"]

CMD_READ_CHANNELS = 0x01
CMD_VERSION = 0x02
CMD_SELFTEST = 0x03
CMD_ECHO = 0x04

ERR_UNKNOWN_CMD = 0xE0
ERR_BAD_PARAM = 0xE1

VERSION = (1, 3, 0)


class DbusA5Simulator:
    """跑在独立线程里的假设备。

    线程模型贴近真实：设备的"固件"在一个循环里轮询接收缓冲，
    上位机测试不需要知道对面是线程还是真串口。
    """

    def __init__(
        self,
        transport: LoopbackTransport,
        channel_values: list[int] | None = None,
        selftest_ok: bool = True,
        response_delay_s: float = 0.0,
        noise: bool = False,
    ) -> None:
        self.transport = transport
        self.response_delay_s = response_delay_s
        self.noise = noise
        self.selftest_ok = selftest_ok

        # 默认给一组典型值：摇杆中位 1024，油门最低
        self.channel_values = channel_values or self._default_channels()
        self.switches = [CHANNEL_MID, CHANNEL_MID]

        self._decoder = FrameDecoder()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.stats = {"commands": 0, "unknown": 0, "bytes_tx": 0, "frames_received": 0}
        self.received_commands: list[int] = []

    @staticmethod
    def _default_channels() -> list[int]:
        """造一组"手在遥控器上"的合理值。"""
        base = [1024] * CHANNEL_COUNT
        base[0] = 1684  # 副翼打满右
        base[1] = 364   # 升降打满下
        base[2] = 1400  # 油门偏高
        base[3] = 1024  # 方向中位
        base[4] = 364   # 开关位
        return base

    # ── 生命周期 ────────────────────────────────────────────────
    def start(self) -> DbusA5Simulator:
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="dbus-a5-sim", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def __enter__(self) -> DbusA5Simulator:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ── 主循环 ──────────────────────────────────────────────────
    def _loop(self) -> None:
        while not self._stop.is_set():
            chunk = self.transport.device_read(64)
            if not chunk:
                time.sleep(0.001)
                continue

            for frame, error in self._decoder.feed(chunk):
                if error is not None or frame is None:
                    continue
                self.stats["frames_received"] += 1
                self._handle(frame.payload)

    def _handle(self, payload: bytes) -> None:
        if not payload:
            self._send_error(ERR_BAD_PARAM, 0x00)
            return

        cmd = payload[0]
        self.received_commands.append(cmd)

        if self.response_delay_s:
            time.sleep(self.response_delay_s)

        if cmd == CMD_READ_CHANNELS:
            self._send(encode_dbus(self.channel_values, self.switches))
        elif cmd == CMD_VERSION:
            self._send(bytes(VERSION))
        elif cmd == CMD_SELFTEST:
            # 自检结果：0x00 通过 / 0x01 传感器异常
            self._send(bytes((0x00 if self.selftest_ok else 0x01,)))
        elif cmd == CMD_ECHO:
            self._send(payload[1:])
        else:
            self.stats["unknown"] += 1
            self._send_error(ERR_UNKNOWN_CMD, cmd)

    def _send_error(self, code: int, echo: int) -> None:
        self._send(bytes((code, echo)))

    def _send(self, payload: bytes) -> None:
        if self.noise:
            # 帧头前撒点垃圾字节，验证上位机能不能重新同步
            self.transport.device_write(b"\x00\xff\x11")
        data = FrameEncoder.encode(payload)
        self.transport.device_write(data)
        self.stats["bytes_tx"] += len(data)
        self.stats["commands"] += 1
