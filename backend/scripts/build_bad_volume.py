#!/usr/bin/env python3
"""手动补一份校验失败的分卷包：清单是正确的，分卷文件被刻意做坏。

用法：
    python3 -I scripts/build_bad_volume.py --out-dir /workspace/samples/packages

产出 package_v2_bad/，两类坏分卷各一：
- part-001：清单登记 2 行、sha256 按 2 行内容计算，写盘时偷偷加 1 行
  -> checksum_mismatch + row_count_mismatch；
- part-002：把 integer 字段写成字符串、boolean 字段写成数字
  -> field_type_mismatch。

流水线摄入此包必须整体阻断（rejected），坏分卷一行都不能进已发布数据。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

PACKAGE_ID = "PKG-V2-BAD-001"
MODULE = "export"

FIELDS = [
    {"name": "任务编号", "type": "string", "nullable": False},
    {"name": "导出格式", "type": "string", "nullable": False},
    {"name": "数据行数", "type": "integer", "nullable": False},
    {"name": "含敏感字段", "type": "boolean", "nullable": False},
]

# 清单计算 checksum 时使用的“名义”内容。
NOMINAL_VOLUMES = {
    "part-001": [
        {"任务编号": "EXPO-9001", "导出格式": "JSON", "数据行数": 10, "含敏感字段": False},
        {"任务编号": "EXPO-9002", "导出格式": "CSV", "数据行数": 20, "含敏感字段": False},
    ],
    "part-002": [
        {"任务编号": "EXPO-9003", "导出格式": "JSONL", "数据行数": 30, "含敏感字段": True},
    ],
    "part-003": [
        # 缺少清单要求的「导出格式」键 -> missing_field（与显式 null 区分的阻断证据）
        {"任务编号": "EXPO-9004", "数据行数": 40, "含敏感字段": False},
    ],
}

# 实际写盘的内容。
ACTUAL_VOLUMES = {
    "part-001": NOMINAL_VOLUMES["part-001"] + [
        {"任务编号": "EXPO-9099", "导出格式": "PARQUET", "数据行数": 99, "含敏感字段": True},
    ],
    "part-002": [
        {"任务编号": "EXPO-9003", "导出格式": "JSONL", "数据行数": "三十行", "含敏感字段": 1},
    ],
    "part-003": NOMINAL_VOLUMES["part-003"],
}


def write_volume(path: Path, payload: dict) -> str:
    """按最终落盘字节计算 sha256，保证与流水线的文件哈希口径一致。"""
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    path.write_bytes(body)
    return hashlib.sha256(body).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="构造带坏分卷的 v2 交付包")
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args()

    package_dir = Path(args.out_dir) / PACKAGE_ID
    volumes_dir = package_dir / "volumes"
    volumes_dir.mkdir(parents=True, exist_ok=True)

    volume_manifest = []
    for volume_id, nominal_items in NOMINAL_VOLUMES.items():
        nominal = {"volume_id": volume_id, "items": nominal_items}
        actual = {"volume_id": volume_id, "items": ACTUAL_VOLUMES[volume_id]}
        # 清单 checksum 按名义内容计算，实际写盘的是被做坏的内容，两者必然不一致。
        checksum = write_volume(volumes_dir / f"{volume_id}.json", actual)
        nominal_body = json.dumps(nominal, ensure_ascii=False, indent=2).encode("utf-8")
        nominal_checksum = hashlib.sha256(nominal_body).hexdigest()
        volume_manifest.append({
            "volume_id": volume_id,
            "file": f"volumes/{volume_id}.json",
            "row_count": len(nominal_items),          # 与实际文件不符
            "sha256": nominal_checksum,               # 按名义内容算，与实际文件不符
        })

    manifest = {
        "package_version": "2",
        "package_id": PACKAGE_ID,
        "module": MODULE,
        "export_interface": "manual.bad.builder",
        "collected_at": "2026-11-20T09:30:00+08:00",
        "total": sum(len(items) for items in NOMINAL_VOLUMES.values()),
        "fields": FIELDS,
        "volumes": volume_manifest,
    }
    (package_dir / "package.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(f"[完成] 坏分卷包：{package_dir}（part-001 行数/校验和坏，part-002 字段类型坏）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
