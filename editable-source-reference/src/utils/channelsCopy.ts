// A draft is keyed by material ID, never by selection position or shared body.
export const textLength = (value: string) => Array.from(value.trim()).length
export const filenameTitle = (filename: string) => filename.replace(/\.[^.]+$/, '').trim()
export const shortTitle = (title: string) => Array.from(title.length < 6 ? `${title}视频素材` : title).slice(0, 16).join('')
export const publishDescription = (title: string, body: string, shared: string) => [title, body, shared].map(s => s.trim()).filter(Boolean).join('\n')
export function seedDrafts(assets: { id: number; filename: string }[], titles: Record<number, string>, shorts: Record<number, string>, bodies: Record<number, string>) {
  for (const asset of assets) {
    if (!(asset.id in titles)) titles[asset.id] = filenameTitle(asset.filename)
    if (!(asset.id in shorts)) shorts[asset.id] = shortTitle(filenameTitle(asset.filename))
    if (!(asset.id in bodies)) bodies[asset.id] = ''
  }
}
