// Opt-in real Qwen + PostgreSQL + unmodified Rakazo Web acceptance.
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
const checks: string[] = [],
	records: any[] = [],
	errors: string[] = [];
let activePage: any;
function passed(name: string) {
	checks.push(name);
	console.log("VISION: " + name);
}
async function rpc(user: any, proc: string, data: object = {}, status = 200) {
	const response = await user.ctx.request.post("/rpc/" + proc, {
		headers: { Origin: fixture.origin },
		data: { json: data },
	});
	assert.equal(response.status(), status, proc + " status");
	return (await response.json()).json;
}
async function rest(user: any, suffix: string, status = 200) {
	const response = await user.ctx.request.get("/api/v1/" + suffix, {
		headers: { Authorization: "Bearer " + fixture.logins[user.name].token },
	});
	assert.equal(response.status(), status, suffix + " status");
	return response.json();
}
async function login(name: string) {
	const ctx = await browser.newContext({
		baseURL: fixture.origin,
		viewport: { width: 1600, height: 1100 },
	});
	await ctx.addInitScript(() => {
		if (window !== window.top) return;
		localStorage.setItem("rakazo.uiLocale", "en");
		localStorage.setItem("rakazo.showToolActivity", "on");
		localStorage.setItem("rakazo.streamReplies", "on");
	});
	const p = await ctx.newPage();
	activePage = p;
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
	return { ctx, p, name, botId: new URL(p.url()).pathname.split("/").at(-1) };
}
async function upload(user: any, filename: string) {
	const chooser = user.p.waitForEvent("filechooser");
	await user.p
		.getByRole("button", { name: "Attach file", exact: true })
		.click();
	await (await chooser).setFiles({
		name: filename,
		mimeType: "image/png",
		buffer: await readFile(fixture.challengeFile),
	});
	await expect(user.p.getByText(filename, { exact: true })).toBeVisible();
}
async function send(user: any, prompt: string) {
	activePage = user.p;
	const receipt = user.p.waitForResponse((r: any) =>
		r.url().endsWith("/rpc/threads/send"),
	);
	await user.p.getByRole("combobox", { name: "Message Chief" }).fill(prompt);
	await user.p.getByRole("button", { name: "Send", exact: true }).click();
	const response = await receipt;
	assert.equal(response.status(), 200);
	const id = (await response.json()).json.runId;
	records.push({ id, user: user.name });
	return id;
}
async function pending(user: any, id: string) {
	const items = (await rest(user, "runs/" + id + "/effects")).items;
	return items.find((x: any) => x.status === "pending");
}
async function answer(user: any, action = "Allow once") {
	const response = user.p.waitForResponse((r: any) =>
		r.url().endsWith("/rpc/threads/answer"),
	);
	await user.p.getByRole("button", { name: action, exact: true }).click();
	assert.equal((await response).status(), 200);
}
async function waitTerminal(user: any, id: string) {
	await expect
		.poll(async () => (await rest(user, "runs/" + id)).status, {
			timeout: 240000,
			intervals: [500, 1000, 2000],
		})
		.toMatch(/^(succeeded|failed|cancelled)$/);
	return rest(user, "runs/" + id);
}
async function journal(user: any, id: string, label: string) {
	const calls = (await rest(user, "runs/" + id + "/tool-calls")).items;
	const record = records.find((r) => r.id === id);
	Object.assign(record, {
		label,
		status: (await rest(user, "runs/" + id)).status,
		tools: calls.map((c: any) => ({
			name: c.name,
			status: c.status,
			error: c.error_code,
		})),
	});
	let index = 0;
	for (const call of calls)
		if (call.output?.desktop_image) {
			const screen = call.output.desktop_image;
			await writeFile(
				path.join(output, label + "-screen-" + index++ + ".png"),
				Buffer.from(screen.data, "base64"),
			);
		}
	return calls;
}
async function finishApproved(
	user: any,
	id: string,
	label: string,
	required: string[],
) {
	const answered = new Set<string>();
	let refreshed = false;
	const deadline = Date.now() + 600000;
	while (Date.now() < deadline) {
		const run = await rest(user, "runs/" + id);
		if (["succeeded", "failed", "cancelled"].includes(run.status)) break;
		if (run.status === "waiting_input") {
			const effect = await pending(user, id);
			assert(effect, "Pending approval must exist");
			assert(
				!answered.has(effect.effect_id),
				"An answered approval was repeated",
			);
			// Reload while paused; the same effect and arguments must remain fixed.
			if (!refreshed || effect.request.tool_reference === "computer_act@1") {
				await user.p.reload();
				await expect(
					user.p.getByRole("button", { name: "Allow once", exact: true }),
				).toBeVisible({ timeout: 30000 });
				assert.deepEqual((await pending(user, id)).request, effect.request);
				assert.equal((await pending(user, id)).effect_id, effect.effect_id);
				refreshed = true;
			}
			await answer(user);
			answered.add(effect.effect_id);
		}
		await user.p.waitForTimeout(600);
	}
	const run = await rest(user, "runs/" + id);
	const calls = await journal(user, id, label);
	assert.equal(run.status, "succeeded", JSON.stringify(run.error));
	for (const name of required)
		assert(
			calls.some((c: any) => c.name === name && c.status === "succeeded"),
			"Missing " + name,
		);
	const effects = (await rest(user, "runs/" + id + "/effects")).items;
	assert(
		effects.every(
			(e: any) => e.status === "succeeded" || e.status === "failed",
		),
	);
	assert.equal(
		new Set(calls.map((c: any) => c.call_id)).size,
		calls.length,
		"Duplicate tool IDs",
	);
	const snapshot = await rpc(user, "threads/get", { botId: user.botId });
	const finalMessage = snapshot.messages.find(
		(m: any) =>
			m.runId === id &&
			m.blocks.some((b: any) => b.kind === "text" && b.text === run.result),
	);
	assert(finalMessage, "Completed reply must be persisted in the thread");
	await user.p.reload();
	await expect(
		user.p
			.getByTestId("message-bot-bubble")
			.locator('[data-quote-message-id="' + finalMessage.id + '"]')
			.first(),
	).toBeVisible({ timeout: 30000 });
	await user.p.screenshot({
		path: path.join(output, label + "-chat.png"),
		fullPage: true,
	});
	return { run, calls, approvals: answered.size };
}
async function screenColors(p: any) {
	try {
		return await p
			.frameLocator("iframe")
			.last()
			.locator("canvas")
			.evaluate((canvas: HTMLCanvasElement) => {
				const colors = { red: 0, green: 0, white: 0 };
				if (canvas.width < 1000 || canvas.height < 700) return colors;
				const pixels = canvas
					.getContext("2d")!
					.getImageData(0, 0, canvas.width, canvas.height - 24).data;
				for (let i = 0; i < pixels.length; i += 64) {
					const [r, g, b] = [pixels[i], pixels[i + 1], pixels[i + 2]];
					if (r > 180 && g < 100 && b < 100) colors.red++;
					if (g > 100 && r < 100 && b < 100) colors.green++;
					if (r > 230 && g > 230 && b > 230) colors.white++;
				}
				return colors;
			});
	} catch {
		// A takeover replaces the iframe and reconnects noVNC; wait for a real frame.
		return { red: 0, green: 0, white: 0 };
	}
}
async function painted(p: any) {
	await expect
		.poll(async () => (await screenColors(p)).white, { timeout: 30000 })
		.toBeGreaterThan(1000);
}
async function openComputer(user: any) {
	const p = await user.ctx.newPage();
	activePage = p;
	p.on("pageerror", (e: Error) => errors.push(e.message));
	await p.goto("/app/" + user.botId);
	const toggle = p.getByTitle("Agent computer", { exact: true });
	await expect(toggle).toBeVisible({ timeout: 30000 });
	if ((await toggle.getAttribute("data-active")) === null) await toggle.click();
	await expect(toggle).toHaveAttribute("data-active", "");
	await p.getByTestId("computer-preview-open").click();
	await expect(p.frameLocator("iframe").last().locator("canvas")).toBeVisible({
		timeout: 30000,
	});
	await painted(p);
	return p;
}
async function control(user: any, p: any, take: boolean) {
	await p
		.getByRole("button", {
			name: take ? "Take control" : "Release control",
			exact: true,
		})
		.click();
	await expect
		.poll(
			async () =>
				(await rpc(user, "computer/status", { botId: user.botId }))
					.controlHolder,
		)
		.toBe(take ? "user" : "none");
	await expect(p.locator("iframe").last()).toHaveAttribute(
		"src",
		new RegExp("view_only=" + (take ? "false" : "true")),
	);
	await painted(p);
}
async function fullVision(user: any, mode: string) {
	await rpc(user, "bots/setComputer", { botId: user.botId, mode });
	await user.p.reload();
	const filename = user.name + "-" + mode + ".png";
	await upload(user, filename);
	const id = await send(
		user,
		"请先用launch_app启动浏览器，再把本次附件保存到电脑并open_path预览。用computer_observe实际查看版面：哪种颜色的面板越过内容边框、在哪一侧裁切？接着computer_act按Page_Down一次，依据操作前后截图回答下方新出现的面板颜色。不要读取图片字节或用Python推断。所有操作等待审批，最后用中文简短回答。",
	);
	const label = user.name + "-" + mode;
	const result = await finishApproved(user, id, label, [
		"launch_app",
		"workspace_save",
		"open_path",
		"computer_observe",
		"computer_act",
	]);
	assert(result.approvals >= 4);
	assert(
		["红", "右", "绿"].every((w) => result.run.result.includes(w)),
		"Visual comparison failed",
	);
	const saved = result.calls.find(
		(c: any) => c.name === "workspace_save" && c.status === "succeeded",
	);
	const pane = await openComputer(user);
	await pane.screenshot({
		path: path.join(output, label + "-desktop.png"),
		fullPage: true,
	});
	await pane.close();
	passed(label + "-vision-native-upload-send-approval-refresh");
	return {
		id,
		mode,
		artifactId: saved.arguments.artifact_id,
		path: saved.output.path,
	};
}
async function deniedRpc(user: any, proc: string, data: object) {
	const response = await user.ctx.request.post("/rpc/" + proc, {
		headers: { Origin: fixture.origin },
		data: { json: data },
	});
	assert(
		[403, 404].includes(response.status()),
		proc + " must deny another user's private resource",
	);
}
async function isolation(owner: any, other: any, resource: any) {
	for (const suffix of ["", "/tool-calls", "/effects"])
		await rest(other, "runs/" + resource.id + suffix, 404);
	await deniedRpc(other, "artifacts/get", {
		botId: owner.botId,
		artifactId: resource.artifactId,
	});
	await deniedRpc(other, "computer/screenUrl", { botId: owner.botId });
	const activity = await rpc(other, "runs/list", { filter: "recent" });
	assert(!JSON.stringify(activity).includes(resource.id));
	passed(
		owner.name +
			"-" +
			resource.mode +
			"-run-screen-attachment-approval-isolation",
	);
}
async function waitAction(user: any, id: string) {
	await expect
		.poll(async () => (await rest(user, "runs/" + id)).status, {
			timeout: 180000,
			intervals: [500, 1000],
		})
		.toBe("waiting_input");
	const effect = await pending(user, id);
	assert.equal(effect.request.tool_reference, "computer_act@1");
	await expect(
		user.p.getByRole("button", { name: "Allow once", exact: true }),
	).toBeVisible({ timeout: 15000 });
	return effect;
}
const actionPrompt =
	"请先computer_observe查看当前屏幕，然后仅用computer_act按Home一次并观察结果。不要打开应用、写文件或执行其他任务，等待本次审批。";
try {
	const alice = await login("alice"),
		bob = await login("bob");
	assert.notEqual(alice.botId, bob.botId);
	await expect(bob.p.getByTestId("message-bubble-frame")).toHaveCount(0);
	const team = await fullVision(alice, "team");
	await isolation(alice, bob, team);
	await rpc(bob, "computer/boot", { botId: bob.botId });
	let controller = await openComputer(alice);
	await control(alice, controller, true);
	await rpc(bob, "computer/takeover", { botId: bob.botId }, 409);
	await control(alice, controller, false);
	await controller.close();
	passed("team-shared-screen-control-exclusive");
	const privateAlice = await fullVision(alice, "dedicated");
	const privateBob = await fullVision(bob, "dedicated");
	await isolation(alice, bob, privateAlice);
	await isolation(bob, alice, privateBob);
	const listA = await rpc(alice, "artifacts/list", { botId: alice.botId });
	const listB = await rpc(bob, "artifacts/list", { botId: bob.botId });
	assert(!listA.some((x: any) => x.id === privateBob.artifactId));
	assert(!listB.some((x: any) => x.id === privateAlice.artifactId));
	const paneA = await openComputer(alice),
		paneB = await openComputer(bob);
	await control(alice, paneA, true);
	await control(bob, paneB, true);
	await control(alice, paneA, false);
	await control(bob, paneB, false);
	await paneA.close();
	await paneB.close();
	passed("private-desktops-independent-control");
	// Block an already-approved action by acquiring control while it is paused.
	const blocked = await send(alice, actionPrompt);
	const effect = await waitAction(alice, blocked);
	const waitingThread = await rpc(alice, "threads/get", { botId: alice.botId });
	const approvalMessage = waitingThread.messages.find((m: any) =>
		m.blocks.some((b: any) => b.approvalEffectId === effect.effect_id),
	);
	assert(
		approvalMessage,
		"Use the real approval card when checking another user",
	);
	await deniedRpc(bob, "threads/answer", {
		botId: alice.botId,
		runId: blocked,
		messageId: approvalMessage.id,
		answer: "allow",
	});
	assert.equal((await pending(alice, blocked)).effect_id, effect.effect_id);
	passed("another-user-cannot-answer-real-approval");
	controller = await openComputer(alice);
	await control(alice, controller, true);
	await answer(alice);
	const stopped = await waitTerminal(alice, blocked);
	assert.equal(stopped.status, "failed");
	const blockedCalls = await journal(alice, blocked, "takeover-blocked");
	assert(
		!blockedCalls.some(
			(c: any) => c.name === "computer_act" && c.status === "succeeded",
		),
	);
	assert.equal(
		(await rpc(alice, "computer/status", { botId: alice.botId })).controlHolder,
		"user",
	);
	// Actual noVNC keyboard input, in the controlled page rather than through an API action.
	await expect
		.poll(async () => (await screenColors(controller)).green, {
			timeout: 30000,
		})
		.toBeGreaterThan(500);
	const canvas = controller.frameLocator("iframe").last().locator("canvas");
	await canvas.click({ position: { x: 200, y: 250 } });
	await controller.keyboard.press("Home");
	await expect
		.poll(async () => (await screenColors(controller)).red, { timeout: 30000 })
		.toBeGreaterThan(500);
	passed("manual-mouse-keyboard-changes-remote-screen");
	await controller.screenshot({
		path: path.join(output, "manual-control.png"),
		fullPage: true,
	});
	await control(alice, controller, false);
	await controller.close();
	assert.equal((await rest(alice, "runs/" + blocked)).status, "failed");
	passed("takeover-blocks-agent-action-no-automatic-replay");
	const resumed = await send(alice, actionPrompt);
	await finishApproved(alice, resumed, "released-new-task", [
		"computer_observe",
		"computer_act",
	]);
	passed("release-allows-new-vision-task");
	for (const decision of ["deny", "cancel"]) {
		const id = await send(alice, actionPrompt);
		await waitAction(alice, id);
		await alice.p.reload();
		await expect(
			alice.p.getByRole("button", { name: "Allow once", exact: true }),
		).toBeVisible();
		if (decision === "deny") await answer(alice, "Deny");
		else
			await alice.p.getByRole("button", { name: "Stop", exact: true }).click();
		const run = await waitTerminal(alice, id);
		if (decision === "cancel") assert.equal(run.status, "cancelled");
		const calls = await journal(alice, id, "native-" + decision);
		assert(
			!calls.some(
				(c: any) => c.name === "computer_act" && c.status === "succeeded",
			),
		);
		passed("native-" + decision + "-prevents-desktop-action");
	}
	assert.deepEqual(errors, []);
	await writeFile(
		path.join(output, "result.json"),
		JSON.stringify({ passed: true, checks, records, errors }, null, 2),
	);
} catch (error) {
	if (activePage) {
		await activePage
			.screenshot({ path: path.join(output, "failure.png"), fullPage: true })
			.catch(() => {});
		await writeFile(
			path.join(output, "failure-ui.txt"),
			await activePage
				.locator("body")
				.innerText()
				.catch(() => ""),
		);
	}
	await writeFile(
		path.join(output, "result.json"),
		JSON.stringify(
			{ passed: false, checks, records, error: String(error), errors },
			null,
			2,
		),
	);
	throw error;
} finally {
	await browser.close();
}
