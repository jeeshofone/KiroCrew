/**
 * The line under Settings > Chat > Model's Default Model about its
 * `Auto (decision model)` entry: what it does when it is the default, and why it
 * is not offered when the picker's gate (`lib/jevRoute.ts`) refuses it. A saved
 * entry the gate refuses says so too, because new chats then start on plain Auto.
 *
 * The link opens the Decisions card, which holds the Decisions switch and the
 * model for each difficulty level; this page does not repeat either.
 */
import type { MouseEvent } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { JEV_ROUTE_MODEL } from '../../lib/jevRoute'
import { settingsPath } from '../../components/settingsPath'
import { useGuardedLeave } from '../../components/NavigationLeaveGuard'
import { i18nT } from '../../i18n/t'

/** Registry id of the Decisions card, the highlight the link lands on. */
const DECISIONS_CARD_ID = 'developer.decisions'

export function DefaultModelDecisionNote({
  shown,
  offered,
  unavailable,
}: {
  /** The Default Model the select shows, the entry's id when it is chosen. */
  shown: string
  /** The gate offers the entry right now; only then may the line say it is in effect. */
  offered: boolean
  /** Why the entry is not offered, or null while it is (or the reads are pending). */
  unavailable: 'policy' | 'consent' | null
}) {
  const navigate = useNavigate()
  const guardedLeave = useGuardedLeave()
  const chosen = shown === JEV_ROUTE_MODEL
  // An unknown gate (a failed read) says nothing rather than claim the entry works.
  if (!unavailable && !(chosen && offered)) return null
  const route = settingsPath({ tab: 'developer', sub: 'feature-previews', highlight: DECISIONS_CARD_ID })
  const onClick = (e: MouseEvent<HTMLAnchorElement>) => {
    // A modified or non-primary click opens a new tab and unmounts nothing.
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
    e.preventDefault()
    // The panel's unsaved drafts stay mounted under this row, so the leave asks first.
    guardedLeave(() => navigate(route), route)
  }
  const message = unavailable === 'policy'
    ? i18nT('pages.settings.chatPanel.default_model_decision_policy')
    : unavailable === 'consent'
      ? i18nT('pages.settings.chatPanel.default_model_decision_consent')
      : i18nT('pages.settings.chatPanel.default_model_decision_on')
  return (
    <div
      className={`mt-1 mb-3 text-[13px] ${unavailable && chosen ? 'text-warn' : 'text-muted'}`}
      role="status"
      data-testid="default-model-decision-note"
    >
      {message}
      {unavailable && chosen && <> {i18nT('pages.settings.chatPanel.default_model_decision_inactive')}</>}
      {unavailable !== 'policy' && (
        <>
          {' '}
          <Link to={route} onClick={onClick} className="text-accent hover:underline">
            {i18nT('pages.settings.chatPanel.default_model_decision_open')}
          </Link>
        </>
      )}
    </div>
  )
}
