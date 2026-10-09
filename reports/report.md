# 嵌入式串口设备自动化测试报告

生成时间：2026-10-09 13:07:30

## 总览

| 指标 | 数值 |
|---|---|
| 用例总数 | 26 |
| 通过 | 26 |
| 失败 | 0 |
| 错误 | 0 |
| 跳过 | 0 |
| 通过率 | 100.0% |
| 总耗时 | 0.37 s |
| 被测设备 | DBUS-A5 遥控接收模块（软件模拟器） |
| 传输层 | LoopbackTransport（内存回环，非物理串口） |
| 自检模式 | 正常 |
| 设备响应延迟 | 0 ms |
| 噪声注入 | 关闭 |
| 设备侧统计 | 收帧 56，发帧 56 |

**结论：PASS —— 全部用例通过**

## 套件结果

| 套件 | 用例 | 通过 | 失败 | 错误 | 跳过 | 通过率 | 耗时 |
|---|---|---|---|---|---|---|---|
| `protocol` | 9 | 9 | 0 | 0 | 0 | 100.0% | 0.00s |
| `functional` | 6 | 6 | 0 | 0 | 0 | 100.0% | 0.01s |
| `robustness` | 7 | 7 | 0 | 0 | 0 | 100.0% | 0.00s |
| `performance` | 4 | 4 | 0 | 0 | 0 | 100.0% | 0.36s |

## 全部用例明细

### 套件 `protocol`

| 用例 | 标题 | 级别 | 结果 | 耗时(ms) | 关键证据 |
|---|---|---|---|---|---|
| `proto.crc16.vector` | CRC-16/MODBUS 标准测试向量 | blocker | PASS | 0.0 | crc16=0x4b37 |
| `proto.crc16.incremental` | CRC 分段计算与整体一致 | critical | PASS | 0.0 | whole=0x52c; split=0x52c |
| `proto.crc16.byteorder` | CRC 线上字节序（低字节在前） | blocker | PASS | 0.0 |  |
| `proto.crc8.vector` | CRC-8/MAXIM 标准测试向量 | normal | PASS | 0.0 |  |
| `proto.frame.roundtrip` | 帧编码解码自洽 | blocker | PASS | 0.1 | encoded_len=37; decoded=1 |
| `proto.frame.layout` | 帧结构布局（头/长/CRC 位置） | critical | PASS | 0.0 |  |
| `proto.dbus.roundtrip` | DBUS 16 通道编解码还原 | blocker | PASS | 0.1 | first=364; last=1684 |
| `proto.dbus.invert` | DBUS 电平反相可逆 | critical | PASS | 0.1 | plain_first=0x0; inverted_first=0xff |
| `proto.dbus.midscale` | DBUS 摇杆中位值解析 | normal | PASS | 0.1 |  |

### 套件 `functional`

| 用例 | 标题 | 级别 | 结果 | 耗时(ms) | 关键证据 |
|---|---|---|---|---|---|
| `func.version` | 读取模块版本号 | blocker | PASS | 1.2 | version=1.3.0 |
| `func.channels.range` | 通道值落在有效范围 | blocker | PASS | 2.5 | channels=[1684, 364, 1400, 1024, 364, 1024, 10...; valid=True |
| `func.channels.values` | 通道实测值与设定一致 | critical | PASS | 1.6 | actual=[1684, 364, 1400, 1024, 364] |
| `func.selftest` | 模块自检通过 | critical | PASS | 1.6 | selftest_ok=True |
| `func.echo` | 回环数据一致性 | normal | PASS | 3.2 |  |
| `func.unknown_cmd` | 未知命令返回错误码 | critical | PASS | 3.2 | error_code=0xe0 |

### 套件 `robustness`

| 用例 | 标题 | 级别 | 结果 | 耗时(ms) | 关键证据 |
|---|---|---|---|---|---|
| `robust.crc_detect` | 坏 CRC 被检出并拒绝 | blocker | PASS | 0.0 | frames=[]; errors=['CrcMismatch'] |
| `robust.fragmented` | 跨 chunk 拆包还原 | critical | PASS | 0.0 | chunks=10; frames=1 |
| `robust.partial` | 半包不报错、补齐后成功 | critical | PASS | 0.0 | first_batch=0 |
| `robust.noise_resync` | 噪声前缀后重新同步 | critical | PASS | 0.0 | resyncs=1 |
| `robust.back_to_back` | 多帧粘连全部还原 | normal | PASS | 0.1 |  |
| `robust.dropped_bytes` | 丢字节被检出且链路可恢复 | critical | PASS | 0.1 | accepted=0; rejected=3; simulated=每帧中部抹掉 3 字节 |
| `robust.fault_matrix` | 多故障场景不崩溃 | normal | PASS | 0.2 | scenarios={'丢字节': {'accepted': 0, 'rejected': 2... |

### 套件 `performance`

| 用例 | 标题 | 级别 | 结果 | 耗时(ms) | 关键证据 |
|---|---|---|---|---|---|
| `perf.roundtrip` | 单帧往返延迟 | critical | PASS | 38.7 | iterations=20; total_ms=38.6; per_frame_ms=1.929 |
| `perf.throughput` | 请求吞吐 | normal | PASS | 60.8 | requests=30; seconds=0.061; frames_per_sec=493.3 |
| `perf.timeout` | 超时按时触发不阻塞 | critical | PASS | 252.4 | elapsed_s=0.252; timeout_s=0.25; raised=True |
| `perf.retry` | 重传路径可用 | normal | PASS | 5.5 | version=(1, 3, 0); retries=0 |

---

### 关于本报告

本报告的每一项都可在 `tests/` 下找到对应的自动化测试；`results.xml` 为 JUnit 格式，可直接被 GitHub Actions 等 CI 解析。
