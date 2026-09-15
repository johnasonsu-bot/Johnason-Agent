import { createSystems, parseProviders } from './systems.mjs';
import { SYSTEM_PATHS, createSystemsHandler } from './systems-ui.mjs';
import { createDataPlatformHandler } from './dataplatform-ui.mjs';
export const name='johnason-systems';
export const inject=['authorization','credentials','tools'];
export function apply(ctx,config){
 const systems=createSystems(parseProviders(config.providers),ctx.credentials);
 for(const p of systems.values()){ctx.effect(()=>ctx.authorization.registerFlow(p.service.flow));ctx.effect(()=>()=>p.service.close());}
 const properties={command:{type:'string'},projectId:{type:'integer',minimum:1},input:{type:'object'}};
 const output={schema:{},render:(_args,value)=>[{type:'text',text:JSON.stringify(value)}]};
 ctx.tools.register({name:'system_execute',description:'Execute a command in an authorized registered system. Sign in through /systems first. Never supply passwords/tokens. Forge project keys belong in input, not numeric projectId. Available systems: '+[...systems.values()].map(p=>p.id+' ['+p.commands.join(', ')+']').join('; '),parameters:{type:'object',properties:{systemId:{type:'string',enum:[...systems.keys()]},...properties},required:['systemId','command'],additionalProperties:false},output,execute:({systemId,...args})=>{const p=systems.get(systemId);if(!p)throw new Error('SYSTEM_NOT_FOUND');return p.service.execute(args);}});
 const candidate=systems.get('dataplatform');
 const dp=candidate?.type==='dataplatform'?candidate:undefined;
 if(dp)ctx.tools.register({name:'dataplatform_execute',description:'Compatibility Data Platform commands. Sign in at /systems or /dataplatform. Never supply passwords or tokens.',parameters:{type:'object',properties:{...properties,command:{type:'string',enum:dp.commands}},required:['command'],additionalProperties:false},output,execute:args=>dp.service.execute(args)});
 ctx.inject(['webServer'],ui=>{
  const handler=createSystemsHandler(systems,ctx.authorization);for(const path of SYSTEM_PATHS)ui.effect(()=>ui.webServer.register({kind:'exact',path,handler}));
  if(dp){const legacy=createDataPlatformHandler(dp.service,ctx.authorization);for(const path of ['/dataplatform','/dataplatform/status','/dataplatform/login','/dataplatform/logout','/dataplatform/execute'])ui.effect(()=>ui.webServer.register({kind:'exact',path,handler:legacy}));}
  ui.effect(()=>ui.webServer.tapIndex(html=>html.replace('</body>','<a href="/systems" target="_blank" rel="noopener" style="position:fixed;bottom:44px;right:12px;z-index:99999;background:#fff;color:#17304a;border:1px solid #cbd5e1;border-radius:8px;padding:8px 12px">系统授权</a></body>')));
 });
}
