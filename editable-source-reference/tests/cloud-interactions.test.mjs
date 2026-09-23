import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

function component(name, exports, api = {}) {
  const source = readFileSync(new URL(`../src/components/${name}.vue`, import.meta.url), 'utf8').split('<script setup lang="ts">')[1].split('</script>')[0];
  const code = ts.transpileModule(source.replace(/^import .*$/gm, '') + `\nObject.assign(result, {${exports}})`, {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.None}}).outputText;
  const result = {};
  const context = { result, api, defineProps:()=>({assets:[],mode:'queue',permissions:{}}),defineEmits:()=>()=>{},
    ref:value=>({value}), reactive:value=>value, computed:fn=>({get value(){return fn()}}), watch:()=>{},onMounted:()=>{},onBeforeUnmount:()=>{},
    document:{hidden:false},window:{setTimeout,clearTimeout},URL,URLSearchParams, console };
  vm.runInNewContext(code, context);
  return result;
}
const plan = {id:'p1',name:'水润',plan_type:'multiplication',can_attach_video:true};
test('plan refresh never implicitly selects recent plans, explicit reuse then clear remain stable',()=>{
  const c=component('QianchuanPushModal','accountId,preferences,plans,selectedPlanKeys,targets,applyPlanResult,restoreRecentPlans,clearTargets,allPushTargets');
  c.accountId.value='a1';c.preferences.value=[{account_id:'a1',target_id:'p1',last_used_at:'2026-09-21T00:00:00Z'}];
  c.applyPlanResult({items:[plan],complete:true});assert.equal(c.selectedPlanKeys.value.length,0);
  c.restoreRecentPlans();assert.equal(c.selectedPlanKeys.value[0],'multiplication:p1');
  c.targets.value=[{advertiser_id:'a2',plan_id:'p2'}];c.clearTargets();
  c.applyPlanResult({items:[plan],complete:true});assert.equal(c.allPushTargets.value.length,0);
});
test('explicit recent selection excludes unavailable plans and another account',()=>{
  const c=component('QianchuanPushModal','accountId,preferences,plans,selectedPlanKeys,restoreRecentPlans');
  c.accountId.value='a1';c.preferences.value=[{account_id:'a2',target_id:'p1',last_used_at:'2026-09-21T00:00:00Z'},{account_id:'a1',target_id:'p2',last_used_at:'2026-09-21T00:00:00Z'}];
  c.plans.value=[plan,{...plan,id:'p2',can_attach_video:false}];c.restoreRecentPlans();assert.equal(c.selectedPlanKeys.value.length,0);
});
test('background review request never starts while inline video plays',async()=>{
  let reads=0;
  const c=component('ReviewCenterPanel','playingReviewIds,loadQueue', {reviewSubmissions:async()=>{reads++;return {items:[]}}});
  c.playingReviewIds.add('review1');await c.loadQueue(true);assert.equal(reads,0);
});
test('video started during in-flight poll cannot be replaced by its response',async()=>{
  let finish;
  const c=component('ReviewCenterPanel','playingReviewIds,submissions,loadQueue',{reviewSubmissions:()=>new Promise(resolve=>finish=resolve)});
  const original=[{id:'old',preview_url:'/video/old'}];c.submissions.value=original;
  const pending=c.loadQueue(true);c.playingReviewIds.add('old');finish({items:[]});await pending;
  assert.equal(c.submissions.value,original);
});

test('saved scheme rechecks availability and never queues tasks',async()=>{
  let queued=0; let reads=0;
  const c=component('QianchuanPushModal','schemes,schemeId,accounts,targets,applyScheme,schemeNotice',{
    qianchuanPlans:async()=>{reads++;return {cached:false,items:['1','2'].map(id=>({id,plan_type:'multiplication',name:'current',can_attach_video:id!=='2'}))}},
    qianchuanPush:async()=>{queued++}
  });
  c.accounts.value=[{id:'10',name:'Account'}];c.schemeId.value='s';
  c.schemes.value=[{id:'s',targets:['1','2','3'].map(id=>({advertiser_id:'10',plan_id:id,plan_name:id,plan_type:'multiplication'})).concat([{advertiser_id:'20',plan_id:'4'}])}];
  await c.applyScheme();assert.equal(c.targets.value.length,1);assert.equal(c.targets.value[0].plan_id,'1');assert.equal(queued,0);assert.equal(reads,1);
  assert.match(c.schemeNotice.value,/未加入 3 个/);
});
test('scheme request failure preserves current selection',async()=>{
  const c=component('QianchuanPushModal','schemes,schemeId,accounts,targets,applyScheme', {qianchuanPlans:async()=>{throw Error('network')}});
  c.accounts.value=[{id:'10'}];c.schemeId.value='s';c.targets.value=[{advertiser_id:'10',plan_id:'existing'}];
  c.schemes.value=[{id:'s',targets:[{advertiser_id:'10',plan_id:'1'}]}];await c.applyScheme();
  assert.equal(c.targets.value.length,1);assert.equal(c.targets.value[0].plan_id,'existing');
});

test('failed review reads stay unknown, preserve records and never report a successful empty result',async()=>{
 const c=component('ReviewCenterPanel','loadQueue,queueReadError,queueHasLoaded,submissions',{reviewSubmissions:async()=>{throw Error('offline')}});
 await c.loadQueue();assert.equal(c.queueReadError.value,true);assert.equal(c.queueHasLoaded.value,false);
 const prior=[{id:'existing'}];c.submissions.value=prior;c.queueHasLoaded.value=true;await c.loadQueue();
 assert.equal(c.submissions.value,prior);assert.equal(c.queueHasLoaded.value,true);
});
test('review video activation replaces the active player and clears old playback bookkeeping',()=>{
 const c=component('ReviewCenterPanel','activateReviewVideo,activeReviewVideoId,playingReviewIds');
 c.activateReviewVideo('a');c.playingReviewIds.add('a');c.activateReviewVideo('b');
 assert.equal(c.activeReviewVideoId.value,'b');assert.equal(c.playingReviewIds.size,0);
});
