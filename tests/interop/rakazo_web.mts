// Black-box browser verification owned by AgentForge; no files added to Rakazo.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdir, mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";

const root = process.env.AGENTFORGE_RAKAZO_ROOT;
assert(root, "Set AGENTFORGE_RAKAZO_ROOT to the pinned, installed Rakazo checkout");
const require = createRequire(path.join(root, "apps/web/package.json"));
const { chromium, expect } = require("@playwright/test");
const origin = process.env.AGENTFORGE_WEB_TEST_ORIGIN || "http://127.0.0.1:15173";
const output = process.env.AGENTFORGE_WEB_TEST_OUTPUT || await mkdtemp(path.join(tmpdir(), "agentforge-original-web-"));
await mkdir(output, {recursive:true});
const browser = await chromium.launch({channel: process.env.PLAYWRIGHT_CHANNEL || "chrome", headless:true});
const context = await browser.newContext({baseURL:origin, viewport:{width:1440, height:900}});
const page = await context.newPage();
const errors: string[] = [];
page.on("pageerror", (error: Error) => errors.push(error.message));
await page.addInitScript(() => localStorage.setItem("rakazo.uiLocale", "en"));
try {
  const fixture = await page.request.get("/api/__test__/fixture");
  assert(fixture.ok(), "Start the temporary AgentForge browser fixture first");
  const {testOnly, webLogins, tokens} = await fixture.json();
  assert.equal(testOnly, true);
  async function login(name: "alice" | "bob") {
    await page.goto("/sign-in");
    await expect(page.getByRole("heading", {name:"Sign in to Rakazo", exact:true})).toBeVisible();
    await page.getByLabel("Email", {exact:true}).fill(webLogins[name].email);
    await page.getByLabel("Password", {exact:true}).fill(webLogins[name].password);
    await page.getByRole("button", {name:"Continue with email", exact:true}).click();
    await expect(page.getByRole("combobox", {name:"Message Chief"})).toBeVisible({timeout:15000});
  }
  await page.goto("/sign-in");
  await page.getByLabel("Email", {exact:true}).fill(webLogins.alice.email);
  await page.getByLabel("Password", {exact:true}).fill("wrong-test-password");
  await page.getByRole("button", {name:"Continue with email", exact:true}).click();
  await expect(page.getByText("Invalid email or password", {exact:true})).toBeVisible();
  await page.screenshot({path:path.join(output,"native-login.png"), fullPage:true});
  await login("alice");
  assert((await page.getByTestId("agentforge-conversations").count()) === 0);
  await expect(page.getByRole("button", {name:"Attach file"})).toBeVisible();
  await expect(page.getByRole("button", {name:"Voice",exact:true})).toBeVisible();
  const composer = page.getByRole("combobox", {name:"Message Chief"});
  async function send(text: string, finish = true) {
    const response = page.waitForResponse((r: any) => r.url().endsWith("/rpc/threads/send"));
    await composer.fill(text);
    await page.getByRole("button", {name:"Send",exact:true}).click();
    const received = await response;
    assert.equal(received.status(),200);
    const {json} = await received.json();
    if (finish) {
      assert.equal((await (await page.request.post("/api/__test__/work")).json()).worked,true);
      await expect(page.getByRole("button", {name:"Stop",exact:true})).toHaveCount(0);
    }
    return json.runId;
  }
  const oldRun = await send("Original UI first report");
  await expect(page.getByTestId("message-bubble-frame")).toHaveCount(2);
  await page.getByTestId("bot-settings-trigger").click();
  const settings = page.getByTestId("bot-settings");
  const changed = page.waitForResponse((r:any) => r.url().endsWith("/rpc/bots/update"));
  await settings.getByRole("textbox", {name:"Description",exact:true}).fill("New instructions from the native settings form.");
  await settings.getByRole("textbox", {name:"Description",exact:true}).blur();
  assert.equal((await changed).status(),200);
  await page.getByRole("button", {name:"Close panel",exact:true}).click();
  const newRun = await send("Original UI second report");
  const headers = {Authorization:`Bearer ${tokens.alice}`};
  const run = await (await page.request.get(`/api/v1/runs/${newRun}`,{headers})).json();
  assert.equal(run.agent_version,2);
  assert.equal(run.execution_config.instructions,"New instructions from the native settings form.");
  const original = await (await page.request.get(`/api/v1/runs/${oldRun}`,{headers})).json();
  assert.equal(original.agent_version,1);
  await page.reload();
  await expect(page.getByTestId("message-bubble-frame")).toHaveCount(4);
  const queuedRun = await send("Clear queued work",false);
  await page.getByTestId("bot-settings-trigger").click();
  await settings.getByRole("button",{name:"Clear conversation",exact:true}).click();
  const cleared = page.waitForResponse((r:any)=>r.url().endsWith("/rpc/threads/clear"));
  await page.getByRole("alertdialog").getByRole("button",{name:"Clear",exact:true}).click();
  assert.equal((await cleared).status(),200);
  await expect(page.getByTestId("message-bubble-frame")).toHaveCount(0);
  await expect(page.getByRole("button",{name:"Stop",exact:true})).toHaveCount(0);
  assert.equal((await (await page.request.get(`/api/v1/runs/${queuedRun}`,{headers})).json()).status,"cancelled");
  assert.equal((await (await page.request.post("/api/__test__/work")).json()).worked,false);
  await page.getByRole("button", {name:"Close panel",exact:true}).click();
  await send("After native clear");
  await page.screenshot({path:path.join(output,"native-chat.png"), fullPage:true});
  await page.getByTestId("bot-settings-trigger").click();
  // Native settings show validation errors without an AgentForge UI extension.
  await settings.getByTestId("bot-settings-advanced").locator("summary").click();
  await settings.getByRole("switch",{name:"Read replies aloud"}).click();
  const unsupported = page.waitForResponse((r:any)=>r.url().endsWith("/rpc/bots/update"));
  await settings.getByRole("button",{name:"Save",exact:true}).click();
  assert.equal((await unsupported).status(),422);
  await expect(settings.getByText("Invalid procedure input",{exact:true})).toBeVisible();
  await page.getByRole("button", {name:"Close panel",exact:true}).click();
  await page.getByTestId("user-menu-trigger").click();
  await expect(page.getByRole("button",{name:"Board",exact:true})).toBeVisible();
  await page.getByRole("button",{name:"Log out",exact:true}).click();
  await login("bob");
  await expect(page.getByTestId("message-bubble-frame")).toHaveCount(0);
  await page.request.post("/api/__test__/revoke/bob");
  await page.reload();
  await expect(page.getByRole("heading",{name:"Sign in to Rakazo",exact:true})).toBeVisible();
  assert.deepEqual(errors, []);
  console.log("PASS: native password login, onboarding, two turns, latest config, clear/cancel, reload, isolation, revocation and unsupported responses");
  console.log("Screenshots:",output);
} catch(error) {
  await page.screenshot({path:path.join(output,"failure.png"),fullPage:true}).catch(()=>{});
  console.error("Browser verification failed; screenshots:", output);
  throw error;
} finally {
  await context.close();
  await browser.close();
}
