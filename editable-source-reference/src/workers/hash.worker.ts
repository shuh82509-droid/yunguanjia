import { createMD5, createSHA256 } from 'hash-wasm'

type HashRequest = { file: File; chunkSize?: number; partSize: number }

self.onmessage = async (event: MessageEvent<HashRequest>) => {
  try {
    const file = event.data.file
    const chunkSize = Math.max(1024 * 1024, event.data.chunkSize || 8 * 1024 * 1024)
    const partSize = Math.max(chunkSize, event.data.partSize)
    const hasher = await createSHA256()
    const partHasher = await createMD5()
    hasher.init()
    partHasher.init()
    const partMd5s: string[] = []
    let bytesInPart = 0
    let offset = 0
    while (offset < file.size) {
      const end = Math.min(file.size, offset + chunkSize)
      const buffer = await file.slice(offset, end).arrayBuffer()
      const bytes = new Uint8Array(buffer)
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
      self.postMessage({ type: 'progress', loaded: offset, total: file.size })
    }
    if (bytesInPart > 0) partMd5s.push(partHasher.digest('hex'))
    self.postMessage({ type: 'complete', sha256: hasher.digest('hex'), partMd5s })
  } catch (error) {
    self.postMessage({ type: 'error', message: error instanceof Error ? error.message : '文件校验失败' })
  }
}

export {}
