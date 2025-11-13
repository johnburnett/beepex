# beepex

Export [Beeper](https://www.beeper.com/) chat history to static HTML, handy for archival purposes.

Example output can be seen [here](https://html-preview.github.io/?url=https://github.com/johnburnett/beepex/blob/main/example/index.html).

## Initial setup

This requires running the Beeper desktop client, and depends upon the Beeper Desktop API being enabled.

1. [Install uv](https://docs.astral.sh/uv/getting-started/installation/)
1. Clone this repo (`git clone https://github.com/johnburnett/beepex`)
1. In the clone, run `uv sync` to install Python and any required libraries (`cd beepex; uv sync`)
1. [Enable the Beeper Desktop API](https://developers.beeper.com/desktop-api)
1. [Create an access token](https://developers.beeper.com/desktop-api/auth).  It is convenient to name the token "beepex" and set it to not expiring.  It is unnecessary to "Allow sensitive actions" (beepex will not modify chats).
1. (Optional) Create a `.env` file containing `BEEPER_ACCESS_TOKEN=<token>`, where `<token>` is what you created in the step above

## Running

If you created a `.env` file above, beepex will use it and the following will export all chats to `/c/temp/beepex`:
```
uv run beepex.py /c/temp/beepex
```
If you didn't create a `.env` file, you must supply your access token on the command line like so:
```
uv run beepex.py --token <token> /c/temp/beepex
```
