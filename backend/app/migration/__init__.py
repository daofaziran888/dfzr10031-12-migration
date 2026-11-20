"""版本化导出包迁移流水线。

- packages：存量 v1 清单（module/total/items）与 v2 分卷交付包的读取、校验
- pipeline：摄入、幂等去重、兼容数据集发布、迁移对账与回滚
"""
from app.migration.pipeline import MigrationPipeline, pipeline
from app.migration.packages import (
    Issue,
    LoadedPackage,
    NormalizedRecord,
    load_package,
    validate_loaded,
)

__all__ = [
    "MigrationPipeline",
    "pipeline",
    "Issue",
    "LoadedPackage",
    "NormalizedRecord",
    "load_package",
    "validate_loaded",
]
