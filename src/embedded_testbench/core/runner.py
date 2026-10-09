"""测试执行器与报告生成。

报告要同时满足三种读者，所以输出三种格式：

======================  ============================================
格式                     用途
======================  ============================================
Markdown (``report.md``) 给人看的：阅读、归档、随交付物附上
JUnit XML (``results.xml``) CI 看的：GitHub Actions 原生解析、失败标红
JSON (``results.json``)  机器看的：趋势统计、看板、二次处理
======================  ============================================

输出 JUnit XML 是为了接入 CI：GitHub Actions、Jenkins、GitLab 都能
直接解析这个格式并标出失败的用例。自定义格式则需要额外的适配。
"""

from __future__ import annotations

import json
import platform
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict
from pathlib import Path

from ..core.case import (
    CheckContext,
    Outcome,
    Severity,
    SuiteResult,
    TestCase,
    TestResult,
)

__all__ = ["Runner", "write_markdown", "write_junit_xml", "write_json"]


class Runner:
    """串行执行测试用例并汇总。

    串行是刻意的：真实串口是独占资源，并行会互相干扰。
    需要并行时应该并行**多台**设备，而不是同一条串口。
    """

    def __init__(self, device=None, verbose: bool = True, fail_fast: bool = False) -> None:
        self.device = device
        self.verbose = verbose
        self.fail_fast = fail_fast

    def run_suite(self, name: str, cases: list[TestCase]) -> SuiteResult:
        suite = SuiteResult(name=name, started_at=time.time())
        start = time.perf_counter()

        for case in cases:
            ctx = CheckContext(device=self.device, timeout_s=case.timeout_s)
            result = case.run(ctx)
            suite.results.append(result)
            if self.verbose:
                self._log(result)
            if self.fail_fast and result.severity is Severity.BLOCKER and not result.passed:
                if self.verbose:
                    print("  [fail-fast] 阻断级用例失败，停止本次运行")
                break

        suite.duration_ms = (time.perf_counter() - start) * 1000.0
        return suite

    @staticmethod
    def _log(r: TestResult) -> None:
        icon = {
            Outcome.PASS: "PASS",
            Outcome.FAIL: "FAIL",
            Outcome.ERROR: "ERROR",
            Outcome.SKIP: "SKIP",
        }[r.outcome]
        line = f"  [{icon:5}] {r.case_id:<28} {r.title}  ({r.duration_ms:.1f}ms)"
        print(line)
        if not r.passed and r.message:
            print(f"          └─ {r.message}")


# ─────────────────────────────────────────────────────────────
# Markdown 报告
# ─────────────────────────────────────────────────────────────

def write_markdown(suites: list[SuiteResult], out: Path, meta: dict | None = None) -> None:
    meta = meta or {}
    total = sum(s.total for s in suites)
    passed = sum(s.passed for s in suites)
    failed = sum(s.failed for s in suites)
    errored = sum(s.errored for s in suites)
    skipped = sum(s.skipped for s in suites)
    counted = total - skipped
    rate = (passed / counted * 100.0) if counted else 100.0
    duration = sum(s.duration_ms for s in suites)

    lines: list[str] = []
    add = lines.append

    add("# 嵌入式串口设备自动化测试报告")
    add("")
    add(f"生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    add("")
    add("## 总览")
    add("")
    add("| 指标 | 数值 |")
    add("|---|---|")
    add(f"| 用例总数 | {total} |")
    add(f"| 通过 | {passed} |")
    add(f"| 失败 | {failed} |")
    add(f"| 错误 | {errored} |")
    add(f"| 跳过 | {skipped} |")
    add(f"| 通过率 | {rate:.1f}% |")
    add(f"| 总耗时 | {duration / 1000:.2f} s |")
    for k, v in meta.items():
        add(f"| {k} | {v} |")
    add("")

    overall_ok = all(s.ok for s in suites)
    add(f"**结论：{'PASS —— 全部用例通过' if overall_ok else 'FAIL —— 存在失败或错误用例'}**")
    add("")

    # 套件明细
    add("## 套件结果")
    add("")
    add("| 套件 | 用例 | 通过 | 失败 | 错误 | 跳过 | 通过率 | 耗时 |")
    add("|---|---|---|---|---|---|---|---|")
    for s in suites:
        add(f"| `{s.name}` | {s.total} | {s.passed} | {s.failed} | "
            f"{s.errored} | {s.skipped} | {s.pass_rate:.1f}% | {s.duration_ms / 1000:.2f}s |")
    add("")

    # 失败详情置顶
    failures = [r for s in suites for r in s.results if not r.passed and r.outcome is not Outcome.SKIP]
    if failures:
        add("## 失败用例详情")
        add("")
        for r in failures:
            add(f"### `{r.case_id}` — {r.title}")
            add("")
            add(f"- 级别：**{r.severity.value}**")
            add(f"- 结果：**{r.outcome.value}**")
            add(f"- 信息：{r.message}")
            if r.assertions:
                add("")
                add("| 断言 | 结果 | 详情 |")
                add("|---|---|---|")
                for a in r.assertions:
                    add(f"| {a.name} | {'OK' if a.ok else 'NG'} | {a.detail} |")
            if r.traceback_text:
                add("")
                add("```")
                add(r.traceback_text.strip())
                add("```")
            add("")

    # 全量明细
    add("## 全部用例明细")
    add("")
    for s in suites:
        add(f"### 套件 `{s.name}`")
        add("")
        add("| 用例 | 标题 | 级别 | 结果 | 耗时(ms) | 关键证据 |")
        add("|---|---|---|---|---|---|")
        for r in s.results:
            mark = {"pass": "PASS", "fail": "**FAIL**", "error": "**ERROR**", "skip": "skip"}[r.outcome.value]
            ev = _evidence_summary(r)
            add(f"| `{r.case_id}` | {r.title} | {r.severity.value} | {mark} | "
                f"{r.duration_ms:.1f} | {ev} |")
        add("")

    add("---")
    add("")
    add("### 关于本报告")
    add("")
    add(
        "本报告的每一项都可在 `tests/` 下找到对应的自动化测试；"
        "`results.xml` 为 JUnit 格式，可直接被 GitHub Actions 等 CI 解析。"
    )
    add("")

    out.write_text("\n".join(lines), encoding="utf-8")


def _evidence_summary(r: TestResult) -> str:
    """把证据压成一行，方便在表格里看。"""
    if not r.evidence:
        return ""
    parts = []
    for k, v in list(r.evidence.items())[:3]:
        if k == "return":
            continue
        s = str(v)
        if len(s) > 40:
            s = s[:37] + "..."
        parts.append(f"{k}={s}")
    return "; ".join(parts)


# ─────────────────────────────────────────────────────────────
# JUnit XML（CI 通用）
# ─────────────────────────────────────────────────────────────

def write_junit_xml(suites: list[SuiteResult], out: Path) -> None:
    total = sum(s.total for s in suites)
    failures = sum(s.failed for s in suites)
    errors = sum(s.errored for s in suites)
    skipped = sum(s.skipped for s in suites)
    duration = sum(s.duration_ms for s in suites) / 1000.0

    root = ET.Element("testsuites", {
        "name": "embedded-testbench",
        "tests": str(total),
        "failures": str(failures),
        "errors": str(errors),
        "skipped": str(skipped),
        "time": f"{duration:.3f}",
    })

    for s in suites:
        ts = ET.SubElement(root, "testsuite", {
            "name": s.name,
            "tests": str(s.total),
            "failures": str(s.failed),
            "errors": str(s.errored),
            "skipped": str(s.skipped),
            "time": f"{s.duration_ms / 1000:.3f}",
        })
        for r in s.results:
            tc = ET.SubElement(ts, "testcase", {
                "classname": f"embedded_testbench.{s.name}",
                "name": r.case_id,
                "time": f"{r.duration_ms / 1000:.4f}",
            })
            if r.outcome is Outcome.FAIL:
                ET.SubElement(tc, "failure", {
                    "message": r.message, "type": "AssertionFailure",
                }).text = _detail_text(r)
            elif r.outcome is Outcome.ERROR:
                ET.SubElement(tc, "error", {
                    "message": r.message, "type": "RuntimeError",
                }).text = r.traceback_text or r.message
            elif r.outcome is Outcome.SKIP:
                ET.SubElement(tc, "skipped", {"message": r.message})
            if r.evidence:
                ET.SubElement(tc, "system-out").text = json.dumps(
                    r.evidence, ensure_ascii=False, default=str, indent=2
                )

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    tree.write(out, encoding="utf-8", xml_declaration=True)


def _detail_text(r: TestResult) -> str:
    parts = [r.message]
    for a in r.assertions:
        parts.append(f"  [{'OK' if a.ok else 'NG'}] {a.name}: {a.detail}")
    return "\n".join(parts)


# ─────────────────────────────────────────────────────────────
# JSON（机器可读）
# ─────────────────────────────────────────────────────────────

def write_json(suites: list[SuiteResult], out: Path, meta: dict | None = None) -> None:
    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
        "meta": meta or {},
        "suites": [
            {
                "name": s.name,
                "total": s.total,
                "passed": s.passed,
                "failed": s.failed,
                "errored": s.errored,
                "skipped": s.skipped,
                "pass_rate": round(s.pass_rate, 2),
                "duration_ms": round(s.duration_ms, 2),
                "ok": s.ok,
                "results": [
                    {
                        "case_id": r.case_id,
                        "title": r.title,
                        "severity": r.severity.value,
                        "outcome": r.outcome.value,
                        "duration_ms": round(r.duration_ms, 3),
                        "message": r.message,
                        "assertions": [asdict(a) for a in r.assertions],
                        "evidence": r.evidence,
                    }
                    for r in s.results
                ],
            }
            for s in suites
        ],
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
