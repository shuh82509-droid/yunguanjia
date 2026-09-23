import test from 'node:test';
import assert from 'node:assert/strict';
import {openAuthorizationWindow} from '../src/authorization-window.ts';
test('provider opens independently and opener is cleared before API call', async () => {
  const order=[];
  const popup={opener:{},location:{replace:url=>order.push(url)},close:()=>assert.fail('unexpected close')};
  await openAuthorizationWindow(()=>{order.push('open');return popup},async()=>{
    assert.equal(popup.opener,null);order.push('api');
    return {url:'https://ad.oceanengine.com/openapi/audit/oauth.html?state=sample'};
  });
  assert.deepEqual(order.slice(0,2),['open','api']);assert.match(order[2],/^https:/);
});
test('blocked popup does not create an OAuth state',async()=>{
  await assert.rejects(()=>openAuthorizationWindow(()=>null,async()=>assert.fail('must not call API')),/阻止/);
});
test('current Qianchuan backend entry opens with all issued parameters unchanged',async()=>{
  const url='https://qianchuan.jinritemai.com/openapi/qc/audit/oauth.html?app_id=1871927445201475&state=test-state&redirect_uri=https%3A%2F%2Fhub.fandow.com%2Fyxb%2Fcallback&scope=test&material_auth=1';
  let actual;
  const popup={opener:{},location:{replace:value=>actual=value},close:()=>assert.fail('official entry closed')};
  await openAuthorizationWindow(()=>popup,async()=>({url}));
  assert.equal(actual,url);assert.equal(popup.opener,null);
});
for(const url of [
  'http://qianchuan.jinritemai.com/openapi/qc/audit/oauth.html',
  'https://qianchuan.jinritemai.com.evil.example/openapi/qc/audit/oauth.html',
  'https://qianchuan.jinritemai.com:8443/openapi/qc/audit/oauth.html',
  'https://person@qianchuan.jinritemai.com/openapi/qc/audit/oauth.html',
  'https://qianchuan.jinritemai.com/openapi/qc/audit/oauth.html#unexpected',
  'https://qianchuan.jinritemai.com/other',
  'https://ad.oceanengine.com:8443/openapi/audit/oauth.html',
])test('untrusted authorization destination is rejected: '+url,async()=>{
  let closed=0;
  const popup={opener:{},location:{replace:()=>assert.fail('unsafe navigation')},close:()=>closed++};
  await assert.rejects(()=>openAuthorizationWindow(()=>popup,async()=>({url})),/授权地址异常/);
  assert.equal(closed,1);
});
test('unsafe provider and API error close the empty popup',async()=>{
  for(const start of [async()=>({url:'https://other.example/'}),async()=>{throw new Error('network')}]){
    let closed=0;
    const popup={opener:{},location:{replace:()=>assert.fail('unsafe navigation')},close:()=>closed++};
    await assert.rejects(()=>openAuthorizationWindow(()=>popup,start));assert.equal(closed,1);
  }
});
