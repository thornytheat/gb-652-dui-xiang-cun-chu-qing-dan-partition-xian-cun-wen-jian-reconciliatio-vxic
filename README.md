# 对象存储清单与分区现存文件对账

        面向一线复核人员的单服务数据工作台。样例数据随仓库提供，结论保留行号、原因码和原值。

        ## 业务模块

        - M1 清单采信：`/api/manifests/import`
- M2 路径契约：`/api/paths/normalize`
- M3 对象核验：`/api/objects/verify`
- M4 分区判定：`/api/partitions`
- M5 差异对账：`/api/reconcile`
- M6 批次导出：`/api/export/differences.csv`
- M6 批次账本：`/api/batches`（建批次、领取回执、确认、更正、导出）

        ## 核心规则

        路径规范化后按 key、size 与摘要逐项比对，目录标记、空分区和缺失对象不得混为一类。

        ## 批次更正

        差异已导出并补数后，若发现原清单误报，**不要重写差异文件**，而是在原批次上
        提交新清单（`POST /api/batches/{id}/correct`）。更正按领取回执逐项决定：

        - 未领取 / 已领取未确认：回执作废，该项可撤销或替换；
        - 已确认：保留原回执编号与内容，生成且只生成一次冲正；
        - 更正后才到达的旧回执确认：只作用于该回执并补一次冲正，不影响新修订；
        - 同一新清单重复提交是重放，不会重复冲正；写到一半失败可用
          `POST /api/batches/{id}/recover` 续做；
        - 导出按当前修订的有效回执生成，行内容与领取回执一致。

        批次页 `/runs`（或 `/batches/{batch_id}`）可领取差异、确认处理并查看更正进度。

        ## 启动

        ```bash
        docker compose up --build
        ```

        浏览器访问 `http://localhost:18211`。运行测试：

        ```bash
        docker compose exec -T app pytest -q
        ```

        ## 样例流程

        依次运行六个模块；最后在批次页复核输入、有效、隔离与输出合计。同一输入摘要重复执行时复用原 `run_id`。

        ## 已知不覆盖

        不处理加密文件与需要外部密钥的对象；不连接真实生产存储。
