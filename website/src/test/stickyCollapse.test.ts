/**
 * The geometry rule behind keeping a pinned folder header in place on collapse:
 * the lane scrolls up by exactly the distance the header is painted below its
 * block's top, measured, so it holds for every depth and for a header the
 * block's end is already pushing off. The sidebar wiring is pinned in
 * ChatSidebar.stickyFolderCollapse.test.tsx.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import { holdPinnedHeaderThroughCollapse, useHoldPinnedHeaderOnCollapse, HOLD_ARM_TTL_MS } from '../pages/chat-sidebar/stickyCollapse'
import type { ChatFolder } from '../types'

function world(laneScrollTop: number, blockTop: number, headerTop: number) {
  const lane = document.createElement('div')
  let top = laneScrollTop
  Object.defineProperty(lane, 'scrollTop', { configurable: true, get: () => top, set: (v: number) => { top = Math.max(0, v) } })
  const block = document.createElement('div')
  const header = document.createElement('div')
  header.setAttribute('data-folder-row', 'f')
  block.appendChild(header)
  lane.appendChild(block)
  const r = (y: number) => ({ top: y, bottom: y + 32, left: 0, right: 300, width: 300, height: 32, x: 0, y, toJSON: () => ({}) }) as DOMRect
  block.getBoundingClientRect = () => r(blockTop)
  header.getBoundingClientRect = () => r(headerTop)
  return { lane, block }
}

describe('holdPinnedHeaderThroughCollapse', () => {
  it('a nested header pinned one row below its parent keeps that painted row', () => {
    // Depth 1 pins at lane top + 32; its block's top is 250px above that.
    const { lane, block } = world(900, 132 - 250, 132)
    expect(holdPinnedHeaderThroughCollapse(lane, block)).toBe(250)
    expect(lane.scrollTop).toBe(650)
  })

  it('a header the block end is pushing off uses its pushed, painted offset', () => {
    // Pushed 20px above its pin: painted at 80, block top 400 above that.
    const { lane, block } = world(700, 80 - 400, 80)
    holdPinnedHeaderThroughCollapse(lane, block)
    expect(lane.scrollTop).toBe(300)
  })

  it('ignores sub-pixel offsets and a missing lane, block or header', () => {
    const { lane, block } = world(300, 100.3, 100.6)
    expect(holdPinnedHeaderThroughCollapse(lane, block)).toBe(0)
    expect(lane.scrollTop).toBe(300)
    expect(holdPinnedHeaderThroughCollapse(null, block)).toBe(0)
    expect(holdPinnedHeaderThroughCollapse(lane, null)).toBe(0)
    expect(holdPinnedHeaderThroughCollapse(lane, document.createElement('div'))).toBe(0)
  })

  it('reads only the block\'s OWN header, never a nested folder\'s', () => {
    const { lane, block } = world(500, 0, 0)
    const nested = document.createElement('div')
    const nestedHeader = document.createElement('div')
    nestedHeader.setAttribute('data-folder-row', 'child')
    nestedHeader.getBoundingClientRect = () => ({ top: 400 }) as DOMRect
    nested.appendChild(nestedHeader)
    block.appendChild(nested)
    expect(holdPinnedHeaderThroughCollapse(lane, block)).toBe(0)
  })
})

describe('useHoldPinnedHeaderOnCollapse', () => {
  afterEach(() => { vi.restoreAllMocks() })

  const folder = (collapsed: boolean): ChatFolder[] => [{ id: 'f', name: 'f', order: 0, collapsed } as ChatFolder]

  function setup() {
    // Header pinned 300px below its block's top, lane scrolled 500px.
    const { lane, block } = world(500, -200, 100)
    document.body.appendChild(lane)
    const laneRef = { current: lane }
    const hook = renderHook(({ folders }) => useHoldPinnedHeaderOnCollapse(laneRef, folders), { initialProps: { folders: folder(false) } })
    return { lane, block, hook }
  }

  it('holds on the first commit that shows the armed folder collapsed, and only once', () => {
    const { lane, block, hook } = setup()
    act(() => hook.result.current.armHold('f', block))
    hook.rerender({ folders: folder(false) })
    expect(lane.scrollTop).toBe(500)
    hook.rerender({ folders: folder(true) })
    expect(lane.scrollTop).toBe(200)
    lane.scrollTop = 500
    hook.rerender({ folders: folder(true).map(f => ({ ...f })) })
    expect(lane.scrollTop).toBe(500)
    lane.remove()
  })

  it('drops an arm whose collapse never rendered within the window', () => {
    const now = vi.spyOn(performance, 'now').mockReturnValue(1000)
    const { lane, block, hook } = setup()
    act(() => hook.result.current.armHold('f', block))
    now.mockReturnValue(1000 + HOLD_ARM_TTL_MS + 1)
    hook.rerender({ folders: folder(true) })
    expect(lane.scrollTop).toBe(500)
    lane.remove()
  })

  it('uses where the header was painted at the arm, so a body that is already closed still holds', () => {
    // Reduced motion: by the collapsed commit the header has fallen to the
    // block's top, so only the arm-time reading still has the 300px offset.
    const { lane, block, hook } = setup()
    act(() => hook.result.current.armHold('f', block))
    const header = block.querySelector<HTMLElement>('[data-folder-row]')!
    header.getBoundingClientRect = block.getBoundingClientRect
    hook.rerender({ folders: folder(true) })
    expect(lane.scrollTop).toBe(200)
    lane.remove()
  })

  it('an expand before the collapse renders disarms it, so a later collapse does not scroll', () => {
    const { lane, block, hook } = setup()
    act(() => hook.result.current.armHold('f', block))
    hook.rerender({ folders: folder(false) })
    act(() => hook.result.current.disarm())
    hook.rerender({ folders: folder(true) })
    expect(lane.scrollTop).toBe(500)
    lane.remove()
  })

  it('disarm cancels a pending hold', () => {
    const { lane, block, hook } = setup()
    act(() => hook.result.current.armHold('f', block))
    act(() => hook.result.current.disarm())
    hook.rerender({ folders: folder(true) })
    expect(lane.scrollTop).toBe(500)
    lane.remove()
  })

  it('names the folder for an instant close only when its header was pinned at the arm', () => {
    const { lane, block, hook } = setup()
    act(() => hook.result.current.armHold('f', block))
    expect(hook.result.current.instantCloseId).toBe('f')
    hook.rerender({ folders: folder(true) })
    expect(hook.result.current.instantCloseId).toBe('f')
    // Seen open again, by any control: the next collapse decides afresh.
    hook.rerender({ folders: folder(false) })
    expect(hook.result.current.instantCloseId).toBeNull()
    // A header at its natural place animates.
    block.getBoundingClientRect = block.querySelector<HTMLElement>('[data-folder-row]')!.getBoundingClientRect
    act(() => hook.result.current.armHold('f', block))
    expect(hook.result.current.instantCloseId).toBeNull()
    lane.remove()
  })

  it('disarm and a dropped arm both clear the instant close', () => {
    const now = vi.spyOn(performance, 'now').mockReturnValue(1000)
    const { lane, block, hook } = setup()
    act(() => hook.result.current.armHold('f', block))
    act(() => hook.result.current.disarm())
    expect(hook.result.current.instantCloseId).toBeNull()
    act(() => hook.result.current.armHold('f', block))
    now.mockReturnValue(1000 + HOLD_ARM_TTL_MS + 1)
    hook.rerender({ folders: folder(true) })
    expect(hook.result.current.instantCloseId).toBeNull()
    lane.remove()
  })

  it('releases the instant close two frames after the collapse commit, so later reorders animate', () => {
    const frames: FrameRequestCallback[] = []
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation(cb => { frames.push(cb); return frames.length })
    vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => {})
    const { lane, block, hook } = setup()
    act(() => hook.result.current.armHold('f', block))
    hook.rerender({ folders: folder(true) })
    expect(hook.result.current.instantCloseId).toBe('f')
    // The first frame is the one the collapse paints in: still instant.
    act(() => frames.shift()!(0))
    expect(hook.result.current.instantCloseId).toBe('f')
    act(() => frames.shift()!(16))
    expect(hook.result.current.instantCloseId).toBeNull()
    lane.remove()
  })
})
