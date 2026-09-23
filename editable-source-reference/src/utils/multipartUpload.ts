import { api } from '../api'
import type { MultipartPartUrl, MultipartUploadedPart, MultipartUploadSession } from '../types'
import { createMD5, createSHA256 } from 'hash-wasm'

type UploadScope = 'marketing_video' | 'product_image' | 'reference_video'
type UploadMeta = { assetScope: UploadScope; category: string }
export type UploadTransferStats = {
  loadedBytes: number
  totalBytes: number
  speedBps: number
  etaSeconds: number | null
  retries: number
}
type UploadCallbacks = {
  onStage?: (message: string) => void
  onProgress?: (percent: number) => void
  onSession?: (session: MultipartUploadSession) => void
  onTransfer?: (stats: UploadTransferStats) => void
}
type UploadOptions = { partConcurrency?: number; stallTimeoutMs?: number }

class BrowserLimiter {
  private active = 0
  private waiting: (() => void)[] = []

  constructor(private readonly limit: number) {}

  async run<T>(operation: () => Promise<T>): Promise<T> {
    if (this.active >= this.limit) await new Promise<void>(resolve => this.waiting.push(resolve))
    this.active += 1
    try { return await operation() }
    finally {
      this.active -= 1
      this.waiting.shift()?.()
    }
  }
}

const browserLimiter = new BrowserLimiter(8)
const receiptMemory = new Map<string, MultipartUploadedPart[]>()
const DEFAULT_STALL_TIMEOUT_MS = 45_000
const abortError = () => new DOMException('上传已暂停', 'AbortError')
const assertActive = (signal?: AbortSignal) => { if (signal?.aborted) throw abortError() }
const wait = (milliseconds: number, signal?: AbortSignal) => new Promise<void>((resolve, reject) => {
  assertActive(signal)
  const onAbort = () => { window.clearTimeout(timer); reject(abortError()) }
  const timer = window.setTimeout(() => { signal?.removeEventListener('abort', onAbort); resolve() }, milliseconds)
  signal?.addEventListener('abort', onAbort, { once: true })
})

const fingerprint = (file: File) => `${file.name}|${file.size}|${file.lastModified}`
const fingerprintHash = async (file: File) => {
  const digest = await window.crypto.subtle.digest('SHA-256', new TextEncoder().encode(fingerprint(file)))
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')
}
const partSizeFor = (fileSize: number) => fileSize >= 1024 * 1024 * 1024 ? 64 * 1024 * 1024 : 32 * 1024 * 1024
type FileHashes = { sha256: string; partMd5s: string[] }

const cachedFileHashes = (file: File, partSize: number): FileHashes | null => {
  const expectedParts = Math.ceil(file.size / partSize)
  try {
    const cached = JSON.parse(localStorage.getItem(`wis-upload-hashes-v2:${fingerprint(file)}`) || '{}') as FileHashes & { partSize?: number }
    if (
      /^[0-9a-f]{64}$/.test(cached.sha256 || '')
      && cached.partSize === partSize
      && cached.partMd5s?.length === expectedParts
      && cached.partMd5s.every(value => /^[0-9a-f]{32}$/.test(value))
    ) return { sha256: cached.sha256, partMd5s: cached.partMd5s }
  } catch { /* stale cache is recalculated */ }
  return null
}

const calculateHashesInPage = async (
  file: File,
  partSize: number,
  onProgress?: (value: number) => void,
  signal?: AbortSignal,
): Promise<FileHashes> => {
  const chunkSize = 8 * 1024 * 1024
  const hasher = await createSHA256()
  const partHasher = await createMD5()
  hasher.init()
  partHasher.init()
  const partMd5s: string[] = []
  let bytesInPart = 0
  let offset = 0
  while (offset < file.size) {
    assertActive(signal)
    const end = Math.min(file.size, offset + chunkSize)
    const bytes = new Uint8Array(await file.slice(offset, end).arrayBuffer())
    let cursor = 0
    while (cursor < bytes.length) {
      const take = Math.min(bytes.length - cursor, partSize - bytesInPart)
      const segment = bytes.subarray(cursor, cursor + take)
      hasher.update(segment)
      partHasher.update(segment)
      cursor += take
      bytesInPart += take
      if (bytesInPart === partSize) {
        partMd5s.push(partHasher.digest('hex'))
        partHasher.init()
        bytesInPart = 0
      }
    }
    offset = end
    onProgress?.(Math.round(offset / Math.max(1, file.size) * 100))
    await new Promise<void>(resolve => window.setTimeout(resolve, 0))
  }
  if (bytesInPart > 0) partMd5s.push(partHasher.digest('hex'))
  return { sha256: hasher.digest('hex'), partMd5s }
}

const calculateHashesInWorker = (
  file: File,
  partSize: number,
  expectedParts: number,
  onProgress?: (value: number) => void,
  signal?: AbortSignal,
) => new Promise<FileHashes>((resolve, reject) => {
  let worker: Worker
  try {
    worker = new Worker(new URL('../workers/hash.worker.ts', import.meta.url), { type: 'module' })
  } catch (error) {
    reject(error instanceof Error ? error : new Error('浏览器无法启动文件校验线程'))
    return
  }
  let settled = false
  const finish = () => worker.terminate()
  const succeed = (value: FileHashes) => {
    if (settled) return
    settled = true
    signal?.removeEventListener('abort', onAbort)
    finish()
    resolve(value)
  }
  const fail = (error: Error) => {
    if (settled) return
    settled = true
    signal?.removeEventListener('abort', onAbort)
    finish()
    reject(error)
  }
  const onAbort = () => fail(abortError())
  signal?.addEventListener('abort', onAbort, { once: true })
  worker.onmessage = (event: MessageEvent<{ type: string; loaded?: number; total?: number; sha256?: string; partMd5s?: string[]; message?: string }>) => {
    if (event.data.type === 'progress') {
      onProgress?.(Math.round(Number(event.data.loaded || 0) / Math.max(1, Number(event.data.total || 1)) * 100))
      return
    }
    if (event.data.type === 'complete' && event.data.sha256 && event.data.partMd5s?.length === expectedParts) {
      succeed({ sha256: event.data.sha256, partMd5s: event.data.partMd5s })
      return
    }
    fail(new Error(event.data.message || '文件校验线程返回了无效结果'))
  }
  worker.onerror = event => fail(new Error(event.message || '文件校验线程异常'))
  try {
    worker.postMessage({ file, chunkSize: 8 * 1024 * 1024, partSize })
  } catch (error) {
    fail(error instanceof Error ? error : new Error('文件无法交给校验线程'))
  }
})

const fileHashes = async (
  file: File,
  partSize: number,
  onProgress?: (value: number) => void,
  signal?: AbortSignal,
  onFallback?: () => void,
): Promise<FileHashes> => {
  const cacheKey = `wis-upload-hashes-v2:${fingerprint(file)}`
  const expectedParts = Math.ceil(file.size / partSize)
  const cached = cachedFileHashes(file, partSize)
  if (cached) {
    onProgress?.(100)
    return cached
  }
  assertActive(signal)
  let result: FileHashes
  try {
    result = await calculateHashesInWorker(file, partSize, expectedParts, onProgress, signal)
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    onFallback?.()
    result = await calculateHashesInPage(file, partSize, onProgress, signal)
  }
  if (!/^[0-9a-f]{64}$/.test(result.sha256) || result.partMd5s.length !== expectedParts || result.partMd5s.some(value => !/^[0-9a-f]{32}$/.test(value))) {
    throw new Error('文件校验结果不完整，请重新选择原文件')
  }
  try { localStorage.setItem(cacheKey, JSON.stringify({ ...result, partSize })) } catch { /* private mode may block storage */ }
  return result
}

const putPart = (
  blob: Blob,
  ticket: MultipartPartUrl,
  signal: AbortSignal | undefined,
  onProgress: (loaded: number) => void,
  stallTimeoutMs: number,
) => new Promise<string>((resolve, reject) => {
  const xhr = new XMLHttpRequest()
  let settled = false
  let stalled = false
  let lastLoaded = 0
  let lastProgressAt = Date.now()
  const onAbort = () => xhr.abort()
  const stallTimer = window.setInterval(() => {
    if (!settled && Date.now() - lastProgressAt >= stallTimeoutMs) {
      stalled = true
      xhr.abort()
    }
  }, Math.min(5_000, Math.max(1_000, Math.round(stallTimeoutMs / 5))))
  const cleanup = () => {
    window.clearInterval(stallTimer)
    signal?.removeEventListener('abort', onAbort)
  }
  const succeed = (etag: string) => {
    if (settled) return
    settled = true
    cleanup()
    resolve(etag)
  }
  const fail = (error: Error) => {
    if (settled) return
    settled = true
    cleanup()
    reject(error)
  }
  xhr.open('PUT', ticket.upload_url)
  Object.entries(ticket.headers || {}).forEach(([name, value]) => xhr.setRequestHeader(name, value))
  xhr.upload.onprogress = event => {
    const loaded = event.lengthComputable ? event.loaded : 0
    if (loaded > lastLoaded) {
      lastLoaded = loaded
      lastProgressAt = Date.now()
    }
    onProgress(loaded)
  }
  xhr.onload = () => xhr.status >= 200 && xhr.status < 300
    ? succeed((xhr.getResponseHeader('ETag') || '').trim())
    : fail(new Error(`OSS 分片上传失败（${xhr.status}）`))
  xhr.onerror = () => fail(new Error('连接 OSS 失败，请检查网络后重试'))
  xhr.onabort = () => fail(stalled ? new Error('OSS 分片长时间无进度') : abortError())
  signal?.addEventListener('abort', onAbort, { once: true })
  xhr.send(blob)
})

const putPartViaCloudManager = (
  sessionId: string,
  partNumber: number,
  blob: Blob,
  partMd5: string,
  signal: AbortSignal | undefined,
  onProgress: (loaded: number) => void,
  stallTimeoutMs: number,
) => new Promise<string>((resolve, reject) => {
  const xhr = new XMLHttpRequest()
  let settled = false
  let stalled = false
  let lastLoaded = 0
  let lastProgressAt = Date.now()
  const onAbort = () => xhr.abort()
  const stallTimer = window.setInterval(() => {
    if (!settled && Date.now() - lastProgressAt >= stallTimeoutMs) {
      stalled = true
      xhr.abort()
    }
  }, Math.min(5_000, Math.max(1_000, Math.round(stallTimeoutMs / 5))))
  const cleanup = () => {
    window.clearInterval(stallTimer)
    signal?.removeEventListener('abort', onAbort)
  }
  const succeed = (etag: string) => {
    if (settled) return
    settled = true
    cleanup()
    resolve(etag)
  }
  const fail = (error: Error) => {
    if (settled) return
    settled = true
    cleanup()
    reject(error)
  }
  xhr.open('PUT', `api/uploads/multipart/sessions/${encodeURIComponent(sessionId)}/relay-parts/${partNumber}`)
  xhr.setRequestHeader('Content-Type', 'application/octet-stream')
  xhr.setRequestHeader('X-Upload-Part-MD5', partMd5)
  xhr.upload.onprogress = event => {
    const loaded = event.lengthComputable ? event.loaded : 0
    if (loaded > lastLoaded) {
      lastLoaded = loaded
      lastProgressAt = Date.now()
    }
    onProgress(loaded)
  }
  xhr.onload = () => {
    if (xhr.status >= 200 && xhr.status < 300) {
      try {
        const data = JSON.parse(xhr.responseText || '{}') as { etag?: string }
        if (data.etag) succeed(data.etag)
        else fail(new Error('云管家中转未返回分片校验值'))
      } catch { fail(new Error('云管家中转返回格式不正确')) }
      return
    }
    try {
      const data = JSON.parse(xhr.responseText || '{}') as { detail?: string }
      fail(new Error(data.detail || `云管家中转失败（${xhr.status}）`))
    } catch { fail(new Error(`云管家中转失败（${xhr.status}）`)) }
  }
  xhr.onerror = () => fail(new Error('连接云管家中转服务失败，请检查网络后重试'))
  xhr.onabort = () => fail(stalled ? new Error('云管家中转长时间无进度') : abortError())
  signal?.addEventListener('abort', onAbort, { once: true })
  xhr.send(blob)
})

const acquireServerLease = async (sessionId: string, signal?: AbortSignal, onStage?: (message: string) => void) => {
  const requestId = window.crypto.randomUUID()
  while (true) {
    assertActive(signal)
    const result = await api.acquireUploadLease(sessionId, requestId)
    if (result.acquired && result.lease_id) {
      return result.lease_id
    }
    const position = Math.max(1, Number(result.queue_position || 1))
    onStage?.(`上传排队第 ${position} 位（公司 ${Number(result.global_in_use || 0)}/${Number(result.global_limit || 24)}，本人 ${Number(result.user_in_use || 0)}/${Number(result.user_limit || 8)}）`)
    await wait(Math.max(250, Number(result.retry_after_ms || 500)), signal)
  }
}

const uploadPartWithLease = async (
  sessionId: string,
  blob: Blob,
  ticket: MultipartPartUrl,
  signal: AbortSignal | undefined,
  onProgress: (loaded: number) => void,
  onStage?: (message: string) => void,
  stallTimeoutMs = DEFAULT_STALL_TIMEOUT_MS,
) => browserLimiter.run(async () => {
  const leaseId = await acquireServerLease(sessionId, signal, onStage)
  const renewTimer = window.setInterval(() => { void api.renewUploadLease(leaseId).catch(() => undefined) }, 30000)
  try { return await putPart(blob, ticket, signal, onProgress, stallTimeoutMs) }
  finally {
    window.clearInterval(renewTimer)
    await api.releaseUploadLease(leaseId).catch(() => undefined)
  }
})

const uploadPartViaCloudManagerWithLease = async (
  sessionId: string,
  partNumber: number,
  blob: Blob,
  partMd5: string,
  signal: AbortSignal | undefined,
  onProgress: (loaded: number) => void,
  onStage?: (message: string) => void,
  stallTimeoutMs = 120_000,
) => browserLimiter.run(async () => {
  const leaseId = await acquireServerLease(sessionId, signal, onStage)
  const renewTimer = window.setInterval(() => { void api.renewUploadLease(leaseId).catch(() => undefined) }, 30000)
  try {
    return await putPartViaCloudManager(
      sessionId,
      partNumber,
      blob,
      partMd5,
      signal,
      onProgress,
      stallTimeoutMs,
    )
  } finally {
    window.clearInterval(renewTimer)
    await api.releaseUploadLease(leaseId).catch(() => undefined)
  }
})

export const uploadFileMultipart = async (
  file: File,
  meta: UploadMeta,
  callbacks: UploadCallbacks = {},
  signal?: AbortSignal,
  options: UploadOptions = {},
): Promise<MultipartUploadSession> => {
  callbacks.onStage?.('正在核对原文件，校验完成后恢复已有上传')
  const expectedPartSize = partSizeFor(file.size)
  // SHA identifies the entire file; unlike part MD5s, it is independent of the
  // entry point's chunk size. The lookup also recognizes legacy fingerprints.
  const identityHashes = cachedFileHashes(file, expectedPartSize)
    || cachedFileHashes(file, 32 * 1024 * 1024)
    || cachedFileHashes(file, 64 * 1024 * 1024)
    || await fileHashes(file, expectedPartSize,
      percent => callbacks.onProgress?.(Math.min(5, Math.round(percent * .05))), signal,
      () => callbacks.onStage?.('校验线程不可用，已切换兼容模式继续校验原文件'))
  assertActive(signal)
  let uploadStarted = false
  const legacyFingerprint = await fingerprintHash(file)
  assertActive(signal)
  callbacks.onStage?.('正在核对原上传会话与已保存分片')
  const request = {
    filename: file.name,
    content_type: file.type || 'application/octet-stream',
    size: file.size,
    sha256: identityHashes.sha256,
    asset_scope: meta.assetScope,
    category: meta.category,
  }
  const matched = await api.lookupMultipartUpload({ ...request, legacy_sha256: legacyFingerprint })
  assertActive(signal)
  if (!matched || !Object.prototype.hasOwnProperty.call(matched, 'session')
      || (matched.session !== null && (typeof matched.session !== 'object' || !matched.session.session_id))
      || (matched.session === null && matched.recovery_required)) {
    throw new Error('原上传查询结果不完整，请稍后恢复；本次未新建上传')
  }
  // A failed lookup is inconclusive and must never create a replacement upload.
  let session = matched.session
    ? (matched.recovery_required ? await api.multipartUploadStatus(matched.session.session_id) : matched.session)
    : await api.createMultipartUpload(request)
  assertActive(signal)
  if (session.file_size !== file.size || !session.session_id || !session.object_key) {
    throw new Error('原上传会话与当前文件不一致，请保留原记录并重新核对')
  }
  callbacks.onSession?.(session)
  if (session.status === 'completed') {
    callbacks.onProgress?.(100)
    return session
  }
  if (session.status !== 'active') throw new Error('上传会话已结束，请重新选择文件')
  if (!Number.isSafeInteger(session.part_size) || session.part_size <= 0
      || !Number.isSafeInteger(session.total_parts)
      || session.total_parts !== Math.ceil(file.size / session.part_size)) {
    throw new Error('原上传会话的分片信息不完整，请稍后恢复原会话')
  }
  // A session may originate in another authorized entry point. Its persisted
  // chunk boundaries also define the hashes and receipts used for resumption.
  const hashesPromise = fileHashes(
    file,
    session.part_size,
    percent => { if (!uploadStarted) callbacks.onProgress?.(Math.min(5, Math.round(percent * .05))) },
    signal,
    () => callbacks.onStage?.('校验线程不可用，已切换兼容模式继续校验原文件'),
  )
  void hashesPromise.catch(() => undefined)
  uploadStarted = true

  const receiptKey = `wis-upload-receipts-v2:${session.session_id}`
  const receipts = new Map<number, MultipartUploadedPart>()
  const addReceipt = (item: MultipartUploadedPart) => {
    const expectedSize = Math.min(session.part_size, file.size - (item.part_number - 1) * session.part_size)
    if (item.part_number >= 1 && item.part_number <= session.total_parts && item.size === expectedSize && item.etag) {
      receipts.set(item.part_number, item)
    }
  }
  ;(receiptMemory.get(session.session_id) || []).forEach(addReceipt)
  try {
    const cached = JSON.parse(localStorage.getItem(receiptKey) || '[]') as MultipartUploadedPart[]
    if (Array.isArray(cached)) cached.forEach(addReceipt)
  } catch { /* corrupt local receipts are ignored */ }
  // Fresh server receipts win over stale browser entries for the same part.
  session.uploaded_parts.forEach(addReceipt)
  const persistReceipts = () => {
    const saved = Array.from(receipts.values()).sort((left, right) => left.part_number - right.part_number)
    receiptMemory.set(session.session_id, saved)
    try { localStorage.setItem(receiptKey, JSON.stringify(saved)) }
    catch { /* Keep successful transfer receipts in memory if browser storage is full. */ }
  }
  const uploaded = new Set(receipts.keys())
  const completedBytes = Array.from(receipts.values()).reduce((sum, item) => sum + Number(item.size || 0), 0)
  const partProgress = new Map<number, number>()
  const transferStartedAt = performance.now()
  const transferSamples: { at: number; bytes: number }[] = [{ at: transferStartedAt, bytes: completedBytes }]
  let lastTransferReportAt = 0
  let retryCount = 0
  const reportProgress = (force = false) => {
    const activeBytes = Array.from(partProgress.values()).reduce((sum, value) => sum + value, 0)
    const transferredBytes = Math.min(file.size, completedBytes + activeBytes)
    callbacks.onProgress?.(Math.max(5, Math.min(99, Math.round(transferredBytes / file.size * 94 + 5))))
    const now = performance.now()
    if (!force && now - lastTransferReportAt < 250) return
    transferSamples.push({ at: now, bytes: transferredBytes })
    while (transferSamples.length > 2 && now - transferSamples[0].at > 8_000) transferSamples.shift()
    const first = transferSamples[0]
    const elapsedSeconds = Math.max(.25, (now - first.at) / 1000)
    const speedBps = Math.max(0, (transferredBytes - first.bytes) / elapsedSeconds)
    const remainingBytes = Math.max(0, file.size - transferredBytes)
    callbacks.onTransfer?.({
      loadedBytes: transferredBytes,
      totalBytes: file.size,
      speedBps,
      etaSeconds: speedBps >= 1024 ? Math.ceil(remainingBytes / speedBps) : null,
      retries: retryCount,
    })
    lastTransferReportAt = now
  }
  reportProgress(true)
  const pending = Array.from({ length: session.total_parts }, (_, index) => index + 1).filter(number => !uploaded.has(number))
  const tickets = new Map<number, MultipartPartUrl>()
  for (let offset = 0; offset < pending.length; offset += 50) {
    assertActive(signal)
    const result = await api.multipartPartUrls(session.session_id, pending.slice(offset, offset + 50))
    result.items.forEach(item => tickets.set(item.part_number, item))
  }

  callbacks.onStage?.(`正在分片并发上传（已恢复 ${uploaded.size}/${session.total_parts} 片）`)
  let cursor = 0
  const worker = async () => {
    while (cursor < pending.length) {
      const partNumber = pending[cursor++]
      let ticket = tickets.get(partNumber)
      if (!ticket) throw new Error(`第 ${partNumber} 个分片缺少上传地址`)
      const start = (partNumber - 1) * session.part_size
      const end = Math.min(file.size, start + session.part_size)
      const blob = file.slice(start, end)
      let lastError: unknown
      for (let attempt = 1; attempt <= 4; attempt += 1) {
        assertActive(signal)
        try {
          const progress = (loaded: number) => {
            partProgress.set(partNumber, loaded)
            reportProgress()
          }
          const responseEtag = attempt <= 2
            ? await uploadPartWithLease(
                session.session_id,
                blob,
                ticket,
                signal,
                progress,
                callbacks.onStage,
                Math.max(20_000, Number(options.stallTimeoutMs || DEFAULT_STALL_TIMEOUT_MS)),
              )
            : await (async () => {
                callbacks.onStage?.(`OSS 直连受限，云管家正在中转第 ${partNumber}/${session.total_parts} 个分片`)
                const partMd5 = (await hashesPromise).partMd5s[partNumber - 1]
                return uploadPartViaCloudManagerWithLease(
                  session.session_id,
                  partNumber,
                  blob,
                  partMd5,
                  signal,
                  progress,
                  callbacks.onStage,
                  Math.max(120_000, Number(options.stallTimeoutMs || DEFAULT_STALL_TIMEOUT_MS) * 2),
                )
              })()
          const etag = responseEtag || `"${(await hashesPromise).partMd5s[partNumber - 1]}"`
          addReceipt({ part_number: partNumber, etag, size: blob.size })
          persistReceipts()
          partProgress.set(partNumber, blob.size)
          reportProgress()
          lastError = undefined
          break
        } catch (error) {
          lastError = error
          partProgress.set(partNumber, 0)
          const currentBytes = Math.min(file.size, completedBytes + Array.from(partProgress.values()).reduce((sum, value) => sum + value, 0))
          transferSamples.splice(0, transferSamples.length, { at: performance.now(), bytes: currentBytes })
          reportProgress(true)
          if (error instanceof DOMException && error.name === 'AbortError') throw error
          if (attempt < 4) {
            retryCount += 1
            callbacks.onStage?.(`网络波动，正在自动重连未完成分片（第 ${retryCount} 次）`)
            try {
              const refreshed = await api.multipartPartUrls(session.session_id, [partNumber])
              ticket = refreshed.items[0] || ticket
            } catch { /* the current signed URL remains valid for one hour */ }
            await wait(600 * attempt, signal)
          }
        }
      }
      if (lastError) throw lastError
    }
  }
  const partConcurrency = Math.min(8, Math.max(1, Math.round(Number(options.partConcurrency || 4))))
  await Promise.all(Array.from({ length: Math.min(partConcurrency, pending.length) }, () => worker()))
  assertActive(signal)
  callbacks.onStage?.('正在核验并合并原文件')
  const hashes = await hashesPromise
  assertActive(signal)
  if (hashes.partMd5s.length !== session.total_parts) throw new Error('文件校验结果不完整，请重新选择原文件')
  const completedParts = Array.from(receipts.values()).sort((left, right) => left.part_number - right.part_number)
  if (completedParts.length !== session.total_parts) throw new Error(`仍有分片未上传完成（${completedParts.length}/${session.total_parts}）`)
  session = await api.completeMultipartUpload(session.session_id, completedParts, hashes.sha256)
  callbacks.onSession?.(session)
  assertActive(signal)
  receiptMemory.delete(session.session_id)
  try { localStorage.removeItem(receiptKey) } catch { /* Completion remains valid. */ }
  callbacks.onProgress?.(100)
  callbacks.onTransfer?.({ loadedBytes: file.size, totalBytes: file.size, speedBps: 0, etaSeconds: 0, retries: retryCount })
  return session
}
