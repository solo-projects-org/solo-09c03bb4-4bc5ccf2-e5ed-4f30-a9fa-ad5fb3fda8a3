const PRESETS = {
  skew: `SELECT m.merchant_id,
       m.merchant_name,
       COUNT(*)      AS order_cnt,
       SUM(o.amount) AS total_amount,
       SUM(o.fee)    AS total_fee
FROM settlement_orders o
JOIN merchants m ON m.merchant_id = o.merchant_id
WHERE left(m.category, 1) = 'A'
GROUP BY m.merchant_id, m.merchant_name
ORDER BY total_amount DESC`,
  single: `SELECT o.order_no, o.amount, o.fee, o.settle_date, m.merchant_name
FROM settlement_orders o
JOIN merchants m ON m.merchant_id = o.merchant_id
WHERE m.merchant_id = 42
  AND o.settle_date >= DATE '2026-11-01'
  AND o.settle_date <  DATE '2026-11-14'
ORDER BY o.settle_date DESC, o.order_no`,
  daily: `SELECT settle_date, COUNT(*) AS order_cnt, SUM(amount) AS total_amount
FROM settlement_orders
WHERE settle_date >= DATE '2026-11-01'
  AND settle_date <  DATE '2026-11-14'
GROUP BY settle_date
ORDER BY settle_date`,
};

const sqlInput = document.getElementById('sql-input');
sqlInput.value = PRESETS.skew;
document.getElementById('preset').onchange = (e) => {
  sqlInput.value = PRESETS[e.target.value] || '';
};

// 样表数据覆盖 2026-08-01 ~ 2026-11-13，默认选中有数据的最后一天
const verifyDateInput = document.getElementById('verify-date');
const insertDateInput = document.getElementById('insert-date');
const insertOrderNoInput = document.getElementById('insert-orderno');
verifyDateInput.value = '2026-11-13';
insertDateInput.value = verifyDateInput.value;

const expanded = new Set();   // 展开了计划详情的批次 id

const STATUS_TEXT = {
  pending: '排队中', running: '运行中', completed: '已完成',
  failed: '失败', timeout: '超时', cancelled: '已取消',
};
const SETTING_TEXT = {
  default: '默认 (enable_nestloop=on)',
  nestloop_off: '抑制嵌套循环 (enable_nestloop=off)',
};
const EX_STATUS_TEXT = {
  pending: '待执行', running: '执行中', ok: '成功',
  timeout: '超时', cancelled: '已取消', error: '报错',
};
const VERDICT_TEXT = {
  improved: '有改善', not_improved: '无改善', inconclusive: '无法判断改善',
};

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}
function fmtMs(v) { return v == null ? '—' : Number(v).toFixed(1) + ' ms'; }
function fmtTime(ts) { return ts ? new Date(ts * 1000).toLocaleString() : '—'; }

/* ---------------- 关账核验 ---------------- */

function renderVerifyState(s) {
  const box = document.getElementById('verify-result');
  let html = '';
  if (s.last_error) {
    html += `<div class="conclusion none verify-error">
      <b>最近一次核验失败</b>（${fmtTime(s.last_error.at)}）：${escapeHtml(s.last_error.message)}<br>
      已保留此前成功结果及其实际有效性，请排查后重新核验。
    </div>`;
  }
  if (!s.result) {
    box.innerHTML = html || '<p class="hint">该结算日尚未核验。</p>';
    return;
  }
  const r = s.result;
  if (s.trusted) {
    html += `<div class="conclusion improved">
      <b>可信，可用于下游汇总</b>：可信订单数 <b>${r.order_cnt}</b> 笔，
      金额合计 <b>${escapeHtml(r.total_amount)}</b> 元
    </div>`;
  } else if (r.duplicate_groups > 0) {
    const rows = (r.duplicates || []).map(d =>
      `<tr><td><code>${escapeHtml(d.order_no)}</code></td><td>${d.cnt}</td></tr>`).join('');
    html += `<div class="conclusion inconclusive">
      <b>存在重号冲突，不可用于下游汇总</b>：${r.duplicate_groups} 个订单号重复，
      涉及 ${r.duplicate_rows} 行
    </div>
    <table class="ex-table">
      <thead><tr><th>冲突订单号</th><th>出现次数</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    ${r.duplicates_truncated ? `<p class="hint">重号过多，仅展示前 ${(r.duplicates || []).length} 组</p>` : ''}`;
    if (s.stale) {
      html += `<div class="conclusion none">核验后该日订单又有补记/变更，以上冲突清单可能已过时，请重新核验。</div>`;
    }
  } else if (s.stale) {
    html += `<div class="conclusion none">
      <b>结果已失效，不得作为可信合计</b>：核验后该日订单有补记/变更
      （快照标记 ${r.snapshot_marker} → 当前标记 ${s.current_marker}）。<br>
      旧结果（仅供参考，不可用于下游汇总）：订单数 ${r.order_cnt} 笔，
      金额合计 ${escapeHtml(r.total_amount)} 元。请重新核验。
    </div>`;
  }
  html += `<p class="hint">核验于 ${fmtTime(r.finished_at)} · 耗时 ${fmtMs(r.duration_ms)}
    · 快照标记 ${r.snapshot_marker} · 当前标记 ${s.current_marker}</p>`;
  box.innerHTML = html;
}

async function refreshVerification() {
  const d = verifyDateInput.value;
  if (!d) return;
  try {
    const res = await fetch(`/api/verifications/${d}`);
    if (!res.ok) return;
    renderVerifyState(await res.json());
  } catch (e) { /* 忽略瞬时网络错误，下一轮再试 */ }
}

verifyDateInput.onchange = () => {
  if (!insertDateInput.value) insertDateInput.value = verifyDateInput.value;
  refreshVerification();
};

document.getElementById('verify-btn').onclick = async () => {
  const msg = document.getElementById('verify-msg');
  const d = verifyDateInput.value;
  msg.className = 'msg';
  if (!d) {
    msg.textContent = '请先选择结算日';
    msg.classList.add('err');
    return;
  }
  msg.textContent = '核验中…';
  try {
    const res = await fetch('/api/verifications', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ settle_date: d }),
    });
    if (res.status === 409) {
      msg.textContent = '该结算日正在核验中，请稍候';
      msg.classList.add('warn');
      return;
    }
    if (!res.ok) {
      msg.textContent = '核验请求失败（HTTP ' + res.status + '）';
      msg.classList.add('err');
      return;
    }
    renderVerifyState(await res.json());
    msg.textContent = '核验完成';
    msg.classList.add('ok');
  } catch (e) {
    msg.textContent = '网络错误：' + e;
    msg.classList.add('err');
  }
};

/* ---------------- 对照批次 ---------------- */

async function refresh() {
  try {
    const res = await fetch('/api/batches');
    const data = await res.json();
    renderSlot(data);
    await renderBatches(data.batches);
  } catch (e) { /* 忽略瞬时网络错误，下一轮再试 */ }
}

function renderSlot(data) {
  const pill = document.getElementById('slot-pill');
  if (data.busy) {
    pill.textContent = '运行中（名额已占用）';
    pill.className = 'pill busy';
  } else {
    pill.textContent = '空闲，可提交对照';
    pill.className = 'pill idle';
  }
}

function conclusionHtml(b) {
  if (b.conclusion) {
    const v = b.conclusion.verdict;
    return `<div class="conclusion ${v}">
      <b>${VERDICT_TEXT[v] || v}</b> · ${escapeHtml(b.conclusion.message)}
    </div>`;
  }
  if (['failed', 'timeout', 'cancelled'].includes(b.status)) {
    const reason = { failed: '报错', timeout: '超时', cancelled: '已取消' }[b.status];
    return `<div class="conclusion none">无比较结论（批次${reason}）</div>`;
  }
  return '';
}

function executionsHtml(b) {
  if (!b.executions.length) {
    return b.status === 'running' ? '<p class="hint">正在建立事务快照…</p>' : '';
  }
  const rows = b.executions.map(ex => `<tr>
    <td>第 ${ex.round} 轮</td>
    <td>#${ex.seq}</td>
    <td>${SETTING_TEXT[ex.setting] || ex.setting}</td>
    <td><span class="ex-status ${ex.status}">${EX_STATUS_TEXT[ex.status] || ex.status}</span></td>
    <td>${fmtMs(ex.execution_time_ms)}</td>
    <td>${fmtMs(ex.planning_time_ms)}</td>
    <td>${ex.error ? escapeHtml(ex.error) : ''}</td>
  </tr>`).join('');
  return `<table class="ex-table">
    <thead><tr><th>轮次</th><th>顺序</th><th>设置</th><th>状态</th>
    <th>执行耗时</th><th>规划耗时</th><th>备注</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function batchCardHtml(b) {
  const cancelBtn = (b.status === 'running' || b.status === 'pending')
    ? (b.cancel_requested
        ? '<span class="msg warn">取消中…</span>'
        : `<button class="btn-cancel" data-cancel="${b.id}">取消本批</button>`)
    : '';
  const toggleBtn = b.executions.length
    ? `<button class="btn-toggle" data-toggle="${b.id}">${expanded.has(b.id) ? '收起计划' : '查看计划与结构'}</button>`
    : '';
  return `<div class="card batch">
    <div class="batch-head">
      <span class="badge ${b.status}">${STATUS_TEXT[b.status] || b.status}</span>
      <code>#${b.id}</code>
      <span class="hint">提交于 ${fmtTime(b.created_at)}</span>
      <span class="spacer"></span>
      ${cancelBtn}${toggleBtn}
    </div>
    <pre class="sql">${escapeHtml(b.sql)}</pre>
    ${b.error ? `<div class="conclusion none">错误：${escapeHtml(b.error)}</div>` : ''}
    ${conclusionHtml(b)}
    ${executionsHtml(b)}
    <div class="detail" id="detail-${b.id}"></div>
  </div>`;
}

async function renderBatches(batches) {
  const container = document.getElementById('batches');
  container.innerHTML = batches.length
    ? batches.map(batchCardHtml).join('')
    : '<p class="hint">暂无批次</p>';
  container.querySelectorAll('[data-cancel]').forEach(btn => {
    btn.onclick = () => cancelBatch(btn.dataset.cancel);
  });
  container.querySelectorAll('[data-toggle]').forEach(btn => {
    btn.onclick = () => toggleDetail(btn.dataset.toggle);
  });
  for (const id of expanded) {
    if (batches.some(b => b.id === id)) await loadDetail(id);
  }
}

async function loadDetail(id) {
  const box = document.getElementById(`detail-${id}`);
  if (!box) return;
  try {
    const res = await fetch(`/api/batches/${id}`);
    if (!res.ok) { box.innerHTML = ''; return; }
    const d = await res.json();
    box.innerHTML = d.executions.map(ex => {
      const struct = (ex.structure_lines || []).join('\n');
      const plan = ex.plan ? JSON.stringify(ex.plan, null, 2) : '（无计划）';
      return `<div class="ex-detail">
        <h4>第 ${ex.round} 轮 · #${ex.seq} · ${SETTING_TEXT[ex.setting]} ·
            ${EX_STATUS_TEXT[ex.status]}${ex.execution_time_ms != null ? ' · ' + fmtMs(ex.execution_time_ms) : ''}</h4>
        ${struct ? `<pre class="struct">${escapeHtml(struct)}</pre>` : ''}
        <details><summary>完整计划 JSON</summary><pre class="plan">${escapeHtml(plan)}</pre></details>
      </div>`;
    }).join('');
  } catch (e) { /* 下一轮再试 */ }
}

async function toggleDetail(id) {
  if (expanded.has(id)) {
    expanded.delete(id);
    const box = document.getElementById(`detail-${id}`);
    if (box) box.innerHTML = '';
  } else {
    expanded.add(id);
    await loadDetail(id);
  }
  refresh();
}

async function cancelBatch(id) {
  await fetch(`/api/batches/${id}/cancel`, { method: 'POST' });
  refresh();
}

document.getElementById('run-btn').onclick = async () => {
  const msg = document.getElementById('submit-msg');
  msg.className = 'msg';
  msg.textContent = '提交中…';
  try {
    const res = await fetch('/api/batches', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sql: sqlInput.value }),
    });
    if (res.status === 409) {
      msg.textContent = '繁忙：已有对照批次在运行，请稍后再试';
      msg.classList.add('warn');
    } else if (res.status === 400) {
      const d = await res.json();
      msg.textContent = '校验失败：' + (d.detail || '');
      msg.classList.add('err');
    } else if (res.ok) {
      msg.textContent = '批次已创建，开始运行';
      msg.classList.add('ok');
    } else {
      msg.textContent = '提交失败（HTTP ' + res.status + '）';
      msg.classList.add('err');
    }
  } catch (e) {
    msg.textContent = '网络错误：' + e;
    msg.classList.add('err');
  }
  refresh();
};

document.getElementById('insert-btn').onclick = async () => {
  const msg = document.getElementById('insert-msg');
  msg.className = 'msg';
  const payload = {};
  if (insertDateInput.value) payload.settle_date = insertDateInput.value;
  if (insertOrderNoInput.value.trim()) payload.order_no = insertOrderNoInput.value.trim();
  try {
    const res = await fetch('/api/sample-row', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const d = await res.json();
    if (d.ok) {
      msg.textContent = `已补记 ${d.row.order_no}（商户 ${d.row.merchant_id}，金额 ${d.row.amount}，结算日 ${d.row.settle_date}）`;
      msg.classList.add('ok');
    } else {
      msg.textContent = '写入失败：' + (d.detail || '');
      msg.classList.add('err');
    }
  } catch (e) {
    msg.textContent = '网络错误：' + e;
    msg.classList.add('err');
  }
  refreshVerification();
};

refresh();
refreshVerification();
setInterval(() => { refresh(); refreshVerification(); }, 1500);
