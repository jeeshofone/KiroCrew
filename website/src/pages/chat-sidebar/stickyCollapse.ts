/**
 * Keeps a pinned folder header where it is when its folder collapses.
 *
 * A list-view folder header is `position: sticky` inside its folder block, so
 * while the block's top has scrolled above the lane the header is painted at
 * its pin offset, not at the block's top. Collapsing shrinks the block to the
 * header alone, which takes away the room the header was sticking in: with an
 * unchanged `scrollTop` the header lands at the block's top, above the lane,
 * and every row below jumps up by the distance scrolled into the folder.
 *
 * The fix is to move the BLOCK, not the header: before the body starts to
 * close, scroll the lane so the block's top sits exactly where the header is
 * painted. The header's natural position then equals its pinned one, so it
 * does not move at all, and the body closes underneath it with the next folder
 * or session rising to meet it. Measuring the painted offset (rather than
 * deriving it from the sticky `top` of each depth) keeps this correct for a
 * nested header pinned below its ancestors and for a header the block's end is
 * already pushing off the lane.
 *
 * WHEN it runs matters as much as how far. The scroll lands in the same commit
 * that marks the folder collapsed, before that frame paints: `FolderBody` hides
 * its rows in that commit. Scrolling earlier, in the click handler, would paint
 * at least one frame of the folder's FIRST rows under the header (the
 * optimistic collapse reaches React a few ticks after the click), so the rows
 * the person was looking at would swap for others before folding away.
 *
 * WHERE the header was painted is read at the click, not in that commit. With
 * motion on, the body's grid track is still at its open height in the
 * collapsed commit, so the header is still pinned there; under
 * `prefers-reduced-motion` the transition is zeroed, the body is already
 * closed, and the header has fallen back to the block's top. The block's own
 * top does not move as its body closes, so the click's header position less
 * the block's top in the collapsed commit is the same distance either way.
 */
import { useCallback, useLayoutEffect, useRef, type RefObject } from 'react'
import type { ChatFolder } from '../../types'

/** How long a click's arm waits for its collapse to render. The optimistic
 *  update lands within a few ticks; this only bounds a dropped one. */
export const HOLD_ARM_TTL_MS = 2000

/** Sub-pixel slack: a header at its natural position measures within this. */
const PINNED_EPSILON_PX = 0.5

/** The viewport top of a folder block's own header, or null without one. */
export function paintedHeaderTop(block: HTMLElement | null): number | null {
  const header = block?.querySelector<HTMLElement>(':scope > [data-folder-row]')
  return header ? header.getBoundingClientRect().top : null
}

/**
 * Scroll `lane` so a collapsing folder block's header stays at `headerTop`,
 * where it was painted before the collapse (see the module note). A header
 * that was at its natural place (the block's top was on screen) leaves the
 * lane untouched.
 *
 * @param lane      the scroll container the sticky header pins to
 * @param block     the folder block (`[data-folder-drop]`) whose first row is the header
 * @param headerTop the header's viewport top before the collapse; defaults to where it is now
 * @returns the distance scrolled, in px (0 when nothing moved)
 */
export function holdPinnedHeaderThroughCollapse(
  lane: HTMLElement | null,
  block: HTMLElement | null,
  headerTop: number | null = paintedHeaderTop(block),
): number {
  if (!lane || !block || headerTop === null) return 0
  const pinnedBy = headerTop - block.getBoundingClientRect().top
  if (pinnedBy <= PINNED_EPSILON_PX) return 0
  const before = lane.scrollTop
  lane.scrollTop = before - pinnedBy
  return before - lane.scrollTop
}

/**
 * The list view's collapse wiring. `armHold` is called from the click with the
 * folder's block; the hold itself runs in a layout effect on the first commit
 * that shows that folder collapsed (see the module note on timing). Expanding,
 * the folder leaving the tree, or the arm outliving `HOLD_ARM_TTL_MS` disarms
 * it.
 */
export function useHoldPinnedHeaderOnCollapse(laneRef: RefObject<HTMLElement | null>, folders: ChatFolder[]) {
  const pending = useRef<{ id: string; block: HTMLElement; headerTop: number | null; at: number } | null>(null)
  const armHold = useCallback((id: string, block: HTMLElement | null) => {
    pending.current = block ? { id, block, headerTop: paintedHeaderTop(block), at: performance.now() } : null
  }, [])
  const disarm = useCallback(() => { pending.current = null }, [])
  useLayoutEffect(() => {
    const p = pending.current
    if (!p) return
    // An arm whose collapse never rendered (the update was dropped) must not
    // fire on a later, unrelated collapse of the same folder.
    if (performance.now() - p.at > HOLD_ARM_TTL_MS) { pending.current = null; return }
    const folder = folders.find(f => f.id === p.id)
    if (!folder) { pending.current = null; return }
    if (!folder.collapsed) return
    pending.current = null
    holdPinnedHeaderThroughCollapse(laneRef.current, p.block.isConnected ? p.block : null, p.headerTop)
  }, [folders, laneRef])
  return { armHold, disarm }
}
