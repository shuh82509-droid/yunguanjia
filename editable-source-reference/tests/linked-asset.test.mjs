import test from 'node:test';
import assert from 'node:assert/strict';
import {linkedAssetRequest,deliveryFileStatus} from '../src/linked-asset.ts';
test('exact asset id and immutable file tag round trip independently from filename',()=>{
 assert.deepEqual(linkedAssetRequest('?asset_id=98419&asset_etag=6b8af034a8a18f7c19d51ac905190c3d-1'),{id:98419,etag:'6b8af034a8a18f7c19d51ac905190c3d-1'});
 assert.equal(linkedAssetRequest('?q=duplicate-name.mp4'),null);
});
for(const query of ['asset_id=0','asset_id=-1','asset_id=1e3','asset_id=01','asset_id=../../1','asset_id=1&asset_id=2','asset_id=9007199254740992','asset_id=1&asset_etag=x<script>','asset_id=1&asset_etag=a&asset_etag=b'])
 test('reject ambiguous or unsafe asset selector '+query,()=>assert.throws(()=>linkedAssetRequest('?'+query)));
test('same file, replaced file and missing evidence remain distinct',()=>{
 assert.equal(deliveryFileStatus('aabb-1','"AABB-1"'),'same');
 assert.equal(deliveryFileStatus('aabb-1','aabb-2'),'changed');
 assert.equal(deliveryFileStatus('aabb-1',undefined),'unknown');
 assert.equal(deliveryFileStatus('','aabb-1'),'unknown');
});
