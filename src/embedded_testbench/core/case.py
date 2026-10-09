"""测试用例与断言：把"检查设备行为"变成可复用、可报告的对象。

设计目标
--------
手写 ``if not ok: print("失败")`` 的问题是：结果不可统计、不可对比、
进了 CI 也没人看得懂。所以这里把每个检查封装成一个 **TestCase**：

* 有 ``id`` / ``title`` / ``severity``，报告里能定位；
* 抛出 :class:`AssertionFailure` 记失败，其它异常记 **错误**（区别对待：
  失败=被测行为不符，错误=框架或设备本身炸了）；
* 记录**耗时**与**证据**（evidence），报告里能贴出实测数值。

这是 pytest/unittest 的核心抽象，只不过这里面向的是**串口设备**：
断言对象不是函数返回值，而是"从设备读回来的帧"。
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

__all__ = [
    "Severity", "Outcome", "AssertionFailure", "TestCase", "TestResult",
    "SuiteResult", "check",
]


class Severity(str, Enum):
    """用例重要程度——决定发布门槛（见 runner 的 fail-fast 策略）。"""

    BLOCKER = "blocker"  # 挂了绝对不能发货
    CRITICAL = "critical"  # 核心功能，挂了要修
    NORMAL = "normal"  # 一般检查
    MINOR = "minor"  # 提示性，不阻塞


class Outcome(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    SKIP = "skip"


class AssertionFailure(Exception):
    """断言不通过——被测对象行为不符合预期。"""


@dataclass
class Assertion:
    """一条断言记录，进报告用。"""

    name: str
    ok: bool
    detail: str = ""


@dataclass
class TestResult:
    """单个用例的执行结果。"""

    case_id: str
    title: str
    severity: Severity
    outcome: Outcome
    duration_ms: float
    message: str = ""
    assertions: list[Assertion] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    traceback_text: str = ""

    @property
    def passed(self) -> bool:
        return self.outcome is Outcome.PASS


@dataclass
class SuiteResult:
    """一个套件的汇总结果。"""

    name: str
    results: list[TestResult] = field(default_factory=list)
    started_at: float = 0.0
    duration_ms: float = 0.0

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.outcome is Outcome.PASS)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.outcome is Outcome.FAIL)

    @property
    def errored(self) -> int:
        return sum(1 for r in self.results if r.outcome is Outcome.ERROR)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.outcome is Outcome.SKIP)

    @property
    def pass_rate(self) -> float:
        counted = self.total - self.skipped
        return (self.passed / counted * 100.0) if counted else 100.0

    @property
    def ok(self) -> bool:
        """本次运行是否算通过（决定 CI 退出码）。

        规则：**任何** FAIL 或 ERROR 都算不通过。

        这里刻意**不**按 severity 过滤失败项。早期版本只让 blocker 级
        失败影响结论，结果是"有 1 条 critical 用例挂了、整体仍报 PASS、
        CI 退出码 0"——红灯被吞掉了，测试框架就失去了意义。
        severity 用来给**人**排优先级（报告里排序、决定是否阻断发布），
        但机器判定必须是"全绿才算过"。

        跳过（skip）不算失败——那是"本环境不适用"，不是"被测对象坏了"。
        """
        return self.failed == 0 and self.errored == 0


class _SkipTest(Exception):
    """内部用：主动跳过用例。"""


class TestCase:
    """一个可执行的设备检查。

    ``fn`` 接收一个 :class:`CheckContext`，用 :func:`check` / ``ctx.expect``
    做断言。返回值会作为 evidence 记录进报告。
    """

    def __init__(
        self,
        case_id: str,
        title: str,
        fn: Callable[[CheckContext], Any],
        severity: Severity = Severity.NORMAL,
        timeout_s: float = 5.0,
        tags: tuple[str, ...] = (),
    ) -> None:
        self.case_id = case_id
        self.title = title
        self.fn = fn
        self.severity = severity
        self.timeout_s = timeout_s
        self.tags = tags

    def run(self, ctx: CheckContext) -> TestResult:
        ctx.reset_assertions()
        start = time.perf_counter()
        outcome = Outcome.PASS
        message = ""
        tb = ""

        try:
            returned = self.fn(ctx)
            if returned is not None:
                ctx.evidence["return"] = returned
        except _SkipTest as e:
            outcome = Outcome.SKIP
            message = str(e)
        except AssertionFailure as e:
            outcome = Outcome.FAIL
            message = str(e)
        except Exception as e:  # noqa: BLE001 - 框架必须兜住一切
            outcome = Outcome.ERROR
            message = f"{type(e).__name__}: {e}"
            tb = traceback.format_exc()

        duration = (time.perf_counter() - start) * 1000.0
        return TestResult(
            case_id=self.case_id,
            title=self.title,
            severity=self.severity,
            outcome=outcome,
            duration_ms=duration,
            message=message,
            assertions=list(ctx.assertions),
            evidence=dict(ctx.evidence),
            traceback_text=tb,
        )


class CheckContext:
    """用例运行时上下文：收集断言与证据，并暴露被测设备。"""

    def __init__(self, device: Any = None, timeout_s: float = 5.0) -> None:
        self.device = device
        self.timeout_s = timeout_s
        self.assertions: list[Assertion] = []
        self.evidence: dict[str, Any] = {}

    def reset_assertions(self) -> None:
        self.assertions = []
        self.evidence = {}

    # ── 断言 API ────────────────────────────────────────────────
    def expect(self, ok: bool, name: str, detail: str = "") -> None:
        """记录一条断言；不通过则立刻抛出，终止本用例。"""
        self.assertions.append(Assertion(name=name, ok=bool(ok), detail=detail))
        if not ok:
            raise AssertionFailure(f"{name}" + (f" — {detail}" if detail else ""))

    def expect_eq(self, actual: Any, expected: Any, name: str = "相等") -> None:
        self.expect(
            actual == expected,
            name,
            f"期望 {expected!r}，实际 {actual!r}",
        )

    def expect_in(self, value: Any, low: Any, high: Any, name: str = "范围") -> None:
        self.expect(
            low <= value <= high,
            name,
            f"期望落在 [{low}, {high}]，实际 {value}",
        )

    def expect_close(self, actual: float, expected: float, tol: float, name: str = "接近") -> None:
        delta = abs(actual - expected)
        self.expect(
            delta <= tol,
            name,
            f"|{actual} - {expected}| = {delta:.4g} > 容差 {tol}",
        )

    def skip(self, reason: str) -> None:
        raise _SkipTest(reason)

    def note(self, **kwargs: Any) -> None:
        """记录证据（实测数值），会出现在报告里。"""
        self.evidence.update(kwargs)


def check(condition: bool, name: str, detail: str = "") -> None:
    """模块级断言助手，便于在辅助函数里使用。"""
    if not condition:
        raise AssertionFailure(f"{name}" + (f" — {detail}" if detail else ""))
