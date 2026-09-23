export type PrivateAsset = {
  id: string; filename: string; content_type: string; category: string; folder_name: string;
  size: number; status: string; media_url: string; created_at: string;
  shared_asset_id: number | null; offset?: number; chunk_bytes?: number;
}
export type PrivatePage = { items: PrivateAsset[]; total: number; page: number; folders: string[] }

export async function privateRequest<T>(path: string, init: RequestInit = {}, signal?: AbortSignal): Promise<T> {
  const controller = new AbortController()
  const abort = () => controller.abort()
  if (signal?.aborted) controller.abort()
  signal?.addEventListener('abort', abort, { once: true })
  const timer = window.setTimeout(abort, 90000)
  try {
    const response = await fetch(`api/private-assets${path}`, { ...init, signal: controller.signal, headers: {
      'Content-Type': init.body instanceof Blob ? 'application/octet-stream' : 'application/json', ...init.headers,
    } })
    if (!response.ok) {
      const body = await response.json().catch(() => ({}))
      throw new Error(typeof body.detail === 'string' ? body.detail : `私人素材请求失败（${response.status}），请重试`)
    }
    if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error('登录已失效，请重新进入云管家后续传')
    return await response.json()
  } catch (error) {
    if (signal?.aborted) throw new Error('已暂停；点击续传或重新选择同一文件即可继续')
    if (controller.signal.aborted) throw new Error('网络等待超时，已上传部分保留，请续传')
    throw error
  } finally {
    window.clearTimeout(timer)
    signal?.removeEventListener('abort', abort)
  }
}

function hashFile(file: File, progress: (stage: string, done: number) => void, signal: AbortSignal): Promise<string> {
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL('../workers/hash.worker.ts', import.meta.url), { type: 'module' })
    const cleanup = () => { worker.terminate(); signal.removeEventListener('abort', abort) }
    const abort = () => { cleanup(); reject(new Error('已暂停；可重新续传')) }
    if (signal.aborted) { abort(); return }
    signal.addEventListener('abort', abort, { once: true })
    worker.onerror = () => { cleanup(); reject(new Error('文件校验进程未完成，请重试')) }
    worker.onmessage = (event: MessageEvent<{ type: string; loaded?: number; sha256?: string; message?: string }>) => {
      if (event.data.type === 'progress') progress('校验文件', (event.data.loaded || 0) / file.size)
      if (event.data.type === 'complete' && event.data.sha256) { cleanup(); resolve(event.data.sha256) }
      if (event.data.type === 'error') { cleanup(); reject(new Error(event.data.message || '文件校验失败')) }
    }
    worker.postMessage({ file, partSize: 8 * 1024 * 1024 })
  })
}

export async function uploadPrivateFile(file: File, category: string, folder: string, progress: (stage: string, done: number) => void, signal: AbortSignal): Promise<{ asset: PrivateAsset; reused: boolean }> {
  if (!/\.(mp4|mov|webm|mkv|jpg|jpeg|png|webp)$/i.test(file.name)) throw new Error('不支持该格式，请选择视频或图片，不要选择工程文件')
  if (!file.size || file.size > 2 * 1024 ** 3) throw new Error('单文件需大于 0 且不超过 2 GiB')
  if (folder.length > 160) throw new Error('完整文件夹路径超过 160 字，请选择更浅的目录或缩短上级目录名称')
  const sha256 = await hashFile(file, progress, signal)
  const created = await privateRequest<{ asset: PrivateAsset; reused: boolean }>('/uploads', {
    method: 'POST', body: JSON.stringify({ filename: file.name, size: file.size, sha256, category, folder_name: folder }),
  }, signal)
  if (created.reused) { progress('已存在，复用私人素材', 1); return created }
  const id = created.asset.id
  let offset = created.asset.offset || 0
  let retries = 0
  while (offset < file.size) {
    progress('上传中', offset / file.size)
    try {
      const part = await privateRequest<PrivateAsset>(`/${id}/upload?offset=${offset}`, {
        method: 'PUT', body: file.slice(offset, Math.min(file.size, offset + 8 * 1024 * 1024)),
      }, signal)
      if (typeof part.offset !== 'number' || part.offset <= offset) throw new Error('上传进度未前进，请续传')
      offset = part.offset
      retries = 0
    } catch (error) {
      if (signal.aborted || ++retries > 2) throw error
      progress('正在核对已上传部分', offset / file.size)
      const status = await privateRequest<PrivateAsset>(`/${id}/upload`, {}, signal)
      offset = status.offset || 0
    }
  }
  progress('上传完成，验证文件', 1)
  return privateRequest<{ asset: PrivateAsset; reused: boolean }>(`/${id}/complete`, { method: 'POST' }, signal)
}

// readEntries may return at most 100 entries per call. Drain every batch.
export async function droppedPrivateFiles(data: DataTransfer): Promise<File[]> {
  const files: File[] = []
  const walk = async (entry: FileSystemEntry, prefix: string, depth: number): Promise<void> => {
    if (depth > 16) throw new Error('文件夹层级超过 16 层，请选择更具体的目录')
    if (entry.isFile) {
      const file = await new Promise<File>((resolve, reject) => (entry as FileSystemFileEntry).file(resolve, reject))
      Object.defineProperty(file, 'webkitRelativePath', { configurable: true, value: prefix + file.name })
      files.push(file)
      if (files.length > 500) throw new Error('每批最多 500 个文件，请拆分上传')
    } else if (entry.isDirectory) {
      const reader = (entry as FileSystemDirectoryEntry).createReader()
      while (true) {
        const entries = await new Promise<FileSystemEntry[]>((resolve, reject) => reader.readEntries(resolve, reject))
        if (!entries.length) break
        for (const child of entries) await walk(child, prefix + entry.name + '/', depth + 1)
      }
    }
  }
  const entries = Array.from(data.items || []).map(item => item.webkitGetAsEntry?.()).filter((entry): entry is FileSystemEntry => Boolean(entry))
  if (entries.length) for (const entry of entries) await walk(entry, '', 0)
  else files.push(...Array.from(data.files))
  if (files.length > 500) throw new Error('每批最多 500 个文件，请拆分上传')
  return files
}
