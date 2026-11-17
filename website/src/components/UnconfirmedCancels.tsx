import { memo } from 'react'
import { i18nT } from '../i18n/t'
import { useLanguageGeneration } from '../i18n/useLanguageGeneration'
import type { UnconfirmedCancels as UnconfirmedCancelsState } from '../hooks/useQueuedMessageActions'
import { queuedDisplayText } from './QueueStack'

/** Queued entries whose cancel got no real answer (`utils/unconfirmedCancels`).
 *  One the server still lists is still queued: its queue card keeps Cancel, and
 *  this only notes that the cancel got no reply. One the server no longer lists
 *  was cancelled or sent, and no client read can tell which, so it is drawn
 *  here with its own payload until the person restores or dismisses it. */
export const UnconfirmedCancels = memo(function UnconfirmedCancels({ messages, listedIds, onRestore, onDismiss }: UnconfirmedCancelsState) {
  useLanguageGeneration() // memo() bails out of the provider-level repaint; subscribe directly
  if (!messages.length) return null
  return (
    <div className="px-4 flex flex-col gap-1.5" data-testid="queue-unconfirmed">
      {messages.map(m => {
        const queueId = m.meta?.queueId as string
        if (listedIds.has(queueId)) {
          return (
            <div key={queueId} data-testid="queue-cancel-no-reply" role="status" className="text-[12px] opacity-80">
              <span className="truncate">{queuedDisplayText(m)}</span>
              {' · '}
              {i18nT('components.queueStack.cancel_no_reply')}
            </div>
          )
        }
        return (
          <div
            key={queueId}
            data-testid="queue-unconfirmed-card"
            className="rounded-xl border border-[var(--border)] bg-[var(--bg)] px-3 py-2 text-[13px]"
          >
            <div className="truncate opacity-60">{queuedDisplayText(m)}</div>
            <div className="mt-1 text-[12px] opacity-80" role="status">
              {i18nT('components.queueStack.cancel_unconfirmed')}
            </div>
            <div className="mt-1.5 flex gap-2">
              <button
                type="button"
                className="rounded-md px-2 py-0.5 text-[12px] hover:bg-[var(--bg-hover)]"
                onClick={() => onRestore(queueId)}
              >
                {i18nT('components.queueStack.restore_to_composer')}
              </button>
              <button
                type="button"
                className="rounded-md px-2 py-0.5 text-[12px] hover:bg-[var(--bg-hover)]"
                onClick={() => onDismiss(queueId)}
              >
                {i18nT('app.dismiss')}
              </button>
            </div>
          </div>
        )
      })}
    </div>
  )
})
