/* ============ SCRM 共享 JS ============ */
const S = {};

S.$ = (id, root) => (root || document).getElementById(id);
S.$$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

// ---------- 统一图标 ----------
// 旧页面以 emoji 编写。这里在界面组件范围内统一转换为轻量线性 SVG，避免外部图标库依赖，
// 也让异步加载的列表、弹窗和空状态保持一致的图标语言；不处理评论、标题等业务数据文本。
const UI_ICON_PATHS = {
  shield: '<path d="M12 3 5 6v5c0 4.6 2.9 8.8 7 10 4.1-1.2 7-5.4 7-10V6l-7-3Z"/><path d="m9.5 12 1.7 1.7 3.6-3.6"/>',
  chart: '<path d="M4 19V5"/><path d="M4 19h16"/><path d="m7 15 4-4 3 2 5-6"/>',
  message: '<path d="M20 11.5a7.5 7.5 0 0 1-8 7.5 8.4 8.4 0 0 1-3.5-.8L4 20l1.3-4A7.2 7.2 0 0 1 4 11.5 7.5 7.5 0 0 1 12 4a7.5 7.5 0 0 1 8 7.5Z"/><path d="M8.5 12h.01M12 12h.01M15.5 12h.01"/>',
  video: '<rect x="3" y="5" width="14" height="14" rx="2"/><path d="m17 10 4-2v8l-4-2"/><path d="m8 9 4 3-4 3Z"/>',
  alert: '<path d="M10.3 4.3 3.4 17a2 2 0 0 0 1.8 3h13.6a2 2 0 0 0 1.8-3L13.7 4.3a1.95 1.95 0 0 0-3.4 0Z"/><path d="M12 9v4M12 17h.01"/>',
  users: '<path d="M16 20v-1.5A4.5 4.5 0 0 0 11.5 14h-4A4.5 4.5 0 0 0 3 18.5V20"/><circle cx="9.5" cy="7.5" r="3.5"/><path d="M17 4.5a3.5 3.5 0 0 1 0 6.7M21 20v-1.5a4.5 4.5 0 0 0-3-4.25"/>',
  mobile: '<rect x="7" y="3" width="10" height="18" rx="2"/><path d="M11 18h2"/>',
  bot: '<rect x="4" y="7" width="16" height="12" rx="3"/><path d="M12 3v4M8 12h.01M16 12h.01M9 16h6"/>',
  brain: '<path d="M9 5.5A3.5 3.5 0 0 0 3 8a3.4 3.4 0 0 0 1 2.4A3.6 3.6 0 0 0 5 17a3.6 3.6 0 0 0 5 1.1A3.7 3.7 0 0 0 12 20V4a3.7 3.7 0 0 0-3 1.5ZM15 5.5A3.5 3.5 0 0 1 21 8a3.4 3.4 0 0 1-1 2.4A3.6 3.6 0 0 1 19 17a3.6 3.6 0 0 1-5 1.1A3.7 3.7 0 0 1 12 20V4a3.7 3.7 0 0 1 3 1.5Z"/>',
  compass: '<circle cx="12" cy="12" r="8"/><path d="m15 9-2 4-4 2 2-4 4-2Z"/>',
  list: '<path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"/>',
  building: '<rect x="4" y="3" width="16" height="18" rx="1"/><path d="M8 7h.01M12 7h.01M16 7h.01M8 11h.01M12 11h.01M16 11h.01M8 15h.01M12 15h.01M16 15h.01"/>',
  funnel: '<path d="M3 5h18l-7 8v5l-4 2v-7L3 5Z"/>',
  file: '<path d="M7 3h7l4 4v14H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>',
  handshake: '<path d="m8 11 3 3a2 2 0 0 0 2.8 0l3.7-3.7"/><path d="m3 10 4-4 3 2 2-2 3 1 2-1 4 4-4 6-2.4-1.7M3 10l4 6 2-1.5"/>',
  tag: '<path d="M4 4h7l8 8-7 7-8-8V4Z"/><circle cx="8" cy="8" r="1"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
  refresh: '<path d="M20 11a8 8 0 0 0-14-4L4 9M4 4v5h5M4 13a8 8 0 0 0 14 4l2-2M20 20v-5h-5"/>',
  arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
  back: '<path d="M19 12H5M11 6l-6 6 6 6"/>',
  check: '<path d="m5 12 4.2 4L19 6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M9 7l1-3h4l1 3M6 7l1 14h10l1-14"/>',
  link: '<path d="M10 13a4 4 0 0 0 5.7.1l3-3a4 4 0 0 0-5.7-5.7l-1.7 1.7"/><path d="M14 11a4 4 0 0 0-5.7-.1l-3 3A4 4 0 0 0 11 19.6l1.7-1.7"/>',
  light: '<circle cx="12" cy="12" r="3"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  star: '<path d="m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1-5.4-2.8-5.4 2.8 1-6.1-4.4-4.3 6.1-.9L12 3Z"/>',
  calendar: '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M8 3v4M16 3v4M4 10h16"/>',
  eye: '<path d="M3 12s3-6 9-6 9 6 9 6-3 6-9 6-9-6-9-6Z"/><circle cx="12" cy="12" r="2.5"/>',
};
const UI_EMOJI_ICONS = {
  "🛡":"shield", "🛡️":"shield", "📈":"chart", "📊":"chart", "💬":"message", "🎬":"video", "📺":"video", "🚨":"alert", "🔔":"alert", "🔥":"alert", "👥":"users", "👤":"users", "👪":"users", "📱":"mobile", "🤖":"bot", "🎭":"bot", "🧠":"brain", "🧭":"compass", "📋":"list", "🏢":"building", "🔻":"funnel", "📨":"message", "📑":"file", "📒":"file", "📌":"tag", "🤝":"handshake", "🏷":"tag", "🔍":"search", "🔎":"search", "🔄":"refresh", "←":"back", "→":"arrow", "✓":"check", "➕":"plus", "🗑":"trash", "🔗":"link", "💡":"light", "✦":"star", "🎉":"star", "⭐":"star", "🚀":"arrow", "🛒":"tag", "📥":"arrow", "📷":"video", "📝":"file", "📞":"mobile", "💾":"file", "📭":"file", "🧩":"tag", "🔓":"shield", "👁":"eye", "🙈":"eye", "👍":"check", "👎":"back", "👉":"arrow", "🍃":"light", "🧊":"alert", "◉":"chart", "◷":"calendar", "⌁":"shield"
};
S.icon = (name, label = "") => `<svg class="ui-icon" aria-hidden="${label ? "false" : "true"}"${label ? ` aria-label="${S.esc(label)}"` : ""} viewBox="0 0 24 24">${UI_ICON_PATHS[name] || UI_ICON_PATHS.file}</svg>`;
const UI_ICON_PATTERN = new RegExp(Object.keys(UI_EMOJI_ICONS).sort((a, b) => b.length - a.length).map(v => v.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "gu");
const UI_ICON_SCOPE = ".topbar,.side,.page-head,.card-title,.btn,.nav-link,.empty,.stat-card,.module-card,.entity-card,.login-page,.deal-hero,.deal-kpi,.deal-progress,.modal-head";
function normalizeUiIcons(root = document.body) {
  if (!root) return;
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, { acceptNode(node) {
    const parent = node.parentElement;
    if (!parent || parent.closest("script,style,textarea,option,[data-ui-icon]")) return NodeFilter.FILTER_REJECT;
    if (!parent.closest(UI_ICON_SCOPE)) return NodeFilter.FILTER_REJECT;
    UI_ICON_PATTERN.lastIndex = 0;
    return UI_ICON_PATTERN.test(node.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
  }});
  const nodes = []; while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach(node => {
    UI_ICON_PATTERN.lastIndex = 0;
    const parts = node.nodeValue.split(UI_ICON_PATTERN);
    const found = node.nodeValue.match(UI_ICON_PATTERN) || [];
    if (!found.length) return;
    const fragment = document.createDocumentFragment();
    parts.forEach((part, index) => {
      if (part) fragment.appendChild(document.createTextNode(part));
      const emoji = found[index];
      if (emoji) { const holder = document.createElement("span"); holder.dataset.uiIcon = UI_EMOJI_ICONS[emoji]; holder.innerHTML = S.icon(UI_EMOJI_ICONS[emoji]); fragment.appendChild(holder); }
    });
    node.replaceWith(fragment);
  });
}
document.addEventListener("DOMContentLoaded", () => {
  const setTheme = theme => {
    const isDark = theme === "dark";
    document.body.classList.toggle("theme-night", isDark);
    document.querySelectorAll("[data-theme-toggle]").forEach(button => {
      button.setAttribute("aria-pressed", String(button.dataset.themeToggle === theme));
    });
    try { localStorage.setItem("scrm.theme", theme); } catch (_) {}
  };
  const savedTheme = (() => { try { return localStorage.getItem("scrm.theme"); } catch (_) { return null; } })();
  setTheme(savedTheme === "light" ? "light" : "dark");
  document.querySelectorAll("[data-theme-toggle]").forEach(button => {
    button.addEventListener("click", () => setTheme(button.dataset.themeToggle));
  });
  normalizeUiIcons();
  new MutationObserver(records => records.forEach(record => record.addedNodes.forEach(node => {
    if (node.nodeType === Node.ELEMENT_NODE || node.nodeType === Node.DOCUMENT_FRAGMENT_NODE) normalizeUiIcons(node);
  }))).observe(document.body, { childList: true, subtree: true });
});

S.esc = s => (s == null ? "" : String(s))
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");

S.api = async (path, opts = {}) => {
  const opt = { headers: {}, ...opts };
  if (opt.body && typeof opt.body !== "string") {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(opt.body);
  }
  const r = await fetch("/scrm/api" + path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ("请求失败 " + r.status));
  return data;
};

// 复用主应用评论操作接口
S.commentApi = async (path, body) => {
  const r = await fetch("/api" + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ("请求失败 " + r.status));
  return data;
};

S.fmtTs = v => {
  if (v === "" || v == null) return "—";
  const n = Number(v);
  if (!n || isNaN(n)) return "—";
  const sec = n > 1e11 ? n / 1000 : n;
  const d = new Date(sec * 1000);
  if (isNaN(d.getTime())) return "—";
  const p = x => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
S.fmtDate = v => (S.fmtTs(v) === "—" ? "—" : S.fmtTs(v).slice(0, 10));
S.ago = v => {
  const n = Number(v);
  if (!n) return "—";
  const diff = Date.now() / 1000 - (n > 1e11 ? n / 1000 : n);
  if (diff < 0) return S.fmtTs(v);
  if (diff < 3600) return Math.max(1, Math.floor(diff / 60)) + " 分钟前";
  if (diff < 86400) return Math.floor(diff / 3600) + " 小时前";
  if (diff < 30 * 86400) return Math.floor(diff / 86400) + " 天前";
  return S.fmtDate(v);
};

S.toast = (msg, type) => {
  const t = document.createElement("div");
  t.className = "toast " + (type || "");
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2600);
};

S.loading = () => '<div class="loading"><span class="spin"></span> 加载中…</div>';
S.empty = (text, icon) => `<div class="empty"><div class="big">${icon || "📭"}</div>${S.esc(text || "暂无数据")}</div>`;

// ---------- 徽章 ----------
S.badge = (text, cls, dot) =>
  `<span class="badge ${cls || ""} ${dot ? "dot" : ""}">${S.esc(text)}</span>`;
S.platformTag = p => `<span class="platform-tag">${S.esc(p)}</span>`;

// ---------- 分页 ----------
S.pager = (el, total, page, size, onGo) => {
  const pages = Math.max(1, Math.ceil(total / size));
  const btns = [];
  const add = (label, p, opts = {}) => {
    const dis = opts.disabled ? "disabled" : "";
    const cur = opts.cur ? "cur" : "";
    btns.push(`<button data-p="${p}" ${dis} class="${cur}">${label}</button>`);
  };
  add("‹", page - 1, { disabled: page <= 1 });
  const win = [];
  for (let p = 1; p <= pages; p++) {
    if (p === 1 || p === pages || Math.abs(p - page) <= 1) win.push(p);
  }
  let last = 0;
  for (const p of win) {
    if (p - last > 1) btns.push('<button disabled>…</button>');
    add(String(p), p, { cur: p === page });
    last = p;
  }
  add("›", page + 1, { disabled: page >= pages });
  el.innerHTML = `
    <div class="info">共 <b>${total.toLocaleString()}</b> 条 · 第 ${page}/${pages} 页</div>
    <div class="ctrl">${btns.join("")}</div>`;
  el.querySelectorAll("button[data-p]").forEach(b => {
    b.onclick = () => { const p = Number(b.dataset.p); if (p >= 1 && p <= pages) onGo(p); };
  });
};

// ---------- 弹窗 ----------
S.modal = ({ title, body, wide, okText, onOk }) => {
  const mask = document.createElement("div");
  mask.className = "modal-mask";
  mask.innerHTML = `
    <div class="modal ${wide ? "wide" : ""}">
      <div class="modal-head">${S.esc(title)}<span class="x">✕</span></div>
      <div class="modal-body"></div>
      <div class="modal-foot">
        <button class="btn" data-act="cancel">取消</button>
        ${okText !== null ? `<button class="btn primary" data-act="ok">${S.esc(okText || "确定")}</button>` : ""}
      </div>
    </div>`;
  const bodyEl = mask.querySelector(".modal-body");
  if (typeof body === "string") bodyEl.innerHTML = body;
  else bodyEl.appendChild(body);
  const close = () => mask.remove();
  mask.querySelector(".x").onclick = close;
  mask.querySelector('[data-act="cancel"]').onclick = close;
  mask.onclick = e => { if (e.target === mask) close(); };
  const okBtn = mask.querySelector('[data-act="ok"]');
  if (okBtn) okBtn.onclick = async () => {
    if (onOk) {
      try {
        const ret = await onOk(bodyEl, close, okBtn);
        if (ret !== false) close();
      } catch (err) { S.toast(err.message, "err"); }
    } else close();
  };
  document.body.appendChild(mask);
  return { mask, bodyEl, close };
};

S.confirm = (text, onOk) => S.modal({
  title: "请确认",
  body: `<p style="margin:6px 0;">${S.esc(text)}</p>`,
  okText: "确定",
  onOk: async () => { await onOk(); },
});


// ---------- 评论快捷操作 ----------
S.commentReplyModal = (row, after) => {
  S.modal({
    title: "修改回复",
    body: `
      <div class="form-row"><label>评论内容</label><div class="sub">${S.esc(row.content || "")}</div></div>
      <div class="form-row"><label>推荐回复</label>
        <textarea id="mReply" placeholder="填写审核前可使用的回复">${S.esc(row.reply || "")}</textarea></div>`,
    okText: "保存回复",
    onOk: async (body) => {
      await S.commentApi("/comment/update", {
        table: row.src_table,
        id: row.src_pk,
        reply_suggestion: body.querySelector("#mReply").value.trim(),
      });
      S.toast("回复已保存", "ok");
      if (after) after();
    },
  });
};

S.commentReview = async (row, after) => {
  await S.commentApi("/comment/review", { table: row.src_table, id: row.src_pk, reviewed: 1 });
  S.toast("已审核通过", "ok");
  if (after) after();
};

S.commentNoAction = async (row, after) => {
  await S.commentApi("/comment/batch", { action: "no-action", records: [{ table: row.src_table, id: row.src_pk }], value: 1 });
  S.toast("已标记无需操作", "ok");
  if (after) after();
};

S.commentMarkDelete = (row, after) => {
  S.confirm("确认标注删除并置为已审核?", async () => {
    const r = await S.commentApi("/comment/delete-accept", { table: row.src_table, id: row.src_pk });
    S.toast(r.affected ? "已标注删除并置为已审核" : "未找到记录", r.affected ? "ok" : "err");
    if (after) after();
  });
};

S.commentActionButtons = (i, opts = {}) => `
  <button class="btn sm" data-cact="reply" data-i="${i}">修改回复</button>
  <button class="btn sm primary" data-cact="review" data-i="${i}">审核通过</button>
  <button class="btn sm danger" data-cact="markDelete" data-i="${i}">标注删除</button>
  <button class="btn sm" data-cact="noAction" data-i="${i}">无需操作</button>
  ${opts.detailHref ? `<a class="btn sm" href="${S.esc(opts.detailHref)}">详情</a>` : ""}
`;

S.bindCommentActionButtons = (root, rows, after) => {
  S.$$('[data-cact]', root || document).forEach(b => b.onclick = async () => {
    const row = rows[Number(b.dataset.i)];
    try {
      if (b.dataset.cact === "reply") return S.commentReplyModal(row, after);
      if (b.dataset.cact === "review") return await S.commentReview(row, after);
      if (b.dataset.cact === "markDelete") return S.commentMarkDelete(row, after);
      if (b.dataset.cact === "noAction") return await S.commentNoAction(row, after);
    } catch (e) { S.toast(e.message, "err"); }
  });
};

// ---------- 查询参数 ----------
S.qs = () => Object.fromEntries(new URLSearchParams(location.search).entries());
S.qsGet = k => new URLSearchParams(location.search).get(k);

// ---------- 折线图(多序列) ----------
S.lineChart = (el, labels, series) => {
  const W = 860, H = 250, PL = 44, PR = 14, PT = 16, PB = 30;
  const iw = W - PL - PR, ih = H - PT - PB;
  const max = Math.max(1, ...series.flatMap(s => s.data));
  const grid = niceMax(max);
  const x = i => PL + (labels.length <= 1 ? iw / 2 : i * iw / (labels.length - 1));
  const y = v => PT + ih - v / grid * ih;
  let html = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;" preserveAspectRatio="xMidYMid meet">`;
  for (let g = 0; g <= 4; g++) {
    const gy = PT + ih - g * ih / 4;
    html += `<line x1="${PL}" y1="${gy}" x2="${W - PR}" y2="${gy}" stroke="#eef0f5"/>`;
    html += `<text x="${PL - 8}" y="${gy + 4}" text-anchor="end" font-size="11" fill="#9ca3af">${Math.round(grid * g / 4)}</text>`;
  }
  const step = Math.ceil(labels.length / 10);
  labels.forEach((lb, i) => {
    if (i % step === 0)
      html += `<text x="${x(i)}" y="${H - 9}" text-anchor="middle" font-size="10.5" fill="#9ca3af">${S.esc(lb)}</text>`;
  });
  series.forEach(s => {
    const pts = s.data.map((v, i) => `${x(i)},${y(v)}`).join(" ");
    if (s.area) {
      html += `<polygon points="${PL},${PT + ih} ${pts} ${W - PR},${PT + ih}" fill="${s.color}" opacity="0.07"/>`;
    }
    html += `<polyline points="${pts}" fill="none" stroke="${s.color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`;
    s.data.forEach((v, i) => {
      html += `<circle cx="${x(i)}" cy="${y(v)}" r="2.6" fill="#fff" stroke="${s.color}" stroke-width="1.6">
        <title>${S.esc(labels[i])} · ${s.name}: ${v}</title></circle>`;
    });
  });
  html += "</svg>";
  html += `<div class="chart-legend">${series.map(s =>
    `<span class="lg"><i style="background:${s.color}"></i>${S.esc(s.name)}</span>`).join("")}</div>`;
  el.innerHTML = html;
};

function niceMax(v) {
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const n = v / p;
  const m = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
  return m * p;
}

// ---------- 环形图 ----------
S.donut = (el, items, centerLabel) => {
  const total = items.reduce((s, i) => s + i.value, 0);
  const R = 62, r = 40, cx = 80, cy = 80;
  let a0 = -Math.PI / 2, paths = "";
  if (total === 0) {
    paths = `<circle cx="${cx}" cy="${cy}" r="${(R + r) / 2}" fill="none" stroke="#eef0f5" stroke-width="${R - r}"/>`;
  }
  items.forEach(it => {
    if (!it.value) return;
    const a1 = a0 + it.value / total * Math.PI * 2;
    const large = a1 - a0 > Math.PI ? 1 : 0;
    paths += arc(cx, cy, R, r, a0, a1, large, it.color);
    a0 = a1;
  });
  el.innerHTML = `
    <div class="donut-flex">
      <svg viewBox="0 0 160 160" style="width:160px;height:160px;flex-shrink:0;">
        ${paths}
        <text x="${cx}" y="${cy - 4}" text-anchor="middle" font-size="24" font-weight="700" fill="#1f2330">${total.toLocaleString()}</text>
        <text x="${cx}" y="${cy + 16}" text-anchor="middle" font-size="11" fill="#9ca3af">${S.esc(centerLabel || "总计")}</text>
      </svg>
      <div class="donut-legend">
        ${items.map(i => `<div class="dl"><i style="background:${i.color}"></i>${S.esc(i.name)}
          <b>${i.value.toLocaleString()} · ${total ? Math.round(i.value * 100 / total) : 0}%</b></div>`).join("")}
      </div>
    </div>`;
};

function arc(cx, cy, R, r, a0, a1, large, color) {
  const p = (rad, ang) => [cx + rad * Math.cos(ang), cy + rad * Math.sin(ang)];
  const [x0, y0] = p(R, a0), [x1, y1] = p(R, a1);
  const [x2, y2] = p(r, a1), [x3, y3] = p(r, a0);
  return `<path d="M${x0.toFixed(2)},${y0.toFixed(2)} A${R},${R} 0 ${large} 1 ${x1.toFixed(2)},${y1.toFixed(2)}
    L${x2.toFixed(2)},${y2.toFixed(2)} A${r},${r} 0 ${large} 0 ${x3.toFixed(2)},${y3.toFixed(2)} Z" fill="${color}"/>`;
}

// ---------- 漏斗 ----------
S.funnel = (el, stages) => {
  const max = Math.max(1, ...stages.map(s => s.value));
  el.innerHTML = stages.map((s, i) => {
    const w = Math.max(5, s.value / max * 100);
    const prev = i === 0 ? null : stages[i - 1].value;
    const stepRate = prev ? Math.round(s.value * 100 / Math.max(1, prev)) : 100;
    return `<div class="funnel-row">
      <div class="fl-label">${S.esc(s.label)}</div>
      <div class="fl-bar" style="width:${w}%;background:${s.color};">${s.value.toLocaleString()}</div>
      <div class="fl-rate">占比 ${s.rate != null ? s.rate : Math.round(s.value * 100 / Math.max(1, stages[0].value))}%
        ${i > 0 ? ` · 转化 ${stepRate}%` : ""}</div>
    </div>`;
  }).join("");
};

// ---------- 通用列表请求 ----------
S.bindSelect = (sel, items, valKey, txtKey, cur) => {
  sel.innerHTML = items.map(i =>
    `<option value="${S.esc(i[valKey])}" ${i[valKey] === cur ? "selected" : ""}>${S.esc(i[txtKey])}</option>`).join("");
};
