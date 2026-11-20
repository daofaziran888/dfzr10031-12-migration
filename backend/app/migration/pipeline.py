"""迁移流水线：摄入版本化导出包，发布兼容数据集，产出可复验的迁移对账。

关键口径（对应交付要求）：
- 同源包幂等：以 package_id 为幂等键，重复迁移同一源包只确认一次，直接回原交付；
- 内容防重放：package_id 相同但源包指纹变化时拒绝，避免拿同号包覆盖已发布数据；
- 不合并：同一业务编号在不同导出接口/不同源包里是各自独立的记录，
  以 (package_id, source_volume, record_no) 唯一标识，绝不按业务键合并成一行；
- 坏包阻断：校验含 error 的包只留校验记录，不进入已发布兼容数据集；
- 可回滚：每次成功发布前留存上一版快照，失败或人工撤回可回到最近一次可复验交付；
- 对账可重跑：对账报告持久化，重跑时原包编号、问题类型、行数都可回查。
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.migration.packages import LoadedPackage, load_package

STATE_DIR = Path(
    os.environ.get("MIGRATION_STATE_DIR")
    or (Path(__file__).resolve().parents[2] / "var" / "migration")
)
PACKAGES_DIR = STATE_DIR / "packages"
SNAPSHOTS_DIR = STATE_DIR / "snapshots"
STATE_FILE = STATE_DIR / "pipeline_state.json"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass
class Delivery:
    """一次摄入确认的结果。"""

    package_id: str
    package_version: str
    module: str
    export_interface: str | None
    collected_at: str | None
    source_path: str
    source_fingerprint: str
    received_at: str
    status: str                       # published（已发布）/ rejected（坏包阻断）/ duplicate（幂等命中）
    record_count: int
    volume_count: int
    issue_summary: dict[str, int]
    issues: list[dict[str, Any]]
    reconciliation_id: str | None = None
    duplicated_of: str | None = None  # 幂等命中时指向首次交付编号
    snapshot_id: str | None = None


@dataclass
class Reconciliation:
    """迁移对账报告：行数、问题类型、数据范围都可复验。"""

    reconciliation_id: str
    package_id: str
    module: str
    generated_at: str
    declared_total: int | None
    accepted_rows: int
    volume_rows: dict[str, int]
    issue_summary: dict[str, int]
    issues: list[dict[str, Any]]
    data_scope: dict[str, Any]        # 当期交付的数据范围：来源接口、采集截面、业务编号集合


@dataclass
class PipelineState:
    deliveries: list[dict[str, Any]] = field(default_factory=list)
    reconciliations: list[dict[str, Any]] = field(default_factory=list)
    published: list[dict[str, Any]] = field(default_factory=list)
    rollbacks: list[dict[str, Any]] = field(default_factory=list)
    seq: int = 0


class MigrationPipeline:
    """状态全部落在 var/migration 下，重启后历史交付与对账仍可复验。"""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self.state_dir = Path(base_dir) if base_dir else STATE_DIR
        self.packages_dir = self.state_dir / "packages"
        self.snapshots_dir = self.state_dir / "snapshots"
        self.state_file = self.state_dir / "pipeline_state.json"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.packages_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()

    # ---------- 持久化 ----------
    def _load_state(self) -> PipelineState:
        if self.state_file.is_file():
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            return PipelineState(**data)
        return PipelineState()

    def _save_state(self) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(asdict(self.state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.state_file)

    def _next_seq(self) -> int:
        self.state.seq += 1
        return self.state.seq

    # ---------- 摄入与发布 ----------
    def ingest(self, path: str) -> dict[str, Any]:
        """摄入一个交付包：校验 -> 幂等判断 -> 发布兼容数据集 / 阻断坏包。"""
        loaded = load_package(path)

        prior = self._find_confirmed_delivery(loaded.package_id)
        if prior is not None:
            if prior["source_fingerprint"] != loaded.source_fingerprint:
                return self._reject_fingerprint(loaded, prior)
            # 已确认交付的同源包重复迁移：只确认一次，回放首次交付，不新增数据集记录。
            duplicate = {
                "package_id": loaded.package_id,
                "package_version": loaded.package_version,
                "module": loaded.module,
                "export_interface": loaded.export_interface,
                "collected_at": loaded.collected_at,
                "source_path": loaded.path,
                "source_fingerprint": loaded.source_fingerprint,
                "received_at": _now(),
                "status": "duplicate",
                "record_count": prior["record_count"],
                "volume_count": prior["volume_count"],
                "issue_summary": prior["issue_summary"],
                "issues": prior["issues"],
                "reconciliation_id": prior.get("reconciliation_id"),
                "duplicated_of": prior["package_id"],
                "snapshot_id": prior.get("snapshot_id"),
            }
            self.state.deliveries.append(duplicate)
            self._save_state()
            return {"ok": True, "status": "duplicate", "delivery": duplicate,
                    "message": "同源交付包已迁移过，本次仅确认，未重复发布"}

        stored_path = self._store_package(loaded)
        if not loaded.ok:
            delivery = self._record_delivery(loaded, stored_path, status="rejected")
            return {"ok": False, "status": "rejected", "delivery": delivery,
                    "message": "分卷校验未通过，坏分卷未混入已发布数据"}

        snapshot_id = self._snapshot_published()
        published_records = self._publish(loaded)
        delivery = self._record_delivery(
            loaded, stored_path, status="published",
            snapshot_id=snapshot_id,
        )
        reconciliation = self._build_reconciliation(loaded, delivery["package_id"])
        delivery["reconciliation_id"] = reconciliation.reconciliation_id
        self.state.reconciliations.append(asdict(reconciliation))
        self._save_state()
        return {"ok": True, "status": "published", "delivery": delivery,
                "reconciliation": asdict(reconciliation),
                "message": f"已发布兼容数据集 {len(published_records)} 行"}

    def _reject_fingerprint(self, loaded: LoadedPackage, prior: dict[str, Any]) -> dict[str, Any]:
        delivery = {
            "package_id": loaded.package_id,
            "package_version": loaded.package_version,
            "module": loaded.module,
            "export_interface": loaded.export_interface,
            "collected_at": loaded.collected_at,
            "source_path": loaded.path,
            "source_fingerprint": loaded.source_fingerprint,
            "received_at": _now(),
            "status": "rejected",
            "record_count": 0,
            "volume_count": len(loaded.volume_manifest),
            "issue_summary": {"content_changed": 1},
            "issues": [{
                "level": "error",
                "code": "content_changed",
                "message": (
                    f"package_id={loaded.package_id} 已发布过且源指纹不同，"
                    "拒绝用同号内容覆盖已发布数据"
                ),
                "volume_id": None,
                "record_index": None,
                "field_name": None,
            }],
            "reconciliation_id": None,
            "duplicated_of": prior["package_id"],
            "snapshot_id": None,
        }
        self.state.deliveries.append(delivery)
        self._save_state()
        return {"ok": False, "status": "rejected", "delivery": delivery,
                "message": "同号包内容与首次交付不一致，已拒绝"}

    def _store_package(self, loaded: LoadedPackage) -> str:
        source = Path(loaded.path)
        if source.is_dir():
            # v2 目录包：整目录留存，重跑对账时 package.json 仍可识别版本。
            target = self.packages_dir / loaded.package_id
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)
            return str(target)
        # v1 单文件：按 package_id 改名留存为文件，重跑时按 v1 清单读取。
        target = self.packages_dir / f"{loaded.package_id}{source.suffix}"
        shutil.copy2(source, target)
        return str(target)

    def _snapshot_published(self) -> str:
        """发布前留存当前已发布数据集，供回滚到最近一次可复验交付。"""
        snapshot_id = f"SNAP-{self._next_seq():04d}"
        payload = {
            "snapshot_id": snapshot_id,
            "created_at": _now(),
            "published": json.loads(json.dumps(self.state.published, ensure_ascii=False)),
        }
        (self.snapshots_dir / f"{snapshot_id}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        return snapshot_id

    def _publish(self, loaded: LoadedPackage) -> list[dict[str, Any]]:
        """把归一化记录追加进兼容数据集；来源不同的同号行各自独立，不合并。"""
        new_rows: list[dict[str, Any]] = []
        for record in loaded.records:
            row = dict(record.values)
            row["__provenance__"] = {
                "package_id": loaded.package_id,
                "package_version": loaded.package_version,
                "export_interface": loaded.export_interface,
                "collected_at": loaded.collected_at,
                "source_volume": record.source_volume,
                "record_no": record.record_no,
                "volume_record_index": record.volume_record_index,
                "missing_fields": record.missing_fields,
                "explicit_null_fields": record.explicit_null_fields,
            }
            new_rows.append(row)
        self.state.published.extend(new_rows)
        return new_rows

    def _record_delivery(
        self,
        loaded: LoadedPackage,
        stored_path: str,
        *,
        status: str,
        snapshot_id: str | None = None,
    ) -> dict[str, Any]:
        delivery = Delivery(
            package_id=loaded.package_id,
            package_version=loaded.package_version,
            module=loaded.module,
            export_interface=loaded.export_interface,
            collected_at=loaded.collected_at,
            source_path=stored_path,
            source_fingerprint=loaded.source_fingerprint,
            received_at=_now(),
            status=status,
            record_count=len(loaded.records),
            volume_count=len(loaded.volume_manifest),
            issue_summary=loaded.issue_summary(),
            issues=loaded.issue_dicts(),
            snapshot_id=snapshot_id,
        )
        data = asdict(delivery)
        self.state.deliveries.append(data)
        self._save_state()
        return data

    # ---------- 对账 ----------
    def _build_reconciliation(self, loaded: LoadedPackage, package_id: str) -> Reconciliation:
        volume_rows: dict[str, int] = {}
        business_keys: list[str] = []
        for record in loaded.records:
            key = record.source_volume or "__legacy__"
            volume_rows[key] = volume_rows.get(key, 0) + 1
            business_key = self._business_key(record.values)
            if business_key is not None:
                business_keys.append(business_key)
        declared_total = loaded.declared_total
        if declared_total is None:
            declared_total = sum(volume_rows.values())
        scope = {
            "package_id": package_id,
            "module": loaded.module,
            "export_interface": loaded.export_interface,
            "collected_at": loaded.collected_at,
            "business_keys": business_keys,
            "business_key_count": len(business_keys),
        }
        return Reconciliation(
            reconciliation_id=f"RECON-{self._next_seq():04d}",
            package_id=package_id,
            module=loaded.module,
            generated_at=_now(),
            declared_total=declared_total,
            accepted_rows=len(loaded.records),
            volume_rows=volume_rows,
            issue_summary=loaded.issue_summary(),
            issues=loaded.issue_dicts(),
            data_scope=scope,
        )

    def rerun_reconciliation(self, package_id: str) -> dict[str, Any]:
        """重复跑对账：从留存的原包重新校验并重建报告，原包编号始终可回查。"""
        delivery = self._find_delivery(package_id)
        if delivery is None:
            raise KeyError(f"找不到 package_id={package_id} 的交付记录")
        stored = Path(delivery["source_path"])
        if not stored.exists():
            raise FileNotFoundError(f"原包留存目录已丢失：{stored}")
        loaded = load_package(stored)
        report = self._build_reconciliation(loaded, package_id)
        report_dict = asdict(report)
        self.state.reconciliations.append(report_dict)
        self._save_state()
        return report_dict

    # ---------- 撤回与回滚 ----------
    def withdraw(self, package_id: str) -> dict[str, Any]:
        """撤回已发布包：移除该来源的记录，数据集回到该包摄入前的可复验状态。"""
        delivery = self._find_delivery(package_id)
        if delivery is None:
            raise KeyError(f"找不到 package_id={package_id} 的交付记录")
        if delivery["status"] != "published":
            raise ValueError(f"交付状态为 {delivery['status']}，无可撤回的已发布数据")
        before = len(self.state.published)
        self.state.published = [
            row for row in self.state.published
            if row.get("__provenance__", {}).get("package_id") != package_id
        ]
        removed = before - len(self.state.published)
        delivery["status"] = "withdrawn"
        rollback = {
            "rollback_id": f"RB-{self._next_seq():04d}",
            "package_id": package_id,
            "kind": "withdraw",
            "created_at": _now(),
            "removed_rows": removed,
        }
        self.state.rollbacks.append(rollback)
        self._save_state()
        return {"ok": True, **rollback, "message": f"已撤回 {removed} 行，兼容数据集已更新"}

    def rollback_last(self) -> dict[str, Any]:
        """回滚到最近一次可复验交付：恢复最近一个发布前快照。"""
        snapshots = sorted(self.snapshots_dir.glob("SNAP-*.json"))
        if not snapshots:
            raise ValueError("没有可回滚的发布前快照")
        latest = snapshots[-1]
        payload = json.loads(latest.read_text(encoding="utf-8"))
        self.state.published = payload["published"]
        rollback = {
            "rollback_id": f"RB-{self._next_seq():04d}",
            "package_id": None,
            "kind": "restore_snapshot",
            "created_at": _now(),
            "snapshot_id": payload["snapshot_id"],
            "restored_rows": len(payload["published"]),
        }
        self.state.rollbacks.append(rollback)
        self._save_state()
        return {"ok": True, **rollback,
                "message": f"已恢复快照 {payload['snapshot_id']}，兼容数据集 {len(payload['published'])} 行"}

    # ---------- 查询 ----------
    def _find_delivery(self, package_id: str) -> dict[str, Any] | None:
        """查交付记录：优先已确认（published/withdrawn）的，其次最近的拒绝记录。

        同号包内容变更产生的 rejected 记录不能盖掉首次发布事实，否则撤回、
        对账等动作会找错对象。
        """
        fallback: dict[str, Any] | None = None
        for delivery in reversed(self.state.deliveries):
            if delivery["package_id"] != package_id or delivery["status"] == "duplicate":
                continue
            if delivery["status"] in ("published", "withdrawn"):
                return delivery
            fallback = fallback or delivery
        return fallback

    def _find_confirmed_delivery(self, package_id: str) -> dict[str, Any] | None:
        """已确认交付（published/withdrawn）才算迁移成功，rejected 不占幂等键。"""
        for delivery in reversed(self.state.deliveries):
            if (
                delivery["package_id"] == package_id
                and delivery["status"] in ("published", "withdrawn")
            ):
                return delivery
        return None

    def list_deliveries(self) -> list[dict[str, Any]]:
        return list(self.state.deliveries)

    def get_delivery(self, package_id: str) -> dict[str, Any]:
        delivery = self._find_delivery(package_id)
        if delivery is None:
            raise KeyError(package_id)
        return delivery

    def list_reconciliations(self, package_id: str | None = None) -> list[dict[str, Any]]:
        if package_id is None:
            return list(self.state.reconciliations)
        return [item for item in self.state.reconciliations if item["package_id"] == package_id]

    def query_dataset(
        self,
        *,
        package_id: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> dict[str, Any]:
        """查兼容数据集：可按来源包过滤，验证同号行不合并、旧包新包都能读。"""
        rows = self.state.published
        if package_id:
            rows = [r for r in rows if r.get("__provenance__", {}).get("package_id") == package_id]
        if keyword:
            rows = [r for r in rows if keyword in json.dumps(r, ensure_ascii=False)]
        total = len(rows)
        start = max(page - 1, 0) * size
        return {
            "items": rows[start:start + size],
            "total": total,
            "page": page,
            "size": size,
        }

    def dataset_scope(self) -> dict[str, Any]:
        """当期已发布数据范围：按源包列出业务编号，便于跨包同号记录明确对账。"""
        scopes: dict[str, dict[str, Any]] = {}
        for row in self.state.published:
            prov = row["__provenance__"]
            pkg = prov["package_id"]
            scope = scopes.setdefault(pkg, {
                "package_id": pkg,
                "package_version": prov["package_version"],
                "export_interface": prov["export_interface"],
                "collected_at": prov["collected_at"],
                "row_count": 0,
                "business_keys": [],
            })
            scope["row_count"] += 1
            key = self._business_key(row)
            if key is not None:
                scope["business_keys"].append(key)
        duplicate_keys = self._duplicate_business_keys()
        return {
            "packages": list(scopes.values()),
            "total_rows": len(self.state.published),
            "shared_business_keys": duplicate_keys,
        }

    @staticmethod
    def _business_key(row: dict[str, Any]) -> str | None:
        """取记录的业务编号；不同导出接口同号即命中（如两份存量包的 id=1）。"""
        for candidate in ("id", "任务编号", "服务编号", "记录编号"):
            if candidate in row and row[candidate] is not None:
                return f"{candidate}={row[candidate]}"
        return None
    def _duplicate_business_keys(self) -> dict[str, list[str]]:
        seen: dict[str, set[str]] = {}
        for row in self.state.published:
            prov = row["__provenance__"]
            key = self._business_key(row)
            if key is not None:
                seen.setdefault(key, set()).add(prov["package_id"])
        return {key: sorted(pkgs) for key, pkgs in seen.items() if len(pkgs) > 1}

    def reset(self) -> None:
        """测试辅助：清空全部流水线状态。"""
        shutil.rmtree(self.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.packages_dir.mkdir(parents=True, exist_ok=True)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.state = PipelineState()
        self._save_state()


pipeline = MigrationPipeline()
