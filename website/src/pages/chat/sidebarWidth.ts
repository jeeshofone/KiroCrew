/**
 * Sidebar width bounds and the viewport clamp.
 *
 * Deliberately NOT in ChatSidebar: ~20 ChatPage suites replace that module with
 * `{ default, SIDEBAR_MIN, SIDEBAR_MAX }`, so anything else imported from it is
 * `undefined` at run time -- silent for a constant, a crash for a function.
 */

export const SIDEBAR_MIN = 180
/** The view ceiling in the list views, and in board view on a normal-width
 *  window. In board view a window wide enough to seat a wider sidebar beside
 *  the nav rail and a minimum chat pane raises it; see `sidebarMaxWidth`. On
 *  any window the paint is also held to `sidebarRoomWidth`. */
export const SIDEBAR_MAX = 1400

/**
 * The widest the user may drag the board-view sidebar in THIS window: the
 * window minus the nav rail minus the chat pane's minimum, never below
 * `SIDEBAR_MAX`. On a normal-width window that difference is under 1400, so
 * this view ceiling is `SIDEBAR_MAX`; only a wide window lifts it. The drag
 * and the paint are held to `sidebarRoomWidth` on top of it.
 */
export function sidebarMaxWidth(
  { winW, railW, chatMin }: { winW: number; railW: number; chatMin: number },
): number {
  return Math.max(SIDEBAR_MAX, winW - railW - chatMin)
}

/**
 * A stored width read back from localStorage: `null` when it is not a usable
 * number or is under `SIDEBAR_MIN`, otherwise the value as saved. It is NOT
 * narrowed to this window: a width saved on a wider window stays in state, and
 * `sidebarPaintWidth` holds it to the current ceiling at render, so widening
 * the window again restores it.
 */
export function parseStoredSidebarWidth(raw: string | null): number | null {
  const n = raw ? parseInt(raw, 10) : NaN
  if (isNaN(n) || n < SIDEBAR_MIN) return null
  return n
}

/**
 * The widest the sidebar root may paint in THIS window: the room beside the
 * nav rail and a `chatMin` chat pane, floored at `SIDEBAR_MIN`. One rule on
 * both sides of `SIDEBAR_MAX`, so a saved 1400 and a saved 1401 paint the
 * same on a window too narrow for either.
 */
export function sidebarRoomWidth(
  { winW, railW, chatMin }: { winW: number; railW: number; chatMin: number },
): number {
  return Math.max(SIDEBAR_MIN, winW - railW - chatMin)
}

/**
 * The width the sidebar root paints at for a stored preference: the stored
 * width held to `sidebarRoomWidth`, so the chat pane keeps its minimum and the
 * resize handle on the root's right edge stays on screen, whatever the stored
 * width and whichever side of `SIDEBAR_MAX` it is on. The preference itself is
 * not narrowed, so widening the window again restores it.
 */
export function sidebarPaintWidth(
  { stored, winW, railW, chatMin }: { stored: number; winW: number; railW: number; chatMin: number },
): number {
  return Math.min(stored, sidebarRoomWidth({ winW, railW, chatMin }))
}

/**
 * The width narrowed to the space the window actually leaves beside the nav
 * rail. Reserves NOTHING for the chat pane: `ChatPage` passes it the width
 * `sidebarPaintWidth` already held beside a minimum chat pane, the same width
 * the sidebar root paints at, so drawer and root agree. Subtracting a chat
 * minimum again here would cap the drawer below the root it holds.
 */
export function clampSidebarWidth(
  { stored, winW, railW }: { stored: number; winW: number; railW: number },
): number {
  return Math.min(stored, Math.max(SIDEBAR_MIN, winW - railW))
}
