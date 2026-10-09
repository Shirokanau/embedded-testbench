"""CRC 校验实现，用于串口帧的完整性校验。

嵌入式设备之间的串口通信没有 TCP 那样的重传保障，噪声、波特率不匹配、
电平反相等问题都会让接收端拿到"看起来正常"的坏数据。所以每一帧都要带校验，
这是工业协议（Modbus、DBUS 等）的通用做法。

本模块实现两个最常用的变体：

* :func:`crc8_maxim` —— Dallas/Maxim CRC-8，单总线器件（如 DS18B20、部分
  温湿度传感器）常用。
* :func:`crc16_modbus` —— Modbus RTU 用的 CRC-16，工业现场出现频率最高。

两个函数都提供"整帧校验"和"分段更新"两种用法，后者用于流式接收时
边收边算，避免等到整帧收完再遍历一遍。
"""

from __future__ import annotations

__all__ = ["crc8_maxim", "crc16_modbus", "Crc16"]

# CRC-8/MAXIM: poly=0x31 (reflected 0x8C), init=0x00, refin/refout=true, xorout=0x00
_CRC8_TABLE: tuple[int, ...] = ()


def _build_crc8_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
        table.append(crc)
    return tuple(table)


_CRC8_TABLE = _build_crc8_table()


def crc8_maxim(data: bytes, crc: int = 0x00) -> int:
    """计算 CRC-8/MAXIM。

    参数
    ----
    data:
        要参与计算的字节。
    crc:
        上一段的中间结果，用于流式校验；首次调用传 ``0x00``。
    """
    for byte in data:
        crc = _CRC8_TABLE[crc ^ byte]
    return crc


class Crc16:
    """CRC-16/MODBUS 的增量计算器。

    Modbus 的 CRC 有两个容易踩的坑，这里都按标准实现：

    1. **字节序**：算出来的 16 位值在报文里是 **低字节在前**（小端）。
       直接用 ``struct.pack('>H', crc)`` 会得到错误的结果。
    2. **初值/异或值**：init = 0xFFFF，结果最后要异或 0x0000（即不变），
       但反射（refin/refout）都必须为 true。

    例子
    ----
    >>> Crc16().update(b"123456789").value == 0x4B37
    True
    """

    __slots__ = ("_crc",)

    def __init__(self, init: int = 0xFFFF) -> None:
        self._crc = init

    def update(self, data: bytes) -> Crc16:
        """喂入一段数据，返回 self 以便链式调用。"""
        crc = self._crc
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
        self._crc = crc
        return self

    @property
    def value(self) -> int:
        """当前 CRC 值（未交换字节序）。"""
        return self._crc

    def digest(self) -> bytes:
        """按 Modbus 线上格式输出：低字节在前。"""
        return bytes((self._crc & 0xFF, (self._crc >> 8) & 0xFF))


def crc16_modbus(data: bytes) -> int:
    """一次性计算 CRC-16/MODBUS 的整数值。

    >>> hex(crc16_modbus(b"123456789"))
    '0x4b37'
    """
    return Crc16().update(data).value
