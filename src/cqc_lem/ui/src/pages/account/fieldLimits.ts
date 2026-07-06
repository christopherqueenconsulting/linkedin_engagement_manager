// Input length limits — kept in lockstep with the DB column widths and the backend Pydantic
// max_length (see api/main.py _LEN_* and the Flyway migrations). Gating the inputs here stops an
// over-long value before it ever reaches the API, which previously overflowed a column and
// silently rolled back the whole settings save (fixed by V52).
export const FIELD_LIMITS = {
  tone: 255, // engagement_preferences.tone (V52: VARCHAR(255))
  comment_style: 255, // engagement_preferences.comment_style VARCHAR(255)
  goals: 2000, // engagement_preferences.business_goals/personal_goals (TEXT; app cap)
  lm_keyword: 128, // lead_magnet_settings.keyword VARCHAR(128)
  lm_message: 2000, // lead_magnet_settings.message (TEXT; app cap)
  dm_template: 2000, // dm_templates.template_text (TEXT; app cap)
  nl_title: 255, // newsletter_settings.title VARCHAR(255)
  nl_topic: 512, // newsletter_settings.topic VARCHAR(512)
} as const
