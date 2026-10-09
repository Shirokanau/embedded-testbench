# embedded-testbench

嵌入式串口设备的自动化测试框架。

把调设备时"接上线、打开串口助手、肉眼看数据对不对"那套流程，改成可重复执行、
有判定标准、能进 CI 的测试。

## 背景

调串口设备（单片机、传感器模块、遥控接收机）时会反复遇到几个问题：

- 数据看着不对，但说不上哪不对。没有帧结构和校验，只能靠眼睛判断。
- 偶发丢帧，上位机和固件单独测都正常。这种情况通常是两端对同一帧算出了不同结果。
- 程序卡在等数据的地方出不来。这是缺少超时导致的。
- 改了一处影响别处，回归只能靠手工再点一遍。

这个项目把这几件事分别做成了协议层、传输层、设备层和用例层。

## 快速开始

```bash
pip install -r requirements.txt

# 跑测试台，在 reports/ 下生成三份报告
PYTHONPATH=src python -m embedded_testbench run --out reports

# 跑框架自身的测试（含 C/Python 一致性比对）
pytest
```

没有硬件也能完整跑通：默认使用内存回环串口和一个软件设备模拟器。
换到真实设备时把 `LoopbackTransport` 换成 `SerialTransport`，测试用例不用改。

## 测试结果

设备用例 26 条、框架自身测试 121 条，全部通过。

| 套件 | 用例数 | 检查内容 |
|---|---|---|
| `protocol` | 9 | CRC-16/MODBUS、CRC-8/MAXIM、帧编解码、11 位通道打包 |
| `functional` | 6 | 版本读取、通道数值范围、自检、回环、未知命令错误处理 |
| `robustness` | 7 | 坏 CRC、跨 chunk 拆包、半包、噪声重新同步、丢字节、多故障矩阵 |
| `performance` | 4 | 单帧往返延迟、吞吐、超时行为、重传恢复 |

性能（本机实测，多次运行有波动，`python scripts/show_metrics.py` 可复核）：
单帧往返约 2–3 ms，吞吐约 400–550 帧/秒，无响应时 0.25 s 内抛超时。

这几个数与机器和当次运行有关，不是固定值，每次运行的实际值记录在
`reports/results.json` 里。

### 报告

`reports/` 下同时输出三种格式：

| 文件 | 用途 |
|---|---|
| `report.md` | 总览、套件明细、失败详情、每条用例的实测证据 |
| `results.xml` | JUnit XML，GitHub Actions / Jenkins / GitLab 可直接解析 |
| `results.json` | 机器可读，用于统计和二次处理 |

### 失效开关

测试框架如果无论输入什么都报通过就没有意义，所以留了一个强制失效的开关：

```bash
python -m embedded_testbench run --selftest-fail   # 退出码 1，报告标 FAIL
```

CI 里有一步专门跑它并断言它必须失败。

## 结构

```
src/embedded_testbench/
├── protocol/     协议层
│   ├── crc.py        CRC-16/MODBUS、CRC-8/MAXIM（查表 + 逐位两种实现）
│   ├── framing.py    帧结构：帧头 + 长度 + 载荷 + CRC，含重新同步
│   └── dbus.py       11 位跨字节打包、电平反相、掩码截断
├── transport/    传输层（可替换）
│   ├── loopback.py   内存回环 + 故障注入（丢字节/坏 CRC/半包/重复/延迟）
│   └── serial_port.py 真实串口（pyserial，DBUS 参数已固化）
├── device/       设备层
│   ├── simulator.py  软件设备：命令→响应，独立线程
│   └── driver.py     上位机驱动：请求/响应、超时、重传
├── core/         框架层
│   ├── case.py       用例、断言、证据收集、severity
│   └── runner.py     执行器 + 三格式报告
└── suites/       用例集：protocol / functional / robustness / performance

firmware/
└── crc.c         C 实现（嵌入式侧），与 Python 逐字节比对

tests/            框架自身的测试（121 条）
└── test_c_conformance.py   跨语言一致性：C / Python 逐字节比对
```

## 帧结构

小端：

```
+------+------+-------+----------------+-------+
| 0xA5 | 0x5A | LEN   | PAYLOAD (LEN)  | CRC16 |
+------+------+-------+----------------+-------+
  帧头    帧头   长度      载荷            校验
CRC 覆盖 LEN + PAYLOAD，不含帧头
```

几个取舍：

- 帧头用两个字节。单字节帧头（如 0x7E）在噪声下容易误同步，数据里偶然出现
  一个就会被当成帧开始。
- 长度字段参与 CRC。否则长度被干扰改掉后，接收端按错误的长度截取，
  CRC 反而检不出问题。
- 半包不报错。串口是流式的，一帧可能分几次到达；数据没齐就报错会把正常的分片
  传输误判成故障。只有 CRC 对不上才算错误。

## 跨语言一致性测试

上位机是 Python，设备固件是 C。两端对同一帧算出不同校验值时，现象是
偶发丢帧、两边单独测都正常，是联调时比较难定位的一类问题。

所以这里不满足于"C 能编译"，而是把两个实现对同一批输入逐字节比对：

```
随机输入 → C 算一遍 → Python 算一遍 → 必须完全相同
```

输入覆盖空串、单字节、全 0、全 0xFF 和 16 种长度的随机数据（固定种子，
结果可复现），比对 CRC-16 值、CRC 线上字节序、CRC-8、11 位打包结果、
整帧编码共 5 类。

编译器可替换：有 gcc/clang 就直接用；没有时 `pip install ziglang` 自带一个
C 编译器，测试会自动发现。找不到编译器时整组标记为 skipped，不会当成通过。

```bash
pytest tests/test_c_conformance.py -v
CC="zig cc" pytest tests/test_c_conformance.py    # 指定编译器
```

## CI

`.github/workflows/ci.yml`，四个任务：

1. **test**：单元测试 + 跨语言一致性，矩阵覆盖 Ubuntu/Windows × Python 3.10/3.12
2. **firmware**：`-std=c99 -Wall -Wextra -Werror` 编译 C 模块 + 标准向量冒烟
3. **lint**：ruff 静态检查
4. **testbench**：跑完整测试台、归档报告，把 `results.xml` 渲染成 PR 里的
   检查结果，并验证故障注入确实能让测试变红

CI 每一步都能在本地复现：

```bash
python scripts/verify_ci_locally.py
```

## 已知限制

- **模拟器不等于真实设备。** 它验证的是协议和流程的正确性，不是某块板子的
  电气特性。电平、时钟、时序这些真实问题模拟器复现不了。
- **没有做硬件在环。** 示波器、可编程电源等仪器的 SCPI 控制没有实现，
  接口留了（`transport` 可替换）。
- **11 位打包不声称与某个具体厂商型号逐字节兼容。** 实现的是打包规则本身
  （位拼接、掩码、反相），不是某张厂商寄存器表。
- **性能数据是短时实测**，没做长时间运行的稳定性验证。
- **C 侧只有 CRC 与打包**，没有完整固件，未涉及中断、DMA、时钟树配置。

## 许可

MIT
