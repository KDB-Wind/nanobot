import { createRequire } from "node:module";
import { mkdirSync } from "node:fs";

const [url, nonce, shotsDir, packageJson] = process.argv.slice(2);
const require = createRequire(packageJson);
const { chromium } = require("playwright");
mkdirSync(shotsDir, { recursive: true });

const browser = await chromium.launch({
  channel: "msedge",
  headless: false,
  args: ["--start-maximized"],
});
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.setDefaultTimeout(60_000);
await page.addInitScript(() => {
  try {
    localStorage.setItem("nanobot.locale", "en");
  } catch {}
});

try {
  await page.goto(url + "#/apps", { waitUntil: "domcontentloaded" });
  await page.locator(".segmented-control").waitFor();
  await page.locator(".segmented-control button").nth(1).click();
  await page.screenshot({ path: shotsDir + "/01-apps.png" });

  const row = page.locator("article", { hasText: "Nanobot Teams" }).first();
  await row.waitFor();
  const before = (await row.innerText()).replace(/\s+/g, " ");
  console.log("apps before:", before.slice(0, 300));
  if (!/enabled/i.test(before)) {
    await row.locator("button").last().click();
    await page.waitForTimeout(3_000);
  }
  const after = (await row.innerText()).replace(/\s+/g, " ");
  console.log("apps after:", after.slice(0, 300));
  await page.screenshot({ path: shotsDir + "/02-apps-enabled.png" });

  await page.goto(url + "#/new", { waitUntil: "domcontentloaded" });
  const input = page.locator("textarea").last();
  await input.waitFor();
  await page.waitForFunction(() => {
    const areas = [...document.querySelectorAll("textarea")];
    return areas.some((area) => area.offsetParent !== null && !area.disabled);
  });

  const prompt = [
    "You must use the Teams tools. Call mcp_teams_list_bots once, then call",
    "mcp_teams_delegate_task with bot_id research and this exact task:",
    "",
    "Read the file secret.txt in your workspace with your file tool. Reply with only its exact contents and nothing else.",
    "",
    "Do not answer from your own knowledge. After the tool returns, reply with the returned content and nothing else.",
  ].join("\n");
  await input.fill(prompt);
  await page.screenshot({ path: shotsDir + "/03-prompt.png" });
  const send = page.getByRole("button", { name: "Send message" });
  if (await send.count()) {
    await send.first().click();
  } else {
    await input.press("Enter");
  }

  await page.waitForFunction(
    (value) => document.body.innerText.includes(value),
    nonce,
    { timeout: 240_000 },
  );
  await page.screenshot({ path: shotsDir + "/04-answer.png", fullPage: true });

  const body = await page.locator("body").innerText();
  console.log("nonce present:", body.includes(nonce));
  console.log("tool hint present:", body.includes("delegate_task"));
  console.log("PASS webui_delegate");
} finally {
  await browser.close();
}
