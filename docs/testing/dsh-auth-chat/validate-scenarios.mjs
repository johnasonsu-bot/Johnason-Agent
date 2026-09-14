import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const require=createRequire(import.meta.url);
const root=new URL('../../../../data-platform-dsh-auth/backend/src/',import.meta.url);
const {evaluate}=require(new URL('modules/access-control/engine.js',root).pathname);
const {configSchema}=require(new URL('modules/access-control/schema.js',root).pathname);
const baseline=JSON.parse(await readFile(new URL('./elements-template.json',import.meta.url)));
baseline.assignments=[{userId:99,attributes:{dsh_test_department:'研发',dsh_test_level:2},scopeIds:['dsh_test_home'],policyIds:['dsh_test_read','dsh_test_deny_write','dsh_test_deny_read']}];
const user={id:99,roleType:'custom',permissions:{modules:['overview','ingestion']}};
const cases=[
 ['overview','overview.read',()=>{},'ALLOWED'],
 ['read','ingestion.read',()=>{},'ALLOWED'],
 ['write deny','ingestion.write',()=>{},'POLICY_DENY'],
 ['RBAC','system_roles.read',()=>{},'MODULE_PERMISSION_FORBIDDEN'],
 ['low level','ingestion.read',c=>c.assignments[0].attributes.dsh_test_level=1,'POLICY_ALLOW_REQUIRED'],
 ['missing','ingestion.read',c=>{delete c.assignments[0].attributes.dsh_test_department},'ATTRIBUTE_MISSING_OR_INVALID'],
 ['deny priority','ingestion.read',c=>c.policies[2].enabled=true,'POLICY_DENY'],
 ['scope','ingestion.read',c=>c.assignments[0].scopeIds=['dsh_test_other'],'SCOPE_FORBIDDEN'],
];
for(const [name,point,mutate,expected]of cases){const config=structuredClone(baseline);mutate(config);configSchema.parse(config);assert.equal(evaluate({config,user,point,projectId:1}).code,expected);console.log('PASS',name,expected);}
const bad=structuredClone(baseline);bad.assignments[0].attributes.dsh_test_level='2';assert.equal(configSchema.safeParse(bad).success,false);console.log('PASS invalid attribute type');
const viewer={id:99,roleType:'viewer',permissions:{modules:['overview'],mode:'readonly',actions:['read']}};
assert.equal(evaluate({config:baseline,user:viewer,point:'overview.write',projectId:1}).code,'READ_ONLY_FORBIDDEN');console.log('PASS viewer readonly');
console.log('10 scenario expectations verified against actual policy engine/schema; no live writes.');
