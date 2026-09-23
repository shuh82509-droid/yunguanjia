import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {webcrypto} from 'node:crypto';
import ts from 'typescript';

const source=readFileSync(new URL('../src/utils/multipartUpload.ts',import.meta.url),'utf8');
const javascript=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ESNext}}).outputText;
const MiB=1024*1024;
async function scenario(partSize,{invalid=false,cachePartSize=64*MiB,noCache=false,noMatch=false,lookupFailure=false,recovery=false,cancelLookup=false,storageFailure=false,staleReceipt=false,malformedLookup}={}){
 const size=1024*MiB+1,total=Math.ceil(size/partSize),sha='a'.repeat(64),seen={slices:[],hashSizes:[],requests:[]};
 const file={name:'cross-entry.mp4',size,lastModified:1,type:'video/mp4',slice:(start,end)=>{seen.slices.push([start,end]);assert.equal(end-start,1);return new Blob(['x']);}};
 const stored=new Map(noCache?[]:[[`wis-upload-hashes-v2:${file.name}|${size}|1`,JSON.stringify({sha256:sha,partSize:cachePartSize,partMd5s:Array(Math.ceil(size/cachePartSize)).fill('b'.repeat(32))})]]);
 const session={session_id:'original-session',object_key:'original-object',file_size:size,status:'active',part_size:partSize,total_parts:invalid?total+1:total,uploaded_parts:Array.from({length:total-1},(_,i)=>({part_number:i+1,size:partSize,etag:`original-${i+1}`}))};
 if(staleReceipt)stored.set('wis-upload-receipts-v2:original-session',JSON.stringify([{part_number:1,size:partSize,etag:'stale-browser-etag'}]));
 let persisted=session,lookups=0,statusReads=0;const controller=new AbortController();
 const api={
  lookupMultipartUpload:async body=>{lookups++;assert.equal(body.sha256,sha,'The full-file digest must identify the original session');assert.match(body.legacy_sha256,/^[0-9a-f]{64}$/);assert.equal(body.size,size);assert.equal(body.category,'其他 WIS 素材');if(lookupFailure)throw Error('lookup unavailable');if(cancelLookup)controller.abort();if(malformedLookup!==undefined)return malformedLookup;return {session:noMatch&&lookups===1?null:persisted,recovery_required:recovery&&lookups===1};},
  multipartUploadStatus:async id=>{assert.equal(id,'original-session');statusReads++;persisted={...session,status:'completed'};return persisted;},
  createMultipartUpload:async body=>{assert.equal(body.sha256,sha);seen.requests.push(body);return session;},
  multipartPartUrls:async(id,parts)=>{assert.equal(id,session.session_id);assert.deepEqual(Array.from(parts),[total]);return {items:[{part_number:total,upload_url:'https://upload.example.invalid/part',headers:{}}]};},
  acquireUploadLease:async id=>{assert.equal(id,session.session_id);return {acquired:true,lease_id:'lease'};},renewUploadLease:async()=>{},releaseUploadLease:async()=>{},
  completeMultipartUpload:async(id,parts,digest)=>{seen.completion={id,parts:JSON.parse(JSON.stringify(parts)),digest};persisted={...session,status:'completed'};return persisted;},
 };
 class Worker {constructor(){}postMessage(message){seen.hashSizes.push(message.partSize);queueMicrotask(()=>this.onmessage({data:{type:'complete',sha256:sha,partMd5s:Array(Math.ceil(size/message.partSize)).fill('c'.repeat(32))}}));}terminate(){}}
 class XHR {status=200;upload={};open(method,url){assert.equal(method,'PUT');assert.equal(url,'https://upload.example.invalid/part');}setRequestHeader(){}getResponseHeader(){return '';}send(blob){assert.equal(blob.size,1);queueMicrotask(()=>{this.upload.onprogress?.({lengthComputable:true,loaded:1});this.onload();});}abort(){this.onabort?.();}}
 const context=vm.createContext({console,URL,Blob,TextEncoder,Uint8Array,DOMException,performance,Worker,XMLHttpRequest:XHR,queueMicrotask,localStorage:{getItem:k=>stored.get(k)||null,setItem:(k,v)=>{if(storageFailure)throw Error('QuotaExceeded');stored.set(k,v);},removeItem:k=>{if(storageFailure)throw Error('StorageUnavailable');stored.delete(k);}},window:{crypto:webcrypto,setTimeout,clearTimeout,setInterval,clearInterval}});
 const apiModule=new vm.SyntheticModule(['api'],function(){this.setExport('api',api);},{context});
 const hashModule=new vm.SyntheticModule(['createMD5','createSHA256'],function(){const fail=()=>{throw Error('Worker fallback was unexpected');};this.setExport('createMD5',fail);this.setExport('createSHA256',fail);},{context});
 const module=new vm.SourceTextModule(javascript,{context,initializeImportMeta:m=>{m.url='file:///acceptance/multipartUpload.js';}});
 await module.link(spec=>spec==='hash-wasm'?hashModule:apiModule);await module.evaluate();
 const invoke=()=>module.namespace.uploadFileMultipart(file,{assetScope:'marketing_video',category:'其他 WIS 素材'},{},controller.signal);
 const run=invoke();
 if(malformedLookup!==undefined){await assert.rejects(run,/原上传查询结果不完整/);assert.equal(seen.slices.length,0);assert.equal(seen.requests.length,0);return;}
 if(lookupFailure||cancelLookup){await assert.rejects(run,lookupFailure?/lookup unavailable/:/上传已暂停/);assert.equal(seen.slices.length,0);assert.equal(seen.requests.length,0);return;}
 if(invalid){await assert.rejects(run,/原上传会话的分片信息不完整/);assert.equal(seen.slices.length,0);assert.equal(seen.hashSizes.length,0);return;}
 const result=await run;
 assert.equal(result.session_id,'original-session');assert.equal(result.object_key,'original-object');
 if(recovery){assert.equal(statusReads,1);assert.equal(result.status,'completed');assert.equal(seen.requests.length,0);assert.equal(seen.slices.length,0);return;}
 assert.deepEqual(seen.slices,[[size-1,size]]);assert.equal(seen.requests.length,noMatch?1:0);
 assert.equal(seen.completion.id,'original-session');assert.equal(seen.completion.digest,sha);
 assert.equal(seen.completion.parts.length,total);
 assert.deepEqual(seen.completion.parts.slice(0,-1),session.uploaded_parts);
 assert.equal(seen.completion.parts.at(-1).etag,`"${(!noCache&&partSize===cachePartSize?'b':'c').repeat(32)}"`);
 assert.deepEqual(seen.hashSizes,noCache?(partSize===64*MiB?[64*MiB]:[64*MiB,partSize]):partSize===cachePartSize?[]:[partSize]);
 const retried=await invoke();assert.equal(retried.session_id,'original-session');assert.equal(retried.status,'completed');
 assert.equal(lookups,2);assert.equal(seen.requests.length,noMatch?1:0);assert.equal(seen.slices.length,1,'Completion retries must not send the file again');
}
test('resumes an existing 32 MiB session for a file over 1 GiB, preserving original receipts and hash boundaries',()=>scenario(32*MiB));
test('retains the existing 64 MiB session and compatible cached hashes',()=>scenario(64*MiB));
test('rejects inconsistent server part metadata before sending bytes or replacing the session',()=>scenario(32*MiB,{invalid:true}));
test('a 32 MiB digest cache still finds the original real-SHA session on consecutive retries',()=>scenario(32*MiB,{cachePartSize:32*MiB}));
test('first use in another entry computes actual SHA before finding the original 32 MiB session',()=>scenario(32*MiB,{noCache:true}));
test('only an explicit no-match creates a session with actual SHA, then retry reads it back',()=>scenario(64*MiB,{noMatch:true}));
test('an inconclusive lookup does not create or upload a replacement',()=>scenario(32*MiB,{lookupFailure:true}));
test('a lost completion receipt recovers the original session by ID',()=>scenario(32*MiB,{recovery:true}));
test('cancelling during lookup prevents session creation and file transmission',()=>scenario(32*MiB,{cancelLookup:true}));
test('browser storage failure never retries a successful part or invalidates completion',()=>scenario(64*MiB,{storageFailure:true}));
test('fresh server part receipts override stale browser receipts',()=>scenario(64*MiB,{staleReceipt:true}));
for(const malformedLookup of [{},{session:false},{session:'unavailable'},{session:null,recovery_required:true}])test('an incomplete lookup response never creates another upload: '+JSON.stringify(malformedLookup),()=>scenario(32*MiB,{malformedLookup}));
