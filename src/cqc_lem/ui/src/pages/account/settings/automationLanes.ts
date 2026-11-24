export type ModeKey =
  | 'nurture_dm_mode'
  | 'artifact_dm_mode'
  | 'group_post_mode'
  | 'followup_step_mode'
  | 'appreciation_dm_mode'
  | 'funnel_stage_mode'

// One row per lane that has a per-user approval mode (issue #2624). `auto` says, in one sentence,
// what switching the lane to auto-approve means.
export const AUTOMATION_LANES: { key: ModeKey; label: string; review: string; auto: string }[] = [
  {
    key: 'nurture_dm_mode',
    label: 'Reply nurture DMs',
    review: 'Each drafted reply to a lead waits for your approval.',
    auto: 'Drafted replies to leads are queued as approved and sent at their slot without you seeing them.',
  },
  {
    key: 'artifact_dm_mode',
    label: 'Lead-magnet DMs',
    review: 'Each resource DM waits for your approval.',
    auto: 'The resource DM goes to anyone who comments your keyword, without you seeing it first.',
  },
  {
    key: 'group_post_mode',
    label: 'Weekly group post',
    review: 'The weekly draft is held until you restore it in the studio.',
    auto: 'The weekly group post publishes at its slot unless you skip it first.',
  },
  {
    key: 'followup_step_mode',
    label: 'DM follow-up steps',
    review: 'Each follow-up step waits as a draft for your approval.',
    auto: 'Follow-up messages to people who have not replied are sent on schedule without you seeing them.',
  },
  {
    key: 'appreciation_dm_mode',
    label: 'Appreciation DMs',
    review: 'Each thank-you DM waits as a draft for your approval.',
    auto: 'Thank-you DMs to people who accept, recommend or collaborate with you are sent without you seeing them.',
  },
  {
    key: 'funnel_stage_mode',
    label: 'Outreach funnel stages',
    review: 'After a comment or connect step fires, the next step waits for your approval.',
    auto: 'After a comment posts, the connect request goes out unseen. The DM still waits for you.',
  },
]
