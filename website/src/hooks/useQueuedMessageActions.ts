import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../api/client'
import { useAppDispatch, useAppSelector } from '../store'
import { cancelQueuedMessage, editQueuedMessage, queueEntryAttachments, refreshSlot, warmSlotCache } from '../store/chatSlice'
import { ApiError } from '../api/apiError'
import { restoreQueuedContent } from '../utils/fileTokens'
import { dropUnconfirmedCancel, getUnconfirmedCancel, recordUnconfirmedCancel, useUnconfirmedCancels } from '../utils/unconfirmedCancels'
import { quoteBlock, readMessageQuote, stripQuoteBlock, type MessageQuote } from '../chat-core/composer/messageQuote'
import { markDictated } from '../chat-core/composer/dictationProvenance'
import type { ChatMessage } from '../types'
import type { PasteBlock } from '../utils/pasteTokens'

/** Pre-serialization composer state of a send the server QUEUED, written by the
 *  host's send path when the `queued: true` receipt names the entry. */
export interface QueuedSendRecord {
  /** The text exactly as the user typed it. */
  raw: string
  /** The staged file paths at send time. */
  files: string[]
  /** The exact POSTed LLM-facing text — the edit guard: an entry edited after
   *  send keeps its queue id but fails this equality, so an edited card falls
   *  to the parser instead of clobbering the edit with pre-edit state. */
  sent: string
  /** The slot's recorded `@rel` alias map for the sent files, captured before
   *  the send-clear dropped it (fork GPT review): restoring text + files
   *  WITHOUT the aliases re-created the pre-fix stuck chip — the restored
   *  mention was invisible to reconciliation, so hand-deleting it left a
   *  stale chip that the next send silently re-attached. */
  aliases?: Record<string, string[]>
  /** The whole-message quote the send consumed (`chat-core/composer/messageQuote`),
   *  so a cancel puts it back as a staged card rather than leaving its `>` block
   *  in the restored text -- or, without a stash hit, losing it (fork GPT review). */
  quote?: MessageQuote
  /** The composer text held dictated words (`dictationProvenance`), so a cancel
   *  that restores `raw` puts the record back with it. */
  dictated?: boolean
}

/** Queued-send stash, keyed by the `queue_id` the send receipt returns (the
 *  same id `queue_push` broadcasts and the card's cancel button carries).
 *  Queue identity is the ONLY sound key: the serialization is not injective
 *  (image @-tokens are erased from the LLM-facing text), so content-keyed
 *  records can collide across different captions, duplicate sends, and other
 *  tabs. Module-level so every host's send path (ChatPage, ChatPane) writes
 *  the one store this hook's cancel consumes — the same one-owner reasoning
 *  as the hook itself (#5891). Deliberately unevicted: an entry dies on the
 *  cancel that consumes it, and evicting a live entry would degrade that
 *  card's cancel to the parser fallback; orphans from normal delivery are
 *  three small strings bounded by queued sends per tab session. */
export const queuedSendStash = new Map<string, QueuedSendRecord>()

/** The queue-card callbacks `QueueStack` takes, plus the in-flight set it
 *  disables its controls from. */
/** A cancel or edit the server refused, with its answer, for the host to show. */
export interface QueueActionRefusal {
  /** The slot whose queue refused: the host shows it only on that chat. */
  slot: string
  queueId: string
  /** `status` 0 when the request got no HTTP answer. */
  error: ApiError
}

export interface QueuedMessageActions {
  onCancel: (queueId: string) => void
  onInterrupt: (queueId: string) => void
  /** `pastes`: the paste blocks the edit kept, when the card carried any
   *  (`QueueStack`'s `onEdit`); the optimistic row takes exactly that set. */
  onEdit: (queueId: string, content: string, pastes?: PasteBlock[]) => void
  /** The card's editor opened (`true`) or closed without a save (`false`). The
   *  server holds the entry while it is open, so a turn that ends meanwhile
   *  does not send the words being changed; a save releases it on its own. */
  onEditOpen: (queueId: string, open: boolean) => void
  onReorder: (queueId: string, direction: 'next' | 'later') => void
  /** The last cancel or edit the server refused (not a 404, which is the entry
   *  having already left the queue), shown by the host through `ErrorNotice`
   *  until `dismissRefusal`. */
  refusal: QueueActionRefusal | null
  dismissRefusal: () => void
  /** Feed straight to `QueueStack`'s `pendingIds`. */
  pendingIds: ReadonlySet<string>
  /** Cancels whose DELETE got no HTTP answer: they may or may not have landed,
   *  so the client decides nothing and keeps each entry's own payload until the
   *  person restores or dismisses it (`UnconfirmedCancels`). */
  unconfirmed: UnconfirmedCancels
}

/** The slot's unconfirmed cancels (`utils/unconfirmedCancels`), for the host to draw. */
export interface UnconfirmedCancels {
  /** The entries, as they were at the click. */
  messages: readonly ChatMessage[]
  /** Those the server still lists: their queue card keeps Cancel, and the
   *  host only notes the cancel got no reply. The rest are unconfirmed cards
   *  with Restore and Dismiss. */
  listedIds: ReadonlySet<string>
  /** Put an unlisted entry's text, attachments and quote back, once, and drop it. */
  onRestore: (queueId: string) => void
  /** Drop it without touching the composer. */
  onDismiss: (queueId: string) => void
}

export interface QueuedMessageActionsOptions {
  /** Slot the cards belong to. Null/empty disables every action, which is what
   *  a host with no active slot needs. */
  slot: string | null | undefined
  /** EVERY `role: 'queued'` row in the slot, including the ones no card is drawn
   *  for (hidden system deliveries, recovery continuations). Reorder submits the
   *  full sequence, so omitting them would let the backend re-append them at the
   *  tail and silently demote automation. */
  allQueued: ChatMessage[]
  /** Just the interactive cards `QueueStack` renders, in render order. A reorder
   *  swaps two ADJACENT VISIBLE cards, so the neighbour is chosen here and then
   *  expressed inside `allQueued`'s full sequence. */
  visibleQueued: ChatMessage[]
  /** Hand a cancelled card's recovered composer state back to the host. Each
   *  host owns its own composer — ChatPage's `input` is draft-persisted per
   *  slot, a pane's is local state that merges a recovered draft — so the
   *  plumbing is injected rather than decided here. `text` is the TYPED text
   *  (from the send-time stash when this tab made the send, else inverted
   *  from the wire serialization by `restoreQueuedContent`), never the raw
   *  card content — the card holds `prepareSendPayload`'s LLM-facing form
   *  (`[attached_file N]` markers, image markdown), and restoring it verbatim
   *  is the marker-in-composer data loss of #560. `files` are the attachment
   *  paths to re-stage; a host MUST merge them into its pending-files state
   *  or the recovered text refers to attachments the re-send will not carry.
   *  Omitted, the recovered state is dropped, which is what cancelling in a
   *  split pane did before #5891 and what no host should do.
   *  `forSlot` is the slot the cancel was clicked in: the server confirms
   *  later, so a host writes that slot's draft and touches its live composer
   *  only while it still shows that slot.
   */
  restoreDraft?: (text: string, files: string[], aliases: Record<string, string[]> | undefined, quote: MessageQuote | undefined, forSlot: string) => void
  /** The paths the clicked slot has staged when the server confirms: its live
   *  composer, or its parked draft when the host shows another slot. The
   *  restore MERGES into that draft, so a mention the parser writes back
   *  must not use a suffix one of those files also has: two cards naming
   *  `x/a.txt` and `y/a.txt`, cancelled in turn, would otherwise both come
   *  back as `@a.txt` and the next send would bind both to one file.
   *  Omitted, the parser only checks the card's own files. */
  stagedFiles?: (forSlot: string) => readonly string[]
  /** The clicked slot's project root, read when the server confirms. A mention the
   *  parser writes back for a file under it is spelled relative to it, the
   *  way the picker inserts it, because that is the only spelling the
   *  composer revives a chip for when the mention is deleted and undone.
   *  Omitted, the parser uses the shortest unambiguous suffix. */
  project?: (forSlot: string) => string | undefined
  /** The host composer's dictation scope (`dictationProvenance`), so a
   *  cancelled dictated card marks the composer it goes back into. */
  dictationScope?: string
}

const queueIdOf = (m: ChatMessage): string | undefined => m.meta?.queueId as string | undefined

/**
 * The queue-card action recipe, owned once and consumed by every host that draws
 * a `QueueStack` over the MAIN slot queue (`ChatPage` and `ChatPane` today).
 *
 * Issue #5891: the four callbacks were copy-mirrored across those hosts, and the
 * copies had drifted — cancel restored the composer in `ChatPage` only, so
 * cancelling from a split pane silently destroyed the draft, and neither host
 * threaded the `pendingIds` latch `QueueStack` has always accepted. Both are
 * behaviours per surface, which is exactly why they must not live in the hosts:
 * fixing one fork does not fix the other, and #2240 was that failure mode.
 *
 * NOT a home for `SideChat`'s queue. That surface talks to a different endpoint
 * family (`/side/queue/…`), stores its cards as `SideQueueEntry[]` under
 * `slotSide[slot]` rather than as `role: 'queued'` transcript rows, and is
 * deliberately SERVER-AUTHORITATIVE where this one is optimistic — the inverse
 * contract, for a documented reason. See that file, and the PR for #5891.
 *
 * Cancel is pessimistic: the card and the composer change only after the server
 * confirms, because a refused cancel would otherwise leave the message's words
 * ready to send while it stays queued. Edit stays optimistic. Either one, when
 * refused, is rebuilt from the server's listing and shown through `refusal`.
 */
export function useQueuedMessageActions({
  slot,
  allQueued,
  visibleQueued,
  restoreDraft,
  stagedFiles,
  project,
  dictationScope,
}: QueuedMessageActionsOptions): QueuedMessageActions {
  const dispatch = useAppDispatch()
  const [pendingIds, setPendingIds] = useState<ReadonlySet<string>>(() => new Set())
  const [refusal, setRefusal] = useState<QueueActionRefusal | null>(null)
  const activeSlotRef = useRef<string | null>(null)
  activeSlotRef.current = useAppSelector(s => s.chat.activeSlot)
  const dismissRefusal = useCallback(() => setRefusal(null), [])
  /** Show a failed Send now or reorder: nothing optimistic to undo, so only the
   *  notice. Every failure shows, a 404 too: these endpoints answer a stale
   *  entry or order with 400/409, and a 404 means the chat itself is gone. */
  const showFailure = useCallback((queueId: string, err: unknown) => {
    if (!slot) return
    setRefusal({ slot, queueId, error: err instanceof ApiError ? err : new ApiError(0, err instanceof Error ? err.message : String(err)) })
  }, [slot])

  // Reads happen inside callbacks that must NOT be re-created when the queue
  // changes: `QueueStack` is memo-compared on callback identity, so a new
  // `onCancel` on every queue mutation would repaint the whole stack mid-animation.
  const allQueuedRef = useRef(allQueued)
  allQueuedRef.current = allQueued
  const visibleQueuedRef = useRef(visibleQueued)
  visibleQueuedRef.current = visibleQueued
  const restoreDraftRef = useRef(restoreDraft)
  restoreDraftRef.current = restoreDraft
  const stagedFilesRef = useRef(stagedFiles)
  stagedFilesRef.current = stagedFiles
  const projectRef = useRef(project)
  projectRef.current = project
  const dictationScopeRef = useRef(dictationScope)
  dictationScopeRef.current = dictationScope

  const markPending = useCallback((queueId: string, pending: boolean) => {
    setPendingIds(prev => {
      if (pending === prev.has(queueId)) return prev
      const next = new Set(prev)
      if (pending) next.add(queueId)
      else next.delete(queueId)
      return next
    })
  }, [])

  // Ids whose interrupt the server ACCEPTED and whose card has not yet been
  // retired. The HTTP 200 is not the end of an interrupt: the entry is dequeued
  // and started, and the card only goes away when the `queue_pop` frame lands.
  // Releasing on the response would re-enable the button inside that gap, and
  // the next click would interrupt the very turn the first click just promoted.
  // So a successful interrupt stays latched until its row is gone.
  const [heldUntilRetired, setHeldUntilRetired] = useState<ReadonlySet<string>>(() => new Set())
  const queuedIdSet = useMemo(
    () => new Set(allQueued.map(queueIdOf).filter((id): id is string => !!id)),
    [allQueued],
  )
  useEffect(() => {
    // Residual, deliberately: if that frame never arrives the control stays
    // disabled. That is the safe direction - it withholds a click that would
    // interrupt a turn the user did not aim at - and it clears on the next
    // hydration of the slot, which rebuilds the queued rows from the server.
    const gone = [...heldUntilRetired].filter(id => !queuedIdSet.has(id))
    if (!gone.length) return
    setHeldUntilRetired(prev => {
      const next = new Set(prev)
      for (const id of gone) next.delete(id)
      return next
    })
    setPendingIds(prev => {
      if (!gone.some(id => prev.has(id))) return prev
      const next = new Set(prev)
      for (const id of gone) next.delete(id)
      return next
    })
  }, [queuedIdSet, heldUntilRetired])

  // No mounted-ref guard around the releases below, deliberately. React 18.3
  // dropped the setState-after-unmount warning, so a request that outlives its
  // host (closing a split pane mid-flight) costs one dropped state write and
  // nothing else — while the obvious guard is actively wrong here: this app
  // renders under StrictMode, whose mount/unmount/remount of effects would
  // latch a `mounted` ref to false for the rest of the session and leave every
  // card's controls disabled after their first click. The StrictMode test in
  // useQueuedMessageActions.test.tsx pins that release survives the remount.
  /** Latch the card for the life of the request so a second click cannot fire a
   *  duplicate, releasing on either outcome. Used by the two actions whose
   *  response settles the card: cancel retires it once confirmed and edit
   *  rewrites it, so there is nothing left to wait for.
   *
   *  A refused cancel or edit rebuilds the cards from the server and shows
   *  why (`reconcileOnRefusal`). It must still release, or one lost request
   *  freezes a card until the queue drains. */
  const run = useCallback((queueId: string, call: Promise<unknown>) => {
    markPending(queueId, true)
    const release = () => markPending(queueId, false)
    call.then(release, release)
  }, [markPending])

  /** After a cancel or edit the server refused, rebuild the slot's queued
   *  cards from the server's own listing, so their order, text and `written`
   *  mark are the server's, not a guess. Any failure but a 404 (the entry
   *  having left the queue) is kept as `refusal` for the host to show on that
   *  chat. The composer is never touched here: a cancel only fills it once the
   *  server has confirmed (`onCancel`). */
  const reconcileOnRefusal = useCallback((queueId: string, call: Promise<unknown>, lostShownByCard = false) => {
    if (!slot) return
    // The open chat refetches through `refreshSlot`; any other slot (a pane's)
    // through `warmSlotCache`, which rebuilds that slot's cached rows.
    const refetch = () => {
      if (activeSlotRef.current === slot) void dispatch(refreshSlot({ key: slot }))
      else void dispatch(warmSlotCache(slot))
    }
    call.catch(err => {
      refetch()
      // No HTTP answer, or 404, on a cancel: its kept entry says so instead.
      const status = err instanceof ApiError ? err.status : 0
      if (lostShownByCard && (status === 0 || status === 404)) return
      if (err instanceof ApiError && err.status === 404) return
      // `status` 0: no HTTP answer, so it may or may not have landed.
      setRefusal({ slot, queueId, error: err instanceof ApiError ? err : new ApiError(0, err instanceof Error ? err.message : String(err)) })
    })
  }, [slot, dispatch])

  /** Put a cancelled entry's text, attachments and quote into its slot's draft.
   *  `project` and `dictationScope` are the slot's (read at the click for an
   *  unconfirmed cancel, which may be restored long after). */
  const restoreEntry = useCallback((slot: string, queueId: string, msg: ChatMessage | undefined, project: string | undefined, dictationScope: string | undefined) => {
    // An app-authored entry is cancellable (that is the user's undo for an
    // unwanted app message) but never draft-restored: merging the app
    // envelope into the human composer would corrupt the draft AND launder
    // app text into user authorship on the next send.
    const isAppEntry = (msg?.meta?.kind as string) === 'mcp_app_message'
    if (!msg?.content || isAppEntry) return
    // Restore by QUEUE IDENTITY: the record was stored under the queue id
    // the send receipt returned, which is the id this cancel carries — so a
    // hit is this card's own pre-send state by construction, whatever its
    // content collides with (duplicate texts, erased-image-token captions,
    // other tabs). The record is consumed either way; `sent` guards the one
    // same-id hazard (see QueuedSendRecord). No stash hit — a reload,
    // another tab's card, an edited entry — falls to the strict parser,
    // handed the entry's own attachment list when the server echoed one
    // on the row (`meta.files`, the same list a user row carries): with
    // it the parser matches each marker by exact text, so a spaced path
    // restores whole and an inline marker comes back as an `@` mention;
    // without it the parser claims only byte-exact round-trippable shapes
    // and is never worse than the verbatim restore this replaced.
    const stashed = queuedSendStash.get(queueId)
    if (stashed) queuedSendStash.delete(queueId)
    const hit = stashed && stashed.sent === msg.content
    // A stash hit restores `raw`, which never carried the dictation note, and
    // says whether its words were dictated, so the provenance goes back with
    // it. Without a stash nothing records that, and the card's text is
    // restored as it stands: a dictation note in it stays visible rather than
    // being guessed at, since the same words could be ones the user typed.
    if (hit && stashed.dictated) markDictated(slot, dictationScope)
    const content = msg.content
    // Without a stash hit the row's own `meta.quote` (the gateway carries
    // it on the entry) is the quote, and its block comes off the head of
    // the parsed text so the host restages a card, not raw `>` lines.
    // Accepted only while its block still opens the text: a record the
    // row kept past an edit that deleted the block is not restaged.
    const rowMetaQuote = hit ? null : readMessageQuote(msg.meta as Record<string, unknown> | undefined)
    const rowQuote = rowMetaQuote && content.startsWith(quoteBlock(rowMetaQuote)) ? rowMetaQuote : undefined
    // The block comes off BEFORE the parse: the parser's producer-block
    // patterns are anchored at offset 0, so a quote on the head would hide
    // an image block behind it and the file would restore as literal text.
    const parsed = hit
      ? { text: stashed.raw, files: stashed.files, aliases: stashed.aliases }
      : restoreQueuedContent(
        rowQuote ? stripQuoteBlock(content, rowQuote) : content,
        queueEntryAttachments(msg.meta).files,
        stagedFilesRef.current?.(slot),
        project,
      )
    const { files, text, aliases } = parsed
    // The stash carries the alias map the send-clear dropped. The parser
    // carries one only for a mention it wrote back from an inline marker
    // (the entry's list proved the path), so that chip and mention stay
    // reconciled too; any other parser restore has none, which falls into
    // the documented reload-limitation class rather than corrupting.
    const quote = hit ? stashed.quote : rowQuote
    // Into the slot the cancel was clicked in, not whichever is shown now.
    restoreDraftRef.current?.(text, files, aliases, quote, slot)
  }, [])

  const onCancel = useCallback((queueId: string) => {
    if (!slot) return
    const msg = allQueuedRef.current.find(m => queueIdOf(m) === queueId)
    // Pessimistic: the card stays (its actions disabled while the request is
    // in flight) and the composer is untouched until the server confirms. A
    // refusal then needs no undo, because nothing was put anywhere.
    const clickProject = projectRef.current?.(slot)
    const clickScope = dictationScopeRef.current
    const call = api.cancelQueuedMessage(slot, queueId)
    // Confirmed: the card goes (the WS echo is a no-op if it is already gone)
    // and its words go into the clicked slot's draft, live only if shown.
    call.then(() => {
      dropUnconfirmedCancel(slot, queueId)
      dispatch(cancelQueuedMessage({ slot, queue_id: queueId }))
      restoreEntry(slot, queueId, msg, projectRef.current?.(slot), clickScope)
    }, err => {
      // No HTTP status, or 404 (the entry left the queue, cancelled or sent):
      // no answer, so the client decides nothing and keeps the payload in the
      // shared registry for the person. While the listing still holds the
      // entry its ordinary card keeps Cancel; once it is gone the host draws
      // the unconfirmed card. Any other status is a refusal and settles it.
      const status = err instanceof ApiError ? err.status : 0
      if (msg && (status === 0 || status === 404)) {
        const kept = getUnconfirmedCancel(slot, queueId)
        recordUnconfirmedCancel(kept ?? { slot, queueId, msg, project: clickProject, dictationScope: clickScope })
        return
      }
      dropUnconfirmedCancel(slot, queueId)
    })
    reconcileOnRefusal(queueId, call, !!msg)
    run(queueId, call)
  }, [slot, dispatch, run, reconcileOnRefusal, restoreEntry])

  const onInterrupt = useCallback((queueId: string) => {
    if (!slot) return
    markPending(queueId, true)
    api.interruptSlot(slot, queueId).then(
      (receipt: { queue_selected?: boolean } | undefined) => {
        // Declined: the server answered without promoting this card (another
        // stop owns the slot), so it stays queued and must stay usable.
        if (receipt?.queue_selected === false) {
          markPending(queueId, false)
          return
        }
        // Accepted: the entry is being promoted, so stay latched until the row
        // is gone rather than until this response landed.
        setHeldUntilRetired(prev => (prev.has(queueId) ? prev : new Set(prev).add(queueId)))
      },
      // Rejected: nothing was promoted and the card is still the same card, so
      // release at once, say why, and let the user try again.
      (err: unknown) => {
        markPending(queueId, false)
        showFailure(queueId, err)
      },
    )
  }, [slot, markPending, showFailure])

  // One card's hold, save and release go out one after another: sent side by
  // side, a close could reach the server before its own open and leave a hold
  // that no editor owns.
  const editChains = useRef(new Map<string, Map<string, Promise<unknown>>>())
  const inOrder = useCallback(<T,>(slotKey: string, queueId: string, send: () => Promise<T>): Promise<T> => {
    let chains = editChains.current.get(slotKey)
    if (!chains) editChains.current.set(slotKey, chains = new Map())
    const prev = chains.get(queueId)
    const next = prev ? prev.then(send, send) : send()
    const tail = next.catch(() => undefined)
    chains.set(queueId, tail)
    tail.then(() => { if (chains.get(queueId) === tail) chains.delete(queueId) })
    return next
  }, [])

  const onEdit = useCallback((queueId: string, content: string, pastes?: PasteBlock[]) => {
    if (!slot) return
    const trimmed = content.trim()
    if (!trimmed) return
    // Optimistically update the card; the WS event reconciles other clients.
    const save = inOrder(slot, queueId, () => api.editQueuedMessage(slot, queueId, trimmed))
    reconcileOnRefusal(queueId, save)
    dispatch(editQueuedMessage({ slot, queue_id: queueId, content: trimmed, ...(pastes ? { pastes } : {}) }))
    // A refused or lost save never reaches the server's release, so the hold
    // its open editor placed is released here instead of waiting out the deadline.
    save.catch(() => inOrder(slot, queueId, () => api.holdQueuedMessageEdit(slot, queueId, false)).catch(() => undefined))
    run(queueId, save)
  }, [slot, dispatch, run, inOrder, reconcileOnRefusal])

  // Fire-and-forget like the rest: a failed hold leaves the drain as it was
  // before holds existed, and a 404 means the entry already left the queue.
  const onEditOpen = useCallback((queueId: string, open: boolean) => {
    if (!slot) return
    inOrder(slot, queueId, () => api.holdQueuedMessageEdit(slot, queueId, open)).catch(() => undefined)
  }, [slot, inOrder])

  const onReorder = useCallback((queueId: string, direction: 'next' | 'later') => {
    if (!slot) return
    const fullIds = allQueuedRef.current.map(queueIdOf).filter((id): id is string => !!id)
    const visibleIds = visibleQueuedRef.current.map(queueIdOf).filter((id): id is string => !!id)
    const vFrom = visibleIds.indexOf(queueId)
    const vTo = direction === 'next' ? vFrom - 1 : vFrom + 1
    if (vFrom < 0 || vTo < 0 || vTo >= visibleIds.length) return
    const a = fullIds.indexOf(visibleIds[vFrom])
    const b = fullIds.indexOf(visibleIds[vTo])
    if (a < 0 || b < 0) return
    const next = [...fullIds]
    ;[next[a], next[b]] = [next[b], next[a]]
    // No optimistic dispatch: the server commits and broadcasts queue_reorder to
    // every client including this one, and that WS event is the authoritative
    // store update. A local dispatch with rollback-on-failure could restore a
    // stale order when the server committed but the HTTP response was lost,
    // leaving this client in conflict with execution order.
    //
    // Unlatched for the same reason the arrows are not gated on `pendingIds`
    // inside QueueStack: with no optimistic move, a latch would freeze the arrows
    // on a card that has not visibly moved yet, and the repeat it would block is
    // an identical full-order PUT the server can absorb.
    api.reorderQueuedMessages(slot, next).catch((err: unknown) => showFailure(queueId, err))
  }, [slot, showFailure])

  // Every host showing the slot draws the shared registry's entries.
  const lost = useUnconfirmedCancels(slot)
  const listedIds = useMemo(() => new Set(allQueued.map(queueIdOf).filter((id): id is string => !!id)), [allQueued])
  const onRestoreUnconfirmed = useCallback((queueId: string) => {
    if (!slot) return
    const entry = getUnconfirmedCancel(slot, queueId)
    // Never while the server still lists it: that entry is still queued.
    if (!entry || allQueuedRef.current.some(m => queueIdOf(m) === queueId)) return
    dropUnconfirmedCancel(slot, queueId)
    restoreEntry(slot, queueId, entry.msg, entry.project, entry.dictationScope)
  }, [slot, restoreEntry])
  const onDismissUnconfirmed = useCallback((queueId: string) => {
    if (slot) dropUnconfirmedCancel(slot, queueId)
  }, [slot])
  const unconfirmed = useMemo<UnconfirmedCancels>(() => ({
    messages: lost.map(e => e.msg),
    listedIds: new Set(lost.map(e => e.queueId).filter(id => listedIds.has(id))),
    onRestore: onRestoreUnconfirmed,
    onDismiss: onDismissUnconfirmed,
  }), [lost, listedIds, onRestoreUnconfirmed, onDismissUnconfirmed])

  return useMemo(
    () => ({ onCancel, onInterrupt, onEdit, onEditOpen, onReorder, pendingIds, refusal: refusal?.slot === slot ? refusal : null, dismissRefusal, unconfirmed }),
    [onCancel, onInterrupt, onEdit, onEditOpen, onReorder, pendingIds, refusal, slot, dismissRefusal, unconfirmed],
  )
}
