# 数据字典

| 字段 | 类型 | 可空 | 定义 |
|---|---|---:|---|
| `run_id` | string | 否 | 输入摘要派生的稳定运行编号 |
| `source_line` | integer | 否 | 原文件物理行号 |
| `reason_code` | string | 是 | 隔离或冲突的稳定原因码 |
| `raw_value` | string | 是 | 未归一的原始证据 |
| `normalized_value` | string | 是 | 按项目规则得到的值 |
| `rule_version` | string | 否 | 计算规则版本 |

## M6 批次账本

| 字段 | 类型 | 可空 | 定义 |
|---|---|---:|---|
| `batch_id` | string | 否 | 规范化差异项摘要派生的稳定批次编号 |
| `revision` | integer | 否 | 批次当前修订；每次有实际变化的更正 +1 |
| `item_key` | string | 否 | 差异项业务键，批次内唯一 |
| `item_generation` | integer | 否 | 同一 key 被替换后的代次；替换产生新代次 |
| `receipt_id` | string | 否 | 由批次、key、代次与序号派生的领取回执编号 |
| `receipt.state` | string | 否 | `claimed` / `confirmed` / `superseded` |
| `receipt.evidence` | string | 是 | 领取瞬间的项内容快照，导出以此为准 |
| `reversal_id` | string | 否 | 由回执编号派生的冲正编号，一条回执至多一条 |
| `reversal.reason` | string | 否 | `WITHDRAW_CONFIRMED_ITEM` / `LATE_CONFIRMATION` / `REVOKED_BEFORE_CONFIRMATION` |
| `correction.state` | string | 否 | `pending` / `applied` / `stale`；pending 可由 recover 续做 |
| `event.seq` | integer | 否 | 批次事件自增序号，撤销与领取的先后以它为准 |

字符串编号逐字保留。金额与计量值用 `Decimal`。时间值带偏移并以 UTC 参与比较。区间统一采用左闭右开。
