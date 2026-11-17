import { loadDrafts, mergeIntoDraft, saveDrafts, setDraft } from './chatDrafts'
import { loadFileDrafts, saveFileDrafts, setFileDraft } from './chatFileDrafts'
import { hasPaneListener, mergePaneDraft } from './chatPaneDrafts'

/** A mounted main composer: takes `text` (and staged `files`) for `slot` and
 *  says whether it did. With `showingOnly`, it takes it only when that slot is
 *  the one on screen. */
type MainComposer = (slot: string, text: string, showingOnly: boolean, files: readonly string[]) => boolean

const mainComposers = new Set<MainComposer>()

/** Register a main chat composer as a destination for restored text. */
export function registerMainComposer(target: MainComposer): () => void {
  mainComposers.add(target)
  return () => { mainComposers.delete(target) }
}

/** Put `text` (and `files`, staged with it) into `slot`'s composer and report
 *  whether that composer is visible. With no composer mounted for the slot it
 *  merges into the slot's persisted draft, freshly loaded. */
export function restoreToComposer(slot: string, text: string, files: readonly string[] = []): boolean {
  if (!text.trim() && !files.length) return false
  for (const target of mainComposers) if (target(slot, text, true, files)) return true
  if (hasPaneListener(slot)) { mergePaneDraft(slot, text, [...files]); return true }
  for (const target of mainComposers) if (target(slot, text, false, files)) return false
  const drafts = loadDrafts()
  setDraft(drafts, slot, mergeIntoDraft(drafts[slot], text))
  saveDrafts(drafts)
  if (files.length) {
    const fileDrafts = loadFileDrafts()
    const cur = fileDrafts[slot] ?? []
    setFileDraft(fileDrafts, slot, [...cur, ...files.filter(f => !cur.includes(f))])
    saveFileDrafts(fileDrafts)
  }
  return false
}
