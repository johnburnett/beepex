# -*- coding: utf-8 -*-
import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
import html
import os
from pathlib import Path
import re
import shutil
import socket
import sys
from typing import no_type_check, NoReturn, TextIO

from beeper_desktop_api import AsyncBeeperDesktop
from beeper_desktop_api.types import Attachment, Chat, ChatListResponse, Message, User
import bleach
from dotenv import load_dotenv
from packaging import version
import requests
from rich import traceback
from tqdm import tqdm
from tqdm.asyncio import tqdm_asyncio

try:
    from __version__ import __version__  # type: ignore
except ModuleNotFoundError:
    __version__ = "dev"

traceback.install(
    show_locals=True,
    locals_max_length=3,
    locals_max_string=148,
    locals_hide_dunder=False,
    width=160,
)


@dataclass(frozen=True, kw_only=True)
class Config:
    beeper_min_version = "4.1.294"
    host_url = "http://localhost:23373"
    access_token: str
    request_headers: dict[str, str]


# fmt: off
FILE_NAME_RESERVED_NAMES = {
    "aux", "con", "nul", "prn",
    "com0", "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt0", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
}
# fmt: on
FILE_NAME_RESERVED_CHARS_RE = re.compile(r'["*/:<>?\\|]')

CONFIG: Config | None = None


def cfg() -> Config:
    assert CONFIG
    return CONFIG


def init_cfg(args) -> None:
    global CONFIG
    assert CONFIG is None
    if args.token:
        access_token = args.token
    else:
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
            # don't use _MEIPASS, which is where the single-file bundle is unpacked to
            bin_path = Path(sys.executable)
        else:
            bin_path = Path(__file__)
        env_file = bin_path.parent / ".env"
        load_dotenv(dotenv_path=env_file)
        access_token = os.environ.get("BEEPER_ACCESS_TOKEN")
    if not access_token:
        fatal(
            "Access token not provided via command line or BEEPER_ACCESS_TOKEN environment variable."
        )
    assert isinstance(access_token, str)
    headers = {"Authorization": f"Bearer {access_token}"}
    CONFIG = Config(access_token=access_token, request_headers=headers)


def get_user_name(user: User) -> str:
    attr_names = (
        "full_name",
        "username",
        "email",
        "phone_number",
        "id",
    )
    for attr_name in attr_names:
        value = getattr(user, attr_name)
        if value:
            return value
    assert False


def get_chat_top_sender_ids(
    chat: Chat, messages: list[Message], self_id: str, max_senders: int
) -> list[str]:
    # using defaultdict here because sometimes there are messages associated
    # with a chat that are sent by a user who isn't listed in the chat
    # participants.  We also prime the dict with all participants, because
    # listed participants haven't always sent messages.
    sent_histogram = defaultdict(
        int, ((user.id, 0) for user in chat.participants.items)
    )
    for msg in messages:
        sent_histogram[msg.sender_id] += 1
    sorted_senders = sorted(sent_histogram.items(), key=lambda it: it[1])
    top_senders = [id for id, _ in filter(lambda it: it[0] != self_id, sorted_senders)][
        :max_senders
    ]
    return top_senders


def get_chat_title(chat: Chat, messages: list[Message]) -> str:
    self_user = None
    for ii in range(len(chat.participants.items)):
        if chat.participants.items[ii].is_self:
            self_user = chat.participants.items[ii]
            break

    full_title = chat.title
    if self_user and chat.title == self_user.full_name:
        max_senders_in_title = 4
        top_sender_ids = get_chat_top_sender_ids(
            chat, messages, self_user.id, max_senders_in_title
        )
        id_to_name = {user.id: user.full_name for user in chat.participants.items}
        # Note: participants list doesn't include all participants?
        # e.g. '@discordgobot:beeper.local' has been seen sending a message
        # with this chat's chat_id, but it isn't in the returned chat participant list).
        # str(...) cast is to work around mypy complaining that .get is type "str|None"
        top_sender_names = [str(id_to_name.get(id, id)) for id in top_sender_ids]
        full_title = ", ".join(top_sender_names)
    assert full_title
    return full_title


def is_message_blank(message: Message) -> bool:
    # Seen in a few messages so far, likely a bug in beeper
    return (
        message.text is None
        and message.attachments is None
        and message.reactions is None
    )


@dataclass
class ExportContext:
    output_file_path: Path
    fout: TextIO
    attachment_dir_path: Path
    # Map attachment source_url (which may not exist locally) to local hydrated file path
    att_source_to_hydrated: dict[str, Path | None]
    resource_dir_path: Path


HE = html.escape

info = print


def fatal(msg: str) -> NoReturn:
    print(msg)
    sys.exit(1)


def sanitize_file_name(file_name: str) -> str:
    if file_name.casefold() in FILE_NAME_RESERVED_NAMES:
        file_name = file_name + "_"
    file_name = FILE_NAME_RESERVED_CHARS_RE.sub("", file_name)
    file_name = file_name.strip(" \t\n.")
    return file_name if file_name else "_"


async def hydrate_attachment(client: AsyncBeeperDesktop, url: str) -> Path | None:
    """Make Beeper download a cached copy of the attachment.

    Returns the file:/// URL of the attachment in Beeper's local cache.
    """
    if url.startswith("mxc://") or url.startswith("localmxc://"):
        response = await client.assets.download(url=url)
        if response.error:
            return None
        else:
            hydrated_url = response.src_url
    else:
        hydrated_url = url
    assert isinstance(hydrated_url, str)
    assert hydrated_url.startswith("file://")
    try:
        return Path.from_uri(hydrated_url)
    except ValueError:
        # Some attachments were incorrectly sent by some Android clients, and
        # are relative file:// paths that point to nothing.
        return None


async def hydrate_chat_attachments(
    client: AsyncBeeperDesktop, chat: Chat, messages: list[Message]
) -> dict[str, Path | None]:
    source_urls = []
    for msg in messages:
        for att in msg.attachments if msg.attachments else []:
            if att.src_url:
                source_urls.append(att.src_url)
    tasks = [hydrate_attachment(client, url) for url in source_urls]
    hydrated_paths = await tqdm_asyncio.gather(
        *tasks, total=len(tasks), desc="Downloading chat attachments", leave=False
    )
    assert len(source_urls) == len(hydrated_paths)
    source_to_hydrated = dict(zip(source_urls, hydrated_paths))
    return source_to_hydrated


def archive_attachment(
    attachment_dir_path: Path,
    att_source_to_hydrated: dict[str, Path | None],
    time_sent: datetime,
    att: Attachment,
) -> Path | None:
    if not att.src_url:
        return None
    source_file_path = att_source_to_hydrated[att.src_url]
    if not source_file_path:
        return None
    attachment_dir_path.mkdir(parents=True, exist_ok=True)
    time_sent_str = time_sent.strftime("%Y-%m-%d_%H-%M-%S")
    target_file_name, target_file_ext = os.path.splitext(att.file_name or "")
    target_file_name = (
        sanitize_file_name(f"{time_sent_str}_{target_file_name}") + target_file_ext
    )
    target_file_path = attachment_dir_path / target_file_name
    mtime = time_sent.timestamp()
    if not target_file_path.exists():
        shutil.copy(source_file_path, target_file_path)
        os.utime(target_file_path, times=(mtime, mtime))
    return target_file_path


async def message_to_html(ctx: ExportContext, chat: Chat, msg: Message) -> None:
    # from pprint import pformat
    # ctx.fout.write(f'<section><pre>{pformat(msg)}</pre></section>\n')
    # return

    sec_class = "msg-self" if msg.is_sender else "msg-them"
    ts_utc = msg.timestamp
    ts_local = ts_utc.astimezone()
    ts_utc_str = ts_utc.strftime("%Y-%m-%d %H:%M:%S %Z")
    ts_local_str = ts_local.strftime("%Y-%m-%d %H:%M:%S")
    replied_link = ""
    linked_message_id = getattr(msg, "linked_message_id", None)
    if linked_message_id:
        replied_link = f'<a title="Reply to message {HE(linked_message_id)}" href="#{HE(linked_message_id)}">&nbsp;(replied &#x2934;&#xFE0E;)</a>'
    ctx.fout.write(
        f'<section class="msg {sec_class}">'
        f'<div id="{HE(msg.id)}" class="msg-header">'
        f'<span class="msg-contact-name">{HE(msg.sender_name or "Unknown")}</span>'
        f"{replied_link}"
        f'<span class="msg-datetime" title="{ts_utc_str}">{ts_local_str}</span>'
        f'<a title="Message {HE(msg.id)}" href="#{HE(msg.id)}">&#x1F517;&#xFE0E;</a>'
        f"</div><div>\n"
    )

    if msg.text:
        msg_text = html.escape(msg.text, quote=False)
        msg_text = msg_text.replace("\n", "<br>\n")
        msg_text = bleach.linkify(msg_text)
        ctx.fout.write(msg_text)

    for att in msg.attachments if msg.attachments else []:
        att_file_path = archive_attachment(
            ctx.attachment_dir_path, ctx.att_source_to_hydrated, ts_local, att
        )
        if att_file_path:
            att_url = att_file_path.relative_to(
                ctx.output_file_path.parent, walk_up=True
            ).as_posix()
            att_url = html.escape(att_url)

            dim_attr = (
                f' width="{att.size.width}" height="{att.size.height}"'
                if att.size
                else ""
            )
            if att.type == "img":
                ctx.fout.write(
                    f'<a href="{att_url}"><img loading="lazy"{dim_attr} src="{att_url}"/></a>\n'
                )
            elif att.type == "video":
                ctx.fout.write(
                    f'<video controls loop playsinline{dim_attr} src="{att_url}"/>\n'
                )
            elif att.type == "audio":
                ctx.fout.write(f'<audio controls src="{att_url}"/>\n')
        else:
            ctx.fout.write(
                f'<span class="error">&#x26A0;&#xFE0E; Missing Attachment: "{att.src_url}"</span>'
            )

    ctx.fout.write("\n</div>")

    if msg.reactions:
        user_id_to_full_name = {}
        for user in chat.participants.items:
            user_id_to_full_name[user.id] = user.full_name
        ctx.fout.write('<span class="reactions">')
        keys_to_names = defaultdict(list)
        for reaction in msg.reactions:
            name = str(
                user_id_to_full_name.get(
                    reaction.participant_id, reaction.participant_id
                )
            )
            keys_to_names[reaction.reaction_key].append(name)
        for key, names in sorted(keys_to_names.items()):
            tooltip = f"{key}\n" + "\n".join(sorted(names))
            ctx.fout.write(f'<div title="{HE(tooltip)}">{HE(key)}</div>')
        ctx.fout.write("</span>")

    ctx.fout.write("</section>\n")


async def chat_to_html(
    ctx: ExportContext, chat_title: str, chat: Chat, messages: list[Message]
) -> None:
    css_dir = html.escape(
        ctx.resource_dir_path.relative_to(
            ctx.output_file_path.parent, walk_up=True
        ).as_posix()
    )
    ctx.fout.write(
        f"<!DOCTYPE html>\n"
        f'<html lang="en">\n'
        f"<head>\n"
        f'    <meta charset="UTF-8">\n'
        f'    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"    <title>Chat: {chat_title}</title>\n"
        f'    <link rel="stylesheet" href="{css_dir}/water.css">\n'
        f'    <link rel="stylesheet" href="{css_dir}/extra.css">\n'
        f"</head>\n"
        f"<body>\n"
    )

    ctx.fout.write("<header>\n")
    ctx.fout.write('<section class="chat-header">\n')
    ctx.fout.write(f"<h1>{chat_title}</h1>\n")
    ctx.fout.write("<details>\n")
    ctx.fout.write(
        f'<div><span class="chat-details-label">Network: </span>{HE(chat.network)}</div>\n'
    )
    ctx.fout.write(
        f'<div><span class="chat-details-label">Account ID: </span>{HE(chat.account_id)}</div>\n'
    )
    ctx.fout.write(
        f'<div><span class="chat-details-label">Chat ID: </span>{HE(chat.id)}</div>\n'
    )
    ctx.fout.write(
        f'<div><span class="chat-details-label">Message Count: </span>{len(messages)}</div>\n'
    )
    names = [get_user_name(user) for user in chat.participants.items]
    ctx.fout.write(
        f'<div><span class="chat-details-label">Participants: </span>{len(names)}</div>\n'
    )
    ctx.fout.write("<ul>\n")
    for name in sorted(names, key=lambda it: it.casefold()):
        ctx.fout.write(f"<li>{HE(name)}</li>\n")
    ctx.fout.write("</ul>\n")
    ctx.fout.write("</details>\n")
    ctx.fout.write("</section>")
    ctx.fout.write("</header>\n")

    ctx.fout.write("<main>\n")
    for msg in tqdm(messages, desc="Writing chat messages", leave=False):
        await message_to_html(ctx, chat, msg)
    ctx.fout.write("</main>\n")

    ctx.fout.write("</body></html>\n")


def write_chats_index(
    output_root_dir: Path,
    resource_dir_path: Path,
    export_time: datetime,
    export_duration: timedelta,
    chat_id_to_html_path: dict[str, Path],
    chat_id_to_title: dict[str, str],
    chats: list[ChatListResponse],
) -> Path:
    network_to_chats: dict[str, list[ChatListResponse]] = {}
    for chat in chats:
        network_to_chats.setdefault(chat.network, []).append(chat)

    index_file_path = output_root_dir / "index.html"
    with open(index_file_path, "w", encoding="utf-8") as fp:
        css_dir = resource_dir_path.relative_to(output_root_dir).as_posix()
        hostname = socket.gethostname()
        export_ymd = export_time.strftime("%Y-%m-%d")
        export_hms = export_time.strftime("%H:%M:%S")
        fp.write(
            f"<!DOCTYPE html>\n"
            f'<html lang="en">\n'
            f"<head>\n"
            f'    <meta charset="UTF-8">\n'
            f'    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
            f"    <title>Beeper Chats</title>\n"
            f'    <link rel="stylesheet" href="{HE(css_dir)}/water.css">\n'
            f'    <link rel="stylesheet" href="{HE(css_dir)}/extra.css">\n'
            f"</head>\n"
            f"<body>\n"
            f"<header>\n"
            f'<section class="chat-header">\n'
            f"    <h1>Beeper Chats</h1>\n"
            f"    <details>\n"
            f'        <div><span class="chat-details-label">beepex Version: </span>{HE(str(__version__))}</div>\n'
            f'        <div><span class="chat-details-label">Export Host: </span>{HE(hostname)}</div>\n'
            f'        <div><span class="chat-details-label">Export Date: </span>{HE(export_ymd)}</div>\n'
            f'        <div><span class="chat-details-label">Export Time: </span>{HE(export_hms)}</div>\n'
            f'        <div><span class="chat-details-label">Export Duration: </span>{HE(str(export_duration))}</div>\n'
            f"    </details>\n"
            f"</section>\n"
            f"</header>\n"
            f"<main>\n"
        )
        fp.write("<ul>\n")
        for network_name, network_chats in sorted(
            network_to_chats.items(), key=lambda it: it[0].casefold()
        ):
            fp.write(f"<li>{network_name}\n")
            fp.write("<ul>\n")
            for chat in sorted(
                network_chats, key=lambda chat: chat_id_to_title[chat.id].casefold()
            ):
                chat_html_path = chat_id_to_html_path[chat.id]
                chat_url = chat_html_path.relative_to(output_root_dir)
                fp.write(
                    f'<li><a href="{chat_url.as_posix()}">{HE(chat_id_to_title[chat.id])}</a></li>\n'
                )
            fp.write("</ul>\n")
            fp.write("</li>\n")
        fp.write("</ul>\n")
        fp.write("</main></body></html>\n")
    return index_file_path


def copy_resource_files(target_dir_path: Path) -> Path:
    source_dir_path = Path(__file__).parent / "css"
    assert source_dir_path.is_dir()
    target_dir_path.mkdir(parents=True, exist_ok=True)
    for file_name in ("water.css", "extra.css"):
        source_file_path = source_dir_path / file_name
        target_file_path = target_dir_path / file_name
        shutil.copy(source_file_path, target_file_path)
    return target_dir_path


async def export_chat(
    client: AsyncBeeperDesktop,
    output_root_dir: Path,
    resource_dir_path: Path,
    chat_summary: Chat,
) -> tuple[str, Path]:
    chat = await client.chats.retrieve(chat_summary.id)
    messages = []
    # seen_ids and sorting by timestamp and not sort_key is to work around
    # a bug with Beeper not filtering out messages or setting sort_key properly.
    seen_ids = set()
    with tqdm(desc="Gathering chat messages", leave=False) as progress:
        async for message in client.messages.list(chat.id):
            progress.update()
            if not is_message_blank(message) and message.id not in seen_ids:
                seen_ids.add(message.id)
                messages.append(message)
    messages.sort(key=lambda message: message.timestamp)

    chat_title = get_chat_title(chat, messages)
    chat_title_safe = sanitize_file_name(f"{chat_title} ({chat.id})")
    att_source_to_hydrated = await hydrate_chat_attachments(client, chat, messages)

    network_dir_name = sanitize_file_name(chat.network.lower())
    output_dir_path = output_root_dir / "chats" / network_dir_name
    output_dir_path.mkdir(parents=True, exist_ok=True)

    html_file_path = output_dir_path / (chat_title_safe + ".html")
    attachment_dir_path = output_root_dir / "media" / network_dir_name / chat_title_safe

    with open(html_file_path, "w", encoding="utf-8") as fp:
        context = ExportContext(
            html_file_path,
            fp,
            attachment_dir_path,
            att_source_to_hydrated,
            resource_dir_path,
        )
        await chat_to_html(context, chat_title, chat, messages)

    if messages:
        mtime = messages[-1].timestamp.astimezone().timestamp()
        os.utime(html_file_path, times=(mtime, mtime))

    return chat_title, html_file_path


async def export_all_chats(
    client: AsyncBeeperDesktop, output_root_dir: Path, include_chat_ids: set[str]
) -> Path:
    info(f'Exporting chats to "{output_root_dir}"')
    time_start = datetime.now()

    resource_dir_path = copy_resource_files(output_root_dir / "media/beepex")

    # Chats returned by list don't currently have all info associated with
    # them (e.g. participants list is truncated), so treating them as
    # summaries to be filled out with individual chats.retrieve(id) calls.
    chat_summaries = []
    async for chat_summary in client.chats.list():
        if include_chat_ids and chat_summary.id not in include_chat_ids:
            continue
        chat_summaries.append(chat_summary)

    chat_id_to_title = {}
    chat_id_to_html_path = {}
    with tqdm(chat_summaries, leave=False) as progress:
        for chat_summary in progress:
            progress.set_description(f'Chat "{chat_summary.id}"')
            chat_title, html_path = await export_chat(
                client, output_root_dir, resource_dir_path, chat_summary
            )
            chat_id_to_title[chat_summary.id] = chat_title
            chat_id_to_html_path[chat_summary.id] = html_path

    time_end = datetime.now()
    export_duration = time_end - time_start
    info(f"Export took {export_duration}")

    chat_index_path = write_chats_index(
        output_root_dir,
        resource_dir_path,
        time_start,
        export_duration,
        chat_id_to_html_path,
        chat_id_to_title,
        chat_summaries,
    )

    return chat_index_path


def check_beeper_version() -> None:
    try:
        resp = requests.get(
            f"{cfg().host_url}/oauth/userinfo",
            headers=cfg().request_headers,
        )
    except requests.ConnectionError as ex:
        info("Error connecting to Beeper, make sure the Beeper Desktop API is enabled.")
        fatal(repr(ex))
    resp.raise_for_status()
    beeper_version_str = resp.headers.get("X-Beeper-Desktop-Version")
    if beeper_version_str:
        beeper_version = version.parse(beeper_version_str)
    else:
        fatal("Can't get Beeper desktop version")
    min_version = version.parse(cfg().beeper_min_version)
    if beeper_version < min_version:
        fatal(
            f"Installed Beeper {beeper_version} is too old, version {min_version} is required."
        )


@no_type_check
async def create_example(output_root_dir: Path):
    from test.mock import MockAsyncBeeperDesktop

    this_dir_path = Path(__file__).parent
    test_data_path = this_dir_path / "test"
    if output_root_dir.exists():
        shutil.rmtree(output_root_dir)

    client = MockAsyncBeeperDesktop(test_data_path)
    index_html_path = await export_all_chats(client, output_root_dir, set())
    with open(index_html_path, encoding="utf-8") as fp:
        output_html = fp.read()
    re_subs = (
        (
            r"beepex Version: </span>.*</div>",
            r"beepex Version: </span>$VERSION</div>",
        ),
        (
            r"Export Host: </span>.*</div>",
            r"Export Host: </span>circusmonkey</div>",
        ),
        (
            r"Export Date: </span>\d\d\d\d-\d\d-\d\d</div>",
            r"Export Date: </span>2025-09-07</div>",
        ),
        (
            r"Export Time: </span>\d\d:\d\d:\d\d</div>",
            r"Export Time: </span>23:12:06</div>",
        ),
        (
            r"Export Duration: </span>\d:\d\d:\d\d\.\d*</div>",
            r"Export Duration: </span>0:00:00.034469</div>",
        ),
    )
    for patt, rep in re_subs:
        output_html = re.sub(patt, rep, output_html, count=1)

    with open(index_html_path, "w", encoding="utf-8") as fp:
        fp.write(output_html)


async def main():
    parser = argparse.ArgumentParser(prog="beepex")
    parser.add_argument("output_root_dir", type=Path)
    parser.add_argument("-v", "--version", action="version", version=__version__)
    parser.add_argument(
        "--token",
        help="Beeper Desktop API access token.  If not provided, uses the BEEPER_ACCESS_TOKEN environment variable, potentially read from a .env file.",
    )
    parser.add_argument(
        "-i",
        "--include_chat_id",
        dest="include_chat_ids",
        action="append",
        default=[],
        metavar="ID",
        help="Chat ID to include (may be supplied multiple times)",
    )
    parser.add_argument("--create_example", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    init_cfg(args)
    # todo
    # check_beeper_version()

    if args.create_example:
        await create_example(args.output_root_dir)
    else:
        client = AsyncBeeperDesktop(access_token=cfg().access_token)
        await export_all_chats(client, args.output_root_dir, set(args.include_chat_ids))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        fatal("Manually aborted")
