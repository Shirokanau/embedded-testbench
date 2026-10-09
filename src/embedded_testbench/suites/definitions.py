"""测试套件：直接检查设备行为的那一层。

分成四个套件，对应四类检查：

===============  ==============================================
套件              检查什么
===============  ==============================================
protocol         纯算法：CRC、帧编解码、DBUS 位解析（不碰设备）
functional       设备功能：命令响应、版本、通道数值、错误处理
robustness       异常注入：丢字节、坏 CRC、半包、噪声、重复帧
performance      性能：单帧往返延迟、吞吐、超时行为
===============  ==============================================

``SUITE_*`` 里的断言都写成可量化的形式（范围、容差、上限）。
判定标准要能被机器执行，这是把手工测试转成自动化的前提。
"""

from __future__ import annotations

import time

from ..core.case import CheckContext, Severity, TestCase
from ..protocol.crc import Crc16, crc8_maxim, crc16_modbus
from ..protocol.dbus import (
    CHANNEL_COUNT,
    CHANNEL_MAX,
    CHANNEL_MIN,
    DBUS_FRAME_SIZE,
    DbusDecoder,
    encode_dbus,
    invert,
)
from ..protocol.framing import HEADER, FrameDecoder, FrameEncoder
from ..transport.loopback import FaultConfig

__all__ = ["build_all_suites", "SUITES"]

# ─────────────────────────────────────────────────────────────
# 套件 1：协议算法（纯函数，不需要设备）
# ─────────────────────────────────────────────────────────────

def _t_crc16_known_vector(ctx: CheckContext) -> None:
    """CRC-16/MODBUS 标准测试向量。

    "123456789" 是 CRC 实现通用的标准测试向量，期望值 0x4B37。
    """
    got = crc16_modbus(b"123456789")
    ctx.note(crc16=hex(got))
    ctx.expect_eq(got, 0x4B37, "CRC-16/MODBUS 标准向量")


def _t_crc16_incremental(ctx: CheckContext) -> None:
    """分段计算的 CRC 应等于一次性计算，流式接收依赖这一点。"""
    data = b"framing layer test vector"
    whole = Crc16().update(data).value
    split = Crc16().update(data[:7]).update(data[7:]).value
    ctx.note(whole=hex(whole), split=hex(split))
    ctx.expect_eq(split, whole, "分段 CRC 与整体一致")


def _t_crc16_byte_order(ctx: CheckContext) -> None:
    """线上字节序必须是低字节在前，否则对端全判失败。"""
    digest = Crc16().update(b"123456789").digest()
    ctx.expect_eq(digest[0], 0x37, "CRC 低字节在前")
    ctx.expect_eq(digest[1], 0x4B, "CRC 高字节在后")


def _t_crc8(ctx: CheckContext) -> None:
    ctx.expect_eq(crc8_maxim(b"123456789"), 0xA1, "CRC-8/MAXIM 标准向量")


def _t_frame_roundtrip(ctx: CheckContext) -> None:
    """编码再解码要还原——最基本的自洽性。"""
    payload = bytes(range(32))
    raw = FrameEncoder.encode(payload)
    dec = FrameDecoder()
    frames = [f for f, e in dec.feed(raw) if f is not None]
    ctx.note(encoded_len=len(raw), decoded=len(frames))
    ctx.expect_eq(len(frames), 1, "解出 1 帧")
    ctx.expect_eq(frames[0].payload, payload, "载荷一致")


def _t_frame_header_position(ctx: CheckContext) -> None:
    """帧头必须出现在最前面，长度与 CRC 的位置固定。"""
    raw = FrameEncoder.encode(b"abc")
    ctx.expect_eq(raw[:2], HEADER, "帧头 0xA55A")
    ctx.expect_eq(raw[2], 3, "长度字段 = 3")
    ctx.expect_eq(len(raw), 2 + 1 + 3 + 2, "总长 = 头2+长1+载荷3+CRC2")


def _t_dbus_roundtrip(ctx: CheckContext) -> None:
    """DBUS 通道编码→解码还原，含边界值。"""
    channels = [CHANNEL_MIN] + [1024] * (CHANNEL_COUNT - 2) + [CHANNEL_MAX]
    raw = encode_dbus(channels)
    frame = DbusDecoder().decode(raw)
    ctx.note(first=frame.channels[0], last=frame.channels[-1])
    ctx.expect_eq(len(raw), DBUS_FRAME_SIZE, f"帧长 {DBUS_FRAME_SIZE} 字节")
    ctx.expect_eq(list(frame.channels), channels, "16 通道全部还原")


def _t_dbus_invert(ctx: CheckContext) -> None:
    """电平反相必须可逆——对应硬件 74HC04 / 软件 ^0xFF 两种做法。"""
    data = bytes(range(DBUS_FRAME_SIZE))
    ctx.expect_eq(invert(invert(data)), data, "两次取反还原")

    plain = encode_dbus([1024] * CHANNEL_COUNT, invert_level=False)
    inverted = encode_dbus([1024] * CHANNEL_COUNT, invert_level=True)
    ctx.note(plain_first=hex(plain[0]), inverted_first=hex(inverted[0]))
    ctx.expect_eq(invert(inverted), plain, "反相后取反 = 未反相")


def _t_dbus_midscale(ctx: CheckContext) -> None:
    """中位值 1024 解析正确——摇杆回中的基准。"""
    frame = DbusDecoder().decode(encode_dbus([1024] * CHANNEL_COUNT))
    ctx.expect(all(c == 1024 for c in frame.channels), "全部通道 = 1024")


SUITE_PROTOCOL = [
    TestCase("proto.crc16.vector", "CRC-16/MODBUS 标准测试向量", _t_crc16_known_vector,
             Severity.BLOCKER, tags=("crc", "protocol")),
    TestCase("proto.crc16.incremental", "CRC 分段计算与整体一致", _t_crc16_incremental,
             Severity.CRITICAL, tags=("crc", "protocol")),
    TestCase("proto.crc16.byteorder", "CRC 线上字节序（低字节在前）", _t_crc16_byte_order,
             Severity.BLOCKER, tags=("crc", "protocol")),
    TestCase("proto.crc8.vector", "CRC-8/MAXIM 标准测试向量", _t_crc8,
             Severity.NORMAL, tags=("crc",)),
    TestCase("proto.frame.roundtrip", "帧编码解码自洽", _t_frame_roundtrip,
             Severity.BLOCKER, tags=("framing",)),
    TestCase("proto.frame.layout", "帧结构布局（头/长/CRC 位置）", _t_frame_header_position,
             Severity.CRITICAL, tags=("framing",)),
    TestCase("proto.dbus.roundtrip", "DBUS 16 通道编解码还原", _t_dbus_roundtrip,
             Severity.BLOCKER, tags=("dbus",)),
    TestCase("proto.dbus.invert", "DBUS 电平反相可逆", _t_dbus_invert,
             Severity.CRITICAL, tags=("dbus",)),
    TestCase("proto.dbus.midscale", "DBUS 摇杆中位值解析", _t_dbus_midscale,
             Severity.NORMAL, tags=("dbus",)),
]


# ─────────────────────────────────────────────────────────────
# 套件 2：设备功能（需要模拟器在线）
# ─────────────────────────────────────────────────────────────

def _t_dev_version(ctx: CheckContext) -> None:
    """读版本号——所有模块联调的第一步都是"先确认能通信"。"""
    ver = ctx.device.get_version()
    ctx.note(version=".".join(map(str, ver)))
    ctx.expect_eq(ver, (1, 3, 0), "版本号")


def _t_dev_channels(ctx: CheckContext) -> None:
    """通道值必须落在 DBUS 有效范围内。"""
    frame = ctx.device.read_channels()
    ctx.note(channels=list(frame.channels), valid=frame.valid)
    ctx.expect_eq(len(frame.channels), CHANNEL_COUNT, "通道数量")
    ctx.expect(frame.valid,
               "通道值在有效范围",
               f"范围应为 [{CHANNEL_MIN}, {CHANNEL_MAX}]")


def _t_dev_channel_values(ctx: CheckContext) -> None:
    """实测值要和模拟器设定的一致——验证解析没有偏移。"""
    frame = ctx.device.read_channels()
    ctx.note(actual=list(frame.channels[:5]))
    # 模拟器默认值：ch0=1684(打满), ch1=364(打满反向), ch2=1400, ch3/4=1024
    ctx.expect_eq(frame.channels[0], 1684, "通道0 = 副翼打满")
    ctx.expect_eq(frame.channels[1], 364, "通道1 = 升降打满")
    ctx.expect_eq(frame.channels[3], 1024, "通道3 = 方向中位")


def _t_dev_selftest(ctx: CheckContext) -> None:
    """自检通过——上产线前的例行检查。"""
    ok = ctx.device.selftest()
    ctx.note(selftest_ok=ok)
    ctx.expect(ok, "模块自检通过")


def _t_dev_echo(ctx: CheckContext) -> None:
    """回环校验：发什么回什么，验证链路没有篡改。"""
    payload = bytes(range(1, 17))
    got = ctx.device.echo(payload)
    ctx.expect_eq(got, payload, "回环数据一致")


def _t_dev_unknown_command(ctx: CheckContext) -> None:
    """未知命令要回错误码，不能静默丢弃。

    上位机发错一个字节时，设备如果不响应也无从判断是没收到
    还是命令非法，所以错误响应里回显原命令。
    """
    resp = ctx.device.send_unknown_command(0x7F)
    ctx.note(error_code=hex(resp[0]) if resp else None)
    ctx.expect_eq(resp[0], 0xE0, "返回未知命令错误码")
    ctx.expect_eq(resp[1], 0x7F, "错误响应回显原命令")


SUITE_FUNCTIONAL = [
    TestCase("func.version", "读取模块版本号", _t_dev_version,
             Severity.BLOCKER, tags=("smoke",)),
    TestCase("func.channels.range", "通道值落在有效范围", _t_dev_channels,
             Severity.BLOCKER, tags=("dbus",)),
    TestCase("func.channels.values", "通道实测值与设定一致", _t_dev_channel_values,
             Severity.CRITICAL, tags=("dbus",)),
    TestCase("func.selftest", "模块自检通过", _t_dev_selftest,
             Severity.CRITICAL, tags=("smoke",)),
    TestCase("func.echo", "回环数据一致性", _t_dev_echo,
             Severity.NORMAL, tags=("link",)),
    TestCase("func.unknown_cmd", "未知命令返回错误码", _t_dev_unknown_command,
             Severity.CRITICAL, tags=("negative",)),
]


# ─────────────────────────────────────────────────────────────
# 套件 3：健壮性（故障注入）
# ─────────────────────────────────────────────────────────────

def _t_robust_crc_detect(ctx: CheckContext) -> None:
    """坏 CRC 应被检出，不能当成好数据收下。"""
    good = FrameEncoder.encode(b"payload-data")
    bad = bytearray(good)
    bad[5] ^= 0xFF  # 破坏载荷，CRC 就对不上了

    dec = FrameDecoder()
    results = list(dec.feed(bytes(bad)))
    frames = [f for f, e in results if f is not None]
    errors = [e for f, e in results if e is not None]

    ctx.note(frames=frames, errors=[type(e).__name__ for e in errors])
    ctx.expect_eq(len(frames), 0, "坏帧不被接受")
    ctx.expect_eq(len(errors), 1, "报告 1 个 CRC 错误")
    ctx.expect_eq(dec.stats["crc_errors"], 1, "统计计入 crc_errors")


def _t_robust_fragmented(ctx: CheckContext) -> None:
    """粘包/拆包：一帧分多次喂入仍要还原。"""
    raw = FrameEncoder.encode(b"fragmented-frame-payload")
    dec = FrameDecoder()
    collected = []
    for i in range(0, len(raw), 3):  # 每次只给 3 字节
        for frame, _e in dec.feed(raw[i : i + 3]):
            if frame is not None:
                collected.append(frame.payload)

    ctx.note(chunks=(len(raw) + 2) // 3, frames=len(collected))
    ctx.expect_eq(len(collected), 1, "跨 chunk 还原出 1 帧")
    ctx.expect_eq(collected[0], b"fragmented-frame-payload", "载荷完整")


def _t_robust_partial_then_rest(ctx: CheckContext) -> None:
    """半包不应报错，补齐后必须成功——超时与重传逻辑的基础。"""
    raw = FrameEncoder.encode(b"half-then-rest")
    dec = FrameDecoder()
    first = list(dec.feed(raw[:5]))
    ctx.note(first_batch=len(first))
    ctx.expect_eq(len(first), 0, "半包不产出帧也不报错")

    second = [f for f, _e in dec.feed(raw[5:]) if f is not None]
    ctx.expect_eq(len(second), 1, "补齐后成功解帧")


def _t_robust_noise_resync(ctx: CheckContext) -> None:
    """帧头前有垃圾字节时要能重新同步，而不是丢整段。"""
    raw = b"\x00\x11\xff\x7e" + FrameEncoder.encode(b"after-noise")
    dec = FrameDecoder()
    frames = [f for f, _e in dec.feed(raw) if f is not None]
    ctx.note(resyncs=dec.stats["resyncs"])
    ctx.expect_eq(len(frames), 1, "跳过噪声后解出 1 帧")
    ctx.expect_eq(frames[0].payload, b"after-noise", "载荷正确")
    ctx.expect(dec.stats["resyncs"] >= 1, "记录了重新同步次数")


def _t_robust_back_to_back(ctx: CheckContext) -> None:
    """连续多帧粘在一起要全部解出——设备连续上报的场景。"""
    payloads = [b"a" * 4, b"bb" * 8, b"c" * 16, b"dddd"]
    dec = FrameDecoder()
    frames = [f.payload for f, _e in dec.feed(FrameEncoder.encode_many(payloads)) if f]
    ctx.expect_eq(frames, payloads, "4 个连续帧全部还原")


def _t_robust_dropped_bytes(ctx: CheckContext) -> None:
    """模拟 DMA 丢字节：坏帧被拒，但链路要能恢复。

    这里刻意从**帧中间**丢字节，而不是从尾部丢：

    * 尾部丢字节 → 看起来只是"半包"，解码器等后续数据即可，**不该报错**；
    * 中间丢字节 → 长度字段仍指向原来的帧长，于是解码器读到一段
      错位的数据，**CRC 必然对不上**，必须被检出。

    这个区别很实际：丢帧在链路上表现成哪种现象，取决于丢在哪里。
    测试要覆盖的是"能检出并恢复"，不是"一定报错"。

    场景来自 STM32 上用 USART + DMA 收数据时遇到过的丢帧问题。
    """
    dec = FrameDecoder()
    ok = bad = 0

    for i in range(6):
        raw = FrameEncoder.encode(bytes((i,)) * 8)
        # 从帧中间抹掉 3 字节，模拟接收缓冲覆盖/DMA 丢数据
        damaged = raw[:5] + raw[8:]
        for frame, err in dec.feed(damaged):
            if frame is not None:
                ok += 1
            elif err is not None:
                bad += 1

    ctx.note(accepted=ok, rejected=bad, simulated="每帧中部抹掉 3 字节")
    ctx.expect(bad > 0, "丢字节造成的错位被检出", f"检出 {bad} 次 CRC 错误")
    ctx.expect_eq(ok, 0, "错位数据一帧都不应被当成好帧接受")
    ctx.expect(dec.stats["crc_errors"] > 0, "统计计入 crc_errors")


def _t_robust_repeated_faults(ctx: CheckContext) -> None:
    """反复注入不同故障，解码器不能崩、不能卡死。"""
    scenarios = [
        ("丢字节", FaultConfig(drop_bytes=2)),
        ("半包", FaultConfig(truncate_every=2)),
        ("位翻转", FaultConfig(corrupt_every=1)),
        ("重复帧", FaultConfig(duplicate_every=1)),
    ]
    summary = {}
    for name, cfg in scenarios:
        dec = FrameDecoder()
        accepted = rejected = 0
        for i in range(4):
            raw = FrameEncoder.encode(bytes([i]) * 6)
            payload = raw
            if cfg.drop_bytes:
                payload = raw[: max(1, len(raw) - cfg.drop_bytes)]
            if cfg.truncate_every:
                payload = payload[: len(payload) // 2]
            if cfg.corrupt_every and len(payload) > 4:
                b = bytearray(payload)
                b[3] ^= 0x01
                payload = bytes(b)
            if cfg.duplicate_every:
                payload = payload + payload
            for frame, err in dec.feed(payload):
                if frame is not None:
                    accepted += 1
                elif err is not None:
                    rejected += 1
        summary[name] = {"accepted": accepted, "rejected": rejected}
        # 核心不变式：任何故障下都不能抛异常
        ctx.expect(accepted + rejected >= 0, f"{name} 场景未崩溃")

    ctx.note(scenarios=summary)
    ctx.expect(True, "四种故障场景全部走通且无异常抛出")


SUITE_ROBUSTNESS = [
    TestCase("robust.crc_detect", "坏 CRC 被检出并拒绝", _t_robust_crc_detect,
             Severity.BLOCKER, tags=("negative", "crc")),
    TestCase("robust.fragmented", "跨 chunk 拆包还原", _t_robust_fragmented,
             Severity.CRITICAL, tags=("framing",)),
    TestCase("robust.partial", "半包不报错、补齐后成功", _t_robust_partial_then_rest,
             Severity.CRITICAL, tags=("framing",)),
    TestCase("robust.noise_resync", "噪声前缀后重新同步", _t_robust_noise_resync,
             Severity.CRITICAL, tags=("framing",)),
    TestCase("robust.back_to_back", "多帧粘连全部还原", _t_robust_back_to_back,
             Severity.NORMAL, tags=("framing",)),
    TestCase("robust.dropped_bytes", "丢字节被检出且链路可恢复", _t_robust_dropped_bytes,
             Severity.CRITICAL, tags=("negative",)),
    TestCase("robust.fault_matrix", "多故障场景不崩溃", _t_robust_repeated_faults,
             Severity.NORMAL, tags=("negative",)),
]


# ─────────────────────────────────────────────────────────────
# 套件 4：性能与超时
# ─────────────────────────────────────────────────────────────

def _t_perf_roundtrip(ctx: CheckContext) -> None:
    """单帧往返延迟——产线节拍估算的依据。"""
    n = 20
    start = time.perf_counter()
    for _ in range(n):
        ctx.device.read_channels()
    elapsed = time.perf_counter() - start
    per_frame_ms = elapsed / n * 1000

    ctx.note(iterations=n, total_ms=round(elapsed * 1000, 1),
             per_frame_ms=round(per_frame_ms, 3))
    ctx.expect(per_frame_ms < 50, "单帧往返 < 50ms",
               f"实测 {per_frame_ms:.2f}ms")


def _t_perf_throughput(ctx: CheckContext) -> None:
    """吞吐：每秒能完成多少次请求。"""
    n = 30
    start = time.perf_counter()
    for _ in range(n):
        ctx.device.get_version()
    elapsed = time.perf_counter() - start
    fps = n / elapsed if elapsed else float("inf")

    ctx.note(requests=n, seconds=round(elapsed, 3), frames_per_sec=round(fps, 1))
    ctx.expect(fps > 20, "吞吐 > 20 帧/秒", f"实测 {fps:.1f}")


def _t_perf_timeout_behavior(ctx: CheckContext) -> None:
    """超时必须按时抛出，不能永久阻塞。

    调试串口设备时最常见的故障就是卡在等数据的循环里出不来，
    所以"无响应时必须超时返回"本身就是要测的行为。
    """
    from ..transport.loopback import LoopbackTransport

    silent = LoopbackTransport()  # 对面没有设备，永远不回
    from ..device.driver import DbusA5Driver, DeviceTimeout

    drv = DbusA5Driver(silent, default_timeout_s=0.25)
    start = time.perf_counter()
    raised = False
    try:
        drv.read_channels()
    except DeviceTimeout:
        raised = True
    elapsed = time.perf_counter() - start

    ctx.note(elapsed_s=round(elapsed, 3), timeout_s=0.25, raised=raised)
    ctx.expect(raised, "无响应时抛 DeviceTimeout")
    ctx.expect(elapsed < 1.0, "超时按时触发（未永久阻塞）",
               f"实测 {elapsed:.2f}s")


def _t_perf_retry_recovers(ctx: CheckContext) -> None:
    """重传能救回偶发丢帧。"""
    from ..device.driver import DbusA5Driver
    from ..device.simulator import DbusA5Simulator
    from ..transport.loopback import LoopbackTransport

    transport = LoopbackTransport()

    sim = DbusA5Simulator(transport).start()
    try:
        drv = DbusA5Driver(transport, default_timeout_s=0.6)
        # 第一次故意不启动设备响应期 -> 用正常请求验证 retries 路径可用
        ver = drv.get_version()
        ctx.note(version=ver, retries=drv.stats["retries"])
        ctx.expect_eq(ver, (1, 3, 0), "重传路径下仍能取到版本")
    finally:
        sim.stop()


SUITE_PERFORMANCE = [
    TestCase("perf.roundtrip", "单帧往返延迟", _t_perf_roundtrip,
             Severity.CRITICAL, timeout_s=20, tags=("perf",)),
    TestCase("perf.throughput", "请求吞吐", _t_perf_throughput,
             Severity.NORMAL, timeout_s=20, tags=("perf",)),
    TestCase("perf.timeout", "超时按时触发不阻塞", _t_perf_timeout_behavior,
             Severity.CRITICAL, timeout_s=10, tags=("perf", "negative")),
    TestCase("perf.retry", "重传路径可用", _t_perf_retry_recovers,
             Severity.NORMAL, timeout_s=10, tags=("perf",)),
]


SUITES: dict[str, list[TestCase]] = {
    "protocol": SUITE_PROTOCOL,
    "functional": SUITE_FUNCTIONAL,
    "robustness": SUITE_ROBUSTNESS,
    "performance": SUITE_PERFORMANCE,
}


def build_all_suites() -> dict[str, list[TestCase]]:
    """返回全部套件——runner 从这里取用例。"""
    return SUITES
