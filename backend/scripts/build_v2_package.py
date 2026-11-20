#!/usr/bin/env python3
"""手动补一份新版本（v2）版本化分卷交付包。

用法：
    python3 -I scripts/build_v2_package.py --out-dir /workspace/samples/packages

产出 package_v2_demo/：
    package.json           清单（字段类型、采集截面、分卷行数与 sha256）
    volumes/part-001.json  分卷一
    volumes/part-002.json  分卷二（含一条显式 null，用来和缺失键做区分）

数据由人工按迁移包契约构造（不是从接口改写），类型覆盖 string/integer/boolean/null，
其中一条记录刻意缺字段，由校验环节按 missing_field 报告。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

PACKAGE_ID = "PKG-V2-DEMO-001"
MODULE = "export"

FIELDS = [
    {"name": "任务编号", "type": "string", "nullable": False},
    {"name": "导出对象", "type": "string", "nullable": False},
    {"name": "导出格式", "type": "string", "nullable": False},
    {"name": "数据行数", "type": "integer", "nullable": True},
    {"name": "文件大小", "type": "integer", "nullable": True},
    {"name": "含敏感字段", "type": "boolean", "nullable": False},
]

VOLUMES = {
    "part-001": [
        {"任务编号": "EXPO-2001", "导出对象": "客户主题宽表", "导出格式": "JSON",
         "数据行数": 1200, "文件大小": 204800, "含敏感字段": False},
        {"任务编号": "EXPO-2002", "导出对象": "订单事实表", "导出格式": "CSV",
         "数据行数": 8600, "文件大小": None, "含敏感字段": True},
    ],
    "part-002": [
        # 显式 null（区别于缺键），另带一个清单未声明的字段 -> extra_field 警告但不阻断
        {"任务编号": "EXPO-2003", "导出对象": "退款明细", "导出格式": "JSONL",
         "数据行数": 320, "文件大小": None, "含敏感字段": False,
         "备注": "人工补录，字段未在清单声明"},
    ],
}


def write_volume(path: Path, payload: dict) -> str:
    """按最终落盘字节计算 sha256，保证与流水线的文件哈希口径一致。"""
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="构造 v2 版本化分卷交付包")
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    package_dir = Path(args.out_dir) / PACKAGE_ID
    volumes_dir = package_dir / "volumes"
    volumes_dir.mkdir(parents=True, exist_ok=True)

    volume_manifest = []
    total_rows = 0
    for volume_id, items in VOLUMES.items():
        payload = {"volume_id": volume_id, "items": items}
        checksum = write_volume(volumes_dir / f"{volume_id}.json", payload)
        volume_manifest.append({
            "volume_id": volume_id,
            "file": f"volumes/{volume_id}.json",
            "row_count": len(items),
            "sha256": checksum,
        })
        total_rows += len(items)

    manifest = {
        "package_version": "2",
        "package_id": PACKAGE_ID,
        "module": MODULE,
        "export_interface": "manual.v2.builder",
        "collected_at": "2026-11-20T09:00:00+08:00",
        "total": total_rows,
        "fields": FIELDS,
        "volumes": volume_manifest,
    }
    (package_dir / "package.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"[完成] v2 交付包：{package_dir}（{len(volume_manifest)} 分卷 / {total_rows} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
