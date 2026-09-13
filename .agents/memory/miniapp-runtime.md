---
name: Mini App runtime
description: Runtime separation between the Telegram bot and the FastAPI Mini App backend.
---

The Telegram bot workflow and the Mini App API are separate runtimes. The bot's `uv` environment includes the bot dependencies, while FastAPI belongs to the webapp dependency set and should be run through the Mini App startup path.

**Why:** Importing the Mini App API from the bot runtime is not a valid health check when the webapp dependencies have not been installed in that environment.

**How to apply:** Validate bot changes with the bot workflow and Python compilation; validate `webapp_api.py` through its dedicated Mini App runtime or after installing `requirements-webapp.txt`, rather than adding webapp dependencies to the bot just for testing.