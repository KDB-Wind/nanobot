import { createRequire } from "node:module";
import { execSync } from "node:child_process";
import { mkdirSync } from "node:fs";

const [url, shotsDir, packageJson, coordPort, wsToken] = process.argv.slice(2);
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
    try { localStorage.setItem("nanobot.locale", "en"); } catch {}
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

  if (coordPort) {
    execSync(
      `pwsh -NoProfile -Command "Get-NetTCPConnection -LocalPort ${coordPort} -State Listen ` +
      `-ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique | ` +
      `ForEach-Object { Stop-Process -Id $_ -Force }"`,
      { stdio: "ignore" },
    );
    await page.evaluate(() => localStorage.setItem("nanobot.locale", "en"));
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
