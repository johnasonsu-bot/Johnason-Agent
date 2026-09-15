import test from 'node:test';
import assert from 'node:assert/strict';
import { parseProviders } from '../src/systems.mjs';
import { DataPlatform } from '../src/dataplatform.mjs';

test('provider config rejects unknown targets, duplicate IDs and secret config',()=>{
 const good={id:'forge',label:'Forge',type:'forge',url:'http://127.0.0.1:8766',python:'/usr/bin/python3',sourceRoot:'/fixture/src',dbPath:'/fixture/data.sqlite'};
 assert.equal(parseProviders([good])[0].id,'forge');
 for(const bad of [[good,good],[{...good,password:'not-allowed'}],[{...good,id:'../bad'}],[{...good,url:'https://example.com'}],[{...good,python:'python3'}]]) assert.throws(()=>parseProviders(bad));
});
test('generic authorization grants are separated per system and use command allowlists',async()=>{
 const records=new Map();const credentials={async readRecord(k){return records.get(k)},async modifyRecord(k,f){records.set(k,f())},async deleteRecord(k){records.delete(k)}};
 const core={async login(){return {token:'fixture-grant'}},async profile(){return {username:'alice'}},async execute({command}){return {command}},async close(){}};
 const service=new DataPlatform({corePath:'/fixture/shared',core,credentials,systemId:'forge',label:'Forge',commands:['issue.list']});
 await service.flow.run({signal:new AbortController().signal,prompt:async()=> 'fixture'});
 assert.equal(service.flow.label,'Forge');assert.deepEqual(await service.execute({command:'issue.list'}),{command:'issue.list'});
 await assert.rejects(service.execute({command:'project.list'}),{code:'DP_UNKNOWN_COMMAND'});
 const other=new DataPlatform({corePath:'/fixture/shared',core,credentials,systemId:'other',label:'Other',commands:['issue.list']});
 assert.equal((await other.status()).authorized,false);
});

test('reserved Data Platform identifier cannot be routed to Forge',()=>{
 assert.throws(()=>parseProviders([{id:'dataplatform',label:'Wrong',type:'forge',url:'http://127.0.0.1:8766',python:'/usr/bin/python3',sourceRoot:'/tmp',dbPath:'/tmp/db'}]));
});

test('Forge bridge preserves configured venv and decodes chunked Unicode',async()=>{
 const {mkdtemp,mkdir,writeFile}=await import('node:fs/promises');const {tmpdir}=await import('node:os');const {join}=await import('node:path');const {execFileSync}=await import('node:child_process');const {createForgeCore}=await import('../src/systems.mjs');
 const python=process.env.FORGE_TEST_PYTHON;if(!python)return;
 const root=await mkdtemp(join(tmpdir(),'bridge-unicode-'));const modules=join(root,'cli_anything/devops');await mkdir(modules,{recursive:true});
 await writeFile(join(modules,'bridge.py'),`import sys,json,time\nsys.stdin.buffer.read()\nvalue=json.dumps({'ok':True,'value':{'prefix':sys.prefix,'text':'中文'}},ensure_ascii=False).encode()\nfor part in value:\n sys.stdout.buffer.write(bytes([part]));sys.stdout.buffer.flush();time.sleep(.001)\n`);
 const core=createForgeCore({python,sourceRoot:root,dbPath:join(root,'fixture.sqlite')});const result=await core.profile('fixture');assert.equal(result.text,'中文');assert.equal(result.prefix,execFileSync(python,['-c','import sys; print(sys.prefix)'],{encoding:'utf8'}).trim());await core.close();await assert.rejects(core.profile('fixture'),{code:'DP_UNAVAILABLE'});
});
