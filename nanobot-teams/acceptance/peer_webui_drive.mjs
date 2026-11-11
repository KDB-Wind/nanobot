import { createRequire } from "node:module";
import { execSync } from "node:child_process";
import { mkdirSync } from "node:fs";

const [url, shotsDir, packageJson, coordPort, wsToken, stopBot] = process.argv.slice(2);
const require = createRequire(packageJson);
const { chromium } = require("playwright");
mkdirSync(shotsDir, { recursive: true });

const browser = await chromium.launch({ channel: "msedge", headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.setDefaultTimeout(60_000);

async function connectOnce() {
  const password = page.locator("input[type=\"password\"]");
  if ((await password.count()) === 0) return;
  await password.first().fill(wsToken);
  await page.keyboard.press("Enter");
  await page.waitForTimeout(2_000);
}

async function openWorkbench() {
  await page.goto(url + "#/settings?section=teams", { waitUntil: "domcontentloaded" });
  await connectOnce();
  try {
    await page.locator("[data-teams-root]").waitFor({ timeout: 45_000 });
  } catch (error) {
    await connectOnce();
    try {
      await page.locator("[data-teams-root]").waitFor({ timeout: 30_000 });
    } catch (retryError) {
      await page.screenshot({ path: shotsDir + "/debug-route.png", fullPage: true });
      console.log("BODY:", (await page.locator("body").innerText()).slice(0, 600));
      throw retryError;
    }
  }
  try {
    await page.locator("[data-teams-usage]").waitFor({ timeout: 30_000 });
  } catch (error) {
    await page.screenshot({ path: shotsDir + "/debug-usage.png", fullPage: true });
    console.log("BODY:", (await page.locator("body").innerText()).slice(0, 800));
    throw error;
  }
}

let failures = 0;
const check = (name, ok) => {
  console.log(`${ok ? "PASS" : "FAIL"} ${name}`);
  if (!ok) failures += 1;
};

try {
  await page.addInitScript(() => {
    // Init scripts re-run on every load; only seed the default so a later
    // in-test locale change survives the reload.
    try {
      if (!localStorage.getItem("nanobot.locale")) localStorage.setItem("nanobot.locale", "en");
    } catch {}
  });
  await openWorkbench();

  await page
    .locator('[data-usage-bot="research"] [data-usage-rate]')
    .waitFor({ timeout: 60_000 })
    .catch(async (error) => {
      await page.screenshot({ path: shotsDir + "/debug-rate.png", fullPage: true });
      console.log("BODY:", (await page.locator("body").innerText()).slice(0, 1200));
      throw error;
    });
  await page.waitForFunction(() => {
    const cell = document.querySelector('[data-usage-bot="research"] [data-usage-rate]');
    return cell && /\d+%/.test(cell.textContent || "");
  });
  const edge = await page.locator('[data-peer-edge="research->writer"]').count();
  check("workbench shows bots, usage, and the peer edge", edge === 1);
  const route = await page.locator("[data-task-route]").first().textContent();
  check("a task route column renders", Boolean(route && route.trim()));
  const analyst = await page.locator('[data-usage-bot="analyst"]').textContent();
  check("missing usage shows a reason, not 0%", /missing/i.test(analyst || ""));
  await page.screenshot({ path: shotsDir + "/01-workbench-en.png", fullPage: true });

  await page.evaluate(() => {
    localStorage.setItem("nanobot.locale", "zh-CN");
  });
  // The locale is read at boot; a hash-only goto never reloads the app.
  await page.reload({ waitUntil: "domcontentloaded" });
  await connectOnce();
  await openWorkbench();
  await page.waitForFunction(() =>
    document.body.innerText.includes("每 Bot 用量"));
  await page.screenshot({ path: shotsDir + "/02-workbench-zh.png", fullPage: true });
  check("Chinese locale renders the usage panel", true);

  await page.setViewportSize({ width: 420, height: 820 });
  await page.waitForTimeout(500);
  await page.screenshot({ path: shotsDir + "/03-narrow.png", fullPage: true });
  check("narrow viewport stays usable",
    (await page.locator("[data-teams-usage]").count()) === 1);

  if (stopBot) {
    // The lab killed this worker, so a task addressed to it never leaves the
    // pending state: the stop control stays clickable and nothing races it.
    await page.evaluate(() => localStorage.setItem("nanobot.locale", "en"));
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto("about:blank");
    await page.goto(url + "#/settings?section=teams", { waitUntil: "domcontentloaded" });
    await connectOnce();
    await page.locator("[data-teams-root]").waitFor();
    const input = page.locator("[data-teams-root] textarea");
    await input.locator("xpath=preceding-sibling::select[1]").selectOption(stopBot);
    await input.fill("Summarise the archive in twenty paragraphs.");
    await input.locator("xpath=following-sibling::button[1]").click();
    const stop = page.locator("[data-stop-workflow]").first();
    await stop.waitFor({ timeout: 60_000 });
    const submitted = (await page.locator("[data-submit-state]").textContent()) || "";
    const taskId = (submitted.match(/task_[0-9a-f-]+/) || [null])[0];
    await page.screenshot({ path: shotsDir + "/05-stop-before.png", fullPage: true });
    await stop.click();
    const stopped = await page
      .waitForFunction(
        (id) => {
          const row = document.querySelector(`tr[data-task-id="${id}"]`);
          if (!row) return false;
          // A cell whose whole text is "confirmed"; "unconfirmed" must not count.
          return [...row.querySelectorAll("td")].some(
            (cell) => (cell.textContent || "").trim().toLowerCase() === "confirmed",
          );
        },
        taskId,
        { timeout: 60_000 },
      )
      .then(() => true)
      .catch(() => false);
    await page.screenshot({ path: shotsDir + "/05-stop-after.png", fullPage: true });
    console.log("stop target:", taskId);
    check("the workbench stop control cancels the pending task", stopped);
  }

  if (coordPort) {
    execSync(
      `pwsh -NoProfile -Command "Get-NetTCPConnection -LocalPort ${coordPort} -State Listen ` +
      `-ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ` +
      `ForEach-Object { Stop-Process -Id $_ -Force }"`,
      { stdio: "ignore" },
    );
    await page.evaluate(() => localStorage.setItem("nanobot.locale", "en"));
    await page.goto("about:blank");
    await page.goto(url + "#/settings?section=teams", { waitUntil: "domcontentloaded" });
    await page.locator("[data-teams-root]").waitFor();
    const sawError = await page
      .waitForFunction(
        () =>
          Boolean(document.querySelector("[data-usage-error]")) ||
          [...document.querySelectorAll('[role="alert"]')].length > 0,
        { timeout: 30_000 },
      )
      .then(() => true)
      .catch(() => false);
    await page.screenshot({ path: shotsDir + "/04-offline.png", fullPage: true });
    check("offline state surfaces an error without crashing", sawError);
  }
} finally {
  await browser.close();
}

console.log(failures === 0 ? "PASS peer_webui_drive" : `FAIL ${failures}`);
process.exit(failures === 0 ? 0 : 1);
