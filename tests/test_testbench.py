"""pytest 测试套件：对框架自身做单元测试。

和 ``suites/definitions.py`` 的区别：

* ``suites/`` 里的用例是**被测对象**——它们检查"那个设备对不对"，
  是这套工具要交付的产品；
* ``tests/`` 里的用例是**对框架本身的测试**——检查"这套工具自己
  有没有 bug"。CI 里跑的是这一层。

把两者分开是测试工程的基本纪律：工具的 bug 不能让"设备通过"
看起来像真的。

运行::

    pytest -q
    pytest --cov=embedded_testbench
"""

from __future__ import annotations

import pytest

from embedded_testbench.device.driver import DbusA5Driver, DeviceTimeout
from embedded_testbench.device.simulator import DbusA5Simulator
from embedded_testbench.protocol.crc import Crc16, crc8_maxim, crc16_modbus
from embedded_testbench.protocol.dbus import (
    CHANNEL_COUNT,
    CHANNEL_MASK,
    CHANNEL_MID,
    DBUS_FRAME_SIZE,
    DbusDecodeError,
    DbusDecoder,
    encode_dbus,
    invert,
)
from embedded_testbench.protocol.framing import (
    HEADER,
    BadLength,
    Frame,
    FrameDecoder,
    FrameEncoder,
)
from embedded_testbench.transport.loopback import FaultConfig, LoopbackTransport

# ── CRC ──────────────────────────────────────────────────────

class TestCrc:
    def test_crc16_standard_vector(self):
        """"123456789" 是校验 CRC 实现的标准测试向量。"""
        assert crc16_modbus(b"123456789") == 0x4B37

    def test_crc16_empty(self):
        """空输入返回初值。"""
        assert Crc16().update(b"").value == 0xFFFF

    def test_crc16_incremental_matches_oneshot(self):
        data = b"the quick brown fox"
        assert Crc16().update(data).value == (
            Crc16().update(data[:5]).update(data[5:]).value
        )

    def test_crc16_byte_order_is_little_endian(self):
        """Modbus 线上是低字节在前，写反了对端全判失败。"""
        d = Crc16().update(b"123456789").digest()
        assert d == bytes([0x37, 0x4B])

    def test_crc16_detects_single_bit_flip(self):
        """CRC 的意义就是检出传输错误。"""
        original = b"payload"
        base = crc16_modbus(original)
        for i in range(len(original)):
            for bit in range(8):
                mutated = bytearray(original)
                mutated[i] ^= 1 << bit
                assert crc16_modbus(bytes(mutated)) != base

    def test_crc8_vector(self):
        assert crc8_maxim(b"123456789") == 0xA1


# ── 帧编解码 ─────────────────────────────────────────────────

class TestFraming:
    def test_roundtrip(self):
        payload = bytes(range(64))
        frames = [f for f, _ in FrameDecoder().feed(FrameEncoder.encode(payload))]
        assert frames and frames[0].payload == payload

    def test_layout(self):
        raw = FrameEncoder.encode(b"abc")
        assert raw[:2] == HEADER
        assert raw[2] == 3
        # 头 2 + 长度 1 + 载荷 3 + CRC 2 = 8
        assert len(raw) == 8

    def test_rejects_oversized_payload(self):
        with pytest.raises(BadLength):
            Frame(b"x" * 300)

    def test_crc_corruption_detected(self):
        bad = bytearray(FrameEncoder.encode(b"data"))
        bad[5] ^= 0xFF
        results = list(FrameDecoder().feed(bytes(bad)))
        assert not [f for f, _ in results if f]
        assert [e for _, e in results if e]

    def test_fragmented_delivery(self):
        """一帧分成 1 字节一次喂，仍要还原——模拟逐字节中断。"""
        raw = FrameEncoder.encode(b"frag")
        dec = FrameDecoder()
        got = []
        for i in range(len(raw)):
            got += [f.payload for f, _ in dec.feed(raw[i:i + 1]) if f]
        assert got == [b"frag"]

    def test_partial_is_not_an_error(self):
        """半包不能报错，否则会把"还没收完"误判成"坏了"。"""
        raw = FrameEncoder.encode(b"hello")
        dec = FrameDecoder()
        assert list(dec.feed(raw[:4])) == []
        assert [f.payload for f, _ in dec.feed(raw[4:]) if f] == [b"hello"]

    def test_resync_after_noise(self):
        raw = b"\xde\xad\xbe\xef" + FrameEncoder.encode(b"clean")
        frames = [f.payload for f, _ in FrameDecoder().feed(raw) if f]
        assert frames == [b"clean"]

    def test_multiple_frames_back_to_back(self):
        payloads = [b"a", b"bb", b"ccc"]
        raw = FrameEncoder.encode_many(payloads)
        assert [f.payload for f, _ in FrameDecoder().feed(raw) if f] == payloads

    def test_truncated_stream_then_close_is_silent(self):
        """数据流断在半截：不应产出垃圾帧。"""
        raw = FrameEncoder.encode(b"never-finished")[:6]
        assert list(FrameDecoder().feed(raw)) == []


# ── 11 位通道协议 ────────────────────────────────────────────

class TestDbus:
    def test_frame_size_matches_bit_packing(self):
        """帧长必须由位宽推导出来，不能是魔数。"""
        expected = (18 * 11 + 7) // 8
        assert DBUS_FRAME_SIZE == expected == 25

    def test_roundtrip_all_channels(self):
        values = list(range(0, CHANNEL_COUNT))
        frame = DbusDecoder().decode(encode_dbus(values))
        assert list(frame.channels) == values

    def test_roundtrip_boundary_values(self):
        values = [0] * CHANNEL_COUNT
        values[0], values[-1] = 0, CHANNEL_MASK
        frame = DbusDecoder().decode(encode_dbus(values))
        assert frame.channels[0] == 0
        assert frame.channels[-1] == CHANNEL_MASK

    def test_mask_truncates_overflow(self):
        """超出 11 位的值必须被截断，不能串进相邻通道。"""
        raw = encode_dbus([0xFFFF] + [CHANNEL_MID] * (CHANNEL_COUNT - 1))
        frame = DbusDecoder().decode(raw)
        assert frame.channels[0] == CHANNEL_MASK
        assert all(c == CHANNEL_MID for c in frame.channels[1:]), "溢出污染了邻通道"

    def test_invert_is_involution(self):
        data = bytes(range(DBUS_FRAME_SIZE))
        assert invert(invert(data)) == data

    def test_invert_flag_roundtrip(self):
        plain = encode_dbus([CHANNEL_MID] * CHANNEL_COUNT, invert_level=False)
        inverted = encode_dbus([CHANNEL_MID] * CHANNEL_COUNT, invert_level=True)
        assert invert(inverted) == plain
        assert DbusDecoder(invert_level=False).decode(inverted).channels[0] != CHANNEL_MID

    def test_short_frame_rejected(self):
        with pytest.raises(DbusDecodeError):
            DbusDecoder().decode(b"\x00" * 5)

    def test_wrong_channel_count_rejected(self):
        with pytest.raises(DbusDecodeError):
            encode_dbus([1, 2, 3])

    def test_valid_range_flag(self):
        assert DbusDecoder().decode(encode_dbus([CHANNEL_MID] * CHANNEL_COUNT)).valid
        assert not DbusDecoder().decode(encode_dbus([0] * CHANNEL_COUNT)).valid


# ── 回环传输与故障注入 ───────────────────────────────────────

class TestLoopback:
    def test_bidirectional(self):
        t = LoopbackTransport()
        t.write(b"to-device")
        assert t.device_read(64) == b"to-device"
        t.device_write(b"to-host")
        assert t.read(64) == b"to-host"

    def test_mtu_limits_read_size(self):
        """真实串口一次中断不会给你一整帧，这里也要模拟。"""
        t = LoopbackTransport(mtu=8)
        t.device_write(b"x" * 40)
        assert len(t.read(100)) == 8

    def test_empty_when_nothing_available(self):
        assert LoopbackTransport().read(10) == b""

    def test_closed_transport_raises(self):
        t = LoopbackTransport()
        t.close()
        with pytest.raises(RuntimeError):
            t.write(b"x")

    def test_truncate_fault_halves_frames(self):
        t = LoopbackTransport(faults=FaultConfig(truncate_every=1))
        t.write(b"0123456789")
        assert t.device_read(64) == b"01234"

    def test_corrupt_fault_changes_a_bit(self):
        t = LoopbackTransport(faults=FaultConfig(corrupt_every=1))
        t.write(b"0123456789")
        out = t.device_read(64)
        assert out != b"0123456789"
        assert len(out) == 10

    def test_duplicate_fault_doubles(self):
        t = LoopbackTransport(faults=FaultConfig(duplicate_every=1))
        t.write(b"ab")
        assert t.device_read(64) == b"abab"


# ── 设备端到端 ───────────────────────────────────────────────

@pytest.fixture()
def device():
    """起一个模拟器 + 驱动，测试结束自动收线程。"""
    transport = LoopbackTransport()
    sim = DbusA5Simulator(transport).start()
    try:
        yield DbusA5Driver(transport)
    finally:
        sim.stop()


class TestDevice:
    def test_version(self, device):
        assert device.get_version() == (1, 3, 0)

    def test_read_channels(self, device):
        frame = device.read_channels()
        assert len(frame.channels) == CHANNEL_COUNT
        assert frame.valid

    def test_echo(self, device):
        assert device.echo(b"ping-pong") == b"ping-pong"

    def test_selftest(self, device):
        assert device.selftest() is True

    def test_selftest_failure_is_reported(self):
        """设备自检失败时要如实返回 False，不能吞掉。"""
        transport = LoopbackTransport()
        sim = DbusA5Simulator(transport, selftest_ok=False).start()
        try:
            assert DbusA5Driver(transport).selftest() is False
        finally:
            sim.stop()

    def test_unknown_command_returns_error(self, device):
        resp = device.send_unknown_command(0x7F)
        assert resp[0] == 0xE0
        assert resp[1] == 0x7F

    def test_timeout_when_device_absent(self):
        """对面没有设备时必须超时，不能永久阻塞。"""
        drv = DbusA5Driver(LoopbackTransport(), default_timeout_s=0.2)
        with pytest.raises(DeviceTimeout):
            drv.read_channels()

    def test_works_through_noisy_link(self):
        """线上有噪声时仍要能通信——验证重新同步是有效的。"""
        transport = LoopbackTransport()
        sim = DbusA5Simulator(transport, noise=True).start()
        try:
            assert DbusA5Driver(transport).get_version() == (1, 3, 0)
        finally:
            sim.stop()


# ── 框架自身的判定语义 ───────────────────────────────────────

class TestRunnerSemantics:
    def test_any_failure_means_not_ok(self):
        """任意一条用例失败，整体判定即为不通过。

        severity 只用于给人排优先级，不参与通过/失败的判定。
        """
        from embedded_testbench.core.case import (
            CheckContext,
            Severity,
            SuiteResult,
            TestCase,
        )

        def always_fail(ctx):
            ctx.expect(False, "故意失败")

        suite = SuiteResult(name="t")
        case = TestCase("t.fail", "必然失败", always_fail, Severity.MINOR)
        suite.results.append(case.run(CheckContext()))
        assert suite.failed == 1
        assert suite.ok is False, "minor 级失败也应让整体判定为不通过"

    def test_pass_when_all_green(self):
        from embedded_testbench.core.case import (
            CheckContext,
            Severity,
            SuiteResult,
            TestCase,
        )

        def always_pass(ctx):
            ctx.expect(True, "必然通过")

        suite = SuiteResult(name="t")
        suite.results.append(
            TestCase("t.pass", "通过", always_pass, Severity.NORMAL).run(CheckContext())
        )
        assert suite.ok is True

    def test_exception_is_error_not_failure(self):
        """框架炸了 vs 被测对象不符，要区分开。"""
        from embedded_testbench.core.case import (
            CheckContext,
            Outcome,
            Severity,
            TestCase,
        )

        def boom(ctx):
            raise ValueError("框架内部错误")

        r = TestCase("t.err", "错误", boom, Severity.NORMAL).run(CheckContext())
        assert r.outcome is Outcome.ERROR
        assert "ValueError" in r.message


# ── 导入冒烟测试 ─────────────────────────────────────────────

class TestImportsAndCli:
    """导入冒烟测试：每个公开模块都要能被导入。

    单元测试只覆盖被 import 到的代码。这里把全部公开模块 import 一遍，
    语法错误、循环导入和缺失依赖都会在这里直接失败。
    """

    def test_all_public_modules_import(self):
        import importlib

        modules = [
            "embedded_testbench",
            "embedded_testbench.cli",
            "embedded_testbench.core.case",
            "embedded_testbench.core.runner",
            "embedded_testbench.device.driver",
            "embedded_testbench.device.simulator",
            "embedded_testbench.protocol.crc",
            "embedded_testbench.protocol.dbus",
            "embedded_testbench.protocol.framing",
            "embedded_testbench.suites.definitions",
            "embedded_testbench.transport.loopback",
            "embedded_testbench.transport.serial_port",
        ]
        for name in modules:
            mod = importlib.import_module(name)
            assert mod is not None, f"{name} 导入失败"

    def test_cli_parser_builds(self):
        """CLI 的 argparse 能正常构造（入口语法正确）。"""
        from embedded_testbench.cli import main

        # --help 会走 SystemExit(0)，能证明入口可执行
        with pytest.raises(SystemExit) as e:
            main(["--help"])
        assert e.value.code == 0

    def test_cli_rejects_unknown_suite(self, tmp_path):
        """未知套件要返回退出码 2（而不是静默成功）。"""
        from embedded_testbench.cli import main

        code = main(["run", "--suite", "no-such-suite", "--out", str(tmp_path)])
        assert code == 2
