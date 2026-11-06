// Original Web acceptance against tests/interop/browser_server.py only.
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir, mkdtemp} from 'node:fs/promises';
import path from 'node:path';
import {tmpdir} from 'node:os';
const root=process.env.AGENTFORGE_RAKAZO_ROOT;
assert(root,'Set AGENTFORGE_RAKAZO_ROOT');
const require=createRequire(path.join(root,'apps/web/package.json'));
const {chromium,expect}=require('@playwright/test');
const origin=process.env.AGENTFORGE_WEB_TEST_ORIGIN || 'http://127.0.0.1:15173';
const output=process.env.AGENTFORGE_WEB_TEST_OUTPUT || await mkdtemp(path.join(tmpdir(),'agentforge-activity-'));
await mkdir(output,{recursive:true});
const browser=await chromium.launch({channel:process.env.PLAYWRIGHT_CHANNEL || 'chrome',headless:true});
try {
  const context=await browser.newContext({baseURL:origin,viewport:{width:1440,height:1000}});
  await context.addInitScript(()=>{
    localStorage.setItem('rakazo.uiLocale','en');
    localStorage.setItem('rakazo.showToolActivity','on');
  });
  const response=await context.request.get('/api/__test__/fixture');
  assert(response.ok(),'Start the isolated browser fixture first');
  const fixture=await response.json();assert.equal(fixture.testOnly,true);
  const page=await context.newPage();
  const errors:string[]=[];page.on('pageerror',(e:Error)=>errors.push(e.message));
  await page.goto('/sign-in');
  await page.getByLabel('Email',{exact:true}).fill(fixture.webLogins.alice.email);
  await page.getByLabel('Password',{exact:true}).fill(fixture.webLogins.alice.password);
  await page.getByRole('button',{name:'Continue with email',exact:true}).click();
  const composer=page.getByRole('combobox',{name:'Message Chief'});
  await expect(composer).toBeVisible({timeout:20000});
  const receipt=page.waitForResponse((r:any)=>r.url().endsWith('/rpc/threads/send'));
  await composer.fill('Activity acceptance report');
  await page.getByRole('button',{name:'Send',exact:true}).click();
  assert.equal((await receipt).status(),200);
  await page.getByRole('button',{name:'Activity',exact:true}).click();
  await expect(page.getByRole('button',{name:'Chief, Queued',exact:true})).toBeVisible();
  const worked=await context.request.post('/api/__test__/work');
  assert.equal((await worked.json()).worked,true);
  await expect(page.getByTestId('tool-activity')).toHaveCount(1);
  await page.getByTestId('tool-activity').locator('summary').click();
  await expect(page.getByTestId('tool-rows')).toContainText('example_report@1');
  await page.reload();
  await expect(page.getByRole('button',{name:'Chief, Done',exact:true})).toBeVisible();
  await expect(page.getByTestId('tool-activity')).toHaveCount(1);
  await page.getByTestId('tool-activity').locator('summary').click();
  await expect(page.getByTestId('tool-rows')).toContainText('example_report@1');
  await page.screenshot({path:path.join(output,'activity-after-reload.png'),fullPage:true});
  const queued=page.waitForResponse((r:any)=>r.url().endsWith('/rpc/threads/send'));
  await composer.fill('Cancel this queued task');
  await page.getByRole('button',{name:'Send',exact:true}).click();
  assert.equal((await queued).status(),200);
  await page.getByRole('button',{name:'Stop',exact:true}).click();
  await page.reload();
  await expect(page.getByRole('button',{name:'Chief, Cancelled',exact:true})).toBeVisible();
  await expect(page.getByTestId('tool-activity')).toHaveCount(1);
  // A fresh second user sees none of the first user's activity.
  const second=await browser.newContext({baseURL:origin,viewport:{width:1440,height:1000}});
  await second.addInitScript(()=>{localStorage.setItem('rakazo.uiLocale','en');localStorage.setItem('rakazo.activity-mode','1');});
  const login=await second.request.post('/api/auth/sign-in/email',{
    headers:{Origin:origin},data:fixture.webLogins.bob,
  });assert(login.ok());
  const other=await second.newPage();await other.goto('/app');
  await expect(other.getByRole('combobox',{name:'Message Chief'})).toBeVisible({timeout:20000});
  await expect(other.getByTestId('activity-list')).toHaveCount(0);
  assert.deepEqual(errors,[]);
  console.log('Original Web passed: queued, completed, cancelled, tool steps after reload and second-user isolation.');
} finally {await browser.close();}
