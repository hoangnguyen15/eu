// Shared client for the bundled web UIs: auth with refresh-token rotation,
// JSON envelope handling, and a resilient realtime channel (WS + replay +
// REST fallback). No dependencies, ES module.

const API = "/api/v1";

export class ApiError extends Error {
  constructor(status, code, message, details) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

export class Session {
  constructor(storageKey) {
    this.key = storageKey;
    this.tokens = null;
    this._refreshing = null;
    try {
      this.tokens = JSON.parse(sessionStorage.getItem(this.key) || "null");
    } catch {
      this.tokens = null;
    }
  }

  get loggedIn() {
    return !!(this.tokens && this.tokens.access_token);
  }

  save(tokens) {
    this.tokens = tokens;
    try {
      if (tokens) sessionStorage.setItem(this.key, JSON.stringify(tokens));
      else sessionStorage.removeItem(this.key);
    } catch {
      /* storage unavailable: keep in memory */
    }
  }

  async login(email, password) {
    const data = await request("POST", "/auth/login", { email, password });
    this.save(data);
    return data;
  }

  async register(email, password) {
    return request("POST", "/auth/register", { email, password });
  }

  async logout() {
    if (this.tokens) {
      try {
        await this.call("POST", "/auth/logout", { refresh_token: this.tokens.refresh_token });
      } catch {
        /* ignore */
      }
    }
    this.save(null);
  }

  async refresh() {
    if (!this.tokens) throw new ApiError(401, "NOT_LOGGED_IN", "Not logged in");
    if (!this._refreshing) {
      this._refreshing = request("POST", "/auth/refresh", { refresh_token: this.tokens.refresh_token })
        .then((t) => this.save(t))
        .catch((e) => {
          this.save(null);
          throw e;
        })
        .finally(() => {
          this._refreshing = null;
        });
    }
    return this._refreshing;
  }

  /** Authenticated call; transparently refreshes once on 401. */
  async call(method, path, body, headers = {}) {
    if (!this.tokens) throw new ApiError(401, "NOT_LOGGED_IN", "Not logged in");
    try {
      return await request(method, path, body, { ...headers, Authorization: `Bearer ${this.tokens.access_token}` });
    } catch (e) {
      if (e.status !== 401 || !this.tokens) throw e;
      await this.refresh();
      return request(method, path, body, { ...headers, Authorization: `Bearer ${this.tokens.access_token}` });
    }
  }
}

export async function request(method, path, body, headers = {}) {
  const opts = { method, headers: { ...headers } };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(API + path, opts);
  } catch (e) {
    throw new ApiError(0, "NETWORK_ERROR", "Không kết nối được máy chủ");
  }
  let json = null;
  try {
    json = await res.json();
  } catch {
    /* non-JSON */
  }
  if (!res.ok || (json && json.success === false)) {
    const err = (json && json.error) || {};
    throw new ApiError(res.status, err.code || `HTTP_${res.status}`, err.message || res.statusText, err.details);
  }
  return json ? json.data : null;
}

/**
 * Realtime events with automatic reconnect (exponential backoff), replay of
 * missed events via ?since=<cursor>, token refresh on 4401, and a callback so
 * the page can fall back to REST polling while disconnected.
 */
export class EventStream {
  constructor(session, { onEvent, onState }) {
    this.session = session;
    this.onEvent = onEvent;
    this.onState = onState || (() => {});
    this.cursor = null;
    this.ws = null;
    this.stopped = false;
    this.attempt = 0;
  }

  start() {
    this.stopped = false;
    this._connect();
  }

  stop() {
    this.stopped = true;
    if (this.ws) this.ws.close();
  }

  _connect() {
    if (this.stopped || !this.session.loggedIn) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const params = new URLSearchParams({ access_token: this.session.tokens.access_token });
    if (this.cursor) params.set("since", this.cursor);
    this.onState(this.attempt ? "reconnecting" : "connecting");
    const ws = new WebSocket(`${proto}://${location.host}/ws/v1/events?${params}`);
    this.ws = ws;
    ws.onmessage = (m) => {
      let evt;
      try {
        evt = JSON.parse(m.data);
      } catch {
        return;
      }
      if (evt.event === "connection.ready") {
        this.attempt = 0;
        this.onState("live");
        return;
      }
      if (evt.cursor) this.cursor = evt.cursor;
      if (evt.event === "ping" || evt.event === "pong") return;
      this.onEvent(evt);
    };
    ws.onclose = async (e) => {
      this.ws = null;
      if (this.stopped) return;
      this.onState("offline");
      if (e.code === 4401) {
        try {
          await this.session.refresh();
        } catch {
          this.onState("logged-out");
          return;
        }
      }
      this.attempt += 1;
      const delay = Math.min(15000, 500 * 2 ** Math.min(this.attempt, 5)) * (0.6 + Math.random() * 0.4);
      setTimeout(() => this._connect(), delay);
    };
  }
}

// ------------------------------------------------------------------ helpers
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(node.dataset, v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export function uuid() {
  if (crypto.randomUUID) return crypto.randomUUID();
  return ([1e7] + -1e3 + -4e3 + -8e3 + -1e11).replace(/[018]/g, (c) =>
    (c ^ (crypto.getRandomValues(new Uint8Array(1))[0] & (15 >> (c / 4)))).toString(16));
}

export function ago(iso) {
  if (!iso) return "—";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)} giây trước`;
  if (s < 3600) return `${Math.round(s / 60)} phút trước`;
  if (s < 86400) return `${Math.round(s / 3600)} giờ trước`;
  return new Date(iso).toLocaleString("vi-VN");
}

export function fmtSeconds(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v < 10 ? `${v.toFixed(2)}s` : `${v.toFixed(1)}s`;
}

export const STATUS_LABEL = {
  queued: "Đang chờ",
  dispatched: "Đã giao máy",
  running: "Đang tạo",
  uploading: "Đang tải lên",
  completed: "Hoàn thành",
  failed: "Lỗi",
  cancelled: "Đã huỷ",
  auth_required: "Cần đăng nhập lại",
  provider_limited: "Bị giới hạn",
  review_required: "Cần xem xét",
};

export const TERMINAL = new Set(["completed", "failed", "cancelled", "auth_required", "provider_limited",
  "review_required"]);

export function badge(status) {
  const s = (status || "").toLowerCase();
  return el("span", { class: `badge st-${s}`, text: STATUS_LABEL[s] || s });
}

export function toast(message, kind = "info") {
  let host = document.getElementById("toasts");
  if (!host) {
    host = el("div", { id: "toasts", class: "toasts", role: "status", "aria-live": "polite" });
    document.body.append(host);
  }
  const t = el("div", { class: `toast ${kind}`, text: message });
  host.append(t);
  setTimeout(() => t.remove(), 4500);
}

export function percentile(values, p) {
  if (!values.length) return NaN;
  const s = [...values].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.max(0, Math.round((p / 100) * (s.length - 1))))];
}
