# -*- coding: utf-8 -*-
import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
import html
import os
import posixpath
import re
import shutil
import socket
import sys
from typing import NoReturn, TextIO

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
class Message:
    id: str
    timestamp: datetime
    sort_key: str
    self_sender: bool
    sender_id: str
    sender_name: str
    text: str | None
    attachments: list[Attachment]

    def __init__(self, d: dict):
        self.id = d["messageID"]
        self.timestamp = datetime.fromisoformat(d["timestamp"])
        self.sort_key = d["sortKey"]
        self.self_sender = d.get("isSender", False)
        self.sender_id = d["senderID"]
        self.sender_name = d.get("senderName", self.sender_id)
        self.text = d.get("text")
        self.attachments = [Attachment(att) for att in d.get("attachments", [])]


@dataclass
class User:
    id: str
    full_name: str
    is_self: bool

    def __init__(self, d: dict):
        self.id = d["id"]
        self.full_name = d.get("fullName", d["id"])
        self.is_self = d.get("isSelf", False)


@dataclass
class ChatDetails:
    id: str
    account: str
    network: str
    title: str
    participants: list[User]

    def __init__(self, d: dict):
        self.id = d["id"]
        self.account = d["accountID"]
        self.network = d["network"]
        self.title = d["title"]
        # todo: may not be full list of users? (d["participants"]["hasMore"])
        self.participants = [User(it) for it in d["participants"]["items"]]


@dataclass
class Chat:
    details: ChatDetails
    messages: list[Message] = field(default_factory=list)
    _title: str | None = None

    def get_title(self) -> str:
        if not self._title:
            self._compute_title()
        return self._title

    def _compute_title(self) -> None:
        self_user = None
        for ii in range(len(self.details.participants)):
            if self.details.participants[ii].is_self:
                self_user = self.details.participants[ii]
                break

        if self_user and self.details.title == self_user.full_name:
            max_senders_in_title = 4
            top_sender_ids = self.get_top_sender_ids(self_user.id, max_senders_in_title)
            id_to_name = {user.id: user.full_name for user in self.details.participants}
            # Note: participants list doesn't include all participants?
            # e.g. '@discordgobot:beeper.local' has been seen sending a message
            # with this chat's chat_id, but it isn't in the returned chat participant list).
            top_sender_names = [id_to_name.get(id, id) for id in top_sender_ids]
            self._title = ", ".join(top_sender_names)
        else:
            self._title = self.details.title
        assert self._title

    def get_top_sender_ids(self, self_id: str, max_senders: int) -> list[str]:
        # using defaultdict here because sometimes there are messages associated
        # with a chat that are sent by a user who isn't listed in the chat
        # participants.  We also prime the dict with all participants, because
        # listed participants haven't always sent messages.
        sent_histogram = defaultdict(int)
        for user in self.details.participants:
            sent_histogram[user.id] = 0
        for msg in self.messages:
            sent_histogram[msg.sender_id] += 1
        sorted_senders = sorted(sent_histogram.items(), key=lambda it: it[1])
        top_senders = [
            id for id, _ in filter(lambda it: it[0] != self_id, sorted_senders)
        ][:max_senders]
        return top_senders


@dataclass
class ExportContext:
    output_file_path: str
    fout: TextIO
    attachment_dir_path: str
    # Map attachment source_url to local hydrated file:/// URL
    att_source_to_hydrated: dict[str, str]
    css_url_sub_dir: str


def fatal(msg: str) -> NoReturn:
    print(msg)
    sys.exit(1)


def sanitize_file_name(file_name: str) -> str:
    if file_name.casefold() in FILE_NAME_RESERVED_NAMES:
        file_name = file_name + "_"
    file_name = FILE_NAME_RESERVED_CHARS_RE.sub("", file_name)
    file_name = file_name.strip(" \t\n.")
    return file_name if file_name else "_"


def chat_details_to_html(fout: TextIO, chat: Chat) -> None:
    fout.write('<section class="chat-header">\n')
    fout.write(f"<h1>{chat.get_title()}</h1>\n")
    fout.write("<details>\n")
    fout.write(
        f'<div><span class="chat-details-label">Network: </span><span>{chat.details.network}</span></div>\n'
    )
    fout.write(
        f'<div><span class="chat-details-label">Account ID: </span><span>{chat.details.account}</span></div>\n'
    )
    fout.write(
        f'<div><span class="chat-details-label">Chat ID: </span><span>{chat.details.id}</span></div>\n'
    )
    fout.write('<div><span class="chat-details-label">Participants:</span></div>\n')
    users = chat.details.participants
    names = [user.full_name for user in users]
    for name in sorted(names, key=lambda it: it.casefold()):
        fout.write(f"<div>{name}</div>\n")
    fout.write("</details>\n")
    fout.write("</section>")


async def hydrate_attachment(url: str) -> str:
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
            return data["srcURL"]
        else:
            return url

    return await asyncio.to_thread(_sync, url)


async def hydrate_attachments(chats: list[Chat]) -> dict[str, str]:
    source_urls = []
    for chat in chats:
        for msg in chat.messages:
            for att in msg.attachments:
                source_urls.append(att.source_url)
    tasks = [hydrate_attachment(url) for url in source_urls]
    hydrated_urls = await tqdm_asyncio.gather(
        *tasks, total=len(tasks), desc="Downloading attachments"
    )
    assert len(source_urls) == len(hydrated_urls)
    for url in hydrated_urls:
        assert url.startswith("file:///")
    source_to_hydrated = dict(zip(source_urls, hydrated_urls))
    return source_to_hydrated


def archive_attachment(
    attachment_dir_path: str,
    att_source_to_hydrated: dict[str, str],
    time_sent: datetime,
    att: Attachment,
) -> str:
    hydrated_url = att_source_to_hydrated[att.source_url]
    source_file_path = hydrated_url[len("file:///") :]
    if os.path.sep != "/":
        source_file_path = source_file_path.replace("/", os.path.sep)
    os.makedirs(attachment_dir_path, exist_ok=True)
    time_sent_str = time_sent.strftime("%Y-%m-%d_%H-%M-%S")
    target_file_name, target_file_ext = os.path.splitext(att.file_name)
    target_file_name = (
        sanitize_file_name(f"{time_sent_str}_{target_file_name}") + target_file_ext
    )
    target_file_path = os.path.join(attachment_dir_path, target_file_name)
    mtime = time_sent.timestamp()
    if not os.path.exists(target_file_path):
        shutil.copy(source_file_path, target_file_path)
        os.utime(target_file_path, times=(mtime, mtime))
    return target_file_path


def message_to_html(ctx: ExportContext, msg: Message) -> None:
    # from pprint import pformat
    # ctx.fout.write(f'<section><pre>{pformat(msg)}</pre></section>\n')
    # return

    sec_class = "msg-self" if msg.self_sender else "msg-them"
    ts_utc = msg.timestamp
    ts_local = ts_utc.astimezone()
    ts_utc_str = ts_utc.strftime("%Y-%m-%d %H:%M:%S %Z")
    ts_local_str = ts_local.strftime("%Y-%m-%d %H:%M:%S %Z")
    ctx.fout.write(
        f'<section class="msg {sec_class}">'
        f'<div id="{msg.id}" class="msg-header">'
        f'<span class="msg-contact-name">{msg.sender_name}</span>'
        f'<span class="msg-datetime" title="{ts_utc_str}">{ts_local_str}</span>'
        f'<a class="permalink" title="Message {msg.id}" href="#{msg.id}">&#x1F517;&#xFE0E;'
        f"</a></div>\n"
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
        att_url = os.path.relpath(
            att_file_path, start=os.path.dirname(ctx.output_file_path)
        )
        if os.path.sep != "/":
            att_url = att_url.replace("\\", "/")

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

    ctx.fout.write("</section>\n")


def chat_to_html(ctx: ExportContext, chat: Chat) -> None:
    if not chat.messages:
        return

    css_dir = posixpath.join("../..", ctx.css_url_sub_dir)
    ctx.fout.write(
        f"<!DOCTYPE html>\n"
        f'<html lang="en">\n'
        f"<head>\n"
        f'    <meta charset="UTF-8">\n'
        f'    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"    <title>Chat: {chat.get_title()}</title>\n"
        f'    <link rel="stylesheet" href="{css_dir}/water.css">\n'
        f'    <link rel="stylesheet" href="{css_dir}/extra.css">\n'
        f"</head>\n"
        f"<body>\n"
    )

    ctx.fout.write("<header>\n")
    chat_details_to_html(ctx.fout, chat)
    ctx.fout.write("</header>\n")

    ctx.fout.write("<main>\n")
    for msg in tqdm(chat.messages, desc="Exporting messages", leave=False):
        message_to_html(ctx, msg)
    ctx.fout.write("</main>\n")

    ctx.fout.write("</body></html>")


def write_chats_index(
    output_root_dir: str,
    css_url_sub_dir: str,
    chat_id_to_html_path: dict[str, str],
    chats: list[Chat],
):
    network_to_chats = {}
    for chat in chats:
        network_to_chats.setdefault(chat.details.network, []).append(chat)

    index_file_path = os.path.join(output_root_dir, "index.html")
    with open(index_file_path, "w", encoding="utf-8") as fp:
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
            f'    <link rel="stylesheet" href="{css_url_sub_dir}/water.css">\n'
            f"</head>\n"
            f"<body>\n"
            f"    <h1>Beeper Chats</h1>\n"
            f'    <div style="color: var(--text-muted);">'
            f'Exported from <span style="font-family: monospace;">{hostname}</span> on {now_date} at {now_time}'
            f"</div>\n"
        )
        fp.write("<ul>\n")
        for network_name, network_chats in sorted(
            network_to_chats.items(), key=lambda it: it[0].casefold()
        ):
            fp.write(f"<li>{network_name}\n")
            fp.write("<ul>\n")
            for chat in sorted(
                network_chats, key=lambda chat: chat.get_title().casefold()
            ):
                chat_html_path = chat_id_to_html_path[chat.details.id]
                chat_url = os.path.relpath(chat_html_path, start=output_root_dir)
                if os.path.sep != "/":
                    chat_url = chat_url.replace("/", os.path.sep)
                fp.write(f'<li><a href="{chat_url}">{chat.get_title()}</a></li>\n')
            fp.write("</ul>\n")
            fp.write("</li>\n")
        fp.write("</ul>\n")
        fp.write("</body></html>")


def copy_css_files(output_root_dir_path: str, data_dir_name: str) -> str:
    source_dir_path = os.path.join(os.path.dirname(__name__), "css")
    assert os.path.isdir(source_dir_path)
    css_url_sub_dir = posixpath.join(data_dir_name, "beepex")
    target_dir_path = os.path.join(output_root_dir_path, css_url_sub_dir)
    os.makedirs(target_dir_path, exist_ok=True)
    for file_name in ("water.css", "extra.css"):
        source_file_path = os.path.join(source_dir_path, file_name)
        target_file_path = os.path.join(target_dir_path, file_name)
        shutil.copy(source_file_path, target_file_path)
    return css_url_sub_dir


def write_html(
    chats: list[Chat], att_source_to_hydrated: dict[str, str], output_root_dir: str
) -> None:
    css_url_sub_dir = copy_css_files(output_root_dir, "media")
    chat_id_to_html_path = {}
    with tqdm(chats, desc="Exporting chats") as progress:
        for chat in progress:
            chat_title = sanitize_file_name(f"{chat.get_title()} ({chat.details.id})")
            progress.set_description(f'Exporting chat "{chat_title}"')
            chat.messages.sort(key=lambda chat: chat.sort_key)

            network_dir_name = sanitize_file_name(chat.details.network.lower())
            output_dir_path = os.path.join(output_root_dir, "chats", network_dir_name)
            os.makedirs(output_dir_path, exist_ok=True)

            html_file_path = os.path.join(output_dir_path, chat_title + ".html")
            attachment_dir_path = os.path.join(
                output_root_dir, "media", network_dir_name, chat_title
            )
            with open(html_file_path, "w", encoding="utf-8") as fp:
                context = ExportContext(
                    html_file_path,
                    fp,
                    attachment_dir_path,
                    att_source_to_hydrated,
                    css_url_sub_dir,
                )
                chat_to_html(context, chat)
            if chat.messages:
                mtime = chat.messages[-1].timestamp.astimezone().timestamp()
                os.utime(html_file_path, times=(mtime, mtime))

            chat_id = chat.details.id
            assert chat_id not in chat_id_to_html_path
            chat_id_to_html_path[chat_id] = html_file_path

    write_chats_index(output_root_dir, css_url_sub_dir, chat_id_to_html_path, chats)


def get_all_chats() -> list[Chat]:
    chat_id_to_details: dict[str, ChatDetails] = {}
    chat_id_to_messages: dict[str, list[Message]] = {}

    cursor = None
    with tqdm(desc="Gathering messages") as progress:
        while True:
            params = {"limit": 20}  # 20 is the cap per page
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
                chat_id_to_details[chat_id] = ChatDetails(details)

            for msg in data.get("items", []):
                chat_id_to_messages.setdefault(msg["chatID"], []).append(Message(msg))

            if not data.get("hasMore") or not data.get("oldestCursor"):
                break
            cursor = data["oldestCursor"]
            progress.update()

    chats = []
    for chat_id, details in chat_id_to_details.items():
        messages = chat_id_to_messages.get(chat_id, [])
        chats.append(Chat(details, messages))
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
    if not beeper_version_str:
        fatal("Can't get Beeper desktop version")
    beeper_version = version.parse(beeper_version_str)
    min_version = version.parse(BEEPER_MIN_VERSION)
    if beeper_version < min_version:
        fatal(
            f"Installed Beeper {beeper_version} is too old, version {min_version} is required."
        )


async def main():
    try:
        check_prerequisites()

        parser = argparse.ArgumentParser()
        parser.add_argument("output_root_dir")
        args = parser.parse_args()

        chats = get_all_chats()
        att_source_to_hydrated = await hydrate_attachments(chats)
        write_html(chats, att_source_to_hydrated, args.output_root_dir)
    except KeyboardInterrupt:
        fatal("Manually aborted")


if __name__ == "__main__":
    asyncio.run(main())
