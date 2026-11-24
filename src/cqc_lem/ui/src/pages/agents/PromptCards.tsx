import CopyBlock from '../../components/CopyBlock'
import SettingsCard from '../../components/SettingsCard'
import { AGENT_PROMPT, STARTER_PROMPTS } from './clientSnippets'

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
