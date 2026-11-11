---
name: TikTok search providers
description: Provider choice and constraints for finding direct public TikTok video URLs at runtime.
---

Use DuckDuckGo Lite first and Bing Video as the fallback. Keep only direct video URLs; profile, hashtag, and generic TikTok pages are not downloadable candidates.

**Why:** Ordinary Bing web/RSS results did not expose usable TikTok clip URLs during live checks, while Bing Video result metadata did. Bing Video also resolved through SSSTik without downloading media into the project.

**How to apply:** Preserve the provider order and cancellation behavior when changing search. Recheck that the fallback yields a direct TikTok clip URL and that SSSTik can resolve it; do not treat a successful search-page response alone as proof.

For auto-import, compare the request with the candidate's spoken transcript and TikTok track title before saving. Prefer transcript or track matches; reject a detected speech mismatch and try another candidate. Use a strong caption match only when speech was not detected.

**Why:** The user chose a combined search that prioritizes actual audio after two TikTok captions matched the request while their attached sounds did not.

**How to apply:** Keep the DuckDuckGo Lite → Bing Video order and parallel downloader race; run relevance checks before publishing, and keep Telegram as the durable audio store.
