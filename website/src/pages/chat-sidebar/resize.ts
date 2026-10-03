/** The sidebar's persisted width and its resize handle (pointer drag and arrow keys). */
import { useState, useRef, useEffect, useLayoutEffect, useCallback } from 'react'
import { SIDEBAR_MIN, SIDEBAR_MAX, sidebarMaxWidth, sidebarRoomWidth, parseStoredSidebarWidth, sidebarPaintWidth } from '../chat/sidebarWidth'
import { CHAT_PANE_MIN_W } from '../chat/SidePanel'
import { useRailWidth } from '../../hooks/useRailWidth'
import { usePointerDrag } from '../../hooks/usePointerDrag'
import { useReducedMotion } from '../../hooks/useReducedMotion'
import { safeSetItem } from '../../utils/safeStorage'
import { SIDEBAR_LS_KEY, SIDEBAR_PRE_BOARD_LS_KEY } from './persistence'

/** How long the root's width eases after a switch between list and board view. */
export const VIEW_SWITCH_WIDTH_MS = 150

/** The drag ceiling: the view's ceiling (SIDEBAR_MAX in the list views, which
 *  gain nothing from a wider sidebar; in board view this window's room beside
 *  a nav rail of `railW`, see sidebarMaxWidth) held to the width the root can
 *  paint at (sidebarRoomWidth), so a drag never ends past what it shows.
 *  `winW` is the same window width the paint uses. */
const sidebarCeiling = (boardActive: boolean, winW: number, railW: number, chatMin: number) => Math.min(
  boardActive ? sidebarMaxWidth({ winW, railW, chatMin: CHAT_PANE_MIN_W }) : SIDEBAR_MAX,
  sidebarRoomWidth({ winW, railW, chatMin }),
)

/** The persisted sidebar width and its drag and keyboard resize. */
export function useSidebarResize({ onWidthChange, onDragChange, winW, fillsHost = false, boardActive, viewSettled }: {
  /** Told the width the host should seat the sidebar at: the stored width,
   *  held to SIDEBAR_MAX outside board view. */
  onWidthChange: ((w: number) => void) | undefined
  onDragChange: ((dragging: boolean) => void) | undefined
  /** The live window width, tracked by the parent (ChatPage). */
  winW: number
  /** The host stretches the sidebar to its own width (the mobile drawer, the
   *  sessions embed), so no chat pane sits beside it to reserve room for. */
  fillsHost?: boolean
  /** Board view is showing. Only the board may grow past SIDEBAR_MAX. */
  boardActive: boolean
  /** The board columns have loaded, so `boardActive` names the view the user
   *  is on. A flip before that is the columns arriving, not a view switch. */
  viewSettled: boolean
}) {
  // The drag ceiling follows the live window and nav rail, read at call time
  // (a ref, not a closure) so a resize mid-session widens or narrows it.
  const railWidth = useRailWidth()
  const railWidthRef = useRef(railWidth)
  railWidthRef.current = railWidth
  const boardActiveRef = useRef(boardActive)
  boardActiveRef.current = boardActive
  const winWRef = useRef(winW)
  winWRef.current = winW
  const chatMin = fillsHost ? 0 : CHAT_PANE_MIN_W
  const chatMinRef = useRef(chatMin)
  chatMinRef.current = chatMin
  const ceilingNow = useCallback(
    () => sidebarCeiling(boardActiveRef.current, winWRef.current, railWidthRef.current, chatMinRef.current), [])
  // Sidebar width (self-managed). The saved width is kept as saved (see
  // parseStoredSidebarWidth); the paint below holds it to this view and this
  // window, so going back to board view or widening the window restores it.
  const [sidebarWidth, setSidebarWidth] = useState(() =>
    parseStoredSidebarWidth(localStorage.getItem(SIDEBAR_LS_KEY)) ?? 260)
  // The list views paint a wider saved width at SIDEBAR_MAX, leaving the
  // saved width itself for board view.
  const seatWidth = boardActive ? sidebarWidth : Math.min(sidebarWidth, SIDEBAR_MAX)
  // The width the root is painted at (see sidebarPaintWidth): a window narrowed
  // mid-session after a wide drag must not leave the root, and the resize handle
  // on its right edge, far outside the drawer box that clips it. The stored
  // preference itself is kept for a wider window, as ChatPage keeps it. The
  // window width comes from ChatPage, which already tracks it for the drawer.
  const paintedSidebarWidth = sidebarPaintWidth({
    stored: seatWidth, winW, railW: railWidth, chatMin,
  })
  // A switch between list and board view can move the painted width a long way
  // (a board width past SIDEBAR_MAX paints at SIDEBAR_MAX in list view), so the
  // root eases to the new width for a moment and the change reads as a resize.
  // Only a view switch arms it: a drag or nudge paints at once, and so does
  // everything under prefers-reduced-motion.
  const reduceMotion = useReducedMotion()
  // The view last painted, and whether it was a real view: the board's columns
  // arrive after the first paint, so the first list-to-board flip on load is the
  // columns landing, not a switch the user made, and paints at once.
  const [lastView, setLastView] = useState({ board: boardActive, settled: viewSettled })
  const [viewSwitching, setViewSwitching] = useState(false)
  if (lastView.board !== boardActive || lastView.settled !== viewSettled) {
    setLastView({ board: boardActive, settled: viewSettled })
    if (lastView.board !== boardActive && lastView.settled) setViewSwitching(true)
  }
  useEffect(() => {
    if (!viewSwitching) return
    const t = window.setTimeout(() => setViewSwitching(false), VIEW_SWITCH_WIDTH_MS)
    return () => window.clearTimeout(t)
  }, [viewSwitching])
  // The root's inline style: the painted width, eased only on a view switch.
  const rootStyle = {
    width: paintedSidebarWidth,
    transition: viewSwitching && !reduceMotion ? `width ${VIEW_SWITCH_WIDTH_MS}ms ease-out` : undefined,
  }
  // Resize logic — Pointer Events (mouse + touch + pen) via usePointerDrag, so
  // the handle works on touch devices too, e.g. a tablet at desktop width where
  // the sidebar is a side-by-side panel (the mouse-only handler ignored touch).
  // setPointerCapture keeps move/up firing when the pointer leaves the thin
  // handle, replacing the old window-level mousemove/mouseup listeners.
  const sidebarStartW = useRef(0)
  // The stored preference when the drag began, restored if the drag ends where
  // it started (see commitResize).
  const sidebarStoredAtStart = useRef(0)
  const sidebarDraggingRef = useRef(false)
  const sidebarWidthRef = useRef(sidebarWidth)
  sidebarWidthRef.current = sidebarWidth
  // A drag or arrow key starts from the PAINTED width, what the user sees, not
  // from a wider stored preference the window is currently clipping.
  const paintedWidthRef = useRef(paintedSidebarWidth)
  paintedWidthRef.current = paintedSidebarWidth
  const onWidthChangeRef = useRef(onWidthChange)
  onWidthChangeRef.current = onWidthChange
  const onDragChangeRef = useRef(onDragChange)
  onDragChangeRef.current = onDragChange
  // The host seats the sidebar from this report, so it follows every change,
  // a switch between list and board view included, before the frame paints.
  useLayoutEffect(() => { onWidthChangeRef.current?.(seatWidth) }, [seatWidth])
  // The one place a drag or nudge saves. A resize that ends on the painted width
  // it began from changed nothing the user can see (a press with no travel, or
  // pushing against the ceiling), so the stored preference stands: it may be a
  // wider width this view or window only clips. Any other end saves.
  const commitResize = useCallback((w: number, fromPainted: number, storedBefore: number) => {
    if (w === fromPainted) {
      setSidebarWidth(storedBefore)
      return
    }
    setSidebarWidth(w)
    safeSetItem(SIDEBAR_LS_KEY, String(w))
  }, [])

  // threshold 0: a dedicated edge affordance resizes immediately on press (no
  // 10px hysteresis), matching the original mouse resizer's feel.
  const sidebarResize = usePointerDrag({
    threshold: 0,
    onStart: () => {
      sidebarStartW.current = paintedWidthRef.current
      sidebarStoredAtStart.current = sidebarWidthRef.current
      sidebarDraggingRef.current = true
      setViewSwitching(false)
      document.body.style.cursor = 'col-resize'
      document.body.style.userSelect = 'none'
      onDragChangeRef.current?.(true)
    },
    onMove: ({ dx }) => {
      // A move with no horizontal travel (the zero-delta move threshold 0 fires
      // on press, or a pen's pressure or vertical jitter) lands on the start
      // width, so commitResize keeps the stored preference on release.
      const newW = Math.min(ceilingNow(), Math.max(SIDEBAR_MIN, sidebarStartW.current + dx))
      setSidebarWidth(newW)
    },
    onEnd: () => {
      sidebarDraggingRef.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      onDragChangeRef.current?.(false)
      commitResize(sidebarWidthRef.current, sidebarStartW.current, sidebarStoredAtStart.current)
    },
  })
  // Arrow-key resize for the shared handle: the same clamp a drag applies,
  // committed at once since a key press has no "release" to commit on.
  const nudgeSidebar = useCallback((dx: number) => {
    const w = Math.min(ceilingNow(), Math.max(SIDEBAR_MIN, paintedWidthRef.current + dx))
    setViewSwitching(false)
    commitResize(w, paintedWidthRef.current, sidebarWidthRef.current)
  }, [commitResize, ceilingNow])
  /** Widen for a board's lanes, remembering what the user had so leaving board view
   *  can give it back. Persisting the automatic width without that destroys their
   *  chosen width permanently and strands a ~900px sidebar in list view. */
  const widenForBoard = useCallback((next: number) => {
    safeSetItem(SIDEBAR_PRE_BOARD_LS_KEY, String(sidebarWidthRef.current))
    setSidebarWidth(next)
    safeSetItem(SIDEBAR_LS_KEY, String(next))
  }, [])
  /** Leaving board view: give back the width the user chose before the lanes were
   *  auto-widened, rather than stranding a ~900px sidebar in list view. */
  const restorePreBoardWidth = useCallback(() => {
    const prior = parseStoredSidebarWidth(localStorage.getItem(SIDEBAR_PRE_BOARD_LS_KEY))
    if (prior !== null) {
      setSidebarWidth(prior)
      safeSetItem(SIDEBAR_LS_KEY, String(prior))
      safeSetItem(SIDEBAR_PRE_BOARD_LS_KEY, '')
    }
  }, [])

  // Unmount guard: if the sidebar unmounts mid-drag (collapse / route change),
  // onEnd never fires — setPointerCapture dies with the element — so the global
  // body styles and the parent's dragging state would stay stuck. Restore them
  // on teardown. The old mouse-only handler did this in its listener cleanup;
  // the pointer migration must preserve it.
  useEffect(() => () => {
    if (sidebarDraggingRef.current) {
      sidebarDraggingRef.current = false
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      onDragChangeRef.current?.(false)
    }
  }, [])
  return { paintedSidebarWidth, rootStyle, sidebarMax: sidebarCeiling(boardActive, winW, railWidth, chatMin), paintedWidthRef, sidebarResize, nudgeSidebar, widenForBoard, restorePreBoardWidth }
}
