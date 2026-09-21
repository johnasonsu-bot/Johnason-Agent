import { readFileSync, realpathSync } from 'node:fs';
import { isAbsolute, join } from 'node:path';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { DataPlatform, COMMANDS } from './dataplatform.mjs';
export const FORGE_COMMANDS = Object.freeze(['project.list','issue.list','issue.get','sprint.list','release.list','activity.list','report.summary','report.analytics','object.list','object.get','object.create','object.update','user.list','user.create','user.update','user.role.set','role.list','role.create','role.update','permission.catalog']);
const error = (code, statusCode=400) => Object.assign(new Error(code), { code, statusCode });
export function parseProviders(value) {
  if (!Array.isArray(value) || value.length > 16) throw error('SYSTEM_CONFIG_INVALID');
  const ids=new Set();
  return value.map(p=>{
    if (!p || typeof p!=='object' || !/^[a-z][a-z0-9_-]{0,39}$/.test(p.id) || ids.has(p.id) || typeof p.label!=='string' || !p.label || p.label.length>80 || !['dataplatform','forge'].includes(p.type)) throw error('SYSTEM_CONFIG_INVALID');
    if(p.id==='dataplatform'&&p.type!=='dataplatform')throw error('SYSTEM_CONFIG_INVALID');
    ids.add(p.id);
    const allowed=['id','label','url','type',...(p.type==='forge'?['python','sourceRoot','dbPath']:['corePath'])];
    if(Object.keys(p).some(k=>!allowed.includes(k)))throw error('SYSTEM_CONFIG_INVALID');
    let url;try{url=new URL(p.url)}catch{throw error('SYSTEM_CONFIG_INVALID')}
    if(url.protocol!=='http:' || !['localhost','127.0.0.1','[::1]'].includes(url.hostname) || url.username || url.password || url.search || url.hash || url.pathname!=='/')throw error('SYSTEM_CONFIG_INVALID');
    for(const k of p.type==='forge'?['python','sourceRoot','dbPath']:['corePath'])if(typeof p[k]!=='string'||!isAbsolute(p[k]))throw error('SYSTEM_CONFIG_INVALID');
    return {...p,url:url.origin};
  });
}
export function readProviders(path, corePath) {
  const providers=path?parseProviders(JSON.parse(readFileSync(path,'utf8'))):[];
  if(corePath&&!providers.some(p=>p.id==='dataplatform'))providers.unshift({id:'dataplatform',label:'Data Platform',type:'dataplatform',url:'http://127.0.0.1:46120',corePath});
  return parseProviders(providers);
}
export function createForgeCore(provider) {
  realpathSync(provider.python);
  const python=provider.python, sourceRoot=realpathSync(provider.sourceRoot);
  const active=new Set(), exits=new Set(); let closed=false;
  function invoke(operation, data={}) {
    if(closed)return Promise.reject(error('DP_UNAVAILABLE',503));
    return new Promise((resolve,reject)=>{
      const child=spawn(python,['-m','cli_anything.devops.bridge','--db',provider.dbPath],{cwd:sourceRoot,env:{PATH:process.env.PATH||'/usr/bin:/bin',HOME:process.env.HOME||'',PYTHONPATH:sourceRoot,PYTHONDONTWRITEBYTECODE:'1'},stdio:['pipe','pipe','pipe']});
      const exited=new Promise(r=>child.once('close',r));exits.add(exited);exited.then(()=>exits.delete(exited));
      const chunks=[];let size=0,settled=false;
      const cancel=()=>finish(error('DP_UNAVAILABLE',503));active.add(cancel);
      const timer=setTimeout(()=>finish(error('DP_UNAVAILABLE',503)),20000);
      function finish(err,value){if(settled)return;settled=true;active.delete(cancel);clearTimeout(timer);if(err){child.kill('SIGTERM');const force=setTimeout(()=>child.kill('SIGKILL'),2000);force.unref();exited.then(()=>clearTimeout(force));}err?reject(err):resolve(value);}
      child.on('error',()=>finish(error('DP_UNAVAILABLE',503)));
      child.stdin.on('error',()=>finish(error('DP_UNAVAILABLE',503)));
      child.stderr.resume(); // Never expose bridge tracebacks, environment or request secrets.
      child.stdout.on('data',chunk=>{size+=chunk.length;if(size>2*1024*1024)return finish(error('DP_UNAVAILABLE',503));chunks.push(chunk);});
      child.on('close',code=>{if(settled)return;try{const result=JSON.parse(Buffer.concat(chunks).toString('utf8'));if(code!==0||result.ok!==true){const status=[400,401,403,404,409,503].includes(result.status)?result.status:503;return finish(error('FORGE_OPERATION_FAILED',status));}finish(null,result.value);}catch{finish(error('DP_UNAVAILABLE',503));}});
      child.stdin.end(JSON.stringify({operation,...data}));
    });
  }
  return {login:args=>invoke('login',args),profile:token=>invoke('profile',{token}),logout:token=>invoke('logout',{token}),execute:({token,command,input})=>invoke('execute',{token,command,input}),async close(){closed=true;for(const cancel of [...active])cancel();await Promise.allSettled([...exits]);}};
}
export function createSystems(providers, credentials) {
  return new Map(providers.map(p=>{
    const commands=p.type==='forge'?FORGE_COMMANDS:COMMANDS;
    // Grant identity includes the actual Forge database and interpreter, not only UI URL.
    const identity=p.type==='forge'?join(p.sourceRoot,'bridge-'+createHash('sha256').update(JSON.stringify([p.python,p.dbPath])).digest('hex')):p.corePath;
    const service=new DataPlatform({corePath:identity,credentials,systemId:p.id,label:p.label,commands,...(p.type==='forge'?{core:createForgeCore(p)}:{})});
    return [p.id,{...p,commands,service}];
  }));
}
