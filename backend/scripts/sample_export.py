#!/usr/bin/env python3
"""从仓库原导出接口取样，落两份存量 v1 清单原始文件。

用法：
    python3 -I scripts/sample_export.py --base-url http://127.0.0.1:8000 --out-dir /workspace/samples/raw

两份样本取自两个既有导出接口（GET /api/export/export 与 GET /api/data_service/export），
保留原始 {module,total,items} 契约不改写。两个接口里存在同号行（同 id、同业务编号），
但字段集合不同，迁移时必须按来源各自保留，不能合并。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

SOURCES = [
    ("export", "/api/export/export", "legacy_export_a.json"),
    ("data_service", "/api/data_service/export", "legacy_export_b.json"),
]


def fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 本地受信服务
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description="从原导出接口取样存量 JSON 清单")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for module, path, filename in SOURCES:
        url = f"{args.base_url.rstrip('/')}{path}"
        payload = fetch(url)
        if not {"module", "total", "items"} <= set(payload):
            print(f"[拒绝] {url} 返回不符合 {module,total,items} 契约", file=sys.stderr)
            return 2
        target = out_dir / filename
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        written.append(str(target))
        print(f"[取样] {url} -> {target}（module={payload['module']} total={payload['total']}）")
    print("[完成] 存量样本：" + "、".join(written))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
