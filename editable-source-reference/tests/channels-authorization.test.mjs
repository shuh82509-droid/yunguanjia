import test from 'node:test'
import assert from 'node:assert/strict'
import { authorizationPhase, isAuthorizationTerminal, normalizeAuthorizationSession } from '../src/utils/channelsAuthorization.ts'

for (const status of ['failed', 'expired', 'authorized']) {
  test(`terminal ${status} clears stale login controls`, () => {
    const current = normalizeAuthorizationSession({ status, capture_mode: 'account_list', qr_ready: true, interaction_required: true, account_choices: [{ choice_id: 'old' }] })
    assert.equal(isAuthorizationTerminal(status), true)
    assert.equal(current.qr_ready, false)
    assert.equal(current.interaction_required, false)
    assert.deepEqual(current.account_choices, [])
    assert.equal(authorizationPhase(status, 'account_list', 0), status === 'authorized' ? 'authorized' : 'failed')
  })
}
for (const status of ['selecting_account', 'verifying']) {
  test(`${status} is verification, not empty-account loading`, () => {
    assert.equal(authorizationPhase(status, 'account_list', 0), 'verifying')
    assert.equal(isAuthorizationTerminal(status), false)
  })
}
test('QR, account picker and resource wait remain distinct', () => {
  assert.equal(authorizationPhase('waiting_scan', 'qr', 0), 'qr')
  assert.equal(authorizationPhase('waiting_choice', 'account_list', 3), 'picker')
  assert.equal(authorizationPhase('waiting_slot', 'qr', 0), 'loading')
})
