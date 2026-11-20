#!/usr/bin/env python3
"""端到端验证：通过数据 API 跑完整迁移流水线（只用标准库，可 python3 -I 运行）。

前置：后端已在 --base-url 上启动，且样本与交付包已生成：
    python3 -I scripts/sample_export.py --out-dir <raw_dir>
    python3 -I scripts/build_v2_package.py --out-dir <pkg_dir>
    python3 -I scripts/build_bad_volume.py --out-dir <pkg_dir>

验证项（逐项打印 PASS/FAIL，任一失败退出码非 0）：
1. 两份存量 v1 包都能读取并发布；同号行不合并，数据范围可对账；
2. v2 新包发布，显式 null 与缺失字段在数据里分别留痕；
3. 坏分卷包被拒绝，已发布数据集行数不变；
4. 同源包重复迁移只确认一次（duplicate），同号不同内容被拒绝；
5. 对账可重跑且能回查原包编号，报告带行数与问题类型；
6. 数据服务 published_dataset 能读存量 JSON 与新包（历史兼容）；
7. 撤回与回滚到最近可复验交付生效。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

FAILURES: list[str] = []


def call(method: str, url: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 本地受信服务
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" —— {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    parser = argparse.ArgumentParser(description="迁移流水线端到端验证")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--raw-dir", required=True)
    parser.add_argument("--pkg-dir", required=True)
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    raw = Path(args.raw_dir)
    pkg = Path(args.pkg_dir)

    status, health = call("GET", f"{base}/api/health")
    check("后端健康检查", status == 200 and health.get("ok") is True, str(health))

    # 1. 两份存量 v1 包
    r1 = call("POST", f"{base}/api/migration/ingest",
              {"path": str(raw / "legacy_export_a.json")})[1]
    r2 = call("POST", f"{base}/api/migration/ingest",
              {"path": str(raw / "legacy_export_b.json")})[1]
    check("存量包 A 发布", r1.get("status") == "published", r1.get("message", ""))
    check("存量包 B 发布", r2.get("status") == "published", r2.get("message", ""))

    scope = call("GET", f"{base}/api/migration/scope")[1]
    shared = scope.get("shared_business_keys", {})
    check("同号行不合并（跨包同业务编号各自保留）",
          len(shared) >= 1, f"跨包同号编号 {len(shared)} 个：{list(shared)[:3]}")
    pkg_rows = {item["package_id"]: item["row_count"] for item in scope["packages"]}
    check("两包数据范围分别可对账",
          all(count > 0 for count in pkg_rows.values()), str(pkg_rows))

    # 2. v2 新包
    v2 = call("POST", f"{base}/api/migration/ingest",
              {"path": str(pkg / "PKG-V2-DEMO-001")})[1]
    check("v2 新包发布", v2.get("status") == "published", v2.get("message", ""))
    recon = v2.get("reconciliation", {})
    check("v2 对账带分卷行数",
          recon.get("volume_rows") == {"part-001": 2, "part-002": 1},
          str(recon.get("volume_rows")))
    check("v2 未声明字段按 extra_field 警告且不阻断",
          recon.get("issue_summary", {}).get("extra_field") == 1,
          str(recon.get("issue_summary")))
    ds = call("GET", f"{base}/api/migration/dataset?package_id=PKG-V2-DEMO-001&size=10")[1]
    null_trace = [
        row for row in ds["items"]
        if "文件大小" in row.get("__provenance__", {}).get("explicit_null_fields", [])
    ]
    check("显式 null 在数据里留痕（与缺失键区分）",
          len(null_trace) == 2,
          f"显式 null 行={len(null_trace)}")

    rows_before_bad = call("GET", f"{base}/api/migration/dataset?size=1")[1]["total"]

    # 3. 坏分卷包
    bad = call("POST", f"{base}/api/migration/ingest",
               {"path": str(pkg / "PKG-V2-BAD-001")})[1]
    check("坏分卷包被拒绝", bad.get("status") == "rejected", bad.get("message", ""))
    bad_codes = set(bad.get("delivery", {}).get("issue_summary", {}))
    check("坏包报告行数、校验和与字段类型问题",
          {"row_count_mismatch", "field_type_mismatch", "checksum_mismatch", "missing_field"}
          <= bad_codes,
          str(sorted(bad_codes)))
    rows_after_bad = call("GET", f"{base}/api/migration/dataset?size=1")[1]["total"]
    check("坏分卷未混入已发布数据", rows_before_bad == rows_after_bad,
          f"{rows_before_bad} -> {rows_after_bad}")

    # 4. 幂等：同源重复只确认一次；同号不同内容拒绝
    dup = call("POST", f"{base}/api/migration/ingest",
               {"path": str(pkg / "PKG-V2-DEMO-001")})[1]
    check("同源包重复迁移只确认一次",
          dup.get("status") == "duplicate" and dup.get("delivery", {}).get("duplicated_of"),
          dup.get("message", ""))
    rows_after_dup = call("GET", f"{base}/api/migration/dataset?size=1")[1]["total"]
    check("重复确认不新增数据行", rows_after_dup == rows_after_bad,
          f"{rows_after_bad} -> {rows_after_dup}")

    tampered_dir = Path(tempfile.mkdtemp(prefix="migration-tampered-")) / "PKG-V2-DEMO-001"
    import shutil
    shutil.copytree(pkg / "PKG-V2-DEMO-001", tampered_dir)
    target = tampered_dir / "volumes" / "part-001.json"
    body = json.loads(target.read_text(encoding="utf-8"))
    body["items"][0]["数据行数"] = 999999
    target.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    tampered = call("POST", f"{base}/api/migration/ingest", {"path": str(tampered_dir)})[1]
    check("同号不同内容被拒绝",
          tampered.get("status") == "rejected"
          and "content_changed" in tampered.get("delivery", {}).get("issue_summary", {}),
          tampered.get("message", ""))

    # 5. 对账重跑，原包编号可回查
    rerun = call("POST", f"{base}/api/migration/deliveries/PKG-V2-DEMO-001/reconcile")[1]
    check("对账重跑带原包编号与问题类型",
          rerun.get("package_id") == "PKG-V2-DEMO-001"
          and rerun.get("issue_summary", {}).get("extra_field") == 1
          and rerun.get("accepted_rows") == 3,
          str({k: rerun.get(k) for k in ("package_id", "accepted_rows", "issue_summary")}))
    recons = call("GET", f"{base}/api/migration/reconciliations?package_id=PKG-V2-DEMO-001")[1]
    check("对账报告可历史查询", recons["total"] >= 2, f"报告数={recons['total']}")
    v1_rerun = call("POST", f"{base}/api/migration/deliveries/v1-legacy_export_a/reconcile")[1]
    check("存量 v1 包也可重跑对账且原包编号可查",
          v1_rerun.get("package_id") == "v1-legacy_export_a"
          and v1_rerun.get("accepted_rows") == 3,
          str({k: v1_rerun.get(k) for k in ("package_id", "accepted_rows", "data_scope")}))

    # 6. 数据服务消费：新旧包同一入口可读
    svc_ds = call("GET", f"{base}/api/data_service/published_dataset?size=200")[1]
    sources = {row["__provenance__"]["package_id"] for row in svc_ds["items"]}
    check("数据服务同读存量 JSON 与新包",
          {"v1-legacy_export_a", "v1-legacy_export_b", "PKG-V2-DEMO-001"} <= sources,
          str(sorted(sources)))
    overview = call("GET", f"{base}/api/data_service/migration_overview")[1]
    check("迁移对账概况可查",
          overview["published_packages"] >= 3 and overview["rejected_packages"] >= 1,
          str({k: overview[k] for k in ("delivery_total", "published_packages", "rejected_packages")}))

    # 7. 撤回与回滚
    withdraw = call("POST", f"{base}/api/migration/deliveries/PKG-V2-DEMO-001/withdraw")[1]
    check("撤回 v2 包生效", withdraw.get("ok") and withdraw.get("removed_rows") == 3,
          str(withdraw))
    remaining = call("GET", f"{base}/api/migration/dataset?package_id=PKG-V2-DEMO-001&size=1")[1]
    check("撤回后来源数据不可查", remaining["total"] == 0, f"剩余 {remaining['total']} 行")
    rollback = call("POST", f"{base}/api/migration/rollback")[1]
    check("回滚到最近可复验交付", rollback.get("ok") and rollback.get("snapshot_id"),
          str(rollback))

    print("\n交付历史：")
    for item in call("GET", f"{base}/api/migration/deliveries")[1]["items"]:
        print(f"  - {item['package_id']:<28} {item['status']:<9} "
              f"rows={item['record_count']} issues={item['issue_summary']}")

    if FAILURES:
        print(f"\n{len(FAILURES)} 项失败：{FAILURES}")
        return 1
    print("\n全部验证通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
