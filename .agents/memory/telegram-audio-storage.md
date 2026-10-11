---
name: Telegram audio storage
description: The intended source of truth and persistence model for sound media.
---

Telegram channel posts are the durable physical store for sound files. The project is the searchable catalog/map: it keeps metadata, Telegram file IDs, and audio hashes for lookup and deduplication, not permanent local copies of media.

**Why:** The user explicitly said project files should not become a second audio archive; sounds should remain in Telegram while the project indexes, searches, imports, and hashes them.

**How to apply:** Publish imported audio to the Telegram channel, persist catalog metadata and hashes, and stream or process playback/import bytes transiently. Do not add numbered audio files or durable audio caches to the project.
