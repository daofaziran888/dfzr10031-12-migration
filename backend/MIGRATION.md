# 版本化导出包迁移流水线

数据导出接口继续提供存量 `{module, total, items}` JSON 清单；迁移交付包在保留该契约的
前提下新增字段类型、采集截面和可校验的分卷清单，数据 API 发布在迁移流水线中消费版本化
导出包，产出可查的兼容数据集和带行数、问题类型的迁移对账。

## 1. 包格式

### v1（存量，接口契约不变）

单文件 JSON，字段只有 `module` / `total` / `items`，读取与归一化逻辑在
`app/migration/packages.py::_load_v1`：

```json
{"module": "export", "total": 3, "items": [{"id": 1, "任务编号": "EXPO-0001"}]}
```

- v1 没有字段类型清单，按并到的键生成宽松 schema，不产生类型校验问题；
- 记录里缺失的键与显式 `null` 分别记录在 `missing_fields` / `explicit_null_fields`；
- `total` 与 `items` 实际数量不符时报 `total_mismatch`（error）。

### v2（新版本化分卷交付包）

目录形式，清单与分卷分离：

```text
PKG-V2-DEMO-001/
├── package.json            # 清单
└── volumes/
    ├── part-001.json       # 分卷：{"volume_id": "part-001", "items": [...]}
    └── part-002.json
```

`package.json` 关键字段：

| 字段 | 说明 |
| --- | --- |
| `package_version` | 固定 `"2"`；读取器只接受受支持版本 |
| `package_id` | 源包幂等键，重复迁移同源包只确认一次 |
| `module` / `export_interface` / `collected_at` | 模块、来源导出接口、采集截面 |
| `fields[]` | 字段类型清单：`name`、`type`（string/integer/number/boolean/object/array）、`nullable` |
| `volumes[]` | 分卷清单：`volume_id`、`file`、`row_count`、`sha256`（按分卷文件字节计算） |

## 2. 校验口径（坏分卷不得混入已发布数据）

任一分卷出现 error 级问题，整包判定为坏包，流水线只留校验记录、不发布任何一行：

| 问题编码 | 级别 | 触发条件 |
| --- | --- | --- |
| `checksum_mismatch` | error | 分卷文件 sha256 与清单不一致 |
| `row_count_mismatch` | error | 分卷实际行数与清单 `row_count` 不一致 |
| `field_type_mismatch` | error | 字段值与声明类型不符（显式 null 单独处理；bool 不算 integer/number） |
| `missing_field` | error | 记录缺少清单要求的键——与显式 null 严格区分 |
| `volume_missing` / `volume_unparsable` | error | 分卷文件缺失、JSON 损坏或结构不对 |
| `manifest_malformed` / `unsupported_version` | error | 清单结构或版本不支持 |
| `total_mismatch` | error | v1 的 total 与 items 数量不符 |
| `extra_field` / `unexpected_volume` | warning | 未声明字段/分卷：原样保留，不阻断发布 |

## 3. 流水线语义（`app/migration/pipeline.py`）

- **幂等确认**：以 `package_id` 为幂等键，同源包（指纹一致）重复摄入返回 `duplicate`，
  不重复发布；`package_id` 相同但内容指纹变化时以 `content_changed` 拒绝，防止同号覆盖。
  rejected 不占用幂等键，坏包修好后允许重新交付。
- **不合并**：每行以 `(package_id, source_volume, record_no)` 溯源，来源不同的同号行
  （如两份存量包的 `id=1`）各自独立保留；`GET /api/migration/scope` 用
  `shared_business_keys` 明确标出跨包同号记录，数据范围按包可对账。
- **撤回**：`withdraw` 按 `__provenance__.package_id` 精确移除该来源的行。
- **回滚**：每次发布前写发布前快照（`var/migration/snapshots/`），构建兼容数据集失败时
  可恢复到最近一次可复验交付。
- **对账可重跑**：原包留存于 `var/migration/packages/`，重跑从原包重新校验，报告始终
  带原包编号、分卷行数、问题类型与当期数据范围（业务编号集合）。
- **持久化**：状态写 `var/migration/pipeline_state.json`，服务重启后交付历史与对账
  仍可复验；目录可用环境变量 `MIGRATION_STATE_DIR` 覆盖。

兼容数据集每行带溯源元数据 `__provenance__`（来源包、版本、接口、采集截面、分卷、
缺失字段、显式 null 字段），业务字段本身不改写，存量 v1 与新版 v2 同口径读取。

## 4. HTTP 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/migration/ingest` | 校验并摄入交付包（body：`{"path": "..."}`） |
| GET | `/api/migration/deliveries` | 交付历史（published/rejected/duplicate/withdrawn） |
| GET | `/api/migration/deliveries/{package_id}` | 单包交付明细 |
| POST | `/api/migration/deliveries/{package_id}/reconcile` | 重跑对账（原包编号可回查） |
| GET | `/api/migration/reconciliations?package_id=` | 历史对账报告 |
| POST | `/api/migration/deliveries/{package_id}/withdraw` | 撤回已发布包 |
| POST | `/api/migration/rollback` | 回滚到最近一次可复验交付 |
| GET | `/api/migration/dataset` | 兼容数据集查询（可按来源包过滤） |
| GET | `/api/migration/scope` | 当期数据范围与跨包同号记录 |
| GET | `/api/data_service/published_dataset` | 数据 API 发布侧消费兼容数据集 |
| GET | `/api/data_service/migration_overview` | 迁移对账概况 |

## 5. 复现步骤

```bash
cd backend
# 1. 取样：从两个原导出接口拉存量 JSON 清单（需先启动后端）
python3 -I scripts/sample_export.py --base-url http://127.0.0.1:8000 --out-dir ../samples/raw
# 2. 手动补包：一份合规 v2 包、一份坏分卷包
python3 -I scripts/build_v2_package.py --out-dir ../samples/packages
python3 -I scripts/build_bad_volume.py --out-dir ../samples/packages
# 3. 通过数据 API 做端到端验证（读取、撤回、历史兼容、回滚共 23 项断言）
python3 -I scripts/e2e_verify.py --base-url http://127.0.0.1:8000 \
    --raw-dir ../samples/raw --pkg-dir ../samples/packages
```

脚本只依赖标准库并以 `python3 -I` 运行；样本目录与流水线状态目录相互独立，
脚本不会从样本所在目录加载任何代码。
