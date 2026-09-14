import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {createServer} from 'node:http';
import {chromium} from 'playwright-core';
test('login labels, URL rejection and safe Chinese 401 feedback',async()=>{
 let attempts=0;
 const html=await readFile(new URL('../public/dataplatform.html',import.meta.url));
 const server=createServer((req,res)=>{res.setHeader('content-type',req.url==='/'?'text/html':'application/json');if(req.url==='/'){res.end(html);return;}if(req.url.endsWith('/login')){attempts++;res.statusCode=401;res.end(JSON.stringify({error:'DP_UNAUTHORIZED',status:401}));return;}res.end(JSON.stringify({authorized:false,commands:['project.list']}));});
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const browser=await chromium.launch({executablePath:process.env.DSH_TEST_BROWSER_PATH||'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
 try{const page=await browser.newPage();await page.goto(`http://127.0.0.1:${server.address().port}/`);
 await page.getByLabel('平台用户名',{exact:true}).fill('http://127.0.0.1:1234');
 await page.getByLabel('平台密码',{exact:true}).fill('synthetic-test-value');
 await page.getByRole('button',{name:'登录',exact:true}).click();
 await page.getByText('请输入平台用户名，例如 admin；这里不能填写网址。',{exact:true}).waitFor();assert.equal(attempts,0);
 await page.getByLabel('平台用户名',{exact:true}).fill('admin');
 await page.getByLabel('平台密码',{exact:true}).fill('synthetic-test-value');
 await page.getByRole('button',{name:'登录',exact:true}).click();
 await page.getByText('登录未通过：请使用 Data Platform 的用户名和密码，并确认账号已启用。',{exact:true}).waitFor();
 assert.equal(attempts,1);assert.equal(await page.locator('#password').inputValue(),'');assert.equal((await page.locator('#result').innerText()).includes('synthetic-test-value'),false);
 }finally{await browser.close();await new Promise(r=>server.close(r));}
});
