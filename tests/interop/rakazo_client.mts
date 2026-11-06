// Wire compatibility test using the user's installed, pinned Rakazo dependencies.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {createInterface} from 'node:readline';
import path from 'node:path';
const root = process.env.AGENTFORGE_RAKAZO_ROOT!;
const require = createRequire(path.join(root, 'apps/web/package.json'));
const {createORPCClient, ORPCError} = await import(pathToFileURL(require.resolve('@orpc/client')).href);
const {RPCLink} = await import(pathToFileURL(require.resolve('@orpc/client/fetch')).href);
const contracts = await import(pathToFileURL(path.join(root, 'packages/contracts/src/index.ts')).href);
const input = createInterface({input:process.stdin, terminal:false})[Symbol.asyncIterator]();
async function exchange(message: object) {
  process.stdout.write(JSON.stringify(message)+'\n');
  const next = await input.next();
  assert(!next.done, 'Python transport closed');
  return JSON.parse(next.value);
}
async function transport(input: Request | string, init?: RequestInit) {
  const request = new Request(input, init);
  const response = await exchange({type:'request', method:request.method, url:request.url,
    headers:Object.fromEntries(request.headers), body:await request.text()});
  return new Response(response.body, {status:response.status, headers:response.headers});
}
if (process.env.AGENTFORGE_BROWSER_INTEROP) {
  const {createAuthClient} = await import(pathToFileURL(require.resolve('better-auth/client')).href);
  const auth = createAuthClient({baseURL:'http://testserver', fetchOptions:{
    customFetchImpl:transport, headers:{Origin:'http://testserver'}}});
  if (process.env.AGENTFORGE_NATIVE_LOGIN) {
    const signedIn = await auth.signIn.email({email:'native@example.test', password:'test-native-password-only'});
    assert.equal(signedIn.error, null);
    assert.equal(signedIn.data?.user.id, 'alice');
  }
  const session = await auth.getSession();
  assert.equal(session.error, null);
  assert.equal(session.data?.user.id, 'alice');
  const browserRpc:any = createORPCClient(new RPCLink({url:'http://testserver/rpc', fetch:transport,
    headers:{Origin:'http://testserver'}}));
  assert.equal((await browserRpc.me()).userId, 'alice');
  const privateSkill = contracts.AgentSkillSchema.parse(await browserRpc.agentSkills.create({
    name:'Cookie recipe', description:'Native cookie write', body:'Only local sample content'
  }));
  assert.equal(privateSkill.source, 'user');
  assert.equal(privateSkill.readOnly, false);
  await browserRpc.agentSkills.remove({skillId:privateSkill.id});
  if (process.env.AGENTFORGE_MODEL_INTEROP) {
    const catalog = (await browserRpc.models.list()).map((item:any) => contracts.ModelCatalogEntrySchema.parse(item));
    const credentials = (await browserRpc.models.credentials()).map((item:any) => contracts.ModelCredentialSchema.parse(item));
    const {connectedModelChoices} = await import(pathToFileURL(path.join(root, 'packages/core/src/model-providers.ts')).href);
    assert.equal(connectedModelChoices(credentials, catalog).length, 2);
    await browserRpc.models.setDefault({provider:'chat_completions', modelId:'test-second-model', thinkingLevel:null});
    const bot = contracts.BotSchema.parse(await browserRpc.bots.create({name:'Gateway chat'}));
    assert.equal(bot.modelId, 'test-second-model');
    await browserRpc.bots.update({botId:bot.id, modelProvider:'chat_completions', modelId:'deepseek-v4-pro', thinkingLevel:null});
    const first = await browserRpc.threads.send({botId:bot.id, text:'First gateway question', clientNonce:'gateway-first'});
    // Changing settings after submit cannot change the queued request's model.
    await browserRpc.bots.update({botId:bot.id, modelProvider:'chat_completions', modelId:'test-second-model'});
    await exchange({type:'worker'});
    let thread = contracts.ThreadSnapshotSchema.parse(await browserRpc.threads.get({botId:bot.id}));
    assert.equal(thread.run?.status, 'completed');
    assert.equal(thread.run?.modelId, 'deepseek-v4-pro');
    assert.equal(thread.messages.length, 2);
    await browserRpc.threads.send({botId:bot.id, text:'Remember the first question', clientNonce:'gateway-second'});
    await exchange({type:'worker'});
    thread = contracts.ThreadSnapshotSchema.parse(await browserRpc.threads.get({botId:bot.id}));
    assert.equal(thread.run?.status, 'completed');
    assert.equal(thread.run?.modelId, 'test-second-model');
    assert.equal(thread.messages.length, 4);
    assert(JSON.stringify(thread).includes('Second context result'));
    await browserRpc.threads.send({botId:bot.id, text:'Error test', clientNonce:'gateway-third'});
    await exchange({type:'worker'});
    thread = contracts.ThreadSnapshotSchema.parse(await browserRpc.threads.get({botId:bot.id}));
    assert.equal(thread.run?.status, 'failed');
    assert.equal(thread.run?.error, 'model_auth_failed');
    assert(!JSON.stringify(thread).includes('private-upstream-details'));
    await assert.rejects(() => browserRpc.bots.update({botId:bot.id, modelProvider:'chat_completions', modelId:'unknown'}), (error:any) => error.status === 422);
    assert(first.runId);
  }
  await auth.signOut();
  assert.equal((await auth.getSession()).data, null);
  process.stdout.write(JSON.stringify({type:'complete', checks:'Better Auth session client and cookie RPC'})+'\n');
  process.exit(0);
}
const rpc:any = createORPCClient(new RPCLink({url:'http://testserver/rpc', fetch:transport,
  headers:{Authorization:'Bearer test-token-for-offline-checks-only-123456'}}));
if (process.env.AGENTFORGE_APPROVAL_INTEROP) {
  const {reduceThreadSnapshot} = await import(pathToFileURL(path.join(root,'apps/web/src/lib/thread-events.ts')).href);
  const bot = contracts.BotSchema.parse((await rpc.bots.list())[0]);
  let folded = contracts.ThreadSnapshotSchema.parse(await rpc.threads.get({botId:bot.id}));
  async function consume() {
    const events = await rpc.threads.subscribe({botId:bot.id,cursor:folded.cursor});
    for await (const raw of events) folded=reduceThreadSnapshot(folded,contracts.ProductEventSchema.parse(raw));
  }
  await exchange({type:'worker'}); await consume();
  assert.equal(folded.run?.status, 'waiting_input');
  const refreshed = contracts.ThreadSnapshotSchema.parse(await rpc.threads.get({botId:bot.id}));
  const card = refreshed.messages.find((m:any)=>m.blocks.some((b:any)=>b.kind==='ask'));
  assert(card);
  assert.deepEqual(card.blocks, folded.messages.find((m:any)=>m.id===card.id)?.blocks);
  const answer={botId:bot.id,runId:refreshed.run!.id,messageId:card.id,answer:'allow'};
  assert.deepEqual(await rpc.threads.answer(answer), {ok:true});
  assert.deepEqual(await rpc.threads.answer(answer), {ok:true});
  await consume();
  assert.equal(folded.messages.find((m:any)=>m.id===card.id)?.blocks[0].status, 'answered');
  await exchange({type:'worker'}); await consume();
  assert.equal(folded.activeRuns?.length ?? 0,0);
  assert.equal(contracts.ThreadSnapshotSchema.parse(await rpc.threads.get({botId:bot.id})).run?.status,'completed');
  assert.equal(folded.messages.at(-1).blocks[0].text,'Done');
  contracts.AppBootstrapSchema.parse(await rpc.bootstrap({botId:bot.id}));
  process.stdout.write(JSON.stringify({type:'complete'})+'\n'); process.exit(0);
}
if (process.env.AGENTFORGE_STREAM_INTEROP) {
  const {reduceThreadSnapshot} = await import(pathToFileURL(path.join(root,'apps/web/src/lib/thread-events.ts')).href);
  const {withLiveStreamingProgress} = await import(pathToFileURL(path.join(root,'packages/core/src/events.ts')).href);
  const bot = contracts.BotSchema.parse(await rpc.bots.create({name:'Streaming bot'}));
  await rpc.threads.send({botId:bot.id,text:'question'});
  let folded = contracts.ThreadSnapshotSchema.parse(await rpc.threads.get({botId:bot.id}));
  async function consume() {
    const events=await rpc.threads.subscribe({botId:bot.id,cursor:folded.cursor});
    for await(const raw of events) {
      const event=contracts.ProductEventSchema.parse(raw);
      folded=reduceThreadSnapshot(folded,event);
    }
  }
  const liveText=(snapshot:any)=>snapshot.messages.filter((m:any)=>m.id.startsWith('progress:')).flatMap((m:any)=>m.blocks).map((b:any)=>b.text??'').join('');
  await exchange({type:'stream',action:'first'}); await consume();
  assert.equal(liveText(folded),'Hello.');
  assert.equal(liveText(withLiveStreamingProgress(folded,false)),'');
  assert.equal(liveText(withLiveStreamingProgress(folded,true)),'Hello.');
  folded=contracts.ThreadSnapshotSchema.parse(await rpc.threads.get({botId:bot.id}));
  assert.equal(liveText(folded),'Hello.');
  await exchange({type:'stream',action:'more'}); await consume();
  assert.equal(liveText(folded),'Hello. World');
  await exchange({type:'stream',action:'retry'}); await consume();
  assert.equal(liveText(folded),'Fresh');
  await exchange({type:'stream',action:'finish'}); await consume();
  assert.equal(liveText(folded),'');
  assert.equal(folded.messages.length,2);
  assert.equal(folded.messages.at(-1).blocks[0].text,'Final');
  process.stdout.write(JSON.stringify({type:'complete'})+'\n'); process.exit(0);
}
const initial = await rpc.bootstrap({});
contracts.AppBootstrapSchema.parse(initial);
const bot = contracts.BotSchema.parse(await rpc.bots.create({name:'真实 oRPC 客户端', instructions:'test', spawnKey:'interop:first'}));
contracts.SpaceNavigationSchema.parse(await rpc.spaces.list());
const skillCatalog = (await rpc.agentSkills.list()).map((entry:any) => contracts.AgentSkillCatalogEntrySchema.parse(entry));
assert.equal(skillCatalog.length, 3);
const selectedSkill = skillCatalog.find((entry:any) => entry.id === 'builtin:document_summary@1');
assert(selectedSkill?.readOnly);
const skillDetail = contracts.AgentSkillSchema.parse(await rpc.agentSkills.get({skillId:selectedSkill.id}));
assert.equal((await rpc.agentSkills.get({name:selectedSkill.name})).id, selectedSkill.id);
assert(skillDetail.content.startsWith('---\n'));
const {serializeComposerPrompt} = await import(pathToFileURL(path.join(root, 'packages/core/src/composer-slash.ts')).href);
const {reduceThreadSnapshot: reduceActivity} = await import(pathToFileURL(path.join(root,'apps/web/src/lib/thread-events.ts')).href);
let foldedActivity = contracts.ThreadSnapshotSchema.parse(await rpc.threads.get({botId:bot.id}));
const firstText = serializeComposerPrompt('第一轮', selectedSkill, []);
const first = await rpc.threads.send({botId:bot.id, text:firstText, clientNonce:'interop-1'});
assert.equal(typeof first.taskId, 'string');
const activeActivity=contracts.RunsListOutputSchema.parse(await rpc.runs.list({filter:'active'}));
assert.equal(activeActivity.runs[0].runId,first.runId);
assert.equal(activeActivity.runs[0].status,'queued');
assert.deepEqual(await rpc.threads.send({botId:bot.id, text:firstText, clientNonce:'interop-1'}), first);
await exchange({type:'worker'});
const savedRun = await (await transport(new Request(`http://testserver/api/v1/runs/${first.runId}`, {
  headers:{Authorization:'Bearer test-token-for-offline-checks-only-123456'}
}))).json();
assert.equal(savedRun.execution_config.requested_skill, selectedSkill.id);
assert.equal(savedRun.status, 'succeeded');
const second = await rpc.threads.send({botId:bot.id, text:'第二轮', clientNonce:'interop-2'});
assert.notEqual(first.runId, second.runId);
await exchange({type:'worker'});
const snapshot = contracts.ThreadSnapshotSchema.parse(await rpc.threads.get({botId:bot.id}));
assert.equal(snapshot.messages.length, 4);
const recentActivity=contracts.RunsListOutputSchema.parse(await rpc.runs.list({filter:'recent'}));
assert.equal(recentActivity.runs.length,2);
assert(recentActivity.runs.every((r:any)=>r.status==='completed'));
assert.equal((await rpc.runs.list({filter:'active'})).runs.length,0);
assert(snapshot.messages.some((m:any)=>m.blocks.some((b:any)=>b.kind==='steps')));
assert.equal(snapshot.run?.status, 'completed');
contracts.AppBootstrapSchema.parse(await rpc.bootstrap({botId:bot.id}));
contracts.ThreadMessagePageSchema.parse(await rpc.threads.messages({botId:bot.id}));
const events = await rpc.threads.subscribe({botId:bot.id, cursor:0});
let cursor=0, completed=0;
for await (const value of events) {
  const event=contracts.ProductEventSchema.parse(value);
  assert(event.seq>cursor);
  cursor=event.seq;
  foldedActivity=reduceActivity(foldedActivity,event);
  if(event.type==='run.completed') completed++;
}
assert.equal(completed, 2);
assert.equal(cursor, snapshot.cursor);
assert.deepEqual(foldedActivity.messages.map((m:any)=>({id:m.id,blocks:m.blocks})), snapshot.messages.map((m:any)=>({id:m.id,blocks:m.blocks})));
await rpc.bots.update({botId:bot.id, instructions:'updated', memoryScope:null,
  autoSpeak:false, voiceId:null, modelProvider:null, modelId:null, thinkingLevel:null});
const oldPage = await rpc.agentforge.conversations.list({botId:bot.id});
assert.equal(oldPage.current.agentVersion, 1);
assert.equal(oldPage.agentVersion, 2);
const created = await rpc.agentforge.conversations.create({botId:bot.id, expectedThreadId:bot.threadId, requestId:crypto.randomUUID()});
assert.notEqual(created.threadId, bot.threadId);
const newPage = await rpc.agentforge.conversations.list({botId:bot.id});
assert.equal(newPage.current.agentVersion, 2);
assert.equal(contracts.ThreadSnapshotSchema.parse(await rpc.threads.get({botId:bot.id})).messages.length, 0);
await rpc.agentforge.conversations.select({botId:bot.id, expectedThreadId:created.threadId, threadId:bot.threadId});
assert.equal(contracts.ThreadSnapshotSchema.parse(await rpc.threads.get({botId:bot.id})).messages.length, 4);
assert.deepEqual(await rpc.threads.clear({botId:bot.id}), {ok:true});
const clearedEvents = await rpc.threads.subscribe({botId:bot.id, cursor:snapshot.cursor});
let clearCount=0;
for await (const value of clearedEvents) {
  const event=contracts.ProductEventSchema.parse(value);
  assert.equal(event.type,'thread.cleared');
  assert(event.seq>snapshot.cursor);
  clearCount++;
}
assert.equal(clearCount,1);
assert.equal(contracts.ThreadSnapshotSchema.parse(await rpc.threads.get({botId:bot.id})).messages.length, 0);
try {await rpc.computer.boot({botId:bot.id});assert.fail('unsupported procedure succeeded');}
catch(error) {assert(error instanceof ORPCError); assert.equal(error.code,'NOT_IMPLEMENTED');}
try {await rpc.bots.get({botId:'bad'});assert.fail('invalid id succeeded');}
catch(error) {assert(error instanceof ORPCError); assert.equal(error.status,422);}
const personal = contracts.AgentSkillSchema.parse(await rpc.agentSkills.create(
  contracts.CreateAgentSkillInput.parse({name:'Client recipe', description:'Personal steps', body:'First revision'})
));
assert.equal(personal.source, 'user');
assert.equal(personal.readOnly, false);
const personalPrompt = serializeComposerPrompt('Check these facts', personal, []);
const personalRun = await rpc.threads.send({botId:bot.id, text:personalPrompt, clientNonce:'personal-1'});
const edited = contracts.AgentSkillSchema.parse(await rpc.agentSkills.update(
  contracts.UpdateAgentSkillInput.parse({skillId:personal.id, body:'Second revision'})
));
assert.equal(edited.id, personal.id);
assert(edited.content.includes('Second revision'));
await exchange({type:'worker'});
const personalSaved = await (await transport(new Request(`http://testserver/api/v1/runs/${personalRun.runId}`, {
  headers:{Authorization:'Bearer test-token-for-offline-checks-only-123456'}
}))).json();
assert.equal(personalSaved.status, 'succeeded');
assert.equal(personalSaved.execution_config.skills[0].manifest.version, 1);
assert(personalSaved.execution_config.skills[0].manifest.content.includes('First revision'));
const latestRun = await rpc.threads.send({botId:bot.id, text:personalPrompt, clientNonce:'personal-2'});
assert.deepEqual(await rpc.agentSkills.remove({skillId:personal.id}), {ok:true});
await exchange({type:'worker'});
const latestSaved = await (await transport(new Request(`http://testserver/api/v1/runs/${latestRun.runId}`, {
  headers:{Authorization:'Bearer test-token-for-offline-checks-only-123456'}
}))).json();
assert.equal(latestSaved.execution_config.skills[0].manifest.version, 2);
assert.equal(latestSaved.status, 'succeeded');
try {await rpc.agentSkills.get({skillId:personal.id});assert.fail('removed skill returned');}
catch(error) {assert(error instanceof ORPCError);assert.equal(error.code,'NOT_FOUND');}
process.stdout.write(JSON.stringify({type:'complete', checks:'oRPC requests, Zod schemas, native skill catalog/CRUD/composer and immutable revisions, two turns, idempotency, SSE and errors'})+'\n');
process.exit(0);
