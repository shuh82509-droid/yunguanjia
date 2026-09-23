type AuthorizationWindow = { opener: unknown; location: { replace(url: string): void }; close(): void }

/** Keep provider sign-in outside the embedded business workspace. */
export async function openAuthorizationWindow(
  open: () => AuthorizationWindow | null,
  start: () => Promise<{ url: string }>,
): Promise<void> {
  const popup = open()
  if (!popup) throw new Error('浏览器阻止了授权窗口，请允许本网站弹出窗口后重试。')
  popup.opener = null
  try {
    const { url } = await start()
    const target = new URL(url)
    const knownEntry = (
      target.origin === 'https://qianchuan.jinritemai.com' && target.pathname === '/openapi/qc/audit/oauth.html'
    ) || (
      target.origin === 'https://ad.oceanengine.com' && target.pathname === '/openapi/audit/oauth.html'
    )
    if (!knownEntry || target.username || target.password || target.hash) {
      throw new Error('授权地址异常，已停止打开，请联系管理员。')
    }
    popup.location.replace(target.href)
  } catch (error) {
    popup.close()
    throw error
  }
}
