"""导出包读取与校验：同时支持存量 v1 清单与 v2 版本化分卷交付包。

v1（存量，接口契约保持不变）::

    {"module": "export", "total": 3, "items": [ ... ]}

v2（新版本化交付包，目录形式）::

    package.json          # 清单：版本、字段类型、采集截面、分卷清单（行数 + sha256）
    volumes/part-001.json # 分卷：{"volume_id": ..., "items": [ ... ]}

校验口径（详见 README「迁移包格式与校验」）：
- 分卷文件必须存在、可解析，sha256 与行数必须与清单一致；
- 每个字段按清单声明的类型校验；缺失键与显式 null 分别记录，不混为一谈；
- 任一分卷出现错误，整包判定为坏包，调用方不得发布。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SUPPORTED_VERSIONS = {"1", "2"}
LATEST_VERSION = "2"

# 问题类型编码；error 阻断发布，warning 不阻断但必须进对账报告。
ISSUE_CODES = [
    "unsupported_version",     # 清单声明了无法读取的包版本
    "manifest_malformed",      # 清单缺关键字段或结构不合法
    "volume_missing",          # 清单登记的分卷文件不存在
    "volume_unparsable",       # 分卷不是合法 JSON 或结构不对
    "checksum_mismatch",       # 分卷 sha256 与清单不一致
    "row_count_mismatch",      # 分卷实际行数与清单登记行数不一致
    "field_type_mismatch",     # 字段值类型与清单声明不符（显式 null 除外）
    "missing_field",           # 记录缺少清单要求的字段键（区别于显式 null）
    "unexpected_volume",       # 目录里出现清单未登记的分卷
    "total_mismatch",          # v1 的 total 与 items 实际数量不符
    "extra_field",             # 记录带了清单未声明的字段（warning）
]

# 声明类型 -> Python 判定函数。bool 不算 number/integer（与 JSON 语义一致）。
TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
}


@dataclass
class Issue:
    """一条校验/对账问题。level 为 error 时阻断发布。"""

    level: str
    code: str
    message: str
    volume_id: str | None = None
    record_index: int | None = None
    field_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "code": self.code,
            "message": self.message,
            "volume_id": self.volume_id,
            "record_index": self.record_index,
            "field_name": self.field_name,
        }


@dataclass
class NormalizedRecord:
    """归一化后的记录：值与来源、缺失/空值证据一起保留，不做跨包合并。"""

    record_no: int                      # 包内全局序号，从 1 开始
    values: dict[str, Any]              # 实际携带的字段（缺失键不放进来）
    missing_fields: list[str]           # 清单要求但该记录完全没有的键
    explicit_null_fields: list[str]     # 显式给了 null 的键
    source_volume: str | None           # 来自哪个分卷（v1 为 None）
    volume_record_index: int | None     # 分卷内序号，从 0 开始

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_no": self.record_no,
            "values": self.values,
            "missing_fields": self.missing_fields,
            "explicit_null_fields": self.explicit_null_fields,
            "source_volume": self.source_volume,
            "volume_record_index": self.volume_record_index,
        }


@dataclass
class LoadedPackage:
    """读入并归一化后的交付包。"""

    path: str
    package_id: str
    package_version: str
    module: str
    export_interface: str | None
    collected_at: str | None
    field_schema: list[dict[str, Any]]
    records: list[NormalizedRecord]
    volume_manifest: list[dict[str, Any]]
    issues: list[Issue] = field(default_factory=list)
    declared_total: int | None = None
    source_fingerprint: str = ""

    @property
    def ok(self) -> bool:
        return not any(issue.level == "error" for issue in self.issues)

    def issue_dicts(self) -> list[dict[str, Any]]:
        return [issue.to_dict() for issue in self.issues]

    def issue_summary(self) -> dict[str, int]:
        """按问题类型聚合计数，给迁移对账用。"""
        summary: dict[str, int] = {}
        for issue in self.issues:
            summary[issue.code] = summary.get(issue.code, 0) + 1
        return summary


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fingerprint(path: Path) -> str:
    """源包指纹：目录则按相对路径排序逐文件哈希，单文件直接哈希。"""
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.read_bytes())
        return digest.hexdigest()
    for child in sorted(p for p in path.rglob("*") if p.is_file()):
        digest.update(str(child.relative_to(path)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(child.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _check_record_types(
    values: dict[str, Any],
    schema: list[dict[str, Any]],
    *,
    volume_id: str | None,
    record_index: int,
    issues: list[Issue],
) -> tuple[list[str], list[str]]:
    """按字段清单核对单条记录，返回 (缺失键, 显式 null 键)。"""
    missing: list[str] = []
    explicit_null: list[str] = []
    declared = {item["name"]: item for item in schema}
    for name, spec in declared.items():
        if name not in values:
            missing.append(name)
            issues.append(Issue(
                "error", "missing_field",
                f"记录缺少清单要求的字段「{name}」（缺失键，与显式 null 区分）",
                volume_id, record_index, name,
            ))
            continue
        value = values[name]
        if value is None:
            if not spec.get("nullable", False):
                issues.append(Issue(
                    "error", "field_type_mismatch",
                    f"字段「{name}」不允许 null，却收到显式 null",
                    volume_id, record_index, name,
                ))
            else:
                explicit_null.append(name)
            continue
        checker = TYPE_CHECKS.get(spec["type"])
        if checker is None or not checker(value):
            issues.append(Issue(
                "error", "field_type_mismatch",
                f"字段「{name}」声明类型 {spec['type']}，实际为 {type(value).__name__}",
                volume_id, record_index, name,
            ))
    for name in values:
        if name not in declared:
            issues.append(Issue(
                "warning", "extra_field",
                f"字段「{name}」未在清单声明，原样保留但不参与类型校验",
                volume_id, record_index, name,
            ))
    return missing, explicit_null


def _build_v1_schema(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """存量包没有字段清单：按并到的键推断一版宽松 schema（全部 nullable string 风格）。

    只用于归一化与展示，不产生类型校验问题——旧包缺类型信息是已知事实。
    """
    names: list[str] = []
    seen: set[str] = set()
    for record in records:
        for key in record:
            if key not in seen:
                seen.add(key)
                names.append(key)
    return [{"name": name, "type": "any", "nullable": True} for name in names]


def _load_v1(path: Path) -> LoadedPackage:
    issues: list[Issue] = []
    data = _load_json(path)
    if not isinstance(data, dict) or "items" not in data or not isinstance(data["items"], list):
        raise ValueError("不是合法的 v1 清单：缺少 items 数组")
    module = str(data.get("module") or path.stem)
    declared_total = data.get("total")
    raw_records = data["items"]
    if isinstance(declared_total, int) and declared_total != len(raw_records):
        issues.append(Issue(
            "error", "total_mismatch",
            f"清单 total={declared_total} 与 items 实际行数 {len(raw_records)} 不一致",
        ))
    schema = _build_v1_schema([r for r in raw_records if isinstance(r, dict)])
    records: list[NormalizedRecord] = []
    for index, raw in enumerate(raw_records):
        if not isinstance(raw, dict):
            issues.append(Issue(
                "error", "field_type_mismatch",
                f"第 {index} 行不是对象，无法作为记录读取",
                None, index,
            ))
            continue
        values = dict(raw)
        explicit_null = [name for name, value in values.items() if value is None]
        # v1 无 schema 约束：未出现的键记为缺失证据，但不产出 error。
        missing = [item["name"] for item in schema if item["name"] not in values]
        records.append(NormalizedRecord(
            record_no=index + 1,
            values=values,
            missing_fields=missing,
            explicit_null_fields=explicit_null,
            source_volume=None,
            volume_record_index=index,
        ))
    package_id = f"v1-{path.stem}"
    return LoadedPackage(
        path=str(path),
        package_id=package_id,
        package_version="1",
        module=module,
        export_interface=data.get("export_interface"),
        collected_at=data.get("collected_at"),
        field_schema=schema,
        records=records,
        volume_manifest=[],
        issues=issues,
        declared_total=declared_total if isinstance(declared_total, int) else len(raw_records),
        source_fingerprint=_fingerprint(path),
    )


def _load_v2(path: Path) -> LoadedPackage:
    issues: list[Issue] = []
    manifest_path = path / "package.json"
    if not manifest_path.is_file():
        raise ValueError("v2 交付包目录下缺少 package.json 清单")
    manifest = _load_json(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError("package.json 清单不是 JSON 对象")

    package_id = manifest.get("package_id")
    module = manifest.get("module")
    schema = manifest.get("fields")
    volumes = manifest.get("volumes")
    if not package_id or not isinstance(package_id, str):
        issues.append(Issue("error", "manifest_malformed", "清单缺少 package_id"))
        package_id = package_id or path.name
    if not module or not isinstance(module, str):
        issues.append(Issue("error", "manifest_malformed", "清单缺少 module"))
        module = module or path.name
    if not isinstance(schema, list) or not all(isinstance(item, dict) and "name" in item for item in schema):
        issues.append(Issue("error", "manifest_malformed", "清单 fields 必须是带 name 的字段定义数组"))
        schema = schema if isinstance(schema, list) else []
    if not isinstance(volumes, list):
        issues.append(Issue("error", "manifest_malformed", "清单 volumes 必须是分卷描述数组"))
        volumes = []

    declared_types = {item["name"]: item.get("type") for item in schema if isinstance(item, dict)}
    for item in schema:
        if isinstance(item, dict) and item.get("type") not in TYPE_CHECKS:
            issues.append(Issue(
                "error", "manifest_malformed",
                f"字段「{item.get('name')}」声明了不支持的类型 {item.get('type')}",
                None, None, item.get("name"),
            ))

    records: list[NormalizedRecord] = []
    seen_volumes: set[str] = set()
    global_no = 1
    for entry in volumes:
        volume_id = entry.get("volume_id") if isinstance(entry, dict) else None
        if not volume_id:
            issues.append(Issue("error", "manifest_malformed", "分卷条目缺少 volume_id"))
            continue
        seen_volumes.add(volume_id)
        volume_path = path / "volumes" / f"{volume_id}.json"
        if not volume_path.is_file():
            issues.append(Issue(
                "error", "volume_missing",
                f"分卷 {volume_id} 的文件 volumes/{volume_id}.json 不存在",
                volume_id,
            ))
            continue
        actual_checksum = _sha256_file(volume_path)
        if entry.get("sha256") and actual_checksum != entry["sha256"]:
            issues.append(Issue(
                "error", "checksum_mismatch",
                f"分卷 {volume_id} 校验和不一致：清单 {entry.get('sha256')[:12]}… 实际 {actual_checksum[:12]}…",
                volume_id,
            ))
        try:
            volume_data = _load_json(volume_path)
        except (json.JSONDecodeError, OSError) as exc:
            issues.append(Issue(
                "error", "volume_unparsable",
                f"分卷 {volume_id} 无法解析：{exc}",
                volume_id,
            ))
            continue
        if not isinstance(volume_data, dict) or not isinstance(volume_data.get("items"), list):
            issues.append(Issue(
                "error", "volume_unparsable",
                f"分卷 {volume_id} 结构应为带 items 数组的对象",
                volume_id,
            ))
            continue
        items = volume_data["items"]
        declared_rows = entry.get("row_count")
        if isinstance(declared_rows, int) and declared_rows != len(items):
            issues.append(Issue(
                "error", "row_count_mismatch",
                f"分卷 {volume_id} 登记行数 {declared_rows}，实际 {len(items)}",
                volume_id,
            ))
        if volume_data.get("volume_id") and volume_data["volume_id"] != volume_id:
            issues.append(Issue(
                "error", "manifest_malformed",
                f"分卷文件自报 volume_id={volume_data['volume_id']} 与清单 {volume_id} 不符",
                volume_id,
            ))
        for index, raw in enumerate(items):
            if not isinstance(raw, dict):
                issues.append(Issue(
                    "error", "field_type_mismatch",
                    f"分卷 {volume_id} 第 {index} 行不是对象",
                    volume_id, index,
                ))
                continue
            missing, explicit_null = _check_record_types(
                raw, [item for item in schema if isinstance(item, dict)],
                volume_id=volume_id, record_index=index, issues=issues,
            )
            records.append(NormalizedRecord(
                record_no=global_no,
                values=dict(raw),
                missing_fields=missing,
                explicit_null_fields=explicit_null,
                source_volume=volume_id,
                volume_record_index=index,
            ))
            global_no += 1

    volumes_dir = path / "volumes"
    if volumes_dir.is_dir():
        for child in sorted(volumes_dir.iterdir()):
            stem = child.stem
            if child.is_file() and stem not in seen_volumes:
                issues.append(Issue(
                    "warning", "unexpected_volume",
                    f"发现清单未登记的分卷文件 {child.name}，不会进入发布数据",
                    stem,
                ))

    return LoadedPackage(
        path=str(path),
        package_id=str(package_id),
        package_version="2",
        module=str(module),
        export_interface=manifest.get("export_interface"),
        collected_at=manifest.get("collected_at"),
        field_schema=[item for item in schema if isinstance(item, dict)],
        records=records,
        volume_manifest=volumes,
        issues=issues,
        declared_total=manifest.get("total"),
        source_fingerprint=_fingerprint(path),
    )


def load_package(path: str | Path) -> LoadedPackage:
    """读取一个交付包（v1 单文件或 v2 目录），返回带校验问题的归一化结果。

    结构层面无法读取时抛 ValueError；业务校验问题放在 LoadedPackage.issues。
    """
    package_path = Path(path)
    if not package_path.exists():
        raise FileNotFoundError(f"交付包路径不存在：{package_path}")
    if package_path.is_dir():
        manifest_path = package_path / "package.json"
        if not manifest_path.is_file():
            raise ValueError(f"目录 {package_path} 下没有 package.json，无法识别交付包")
        manifest = _load_json(manifest_path)
        version = str(manifest.get("package_version", "2")) if isinstance(manifest, dict) else "2"
        if version not in SUPPORTED_VERSIONS:
            raise ValueError(f"不支持的交付包版本：{version}")
        return _load_v2(package_path)
    data = _load_json(package_path)
    if isinstance(data, dict) and str(data.get("package_version", "1")) not in SUPPORTED_VERSIONS:
        raise ValueError(f"不支持的交付包版本：{data.get('package_version')}")
    return _load_v1(package_path)


def validate_loaded(loaded: LoadedPackage) -> list[Issue]:
    """对外的校验入口：返回全部问题；调用方用 LoadedPackage.ok 判断是否可发布。"""
    return list(loaded.issues)
