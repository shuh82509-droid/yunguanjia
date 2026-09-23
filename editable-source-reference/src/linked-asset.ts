export function linkedAssetRequest(search: string): {id: number; etag: string} | null {
  const params = new URLSearchParams(search);
  if (!params.has('asset_id')) return null;
  const id = params.get('asset_id') || '';
  if (params.getAll('asset_id').length !== 1 || !/^[1-9]\d{0,14}$/.test(id) || !Number.isSafeInteger(Number(id))) throw Error('交付素材编号无效，请返回原任务核对。');
  const etag = params.get('asset_etag') || '';
  if (params.getAll('asset_etag').length > 1 || (etag && (!/^[a-fA-F0-9]+(?:-\d+)?$/.test(etag) || etag.length > 128))) throw Error('交付文件标识无效，请返回原任务核对。');
  return {id: Number(id), etag};
}
export function deliveryFileStatus(expected: string, current: string | undefined): 'same' | 'changed' | 'unknown' {
  if (!expected || !current) return 'unknown';
  return expected.toLowerCase() === current.replace(/^"|"$/g, '').toLowerCase() ? 'same' : 'changed';
}
