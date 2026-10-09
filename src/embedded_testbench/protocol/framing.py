"""串口帧编解码：把"字节流"变成"可校验、可定位问题的帧"。

为什么需要这一层
----------------
串口是**没有边界**的字节流。设备连续发数据时，接收端只看到一串字节，
既不知道帧从哪开始，也不知道有没有丢字节。工程上必须先定义一个
「帧结构」，才能做三件事：

1. **找同步**——靠帧头把流切开；
2. **验完整**——靠长度 + CRC 判断这一帧是不是好的；
3. **能恢复**——校验失败时丢弃并重新找帧头，而不是整条链路崩掉。

本模块实现一个通用的「帧头 + 长度 + 载荷 + CRC16」结构。它和
DBUS、Modbus 这类真实协议是同一套思路，只是字段更直白，便于测试与教学。

帧格式（小端）::

    +--------+--------+--------+------------------+--------+
    | 0xA5   | 0x5A   | LEN(1) | PAYLOAD (LEN)    | CRC(2) |
    +--------+--------+--------+------------------+--------+
     帧头      帧头      长度      载荷（LEN 字节）    校验

    CRC 覆盖范围：LEN + PAYLOAD（不含帧头）。
    这在工程上很常见——帧头只是同步用，不参与校验。

设计取舍
--------
* **不用固定长度**：真实协议载荷长度经常变化（DBUS 18 字节、
  Modbus 可变长度），固定长度会限制扩展。
* **帧头用双字节**：单字节帧头（如 0x7E）在噪声下容易误同步，
  双字节（0xA5 0x5A）能显著降低概率。
* **不做转义**：SLIP/HDLC 那类转义能避免载荷里出现帧头的问题，
  但会让长度翻倍。这里用「长度字段 + CRC」先保证正确性，
  转义作为后续扩展。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from .crc import Crc16

__all__ = [
    "BadLength", "CrcMismatch", "Frame", "FrameDecoder", "FrameEncoder",
    "FrameError", "HEADER", "MAX_PAYLOAD",
]

HEADER = b"\xa5\x5a"
MAX_PAYLOAD = 255  # 长度字段 1 字节，所以载荷最大 255


class FrameError(Exception):
    """帧解析失败的基类。"""


class CrcMismatch(FrameError):
    """CRC 校验不通过——帧在传输中被破坏了。"""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"CRC 校验失败: 期望 0x{expected:04X}, 实际 0x{actual:04X}")
        self.expected = expected
        self.actual = actual


class BadLength(FrameError):
    """长度字段超出允许范围。"""


@dataclass(frozen=True)
class Frame:
    """一个已解析的帧。

    只读（frozen）是刻意的：测试里经常要把帧存进集合或当字典键，
    而且能防止"解析完又被别处改掉"这类很难查的 bug。
    """

    payload: bytes

    def __post_init__(self) -> None:
        if len(self.payload) > MAX_PAYLOAD:
            raise BadLength(f"载荷 {len(self.payload)} 字节，超过上限 {MAX_PAYLOAD}")

    @property
    def length(self) -> int:
        return len(self.payload)

    def encode(self) -> bytes:
        """序列化为线上字节。"""
        body = bytes((self.length,)) + self.payload
        crc = Crc16().update(body).digest()
        return HEADER + body + crc

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        hexed = self.payload[:8].hex(" ")
        more = " ..." if len(self.payload) > 8 else ""
        return f"<Frame len={self.length} payload={hexed}{more}>"


class FrameEncoder:
    """把载荷编成帧。

    保留成类是为了以后能加状态（比如自增序号、转义开关），
    目前是无状态的，纯函数式使用即可。
    """

    @staticmethod
    def encode(payload: bytes) -> bytes:
        return Frame(payload).encode()

    @staticmethod
    def encode_many(payloads: list[bytes]) -> bytes:
        """把多个载荷拼成一整段字节流，模拟设备连续发送。"""
        return b"".join(FrameEncoder.encode(p) for p in payloads)


class FrameDecoder:
    """增量式帧解码器——边收边解，适合中断/DMA 回调场景。

    用法::

        dec = FrameDecoder()
        for chunk in serial_chunks:
            for frame, error in dec.feed(chunk):
                ...

    关键行为：

    * 跨 chunk 粘包：一帧被切成几段喂进来，仍要正确还原。
    * 半包：帧只到一半，不报错，等后续数据。
    * 坏 CRC：报告错误并重新同步，不能卡死。
    * 垃圾前缀：帧头前有噪声，跳过而不是丢弃整段。
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.stats = {"frames_ok": 0, "crc_errors": 0, "bad_length": 0, "resyncs": 0}

    def reset(self) -> None:
        self._buf.clear()

    def feed(self, data: bytes) -> Iterator[tuple[Frame | None, FrameError | None]]:
        """喂入一段字节，逐个产出 (帧, 错误) 二元组。

        正常帧返回 ``(frame, None)``；解析出错返回 ``(None, error)``
        且解码器自动重新同步。调用方可以据此决定是记录日志还是重试。
        """
        self._buf.extend(data)

        while True:
            # 丢掉帧头之前的字节
            idx = self._buf.find(HEADER)
            if idx < 0:
                # 保留最后 1 字节：它可能是被截断的帧头首字节
                if len(self._buf) > 1:
                    dropped = len(self._buf) - 1
                    del self._buf[:dropped]
                    self.stats["resyncs"] += 1
                return
            if idx > 0:
                del self._buf[:idx]
                self.stats["resyncs"] += 1

            # 长度字段还没到齐
            if len(self._buf) < 3:
                return
            length = self._buf[2]
            if length > MAX_PAYLOAD:
                # 长度非法，丢掉帧头后重新找
                del self._buf[:2]
                self.stats["bad_length"] += 1
                yield None, BadLength(f"长度字段 {length} 超过上限 {MAX_PAYLOAD}")
                continue

            total = 2 + 1 + length + 2
            # 整帧还没收完，等下一批数据
            if len(self._buf) < total:
                return

            raw = bytes(self._buf[:total])
            payload = raw[3 : 3 + length]
            got_crc = raw[3 + length] | (raw[3 + length + 1] << 8)
            want_crc = Crc16().update(raw[2 : 3 + length]).value

            del self._buf[:total]

            if got_crc != want_crc:
                self.stats["crc_errors"] += 1
                yield None, CrcMismatch(want_crc, got_crc)
                continue

            self.stats["frames_ok"] += 1
            yield Frame(payload), None
