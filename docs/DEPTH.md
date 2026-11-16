# 深度登记 对象存储清单与分区现存文件对账

        - 规模：中型
        - 核心规则：路径规范化后按 key、size 与摘要逐项比对，目录标记、空分区和缺失对象不得混为一类。
        - 使用者与工作流：导入随仓样例，逐模块复核，最后导出单一证据清单。

        ## 功能模块

        | 模块 | 使用者动作 | 入口 | 核心文件 | 测试 |
        |---|---|---|---|---|
        | M1 清单采信 | 运行样例并复核证据 | `/api/manifests/import` / `/manifest` | `app/modules/m1_manifest/core.py` `app/modules/m1_manifest/service.py` | `tests/test_m1_manifest.py` |
| M2 路径契约 | 运行样例并复核证据 | `/api/paths/normalize` / `/paths` | `app/modules/m2_paths/core.py` `app/modules/m2_paths/service.py` | `tests/test_m2_paths.py` |
| M3 对象核验 | 运行样例并复核证据 | `/api/objects/verify` / `/objects` | `app/modules/m3_objects/core.py` `app/modules/m3_objects/service.py` | `tests/test_m3_objects.py` |
| M4 分区判定 | 运行样例并复核证据 | `/api/partitions` / `/partitions` | `app/modules/m4_partitions/core.py` `app/modules/m4_partitions/service.py` | `tests/test_m4_partitions.py` |
| M5 差异对账 | 运行样例并复核证据 | `/api/reconcile` / `/reconcile` | `app/modules/m5_reconcile/core.py` `app/modules/m5_reconcile/service.py` | `tests/test_m5_reconcile.py` |
| M6 批次导出 | 运行样例并复核证据 | `/api/export/differences.csv` / `/runs` | `app/modules/m6_runs/core.py` `app/modules/m6_runs/service.py` | `tests/test_m6_runs.py` |
| M6 批次账本 | 领取差异、确认处理、按回执更正、查看进度 | `/api/batches`（`/claim` `/confirm` `/correct` `/recover` `/export.csv`） / `/batches/{id}` | `app/modules/m6_runs/batch.py` `app/modules/m6_runs/service.py` | `tests/test_m6_batches.py` |

        ## 陷阱登记

        | TRAP | 现象 | 朴素实现 | 本项目处理 | 处理位置 | 测试 |
        |---|---|---|---|---|---|
        | TRAP-02 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m1_manifest/core.py` | `tests/test_m1_manifest.py::test_trap_1` |
| TRAP-05 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m2_paths/core.py` | `tests/test_m2_paths.py::test_trap_1` |
| TRAP-08 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m3_objects/core.py` | `tests/test_m3_objects.py::test_trap_1` |
| TRAP-09 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m4_partitions/core.py` | `tests/test_m4_partitions.py::test_trap_1` |
| TRAP-14 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m5_reconcile/core.py` | `tests/test_m5_reconcile.py::test_trap_1` |
| TRAP-16 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m6_runs/core.py` | `tests/test_m6_runs.py::test_trap_1` |
| TRAP-18 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m1_manifest/core.py` | `tests/test_m1_manifest.py::test_trap_2` |
| TRAP-21 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m2_paths/core.py` | `tests/test_m2_paths.py::test_trap_2` |
| TRAP-22 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m3_objects/core.py` | `tests/test_m3_objects.py::test_trap_2` |
| TRAP-23 | 真实边界输入 | 朴素处理会错分 | 保留证据并稳定裁决 | `app/modules/m4_partitions/core.py` | `tests/test_m4_partitions.py::test_trap_2` |
| TRAP-24 | 清单误报后重写差异文件 | 下游重复补数或漏冲正 | 按领取回执更正：未确认项替换、已确认项留凭据并冲正一次 | `app/modules/m6_runs/service.py` | `tests/test_m6_batches.py` |
| TRAP-25 | 更正写一半崩溃 | 重复冲正或半条记录 | 两阶段更正单 + `recover` 续做，冲正编号由回执派生 | `app/modules/m6_runs/service.py` | `tests/test_m6_batches.py::test_correction_crash_is_recoverable_and_retry_adds_no_reversal` |
| TRAP-26 | 旧回执乱序确认 | 影响新批次进度 | 只作用于该回执并补一次乱序冲正 | `app/modules/m6_runs/service.py` | `tests/test_m6_batches.py::test_late_confirmation_of_superseded_receipt_only_reverses_that_receipt` |
| TRAP-27 | 撤销与领取并发 | 回执挂在已撤销项上 | 写锁串行化 + 事件序号可追溯 | `app/store.py` | `tests/test_m6_batches.py::test_claim_and_correction_concurrently_stay_traceable` |

        ## 对账规则

        - 输入记录数 = 有效记录数 + 隔离记录数 + 空记录数。
        - 核心数值或事件数在转换、判定和导出之间保持守恒。
        - 同一输入摘要重跑复用原运行编号，不增加结果行。
        - 批次内：有效项数 = 已撤销 + 已替换 + 当前项；每条已确认且被撤销/替换的回执对应且仅对应一条冲正。
        - 导出只含当前修订的有效回执，且行内容取自领取回执快照。

        ## 已知不覆盖

        - 生产系统连接、身份认证和外部密钥不在演示范围内。
