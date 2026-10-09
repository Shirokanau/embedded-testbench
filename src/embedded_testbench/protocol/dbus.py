"""11 位打包的遥控通道协议解析（DBUS / SBUS 家族）。

背景
----
机器人竞赛与航模用的遥控接收机，普遍把多个通道**按 11 位紧凑打包**
成一个字节流，而不是每个通道给 1 个或 2 个整字节。原因是省带宽：
16 个通道如果各占 2 字节要 32 字节，按 11 位打包只要 22 字节。

这个"11 位跨字节打包"是嵌入式协议解析里最经典的坑之一，因为它
意味着**一个通道的值横跨两个字节的边界**，必须做位移拼接：

.. code-block:: text

    通道0 =  bit0..bit10
    通道1 =  bit11..bit21     <- 从字节1的第3位开始，跨到字节2
    通道2 =  bit22..bit32
    ...

对应到代码就是 ``(buf[1] >> 3) | (buf[2] << 5)`` 这种写法，
少写一位、错一个移位方向，通道值就会乱跳。

本实现覆盖的真实工程细节：

1. **11 位掩码 ``0x07FF``**——拼接后必须截断，否则高位会串进相邻通道；
2. **电平反相**——这类总线的电平定义可能与普通串口相反，需要
   硬件 74HC04 反相器或软件 ``^0xFF``；
3. **有效范围校验**——通道原始值正常落在 364 ~ 1684（中位约 1024），
   超出说明帧损坏或遥控器失联。

关于帧长
--------
11 位打包下，通道数决定帧长：``ceil(通道数 × 11 / 8)`` 字节。
本模块按 16 个比例通道 + 2 个开关通道实现，因此是
``ceil(18 × 11 / 8) = 25`` 字节。

真实设备上的变体不少（例如大疆 DBUS 用的是 18 字节帧、只携带其中
一部分通道）。本模块实现的是打包规则本身（位拼接、掩码、反相），
不针对某个具体型号的帧长与通道分配。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "DbusFrame",
    "DbusDecoder",
    "DbusDecodeError",
    "encode_dbus",
    "invert",
    "CHANNEL_COUNT",
    "SWITCH_COUNT",
    "BITS_PER_CHANNEL",
    "DBUS_FRAME_SIZE",
    "CHANNEL_MID",
    "CHANNEL_MIN",
    "CHANNEL_MAX",
]

BITS_PER_CHANNEL = 11
CHANNEL_MASK = (1 << BITS_PER_CHANNEL) - 1  # 0x07FF
CHANNEL_COUNT = 16
SWITCH_COUNT = 2
TOTAL_CHANNELS = CHANNEL_COUNT + SWITCH_COUNT
DBUS_FRAME_SIZE = (TOTAL_CHANNELS * BITS_PER_CHANNEL + 7) // 8  # = 25

CHANNEL_MID = 1024
CHANNEL_MIN = 364
CHANNEL_MAX = 1684


class DbusDecodeError(Exception):
    """通道帧解析失败。"""


def invert(data: bytes) -> bytes:
    """按位取反——软件方式修正电平反相。

    硬件上用 74HC04 反相器解决同一个问题。软件取反的好处是
    不用改板子；坏处是每个字节多一次运算，且如果硬件已经反过一次，
    软件再反就错了——这也是"接上没数据"最常见的两个原因之一。
    """
    return bytes(b ^ 0xFF for b in data)


@dataclass(frozen=True)
class DbusFrame:
    """一帧解析后的通道数据。"""

    channels: tuple[int, ...]  # 比例通道，如摇杆
    switches: tuple[int, ...]  # 开关通道，如拨杆

    @property
    def valid(self) -> bool:
        """比例通道是否都落在正常范围内。

        注意：``valid`` 只说明"数值形态正常"，不等于"遥控器在线"。
        失联时接收机常输出固定值（例如全部 1024），此时数值完全合法，
        所以真实系统还要配合**帧超时**判断失联——这一点在
        ``func.channels.range`` 与超时用例里都有体现。
        """
        return all(CHANNEL_MIN <= c <= CHANNEL_MAX for c in self.channels)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DbusFrame ch={self.channels[:4]}... sw={self.switches}>"


class _BitWriter:
    """按位写入——编码用。"""

    def __init__(self, n_bytes: int) -> None:
        self.buf = bytearray(n_bytes)
        self.pos = 0

    def write(self, value: int, n_bits: int) -> None:
        for i in range(n_bits):
            if (value >> i) & 1:
                self.buf[self.pos // 8] |= 1 << (self.pos % 8)
            self.pos += 1


class _BitReader:
    """按位读出——解码用。"""

    def __init__(self, buf: bytes) -> None:
        self.buf = buf
        self.pos = 0

    def read(self, n_bits: int) -> int:
        value = 0
        for i in range(n_bits):
            byte_i = (self.pos + i) // 8
            if byte_i >= len(self.buf):
                raise DbusDecodeError(
                    f"读取第 {self.pos} 位起的 {n_bits} 位时数据不足"
                    f"（共 {len(self.buf)} 字节）"
                )
            if (self.buf[byte_i] >> ((self.pos + i) % 8)) & 1:
                value |= 1 << i
        self.pos += n_bits
        return value


class DbusDecoder:
    """把打包字节流解析成通道值。"""

    def __init__(self, invert_level: bool = True) -> None:
        # 默认开启反相——接真实总线时通常就是这个情况
        self.invert_level = invert_level
        self.stats = {"decoded": 0, "errors": 0}

    def decode(self, data: bytes) -> DbusFrame:
        if len(data) < DBUS_FRAME_SIZE:
            self.stats["errors"] += 1
            raise DbusDecodeError(
                f"帧长应为 {DBUS_FRAME_SIZE} 字节，实际 {len(data)}"
            )

        buf = invert(data) if self.invert_level else data
        reader = _BitReader(buf)

        channels = tuple(reader.read(BITS_PER_CHANNEL) & CHANNEL_MASK
                         for _ in range(CHANNEL_COUNT))
        switches = tuple(reader.read(BITS_PER_CHANNEL) & CHANNEL_MASK
                         for _ in range(SWITCH_COUNT))

        self.stats["decoded"] += 1
        return DbusFrame(channels, switches)


def encode_dbus(
    channels: list[int],
    switches: list[int] | None = None,
    invert_level: bool = True,
) -> bytes:
    """把通道值打包回字节流——用于造测试数据（设备模拟器）。

    与 :meth:`DbusDecoder.decode` 严格互逆，这一点由
    ``proto.dbus.roundtrip`` 用例保证。
    """
    if len(channels) != CHANNEL_COUNT:
        raise DbusDecodeError(f"需要 {CHANNEL_COUNT} 个比例通道，给了 {len(channels)}")

    switches = switches if switches is not None else [CHANNEL_MID] * SWITCH_COUNT
    if len(switches) != SWITCH_COUNT:
        raise DbusDecodeError(f"需要 {SWITCH_COUNT} 个开关通道，给了 {len(switches)}")

    writer = _BitWriter(DBUS_FRAME_SIZE)
    for value in list(channels) + list(switches):
        writer.write(value & CHANNEL_MASK, BITS_PER_CHANNEL)

    return invert(bytes(writer.buf)) if invert_level else bytes(writer.buf)
