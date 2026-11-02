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
  const session = await auth.getSession();
  assert.equal(session.error, null);
  assert.equal(session.data?.user.id, 'alice');
  const browserRpc:any = createORPCClient(new RPCLink({url:'http://testserver/rpc', fetch:transport,
    headers:{Origin:'http://testserver'}}));
  assert.equal((await browserRpc.me()).userId, 'alice');
  await auth.signOut();
  assert.equal((await auth.getSession()).data, null);
  process.stdout.write(JSON.stringify({type:'complete', checks:'Better Auth session client and cookie RPC'})+'\n');
  process.exit(0);
}
const rpc:any = createORPCClient(new RPCLink({url:'http://testserver/rpc', fetch:transport,
  headers:{Authorization:'Bearer test-token-for-offline-checks-only-123456'}}));
const initial = await rpc.bootstrap({});
contracts.AppBootstrapSchema.parse(initial);
const bot = contracts.BotSchema.parse(await rpc.bots.create({name:'真实 oRPC 客户端', instructions:'test', spawnKey:'interop:first'}));
contracts.SpaceNavigationSchema.parse(await rpc.spaces.list());
const first = await rpc.threads.send({botId:bot.id, text:'第一轮', clientNonce:'interop-1'});
assert.equal(typeof first.taskId, 'string');
assert.deepEqual(await rpc.threads.send({botId:bot.id, text:'第一轮', clientNonce:'interop-1'}), first);
await exchange({type:'worker'});
const second = await rpc.threads.send({botId:bot.id, text:'第二轮', clientNonce:'interop-2'});
assert.notEqual(first.runId, second.runId);
await exchange({type:'worker'});
const snapshot = contracts.ThreadSnapshotSchema.parse(await rpc.threads.get({botId:bot.id}));
assert.equal(snapshot.messages.length, 4);
assert.equal(snapshot.run?.status, 'completed');
contracts.AppBootstrapSchema.parse(await rpc.bootstrap({botId:bot.id}));
contracts.ThreadMessagePageSchema.parse(await rpc.threads.messages({botId:bot.id}));
const events = await rpc.threads.subscribe({botId:bot.id, cursor:0});
let cursor=0, completed=0;
for await (const value of events) {
  const event=contracts.ProductEventSchema.parse(value);
  assert(event.seq>cursor);
  cursor=event.seq;
  if(event.type==='run.completed') completed++;
}
assert.equal(completed, 2);
assert.equal(cursor, snapshot.cursor);
try {await rpc.computer.boot({botId:bot.id});assert.fail('unsupported procedure succeeded');}
catch(error) {assert(error instanceof ORPCError); assert.equal(error.code,'NOT_IMPLEMENTED');}
try {await rpc.bots.get({botId:'bad'});assert.fail('invalid id succeeded');}
catch(error) {assert(error instanceof ORPCError); assert.equal(error.status,422);}
process.stdout.write(JSON.stringify({type:'complete', checks:'oRPC requests, Zod schemas, two turns, idempotency, SSE and errors'})+'\n');
process.exit(0);
