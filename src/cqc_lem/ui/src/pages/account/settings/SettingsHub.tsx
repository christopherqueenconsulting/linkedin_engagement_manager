import { useSearchParams } from 'react-router-dom'
import SubscriptionCard from '../SubscriptionCard'
import SecurityCard from '../SecurityCard'
import AuthFactorsCard from '../AuthFactorsCard'
import VideoCreditsCard from '../VideoCreditsCard'
import AffiliateCard from '../AffiliateCard'
import TimezoneCard from '../TimezoneCard'
import LoginLocationCard from '../LoginLocationCard'
import LinkedInLoginCard from '../LinkedInLoginCard'
import LinkedInDisplayNameCard from '../LinkedInDisplayNameCard'
import LinkedInProfileRefreshCard from '../LinkedInProfileRefreshCard'
import YouTubePublishingCard from '../YouTubePublishingCard'
import CompanyPageCard from '../CompanyPageCard'
import ContentProfileCard from '../ContentProfileCard'
import StoryBankCard from '../StoryBankCard'
import VoiceExemplarsCard from '../VoiceExemplarsCard'
import NewsletterCard from '../NewsletterCard'
import GroupsCard from '../GroupsCard'
import DmTemplatesCard from '../DmTemplatesCard'
import EngagementRosterCard from '../EngagementRosterCard'
import LeadMagnetCard from '../LeadMagnetCard'
import IcpDefinitionCard from '../IcpDefinitionCard'
import { SettingsSaveProvider, SaveAllBar } from '../SettingsSaveContext'
import { EngagementPrefsProvider } from './EngagementPrefsContext'
import { useEngagementPrefs } from './engagementPrefsCtx'
import { UserPrefsProvider } from './UserPrefsContext'
import { useUserPrefs } from './userPrefsCtx'
import { ConflictsProvider } from './ConflictsContext'
import { useConflicts } from './conflictsCtx'
import { SECTIONS, resolveSection, type SectionKey } from './sections'
import VoiceSection from './VoiceSection'
import ContentSection from './ContentSection'
import TargetingSection from './TargetingSection'
import VolumeSection from './VolumeSection'
import OutreachSection from './OutreachSection'
import SafetyCard from './SafetyCard'
import { useSuggestOnly } from '../../../hooks/useEngagementMode'

function SectionBody({ section }: { section: SectionKey }) {
  switch (section) {
    case 'setup':
      return <><LinkedInLoginCard /><LinkedInDisplayNameCard /><LinkedInProfileRefreshCard /><CompanyPageCard /><TimezoneCard /><LoginLocationCard /><YouTubePublishingCard /><SafetyCard /></>
    case 'voice':
      return <><VoiceSection /><VoiceExemplarsCard /></>
    case 'content':
      return <><ContentProfileCard /><ContentSection /><StoryBankCard /></>
    case 'targeting':
      return <><EngagementRosterCard /><IcpDefinitionCard /><TargetingSection /><GroupsCard /></>
    case 'volume':
      return <VolumeSection />
    case 'outreach':
      return <><OutreachSection /><DmTemplatesCard /><LeadMagnetCard /></>
    case 'newsletter':
      return <NewsletterCard />
    case 'billing':
      return <><SubscriptionCard /><VideoCreditsCard /><AffiliateCard /><AuthFactorsCard /><SecurityCard /></>
  }
}

function SectionNav({
  active, onSelect,
}: { active: SectionKey; onSelect: (key: SectionKey) => void }) {
  const { alertCounts } = useConflicts()
  return (
    <nav aria-label="Settings sections" className="lg:w-56 lg:shrink-0">
      <ul className="flex lg:flex-col gap-1 overflow-x-auto lg:overflow-visible border-b lg:border-b-0 border-hairline pb-1 lg:pb-0">
        {SECTIONS.map((s) => {
          const alerts = alertCounts[s.key] ?? 0
          const selected = active === s.key
          return (
            <li key={s.key}>
              <button
                type="button"
                onClick={() => onSelect(s.key)}
                aria-current={selected ? 'page' : undefined}
                data-testid={`nav-${s.key}`}
                className={`w-full whitespace-nowrap text-left px-3 py-2 rounded-panel text-sm font-semibold transition-colors flex items-center justify-between gap-2 ${
                  selected ? 'bg-accent/5 text-accent' : 'text-fg-muted hover:bg-page'
                }`}
              >
                <span>{s.label}</span>
                {alerts > 0 && (
                  <span
                    data-testid={`nav-alerts-${s.key}`}
                    title={`${alerts} thing${alerts === 1 ? '' : 's'} to look at`}
                    className="shrink-0 min-w-5 h-5 px-1 rounded-full bg-warning text-white text-xs font-bold flex items-center justify-center"
                  >
                    {alerts}
                  </span>
                )}
              </button>
            </li>
          )
        })}
      </ul>
    </nav>
  )
}

// The two shared single-row writes have no per-card save button of their own any more, so their
// result (including "fix N invalid values before saving") is reported here, next to the section.
function SharedSaveStatus() {
  const { message: engMessage } = useEngagementPrefs()
  const { message: userMessage } = useUserPrefs()
  const messages = [engMessage, userMessage].filter(Boolean)
  if (messages.length === 0) return null
  return (
    <div className="space-y-1">
      {messages.map((m, i) => (
        <p key={i} className={`text-sm font-medium ${m!.ok ? 'text-success' : 'text-danger'}`}>
          {m!.text}
        </p>
      ))}
    </div>
  )
}

/**
 * The 8-section Settings hub (issue #558). One save provider for the WHOLE hub — the engagement
 * preferences and the account preferences are each a single-row write shared across several
 * sections, so they register once and Save All only writes what is actually dirty.
 *
 * Every section stays MOUNTED (inactive ones are hidden) so switching sections can never quietly
 * discard a card's unsaved edits — the save bar is the only thing that decides what is written.
 */
export default function SettingsHub() {
  const [searchParams, setSearchParams] = useSearchParams()
  const active = resolveSection(searchParams.get('section'), searchParams.get('tab'))
  const suggestOnly = useSuggestOnly()

  const selectSection = (key: SectionKey) => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.set('section', key)
      next.delete('tab')
      return next
    }, { replace: true })
  }

  return (
    <SettingsSaveProvider>
      <EngagementPrefsProvider>
        <UserPrefsProvider>
          <ConflictsProvider>
            <div className="flex flex-col lg:flex-row gap-6">
              <SectionNav active={active} onSelect={selectSection} />
              <div className="flex-1 min-w-0">
                {SECTIONS.map((s) => (
                  <div
                    key={s.key}
                    aria-hidden={s.key !== active}
                    className={s.key === active ? 'space-y-6' : 'hidden'}
                  >
                    <div>
                      <h2 className="text-xl font-semibold text-fg">{s.label}</h2>
                      <p className="text-sm text-fg-muted">{(suggestOnly && s.suggestBlurb) || s.blurb}</p>
                    </div>
                    <SectionBody section={s.key} />
                  </div>
                ))}
                <SharedSaveStatus />
              </div>
            </div>
            <SaveAllBar />
          </ConflictsProvider>
        </UserPrefsProvider>
      </EngagementPrefsProvider>
    </SettingsSaveProvider>
  )
}
