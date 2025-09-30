Export chat history from the Beeper desktop client

Requires enabling the Beeper Desktop API, see https://developers.beeper.com/

To run, create a `.env` file containing `BEEPER_ACCESS_TOKEN=<token>`, where `<token>` is what you get back per https://developers.beeper.com/desktop-api/auth, then run:

```
uv run --env-file .env python beepex.py /c/temp/beepex
```
