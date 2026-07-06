-- Cached, DURABLE voice/expertise synthesis of each scraped profile. The generation prompts used to
-- inline the ENTIRE profile JSON (profile.model_dump_json()) as the "voice reference" — bloated, and
-- the root of "LEM drift": the profile's volatile recent_activities made the model talk about
-- whatever the user is currently building. Instead we distill a small, stable voice brief on a WEEKLY
-- schedule (auto-refreshed on a fresh scrape) and drop THAT into every generation prompt. `synthesis`
-- holds the brief text; `synthesis_generated_at` drives the ~7-day staleness selector for the weekly
-- refresh task.
ALTER TABLE profiles
    ADD COLUMN synthesis TEXT NULL AFTER data,
    ADD COLUMN synthesis_generated_at TIMESTAMP NULL AFTER synthesis;
