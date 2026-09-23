import type { ChannelsAuthSession } from '../types'

export const isAuthorizationTerminal = (status: string) => ['authorized', 'failed', 'expired'].includes(status)

// Old servers may return stale picker/QR fields together with a terminal status.
export function normalizeAuthorizationSession(current: ChannelsAuthSession): ChannelsAuthSession {
  if (!isAuthorizationTerminal(current.status)) return current
  return { ...current, qr_ready: false, capture_mode: 'qr', interaction_required: false, account_choices: [] }
}

export function authorizationPhase(status: string, mode: string, choices: number) {
  if (status === 'failed' || status === 'expired') return 'failed'
  if (status === 'authorized') return 'authorized'
  if (status === 'selecting_account' || status === 'verifying') return 'verifying'
  if (mode === 'account_list' && choices > 0) return 'picker'
  if (mode === 'account_choice') return 'picker'
  if (status === 'waiting_scan') return 'qr'
  return 'loading'
}
