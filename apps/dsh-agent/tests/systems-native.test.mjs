import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn, execFileSync } from 'node:child_process';
import { createServer } from 'node:net';
import { mkdtemp, writeFile, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { randomBytes } from 'node:crypto';

// Explicit opt-in real MySQL test. Test schema retained; never touches existing business rows.
test('native DSH logs into real Data Platform and Forge cores with separate grants and RBAC', {timeout:90000,skip:!process.env.DSH_DP_TEST_ENV||!process.env.DSH_DP_CORE||!process.env.FORGE_TEST_SOURCE||!process.env.FORGE_TEST_PYTHON},async t=>{
 const root=await mkdtemp(join(tmpdir(),'systems-native-'));
 const dpCore=process.env.DSH_DP_CORE,require=createRequire(dpCore);
 const env=require('dotenv').parse(await readFile(process.env.DSH_DP_TEST_ENV));
 const mysql=require('mysql2/promise');const db=await mysql.createConnection({host:env.DB_HOST,port:env.DB_PORT,user:env.DB_USER,password:env.DB_PASSWORD});
 const dbName='dsh_auth_test_'+randomBytes(6).toString('hex');
 const identifier=s=>'`'+s.replaceAll('`','``')+'`';
 const password=randomBytes(24).toString('base64url'), viewerPassword=randomBytes(24).toString('base64url'), master=randomBytes(24).toString('base64url');
 try{
  await db.query('CREATE DATABASE '+identifier(dbName));
  for(const table of ['users','system_roles','auth_sessions','project_spaces','project_members','access_control_config'])await db.query(`CREATE TABLE ${identifier(dbName)}.${identifier(table)} LIKE ${identifier(env.DB_NAME)}.${identifier(table)}`);
  await db.query(`INSERT INTO ${identifier(dbName)}.access_control_config (id,version,config_json) VALUES (1,0,?)`,[JSON.stringify({attributes:[],scopes:[],policies:[],assignments:[]})]);
  await db.query(`INSERT INTO ${identifier(dbName)}.system_roles SELECT * FROM ${identifier(env.DB_NAME)}.system_roles WHERE id IN (1,4)`);
  await db.query(`INSERT INTO ${identifier(dbName)}.users (id,username,password_hash,display_name,role_id,role_code,default_project_id) VALUES (1,'integration_admin',?,'Integration Admin',1,'admin',1)`,[await require('bcryptjs').hash(password,10)]);
  await db.query(`INSERT INTO ${identifier(dbName)}.project_spaces (id,project_name,project_code,owner_user_id) VALUES (1,'Isolated login test','login_test',1)`);
  await db.query(`INSERT INTO ${identifier(dbName)}.project_members (project_id,user_id,project_role) VALUES (1,1,'admin')`);
 }finally{await db.end();}
 const forgeDb=join(root,'forge.sqlite');
 execFileSync(process.env.FORGE_TEST_PYTHON,['-c',"import sys,json; from cli_anything.devops.auth import AuthCore; d=json.load(sys.stdin); AuthCore(d['db']).bootstrap(d['username'],d['password'])"],{env:{...process.env,PYTHONPATH:process.env.FORGE_TEST_SOURCE},input:JSON.stringify({db:forgeDb,username:'integration_admin',password}),stdio:['pipe','pipe','pipe']});
 const configPath=join(root,'providers.json');await writeFile(configPath,JSON.stringify([{id:'dataplatform',label:'Data Platform',type:'dataplatform',url:'http://127.0.0.1:46120',corePath:dpCore},{id:'forge',label:'Forge DevOps',type:'forge',url:'http://127.0.0.1:8766',python:process.env.FORGE_TEST_PYTHON,sourceRoot:process.env.FORGE_TEST_SOURCE,dbPath:forgeDb}]));
 const probe=createServer();await new Promise(r=>probe.listen(0,'127.0.0.1',r));const port=probe.address().port;await new Promise(r=>probe.close(r));
 const child=spawn(process.execPath,[fileURLToPath(new URL('../src/cli.mjs',import.meta.url)),'web','--systems',configPath,'--data-dir',root,'--port',String(port),'--no-open'],{env:{...env,PATH:process.env.PATH,HOME:root,DB_NAME:dbName,JWT_SECRET:randomBytes(32).toString('hex')},stdio:['ignore','pipe','pipe']});let logs='';child.stdout.on('data',b=>logs+=b);child.stderr.on('data',b=>logs+=b);const exited=new Promise(r=>child.once('exit',r));t.after(async()=>{child.kill('SIGTERM');await exited;});
 const deadline=Date.now()+30000;while(!logs.includes(`dsh web: http://127.0.0.1:${port}`)){assert.equal(child.exitCode,null,logs);if(Date.now()>deadline)throw Error(logs);await new Promise(r=>setTimeout(r,50));}
 const origin=`http://127.0.0.1:${port}`;const post=async(action,data)=>{const r=await fetch(origin+action,{method:'POST',headers:{origin,'content-type':'application/json'},body:JSON.stringify(data)});return r.json();};
 assert.equal((await post('/systems/status',{systemId:'forge'})).error,'VAULT_LOCKED');
 await post('/vault/initialize',{password:master,confirmation:master});
 const {chromium}=await import('playwright-core');const browser=await chromium.launch({headless:true,executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'});t.after(()=>browser.close());const page=await browser.newPage();const errors=[];page.on('pageerror',e=>errors.push(e.message));await page.goto(origin+'/systems');await page.waitForFunction(()=>document.querySelector('#system').options.length===2);
 for(const system of ['dataplatform','forge']){
  await page.locator('#system').selectOption(system);await page.locator('#username').fill('integration_admin');await page.locator('#password').fill(randomBytes(16).toString('hex'));await page.getByRole('button',{name:'登录',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#feedback').textContent.includes('失败'));
  await page.locator('#password').fill(password);await page.getByRole('button',{name:'登录',exact:true}).click();await page.getByText('登录成功。',{exact:true}).waitFor();await page.waitForFunction(()=>document.querySelector('#status').textContent.startsWith('已登录'));
  assert.equal(await page.locator('#password').inputValue(),'');const state=await post('/systems/status',{systemId:system});assert.equal(state.authorized,true);assert.equal(state.user.username,'integration_admin');
  await page.locator('#command').selectOption(system==='forge'?'role.list':'project.list');await page.getByRole('button',{name:'执行命令',exact:true}).click();await page.waitForFunction(()=>document.querySelector('#feedback').textContent!=='正在执行…');assert.equal(await page.locator('#feedback').textContent(),'操作完成。',system+': '+await page.locator('#result').textContent());assert.doesNotMatch(await page.locator('#result').textContent(),/"error"/);
 }
 const user=await post('/systems/execute',{systemId:'forge',command:'user.create',input:{username:'integration_viewer',role:'viewer'},userPassword:viewerPassword});assert.equal(user.username,'integration_viewer');
 await post('/systems/logout',{systemId:'forge'});assert.equal((await post('/systems/status',{systemId:'dataplatform'})).authorized,true);
 assert.equal((await post('/systems/login',{systemId:'forge',url:'http://127.0.0.1:8766',username:'integration_viewer',password:viewerPassword})).status,'authorized');
 assert.equal((await post('/systems/execute',{systemId:'forge',command:'object.create',input:{kind:'project',fields:{key:'DENY',name:'Must not create'}}})).error,'DP_PERMISSION_DENIED');
 await post('/systems/logout',{systemId:'forge'});assert.equal((await post('/systems/execute',{systemId:'forge',command:'project.list'})).error,'DP_LOGIN_REQUIRED');
 await page.setViewportSize({width:390,height:800});await page.screenshot({path:join(root,'systems-mobile.png'),fullPage:true});
 for(const secret of [password,viewerPassword,master]){assert.equal(logs.includes(secret),false);assert.equal((await readFile(join(root,'standalone-web.patch.yml'),'utf8')).includes(secret),false);}
 assert.deepEqual(errors,[]);t.diagnostic(`Real core login PASS; isolated MySQL schema ${dbName}, Forge DB and browser evidence ${root}. Retained without deletion.`);
});
