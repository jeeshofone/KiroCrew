/**
 * `api.createChatSlot` answers with the created slot. When the gateway opened the
 * session but refused to file it into the requested folder, the slot carries
 * `filing_error` and the client logs ONE warning naming the stable code, so every
 * caller (the sidebar, the app session hooks, Code Review Sage) records it.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { api, __resetAuthRecoveryStateForTests } from '../api/client'

function res(status: number, body: unknown): Response {
  const text = JSON.stringify(body)
  return {
    ok: status >= 200 && status < 300,
    status,
    url: 'http://localhost:6776/api/chat/slots',
    headers: { get: () => null },
    json: async () => body,
    text: async () => text,
    clone: () => res(status, body),
  } as unknown as Response
}

const fetchMock = vi.fn()
let warn: ReturnType<typeof vi.spyOn>

beforeEach(() => {
  fetchMock.mockReset()
  vi.stubGlobal('fetch', fetchMock)
  __resetAuthRecoveryStateForTests()
  warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
})

afterEach(() => {
  warn.mockRestore()
  vi.unstubAllGlobals()
  __resetAuthRecoveryStateForTests()
})

const create = () => api.createChatSlot('demo', 'worker', undefined, undefined, 'persistent', undefined, undefined, 'f1')

describe('createChatSlot filing_error', () => {
  it('returns the unfiled slot with its filing_error and warns once with the code', async () => {
    const filingError = { code: 'folder_filing_refused', status: 409, reason: 'slot_replaced', error: 'slot replaced' }
    fetchMock.mockResolvedValue(res(200, { key: 'demo', folder_id: '', filing_error: filingError }))
    const slot = await create()
    expect(slot.filing_error).toEqual(filingError)
    const filingWarnings = warn.mock.calls.filter(args => args.includes('folder_filing_refused'))
    expect(filingWarnings).toHaveLength(1)
    expect(filingWarnings[0]).toContain('demo')
  })

  it('does not warn when the slot was filed', async () => {
    fetchMock.mockResolvedValue(res(200, { key: 'demo', folder_id: 'f1' }))
    const slot = await create()
    expect(slot.filing_error).toBeUndefined()
    expect(warn.mock.calls.filter(args => args.includes('folder_filing_refused'))).toEqual([])
  })
})
