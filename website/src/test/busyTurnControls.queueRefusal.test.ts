import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { createElement, type MutableRefObject, type ReactNode } from 'react'
import { Provider } from 'react-redux'

import { store } from '../store'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { queuedSendStash } from '../hooks/useQueuedMessageActions'
import { useBusyTurnControls } from '../pages/chat/page/busyTurnControls'
import { prepareSendPayload } from '../utils/fileTokens'
import { prependQuote, type MessageQuote } from '../chat-core/composer/messageQuote'
import { loadDrafts, saveDrafts as persistDrafts } from '../utils/chatDrafts'
import { loadFileDrafts } from '../utils/chatFileDrafts'
import { __resetUnconfirmedCancelsForTests, getUnconfirmedCancel } from '../utils/unconfirmedCancels'
import { liveStagedFiles, setLivePaneStaged } from '../utils/chatPaneDrafts'
import type { ChatMessage } from '../types'
import type { ComposerDraftStores } from '../pages/chat/page/composerDrafts'
import type { ComposerStaging } from '../pages/chat/page/composerStaging'

// The chat page queue card: cancel is pessimistic. The composer changes
// only after the server confirms, and a refusal is kept for the page to show.

const SLOT = 'dashboard:queued'
const FILE = '/x/report.pdf'
const ref = <T,>(current: T) => ({ current }) as MutableRefObject<T>
const queued: ChatMessage = { role: 'queued', content: 'held words', cls: 'msg msg-queued', ts: '2026-11-18T09:00:00Z', meta: { queueId: 'q1' } }

function setup(messages: ChatMessage[] = [queued], pendingFiles?: string[], composerMounted = true, composerOnScreen = true) {
  const drafts = ref<Record<string, string>>({})
  const fileDrafts = ref<Record<string, string[]>>({})
  const stores = { drafts, fileDrafts, pasteDrafts: ref({}), sessionRefDrafts: ref({}), saveDrafts: vi.fn(() => true) } as unknown as ComposerDraftStores
  const pendingFilesRef = ref<string[]>([])
  const setPendingFiles = vi.fn((v: string[]) => { pendingFilesRef.current = v })
  const staging = {
    pendingFiles, pendingFilesRef, pasteBlocksRef: ref([]), setPasteBlocks: vi.fn(), setPendingFiles,
    pendingSessionsRef: ref([]), setPendingSessions: vi.fn(), pickedFileTokens: ref({}), mergeSlotTokens: vi.fn(),
  } as unknown as ComposerStaging
  const setInput = vi.fn()
  const composerSlotRef = ref<string | null>(SLOT)
  const composerMountedRef = ref(composerMounted)
  // Restages a quote on a live composer (returns the text), else folds it in.
  const recoverInto = vi.fn((t: string, q: MessageQuote | null, live = true) => (q && !live ? prependQuote(t, q) : t))
  const { result, unmount, rerender } = renderHook(({ messages, composerOnScreen }: { messages: ChatMessage[]; composerOnScreen: boolean }) => useBusyTurnControls({
    activeSlot: SLOT, currentSlot: undefined, connected: true, activeSlotRef: ref<string | null>(SLOT),
    slotRunning: false, messages, send: vi.fn(async () => true), steerMutation: { mutateAsync: vi.fn() } as never,
    messageQuote: { consume: () => ({ quote: null }), recoverInto } as never,
    composerRef: ref(null), composerSlotRef, composerMountedRef, forkUnavailable: false,
    inputRef: ref(''), setInput, stores, staging, pendingQuestion: undefined as never,
    softStopAtMapRef: ref(new Map<string, number>()), dispatch: vi.fn() as never, composerOnScreen,
  }), { initialProps: { messages, composerOnScreen }, wrapper: ({ children }: { children: ReactNode }) => createElement(Provider, { store }, children) })
  // The server's listing moves: redraw with the queued rows it now holds.
  let props = { messages, composerOnScreen }
  const listNow = (rows: ChatMessage[]) => rerender(props = { ...props, messages: rows })
  const showComposer = (onScreen: boolean) => rerender(props = { ...props, composerOnScreen: onScreen })
  return { result, unmount, listNow, showComposer, drafts, fileDrafts, setPendingFiles, setInput, composerSlotRef, pendingFilesRef, composerMountedRef, saveDrafts: stores.saveDrafts, recoverInto }
}

afterEach(() => { vi.restoreAllMocks(); queuedSendStash.clear(); __resetUnconfirmedCancelsForTests() })


const refusedSent = (reason = 'refused') => new ApiError(409, reason, '{"code":"queue_item_already_sent"}')
const emptyListing = () => vi.spyOn(api, 'chatSlotDetail').mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] } as never)

describe('a cancel on the chat page queue card', () => {
  it('touches the composer only after the server confirms', async () => {
    emptyListing()
    let confirm!: (v: unknown) => void
    vi.spyOn(api, 'cancelQueuedMessage').mockReturnValue(new Promise(r => { confirm = r }) as never)
    queuedSendStash.set('q1', { raw: 'held words', sent: 'held words', files: [FILE] })
    const h = setup()
    act(() => { h.result.current.handleCancelQueued('q1') })
    expect(h.drafts.current[SLOT]).toBeUndefined()
    expect(h.fileDrafts.current[SLOT]).toBeUndefined()
    expect(h.setPendingFiles).not.toHaveBeenCalled()
    await act(async () => { confirm({ ok: true }) })
    await waitFor(() => expect(h.drafts.current[SLOT]).toBe('held words'))
    expect(h.fileDrafts.current[SLOT]).toEqual([FILE])
    expect(h.setInput).toHaveBeenCalled()
  })

  it('puts the words in the clicked chat\'s draft when the composer moved to another chat before the server confirmed', async () => {
    emptyListing()
    let confirm!: (v: unknown) => void
    vi.spyOn(api, 'cancelQueuedMessage').mockReturnValue(new Promise(r => { confirm = r }) as never)
    queuedSendStash.set('q1', { raw: 'held words', sent: 'held words', files: [FILE] })
    const h = setup()
    act(() => { h.result.current.handleCancelQueued('q1') })
    h.composerSlotRef.current = 'dashboard:other'
    h.drafts.current['dashboard:other'] = 'other words'
    await act(async () => { confirm({ ok: true }) })
    await waitFor(() => expect(h.drafts.current[SLOT]).toBe('held words'))
    expect(h.fileDrafts.current[SLOT]).toEqual([FILE])
    expect(h.drafts.current['dashboard:other']).toBe('other words')
    expect(h.fileDrafts.current['dashboard:other']).toBeUndefined()
    expect(h.setInput).not.toHaveBeenCalled()
    expect(h.setPendingFiles).not.toHaveBeenCalled()
  })

  it('leaves the composer untouched on a refusal and keeps the refusal to show', async () => {
    emptyListing()
    const reason = 'this message is already in the conversation and can no longer be cancelled'
    vi.spyOn(api, 'cancelQueuedMessage').mockRejectedValue(refusedSent(reason) as never)
    queuedSendStash.set('q1', { raw: 'held words', sent: 'held words', files: [FILE] })
    const h = setup()
    h.drafts.current[SLOT] = 'my draft'
    act(() => { h.result.current.handleCancelQueued('q1') })
    await waitFor(() => expect(h.result.current.queueRefusal?.error.message).toBe(reason))
    expect(h.drafts.current[SLOT]).toBe('my draft')
    expect(h.fileDrafts.current[SLOT]).toBeUndefined()
    expect(h.setPendingFiles).not.toHaveBeenCalled()
  })
})

describe('the refusal notice belongs to its chat', () => {
  it('is not handed to another slot after a switch', async () => {
    const { useQueuedMessageActions } = await import('../hooks/useQueuedMessageActions')
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue({ messages: [], running: false, has_more: false, total: 0, queue: [] } as never)
    vi.spyOn(api, 'cancelQueuedMessage').mockRejectedValue(new ApiError(409, 'refused', '{"code":"queue_item_already_sent"}') as never)
    const view = renderHook(({ slot }) => useQueuedMessageActions({ slot, allQueued: [queued], visibleQueued: [queued] }), {
      initialProps: { slot: 'chat-a' },
      wrapper: ({ children }: { children: ReactNode }) => createElement(Provider, { store }, children),
    })
    act(() => { view.result.current.onCancel('q1') })
    await waitFor(() => expect(view.result.current.refusal?.slot).toBe('chat-a'))
    view.rerender({ slot: 'chat-b' })
    expect(view.result.current.refusal).toBeNull()
  })
})


describe('two identical cancels on the chat page', () => {
  it('leave the composer byte-identical when both are refused', async () => {
    emptyListing()
    vi.spyOn(api, 'cancelQueuedMessage').mockRejectedValue(refusedSent() as never)
    const second: ChatMessage = { ...queued, meta: { queueId: 'q2' } }
    queuedSendStash.set('q1', { raw: 'held words', sent: 'held words', files: [FILE] })
    queuedSendStash.set('q2', { raw: 'held words', sent: 'held words', files: [FILE] })
    const h = setup([queued, second])
    h.drafts.current[SLOT] = 'mine\n\nheld words'
    act(() => { h.result.current.handleCancelQueued('q1') })
    act(() => { h.result.current.handleCancelQueued('q2') })
    await waitFor(() => expect(h.result.current.queueRefusal).not.toBeNull())
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    expect(h.drafts.current[SLOT]).toBe('mine\n\nheld words')
    expect(h.fileDrafts.current[SLOT]).toBeUndefined()
  })
})

// A reloaded entry has no stash, so the parser spells its mention against
// the files the clicked slot stages when the server confirms.
describe('a cancel whose slot stages a same-named file while the DELETE is pending', () => {
  const foreign: ChatMessage = { ...queued, content: 'against [attached_file 1] /repo/y/a.txt', meta: { queueId: 'q1', files: ['/repo/y/a.txt'] } }
  const namesYInline = (text: string) => /against \[attached_file \d+\] \/repo\/y\/a\.txt/.test(text)

  it('binds the restored mention to the queued file in the live composer', async () => {
    emptyListing()
    let confirm!: (v: unknown) => void
    vi.spyOn(api, 'cancelQueuedMessage').mockReturnValue(new Promise(r => { confirm = r }) as never)
    const h = setup([foreign])
    act(() => { h.result.current.handleCancelQueued('q1') })
    h.pendingFilesRef.current = ['/repo/x/a.txt']
    h.fileDrafts.current[SLOT] = ['/repo/x/a.txt']
    await act(async () => { confirm({ ok: true }) })
    await waitFor(() => expect(h.drafts.current[SLOT]).toBe('against @y/a.txt'))
    const payload = prepareSendPayload(h.drafts.current[SLOT], h.fileDrafts.current[SLOT])
    expect(namesYInline(payload.txt)).toBe(true)
    expect(payload.txt.match(/\/repo\/y\/a\.txt/g)).toHaveLength(1)
  })

  it('reads the parked draft of the clicked slot when the composer shows another', async () => {
    emptyListing()
    let confirm!: (v: unknown) => void
    vi.spyOn(api, 'cancelQueuedMessage').mockReturnValue(new Promise(r => { confirm = r }) as never)
    const h = setup([foreign])
    act(() => { h.result.current.handleCancelQueued('q1') })
    h.composerSlotRef.current = 'dashboard:other'
    h.pendingFilesRef.current = []
    h.fileDrafts.current[SLOT] = ['/repo/x/a.txt']
    await act(async () => { confirm({ ok: true }) })
    await waitFor(() => expect(h.drafts.current[SLOT]).toBe('against @y/a.txt'))
    expect(h.fileDrafts.current[SLOT]).toEqual(['/repo/x/a.txt', '/repo/y/a.txt'])
    expect(namesYInline(prepareSendPayload(h.drafts.current[SLOT], h.fileDrafts.current[SLOT]).txt)).toBe(true)
  })
})

describe('a cancel the server confirms after the chat page unmounted', () => {
  afterEach(() => { localStorage.clear(); sessionStorage.clear() })

  it('keeps the quote in the persisted draft and leaves a newer page\'s drafts alone', async () => {
    emptyListing()
    let confirm!: (v: unknown) => void
    vi.spyOn(api, 'cancelQueuedMessage').mockReturnValue(new Promise(r => { confirm = r }) as never)
    const quote: MessageQuote = { role: 'assistant', text: 'the quoted reply' }
    queuedSendStash.set('q1', { raw: 'held words', sent: 'held words', files: [FILE], quote })
    const h = setup()
    act(() => { h.result.current.handleCancelQueued('q1') })
    // The person navigates away; a newer page then saves its own drafts.
    h.composerMountedRef.current = false
    persistDrafts({ [SLOT]: 'typed on the new page', 'dashboard:other': 'newer words' })
    await act(async () => { confirm({ ok: true }) })
    await waitFor(() => expect(loadDrafts()[SLOT]).toBe(`typed on the new page\n\n${prependQuote('held words', quote)}`))
    expect(loadDrafts()['dashboard:other']).toBe('newer words')
    expect(loadFileDrafts()[SLOT]).toEqual([FILE])
    expect(h.saveDrafts).not.toHaveBeenCalled()
    expect(h.setInput).not.toHaveBeenCalled()
  })
})

// A cancel with no real answer (no HTTP status, or a 404) is kept in the shared
// registry with its payload. While the listing holds the entry its queue card
// keeps Cancel; once the entry is gone it is an unconfirmed card.
describe('a cancel whose response is lost', () => {
  const quote: MessageQuote = { role: 'assistant', text: 'the quoted reply' }
  const lose = () => vi.spyOn(api, 'cancelQueuedMessage').mockRejectedValueOnce(new TypeError('Failed to fetch') as never)
  const lostCancel = async (h: ReturnType<typeof setup>) => {
    act(() => { h.result.current.handleCancelQueued('q1') })
    await waitFor(() => expect(getUnconfirmedCancel(SLOT, 'q1')).toBeDefined())
    await waitFor(() => expect(h.result.current.queuePendingIds.has('q1')).toBe(false))
  }

  it('keeps a listed entry as a queue card with Cancel, and Restore does nothing', async () => {
    emptyListing()
    lose()
    queuedSendStash.set('q1', { raw: 'held words', sent: 'held words', files: [FILE], quote })
    const h = setup()
    await lostCancel(h)
    expect(h.result.current.queueUnconfirmed.listedIds.has('q1')).toBe(true)
    act(() => { h.result.current.queueUnconfirmed.onRestore('q1') })
    expect(getUnconfirmedCancel(SLOT, 'q1')).toBeDefined()
    expect(h.drafts.current[SLOT]).toBeUndefined()
    expect(h.result.current.queueRefusal).toBeNull()
  })

  it('becomes an unconfirmed card when the listing no longer holds it, and Restore puts the text, file and quote in once', async () => {
    emptyListing()
    lose()
    queuedSendStash.set('q1', { raw: 'held words', sent: 'held words', files: [FILE], quote })
    const h = setup()
    h.drafts.current[SLOT] = 'mine'
    await lostCancel(h)
    h.listNow([])
    expect(h.result.current.queueUnconfirmed.messages.map(m => m.content)).toEqual(['held words'])
    expect(h.result.current.queueUnconfirmed.listedIds.has('q1')).toBe(false)
    expect(h.drafts.current[SLOT]).toBe('mine')
    act(() => { h.result.current.queueUnconfirmed.onRestore('q1') })
    act(() => { h.result.current.queueUnconfirmed.onRestore('q1') })
    expect(h.drafts.current[SLOT]).toBe('mine\n\nheld words')
    expect(h.fileDrafts.current[SLOT]).toEqual([FILE])
    expect(h.recoverInto).toHaveBeenCalledTimes(1)
    expect(h.recoverInto).toHaveBeenCalledWith('held words', quote, true)
    expect(h.result.current.queueUnconfirmed.messages).toHaveLength(0)
  })

  it('Dismiss removes it without touching the composer', async () => {
    emptyListing()
    lose()
    queuedSendStash.set('q1', { raw: 'held words', sent: 'held words', files: [FILE] })
    const h = setup()
    h.drafts.current[SLOT] = 'mine'
    await lostCancel(h)
    h.listNow([])
    act(() => { h.result.current.queueUnconfirmed.onDismiss('q1') })
    expect(h.result.current.queueUnconfirmed.messages).toHaveLength(0)
    expect(h.drafts.current[SLOT]).toBe('mine')
    expect(h.fileDrafts.current[SLOT]).toBeUndefined()
  })

  it('a retry that answers 200 restores exactly once and clears it', async () => {
    emptyListing()
    lose()
    queuedSendStash.set('q1', { raw: 'held words', sent: 'held words', files: [FILE] })
    const h = setup()
    await lostCancel(h)
    vi.spyOn(api, 'cancelQueuedMessage').mockResolvedValueOnce({ ok: true } as never)
    await act(async () => { h.result.current.handleCancelQueued('q1') })
    await waitFor(() => expect(getUnconfirmedCancel(SLOT, 'q1')).toBeUndefined())
    expect(h.drafts.current[SLOT]).toBe('held words')
    expect(h.recoverInto).toHaveBeenCalledTimes(1)
  })

  it('a retry that is refused clears it and takes the refusal path', async () => {
    emptyListing()
    lose()
    queuedSendStash.set('q1', { raw: 'held words', sent: 'held words', files: [FILE] })
    const h = setup()
    await lostCancel(h)
    vi.spyOn(api, 'cancelQueuedMessage').mockRejectedValueOnce(new ApiError(409, 'refused', '{"code":"queue_item_already_sent"}') as never)
    act(() => { h.result.current.handleCancelQueued('q1') })
    await waitFor(() => expect(h.result.current.queueRefusal?.error.message).toBe('refused'))
    expect(getUnconfirmedCancel(SLOT, 'q1')).toBeUndefined()
    expect(h.drafts.current[SLOT]).toBeUndefined()
  })

  it('a retry with no answer again keeps it exactly as it is', async () => {
    emptyListing()
    lose()
    queuedSendStash.set('q1', { raw: 'held words', sent: 'held words', files: [FILE] })
    const h = setup()
    await lostCancel(h)
    const first = getUnconfirmedCancel(SLOT, 'q1')
    lose()
    act(() => { h.result.current.handleCancelQueued('q1') })
    await waitFor(() => expect(h.result.current.queuePendingIds.has('q1')).toBe(false))
    expect(getUnconfirmedCancel(SLOT, 'q1')).toBe(first)
    expect(h.result.current.queueUnconfirmed.listedIds.has('q1')).toBe(true)
    expect(h.result.current.queueRefusal).toBeNull()
  })

  it('a retry that answers 404 keeps the payload for an unconfirmed card and restores nothing', async () => {
    emptyListing()
    lose()
    queuedSendStash.set('q1', { raw: 'held words', sent: 'held words', files: [FILE], quote })
    const h = setup()
    await lostCancel(h)
    vi.spyOn(api, 'cancelQueuedMessage').mockRejectedValueOnce(new ApiError(404, 'gone', '{}') as never)
    act(() => { h.result.current.handleCancelQueued('q1') })
    await waitFor(() => expect(h.result.current.queuePendingIds.has('q1')).toBe(false))
    h.listNow([])
    expect(h.result.current.queueUnconfirmed.messages.map(m => m.content)).toEqual(['held words'])
    expect(h.result.current.queueUnconfirmed.listedIds.has('q1')).toBe(false)
    expect(h.drafts.current[SLOT]).toBeUndefined()
    act(() => { h.result.current.queueUnconfirmed.onRestore('q1') })
    expect(h.drafts.current[SLOT]).toBe('held words')
    expect(h.fileDrafts.current[SLOT]).toEqual([FILE])
    expect(h.recoverInto).toHaveBeenCalledWith('held words', quote, true)
  })

  it('outlives the host: an unmount and a new host on the slot still draw it (GPT :186, Opus 1)', async () => {
    emptyListing()
    let fail!: (e: unknown) => void
    vi.spyOn(api, 'cancelQueuedMessage').mockReturnValueOnce(new Promise((_, r) => { fail = r }) as never)
    queuedSendStash.set('q1', { raw: 'held words', sent: 'held words', files: [FILE], quote })
    const old = setup()
    act(() => { old.result.current.handleCancelQueued('q1') })
    // The person leaves before the DELETE settles; it lands, its reply is lost.
    old.composerMountedRef.current = false
    old.unmount()
    await act(async () => { fail(new TypeError('Failed to fetch')) })
    // Returning to the slot: the listing no longer has the entry.
    const back = setup([])
    expect(back.result.current.queueUnconfirmed.messages.map(m => m.content)).toEqual(['held words'])
    const second = setup([])
    expect(second.result.current.queueUnconfirmed.messages.map(m => m.content)).toEqual(['held words'])
    act(() => { back.result.current.queueUnconfirmed.onRestore('q1') })
    expect(back.drafts.current[SLOT]).toBe('held words')
    expect(second.result.current.queueUnconfirmed.messages).toHaveLength(0)
  })

  it('keeps Cancel reachable when the DELETE never reached the server (GPT :506, Opus 2)', async () => {
    emptyListing()
    lose()
    queuedSendStash.set('q1', { raw: 'held words', sent: 'held words', files: [FILE] })
    const h = setup()
    await lostCancel(h)
    // The refresh still lists it: no unconfirmed card, the queue card stays.
    h.listNow([queued])
    expect(h.result.current.queueUnconfirmed.listedIds.has('q1')).toBe(true)
    act(() => { h.result.current.queueUnconfirmed.onRestore('q1') })
    expect(h.drafts.current[SLOT]).toBeUndefined()
    vi.spyOn(api, 'cancelQueuedMessage').mockResolvedValueOnce({ ok: true } as never)
    await act(async () => { h.result.current.handleCancelQueued('q1') })
    await waitFor(() => expect(h.drafts.current[SLOT]).toBe('held words'))
    expect(getUnconfirmedCancel(SLOT, 'q1')).toBeUndefined()
  })
})

describe('a cancel confirmed after the chat page was replaced', () => {
  afterEach(() => { localStorage.clear(); sessionStorage.clear() })

  it('spells the mention against the new page\'s staged files, not the old page\'s', async () => {
    emptyListing()
    let confirm!: (v: unknown) => void
    vi.spyOn(api, 'cancelQueuedMessage').mockReturnValue(new Promise(r => { confirm = r }) as never)
    const foreignTmp: ChatMessage = { ...queued, content: 'against [attached_file 1] /tmp/y/a.txt', meta: { queueId: 'q1', files: ['/tmp/y/a.txt'] } }
    const old = setup([foreignTmp])
    act(() => { old.result.current.handleCancelQueued('q1') })
    // The person leaves the chat, then reopens it and stages a same-named file.
    old.composerMountedRef.current = false
    old.unmount()
    setup([], ['/tmp/x/a.txt'])
    await act(async () => { confirm({ ok: true }) })
    await waitFor(() => expect(loadDrafts()[SLOT]).toBe('against @y/a.txt'))
    const payload = prepareSendPayload(loadDrafts()[SLOT], ['/tmp/x/a.txt', ...(loadFileDrafts()[SLOT] ?? [])])
    expect(payload.txt.startsWith('against [attached_file 1] /tmp/y/a.txt')).toBe(true)
    expect(payload.txt.match(/\/tmp\/y\/a\.txt/g)).toHaveLength(1)
  })
})

describe('the chat page publishing its staged files', () => {
  it('does not overwrite a pane\'s live list while split view hides the page composer, and republishes on leaving it', () => {
    const h = setup([queued], ['/repo/stale.txt'])
    expect(liveStagedFiles(SLOT)).toEqual(['/repo/stale.txt'])
    // Entering split view: the page stays mounted, its composer is off screen.
    h.showComposer(false)
    expect(liveStagedFiles(SLOT)).toBeUndefined()
    const unpublish = setLivePaneStaged(SLOT, {}, ['/repo/live.txt'])
    h.listNow([queued])
    expect(liveStagedFiles(SLOT)).toEqual(['/repo/live.txt'])
    unpublish()
    // Leaving it: the page composer is back and publishes again.
    h.showComposer(true)
    expect(liveStagedFiles(SLOT)).toEqual(['/repo/stale.txt'])
    h.unmount()
    expect(liveStagedFiles(SLOT)).toBeUndefined()
  })
})
