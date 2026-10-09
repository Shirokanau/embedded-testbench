"""加载模拟器并跑完整测试，输出三份报告。

CLI 用法::

    python -m embedded_testbench.cli run                  # 全部套件
    python -m embedded_testbench.cli run --suite protocol # 只跑协议套件
    python -m embedded_testbench.cli run --selftest-fail  # 让模拟设备自检失败

退出码约定（和 CI 对接）：

* ``0`` —— 全部用例通过
* ``1`` —— 有用例失败/出错

``--selftest-fail`` 让模拟设备返回自检失败，用于验证失败路径：
报告应标为 FAIL 且退出码为 1。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .core.runner import Runner, write_json, write_junit_xml, write_markdown
from .device.driver import DbusA5Driver
from .device.simulator import DbusA5Simulator
from .suites.definitions import build_all_suites
from .transport.loopback import FaultConfig, LoopbackTransport

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="embedded_testbench",
        description="嵌入式串口设备自动化测试框架",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="执行测试并生成报告")
    run_p.add_argument("--suite", action="append", default=None,
                       help="只跑指定套件（可重复），默认全部")
    run_p.add_argument("--out", default="reports", help="报告输出目录")
    run_p.add_argument("--selftest-fail", action="store_true",
                       help="让模拟设备自检失败")
    run_p.add_argument("--response-delay", type=float, default=0.0,
                       help="模拟设备响应延迟（秒）")
    run_p.add_argument("--noise", action="store_true",
                       help="模拟设备在帧头前输出噪声")
    run_p.add_argument("--quiet", action="store_true", help="不打印逐条结果")

    args = parser.parse_args(argv)

    if args.command != "run":  # pragma: no cover
        parser.error("未知命令")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    transport = LoopbackTransport(faults=FaultConfig())
    simulator = DbusA5Simulator(
        transport,
        selftest_ok=not args.selftest_fail,
        response_delay_s=args.response_delay,
        noise=args.noise,
    ).start()
    driver = DbusA5Driver(transport)

    suites_map = build_all_suites()
    selected = args.suite or list(suites_map)
    unknown = [s for s in selected if s not in suites_map]
    if unknown:
        print(f"未知套件：{', '.join(unknown)}")
        print(f"可用套件：{', '.join(suites_map)}")
        simulator.stop()
        return 2

    runner = Runner(device=driver, verbose=not args.quiet)

    print("=" * 68)
    print("  嵌入式串口设备自动化测试  embedded-testbench")
    print(f"  套件：{', '.join(selected)}")
    print("  设备：DBUS-A5 模拟模块（回环串口）")
    print("=" * 68)

    results = []
    try:
        for name in selected:
            print(f"\n[套件] {name}")
            results.append(runner.run_suite(name, suites_map[name]))
    finally:
        simulator.stop()

    meta = {
        "被测设备": "DBUS-A5 遥控接收模块（软件模拟器）",
        "传输层": "LoopbackTransport（内存回环，非物理串口）",
        "自检模式": "失败" if args.selftest_fail else "正常",
        "设备响应延迟": f"{args.response_delay * 1000:.0f} ms",
        "噪声注入": "开启" if args.noise else "关闭",
        "设备侧统计": f"收帧 {simulator.stats['frames_received']}，"
                      f"发帧 {simulator.stats['commands']}",
    }

    md_path = out_dir / "report.md"
    xml_path = out_dir / "results.xml"
    json_path = out_dir / "results.json"
    write_markdown(results, md_path, meta)
    write_junit_xml(results, xml_path)
    write_json(results, json_path, meta)

    total = sum(s.total for s in results)
    passed = sum(s.passed for s in results)
    failed = sum(s.failed for s in results)
    errored = sum(s.errored for s in results)
    overall = all(s.ok for s in results)

    print("\n" + "=" * 68)
    print(f"  用例 {total}　通过 {passed}　失败 {failed}　错误 {errored}")
    print(f"  结论：{'PASS' if overall else 'FAIL'}")
    print(f"  报告：{md_path}  |  {xml_path}  |  {json_path}")
    print("=" * 68)

    return 0 if overall else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
