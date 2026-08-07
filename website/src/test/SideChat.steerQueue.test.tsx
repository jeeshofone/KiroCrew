import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import reducer, { sseSideResult, sseSideQueue } from '../store/chatSlice'
import { renderWithProviders, createTestStore } from './helpers'

vi.mock('../api/client', () => ({
  api: {
    sideOpen: vi.fn().mockResolvedValue({ ok: true, open: true, messages: 0, last_run_id: '', created_at: new Date().toISOString() }),
    sideTurn: vi.fn().mockResolvedValue({ ok: true, run_id: 'r1', messages: 1 }),
    sideClose: vi.fn().mockResolvedValue({ ok: true, was_open: true }),
    sideQueueCancel: vi.fn().mockResolvedValue({ ok: true, content: 'queued text', depth: 0 }),
    sideQueueEdit: vi.fn().mockResolvedValue({ ok: true, depth: 1 }),
  },
  SEARCH_MIN_CHARS: 2,
}))

import SideChat from '../pages/chat/SideChat'
import { api } from '../api/client'

const SLOT = 'test-slot-1'
const initial = reducer(undefined, { type: '@@INIT' })

/** A side that is mid-turn: the answer is streaming, so a submit can only
 *  steer or queue. */
function busyState(extra: Record<string, unknown> = {}) {
  return createTestStore({
    chat: {
      ...initial,
      activeSlot: SLOT,
      slotSide: {
        [SLOT]: {
          messages: [
            { role: 'user' as const, content: 'q1', ts: '2026-05-20T00:00:00Z', run_id: 'r1' },
            { role: 'assistant' as const, content: 'partial', ts: '2026-05-20T00:00:01Z', run_id: 'r1' },
          ],
          lastRunId: 'r1',
          pending: false,
          streaming: true,
          openedAtTurnCount: 0,
          createdAt: '2026-05-20T00:00:00Z',
          ...extra,
        },
      },
    },
  })
}

describe('SideChat busy-send: steer vs queue', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
  })

  it('shows the split send button while a turn is in flight and steers by default', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SideChat slot={SLOT} />, { store: busyState() })

    await user.type(screen.getByLabelText('Ask a side question'), 'actually use QUIC')
    await user.click(screen.getByTestId('busy-send-button'))

    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'actually use QUIC', { steer: true }))
  })

  it('Queue mode submits without the steer flag', async () => {
    const user = userEvent.setup()
    renderWithProviders(<SideChat slot={SLOT} />, { store: busyState() })

    await user.click(screen.getByTestId('busy-send-caret'))
    await user.click(screen.getByTestId('busy-send-mode-queue'))
    await user.type(screen.getByLabelText('Ask a side question'), 'later please')
    await user.click(screen.getByTestId('busy-send-button'))

    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'later please', undefined))
  })

  it('an idle side keeps the plain send button and never sends a steer flag', async () => {
    const user = userEvent.setup()
    const store = createTestStore({ chat: { ...initial, activeSlot: SLOT } })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    await user.type(screen.getByLabelText('Ask a side question'), 'fresh question')
    expect(screen.queryByTestId('busy-send-button')).not.toBeInTheDocument()
    await user.click(screen.getByLabelText('Send'))

    await waitFor(() => expect(api.sideTurn).toHaveBeenCalledWith(SLOT, 'fresh question', undefined))
  })

  it('the composer stays usable while a turn runs', () => {
    renderWithProviders(<SideChat slot={SLOT} />, { store: busyState() })
    expect(screen.getByLabelText('Ask a side question')).not.toBeDisabled()
  })

  it('a rejected submit keeps BOTH its text and whatever was typed since', async () => {
    const user = userEvent.setup()
    // The queue-full 429 makes rejection a reachable path, and the composer is
    // live during the request, so the user can be mid-draft when it lands. The
    // test settles the request itself rather than racing a timer, so "typed in
    // flight" is a fact of the schedule and not of how fast typing happens.
    let failRequest!: (err: Error) => void
    vi.mocked(api.sideTurn).mockImplementationOnce(
      () => new Promise((_resolve, reject) => { failRequest = reject })
    )
    renderWithProviders(<SideChat slot={SLOT} />, { store: busyState() })

    const box = screen.getByLabelText('Ask a side question')
    await user.type(box, 'rejected one')
    await user.click(screen.getByTestId('busy-send-button'))
    // onMutate cleared the draft; the user starts a new thought while it is in flight.
    await waitFor(() => expect(box).toHaveValue(''))
    await user.type(box, 'a new thought')

    failRequest(new Error('side queue is full (max 20)'))

    await waitFor(() => expect(box).toHaveValue('a new thought\n\nrejected one'))
  })
})

describe('SideChat queue cards', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
  })

  it('cancels through the server and only then hands the text back', async () => {
    const user = userEvent.setup()
    const store = busyState({
      queue: [{ id: 'q-1', content: 'queued text', ts: '2026-05-20T00:00:02Z' }],
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    expect(screen.getByText('queued text')).toBeInTheDocument()
    await user.click(screen.getByLabelText('Cancel queued message'))

    await waitFor(() => expect(api.sideQueueCancel).toHaveBeenCalledWith(SLOT, 'q-1'))
    // The HTTP response is one of TWO convergence paths (the other is the
    // `chat.side_queue` frame), so the card retires without any WebSocket
    // delivery — a dropped socket cannot leave it stale forever.
    await waitFor(() => expect(store.getState().chat.slotSide[SLOT].queue).toEqual([]))
    await waitFor(() => expect(screen.getByLabelText('Ask a side question')).toHaveValue('queued text'))

    // And the frame arriving afterwards is a no-op rather than a double-apply.
    store.dispatch(sseSideQueue({ slot: SLOT, action: 'cancel', queue_id: 'q-1' }))
    expect(store.getState().chat.slotSide[SLOT].queue).toEqual([])
  })

  it('a cancel keeps BOTH the queued text and an in-progress draft', async () => {
    const user = userEvent.setup()
    const store = busyState({
      queue: [{ id: 'q-1', content: 'queued text', ts: '2026-05-20T00:00:02Z' }],
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    // Typing while something is queued is the intended flow now that the composer
    // stays live, so this is the common case — not an edge one.
    await user.type(screen.getByLabelText('Ask a side question'), 'half-typed')
    await user.click(screen.getByLabelText('Cancel queued message'))

    // Both are typed work and the released text has no other home, so neither
    // may be discarded — they are merged for the user to edit.
    await waitFor(() =>
      expect(screen.getByLabelText('Ask a side question')).toHaveValue('half-typed\n\nqueued text')
    )
  })

  it('a demoted steer says so instead of only showing a card', async () => {
    const user = userEvent.setup()
    vi.mocked(api.sideTurn).mockResolvedValueOnce({ ok: true, queued: true, demoted: true, queue_id: 'q-9', depth: 1 })
    renderWithProviders(<SideChat slot={SLOT} />, { store: busyState() })

    await user.type(screen.getByLabelText('Ask a side question'), 'too late')
    await user.click(screen.getByTestId('busy-send-button'))

    await waitFor(() =>
      expect(screen.getByText('The turn ended — queued instead')).toBeInTheDocument()
    )
  })

  it('a second cancel click cannot fire while the first is in flight', async () => {
    const user = userEvent.setup()
    // The card is only retired when the server's frame lands, so it stays on
    // screen through the request. A duplicate would race the first and 404 —
    // reporting a failure for a cancel that worked.
    let release!: () => void
    vi.mocked(api.sideQueueCancel).mockImplementationOnce(
      () => new Promise(resolve => { release = () => resolve({ ok: true, content: 'queued text', depth: 0 }) })
    )
    const store = busyState({
      queue: [{ id: 'q-1', content: 'queued text', ts: '2026-05-20T00:00:02Z' }],
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    const cancelBtn = screen.getByLabelText('Cancel queued message')
    await user.click(cancelBtn)
    await waitFor(() => expect(cancelBtn).toBeDisabled())
    await user.click(cancelBtn)

    expect(api.sideQueueCancel).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('Could not cancel that queued question')).not.toBeInTheDocument()

    release()
    // Once the server confirms, the card is retired outright — so "pending
    // cleared" is no longer observable on the button; the card's absence is the
    // post-success state to assert.
    await waitFor(() => expect(screen.queryByLabelText('Cancel queued message')).not.toBeInTheDocument())
  })

  it('a cancel frame with no HTTP response still releases the text', async () => {
    // Mirror case of the WS-loss test: the DELETE succeeds server-side but its
    // response never arrives, so only the frame lands. It must still hand the
    // text back, or a confirmed cancel silently destroys the question.
    const store = busyState({
      queue: [{ id: 'q-1', content: 'released by frame', ts: '2026-05-20T00:00:02Z' }],
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    store.dispatch(sseSideQueue({ slot: SLOT, action: 'cancel', queue_id: 'q-1', content: 'released by frame' }))

    await waitFor(() => expect(screen.getByLabelText('Ask a side question')).toHaveValue('released by frame'))
    // Released exactly once — the stash is cleared, so a redelivered frame or the
    // late HTTP response cannot append it a second time.
    expect(store.getState().chat.slotSide[SLOT].releasedText).toBeUndefined()
    store.dispatch(sseSideQueue({ slot: SLOT, action: 'cancel', queue_id: 'q-1', content: 'released by frame' }))
    await waitFor(() => expect(screen.getByLabelText('Ask a side question')).toHaveValue('released by frame'))
  })

  it('a cancel the server refuses leaves the card standing and reports it', async () => {
    const user = userEvent.setup()
    // A drain can dequeue the entry between render and click — the server then
    // 404s, and the card must NOT disappear as though the text were cancelled.
    vi.mocked(api.sideQueueCancel).mockRejectedValueOnce(new Error('queue entry not found'))
    const store = busyState({
      queue: [{ id: 'q-1', content: 'already running', ts: '2026-05-20T00:00:02Z' }],
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    await user.click(screen.getByLabelText('Cancel queued message'))

    await waitFor(() => expect(screen.getByText('Could not cancel that queued question')).toBeInTheDocument())
    expect(store.getState().chat.slotSide[SLOT].queue).toHaveLength(1)
    expect(screen.getByLabelText('Ask a side question')).toHaveValue('')
  })

  it('edits through the server and takes the content from its frame', async () => {
    const user = userEvent.setup()
    const store = busyState({
      queue: [{ id: 'q-1', content: 'old', ts: '2026-05-20T00:00:02Z' }],
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    await user.click(screen.getByLabelText('Edit queued message'))
    const editor = screen.getByLabelText('Edit queued message')
    await user.clear(editor)
    await user.type(editor, 'new{Enter}')

    await waitFor(() => expect(api.sideQueueEdit).toHaveBeenCalledWith(SLOT, 'q-1', 'new'))
    // Converges from the HTTP response, without needing the WS frame.
    await waitFor(() => expect(store.getState().chat.slotSide[SLOT].queue?.[0].content).toBe('new'))

    // The frame arriving afterwards is idempotent.
    store.dispatch(sseSideQueue({ slot: SLOT, action: 'edit', queue_id: 'q-1', content: 'new' }))
    expect(store.getState().chat.slotSide[SLOT].queue?.[0].content).toBe('new')
  })

  it('an edit the server refuses leaves the old content and reports it', async () => {
    const user = userEvent.setup()
    vi.mocked(api.sideQueueEdit).mockRejectedValueOnce(new Error('queue entry not found'))
    const store = busyState({
      queue: [{ id: 'q-1', content: 'old', ts: '2026-05-20T00:00:02Z' }],
    })
    renderWithProviders(<SideChat slot={SLOT} />, { store })

    await user.click(screen.getByLabelText('Edit queued message'))
    const editor = screen.getByLabelText('Edit queued message')
    await user.clear(editor)
    await user.type(editor, 'new{Enter}')

    await waitFor(() => expect(screen.getByText('Could not update that queued question')).toBeInTheDocument())
    expect(store.getState().chat.slotSide[SLOT].queue?.[0].content).toBe('old')
  })
})

describe('chatSlice side queue reducer', () => {
  it('push appends, edit rewrites, cancel and drain remove', () => {
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'a', content: 'first' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'b', content: 'second' }))
    expect(state.slotSide[SLOT].queue?.map(e => e.content)).toEqual(['first', 'second'])

    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'edit', queue_id: 'a', content: 'first edited' }))
    expect(state.slotSide[SLOT].queue?.map(e => e.content)).toEqual(['first edited', 'second'])

    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'drain', queue_id: 'a' }))
    expect(state.slotSide[SLOT].queue?.map(e => e.id)).toEqual(['b'])

    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'cancel', queue_id: 'b' }))
    expect(state.slotSide[SLOT].queue).toEqual([])
  })

  it('a redelivered push updates in place instead of doubling the card', () => {
    let state = reducer(initial, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'a', content: 'x' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'a', content: 'x' }))
    expect(state.slotSide[SLOT].queue).toHaveLength(1)
  })

  it('a head-insert push PREPENDS, matching the order the backend will run', () => {
    // A requeued steer and a failed drain's entry go to the HEAD server-side.
    // Appending them would show a different next question than the backend runs.
    let state = reducer(initial, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'existing', content: 'already queued' }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'steer-1', content: 'first steer', front: true }))
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'steer-2', content: 'second steer', front: true }))

    // The backend inserts each at the head in reverse order, so the resulting
    // order is [second, first, existing] on both sides.
    expect(state.slotSide[SLOT].queue?.map(e => e.content)).toEqual([
      'second steer',
      'first steer',
      'already queued',
    ])
  })

  it('a queue frame never resurrects a closed side', () => {
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q' }))
    state = reducer(state, { type: 'chat/sideClose', payload: SLOT })
    state = reducer(state, sseSideQueue({ slot: SLOT, action: 'push', queue_id: 'a', content: 'late' }))
    expect(state.slotSide[SLOT]).toBeUndefined()
  })
})

describe('chatSlice steer frame placement', () => {
  it('lands the steer bubble ABOVE the streaming answer so the terminal frame still replaces it', () => {
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'partial' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'steer me', steer: true }))

    const rows = state.slotSide[SLOT].messages
    expect(rows.map(m => [m.role, m.content])).toEqual([
      ['user', 'q1'],
      ['user', 'steer me'],
      ['assistant', 'partial'],
    ])
    expect(rows[1].steer).toBe(true)

    // Terminal frame carries the WHOLE turn: it must replace the assistant row,
    // not append a fourth row or concatenate onto the partial text.
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'partial and the rest', final: true }))
    const after = state.slotSide[SLOT].messages
    expect(after).toHaveLength(3)
    expect(after[2].content).toBe('partial and the rest')
    expect(state.slotSide[SLOT].streaming).toBe(false)
  })

  it('a steer frame arriving after the terminal frame does not revive busy state', () => {
    // The steer RPC and the stream are separate paths, so the chip can land after
    // the turn has already finished. Reviving pending/streaming there strands the
    // panel: nothing later would clear it, and the composer keeps offering Steer
    // for a turn that ended.
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'assistant', content: 'the whole answer', final: true }))
    expect(state.slotSide[SLOT].streaming).toBe(false)
    expect(state.slotSide[SLOT].pending).toBe(false)

    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'late steer', steer: true }))

    expect(state.slotSide[SLOT].streaming).toBe(false)
    expect(state.slotSide[SLOT].pending).toBe(false)
    // The chip is still recorded — it just does not claim the turn is live.
    expect(state.slotSide[SLOT].messages.some(m => m.steer)).toBe(true)
  })

  it('a steer arriving before any delta simply appends', () => {
    let state = reducer(initial, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'q1' }))
    state = reducer(state, sseSideResult({ slot: SLOT, run_id: 'r1', role: 'user', content: 'early steer', steer: true }))
    expect(state.slotSide[SLOT].messages.map(m => m.content)).toEqual(['q1', 'early steer'])
  })
})
