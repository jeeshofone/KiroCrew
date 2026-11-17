/**
 * Queued-message cancels that got no real answer: the DELETE had no HTTP
 * status, or a retry answered 404 (the entry left the queue, cancelled or
 * sent). No client read can tell which, so each entry is kept here with the
 * data its restore needs until the person restores or dismisses it, or a retry
 * gets a real answer. Module-level so it outlives the host that clicked and
 * every host showing the slot draws the same entries.
 */
import { useSyncExternalStore } from 'react'
import type { ChatMessage } from '../types'

export interface UnconfirmedCancel {
  slot: string
  queueId: string
  /** The entry as it was at the click. */
  msg: ChatMessage
  /** The clicked slot's project and dictation scope at the click. */
  project?: string
  dictationScope?: string
}

const entries = new Map<string, UnconfirmedCancel>()
const listeners = new Set<() => void>()
const snapshots = new Map<string, readonly UnconfirmedCancel[]>()
const key = (slot: string, queueId: string) => JSON.stringify([slot, queueId])
const EMPTY: readonly UnconfirmedCancel[] = []

function changed(slot: string) {
  snapshots.delete(slot)
  for (const fn of Array.from(listeners)) fn()
}

export function recordUnconfirmedCancel(entry: UnconfirmedCancel): void {
  entries.set(key(entry.slot, entry.queueId), entry)
  changed(entry.slot)
}

export function dropUnconfirmedCancel(slot: string, queueId: string): void {
  if (entries.delete(key(slot, queueId))) changed(slot)
}

export function getUnconfirmedCancel(slot: string, queueId: string): UnconfirmedCancel | undefined {
  return entries.get(key(slot, queueId))
}

/** The slot's entries, as a stable snapshot (same array until one changes). */
export function unconfirmedCancelsFor(slot: string | null | undefined): readonly UnconfirmedCancel[] {
  if (!slot) return EMPTY
  let snap = snapshots.get(slot)
  if (!snap) {
    snap = [...entries.values()].filter(e => e.slot === slot)
    snapshots.set(slot, snap)
  }
  return snap
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => { listeners.delete(fn) }
}

/** Subscribe a host to the slot's entries. */
export function useUnconfirmedCancels(slot: string | null | undefined): readonly UnconfirmedCancel[] {
  return useSyncExternalStore(subscribe, () => unconfirmedCancelsFor(slot), () => EMPTY)
}

export function __resetUnconfirmedCancelsForTests(): void {
  // Silent: a host still mounted at teardown must not re-render.
  entries.clear()
  snapshots.clear()
}
