// SettingsSelect wraps Radix Select, which needs pointer APIs jsdom lacks; the
// same lightweight mock the other ChatPanel suites use makes options real nodes.
vi.mock('@radix-ui/react-select', async () => await import('./__mocks__/@radix-ui/react-select'))

import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import React from 'react'

const { patchConfigMock, kirocrewConfigMock, dashboardConfigMock, consentMock } = vi.hoisted(() => ({
  patchConfigMock: vi.fn(() => Promise.resolve({})),
  kirocrewConfigMock: vi.fn(() => Promise.resolve({ agent: { model: 'auto', reasoning_effort: '' } })),
  dashboardConfigMock: vi.fn(() => Promise.resolve({ decisions_enabled: true })),
  consentMock: vi.fn(() => Promise.resolve({ enabled: true, permits: true })),
}))

vi.mock('../api/client', () => ({
  api: {
    dashboardConfig: () => dashboardConfigMock().then(extra => ({
      restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false,
      widget_density: 'more', ...extra,
    })),
    getDecisionsConsent: consentMock,
    kirocrewConfig: kirocrewConfigMock,
    models: () => Promise.resolve([
      { model_name: 'auto', description: 'Default' },
      { model_name: 'model-a', description: 'A' },
    ]),
    patchConfig: patchConfigMock,
    updateDashboardConfig: () => Promise.resolve({}),
    tipsStatus: () => Promise.resolve({ enabled_config: true, opted_out: false }),
    tipsFeedback: () => Promise.resolve({ ok: true }),
    featureVideoStatus: () => Promise.resolve({
      enabled: true, download_enabled: false, release: 'r1', cached: 0, total: 0, downloading: null,
    }),
    featureVideoFetchAll: () => Promise.resolve({ ok: true }),
  },
}))

import { ChatPanel } from '../pages/settings/ChatPanel'
import { createTestStore } from './helpers'

const ENTRY = 'Auto (decision model)'

function wrap() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <MemoryRouter initialEntries={['/settings?tab=chat&sub=models']}>
      <Provider store={createTestStore()}>
        <QueryClientProvider client={qc}><ChatPanel /></QueryClientProvider>
      </Provider>
    </MemoryRouter>,
  )
}

async function defaultModelOptions(): Promise<string[]> {
  const trigger = await screen.findByRole('combobox', { name: 'Default Model' })
  await waitFor(() => expect(trigger).not.toHaveAttribute('data-disabled'))
  fireEvent.click(trigger)
  return screen.getAllByRole('option').filter(o => !o.closest('nav')).map(o => o.textContent || '')
}

describe('ChatPanel — Default Model offers Auto (decision model)', () => {
  beforeEach(() => {
    patchConfigMock.mockClear()
    kirocrewConfigMock.mockImplementation(() => Promise.resolve({ agent: { model: 'auto', reasoning_effort: '' } }))
    dashboardConfigMock.mockImplementation(() => Promise.resolve({ decisions_enabled: true }))
    consentMock.mockImplementation(() => Promise.resolve({ enabled: true, permits: true }))
  })

  it('offers the entry when the fleet allows it and consent permits, and saves its id', async () => {
    wrap()
    await defaultModelOptions()
    // The menu stays open while the two gate reads land, and the entry joins it.
    await waitFor(() => expect(screen.getByRole('option', { name: ENTRY })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('option', { name: ENTRY }))
    await waitFor(() => expect(patchConfigMock).toHaveBeenCalledWith('agent.model', 'auto:jev'))
  })

  it('shows a saved entry the gate offers as the default, and says what new chats do', async () => {
    kirocrewConfigMock.mockImplementation(() =>
      Promise.resolve({ agent: { model: 'auto', model_jev_route: true, reasoning_effort: '' } }))
    wrap()
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: 'Default Model' })).toHaveTextContent(ENTRY))
    expect(await screen.findByTestId('default-model-decision-note')).toHaveTextContent(
      'New chats let the decision model pick',
    )
    expect(screen.getByRole('link', { name: 'Open Decisions settings' })).toBeInTheDocument()
  })

  it('does not offer it under a fleet denial, and says why', async () => {
    dashboardConfigMock.mockImplementation(() => Promise.resolve({ decisions_enabled: false }))
    wrap()
    expect(await screen.findByTestId('default-model-decision-note')).toHaveTextContent(
      'your organisation does not allow decision models',
    )
    expect(await defaultModelOptions()).not.toContain(ENTRY)
  })

  it('does not offer it while consent is enabled but does not permit, and links to the switch', async () => {
    // Consent given for another address: `enabled` alone would wrongly offer it.
    consentMock.mockImplementation(() => Promise.resolve({ enabled: true, permits: false }))
    wrap()
    const note = await screen.findByTestId('default-model-decision-note')
    expect(note).toHaveTextContent('once the Decisions switch is on')
    expect(screen.getByRole('link', { name: 'Open Decisions settings' })).toBeInTheDocument()
    expect(await defaultModelOptions()).not.toContain(ENTRY)
  })

  it('reads a failed consent read as no answer, not as consent switched off', async () => {
    consentMock.mockImplementation(() => Promise.reject(new Error('read failed')))
    // A saved entry too: an unknown gate must not claim the entry is in effect.
    kirocrewConfigMock.mockImplementation(() =>
      Promise.resolve({ agent: { model: 'auto', model_jev_route: true, reasoning_effort: '' } }))
    const { unmount } = wrap()
    await waitFor(() => expect(consentMock).toHaveBeenCalled())
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: 'Default Model' })).toHaveTextContent(ENTRY))
    expect(screen.queryByTestId('default-model-decision-note')).not.toBeInTheDocument()
    unmount()
    // The fleet's own answer still explains itself without the consent read.
    dashboardConfigMock.mockImplementation(() => Promise.resolve({ decisions_enabled: false }))
    wrap()
    expect(await screen.findByTestId('default-model-decision-note')).toHaveTextContent(
      'your organisation does not allow decision models',
    )
  })

  it('shows a saved entry the gate now refuses, and says new chats start on Auto', async () => {
    kirocrewConfigMock.mockImplementation(() =>
      Promise.resolve({ agent: { model: 'auto', model_jev_route: true, reasoning_effort: '' } }))
    consentMock.mockImplementation(() => Promise.resolve({ enabled: false, permits: false }))
    wrap()
    await waitFor(() =>
      expect(screen.getByRole('combobox', { name: 'Default Model' })).toHaveTextContent(ENTRY))
    expect(await screen.findByTestId('default-model-decision-note')).toHaveTextContent(
      'new chats start on Auto instead',
    )
  })
})
