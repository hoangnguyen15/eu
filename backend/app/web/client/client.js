import {
  EventStream, Session, TERMINAL, badge, el, fmtSeconds, percentile, toast, uuid,
} from "/shared/api.js";

const session = new Session("flow.client.tokens");
const $ = (id) => document.getElementById(id);
const jobs = new Map(); // id -> {data, node}
let stream = null;
let nextCursor = null;
let connState = "offline";
let stress = null; // {ids:Set, t0:Map, tEnd:Map, startedAt}

// ------------------------------------------------------------------- auth
function showAuth(msg = "") {
  $("main").classList.add("hidden");
  $("auth").classList.remove("hidden");
  $("auth-error").textContent = msg;
}

async function doAuth(kind) {
  const email = $("email").value.trim();
  const password = $("password").value;
  $("auth-error").textContent = "";
  try {
    if (kind === "register") {
      await session.register(email, password);
      toast("Đăng ký thành công");
    }
    await session.login(email, password);
    await start();
  } catch (e) {
    $("auth-error").textContent = e.details && Array.isArray(e.details)
      ? `${e.message}: ${e.details.map((d) => d.msg).join("; ")}` : e.message;
  }
}

$("btn-login").addEventListener("click", () => doAuth("login"));
$("btn-register").addEventListener("click", () => doAuth("register"));
$("password").addEventListener("keydown", (e) => { if (e.key === "Enter") doAuth("login"); });
$("btn-logout").addEventListener("click", async () => {
  if (stream) stream.stop();
  await session.logout();
  jobs.clear();
  $("jobs").replaceChildren();
  showAuth();
});

// ---------------------------------------------------------------- startup
async function start() {
  try {
    const me = await session.call("GET", "/user/profile");
    $("who").textContent = me.email;
  } catch (e) {
    showAuth(e.status === 401 ? "" : e.message);
    return;
  }
  $("auth").classList.add("hidden");
  $("main").classList.remove("hidden");
  await Promise.all([loadCredits(), loadJobs(true)]);
  stream = new EventStream(session, { onEvent, onState: setConn });
  stream.start();
}

function setConn(state) {
  connState = state;
  $("conn-dot").className = `dot ${state}`;
  $("conn-text").textContent = {
    live: "Realtime", connecting: "Đang kết nối", reconnecting: "Kết nối lại…",
    offline: "Mất kết nối (REST)", "logged-out": "Hết phiên",
  }[state] || state;
  if (state === "logged-out") showAuth("Phiên đăng nhập hết hạn, vui lòng đăng nhập lại.");
  if (state === "live") refreshInFlight(); // catch up on anything missed
}

async function loadCredits() {
  const c = await session.call("GET", "/user/credits");
  renderCredits(c);
}

function renderCredits(c) {
  $("credits-available").textContent = c.available_balance;
  $("credits-reserved").textContent = c.reserved_balance ? `· ${c.reserved_balance} đang giữ` : "";
}

async function loadJobs(reset) {
  if (reset) {
    nextCursor = null;
    jobs.clear();
    $("jobs").replaceChildren();
  }
  const q = new URLSearchParams({ limit: "10" });
  if (nextCursor) q.set("cursor", nextCursor);
  const page = await session.call("GET", `/generations?${q}`);
  for (const g of page.items) upsert(g, false);
  nextCursor = page.next_cursor;
  $("btn-more").classList.toggle("hidden", !nextCursor);
}

$("btn-more").addEventListener("click", () => loadJobs(false));
$("btn-refresh").addEventListener("click", () => Promise.all([loadCredits(), loadJobs(true)]));

// --------------------------------------------------------------- realtime
async function onEvent(evt) {
  if (evt.event === "credits.updated") {
    renderCredits(evt.data);
    return;
  }
  const id = evt.data && evt.data.generation_id;
  if (!id) return;
  const now = performance.now();
  if (stress && stress.ids.has(id)) {
    if (evt.event === "generation.started" && !stress.tStart.has(id)) stress.tStart.set(id, now);
    if (TERMINAL.has(evt.data.status) && !stress.tEnd.has(id)) {
      stress.tEnd.set(id, now);
      stress.status.set(id, evt.data.status);
      renderStress();
    }
  }
  const known = jobs.get(id);
  if (!known || TERMINAL.has(evt.data.status)) {
    await fetchJob(id); // assets + final metadata come from REST (source of truth)
    return;
  }
  Object.assign(known.data, { status: evt.data.status, progress: evt.data.progress });
  if (evt.event === "generation.started" && !known.data.started_at) known.data.started_at = evt.timestamp;
  render(known);
}

async function fetchJob(id) {
  try {
    upsert(await session.call("GET", `/generations/${id}`), true);
  } catch (e) {
    if (e.status !== 404) console.warn(e);
  }
}

// REST fallback: while the socket is down, poll in-flight jobs.
async function refreshInFlight() {
  const pending = [...jobs.values()].filter((j) => !TERMINAL.has(j.data.status)).map((j) => j.data.id);
  await Promise.all(pending.map(fetchJob));
}
setInterval(() => {
  if (session.loggedIn && connState !== "live" && !$("main").classList.contains("hidden")) {
    refreshInFlight();
    loadCredits().catch(() => {});
  }
}, 3000);

// ------------------------------------------------------------------ create
for (const chip of document.querySelectorAll(".chip")) {
  chip.addEventListener("click", () => {
    const p = $("prompt");
    if (!p.value.includes(chip.dataset.token)) p.value = `${p.value.trim()} ${chip.dataset.token}`;
  });
}

/** POST with an Idempotency-Key; retries network/5xx errors with the SAME key. */
async function createGeneration(body) {
  const key = uuid();
  for (let attempt = 1; ; attempt++) {
    try {
      return await session.call("POST", "/generations", body, { "Idempotency-Key": key });
    } catch (e) {
      const retryable = e.status === 0 || e.status >= 500 || e.status === 429;
      if (!retryable || attempt >= 4) throw e;
      await new Promise((r) => setTimeout(r, 400 * 2 ** attempt));
    }
  }
}

$("create").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("btn-create");
  btn.disabled = true;
  try {
    const data = await createGeneration({
      prompt: $("prompt").value.trim(),
      aspect_ratio: $("aspect").value,
      number_of_images: Number($("count").value),
      reference_images: [],
      metadata: { source: "web-demo", client_version: "1.1.0" },
    });
    await fetchJob(data.generation_id);
  } catch (err) {
    toast(err.code === "INSUFFICIENT_CREDITS" ? "Không đủ credit" : err.message, "error");
  } finally {
    btn.disabled = false;
  }
});

// ------------------------------------------------------------ stress test
$("btn-stress").addEventListener("click", async () => {
  const n = Number($("stress-n").value);
  const btn = $("btn-stress");
  btn.disabled = true;
  stress = { ids: new Set(), t0: new Map(), tStart: new Map(), tEnd: new Map(), status: new Map(), n,
    begin: performance.now(), rejected: 0 };
  renderStress();
  await Promise.all(Array.from({ length: n }, async (_, i) => {
    const t0 = performance.now();
    try {
      const d = await createGeneration({ prompt: `stress ${i + 1}/${n}`, aspect_ratio: "1:1", number_of_images: 1,
        metadata: { source: "web-demo-stress" } });
      stress.ids.add(d.generation_id);
      stress.t0.set(d.generation_id, t0);
      fetchJob(d.generation_id);
    } catch (e) {
      stress.rejected += 1;
      if (e.code === "INSUFFICIENT_CREDITS") toast("Một số job bị từ chối: không đủ credit", "error");
    }
  }));
  renderStress();
  btn.disabled = false;
});

function renderStress() {
  if (!stress) return;
  const box = $("stress-result");
  box.classList.remove("hidden");
  const done = [...stress.tEnd.keys()];
  const ok = done.filter((id) => stress.status.get(id) === "completed");
  const totals = ok.map((id) => (stress.tEnd.get(id) - stress.t0.get(id)) / 1000);
  const waits = ok.filter((id) => stress.tStart.has(id)).map((id) => (stress.tStart.get(id) - stress.t0.get(id)) / 1000);
  const last = done.length ? Math.max(...done.map((id) => stress.tEnd.get(id))) : performance.now();
  const wall = (last - stress.begin) / 1000;
  const finished = done.length + stress.rejected >= stress.n;
  const metric = (k, v) => el("div", { class: "metric" }, el("div", { class: "v", text: v }), el("div", { class: "k", text: k }));
  box.replaceChildren(
    metric("Hoàn thành", `${ok.length}/${stress.n}`),
    metric("Lỗi / từ chối", `${done.length - ok.length} / ${stress.rejected}`),
    metric("Thông lượng", finished && wall > 0 ? `${(ok.length / wall).toFixed(2)}/s` : "…"),
    metric("Tổng p50", fmtSeconds(percentile(totals, 50))),
    metric("Tổng p95", fmtSeconds(percentile(totals, 95))),
    metric("Chờ bắt đầu p95", fmtSeconds(percentile(waits, 95))),
  );
}

// ---------------------------------------------------------------- render
function upsert(g, front) {
  let j = jobs.get(g.id);
  if (!j) {
    j = { data: g, node: el("article", { class: "card job" }) };
    jobs.set(g.id, j);
    const list = $("jobs");
    if (front) list.prepend(j.node);
    else list.append(j.node);
  } else {
    j.data = g;
  }
  render(j);
}

function secondsBetween(a, b) {
  if (!a || !b) return null;
  return (new Date(b) - new Date(a)) / 1000;
}

function render(j) {
  const g = j.data;
  const bar = el("span");
  bar.style.width = `${g.status === "completed" ? 100 : Math.max(4, g.progress || 0)}%`;
  const wait = secondsBetween(g.created_at, g.started_at);
  const total = secondsBetween(g.created_at, g.completed_at);
  const children = [
    el("div", { class: "head" },
      el("div", { class: "spacer" },
        el("div", { class: "prompt", text: g.prompt }),
        el("div", { class: "meta", text: `${g.aspect_ratio} · ${g.number_of_images} ảnh · ${g.credit_cost} credit · ${new Date(g.created_at).toLocaleTimeString("vi-VN")}` })),
      badge(g.status)),
  ];
  if (!TERMINAL.has(g.status)) children.push(el("div", { class: "progress" }, bar));
  children.push(el("div", { class: "timeline" },
    el("span", {}, "Chờ máy: ", el("b", { text: fmtSeconds(wait) })),
    el("span", {}, "Tổng: ", el("b", { text: fmtSeconds(total) })),
    el("span", { text: `Credit: ${g.credit_state}` })));
  if (g.error_code) children.push(el("div", { class: "err", text: `${g.error_code}${g.error_message ? ` — ${g.error_message}` : ""}` }));
  if (g.assets && g.assets.length) {
    children.push(el("div", { class: "thumbs" }, g.assets.map((a) =>
      el("img", { src: a.thumbnail_url || a.url, alt: `Ảnh ${a.image_index + 1}`, loading: "lazy",
        onclick: () => openAsset(a.id) }))));
  }
  if (g.status === "queued" || g.status === "dispatched") {
    children.push(el("div", { class: "actions" }, el("button", {
      class: "secondary small",
      onclick: async () => {
        try {
          upsert(await session.call("POST", `/generations/${g.id}/cancel`), false);
        } catch (e) {
          toast(e.message, "error");
        }
      },
    }, "Huỷ")));
  }
  j.node.replaceChildren(...children);
}

async function openAsset(id) {
  try {
    const a = await session.call("GET", `/assets/${id}`); // fresh signed URL
    const img = el("img", { src: a.url, alt: "Ảnh gốc" });
    const box = el("div", { class: "lightbox", role: "dialog", "aria-label": "Xem ảnh", onclick: () => box.remove() },
      img);
    document.body.append(box);
  } catch (e) {
    toast(e.message, "error");
  }
}

// ------------------------------------------------------------------- boot
if (session.loggedIn) start();
else showAuth();
