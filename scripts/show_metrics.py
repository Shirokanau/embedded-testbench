import json
from pathlib import Path

p = Path(__file__).resolve().parents[1] / "reports" / "results.json"
d = json.loads(p.read_text(encoding="utf-8"))

print("=== 性能套件实测值（用于核对 README 中的数字）===")
for s in d["suites"]:
    if s["name"] != "performance":
        continue
    for r in s["results"]:
        ev = {k: v for k, v in r.get("evidence", {}).items() if k != "return"}
        print(f'  {r["case_id"]:22} {r["outcome"]:5} {ev}')

print()
print("=== 全部套件汇总 ===")
tot = passed = 0
for s in d["suites"]:
    tot += s["total"]
    passed += s["passed"]
    print(f'  {s["name"]:12} {s["total"]:3} 用例  {s["passed"]:3} 通过  {s["duration_ms"]:8.1f} ms')
print(f'  {"合计":12} {tot:3} 用例  {passed:3} 通过')
