# beepex

Export chat history from the Beeper desktop client to html files, handy for archival purposes.

[Click here for example output](https://html-preview.github.io/?url=https://github.com/johnburnett/beepex/blob/main/example/index.html)

# Usage

Requires enabling the Beeper Desktop API, see https://developers.beeper.com/

To run:

- [Install uv](https://docs.astral.sh/uv/getting-started/installation/)
- Clone this repo
- In the clone, run `uv sync`
- Create a `.env` file containing `BEEPER_ACCESS_TOKEN=<token>`, where `<token>` is what you get back per https://developers.beeper.com/desktop-api/auth
- Run the following to export all chats to `/c/temp/beepex`
```
uv run --env-file .env python beepex.py /c/temp/beepex
```
