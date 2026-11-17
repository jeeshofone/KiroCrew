import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { ReactNode } from 'react'
import { act, render, waitFor, fireEvent, within } from '@testing-library/react'
import type { RootState } from '../store'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ThemeProvider } from '../hooks/useTheme'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

/* A crewmate's chat draws a queued message as the person's own bubble with no
 * controls, because the next drain sends it. A held entry (the server's
 * `meta.held`) is never drained, so it keeps the queue card with send now, edit
 * and cancel. A restored entry the server will still drain is not held. */

vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: { data?: unknown[]; itemContent: (index: number, item: unknown) => ReactNode }) => (
    <div data-testid="virtuoso">{data?.map((d: unknown, i: number) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))
vi.mock('../api/client', () => ({
  api: {
    chatSlots: vi.fn().mockResolvedValue([]),
    chatSlotDetail: vi.fn().mockResolvedValue({ messages: [], running: false, has_more: false, total: 0 }),
    sendChat: vi.fn().mockResolvedValue({ ok: true, json: () => Promise.resolve({ ok: true }) }),
    cancelQueuedMessage: vi.fn().mockResolvedValue({ ok: true }),
    editQueuedMessage: vi.fn().mockResolvedValue({ ok: true }),
    holdQueuedMessageEdit: vi.fn().mockResolvedValue({ ok: true }),
    interruptSlot: vi.fn().mockResolvedValue({ queue_selected: true }),
    chatHistory: vi.fn().mockResolvedValue({ sessions: [] }),
    models: vi.fn().mockResolvedValue([]),
    agents: vi.fn().mockResolvedValue([]),
    agentDetail: vi.fn().mockResolvedValue({}),
    workspaces: vi.fn().mockResolvedValue({ workspaces: [] }),
    spawnList: vi.fn().mockResolvedValue({ agents: [] }),
    uploadFiles: vi.fn().mockResolvedValue({ paths: [] }),
    screenshot: vi.fn().mockResolvedValue({ path: null }),
    fileSearch: vi.fn().mockResolvedValue({ root: '/repo', results: [] }),
  },
  SEARCH_MIN_CHARS: 2,
}))
vi.mock('../hooks/useVoiceInput', () => ({ useVoiceInput: () => ({ recording: false, transcribing: false, toggle: vi.fn() }), voiceInputSupported: false }))
vi.mock('../hooks/useBranding', () => ({ useBranding: () => ({ botName: 'Kiro Crew', avatar: '' }) }))
vi.mock('../hooks/useAgents', () => ({ useAgents: () => ({ agents: [], defaultAgent: 'default' }) }))
vi.mock('../components/MarkdownRenderer', () => ({ default: ({ content }: { content: string }) => <span>{content}</span> }))
vi.mock('../hooks/useWebSocket', () => ({ useWebSocket: () => ({ subscribeLogs: () => {} }) }))

Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockReturnValue({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
})

import ChatPane from '../components/ChatPane'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { readPaneDraft, takePaneDraft, writePaneDraft } from '../utils/chatPaneDrafts'
import { queuedSendStash } from '../hooks/useQueuedMessageActions'
import { landComposerAttachments } from '../utils/composerSendHolds'
import { __resetUnconfirmedCancelsForTests } from '../utils/unconfirmedCancels'

const SLOT = 'member-radar'
const HISTORY = [
  { role: 'user', content: 'first words', cls: 'msg msg-u', ts: '2026-09-22T06:00:00Z' },
  { role: 'assistant', content: 'a reply', cls: '', ts: '2026-09-22T06:00:09Z' },
]

function renderPane(slotKey = SLOT) {
  const store = configureStore({
    reducer: { dashboard: dashboardReducer, chat: chatReducer, notifications: notificationsReducer },
    preloadedState: {
      dashboard: {
        status: null, connected: true,
        slots: [{ key: SLOT, messages: HISTORY.length, running: false, mode: '', pending_approval: false, waiting_for_input: false, last_activity_ts: undefined }],
        slotsLoaded: true,
        unreadSlots: [], refreshTrigger: 0, approvalMode: 'normal',
        subagentRunning: {}, subagentDetails: {}, subagentText: {},
      } as unknown as RootState['dashboard'],
    } as Partial<RootState>,
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const tree = (key: string) => (
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ThemeProvider>
          <MemoryRouter>
            <ChatPane slotKey={key} crewmate={{ name: 'Radar' }} />
          </MemoryRouter>
        </ThemeProvider>
      </QueryClientProvider>
    </Provider>
  )
  const view = render(tree(slotKey))
  // The Members page rebinds one pane to another crewmate without remounting it.
  return Object.assign(view, { switchTo: (key: string) => view.rerender(tree(key)) })
}

beforeEach(() => {
  vi.clearAllMocks()
  // Drop any one-shot answer a test left unconsumed.
  for (const fn of [api.cancelQueuedMessage, api.editQueuedMessage]) (fn as ReturnType<typeof vi.fn>).mockReset().mockResolvedValue({ ok: true })
  ;(api.interruptSlot as ReturnType<typeof vi.fn>).mockReset().mockResolvedValue({ queue_selected: true })
  // A cancel puts the entry's words in the composer, and an unmounted pane
  // parks its composer per slot: each test starts with an empty one.
  sessionStorage.clear()
  takePaneDraft(SLOT)
  queuedSendStash.clear()
  __resetUnconfirmedCancelsForTests()
  ;(api.cancelQueuedMessage as ReturnType<typeof vi.fn>).mockImplementation(() => Promise.resolve({ ok: true }))
  ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
    messages: HISTORY, running: false, has_more: false, total: HISTORY.length,
    queue: [
      { id: 'held-1', content: 'held words', meta: { restored: true, held: true } },
      { id: 'drain-1', content: 'restored drained words', meta: { restored: true } },
      { id: 'live-1', content: 'live words' },
    ],
  })
})


/** What the server lists after a refused cancel or edit: the refetch draws it. */
function serverLists(queue: Array<{ id: string; content: string; meta?: Record<string, unknown> }>) {
  ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
    messages: HISTORY, running: false, has_more: false, total: HISTORY.length, queue,
  })
}

describe("a held queued message in a mate's chat", () => {
  it('keeps the queue card with send now, edit and cancel', async () => {
    const view = renderPane()
    await view.findByText('held words')
    expect(view.getAllByLabelText('Send now')).toHaveLength(1)
    expect(view.getAllByLabelText('Edit queued message')).toHaveLength(1)
    expect(view.getAllByLabelText('Cancel queued message')).toHaveLength(1)
  })

  it('cancels the restored entry through the queue endpoint', async () => {
    const view = renderPane()
    await view.findByText('held words')
    fireEvent.click(view.getByLabelText('Cancel queued message'))
    await waitFor(() => expect(api.cancelQueuedMessage).toHaveBeenCalledWith(SLOT, 'held-1'))
  })

  it('sends the held entry now through the interrupt endpoint', async () => {
    const view = renderPane()
    // Read inside the card: a cancel in another test puts the same words back
    // in the slot's composer draft.
    const card = await view.findByTestId('queue-card')
    fireEvent.click(within(card).getByLabelText('Send now'))
    await waitFor(() => expect(api.interruptSlot).toHaveBeenCalledWith(SLOT, 'held-1'))
  })

  it('offers only Send now on a held entry whose row is already written', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: HISTORY, running: false, has_more: false, total: HISTORY.length,
      queue: [{ id: 'held-1', content: 'held words', meta: { restored: true, held: true, written: true } }],
    })
    const view = renderPane()
    const card = await view.findByTestId('queue-card')
    expect(within(card).getAllByLabelText('Send now')).toHaveLength(1)
    expect(within(card).queryByLabelText('Edit queued message')).toBeNull()
    expect(within(card).queryByLabelText('Cancel queued message')).toBeNull()
  })

  it('puts the card back when the server refuses the cancel', async () => {
    const refused = new ApiError(403, 'refused', '{"error":"denied"}')
    ;(api.cancelQueuedMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce(refused)
    const view = renderPane()
    fireEvent.click(within(await view.findByTestId('queue-card')).getByLabelText('Cancel queued message'))
    await waitFor(() => expect(api.cancelQueuedMessage).toHaveBeenCalledWith(SLOT, 'held-1'))
    const card = await view.findByTestId('queue-card')
    expect(within(card).getAllByLabelText('Cancel queued message')).toHaveLength(1)
  })

  it('rebuilds a cancel refused as already sent from the server, with Send now only', async () => {
    const refused = new ApiError(409, 'refused', '{"error":"x","code":"queue_item_already_sent"}')
    ;(api.cancelQueuedMessage as ReturnType<typeof vi.fn>).mockImplementationOnce(() => {
      serverLists([{ id: 'held-1', content: 'held words', meta: { restored: true, held: true, written: true } }])
      return Promise.reject(refused)
    })
    const view = renderPane()
    fireEvent.click(within(await view.findByTestId('queue-card')).getByLabelText('Cancel queued message'))
    const card = await view.findByTestId('queue-card')
    await waitFor(() => expect(within(card).queryByLabelText('Cancel queued message')).toBeNull())
    expect(within(card).getAllByLabelText('Send now')).toHaveLength(1)
    expect(within(card).queryByLabelText('Edit queued message')).toBeNull()
  })

  // Cancel is pessimistic: the composer changes only after the server
  // confirms, so a refusal has nothing to take back out.
  const composerOf = (view: ReturnType<typeof renderPane>) => () => view.getByPlaceholderText(/Message Radar/) as HTMLTextAreaElement
  const refusedSent = () => new ApiError(409, 'refused', '{"code":"queue_item_already_sent"}')

  it('disables the card actions and leaves the composer alone while the cancel is pending', async () => {
    ;(api.cancelQueuedMessage as ReturnType<typeof vi.fn>).mockReturnValueOnce(new Promise(() => {}))
    const view = renderPane()
    const composer = composerOf(view)
    fireEvent.change(composer(), { target: { value: 'my draft' } })
    const card = await view.findByTestId('queue-card')
    fireEvent.click(within(card).getByLabelText('Cancel queued message'))
    await waitFor(() => expect(within(card).getByLabelText('Cancel queued message')).toBeDisabled())
    expect(within(card).getByLabelText('Send now')).toBeDisabled()
    expect(within(card).getByLabelText('Edit queued message')).toBeDisabled()
    expect(composer().value).toBe('my draft')
  })

  it('puts the words in the composer only after the server confirms, after a paragraph break', async () => {
    let confirm!: (v: unknown) => void
    ;(api.cancelQueuedMessage as ReturnType<typeof vi.fn>).mockReturnValueOnce(new Promise(r => { confirm = r }))
    const view = renderPane()
    const composer = composerOf(view)
    fireEvent.change(composer(), { target: { value: 'my draft' } })
    fireEvent.click(within(await view.findByTestId('queue-card')).getByLabelText('Cancel queued message'))
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    expect(composer().value).toBe('my draft')
    await act(async () => { confirm({ ok: true }) })
    await waitFor(() => expect(composer().value).toBe('my draft\n\nheld words'))
  })

  it('leaves the composer byte-identical across a refused cancel and brings the card back from the listing', async () => {
    let refuse!: (e: unknown) => void
    ;(api.cancelQueuedMessage as ReturnType<typeof vi.fn>).mockReturnValueOnce(new Promise((_, r) => { refuse = r }))
    queuedSendStash.set('held-1', { raw: 'held words', sent: 'held words', files: ['/tmp/report-x1.pdf'] })
    const view = renderPane()
    const composer = composerOf(view)
    fireEvent.change(composer(), { target: { value: 'my draft  \n' } })
    fireEvent.click(within(await view.findByTestId('queue-card')).getByLabelText('Cancel queued message'))
    serverLists([{ id: 'held-1', content: 'held words', meta: { restored: true, held: true, written: true } }])
    await act(async () => { refuse(refusedSent()) })
    await view.findByTestId('queue-action-refused')
    const card = await view.findByTestId('queue-card')
    await waitFor(() => expect(within(card).queryByLabelText('Cancel queued message')).toBeNull())
    expect(composer().value).toBe('my draft  \n')
    expect(view.queryAllByText(/report-x1\.pdf/)).toHaveLength(0)
  })

  it('puts the words in the clicked chat\'s draft when the pane moved to another chat before the server confirmed', async () => {
    takePaneDraft('member-other')
    let confirm!: (v: unknown) => void
    ;(api.cancelQueuedMessage as ReturnType<typeof vi.fn>).mockReturnValueOnce(new Promise(r => { confirm = r }))
    const view = renderPane()
    const composer = composerOf(view)
    fireEvent.click(within(await view.findByTestId('queue-card')).getByLabelText('Cancel queued message'))
    await act(async () => { view.switchTo('member-other') })
    fireEvent.change(composer(), { target: { value: 'words for the other chat' } })
    await act(async () => { confirm({ ok: true }) })
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    expect(composer().value).toBe('words for the other chat')
    expect(readPaneDraft(SLOT).text).toBe('held words')
    await act(async () => { view.switchTo(SLOT) })
    await waitFor(() => expect(composer().value).toBe('held words'))
    takePaneDraft('member-other')
  })

  // A reloaded entry has no stash, so the parser spells its mention against
  // the files the clicked slot stages when the server confirms.
  const foreignY = { id: 'held-1', content: 'against [attached_file 1] /repo/y/a.txt', meta: { restored: true, held: true, files: ['/repo/y/a.txt'] } }

  it('binds the restored mention to the queued file when the pane staged a same-named file during the DELETE', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: HISTORY, running: false, has_more: false, total: HISTORY.length,
      queue: [foreignY, { id: 'held-2', content: 'see x', meta: { restored: true, held: true } }],
    })
    // The second card's own cancel stages /repo/x/a.txt in this slot while the first is pending.
    queuedSendStash.set('held-2', { raw: 'see x', sent: 'see x', files: ['/repo/x/a.txt'] })
    let confirmFirst!: (v: unknown) => void
    ;(api.cancelQueuedMessage as ReturnType<typeof vi.fn>).mockImplementation((_slot: string, id: string) =>
      id === 'held-1' ? new Promise(r => { confirmFirst = r }) : Promise.resolve({ ok: true }))
    const view = renderPane()
    const composer = composerOf(view)
    const cancelOf = async (text: RegExp) => {
      const card = (await view.findByText(text)).closest('[data-testid="queue-card"]') as HTMLElement
      await act(async () => { fireEvent.click(within(card).getByLabelText('Cancel queued message')) })
    }
    fireEvent.click(await view.findByText(/^against/))
    await cancelOf(/^against/)
    await cancelOf(/^see x$/)
    await waitFor(() => expect(composer().value).toBe('see x'))
    await act(async () => { confirmFirst({ ok: true }) })
    await waitFor(() => expect(composer().value).toBe('see x\n\nagainst @y/a.txt'))
  })

  it('binds the restored mention against the parked draft when the pane moved to another chat', async () => {
    takePaneDraft('member-other')
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: HISTORY, running: false, has_more: false, total: HISTORY.length, queue: [foreignY],
    })
    let confirm!: (v: unknown) => void
    ;(api.cancelQueuedMessage as ReturnType<typeof vi.fn>).mockReturnValueOnce(new Promise(r => { confirm = r }))
    const view = renderPane()
    fireEvent.click(within(await view.findByTestId('queue-card')).getByLabelText('Cancel queued message'))
    await act(async () => { view.switchTo('member-other') })
    writePaneDraft(SLOT, { ...readPaneDraft(SLOT), files: ['/repo/x/a.txt'] })
    await act(async () => { confirm({ ok: true }) })
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    expect(readPaneDraft(SLOT).text).toBe('against @y/a.txt')
    expect(readPaneDraft(SLOT).files).toEqual(['/repo/x/a.txt', '/repo/y/a.txt'])
    takePaneDraft('member-other')
  })

  it('binds the restored mention against a replacement pane that staged a same-named file during the DELETE', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: HISTORY, running: false, has_more: false, total: HISTORY.length, queue: [foreignY],
    })
    let confirm!: (v: unknown) => void
    ;(api.cancelQueuedMessage as ReturnType<typeof vi.fn>).mockReturnValueOnce(new Promise(r => { confirm = r }))
    const first = renderPane()
    fireEvent.click(within(await first.findByTestId('queue-card')).getByLabelText('Cancel queued message'))
    first.unmount()
    const next = renderPane()
    const composer = composerOf(next)
    await next.findByTestId('queue-card')
    act(() => { landComposerAttachments(SLOT, ['/repo/x/a.txt']) })
    await waitFor(() => expect(next.getAllByText(/a\.txt/).length).toBeGreaterThan(0))
    await act(async () => { confirm({ ok: true }) })
    await waitFor(() => expect(composer().value).toBe('against @y/a.txt'))
  })

  it('binds the restored mention against the parked draft when no pane shows the slot', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: HISTORY, running: false, has_more: false, total: HISTORY.length, queue: [foreignY],
    })
    let confirm!: (v: unknown) => void
    ;(api.cancelQueuedMessage as ReturnType<typeof vi.fn>).mockReturnValueOnce(new Promise(r => { confirm = r }))
    const view = renderPane()
    fireEvent.click(within(await view.findByTestId('queue-card')).getByLabelText('Cancel queued message'))
    view.unmount()
    writePaneDraft(SLOT, { ...readPaneDraft(SLOT), files: ['/repo/x/a.txt'] })
    await act(async () => { confirm({ ok: true }) })
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    expect(readPaneDraft(SLOT).text).toBe('against @y/a.txt')
    expect(readPaneDraft(SLOT).files).toEqual(['/repo/x/a.txt', '/repo/y/a.txt'])
  })

  it('leaves the composer unchanged when two identical cancels are both refused and the same paragraph was typed meanwhile', async () => {
    ;(api.chatSlotDetail as ReturnType<typeof vi.fn>).mockResolvedValue({
      messages: HISTORY, running: false, has_more: false, total: HISTORY.length,
      queue: [
        { id: 'held-1', content: 'held words', meta: { restored: true, held: true } },
        { id: 'held-2', content: 'held words', meta: { restored: true, held: true } },
      ],
    })
    const refusals: Array<(e: unknown) => void> = []
    ;(api.cancelQueuedMessage as ReturnType<typeof vi.fn>).mockImplementation(() => new Promise((_, r) => { refusals.push(r) }))
    const view = renderPane()
    const composer = composerOf(view)
    // Two cards draw collapsed with no per-card actions: open the stack first.
    fireEvent.click((await view.findAllByText('held words'))[0])
    for (const card of view.getAllByTestId('queue-card')) {
      await act(async () => { fireEvent.click(within(card).getByLabelText('Cancel queued message')) })
    }
    await waitFor(() => expect(refusals).toHaveLength(2))
    fireEvent.change(composer(), { target: { value: 'mine\n\nheld words' } })
    serverLists([
      { id: 'held-1', content: 'held words', meta: { restored: true, held: true, written: true } },
      { id: 'held-2', content: 'held words', meta: { restored: true, held: true, written: true } },
    ])
    await act(async () => { for (const refuse of refusals) refuse(refusedSent()) })
    await view.findByTestId('queue-action-refused')
    await act(async () => { await new Promise(r => setTimeout(r, 0)) })
    expect(composer().value).toBe('mine\n\nheld words')
  })

  it('keeps a listed entry\'s queue card with Cancel when the cancel gets no reply', async () => {
    ;(api.cancelQueuedMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const view = renderPane()
    fireEvent.click(within(await view.findByTestId('queue-card')).getByLabelText('Cancel queued message'))
    expect(await view.findByTestId('queue-cancel-no-reply')).toHaveTextContent('The cancel got no reply. Cancel again.')
    const card = view.getByTestId('queue-card')
    await waitFor(() => expect(within(card).getByLabelText('Cancel queued message')).not.toBeDisabled())
    expect(view.queryByTestId('queue-unconfirmed-card')).toBeNull()
    expect(view.queryByRole('button', { name: 'Restore to composer' })).toBeNull()
  })

  it('draws an unconfirmed card once the listing drops the entry, and Restore fills the composer', async () => {
    ;(api.cancelQueuedMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const view = renderPane()
    const composer = composerOf(view)
    const queueCard = await view.findByTestId('queue-card')
    // The refetch after the lost reply finds the entry gone.
    serverLists([])
    fireEvent.click(within(queueCard).getByLabelText('Cancel queued message'))
    const card = await view.findByTestId('queue-unconfirmed-card')
    expect(card).toHaveTextContent('held words')
    expect(card).toHaveTextContent('The cancel may not have reached the server')
    await waitFor(() => expect(view.queryByTestId('queue-card')).toBeNull())
    expect(view.getByTestId('queue-unconfirmed-card')).toBeInTheDocument()
    expect(composer().value).toBe('')
    fireEvent.click(within(card).getByRole('button', { name: 'Restore to composer' }))
    await waitFor(() => expect(composer().value).toBe('held words'))
    expect(view.queryByTestId('queue-unconfirmed-card')).toBeNull()
  })

  it('says why on a refused cancel, with no hand-off that would drop the draft', async () => {
    const reason = 'this message is already in the conversation and can no longer be cancelled'
    ;(api.cancelQueuedMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new ApiError(409, reason, '{"code":"queue_item_already_sent"}'))
    const view = renderPane()
    fireEvent.click(within(await view.findByTestId('queue-card')).getByLabelText('Cancel queued message'))
    const notice = await view.findByTestId('queue-action-refused')
    expect(notice).toHaveTextContent("Couldn't change the queued message")
    expect(notice).toHaveTextContent(reason)
    expect(within(notice).queryByRole('button', { name: /ask/i })).toBeNull()
  })

  it('draws the queue in the server order after a refusal, not from a remembered neighbour', async () => {
    serverLists([
      { id: 'a', content: 'first held', meta: { restored: true, held: true } },
      { id: 'b', content: 'second held', meta: { restored: true, held: true } },
      { id: 'c', content: 'third held', meta: { restored: true, held: true } },
    ])
    let refuseA!: (e: unknown) => void
    ;(api.cancelQueuedMessage as ReturnType<typeof vi.fn>).mockImplementation((_slot: string, id: string) =>
      id === 'a' ? new Promise((_, r) => { refuseA = r }) : Promise.resolve({ ok: true }))
    const view = renderPane()
    fireEvent.click(await view.findByText('first held'))
    // Each click settles inside `act`: a cancel that resolves at once updates
    // the pane, and that update must land before the test moves on.
    const cancelOf = async (text: string) => {
      const card = (await view.findByText(text)).closest('[data-testid="queue-card"]') as HTMLElement
      await act(async () => { fireEvent.click(within(card).getByLabelText('Cancel queued message')) })
    }
    await cancelOf('first held')
    await cancelOf('second held')
    // B left the queue; A stayed, ahead of C.
    serverLists([
      { id: 'a', content: 'first held', meta: { restored: true, held: true, written: true } },
      { id: 'c', content: 'third held', meta: { restored: true, held: true } },
    ])
    await act(async () => { refuseA(new ApiError(409, 'refused', '{"code":"queue_item_already_sent"}')) })
    await waitFor(() => expect(view.getAllByTestId('queue-card').map(c => (c.textContent?.includes('first held') ? 'a' : c.textContent?.includes('third held') ? 'c' : '?'))).toEqual(['a', 'c']))
  })

  it('does not bring back a card whose cancel answered 404 (the entry left the queue)', async () => {
    ;(api.cancelQueuedMessage as ReturnType<typeof vi.fn>).mockImplementationOnce(() => {
      serverLists([])
      return Promise.reject(new ApiError(404, 'gone', '{"error":"queue item not found"}'))
    })
    const view = renderPane()
    fireEvent.click(within(await view.findByTestId('queue-card')).getByLabelText('Cancel queued message'))
    await waitFor(() => expect(api.cancelQueuedMessage).toHaveBeenCalledWith(SLOT, 'held-1'))
    // Let the refused request's handlers run before reading the result.
    await act(async () => { await (api.cancelQueuedMessage as ReturnType<typeof vi.fn>).mock.results[0].value.catch(() => undefined) })
    expect(view.queryAllByTestId('queue-card')).toHaveLength(0)
  })

  it('puts the old words back when the server refuses the edit', async () => {
    const refused = new ApiError(409, 'refused', '{"code":"queue_item_already_sent"}')
    ;(api.editQueuedMessage as ReturnType<typeof vi.fn>).mockRejectedValueOnce(refused)
    const view = renderPane()
    const card = await view.findByTestId('queue-card')
    fireEvent.click(within(card).getByLabelText('Edit queued message'))
    const box = within(view.getByTestId('queue-card')).getByRole('textbox')
    fireEvent.change(box, { target: { value: 'new words' } })
    fireEvent.click(within(view.getByTestId('queue-card')).getByLabelText('Save edit'))
    await waitFor(() => expect(api.editQueuedMessage).toHaveBeenCalledWith(SLOT, 'held-1', 'new words'))
    await waitFor(() => expect(within(view.getByTestId('queue-card')).getByText('held words')).toBeInTheDocument())
  })

  it('says why when Send now fails', async () => {
    const reason = 'another stop owns this chat'
    ;(api.interruptSlot as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new ApiError(409, reason, '{}'))
    const view = renderPane()
    const card = await view.findByTestId('queue-card')
    await act(async () => { fireEvent.click(within(card).getByLabelText('Send now')) })
    expect(await view.findByTestId('queue-action-refused')).toHaveTextContent(reason)
  })

  it('says so when Send now finds the chat gone (404)', async () => {
    ;(api.interruptSlot as ReturnType<typeof vi.fn>).mockRejectedValueOnce(new ApiError(404, 'slot not found', '{"code":"slot_not_found"}'))
    const view = renderPane()
    const card = await view.findByTestId('queue-card')
    await act(async () => { fireEvent.click(within(card).getByLabelText('Send now')) })
    expect(await view.findByTestId('queue-action-refused')).toHaveTextContent('slot not found')
  })

  it('draws an ordinary queued message as a bubble with no controls', async () => {
    const view = renderPane()
    const bubble = await view.findByText('live words')
    expect(bubble.closest('[data-testid="queue-card"]')).toBeNull()
  })

  it('draws a restored entry the server will drain as a bubble with no controls', async () => {
    const view = renderPane()
    const bubble = await view.findByText('restored drained words')
    expect(bubble.closest('[data-testid="queue-card"]')).toBeNull()
    expect(view.getAllByTestId('queue-card')).toHaveLength(1)
  })
})
