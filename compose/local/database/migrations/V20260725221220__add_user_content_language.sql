-- Per-user content language (issue #548, Decision 2A).
-- Audio-capable video models (Veo 3.1 / 3.1 fast) expose NO API-level language or voice
-- parameter — the spoken/ambient audio is entirely prompt-driven. With native audio ON and
-- no audio direction in the motion prompt, Veo invented a voiceover AND chose its language
-- freely (posts #34/#36 shipped with foreign-language voiceovers).
-- The generator now writes the language into every audio-capable render's prompt. This column
-- is the explicit source of truth for it; when NULL we fall back to the Login Location locale
-- (users.locale, V31) and then to 'en-US'. Explicit beats location-derived: a US-based user
-- may publish in Spanish.
-- VARCHAR(16) holds a BCP-47 tag ('en-US', 'pt-BR', 'zh-Hans-CN'); matches users.locale's intent
-- while leaving room for script subtags.
ALTER TABLE users
    ADD COLUMN content_language VARCHAR(16) NULL AFTER locale;
