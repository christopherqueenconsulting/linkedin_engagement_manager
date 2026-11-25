import CopyBlock from '../../components/CopyBlock'
import SettingsCard from '../../components/SettingsCard'
import { AGENT_PROMPT, ALIGN_ACCOUNT_PROMPT, STARTER_PROMPTS } from './clientSnippets'

/**
 * What to say to the agent once it is connected (plan §6 card 3). This is the block people paste
 * into a CHAT, so it is static text and never carries the token or a connect command.
 */
export function AgentPromptCard() {
  return (
    <SettingsCard
      title="First message to your agent"
      subtitle="Once the command above has run, paste this into your agent's chat to check the connection."
    >
      <CopyBlock label="First message" text={AGENT_PROMPT} warning="Safe to paste into your agent's chat" />
    </SettingsCard>
  )
}

/** A few things worth asking once it works (plan §6 card 4). Static, like the card above. */
export function StarterPromptsCard() {
  return (
    <SettingsCard title="Things to ask" subtitle="Starting points. Your agent can read; it cannot approve or send.">
      <div className="space-y-3">
        {STARTER_PROMPTS.map((p) => (
          <CopyBlock key={p.id} label={p.title} text={p.text} />
        ))}
      </div>
    </SettingsCard>
  )
}

/**
 * Setup interview (#2802). Unlike the cards above it can change settings, so it says what stays with
 * you: every change is shown first, can be undone, and anything that loosens a safety gate is a
 * request you approve in LEM.
 */
export function AlignAccountCard() {
  return (
    <SettingsCard
      title="Align my account"
      subtitle="Your agent asks about your goals, customer and voice, then proposes settings. You see each change before it is made; gate-loosening changes wait for your approval in LEM."
    >
      <CopyBlock label={ALIGN_ACCOUNT_PROMPT.title} text={ALIGN_ACCOUNT_PROMPT.text} />
    </SettingsCard>
  )
}
