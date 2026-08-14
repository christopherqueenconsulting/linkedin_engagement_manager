import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'
import SettingsHub from './SettingsHub'

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))

vi.mock('../../contexts/useAuth', () => ({
  useAuth: () => ({ sessionToken: 'tok' }),
}))

vi.mock('../SettingsSaveContext', () => ({
  SettingsSaveProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
  SaveAllBar: () => <div data-testid="save-all-bar" />,
}))

vi.mock('../settingsSave', () => ({
  useRegisterSaveSection: vi.fn(),
  sectionSaveCallbacks: () => ({ onSuccess: vi.fn(), onError: vi.fn() }),
}))

vi.mock('./EngagementPrefsContext', () => ({
  EngagementPrefsProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
}))
vi.mock('./engagementPrefsCtx', () => ({ useEngagementPrefs: () => ({ message: null }) }))

vi.mock('./UserPrefsContext', () => ({
  UserPrefsProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
}))
vi.mock('./userPrefsCtx', () => ({ useUserPrefs: () => ({ message: null }) }))

vi.mock('./ConflictsContext', () => ({
  ConflictsProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
}))
vi.mock('./conflictsCtx', () => ({ useConflicts: () => ({ alertCounts: {} }) }))

vi.mock('../SubscriptionCard', () => ({ default: () => <div data-testid="subscription-card" /> }))
vi.mock('../VideoCreditsCard', () => ({ default: () => <div data-testid="video-credits-card" /> }))
vi.mock('../TimezoneCard', () => ({ default: () => <div data-testid="timezone-card" /> }))
vi.mock('../LoginLocationCard', () => ({ default: () => <div data-testid="login-location-card" /> }))
vi.mock('../LinkedInLoginCard', () => ({ default: () => <div data-testid="linkedin-login-card" /> }))
vi.mock('../LinkedInDisplayNameCard', () => ({ default: () => <div data-testid="linkedin-display-name-card" /> }))
vi.mock('../LinkedInProfileRefreshCard', () => ({ default: () => <div data-testid="linkedin-profile-refresh-card" /> }))
vi.mock('../YouTubePublishingCard', () => ({ default: () => <div data-testid="youtube-publishing-card" /> }))
vi.mock('../CompanyPageCard', () => ({ default: () => <div data-testid="company-page-card" /> }))
vi.mock('../ContentProfileCard', () => ({ default: () => <div data-testid="content-profile-card" /> }))
vi.mock('../StoryBankCard', () => ({ default: () => <div data-testid="story-bank-card" /> }))
vi.mock('../NewsletterCard', () => ({ default: () => <div data-testid="newsletter-card" /> }))
vi.mock('../GroupsCard', () => ({ default: () => <div data-testid="groups-card" /> }))
vi.mock('../DmTemplatesCard', () => ({ default: () => <div data-testid="dm-templates-card" /> }))
vi.mock('../EngagementRosterCard', () => ({ default: () => <div data-testid="engagement-roster-card" /> }))
vi.mock('../LeadMagnetCard', () => ({ default: () => <div data-testid="lead-magnet-card" /> }))
vi.mock('../AffiliateCard', () => ({ default: () => <div data-testid="affiliate-card" /> }))
vi.mock('../SecurityCard', () => ({ default: () => <div data-testid="security-card" /> }))
vi.mock('../AuthFactorsCard', () => ({ default: () => <div data-testid="auth-factors-card" /> }))

vi.mock('./VoiceSection', () => ({ default: () => <div data-testid="voice-section" /> }))
vi.mock('./ContentSection', () => ({ default: () => <div data-testid="content-section" /> }))
vi.mock('./TargetingSection', () => ({ default: () => <div data-testid="targeting-section" /> }))
vi.mock('./VolumeSection', () => ({ default: () => <div data-testid="volume-section" /> }))
vi.mock('./OutreachSection', () => ({ default: () => <div data-testid="outreach-section" /> }))
vi.mock('./SafetyCard', () => ({ default: () => <div data-testid="safety-card" /> }))

function harness(initialEntry: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <SettingsHub />
    </MemoryRouter>,
  )
}

describe('SettingsHub card order', () => {
  it('positions Story Bank after Publishing on the content section', () => {
    harness('/account?section=content')
    const profile = screen.getByTestId('content-profile-card')
    const publishing = screen.getByTestId('content-section')
    const storyBank = screen.getByTestId('story-bank-card')
    expect(profile.compareDocumentPosition(publishing) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(publishing.compareDocumentPosition(storyBank) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('keeps every other section body mounted but hidden', () => {
    harness('/account?section=content')
    expect(screen.getByTestId('content-section').parentElement?.getAttribute('aria-hidden')).toBe('false')
    expect(screen.getByTestId('voice-section').parentElement?.getAttribute('aria-hidden')).toBe('true')
  })
})
