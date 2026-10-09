"""框架层：用例模型、断言、执行器与报告。"""
from .case import (
    AssertionFailure,
    CheckContext,
    Outcome,
    Severity,
    SuiteResult,
    TestCase,
    TestResult,
    check,
)
from .runner import Runner, write_json, write_junit_xml, write_markdown

__all__ = [
    "AssertionFailure", "CheckContext", "Outcome", "Severity", "SuiteResult",
    "TestCase", "TestResult", "check",
    "Runner", "write_json", "write_junit_xml", "write_markdown",
]
