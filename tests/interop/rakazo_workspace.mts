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
	async function fileTurn(prompt: string, label: string) {
		const id = await send(ctx, prompt, false);
		const deadline = Date.now() + 600000;
		let approvals = 0,
			reloaded = false;
		while (Date.now() < deadline) {
			const run = await runData(ctx, id);
			if (["succeeded", "failed", "cancelled"].includes(run.status)) {
				assert.equal(run.status, "succeeded", JSON.stringify(run.error));
				break;
			}
			if (run.status === "waiting_input") {
				if (!reloaded) {
					await page.reload();
					reloaded = true;
				}
				const allow = page.getByRole("button", {
					name: "Allow once",
					exact: true,
				});
				await expect(allow).toBeVisible({ timeout: 15000 });
				const response = page.waitForResponse((r: any) =>
					r.url().endsWith("/rpc/threads/answer"),
				);
				await allow.click();
				assert.equal((await response).status(), 200);
				approvals++;
			}
			await page.waitForTimeout(750);
		}
		assert.equal((await runData(ctx, id)).status, "succeeded");
		assert(approvals >= 2);
		const response = await ctx.request.get(
			"/api/v1/runs/" + id + "/tool-calls",
			{
				headers: { Authorization: "Bearer " + fixture.logins.alice.token },
			},
		);
		assert.equal(response.status(), 200);
		const calls = (await response.json()).items;
		for (const name of ["python_execute", "workspace_save", "open_path"])
			assert(
				calls.some((x: any) => x.name === name && x.status === "succeeded"),
				name,
			);
		await writeFile(
			path.join(output, label + "-tools.json"),
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
		await page.reload();
		await expect(
			page.getByRole("combobox", { name: "Message Chief" }),
		).toBeVisible();
		await page.screenshot({
			path: path.join(output, label + "-chat.png"),
			fullPage: true,
		});
		checks.push(label + "-native-send-approval-refresh");
	}
	for (const mode of ["team", "dedicated"]) {
		if (mode === "dedicated" && !fixture.keepModeHistory) {
			await rpc(ctx, "threads/clear", { botId });
			await page.reload();
			await expect(
				page.getByRole("combobox", { name: "Message Chief" }),
			).toBeVisible();
		}
		await rpc(ctx, "bots/setComputer", { botId, mode });
		await fileTurn(
			"使用PPT生成技能创建一页中文PPT，文件名" +
				mode +
				".pptx，标题“第一版验收”，正文“生成、保存、预览”。实际生成附件后保存到电脑并调用open_path预览，需要审批请等待，不要只提供大纲。",
			mode + "-first",
		);
		const before = await rpc(ctx, "artifacts/list", { botId });
		const old = before.find((a: any) => a.name === mode + ".pptx");
		assert(old);
		const original = await rpc(ctx, "artifacts/get", {
			botId,
			artifactId: old.id,
		});
		await fileTurn(
			"请修改刚才的" +
				mode +
				".pptx，使用该附件作为输入，将标题改为“第二版验收”，保持正文，用相同输入文件名导出为该附件的新版本，保存新版到电脑并open_path预览。",
			mode + "-second",
		);
		const after = await rpc(ctx, "artifacts/list", { botId });
		const latest = after.find((a: any) => a.name === mode + ".pptx");
		assert(latest);
		assert.notEqual(latest.id, old.id);
		const updated = await rpc(ctx, "artifacts/get", {
			botId,
			artifactId: latest.id,
		});
		assert.notEqual(updated.contentBase64, original.contentBase64);
		assert.equal(
			(await rpc(ctx, "artifacts/get", { botId, artifactId: old.id }))
				.contentBase64,
			original.contentBase64,
		);
		const versions = await rpc(ctx, "artifacts/listVersions", {
			familyId: latest.id,
		});
		assert(versions.length >= 2);
		assert(versions.some((v: any) => v.id === old.id));
		assert(versions.some((v: any) => v.id === latest.id));
		const artifactPage = await ctx.newPage();
		await artifactPage.goto("/app/artifacts/" + latest.id);
		const select = artifactPage.getByRole("combobox", {
			name: "Version",
			exact: true,
		});
		await expect(select).toBeVisible();
		const selected = artifactPage.waitForResponse((r: any) =>
			r.url().endsWith("/rpc/artifacts/getById"),
		);
		await select.selectOption(old.id);
		assert.equal((await selected).status(), 200);
		await expect
			.poll(
				async () =>
					(await rpc(ctx, "artifacts/getById", { artifactId: old.id }))
						.contentBase64,
			)
			.toBe(original.contentBase64);
		const downloadResponse = artifactPage.waitForEvent("download");
		await artifactPage
			.getByRole("button", { name: "Download", exact: true })
			.click();
		const download = await downloadResponse;
		assert.equal(
			(await readFile(await download.path())).toString("base64"),
			original.contentBase64,
		);
		await artifactPage.close();
		checks.push(mode + "-native-version-download-original-preserved");
		if (!(await page.getByTestId("computer-preview-open").isVisible()))
			await page.getByTitle("Agent computer", { exact: true }).click();
		await page.getByTestId("computer-preview-open").click();
		await expect(
			page.frameLocator("iframe").last().locator("canvas"),
		).toBeVisible({ timeout: 30000 });
		await page.screenshot({
			path: path.join(output, mode + "-preview.png"),
			fullPage: true,
		});
		await page
			.getByRole("button", { name: "Take control", exact: true })
			.click();
		await expect
			.poll(
				async () =>
					(await rpc(ctx, "computer/status", { botId })).controlHolder,
			)
			.toBe("user");
		await page
			.getByRole("button", { name: "Release control", exact: true })
			.click();
		await expect
			.poll(
				async () =>
					(await rpc(ctx, "computer/status", { botId })).controlHolder,
			)
			.toBe("none");
		await page
			.getByRole("button", { name: "Close computer", exact: true })
			.click();
		checks.push(mode + "-visible-preview-takeover-release");
	}
	for (const decision of ["deny", "cancel"]) {
		const id = await send(
			ctx,
			"请仅调用open_path重新预览刚才已保存的dedicated.pptx，不生成或修改任何文件，等待本次审批。",
			false,
		);
		await expect
			.poll(async () => (await runData(ctx, id)).status, { timeout: 180000 })
			.toBe("waiting_input");
		if (decision === "deny") {
			await page.getByRole("button", { name: "Deny", exact: true }).click();
			await expect
				.poll(async () => (await runData(ctx, id)).status, { timeout: 180000 })
				.toMatch(/^(succeeded|failed)$/);
		} else {
			await page.getByRole("button", { name: "Stop", exact: true }).click();
			await expect
				.poll(async () => (await runData(ctx, id)).status)
				.toBe("cancelled");
		}
		const response = await ctx.request.get(
			"/api/v1/runs/" + id + "/tool-calls",
			{ headers: { Authorization: "Bearer " + fixture.logins.alice.token } },
		);
		assert.equal(response.status(), 200);
		assert(
			!(await response.json()).items.some(
				(x: any) => x.name === "open_path" && x.status === "succeeded",
			),
		);
		checks.push("native-" + decision + "-no-preview-success");
	}
	const bob = await login("bob");
	const privateRead = await bob.ctx.request.get("/api/v1/runs/" + runs[0], {
		headers: { Authorization: "Bearer " + fixture.logins.bob.token },
	});
	assert.equal(privateRead.status(), 404);
	checks.push("second-user-run-isolation");
	await writeFile(
		path.join(output, "result.json"),
		JSON.stringify({ passed: true, checks, runs, errors }, null, 2),
	);
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
