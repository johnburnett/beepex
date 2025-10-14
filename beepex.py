# -*- coding: utf-8 -*-
import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
import html
import os
from pathlib import Path
import re
import socket
import sys
from typing import Any, NewType, NoReturn, Protocol, TextIO, TypeVar, Type

import bleach
from packaging import version
import requests
from rich import traceback
from tqdm import tqdm
from tqdm.asyncio import tqdm_asyncio

traceback.install(
    show_locals=True,
    locals_max_length=3,
    locals_max_string=148,
    locals_hide_dunder=False,
    width=160,
)

BEEPER_MIN_VERSION = "4.1.244"
BEEPER_HOST_URL = "http://localhost:23373/"
BEEPER_ACCESS_TOKEN = os.environ.get("BEEPER_ACCESS_TOKEN")
REQUEST_HEADERS = {"Authorization": f"Bearer {BEEPER_ACCESS_TOKEN}"}

# fmt: off
FILE_NAME_RESERVED_NAMES = {
    "aux", "con", "nul", "prn",
    "com0", "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt0", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
}
# fmt: on
FILE_NAME_RESERVED_CHARS_RE = re.compile(r'["*/:<>?\\|]')


class DictConstructible(Protocol):
    def __init__(self, data: dict) -> None: ...


TDC = TypeVar("TDC", bound=DictConstructible)
ChatID = NewType("ChatID", str)
MessageID = NewType("MessageID", str)
ReactionID = NewType("ReactionID", str)
UserID = NewType("UserID", str)


@dataclass
class Attachment:
    type: str
    source_url: str
    file_name: str
    resolution: tuple[int, int] | None

    def __init__(self, d: dict):
        self.type = d["type"]
        self.source_url = d["srcURL"]
        self.file_name = d.get("fileName", "")
        self.resolution = None
        if size := d.get("size"):
            self.resolution = (size["width"], size["height"])
        # if self.type not in ("img", "video", "audio"):
        #     print(self)


@dataclass
class Reaction:
    id: ReactionID
    sender_id: UserID
    key: str

    def __init__(self, d: dict):
        self.id = d["id"]
        self.sender_id = d["participantID"]
        self.key = d["reactionKey"]


@dataclass
class Message:
    id: MessageID
    timestamp: datetime
    sort_key: str
    from_self: bool
    sender_id: UserID
    sender_name: str
    text: str | None
    reactions: list[Reaction]
    attachments: list[Attachment]

    def __init__(self, d: dict):
        self.id = d["messageID"]
        self.timestamp = datetime.fromisoformat(d["timestamp"])
        self.sort_key = d["sortKey"]
        self.from_self = d.get("isSender", False)
        self.sender_id = d["senderID"]
        self.sender_name = d.get("senderName", self.sender_id)
        self.text = d.get("text")
        self.reactions = [Reaction(r) for r in d.get("reactions", [])]
        self.attachments = [Attachment(att) for att in d.get("attachments", [])]


@dataclass
class User:
    id: UserID
    full_name: str
    is_self: bool

    def __init__(self, d: dict):
        self.id = d["id"]
        self.full_name = d.get("fullName", d["id"])
        self.is_self = d.get("isSelf", False)


@dataclass
class Chat:
    id: ChatID
    account: str
    network: str
    title: str
    participants: list[User]

    messages: list[Message] = field(default_factory=list)
    _full_title: str | None = None

    def __init__(self, d: dict):
        self.id = d["id"]
        self.account = d["accountID"]
        self.network = d["network"]
        self.title = d["title"]
        # todo: may not be full list of users? (d["participants"]["hasMore"])
        self.participants = [User(it) for it in d["participants"]["items"]]

    def full_title(self) -> str:
        if not self._full_title:
            self_user = None
            for ii in range(len(self.participants)):
                if self.participants[ii].is_self:
                    self_user = self.participants[ii]
                    break

            if self_user and self.title == self_user.full_name:
                max_senders_in_title = 4
                top_sender_ids = self._get_top_sender_ids(
                    self_user.id, max_senders_in_title
                )
                id_to_name = {user.id: user.full_name for user in self.participants}
                # Note: participants list doesn't include all participants?
                # e.g. '@discordgobot:beeper.local' has been seen sending a message
                # with this chat's chat_id, but it isn't in the returned chat participant list).
                top_sender_names = [id_to_name.get(id, id) for id in top_sender_ids]
                self._full_title = ", ".join(top_sender_names)
            else:
                self._full_title = self.title
            assert self._full_title
        return self._full_title

    def _get_top_sender_ids(self, self_id: str, max_senders: int) -> list[UserID]:
        # using defaultdict here because sometimes there are messages associated
        # with a chat that are sent by a user who isn't listed in the chat
        # participants.  We also prime the dict with all participants, because
        # listed participants haven't always sent messages.
        sent_histogram = defaultdict(int, ((user.id, 0) for user in self.participants))
        for msg in self.messages:
            sent_histogram[msg.sender_id] += 1
        sorted_senders = sorted(sent_histogram.items(), key=lambda it: it[1])
        top_senders = [
            id for id, _ in filter(lambda it: it[0] != self_id, sorted_senders)
        ][:max_senders]
        return top_senders


@dataclass
class ExportContext:
    output_file_path: Path
    fout: TextIO
    attachment_dir_path: Path
    # Map attachment source_url (which may not exist locally) to local hydrated file path
    att_source_to_hydrated: dict[str, Path]
    resource_dir_path: Path
    users: dict[UserID, User]


HE = html.escape


def fatal(msg: str) -> NoReturn:
    print(msg)
    sys.exit(1)


def sanitize_file_name(file_name: str) -> str:
    if file_name.casefold() in FILE_NAME_RESERVED_NAMES:
        file_name = file_name + "_"
    file_name = FILE_NAME_RESERVED_CHARS_RE.sub("", file_name)
    file_name = file_name.strip(" \t\n.")
    return file_name if file_name else "_"


def get_users(chats: list[Chat]) -> dict[UserID, User]:
    users = {}
    for chat in chats:
        users.update({user.id: user for user in chat.participants})
    return users


async def hydrate_attachment(url: str) -> Path:
    """Make Beeper download a cached copy of the attachment.

    Returns the file:/// URL of the attachment in Beeper's local cache.
    """

    def _sync(url):
        if url.startswith("mxc://"):
            resp = requests.post(
                f"{BEEPER_HOST_URL}/v0/download-asset",
                headers=REQUEST_HEADERS,
                json={"url": url},
            )
            resp.raise_for_status()
            data = resp.json()
            hydrated_url = data["srcURL"]
        else:
            hydrated_url = url
        assert hydrated_url.startswith("file://")
        return Path.from_uri(hydrated_url)

    return await asyncio.to_thread(_sync, url)


async def hydrate_attachments(chats: list[Chat]) -> dict[str, Path]:
    source_urls = []
    for chat in chats:
        for msg in chat.messages:
            for att in msg.attachments:
                source_urls.append(att.source_url)
    tasks = [hydrate_attachment(url) for url in source_urls]
    hydrated_paths = await tqdm_asyncio.gather(
        *tasks, total=len(tasks), desc="Downloading attachments"
    )
    assert len(source_urls) == len(hydrated_paths)
    source_to_hydrated = dict(zip(source_urls, hydrated_paths))
    return source_to_hydrated


def archive_attachment(
    attachment_dir_path: Path,
    att_source_to_hydrated: dict[str, Path],
    time_sent: datetime,
    att: Attachment,
) -> Path:
    source_file_path = att_source_to_hydrated[att.source_url]
    attachment_dir_path.mkdir(parents=True, exist_ok=True)
    time_sent_str = time_sent.strftime("%Y-%m-%d_%H-%M-%S")
    target_file_name, target_file_ext = os.path.splitext(att.file_name)
    target_file_name = (
        sanitize_file_name(f"{time_sent_str}_{target_file_name}") + target_file_ext
    )
    target_file_path = attachment_dir_path / target_file_name
    mtime = time_sent.timestamp()
    if not target_file_path.exists():
        source_file_path.copy(target_file_path)
        os.utime(target_file_path, times=(mtime, mtime))
    return target_file_path


def message_to_html(ctx: ExportContext, msg: Message) -> None:
    # from pprint import pformat
    # ctx.fout.write(f'<section><pre>{pformat(msg)}</pre></section>\n')
    # return

    sec_class = "msg-self" if msg.from_self else "msg-them"
    ts_utc = msg.timestamp
    ts_local = ts_utc.astimezone()
    ts_utc_str = ts_utc.strftime("%Y-%m-%d %H:%M:%S %Z")
    ts_local_str = ts_local.strftime("%Y-%m-%d %H:%M:%S %Z")
    ctx.fout.write(
        f'<section class="msg {sec_class}">'
        f'<div id="{HE(msg.id)}" class="msg-header">'
        f'<span class="msg-contact-name">{HE(msg.sender_name)}</span>'
        f'<span class="msg-datetime" title="{ts_utc_str}">{ts_local_str}</span>'
        f'<a class="permalink" title="Message {HE(msg.id)}" href="#{HE(msg.id)}">&#x1F517;&#xFE0E;'
        f"</a></div>\n"
        f"<div>\n"
    )

    if msg.text:
        msg_text = html.escape(msg.text, quote=False)
        msg_text = msg_text.replace("\n", "<br>\n")
        msg_text = bleach.linkify(msg_text)
        ctx.fout.write(msg_text)

    for att in msg.attachments:
        att_file_path = archive_attachment(
            ctx.attachment_dir_path, ctx.att_source_to_hydrated, ts_local, att
        )
        att_url = att_file_path.relative_to(
            ctx.output_file_path.parent, walk_up=True
        ).as_posix()
        att_url = html.escape(att_url)

        dim_attr = (
            f' width="{att.resolution[0]}" height="{att.resolution[1]}"'
            if att.resolution
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

    ctx.fout.write("</div>")

    if msg.reactions:
        ctx.fout.write('<span class="reactions">\n')
        keys_to_names = defaultdict(list)
        for reaction in msg.reactions:
            name = ctx.users[reaction.sender_id].full_name
            keys_to_names[reaction.key].append(name)
        for key, names in sorted(keys_to_names.items()):
            tooltip = f"{key}\n" + "\n".join(sorted(names))
            ctx.fout.write(f'<div title="{HE(tooltip)}">{HE(key)}</div>')
        ctx.fout.write("</span>\n")

    ctx.fout.write("</section>\n")


def chat_to_html(ctx: ExportContext, chat: Chat) -> None:
    if not chat.messages:
        return

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
        f"    <title>Chat: {HE(chat.full_title())}</title>\n"
        f'    <link rel="stylesheet" href="{css_dir}/water.css">\n'
        f'    <link rel="stylesheet" href="{css_dir}/extra.css">\n'
        f"</head>\n"
        f"<body>\n"
    )

    ctx.fout.write("<header>\n")
    ctx.fout.write('<section class="chat-header">\n')
    ctx.fout.write(f"<h1>{HE(chat.full_title())}</h1>\n")
    ctx.fout.write("<details>\n")
    ctx.fout.write(
        f'<div><span class="chat-details-label">Network: </span>{HE(chat.network)}</div>\n'
    )
    ctx.fout.write(
        f'<div><span class="chat-details-label">Account ID: </span>{HE(chat.account)}</div>\n'
    )
    ctx.fout.write(
        f'<div><span class="chat-details-label">Chat ID: </span>{HE(chat.id)}</div>\n'
    )
    ctx.fout.write(
        f'<div><span class="chat-details-label">Message Count: </span>{len(chat.messages)}</div>\n'
    )
    ctx.fout.write(
        f'<div><span class="chat-details-label">Participants: </span>{len(chat.participants)}</div>\n'
    )
    users = chat.participants
    names = [user.full_name for user in users]
    ctx.fout.write("<ul>\n")
    for name in sorted(names, key=lambda it: it.casefold()):
        ctx.fout.write(f"<li>{HE(name)}</li>\n")
    ctx.fout.write("</ul>\n")
    ctx.fout.write("</details>\n")
    ctx.fout.write("</section>")
    ctx.fout.write("</header>\n")

    ctx.fout.write("<main>\n")
    for msg in tqdm(chat.messages, desc="Exporting messages", leave=False):
        message_to_html(ctx, msg)
    ctx.fout.write("</main>\n")

    ctx.fout.write("</body></html>\n")


def write_chats_index(
    output_root_dir: Path,
    resource_dir_path: Path,
    chat_id_to_html_path: dict[ChatID, Path],
    chats: list[Chat],
) -> None:
    network_to_chats: dict[str, list[Chat]] = {}
    for chat in chats:
        network_to_chats.setdefault(chat.network, []).append(chat)

    index_file_path = output_root_dir / "index.html"
    with open(index_file_path, "w", encoding="utf-8") as fp:
        css_dir = resource_dir_path.relative_to(output_root_dir).as_posix()
        hostname = socket.gethostname()
        now = datetime.now()
        now_date = now.strftime("%Y-%m-%d")
        now_time = now.strftime("%H:%M:%S")
        fp.write(
            f"<!DOCTYPE html>\n"
            f'<html lang="en">\n'
            f"<head>\n"
            f'    <meta charset="UTF-8">\n'
            f'    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
            f"    <title>Beeper Chats</title>\n"
            f'    <link rel="stylesheet" href="{HE(css_dir)}/water.css">\n'
            f"</head>\n"
            f"<body>\n"
            f"    <h1>Beeper Chats</h1>\n"
            f'    <div style="color: var(--text-muted);">'
            f'Exported from <span style="font-family: monospace;">{HE(hostname)}</span> on {now_date} at {now_time}'
            f"</div>\n"
        )
        fp.write("<ul>\n")
        for network_name, network_chats in sorted(
            network_to_chats.items(), key=lambda it: it[0].casefold()
        ):
            fp.write(f"<li>{network_name}\n")
            fp.write("<ul>\n")
            for chat in sorted(
                network_chats, key=lambda chat: chat.full_title().casefold()
            ):
                chat_html_path = chat_id_to_html_path[chat.id]
                chat_url = chat_html_path.relative_to(output_root_dir)
                fp.write(
                    f'<li><a href="{chat_url.as_posix()}">{HE(chat.full_title())}</a></li>\n'
                )
            fp.write("</ul>\n")
            fp.write("</li>\n")
        fp.write("</ul>\n")
        fp.write("</body></html>\n")


def copy_resource_files(target_dir_path: Path) -> Path:
    source_dir_path = Path(__name__).parent / "css"
    assert source_dir_path.is_dir()
    target_dir_path.mkdir(parents=True, exist_ok=True)
    for file_name in ("water.css", "extra.css"):
        source_file_path = source_dir_path / file_name
        target_file_path = target_dir_path / file_name
        source_file_path.copy(target_file_path)
    return target_dir_path


def write_html(
    chats: list[Chat], att_source_to_hydrated: dict[str, Path], output_root_dir: Path
) -> None:
    resource_dir_path = copy_resource_files(output_root_dir / "media/beepex")
    chat_id_to_html_path = {}
    users = get_users(chats)
    with tqdm(chats, desc="Exporting chats") as progress:
        for chat in progress:
            chat_title = sanitize_file_name(f"{chat.full_title()} ({chat.id})")
            progress.set_description(f'Exporting chat "{chat_title}"')
            chat.messages.sort(key=lambda chat: chat.sort_key)

            network_dir_name = sanitize_file_name(chat.network.lower())
            output_dir_path = output_root_dir / "chats" / network_dir_name
            output_dir_path.mkdir(parents=True, exist_ok=True)

            html_file_path = output_dir_path / (chat_title + ".html")
            attachment_dir_path = (
                output_root_dir / "media" / network_dir_name / chat_title
            )
            with open(html_file_path, "w", encoding="utf-8") as fp:
                context = ExportContext(
                    html_file_path,
                    fp,
                    attachment_dir_path,
                    att_source_to_hydrated,
                    resource_dir_path,
                    users,
                )
                chat_to_html(context, chat)
            if chat.messages:
                mtime = chat.messages[-1].timestamp.astimezone().timestamp()
                os.utime(html_file_path, times=(mtime, mtime))

            chat_id = chat.id
            assert chat_id not in chat_id_to_html_path
            chat_id_to_html_path[chat_id] = html_file_path

    write_chats_index(output_root_dir, resource_dir_path, chat_id_to_html_path, chats)


def get_all_chats() -> list[Chat]:
    chat_id_to_chats: dict[str, Chat] = {}
    chat_id_to_messages: dict[str, list[Message]] = {}

    cursor = None
    with tqdm(desc="Gathering messages") as progress:
        while True:
            params: dict[Any, Any] = {"limit": 20}  # 20 is the cap per page
            params["excludeLowPriority"] = False
            params["includeMuted"] = True
            if cursor:
                params["cursor"] = cursor
                params["direction"] = "before"

            resp = requests.get(
                f"{BEEPER_HOST_URL}/v0/search-messages",
                headers=REQUEST_HEADERS,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

            for chat_id, details in data.get("chats").items():
                chat_id_to_chats[chat_id] = Chat(details)

            for msg in data.get("items", []):
                chat_id_to_messages.setdefault(msg["chatID"], []).append(Message(msg))

            if not data.get("hasMore") or not data.get("oldestCursor"):
                break
            cursor = data["oldestCursor"]
            progress.update()

    messages_with_no_chat = set(chat_id_to_messages.keys()) - set(
        chat_id_to_chats.keys()
    )
    assert not messages_with_no_chat

    chats = []
    for chat_id, chat in chat_id_to_chats.items():
        chat.messages = chat_id_to_messages.get(chat_id, [])
        chats.append(chat)
    return chats


def get_beeper_items(
    progress_text: str, endpoint: str, params: dict, item_type: Type[TDC]
) -> list[TDC]:
    items = []
    cursor = None
    params = params.copy()
    with tqdm(desc=progress_text, leave=False) as progress:
        while True:
            if cursor:
                params["cursor"] = cursor
                params["direction"] = "before"
            resp = requests.get(
                f"{BEEPER_HOST_URL}/v0/{endpoint}",
                headers=REQUEST_HEADERS,
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            items.extend(data["items"])
            if not data.get("hasMore") or not data.get("oldestCursor"):
                break
            cursor = data["oldestCursor"]
            progress.update()
    return items


def get_all_chats2() -> list[Chat]:
    chats = get_beeper_items("Gathering chats", "list-chats", {"limit": 200}, Chat)
    for chat in tqdm(chats, desc="Gathering chat messages", leave=False):
        chat.messages = get_beeper_items(
            f'Chat "{chat.id}"',
            "list-messages",
            {"limit": 500, "chatID": chat.id},
            Message,
        )
    return chats


def check_prerequisites() -> None:
    if not BEEPER_ACCESS_TOKEN:
        fatal("BEEPER_ACCESS_TOKEN environment variable not set.")

    try:
        resp = requests.get(
            f"{BEEPER_HOST_URL}/oauth/userinfo",
            headers=REQUEST_HEADERS,
        )
    except requests.ConnectionError as ex:
        print(
            "Error connecting to Beeper, make sure the Beeper Desktop API is enabled."
        )
        fatal(repr(ex))
    resp.raise_for_status()
    beeper_version_str = resp.headers.get("X-Beeper-Desktop-Version")
    if beeper_version_str:
        beeper_version = version.parse(beeper_version_str)
    else:
        fatal("Can't get Beeper desktop version")
    min_version = version.parse(BEEPER_MIN_VERSION)
    if beeper_version < min_version:
        fatal(
            f"Installed Beeper {beeper_version} is too old, version {min_version} is required."
        )


def create_example():
    import json
    import shutil

    this_dir_path = Path(__file__).parent
    output_root_dir = this_dir_path / "example"
    if output_root_dir.exists():
        shutil.rmtree(output_root_dir)

    with open("test/chat.json", encoding="utf-8") as fp:
        data = json.load(fp)
    chat = Chat(data["chat"])
    chat.messages = [Message(msg) for msg in data["messages"]]
    example_png_path = this_dir_path / "test" / "goodgood.png"

    write_html([chat], {"file:///test/goodgood.png": example_png_path}, output_root_dir)

    index_html_path = output_root_dir / "index.html"
    with open(index_html_path, encoding="utf-8") as fp:
        output_html = fp.read()
    output_html = re.sub(
        r'monospace;">.*</span> on \d\d\d\d-\d\d-\d\d at \d\d:\d\d:\d\d</div>',
        r'monospace;">circusmonkey</span> on 2025-09-07 at 23:12:06</div>',
        output_html,
        count=1,
    )
    with open(index_html_path, "w", encoding="utf-8") as fp:
        fp.write(output_html)


async def main():
    try:
        check_prerequisites()

        parser = argparse.ArgumentParser()
        parser.add_argument("output_root_dir", type=Path)
        args = parser.parse_args()

        chats = get_all_chats()
        att_source_to_hydrated = await hydrate_attachments(chats)
        write_html(chats, att_source_to_hydrated, args.output_root_dir)
    except KeyboardInterrupt:
        fatal("Manually aborted")


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--create-example":
        create_example()
    else:
        asyncio.run(main())
