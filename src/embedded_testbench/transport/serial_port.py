"""真实串口传输层：接上物理设备时用这个替换回环。

与 :class:`~embedded_testbench.transport.loopback.LoopbackTransport`
实现同一个接口，因此测试用例不需要改动。

DBUS 的串口参数比较少见，这里固化成常量，避免每次调用都写错：

* 波特率 ``100000``（不是 115200）
* 偶校验（Even）
* 8 数据位 / 1 停止位

``pyserial`` 是可选依赖：只跑模拟器时不需要安装。
"""

from __future__ import annotations

__all__ = ["SerialTransport", "DBUS_SERIAL_KWARGS", "SerialUnavailable"]

# 这三个值错一个就收不到数据
DBUS_SERIAL_KWARGS = {
    "baudrate": 100000,
    "bytesize": 8,
    "parity": "E",  # 偶校验
    "stopbits": 1,
}


class SerialUnavailable(RuntimeError):
    """没有可用的 pyserial，或串口打不开。"""


def _require_serial():
    try:
        import serial  # noqa: PLC0415 - 可选依赖，延迟导入
    except ImportError as e:  # pragma: no cover - 环境相关
        raise SerialUnavailable(
            "未安装 pyserial，无法使用真实串口。安装：pip install pyserial\n"
            "（只跑模拟器则不需要）"
        ) from e
    return serial


class SerialTransport:
    """基于 pyserial 的真实串口。"""

    def __init__(self, port: str, timeout: float = 0.05, **overrides) -> None:
        serial = _require_serial()
        kwargs = dict(DBUS_SERIAL_KWARGS)
        kwargs.update(overrides)
        try:
            self._ser = serial.Serial(port=port, timeout=timeout, **kwargs)
        except Exception as e:  # noqa: BLE001 - 交给调用方判定
            raise SerialUnavailable(f"打开串口 {port} 失败：{e}") from e

    def write(self, data: bytes) -> int:
        return self._ser.write(data)

    def read(self, size: int = 1) -> bytes:
        return self._ser.read(size)

    @property
    def in_waiting(self) -> int:
        return self._ser.in_waiting

    def reset_input_buffer(self) -> None:
        """清空接收缓冲——重传前调用，避免读到上一轮的残留。"""
        self._ser.reset_input_buffer()

    def close(self) -> None:
        self._ser.close()

    def __enter__(self) -> SerialTransport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
