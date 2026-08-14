import { describe, expect, it, vi, afterEach, beforeEach } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { SettingsSaveProvider, SaveAllBar } from './SettingsSaveContext'
import { useRegisterSaveSection, sectionSaveCallbacks } from './settingsSave'

const capture = vi.fn()
vi.mock('../../utils/analytics', () => ({
  capture: (...args: unknown[]) => capture(...args),
  EVENTS: { prefsSaved: 'prefs_saved' },
}))

function Section({ id, save }: { id: string; save: () => Promise<boolean> }) {
  useRegisterSaveSection(id, id, true, save)
  return null
}

function harness(sections: { id: string; save: () => Promise<boolean> }[]) {
  return render(
    <SettingsSaveProvider>
      {sections.map((s) => <Section key={s.id} id={s.id} save={s.save} />)}
      <SaveAllBar />
    </SettingsSaveProvider>
  )
}

beforeEach(() => capture.mockClear())
afterEach(cleanup)

describe('prefs_saved instrumentation (issue #646)', () => {
  it('reports one event per saved section, named by section', async () => {
    harness([
      { id: 'voice', save: () => Promise.resolve(true) },
      { id: 'targeting', save: () => Promise.resolve(true) },
    ])
    fireEvent.click(screen.getByRole('button', { name: /Save All/ }))
    await waitFor(() => expect(capture).toHaveBeenCalledTimes(2))
    expect(capture).toHaveBeenCalledWith('prefs_saved', { section: 'voice', ok: true })
    expect(capture).toHaveBeenCalledWith('prefs_saved', { section: 'targeting', ok: true })
  })

  it('records a failed save as ok:false rather than dropping it', async () => {
    harness([
      { id: 'voice', save: () => Promise.reject(new Error('500')) },
      { id: 'volume', save: () => Promise.resolve(false) },
    ])
    fireEvent.click(screen.getByRole('button', { name: /Save All/ }))
    await waitFor(() => expect(capture).toHaveBeenCalledTimes(2))
    expect(capture).toHaveBeenCalledWith('prefs_saved', { section: 'voice', ok: false })
    expect(capture).toHaveBeenCalledWith('prefs_saved', { section: 'volume', ok: false })
  })

  // Rendered outside a Layout there is no dock to portal into, so the bar must still place itself
  // — otherwise a standalone settings card would lose its Save All entirely (issue #596).
  it('falls back to its own fixed corner when no floating dock exists', () => {
    const { container } = harness([{ id: 'voice', save: () => Promise.resolve(true) }])
    const bar = screen.getByRole('button', { name: /Save All/ }).parentElement as HTMLElement
    expect(container.contains(bar)).toBe(true)
    expect(bar.className).toContain('fixed')
  })

  // A card's own Save button never goes through saveAll, so it has to report the same event or
  // prefs_saved silently counts only the Save All users.
  it('reports the same event from a card own save button', () => {
    sectionSaveCallbacks('groups').onSuccess()
    expect(capture).toHaveBeenCalledWith('prefs_saved', { section: 'groups', ok: true })
    sectionSaveCallbacks('lead-magnet').onError()
    expect(capture).toHaveBeenCalledWith('prefs_saved', { section: 'lead-magnet', ok: false })
  })
})
