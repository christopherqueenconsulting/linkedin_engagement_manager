import { useEffect, useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import type { EngPrefs } from '../types'
import AudienceMixCard from './AudienceMixCard'
import { mergeMix, sharePercent } from './audienceMix'

let eng: Partial<EngPrefs> = {}
const subscribers = new Set<() => void>()

vi.mock('./engagementPrefsCtx', () => ({
  useEngagementPrefs: () => {
    const [, rerender] = useState(0)
    useEffect(() => {
      const sub = () => rerender((n) => n + 1)
      subscribers.add(sub)
      return () => { subscribers.delete(sub) }
    }, [])
    return {
      eng,
      setEng: (patch: Partial<EngPrefs>) => {
        eng = { ...eng, ...patch }
        subscribers.forEach((sub) => sub())
      },
    }
  },
}))

vi.mock('./conflictsCtx', () => ({
  useConflicts: () => ({ findings: [], alertCounts: {}, applyFix: () => {} }),
}))

afterEach(() => { cleanup(); eng = {} })

const box = (settingKey: string) =>
  within(screen.getByTestId(`field-${settingKey}`)).getByRole('textbox') as HTMLTextAreaElement

describe('audience mix card', () => {
  it('starts a user with no mix at a 0% share', () => {
    eng = { audience_mix: null }
    render(<AudienceMixCard />)
    expect((screen.getByTestId('audience-share') as HTMLInputElement).value).toBe('0')
  })

  it('writes both audiences, the share and the topics into one audience_mix object', () => {
    eng = { audience_mix: null }
    render(<AudienceMixCard />)
    fireEvent.change(box('audience_primary'), { target: { value: 'ops leaders' } })
    fireEvent.change(box('audience_secondary'), { target: { value: 'small-business owners' } })
    fireEvent.change(screen.getByTestId('audience-share'), { target: { value: '60' } })
    expect(eng.audience_mix).toEqual({
      primary_audience: 'ops leaders',
      secondary_audience: 'small-business owners',
      secondary_share: 0.6,
    })
  })

  it('drops a cleared field instead of sending it blank', () => {
    expect(mergeMix({ primary_audience: 'ops', secondary_focus_topics: ['x'] },
      { primary_audience: '', secondary_focus_topics: [] })).toEqual({})
    expect(sharePercent({ secondary_share: 7 })).toBe(100)
    expect(sharePercent({})).toBe(0)
  })
})
