// Browser end-to-end check of the bundled web UIs (Playwright + Chromium).
//   npm i playwright && npx playwright install chromium     (once)
//   BASE=http://192.168.1.10:8000 ADMIN_EMAIL=... ADMIN_PASSWORD=... node scripts/ui-check.js
// Writes screenshots to ./ui-screenshots and the provisioning config it
// downloads to ./ui-screenshots/flowagent-config.json. Exit code 0 = all pass.
const { chromium, devices } = require("playwright");
const fs = require("fs");
const path = require("path");

const BASE = process.env.BASE || "http://localhost:8000";
const OUT = process.env.OUT || "ui-screenshots";
const results = [];
const check = (name, ok, detail = "") => {
  results.push({ name, ok: !!ok });
  console.log(`[${ok ? "PASS" : "FAIL"}] ${name}${!ok && detail ? ` — ${detail}` : ""}`);
};

function watchConsole(page, label, sink) {
  page.on("console", (m) => { if (m.type() === "error") sink.push(`${label}: ${m.text()}`); });
  page.on("pageerror", (e) => sink.push(`${label}: ${e.message}`));
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  // PW_ARGS lets CI/sandboxes bypass an HTTP proxy that cannot carry WebSockets,
  // e.g. PW_ARGS=--no-proxy-server
  const browser = await chromium.launch({ args: (process.env.PW_ARGS || "").split(" ").filter(Boolean) });
  const errors = [];

  // ------------------------------------------------------------ mobile client
  const mobile = await browser.newContext({ ...devices["iPhone 13"] });
  const page = await mobile.newPage();
  watchConsole(page, "client", errors);
  await page.goto(`${BASE}/app/`);
  const email = `ui-${Date.now()}@example.com`;
  await page.fill("#email", email);
  await page.fill("#password", "ui-check-password-1");
  await page.click("#btn-register");
  await page.waitForSelector("#main:not(.hidden)", { timeout: 15000 });
  await page.waitForFunction(() => document.querySelector("#credits-available").textContent === "10");
  check("client: register + login, 10 signup credits shown", true);
  await page.waitForFunction(() => document.querySelector("#conn-text").textContent === "Realtime", null, { timeout: 15000 });
  check("client: realtime WebSocket connected", true);

  await page.fill("#prompt", "A cinematic futuristic city at dusk");
  await page.selectOption("#count", "2");
  await page.click("#btn-create");
  const card = page.locator("article.job").first();
  await card.locator(".badge", { hasText: "Hoàn thành" }).waitFor({ timeout: 30000 });
  check("client: job reaches 'Hoàn thành' via realtime updates", true);
  await page.waitForFunction(() => {
    const imgs = document.querySelectorAll("article.job:first-child .thumbs img");
    return imgs.length === 2 && [...imgs].every((i) => i.complete && i.naturalWidth > 0);
  }, null, { timeout: 15000 });
  check("client: 2 thumbnails loaded from signed storage URLs", true);
  const credits = await page.textContent("#credits-available");
  check("client: credits settled (10 -> 8)", credits === "8", credits);
  await page.screenshot({ path: path.join(OUT, "client-mobile-completed.png") });

  await card.locator(".thumbs img").first().click();
  await page.waitForFunction(() => {
    const i = document.querySelector(".lightbox img");
    return i && i.complete && i.naturalWidth > 0;
  }, null, { timeout: 10000 });
  check("client: full-size image opens (fresh signed URL)", true);
  await page.screenshot({ path: path.join(OUT, "client-mobile-lightbox.png") });
  await page.click(".lightbox");

  // failure path: refund
  await page.fill("#prompt", "city");
  await page.click('.chip[data-token="[mock:fail]"]');
  await page.selectOption("#count", "1");
  await page.click("#btn-create");
  await page.locator("article.job").first().locator(".badge", { hasText: "Lỗi" }).waitFor({ timeout: 30000 });
  await page.waitForFunction(() => document.querySelector("#credits-available").textContent === "8");
  check("client: provider failure shown as 'Lỗi' and credit refunded", true);

  // stress test from the phone UI
  await page.selectOption("#stress-n", "5");
  await page.click("#btn-stress");
  await page.waitForFunction(() => {
    const v = document.querySelector("#stress-result .metric .v");
    return v && v.textContent === "5/5";
  }, null, { timeout: 60000 });
  const metrics = await page.$$eval("#stress-result .metric", (ms) => ms.map((m) => `${m.querySelector(".k").textContent}=${m.querySelector(".v").textContent}`));
  check(`client: stress 5 jobs all completed (${metrics.join(", ")})`, true);
  await page.screenshot({ path: path.join(OUT, "client-mobile-stress.png"), fullPage: true });

  // ----------------------------------------------------------- admin console
  const desk = await browser.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true });
  const admin = await desk.newPage();
  watchConsole(admin, "admin", errors);
  await admin.goto(`${BASE}/admin/`);
  await admin.fill("#email", process.env.ADMIN_EMAIL);
  await admin.fill("#password", process.env.ADMIN_PASSWORD);
  await admin.click("#btn-login");
  await admin.waitForSelector("#app:not(.hidden)", { timeout: 15000 });
  await admin.waitForSelector("#kpis .kpi");
  const online = await admin.locator("#kpis .kpi").first().locator(".v").textContent();
  check("admin: overview shows workstations online", /^[1-9]\d*\/\d+$/.test(online), online);
  await admin.waitForTimeout(500);
  await admin.screenshot({ path: path.join(OUT, "admin-overview.png"), fullPage: true });

  await admin.click('.nav[data-tab="workstations"]');
  await admin.locator("#ws-table tbody tr").first().waitFor();
  const wsRows = await admin.locator("#ws-table tbody tr").allInnerTexts();
  check("admin: a workstation row is ONLINE with agent version",
    wsRows.some((r) => /\bonline\b/i.test(r) && /1\.1\.0/.test(r)), wsRows.join(" | "));
  await admin.screenshot({ path: path.join(OUT, "admin-workstations.png"), fullPage: true });

  await admin.click('.nav[data-tab="sessions"]');
  await admin.locator("#sessions-table tbody tr").first().waitFor();
  const sessRow = await admin.locator("#sessions-table tbody tr").first().innerText();
  check("admin: provider session reported ACTIVE", /active/i.test(sessRow), sessRow);
  await admin.locator("#sessions-table tbody tr").first().locator("button", { hasText: "Kiểm tra lại phiên" }).click();
  await admin.locator(".toast", { hasText: "Đã gửi yêu cầu" }).waitFor({ timeout: 5000 });
  check("admin: 'Kiểm tra lại phiên' sends SESSION_CHECK", true);
  await admin.screenshot({ path: path.join(OUT, "admin-sessions.png"), fullPage: true });

  await admin.click('.nav[data-tab="jobs"]');
  await admin.locator("#jobs-table tbody tr").first().waitFor();
  const jobRows = await admin.locator("#jobs-table tbody tr").count();
  await admin.waitForFunction(() => [...document.querySelectorAll("#jobs-table img")].some((i) => i.complete && i.naturalWidth > 0));
  check(`admin: jobs list with result thumbnails (${jobRows} rows)`, jobRows >= 7);
  await admin.screenshot({ path: path.join(OUT, "admin-jobs.png"), fullPage: true });

  await admin.click('.nav[data-tab="provision"]');
  await admin.locator("#pv-accounts input[type=checkbox]").first().waitFor();
  await admin.fill("#pv-name", "WIN-UI-TEST");
  await admin.locator("#pv-accounts input[type=checkbox]").first().check();
  const [download] = await Promise.all([admin.waitForEvent("download"), admin.click("#provision-form button[type=submit]")]);
  const cfgPath = path.join(OUT, "flowagent-config.json");
  await download.saveAs(cfgPath);
  const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));
  check("admin: provisioning downloads installer config", cfg.EnrollmentToken && cfg.CaCertificatePem.includes("BEGIN CERTIFICATE")
    && cfg.ServerBaseUrl.startsWith("https://") && cfg.Accounts.length === 1, JSON.stringify(Object.keys(cfg)));
  await admin.screenshot({ path: path.join(OUT, "admin-provision.png"), fullPage: true });

  const csp = errors.filter((e) => /Content Security Policy|Refused to/i.test(e));
  check("no CSP violations", csp.length === 0, csp.join(" | "));
  check("no JavaScript console errors", errors.length === 0, errors.join(" | "));
  await browser.close();

  const failed = results.filter((r) => !r.ok).length;
  console.log(`\nUI RESULT: ${failed ? "FAIL" : "PASS"} (${results.length - failed}/${results.length} checks)`);
  process.exit(failed ? 1 : 0);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
