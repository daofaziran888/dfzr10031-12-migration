"""迁移流水线接口：摄入版本化导出包、查询交付与对账、撤回与回滚。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.migration.pipeline import pipeline

router = APIRouter(prefix="/api/migration", tags=["迁移流水线"])


class IngestPayload(BaseModel):
    path: str = Field(..., description="交付包路径：v1 为 JSON 文件，v2 为含 package.json 的目录")


@router.post("/ingest")
def ingest_package(payload: IngestPayload) -> dict[str, Any]:
    """校验并摄入一个交付包；坏包只留校验记录，不会混入已发布数据。"""
    try:
        return pipeline.ingest(payload.path)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/deliveries")
def list_deliveries() -> dict[str, Any]:
    """交付历史：同源包重复迁移只确认一次，duplicate 记录保留确认痕迹。"""
    items = pipeline.list_deliveries()
    return {"items": items, "total": len(items)}


@router.get("/deliveries/{package_id}")
def get_delivery(package_id: str) -> dict[str, Any]:
    try:
        return pipeline.get_delivery(package_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"交付包 {package_id} 不存在") from None


@router.post("/deliveries/{package_id}/reconcile")
def rerun_reconciliation(package_id: str) -> dict[str, Any]:
    """重跑对账：从留存原包重新校验，报告里始终带原包编号。"""
    try:
        return pipeline.rerun_reconciliation(package_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"交付包 {package_id} 不存在") from None
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/reconciliations")
def list_reconciliations(package_id: str | None = Query(default=None)) -> dict[str, Any]:
    items = pipeline.list_reconciliations(package_id)
    return {"items": items, "total": len(items)}


@router.post("/deliveries/{package_id}/withdraw")
def withdraw_delivery(package_id: str) -> dict[str, Any]:
    """撤回已发布包，移除该来源的全部兼容数据集记录。"""
    try:
        return pipeline.withdraw(package_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"交付包 {package_id} 不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/rollback")
def rollback_last() -> dict[str, Any]:
    """构建兼容数据集失败时，回滚到最近一次可复验交付（发布前快照）。"""
    try:
        return pipeline.rollback_last()
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/dataset")
def query_dataset(
    package_id: str | None = Query(default=None, description="按来源包编号过滤"),
    keyword: str | None = Query(default=None, description="按记录内容模糊检索"),
    page: int = 1,
    size: int = 20,
) -> dict[str, Any]:
    """查兼容数据集：存量 v1 与新版 v2 记录用同一口径读取。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    return pipeline.query_dataset(package_id=package_id, keyword=keyword, page=page, size=size)


@router.get("/scope")
def dataset_scope() -> dict[str, Any]:
    """当期交付数据范围：按源包列业务编号，并标出跨包同号记录。"""
    return pipeline.dataset_scope()
