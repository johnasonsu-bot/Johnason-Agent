import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createServer} from 'node:http';
import {chromium} from 'playwright-core';
test('JSON login validates target, reports status and clears credentials', async()=>{
 const html=await readFile(new URL('../public/dataplatform.html',import.meta.url));let payload;let authorized=false;
 const server=createServer(async(req,res)=>{res.setHeader('content-type',req.url==='/'?'text/html':'application/json');if(req.url==='/')return res.end(html);if(req.url.endsWith('/login')){let body='';for await(const chunk of req)body+=chunk;payload=JSON.parse(body);authorized=true;return res.end(JSON.stringify({status:'authorized'}));}res.end(JSON.stringify({authorized,user:{username:'admin'},commands:['project.list']}));});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));const browser=await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
 try{const page=await browser.newPage();await page.goto(`http://127.0.0.1:${server.address().port}/`);
 await page.getByText('尚未登录 Data Platform。',{exact:true}).waitFor();
 await page.getByRole('button',{name:'登录',exact:true}).click();await page.getByText('请填写平台用户名和密码。',{exact:true}).waitFor();
 await page.getByLabel('平台网址',{exact:true}).fill('http://127.0.0.1:1234');await page.getByLabel('平台用户名',{exact:true}).fill('admin');await page.getByLabel('平台密码',{exact:true}).fill('synthetic-only');await page.getByRole('button',{name:'登录',exact:true}).click();await page.getByText('此网址尚未配置本地共享核心接入，请使用 http://127.0.0.1:46120。',{exact:true}).waitFor();assert.equal(payload,undefined);
 await page.getByLabel('平台网址',{exact:true}).fill('http://127.0.0.1:46120');await page.getByLabel('平台密码',{exact:true}).fill('synthetic-only');await page.locator('summary').click();await page.getByRole('button',{name:'生成登录 JSON',exact:true}).click();const parsed=JSON.parse(await page.locator('#loginJson').inputValue());assert.deepEqual(parsed,{url:'http://127.0.0.1:46120',username:'admin',password:'synthetic-only'});
 await page.getByRole('button',{name:'使用 JSON 登录',exact:true}).click();await page.getByText('登录成功，当前平台账号：admin',{exact:true}).waitFor();assert.deepEqual(payload,parsed);assert.equal(await page.locator('#loginJson').inputValue(),'');assert.equal(await page.locator('#password').inputValue(),'');assert.equal((await page.locator('#result').textContent()).includes('synthetic-only'),false);
 }finally{await browser.close();await new Promise(r=>server.close(r));}
});

test('login handler rejects an unconfigured URL before authorization',async()=>{
 const {createDataPlatformHandler}=await import('../src/dataplatform-ui.mjs');let attempts=0;
 const server=createServer(createDataPlatformHandler({key:'test'},{async begin(){attempts++;return {status:'authorized'};}}));await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const origin=`http://127.0.0.1:${server.address().port}`;
 try{for(const url of ['http://127.0.0.1:1234','https://example.com',42]){const response=await fetch(origin+'/dataplatform/login',{method:'POST',headers:{origin,'content-type':'application/json'},body:JSON.stringify({url,username:'admin',password:'synthetic-only'})});assert.equal(response.status,400);assert.equal((await response.json()).error,'DP_TARGET_NOT_CONFIGURED');}assert.equal(attempts,0);
 const response=await fetch(origin+'/dataplatform/login',{method:'POST',headers:{origin,'content-type':'application/json'},body:JSON.stringify({url:'http://127.0.0.1:46120',username:'admin',password:'synthetic-only'})});assert.equal(response.status,200);assert.equal(attempts,1);
 }finally{await new Promise(r=>server.close(r));}
});
