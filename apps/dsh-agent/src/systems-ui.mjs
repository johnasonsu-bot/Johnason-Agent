import { readFile } from 'node:fs/promises';
import { safeError } from './dataplatform.mjs';
export const SYSTEM_PATHS=['/systems','/systems/providers','/systems/status','/systems/login','/systems/logout','/systems/execute'];
export function createSystemsHandler(systems,authorization){
 return async(req,res)=>{
  const send=(status,data,html=false)=>{res.writeHead(status,{'content-type':html?'text/html; charset=utf-8':'application/json','cache-control':'no-store','x-content-type-options':'nosniff','referrer-policy':'no-referrer','content-security-policy':"default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'"});res.end(html?data:JSON.stringify(data));};
  const port=req.socket.localPort, host=req.headers.host,origin=`http://${host}`;
  if(!['127.0.0.1','::1','::ffff:127.0.0.1'].includes(req.socket.remoteAddress)||![`127.0.0.1:${port}`,`localhost:${port}`,`[::1]:${port}`].includes(host)||(req.headers.origin&&req.headers.origin!==origin)||(req.headers['sec-fetch-site']&&!['same-origin','none'].includes(req.headers['sec-fetch-site'])))return send(403,{error:'SAME_ORIGIN_REQUIRED'});
  try{
   const path=new URL(req.url,origin).pathname;
   if(req.method==='GET'&&path==='/systems')return send(200,await readFile(new URL('../public/systems.html',import.meta.url),'utf8'),true);
   if(req.method==='GET'&&path==='/systems/providers')return send(200,[...systems.values()].map(p=>({id:p.id,label:p.label,url:p.url,commands:p.commands})));
   if(req.method!=='POST'||!SYSTEM_PATHS.slice(2).includes(path))return send(405,{error:'METHOD_NOT_ALLOWED'});
   if(req.headers.origin!==origin||req.headers['content-type']?.split(';')[0]!=='application/json')return send(403,{error:'SAME_ORIGIN_REQUIRED'});
   const chunks=[];let size=0;for await(const chunk of req){size+=chunk.length;if(size>262144)return send(413,{error:'INPUT_TOO_LARGE'});chunks.push(chunk);}
   const data=JSON.parse(Buffer.concat(chunks).toString());chunks.length=0;
   if(!data||typeof data!=='object'||Array.isArray(data))return send(400,{error:'DP_INVALID_INPUT'});
   const p=systems.get(data.systemId);if(!p)return send(400,{error:'SYSTEM_NOT_FOUND'});
   if(path==='/systems/status')return send(200,await p.service.status());
   if(path==='/systems/logout')return send(200,await p.service.logout());
   if(path==='/systems/login'){
    try{
     let url;try{url=new URL(data.url);}catch{return send(400,{error:'SYSTEM_TARGET_MISMATCH'});}
     if(url.origin!==p.url||url.pathname!=='/'||url.username||url.password||url.search||url.hash)return send(400,{error:'SYSTEM_TARGET_MISMATCH'});
     if(typeof data.username!=='string'||!data.username.trim()||data.username.length>128||typeof data.password!=='string'||!data.password||data.password.length>4096)return send(400,{error:'DP_INVALID_INPUT'});
     const controller=new AbortController();res.on('close',()=>{if(!res.writableEnded)controller.abort()});
     const outcome=await authorization.begin({key:p.service.key,signal:controller.signal,interaction:{notify(){},async prompt(prompt){return prompt.kind==='secret'?data.password:data.username.trim();}}});return send(200,outcome);
    }finally{data.password='';}
   }
   try{return send(200,await p.service.executeHuman({command:data.command,input:data.input,...(data.projectId===undefined?{}:{projectId:data.projectId})},data.userPassword||undefined));}finally{data.userPassword='';}
  }catch(cause){return send(400,safeError(cause));}
 };
}
