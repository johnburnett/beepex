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

## Full usage
```
usage: beepex [-h] [-v] [--token TOKEN] [--env ENV]
              [--include_account_ids AccountID [AccountID ...]]
              [--exclude_account_ids AccountID [AccountID ...]]
              [--include_chat_ids ChatID [ChatID ...]] [--exclude_chat_ids ChatID [ChatID ...]]
              [--chat_names_remap_file CHAT_NAMES_REMAP_FILE]
              output_root_dir

positional arguments:
  output_root_dir

options:
  -h, --help            show this help message and exit
  -v, --version         show program's version number and exit
  --token TOKEN         Beeper Desktop API access token. If not provided, uses the
                        BEEPER_ACCESS_TOKEN environment variable, potentially read from a .env file
                        if it is next to the beepex executable.
  --env ENV             Path to an env file that contains a definition of the BEEPER_ACCESS_TOKEN
                        environment variable.
  --include_account_ids AccountID [AccountID ...]
  --exclude_account_ids AccountID [AccountID ...]
  --include_chat_ids ChatID [ChatID ...]
  --exclude_chat_ids ChatID [ChatID ...]
  --chat_names_remap_file CHAT_NAMES_REMAP_FILE
                        Path to a CSV file that contains mappings from a chatID to name, one per
                        line. Useful for when someone has deleted their account on a platform and no
                        longer has a name exposed.

The include/exclude arguments are processed in the order given, and may be used multiple times. The
starting set of chats to include depends upon the first include/exclude argument that is used:
- If the first is an "include_" type, the include/excludes are "building up" the set of chat IDs
  from nothing.
- If the first is an "exclude_" type, the include/excludes are "pruning down" the set of chat IDs
  from all possible chats.
- In either case, subsequent includes can re-add chats that were previously excluded, and vice-
  versa.
```
