(function () {
  const dataNode = document.querySelector("#batch-data");
  const data = dataNode ? JSON.parse(dataNode.textContent) : {};
  const batchId = data.batch_id || null;
  const result = document.querySelector("#action-result");
  const workerInput = document.querySelector("#worker");

  const say = (value) => {
    if (!result) return;
    result.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  };

  async function call(path, options) {
    const response = await fetch(path, options);
    const text = await response.text();
    let body;
    try { body = JSON.parse(text); } catch (e) { body = text; }
    if (!response.ok) throw body;
    return body;
  }

  const exportLink = document.querySelector("#batch-export");
  if (exportLink) {
    exportLink.href = batchId ? `/api/batches/${batchId}/export.csv` : "#";
  }

  document.querySelector("#batch-select")?.addEventListener("change", (event) => {
    location.href = `/batches/${event.target.value}`;
  });

  document.querySelector("#act-refresh")?.addEventListener("click", () => location.reload());

  document.querySelector("#act-claim")?.addEventListener("click", async () => {
    try {
      const body = await call(`/api/batches/${batchId}/claim`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ worker: workerInput.value }),
      });
      say({
        新发回执: body.issued.map((x) => `${x.receipt_id} ← ${x.key}`),
        原回执复用: body.replayed.map((x) => `${x.receipt_id} ← ${x.key}`),
        他人持有: body.contended.map((x) => `${x.receipt_id} (${x.claimed_by})`),
      });
      setTimeout(() => location.reload(), 800);
    } catch (error) { say(error); }
  });

  document.querySelector("#act-confirm")?.addEventListener("click", async () => {
    try {
      const body = await call(`/api/batches/${batchId}/confirm`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ worker: workerInput.value }),
      });
      say({
        已确认: body.confirmed.map((x) => `${x.receipt_id} ← ${x.key}`),
        重复确认: body.replayed.map((x) => `${x.receipt_id} ← ${x.key}`),
        乱序确认: body.late_confirmed.map((x) => `${x.receipt_id}（仅冲正该回执）`),
      });
      setTimeout(() => location.reload(), 800);
    } catch (error) { say(error); }
  });

  document.querySelector("#act-correct")?.addEventListener("click", async () => {
    let items;
    try { items = JSON.parse(document.querySelector("#correct-items").value); }
    catch (e) { say("新清单不是合法 JSON"); return; }
    try {
      const body = await call(`/api/batches/${batchId}/correct`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({
          items,
          reason: document.querySelector("#correct-reason").value,
        }),
      });
      say({
        修订: body.revision,
        撤销: body.progress.withdrawn,
        替换: body.progress.replaced,
        冲正: body.progress.reversals,
        重放: body.replayed,
      });
      setTimeout(() => location.reload(), 800);
    } catch (error) { say(error); }
  });

  document.querySelector("#act-create")?.addEventListener("click", async () => {
    let items;
    try { items = JSON.parse(document.querySelector("#new-items").value); }
    catch (e) { say("差异项不是合法 JSON"); return; }
    try {
      const body = await call("/api/batches", {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ items }),
      });
      location.href = `/batches/${body.batch_id}`;
    } catch (error) { say(error); }
  });
})();
