# beepex

Export [Beeper](https://www.beeper.com/) chat history to static HTML, handy for archival purposes.

Example output can be seen [here](https://html-preview.github.io/?url=https://github.com/johnburnett/beepex/blob/main/example/index.html).

# Initial setup

beepex requires running the Beeper desktop client and depends upon the Beeper Desktop API being enabled.

1. [Enable the Beeper Desktop API](https://developers.beeper.com/desktop-api)
1. [Create an access token](https://developers.beeper.com/desktop-api/auth) and make note of it for future steps below.
    - It is convenient to name the token "beepex" and set it to non-expiring.
    - It is unnecessary to "Allow sensitive actions" (beepex is read-only and will not modify chats).

# Installing beepex

## Pre-built

Grab the latest [pre-built release](https://github.com/johnburnett/beepex/releases)

## Building manually

1. [Install uv](https://docs.astral.sh/uv/getting-started/installation/)
1. Clone this repo (`git clone https://github.com/johnburnett/beepex`)
1. In the clone, either run `build.sh` to build a self-contained binary, or run `uv run beepex.py` to run without building

# Running beepex

beepex needs to know the value of the access token created during initial setup above.  It can be provided in a few ways, depending on your preferences:

1. Set an environment variable named `BEEPER_ACCESS_TOKEN` with the value of the access token above.
1. Saved in a file called `.env` file next to the beepex executable, containing `BEEPER_ACCESS_TOKEN=YOUR_TOKEN`.
1. Passed on the command line with the `--token YOUR_TOKEN` argument.

To export all chats to the directory `C:\temp\beepex_export`, run:

```
beepex.exe C:\temp\beepex_export
```
