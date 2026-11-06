// Explicit real-model acceptance; credentials come only from the disposable runner.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import path from "node:path";
const root = process.env.AGENTFORGE_RAKAZO_ROOT!;
const require = createRequire(path.join(root, "apps/web/package.json"));
const { chromium, expect } = require("@playwright/test");
const fixture = JSON.parse(
	await readFile(process.env.AGENTFORGE_P0_FIXTURE!, "utf8"),
);
const output = process.env.AGENTFORGE_WEB_TEST_OUTPUT!;
await mkdir(output, { recursive: true });
const browser = await chromium.launch({
	channel: process.env.PLAYWRIGHT_CHANNEL || "chrome",
	headless: true,
});
const errors: string[] = [];
const checks: string[] = [];
const runs: string[] = [];
let page: any;
async function rpc(ctx: any, proc: string, input: object = {}, status = 200) {
	const response = await ctx.request.post("/rpc/" + proc, {
		headers: { Origin: fixture.origin },
		data: { json: input },
	});
	assert.equal(
		response.status(),
		status,
		proc + ": " + (await response.text()),
	);
	return (await response.json()).json;
}
async function login(name: string) {
	const ctx = await browser.newContext({
		baseURL: fixture.origin,
		viewport: { width: 1600, height: 1000 },
	});
	await ctx.addInitScript(() => {
		if (window !== window.top) return;
		localStorage.setItem("rakazo.uiLocale", "en");
		localStorage.setItem("rakazo.showToolActivity", "on");
		localStorage.setItem("rakazo.streamReplies", "on");
	});
	const p = await ctx.newPage();
	p.on("pageerror", (e: Error) => errors.push(e.message));
	await p.goto("/sign-in");
	await p.getByLabel("Email", { exact: true }).fill(fixture.logins[name].email);
	await p
		.getByLabel("Password", { exact: true })
		.fill(fixture.logins[name].password);
	await p
		.getByRole("button", { name: "Continue with email", exact: true })
		.click();
	await expect(p.getByRole("combobox", { name: "Message Chief" })).toBeVisible({
		timeout: 30000,
	});
	return { ctx, p, botId: new URL(p.url()).pathname.split("/").at(-1) };
}
async function runData(ctx: any, id: string) {
	const resp = await ctx.request.get("/api/v1/runs/" + id, {
		headers: { Authorization: "Bearer " + fixture.logins.alice.token },
	});
	assert.equal(resp.status(), 200);
	return resp.json();
}
async function send(ctx: any, text: string, finish = true) {
	const receipt = page.waitForResponse((r: any) =>
		r.url().endsWith("/rpc/threads/send"),
	);
	await page.getByRole("combobox", { name: "Message Chief" }).fill(text);
	await page.getByRole("button", { name: "Send", exact: true }).click();
	const response = await receipt;
	assert.equal(response.status(), 200);
	const id = (await response.json()).json.runId;
	runs.push(id);
	if (finish) {
		await expect
			.poll(async () => (await runData(ctx, id)).status, {
				timeout: 300000,
				intervals: [500, 1000, 2000],
			})
			.toMatch(/^(succeeded|failed|cancelled)$/);
		const run = await runData(ctx, id);
		assert.equal(run.status, "succeeded", JSON.stringify(run.error));
		await expect(
			page.getByRole("button", { name: "Stop", exact: true }),
		).toHaveCount(0, { timeout: 15000 });
	}
	return id;
}
try {
	const alice = await login("alice");
	page = alice.p;
	const { ctx, botId } = alice;
	assert(botId && botId !== "app");
	checks.push("native-login-onboarding");
	await page.getByTestId("bot-settings-trigger").click();
	const settings = page.getByTestId("bot-settings");
	await settings
		.getByTestId("bot-settings-advanced")
		.locator("summary")
		.click();
	const model = settings.locator('select[id$="-model"]');
	await expect(
		model.locator("option").filter({ hasText: "AgentForge Gateway" }),
	).toHaveCount(1, { timeout: 15000 });
	const options = await model
		.locator("option")
		.evaluateAll((els: any[]) =>
			els.map((e) => ({ value: e.value, text: e.textContent })),
		);
	const choice = options.find(
		(o: any) =>
			o.value &&
			(o.value.includes(fixture.model) || o.text.includes(fixture.model)),
	);
	assert(choice, "Configured model is selectable");
	await model.selectOption(choice.value);
	const saved = page.waitForResponse((r: any) =>
		r.url().endsWith("/rpc/bots/update"),
	);
	await settings.getByRole("button", { name: "Save", exact: true }).click();
	assert.equal((await saved).status(), 200);
	if (
		await page.getByRole("button", { name: "Close panel", exact: true }).count()
	)
		await page
			.getByRole("button", { name: "Close panel", exact: true })
			.click();
	checks.push("native-model-selection");
	const first = await send(
		ctx,
		"请记住本次验收代号是星河P0。必须调用 example_report 工具，把“星河P0验收”整理成报告，然后回复代号和工具结果。",
	);
	const toolResponse = await ctx.request.get(
		"/api/v1/runs/" + first + "/tool-calls",
		{ headers: { Authorization: "Bearer " + fixture.logins.alice.token } },
	);
	const toolBody = await toolResponse.json();
	assert(JSON.stringify(toolBody).includes("example_report"));
	checks.push("real-model-tool-loop");
	await send(ctx, "本次验收代号是什么？上一轮报告的原文是什么？请简短回答。");
	await expect(page.getByTestId("message-bot-bubble").last()).toContainText(
		"星河P0",
	);
	checks.push("real-multi-turn");
	const chooser = page.waitForEvent("filechooser");
	await page.getByRole("button", { name: "Attach file", exact: true }).click();
	await (await chooser).setFiles({
		name: "p0-input.txt",
		mimeType: "text/plain",
		buffer: Buffer.from("验收代号：星河P0。访问 Python 官网，返回真实来源。"),
	});
	await expect(page.getByText("p0-input.txt", { exact: true })).toBeVisible();
	const webRun = await send(
		ctx,
		"先调用 read_attachment 读取附件，再用电脑浏览器实际打开 https://www.python.org/ ，查找页面中的 Python 字样，再点击 About 链接。必须通过浏览器工具操作，不要凭记忆回答。返回实际打开页面的标题及来源链接，并使用 write_attachment 生成 p0-browser-result.txt 保存这两项结果。",
	);
	const calls = (
		await (
			await ctx.request.get("/api/v1/runs/" + webRun + "/tool-calls", {
				headers: { Authorization: "Bearer " + fixture.logins.alice.token },
			})
		).json()
	).items;
	await writeFile(
		path.join(output, "tool-summary.json"),
		JSON.stringify(
			calls.map((x: any) => ({
				name: x.name,
				status: x.status,
				error_code: x.error_code,
			})),
			null,
			2,
		),
	);
	for (const name of [
		"browser_open",
		"browser_find",
		"browser_follow",
		"write_attachment",
	])
		assert(
			calls.some((x: any) => x.name === name && x.status === "succeeded"),
			"Confirmed " + name,
		);
	checks.push("real-browser-open-find-follow-attachment");
	const artifacts = await rpc(ctx, "artifacts/list", { botId });
	const artifact = artifacts.find(
		(x: any) => x.name === "p0-browser-result.txt",
	);
	assert(artifact);
	const file = await rpc(ctx, "artifacts/get", {
		botId,
		artifactId: artifact.id,
	});
	const fileText = Buffer.from(file.contentBase64, "base64").toString("utf8");
	assert(fileText.includes("https://www.python.org/about/"));
	await writeFile(path.join(output, "p0-browser-result.txt"), fileText);
	checks.push("persisted-result-file");
	await page.reload();
	await expect(
		page.getByRole("combobox", { name: "Message Chief" }),
	).toBeVisible();
	await expect(page.getByTestId("tool-activity").last()).toBeVisible({
		timeout: 15000,
	});
	await page.getByTestId("tool-activity").last().locator("summary").click();
	await expect(page.getByTestId("tool-rows").last()).toContainText(
		"browser_follow",
	);
	await page.getByRole("button", { name: "Activity", exact: true }).click();
	await expect(
		page.getByRole("button", { name: "Chief, Done", exact: true }).first(),
	).toBeVisible();
	await page.screenshot({
		path: path.join(output, "chat-activity.png"),
		fullPage: true,
	});
	checks.push("activity-and-reload");
	await page.getByTitle("Agent computer", { exact: true }).click();
	await page.getByTestId("computer-preview-open").click();
	await expect(page.locator("iframe").last()).toBeVisible({ timeout: 30000 });
	const frame = page.frameLocator("iframe").last();
	await expect(frame.locator("canvas")).toBeVisible({ timeout: 30000 });
	await page.screenshot({
		path: path.join(output, "visible-computer.png"),
		fullPage: true,
	});
	checks.push("original-viewer-live-canvas");
	await rpc(ctx, "computer/screenUrl", { botId });
	await page.getByRole("button", { name: "Take control", exact: true }).click();
	await expect
		.poll(
			async () => (await rpc(ctx, "computer/status", { botId })).controlHolder,
		)
		.toBe("user");
	await page
		.getByRole("button", { name: "Release control", exact: true })
		.click();
	await expect
		.poll(
			async () => (await rpc(ctx, "computer/status", { botId })).controlHolder,
		)
		.toBe("none");
	checks.push("native-takeover-release");
	await page
		.getByRole("button", { name: "Close computer", exact: true })
		.click();
	const bob = await login("bob");
	const privateRead = await bob.ctx.request.get("/api/v1/runs/" + first, {
		headers: { Authorization: "Bearer " + fixture.logins.bob.token },
	});
	assert.equal(privateRead.status(), 404);
	const activity = await rpc(bob.ctx, "runs/list", { filter: "recent" });
	assert(!JSON.stringify(activity).includes(first));
	await expect(bob.p.getByTestId("message-bubble-frame")).toHaveCount(0);
	checks.push("second-user-private-history");
	// A Team computer is shared, but control remains exclusive to a login session.
	await rpc(ctx, "computer/takeover", { botId });
	await rpc(bob.ctx, "computer/takeover", { botId: bob.botId }, 409);
	await rpc(ctx, "computer/release", { botId });
	checks.push("team-control-exclusive");
	await rpc(ctx, "bots/setComputer", { botId, mode: "dedicated" });
	await rpc(ctx, "computer/boot", { botId });
	await rpc(ctx, "computer/takeover", { botId });
	await rpc(bob.ctx, "computer/takeover", { botId: bob.botId });
	await rpc(ctx, "computer/release", { botId });
	await rpc(bob.ctx, "computer/release", { botId: bob.botId });
	checks.push("private-control-independent");
	const cancel = await send(
		ctx,
		"请用电脑打开 https://www.python.org/ ，逐段阅读网页并总结。",
		false,
	);
	await page.getByRole("button", { name: "Stop", exact: true }).click();
	await expect
		.poll(async () => (await runData(ctx, cancel)).status)
		.toBe("cancelled");
	checks.push("native-cancel");
	await rpc(ctx, "computer/stop", { botId });
	await rpc(bob.ctx, "computer/stop", { botId: bob.botId });
	assert.deepEqual(errors, []);
	await writeFile(
		path.join(output, "result.json"),
		JSON.stringify(
			{ passed: true, checks, runs, model: fixture.model },
			null,
			2,
		),
	);
	console.log("PASS P0:", checks.join(", "));
} catch (error) {
	if (page) {
		await page
			.screenshot({ path: path.join(output, "failure.png"), fullPage: true })
			.catch(() => {});
		await writeFile(
			path.join(output, "failure-ui.txt"),
			await page
				.locator("body")
				.innerText()
				.catch(() => ""),
		);
		await writeFile(
			path.join(output, "failure-selects.json"),
			JSON.stringify(
				await page
					.locator("select")
					.evaluateAll((els: any[]) =>
						els.map((e) => ({ id: e.id, html: e.outerHTML })),
					),
			),
		);
	}
	await writeFile(
		path.join(output, "result.json"),
		JSON.stringify(
			{ passed: false, checks, runs, error: String(error) },
			null,
			2,
		),
	);
	throw error;
} finally {
	await browser.close();
}
