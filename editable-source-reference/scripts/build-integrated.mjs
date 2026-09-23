import { spawnSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
const cwd = fileURLToPath(new URL('../', import.meta.url));
const base = '/yxb/wis-marketing-hub/modules/cloud-manager/';
for (const [script,args] of [['node_modules/vue-tsc/bin/vue-tsc.js',['-b']],['node_modules/vite/bin/vite.js',['build']]]) {
  const result=spawnSync(process.execPath,[script,...args],{cwd,env:{...process.env,INTEGRATED_BASE_PATH:base},stdio:'inherit'});
  if(result.status!==0)process.exit(result.status||1);
}
const html=readFileSync(new URL('../dist/index.html',import.meta.url),'utf8');
const resources=[...html.matchAll(/(?:src|href)="([^"]+)"/g)].map(m=>m[1]);
if(!resources.length||resources.some(url=>!url.startsWith(base)))throw Error('Integrated entry contains an unexpected resource path');
console.log('Integrated resource prefix verified:',base);
