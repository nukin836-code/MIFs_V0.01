---
name: TikTok search providers
description: Provider choice and constraints for finding direct public TikTok video URLs at runtime.
---

Use DuckDuckGo Lite first and Bing Video as the fallback. Keep only direct video URLs; profile, hashtag, and generic TikTok pages are not downloadable candidates.

**Why:** Ordinary Bing web/RSS results did not expose usable TikTok clip URLs during live checks, while Bing Video result metadata did. Bing Video also resolved through SSSTik without downloading media into the project.

**How to apply:** Preserve the provider order and cancellation behavior when changing search. Recheck that the fallback yields a direct TikTok clip URL and that SSSTik can resolve it; do not treat a successful search-page response alone as proof.
