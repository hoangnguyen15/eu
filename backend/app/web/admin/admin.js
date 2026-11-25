import { Session, ago, badge, el, fmtSeconds, toast } from "/shared/api.js";

const session = new Session("flow.admin.tokens");
const $ = (id) => document.getElementById(id);
let tab = "overview";
let timer = null;

// ------------------------------------------------------------------- auth
function showLogin(msg = "") {
  $("app").classList.add("hidden");
  $("login").classList.remove("hidden");
  $("login-error").textContent = msg;
}

async function login() {
  try {
    await session.login($("email").value.trim(), $("password").value);
    await start();
  } catch (e) {
    $("login-error").textContent = e.message;
  }
}
$("btn-login").addEventListener("click", login);
$("password").addEventListener("keydown", (e) => { if (e.key === "Enter") login(); });
$("btn-logout").addEventListener("click", async () => {
  clearInterval(timer);
  await session.logout();
  showLogin();
});

async function start() {
  let me;
  try {
    me = await session.call("GET", "/user/profile");
  } catch {
    showLogin();
    return;
  }
  if (me.role !== "admin") {
    await session.logout();
    showLogin("Tài khoản này không có quyền admin.");
    return;
  }
  $("who").textContent = me.email;
  $("login").classList.add("hidden");
  $("app").classList.remove("hidden");
  switchTab(tab);
  clearInterval(timer);
  timer = setInterval(() => { if (!document.hidden) refresh(false); }, 3000);
}

// -------------------------------------------------------------------- tabs
for (const b of document.querySelectorAll(".nav")) b.addEventListener("click", () => switchTab(b.dataset.tab));
for (const b of document.querySelectorAll("[data-goto]")) b.addEventListener("click", () => switchTab(b.dataset.goto));
$("job-filter").addEventListener("change", () => refresh(true));

function switchTab(name) {
  tab = name;
  for (const b of document.querySelectorAll(".nav")) b.classList.toggle("active", b.dataset.tab === name);
  for (const s of document.querySelectorAll(".tab")) s.classList.toggle("hidden", s.id !== `tab-${name}`);
  refresh(true);
}

const AUTO = new Set(["overview", "workstations", "sessions", "jobs", "review"]);

async function refresh(force) {
  if (!force && !AUTO.has(tab)) return;
  try {
    if (tab === "overview") await renderOverview();
    else if (tab === "workstations") await renderWorkstations();
    else if (tab === "sessions") await renderSessions();
    else if (tab === "jobs") await renderJobs();
    else if (tab === "review") await renderReview();
    else if (tab === "provision" && force) await renderProvision();
    else if (tab === "audit") await renderAudit();
    updateReviewCount();
    $("updated").textContent = new Date().toLocaleTimeString("vi-VN");
  } catch (e) {
    if (e.status === 401) showLogin("Phiên đăng nhập hết hạn.");
    else toast(e.message, "error");
  }
}

async function updateReviewCount() {
  const rows = await session.call("GET", "/admin/generations?status=REVIEW_REQUIRED&limit=200");
  const c = $("review-count");
  c.textContent = rows.length;
  c.classList.toggle("hidden", rows.length === 0);
}

// ----------------------------------------------------------------- helpers
function table(headers, rows, empty = "Không có dữ liệu") {
  if (!rows.length) return el("p", { class: "muted small", text: empty });
  return el("table", {},
    el("thead", {}, el("tr", {}, headers.map((h) => el("th", { text: h })))),
    el("tbody", {}, rows.map((cells) => el("tr", {}, cells.map((c) => el("td", {}, c))))));
}

function progress(pct) {
  const bar = el("span");
  bar.style.width = `${Math.max(3, pct)}%`;
  return el("div", { class: "progress" }, bar);
}

function thumbs(assets) {
  if (!assets || !assets.length) return "—";
  return el("div", { class: "thumbs" }, assets.map((a) => el("img", {
    src: a.thumbnail_url || a.url, alt: "kết quả", loading: "lazy",
    onclick: () => {
      const box = el("div", { class: "lightbox", onclick: () => box.remove() }, el("img", { src: a.url, alt: "ảnh gốc" }));
      document.body.append(box);
    },
  })));
}

function action(label, fn, cls = "secondary small") {
  return el("button", {
    class: cls,
    onclick: async (e) => {
      e.target.disabled = true;
      try {
        await fn();
        await refresh(true);
      } catch (err) {
        toast(err.message, "error");
      } finally {
        e.target.disabled = false;
      }
    },
  }, label);
}

function kpi(k, v, sub, cls = "") {
  return el("div", { class: `kpi ${cls}` }, el("div", { class: "v", text: String(v) }), el("div", { class: "k", text: k }),
    sub ? el("div", { class: "s", text: sub }) : null);
}

function jobCells(j) {
  const active = ["queued", "dispatched", "running", "uploading"].includes(j.status);
  return [
    el("div", {}, badge(j.status), active ? progress(j.status === "queued" ? 2 : j.progress) : null),
    el("div", { class: "prompt-cell" }, el("div", { text: j.prompt }),
      el("div", { class: "small muted ellipsis", title: j.user_email, text: `${j.number_of_images} ảnh · ${j.user_email}` }),
      j.error_code ? el("div", { class: "small error-text", text: `${j.error_code}${j.error_message ? ` — ${j.error_message}` : ""}` }) : null),
    el("div", { class: "small" }, el("div", { class: "nowrap", text: j.worker_name || "—" }),
      el("div", { class: "muted ellipsis", title: j.account_email || "", text: j.account_email || "" })),
    el("div", { class: "small nowrap" }, el("div", { text: ago(j.created_at) }),
      el("div", { class: "muted", text: j.duration_seconds != null ? `tổng ${fmtSeconds(j.duration_seconds)}` : "" })),
    thumbs(j.assets),
  ];
}

// --------------------------------------------------------------- overview
async function renderOverview() {
  const [ov, wss, jobs] = await Promise.all([
    session.call("GET", "/admin/overview"),
    session.call("GET", "/admin/workstations"),
    session.call("GET", "/admin/jobs?limit=8"),
  ]);
  const g = ov.generations;
  const online = (ov.workstations.ONLINE || {}).count || 0;
  const total = Object.values(ov.workstations).reduce((a, w) => a + w.count, 0);
  const inflight = (g.DISPATCHED || 0) + (g.RUNNING || 0) + (g.UPLOADING || 0);
  const c24 = ov.last_24h.completed || { count: 0, avg_seconds: 0, p95_seconds: 0 };
  const failed24 = ["failed", "auth_required", "provider_limited"].reduce((a, k) => a + ((ov.last_24h[k] || {}).count || 0), 0);
  const authNeeded = ov.accounts.filter((a) => a.session_status === "AUTH_REQUIRED").reduce((a, x) => a + x.count, 0);
  const limited = ov.accounts.filter((a) => a.status === "LIMITED").reduce((a, x) => a + x.count, 0);
  $("kpis").replaceChildren(
    kpi("Máy trạm online", `${online}/${total}`, total - online ? `${total - online} offline/khác` : "tất cả online",
      online < total ? "warn" : ""),
    kpi("Slot đang chạy", `${ov.slots.busy}/${ov.slots.online_capacity}`, "đang bận / tổng slot online"),
    kpi("Hàng đợi", g.QUEUED || 0, ov.queue_oldest_seconds ? `lâu nhất ${fmtSeconds(ov.queue_oldest_seconds)}` : "trống",
      ov.queue_oldest_seconds > 60 ? "warn" : ""),
    kpi("Đang xử lý", inflight),
    kpi("Hoàn thành 24h", c24.count, `TB ${fmtSeconds(c24.avg_seconds)} · p95 ${fmtSeconds(c24.p95_seconds)}`),
    kpi("Lỗi 24h", failed24, "", failed24 ? "bad" : ""),
    kpi("Cần xem xét", g.REVIEW_REQUIRED || 0, "", g.REVIEW_REQUIRED ? "warn" : ""),
    kpi("Phiên cần đăng nhập lại", authNeeded, limited ? `${limited} tài khoản bị giới hạn` : "", authNeeded ? "warn" : ""),
  );
  $("ov-ws").replaceChildren(table(["Máy", "Trạng thái", "Slot", "Heartbeat"], wss.map((w) => [
    w.name, badge(w.status.toLowerCase()), `${w.active_leases}/${w.capacity}`, ago(w.last_heartbeat_at)])));
  $("ov-jobs").replaceChildren(table(["Trạng thái", "Prompt", "Máy", "Thời gian", "Kết quả"], jobs.map(jobCells)));
}

// ----------------------------------------------------------- workstations
async function renderWorkstations() {
  const wss = await session.call("GET", "/admin/workstations");
  $("ws-table").replaceChildren(table(
    ["Máy trạm", "Trạng thái", "Phiên bản", "Slot", "Provider", "Heartbeat", "Chứng chỉ", ""],
    wss.map((w) => [
      el("div", {}, el("strong", { text: w.name }), el("div", { class: "small muted mono", text: w.hostname || "" })),
      badge(w.status.toLowerCase()),
      w.agent_version || "—",
      el("div", {}, `${w.active_leases}/${w.capacity}`, progress(w.capacity ? (100 * w.active_leases) / w.capacity : 0)),
      (w.providers || []).join(", ") || "—",
      ago(w.last_heartbeat_at),
      w.cert_expires_at ? new Date(w.cert_expires_at).toLocaleDateString("vi-VN") : "—",
      el("div", { class: "row" },
        action("Kiểm tra lại phiên", () => session.call("POST", `/admin/workstations/${w.id}/session-check`)
          .then((r) => toast(r.online ? "Đã gửi yêu cầu kiểm tra phiên" : "Máy đang offline; sẽ báo khi kết nối lại"))),
        w.status === "DISABLED"
          ? action("Kích hoạt", () => session.call("POST", `/admin/workstations/${w.id}/enable`))
          : action("Vô hiệu hoá", async () => {
            if (!confirm(`Vô hiệu hoá máy ${w.name}? Máy sẽ bị ngắt kết nối ngay.`)) return;
            await session.call("POST", `/admin/workstations/${w.id}/disable`);
          }, "danger small")),
    ]), "Chưa có máy trạm nào. Dùng mục “Thêm máy trạm”."));
}

// --------------------------------------------------------------- sessions
async function renderSessions() {
  const [rows, accts] = await Promise.all([
    session.call("GET", "/admin/sessions"),
    session.call("GET", "/admin/google-accounts"),
  ]);
  $("sessions-table").replaceChildren(table(
    ["Máy trạm", "Tài khoản", "Profile", "Provider", "Phiên", "Kiểm tra lần cuối", ""],
    rows.map((s) => [
      el("div", {}, s.workstation_name, " ", badge(s.workstation_status.toLowerCase())),
      el("div", {}, s.google_email, el("div", { class: "small" }, badge(s.account_status.toLowerCase()),
        s.limited_until ? el("span", { class: "small muted", text: ` đến ${new Date(s.limited_until).toLocaleTimeString("vi-VN")}` }) : null)),
      el("span", { class: "mono", text: s.profile_ref }),
      s.provider,
      badge(s.session_status.toLowerCase()),
      ago(s.last_verified_at),
      action("Kiểm tra lại phiên", () => session.call("POST", `/admin/workstations/${s.workstation_id}/session-check`)
        .then(() => toast("Đã gửi yêu cầu; trạng thái cập nhật sau vài giây"))),
    ]), "Chưa có phiên nào được máy trạm báo cáo."));
  $("accounts-table").replaceChildren(table(
    ["Email", "Uỷ quyền", "Phiên", "Trạng thái", ""],
    accts.map((a) => [
      a.google_email, badge(a.authorization_status.toLowerCase()), badge(a.session_status.toLowerCase()),
      badge(a.status.toLowerCase()),
      el("div", { class: "row" },
        a.authorization_status === "AUTHORIZED"
          ? action("Thu hồi", () => session.call("PATCH", `/admin/google-accounts/${a.id}`, { authorization_status: "REVOKED" }), "danger small")
          : action("Uỷ quyền", () => session.call("PATCH", `/admin/google-accounts/${a.id}`, { authorization_status: "AUTHORIZED" })),
        a.status === "LIMITED"
          ? action("Bỏ giới hạn", () => session.call("PATCH", `/admin/google-accounts/${a.id}`, { status: "ACTIVE" }))
          : null),
    ])));
}

$("account-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await session.call("POST", "/admin/google-accounts", {
      google_email: $("acct-email").value.trim(), authorization_status: "AUTHORIZED",
      attestation: $("acct-attest").value.trim(),
    });
    toast("Đã thêm tài khoản");
    e.target.reset();
    await renderSessions();
  } catch (err) {
    toast(err.message, "error");
  }
});

// ------------------------------------------------------------------- jobs
async function renderJobs() {
  const rows = await session.call("GET", `/admin/jobs?limit=100&status=${encodeURIComponent($("job-filter").value)}`);
  $("jobs-table").replaceChildren(table(["Trạng thái", "Prompt / người dùng", "Máy / tài khoản", "Thời gian", "Kết quả"],
    rows.map(jobCells)));
}

// ----------------------------------------------------------------- review
async function renderReview() {
  const [review, dlq] = await Promise.all([
    session.call("GET", "/admin/jobs?status=REVIEW_REQUIRED&limit=100"),
    session.call("GET", "/admin/dead-letters"),
  ]);
  $("review-table").replaceChildren(table(["Trạng thái", "Prompt / người dùng", "Máy / tài khoản", "Thời gian", ""],
    review.map((j) => {
      const cells = jobCells(j).slice(0, 4);
      if (j.credit_state === "reserved") {
        cells.push(el("div", { class: "row" },
          action("Hoàn credit", () => session.call("POST", `/admin/generations/${j.id}/resolve`, { action: "refund" })),
          action("Tính phí", () => session.call("POST", `/admin/generations/${j.id}/resolve`, { action: "charge" }), "danger small")));
      } else {
        cells.push(el("span", { class: "small muted", text: `credit: ${j.credit_state}` }));
      }
      return cells;
    }), "Không có job nào cần xem xét."));
  $("dlq-table").replaceChildren(table(["Generation", "Số lần phân phối", "Lỗi", "Tạo lúc"],
    dlq.map((d) => [el("span", { class: "mono", text: d.id }), d.dispatch_attempts, d.error_message || "", ago(d.created_at)]),
    "Dead-letter trống."));
}

// --------------------------------------------------------------- provision
async function renderProvision() {
  const accts = (await session.call("GET", "/admin/google-accounts")).filter((a) => a.authorization_status === "AUTHORIZED");
  const box = $("pv-accounts");
  if (!accts.length) {
    box.replaceChildren(el("p", { class: "small muted", text: "Chưa có tài khoản được uỷ quyền (thêm ở mục Phiên đăng nhập)." }));
    return;
  }
  box.replaceChildren(...accts.map((a, i) => el("div", { class: "acct-row" },
    el("input", { type: "checkbox", id: `pv-a-${i}`, dataset: { email: a.google_email } }),
    el("label", { for: `pv-a-${i}`, text: a.google_email }),
    el("input", { id: `pv-p-${i}`, value: `profile-${i + 1}`, "aria-label": "Profile ref", placeholder: "profile ref" }))));
}

$("provision-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const accounts = [...document.querySelectorAll("#pv-accounts input[type=checkbox]")]
    .filter((c) => c.checked)
    .map((c) => ({ google_email: c.dataset.email, profile_ref: $(c.id.replace("pv-a-", "pv-p-")).value.trim(), provider: "mock" }));
  try {
    const res = await session.call("POST", "/admin/workstations/provision", {
      workstation_name: $("pv-name").value.trim(), capacity: Number($("pv-cap").value), accounts,
    });
    const blob = new Blob([JSON.stringify(res.config, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = el("a", { href: url, download: res.file_name, class: "btn", text: `Tải ${res.file_name}` });
    $("provision-result").classList.remove("hidden");
    $("provision-result").replaceChildren(
      el("h2", { text: "Cấu hình đã tạo" }),
      el("p", { class: "small", text: `Token dùng một lần, hết hạn ${new Date(res.expires_at).toLocaleString("vi-VN")}. File chứa token — chỉ chuyển cho máy trạm tương ứng.` }),
      link,
      el("ol", { class: "small" },
        el("li", { text: "Giải nén gói FlowAgent-<version>-win-x64.zip trên máy trạm (Windows Server 2022)." }),
        el("li", { text: `Đặt file ${res.file_name} vào cùng thư mục, đổi tên thành flowagent-config.json.` }),
        el("li", { text: "Chuột phải Install-FlowAgent.cmd → Run as administrator." }),
        el("li", { text: "Máy trạm sẽ xuất hiện ONLINE trong mục Máy trạm sau vài giây." })),
      el("pre", { class: "config mono", text: JSON.stringify({ ...res.config, EnrollmentToken: "••••••", CaCertificatePem: "(…)" }, null, 2) }));
    link.click();
  } catch (err) {
    toast(err.details && err.details.accounts ? `${err.message}: ${err.details.accounts.join(", ")}` : err.message, "error");
  }
});

// ------------------------------------------------------------------- audit
async function renderAudit() {
  const rows = await session.call("GET", "/admin/audit-logs?limit=150");
  $("audit-table").replaceChildren(table(["Thời gian", "Hành động", "Đối tượng", "Actor", "IP"],
    rows.map((r) => [new Date(r.created_at).toLocaleString("vi-VN"), el("span", { class: "mono", text: r.action }),
      el("span", { class: "mono small", text: `${r.resource_type}:${(r.resource_id || "").slice(0, 8)}` }),
      `${r.actor_type}`, r.ip_address || ""])));
}

// -------------------------------------------------------------------- boot
if (session.loggedIn) start();
else showLogin();
