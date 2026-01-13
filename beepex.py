# -*- coding: utf-8 -*-
import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
import html
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import sys
import threading
from typing import no_type_check, NewType, NoReturn, TextIO, Union
import urllib.parse

from argparse_formatter import FlexiFormatter
from beeper_desktop_api import AsyncBeeperDesktop
from beeper_desktop_api.types import Attachment, Chat, Message, User
import bleach
from dotenv import load_dotenv
from packaging import version
from PIL import Image
import requests
from rich import traceback
from tqdm import tqdm
from tqdm.asyncio import tqdm_asyncio

__version__ = "dev"
try:
    from __version__ import __version__  # type: ignore
except ModuleNotFoundError:
    pass

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


@dataclass(frozen=True, kw_only=True)
class ExportPaths:
    # Map attachment src_url (which may not exist locally) to local hydrated file path
    att_source_to_hydrated: dict[str, Path | None]
    # Map attachment src_url to archived file path
    att_source_to_archived: dict[str, Path | None]
    # Set of src_urls that had thumbnails created for them
    src_urls_with_thumbs: set[str]
    resource_dir: Path
    chat_html_file: Path
    gallery_html_file: Path
    media_dir: Path
    thumb_dir: Path


AccountID = NewType("AccountID", str)
ChatID = NewType("ChatID", str)
UserID = NewType("UserID", str)


# fmt: off
class IncludeAccountSet(set): ...
class ExcludeAccountSet(set): ...
class IncludeChatSet(set): ...
class ExcludeChatSet(set): ...
IncludeExcludeSet = Union[IncludeAccountSet | ExcludeAccountSet | IncludeChatSet | ExcludeChatSet]
# fmt: on


# fmt: off
FILE_NAME_RESERVED_NAMES = {
    "aux", "con", "nul", "prn",
    "com0", "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt0", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
}
# fmt: on
FILE_NAME_RESERVED_CHARS_RE = re.compile(r'["*/:<>?\\|]')
CONFIG: Config | None = None
MAX_THUMB_DIM = 256
# png files are often screenshots with text, so keep the thumbnails larger for legibility
MAX_PNG_THUMB_DIM = 512


def cfg() -> Config:
    assert CONFIG
    return CONFIG


def init_cfg(args) -> None:
    global CONFIG
    assert CONFIG is None
    if args.token:
        access_token = args.token
    else:
        if args.env:
            env_file = args.env
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


def start_work_queue(*, num_threads=4):
    def worker_proc(work_queue):
        while True:
            proc, args, kwargs = work_queue.get()
            try:
                proc(*args, **kwargs)
            except Exception as ex:
                try:
                    while work_queue.get_nowait():
                        work_queue.task_done()
                except queue.Empty:
                    pass
                raise ex
            finally:
                work_queue.task_done()

    work_queue = queue.Queue()
    for ii in range(num_threads):
        th = threading.Thread(
            name="worker%d" % ii, target=worker_proc, args=(work_queue,)
        )
        th.daemon = True
        th.start()
    return work_queue


def filter_chat_ids(
    all_chat_ids: set[ChatID],
    chat_id_to_account_id: dict[ChatID, AccountID],
    include_exclude_sets: list[IncludeExcludeSet],
) -> set[ChatID]:
    if len(include_exclude_sets) == 0:
        return set(all_chat_ids)

    if isinstance(include_exclude_sets[0], (ExcludeAccountSet, ExcludeChatSet)):
        chat_ids = set(all_chat_ids)
    else:
        chat_ids = set()

    account_id_to_chat_ids = defaultdict(set)
    for chat_id, account_id in chat_id_to_account_id.items():
        account_id_to_chat_ids[account_id].add(chat_id)

    for ie_set in include_exclude_sets:
        if isinstance(ie_set, IncludeChatSet):
            for chat_id in ie_set:
                if chat_id not in all_chat_ids:
                    fatal(f'Unknown chat ID: "{chat_id}"')
            chat_ids.update(ie_set)
        elif isinstance(ie_set, ExcludeChatSet):
            chat_ids.difference_update(ie_set)
        elif isinstance(ie_set, IncludeAccountSet):
            for account_id in ie_set:
                account_chat_ids = account_id_to_chat_ids.get(account_id)
                if account_chat_ids is None:
                    fatal(f'Unknown account ID: "{account_id}"')
                else:
                    chat_ids.update(account_chat_ids)
        elif isinstance(ie_set, ExcludeAccountSet):
            for account_id in ie_set:
                account_chat_ids = account_id_to_chat_ids.get(account_id)
                if account_chat_ids is None:
                    fatal(f'Unknown account ID: "{account_id}"')
                else:
                    chat_ids.difference_update(account_chat_ids)
    return chat_ids


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
    chat: Chat, messages: list[Message], self_id: UserID, max_senders: int
) -> list[UserID]:
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
            chat, messages, UserID(self_user.id), max_senders_in_title
        )
        id_to_name: dict[UserID, str] = {
            UserID(user.id): str(user.full_name) for user in chat.participants.items
        }
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


info = print


def fatal(msg: str) -> NoReturn:
    print(msg)
    sys.exit(1)


HE = html.escape


def LQ(s):
    return urllib.parse.quote(html.escape(s))


def sanitize_file_name(file_name: str) -> str:
    if file_name.casefold() in FILE_NAME_RESERVED_NAMES:
        file_name = file_name + "_"
    file_name = FILE_NAME_RESERVED_CHARS_RE.sub("_", file_name)
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
    media_dir_path: Path,
    att_source_to_hydrated: dict[str, Path | None],
    time_sent: datetime,
    att: Attachment,
) -> Path | None:
    if not att.src_url:
        return None
    hydrated_file_path = att_source_to_hydrated[att.src_url]
    if not hydrated_file_path:
        return None
    time_sent_str = time_sent.strftime("%Y-%m-%d_%H-%M-%S")
    target_file_name, target_file_ext = os.path.splitext(att.file_name or "")
    target_file_name = (
        sanitize_file_name(f"{time_sent_str}_{target_file_name}") + target_file_ext
    )
    archived_file_path = media_dir_path / target_file_name
    mtime = time_sent.timestamp()
    if not archived_file_path.exists():
        shutil.copy(hydrated_file_path, archived_file_path)
        os.utime(archived_file_path, times=(mtime, mtime))
    return archived_file_path


def get_thumbnail_dim(media_file_path: Path) -> int | None:
    suffix = media_file_path.suffix.casefold()
    if suffix not in (".jpg", ".jpeg", ".png"):
        return None
    else:
        return MAX_PNG_THUMB_DIM if suffix == ".png" else MAX_THUMB_DIM


def get_thumbnail_file_path(media_file_path: Path, thumb_dir_path: Path) -> Path | None:
    max_dim = get_thumbnail_dim(media_file_path)
    if not max_dim:
        return None
    image = Image.open(media_file_path)
    if image.width <= max_dim and image.height <= max_dim:
        return None
    else:
        return thumb_dir_path / (media_file_path.stem + ".jpg")


def create_thumbnail(media_file_path: Path, thumb_file_path: Path):
    image = Image.open(media_file_path)
    if image.mode != "RGB":
        image = image.convert("RGB")
    max_dim = get_thumbnail_dim(media_file_path)
    assert isinstance(max_dim, int)
    image.thumbnail((max_dim, max_dim), Image.Resampling.LANCZOS, reducing_gap=2.0)
    image.save(thumb_file_path, quality="medium")


async def message_to_html(
    fout: TextIO, work_queue: queue.Queue, paths: ExportPaths, chat: Chat, msg: Message
) -> None:
    # from pprint import pformat
    # fout.write(f'<div><pre>{pformat(msg)}</pre></div>\n')
    # return

    sec_class = "msg-self" if msg.is_sender else "msg-them"
    ts_utc = msg.timestamp
    ts_local = ts_utc.astimezone()
    ts_utc_str = ts_utc.strftime("%Y-%m-%d %H:%M:%S %Z")
    ts_local_str = ts_local.strftime("%Y-%m-%d %H:%M:%S")
    replied_link = ""
    linked_message_id = getattr(msg, "linked_message_id", None)
    if linked_message_id:
        replied_link = f'    <a title="Reply to message {HE(linked_message_id)}" href="#{LQ(linked_message_id)}">&nbsp;(replied &#x2934;&#xFE0E;)</a>\n'
    fout.write(
        f'<div class="msg {sec_class}">\n'
        f'  <div id="{HE(msg.id)}" class="msg-header">\n'
        f'    <span class="msg-contact-name">{HE(msg.sender_name or "Unknown")}</span>\n'
        f"{replied_link}"
        f'    <span class="msg-datetime" title="{ts_utc_str}">{ts_local_str}</span>\n'
        f'    <a title="Message {HE(msg.id)}" href="#{HE(msg.id)}">&#x1F517;&#xFE0E;</a>\n'
        f"  </div>\n"
        f"  <div>\n"
    )

    if msg.text:
        msg_text = msg.text
        msg_text = html.escape(msg_text, quote=False)
        msg_text = msg_text.replace("\n", "<br>\n")
        msg_text = bleach.linkify(msg_text)
        fout.write(msg_text)

    for att in msg.attachments if msg.attachments else []:
        archived_file_path = archive_attachment(
            paths.media_dir, paths.att_source_to_hydrated, ts_local, att
        )
        if att.src_url:
            paths.att_source_to_archived[att.src_url] = archived_file_path
        if archived_file_path:
            thumb_file_path = get_thumbnail_file_path(
                archived_file_path, paths.thumb_dir
            )
            if thumb_file_path:
                if att.src_url:
                    paths.src_urls_with_thumbs.add(att.src_url)
                work_queue.put(
                    (create_thumbnail, (archived_file_path, thumb_file_path), {})
                )

            att_url = LQ(
                archived_file_path.relative_to(
                    paths.chat_html_file.parent, walk_up=True
                ).as_posix()
            )
            thumb_url = (
                LQ(
                    thumb_file_path.relative_to(
                        paths.chat_html_file.parent, walk_up=True
                    ).as_posix()
                )
                if thumb_file_path
                else att_url
            )

            dim_attr = (
                f' width="{att.size.width}" height="{att.size.height}"'
                if att.size
                else ""
            )
            if att.type == "img":
                fout.write(
                    f'<a href="{att_url}"><img loading="lazy"{dim_attr} src="{thumb_url}" alt=""></a>\n'
                )
            elif att.type == "video":
                fout.write(
                    f'<video controls loop playsinline{dim_attr} src="{att_url}"></video>\n'
                )
            elif att.type == "audio":
                fout.write(f'<audio controls src="{att_url}"/>\n')
        else:
            fout.write(
                f'<span class="error">&#x26A0;&#xFE0E; Missing Attachment: "{att.src_url}"</span>'
            )

    fout.write("\n  </div>\n")

    if msg.reactions:
        user_id_to_full_name: dict[UserID, str] = {}
        for user in chat.participants.items:
            user_id_to_full_name[UserID(user.id)] = str(user.full_name)
        fout.write('  <span class="reactions">\n')
        keys_to_names = defaultdict(list)
        for reaction in msg.reactions:
            name = str(
                user_id_to_full_name.get(
                    UserID(reaction.participant_id), reaction.participant_id
                )
            )
            keys_to_names[reaction.reaction_key].append(name)
        for key, names in sorted(keys_to_names.items()):
            tooltip = f"{key}&#10;" + "&#10;".join([HE(name) for name in sorted(names)])
            fout.write(f'    <span title="{tooltip}">{HE(key)}</span>\n')
        fout.write("  </span>")

    fout.write("</div>\n")


async def write_chat_html(
    fout: TextIO,
    work_queue: queue.Queue,
    paths: ExportPaths,
    chat_title: str,
    chat: Chat,
    messages: list[Message],
) -> None:
    resource_dir_rel = LQ(
        paths.resource_dir.relative_to(
            paths.chat_html_file.parent, walk_up=True
        ).as_posix()
    )
    gallery_html_file_rel = LQ(
        paths.gallery_html_file.relative_to(
            paths.chat_html_file.parent, walk_up=True
        ).as_posix()
    )
    fout.write(
        f"<!DOCTYPE html>\n"
        f'<html lang="en">\n'
        f"<head>\n"
        f'  <meta charset="utf-8">\n'
        f'  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"  <title>Chat: {chat_title}</title>\n"
        f'  <link rel="stylesheet" href="{resource_dir_rel}/water.css">\n'
        f'  <link rel="stylesheet" href="{resource_dir_rel}/chat.css">\n'
        f"</head>\n"
        f"<body>\n"
    )

    names = [get_user_name(user) for user in chat.participants.items]
    fout.write(
        f"<header>\n"
        f'  <div class="chat-header">\n'
        f'    <div class="chat-header-title">\n'
        f"      <h1>{chat_title}</h1>\n"
        f'      <a class="gallery-link" href="{gallery_html_file_rel}">&#x25A6; Media Gallery</a>\n'
        f"    </div>\n"
        f"    <details><summary>Details</summary>\n"
        f'      <div><span class="chat-details-label">Account ID: </span>{HE(chat.account_id)}</div>\n'
        f'      <div><span class="chat-details-label">Chat ID: </span>{HE(chat.id)}</div>\n'
        f'      <div><span class="chat-details-label">Message Count: </span>{len(messages)}</div>\n'
        f'      <div><span class="chat-details-label">Participants: </span>{len(names)}</div>\n'
        f"      <ul>\n"
    )
    for name in sorted(names, key=lambda it: it.casefold()):
        fout.write(f"        <li>{HE(name)}</li>\n")
    fout.write("      </ul>\n    </details>\n  </div></header>\n")

    fout.write("<main>\n")
    for msg in tqdm(messages, desc="Writing chat messages", leave=False):
        await message_to_html(fout, work_queue, paths, chat, msg)
    fout.write("</main>\n")

    fout.write("</body></html>\n")


async def write_gallery_html(
    fout: TextIO,
    paths: ExportPaths,
    chat_title: str,
    chat: Chat,
    messages: list[Message],
) -> None:
    chat_file_rel = LQ(
        paths.chat_html_file.relative_to(
            paths.gallery_html_file.parent, walk_up=True
        ).as_posix()
    )
    resource_dir_rel = LQ(
        paths.resource_dir.relative_to(
            paths.gallery_html_file.parent, walk_up=True
        ).as_posix()
    )
    fout.write(
        f"<!DOCTYPE html>\n"
        f'<html lang="en">\n'
        f"<head>\n"
        f'  <meta charset="utf-8" />\n'
        f'  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"  <title>Gallery: {chat_title}</title>\n"
        f'  <link rel="stylesheet" href="{resource_dir_rel}/gallery.css">\n'
        f"</head>\n"
        f"<body>\n"
        f"  <header>\n"
        f'    <div class="wrap">\n'
        f"      <h1>{chat_title}</h1>\n"
        f'      <div id="search-bar">\n'
        f'        <input id="search-text" type="search" placeholder="Filter..." />\n'
        f'        <div id="search-count"></div>\n'
        f"      </div>\n"
        f"    </div>\n"
        f"  </header>\n"
        f"  <main>\n"
        f'    <div class="wrap">\n'
        f'      <div id="gallery-grid"></div>\n'
        f"    </div>\n"
        f"  </main>\n"
        f"  <script>\n"
    )
    media_dir_rel = paths.media_dir.relative_to(
        paths.gallery_html_file.parent, walk_up=True
    ).as_posix()
    thumb_dir_rel = paths.thumb_dir.relative_to(
        paths.gallery_html_file.parent, walk_up=True
    ).as_posix()
    fout.write(f'    window.CHAT_FILE_URL = "{chat_file_rel}";\n')
    fout.write(f'    window.MEDIA_PREFIX = "{media_dir_rel}";\n')
    fout.write(f'    window.THUMB_PREFIX = "{thumb_dir_rel}";\n')
    fout.write("    window.MEDIA = [\n")
    # BBUG-3: works around multiple src_urls resolving to the same archive file path
    seen_archive_urls = set()
    for msg in messages:
        for att in msg.attachments if msg.attachments else []:
            if att.src_url and att.src_url not in seen_archive_urls:
                seen_archive_urls.add(att.src_url)
                archived_file_path = paths.att_source_to_archived[att.src_url]
                has_thumb = att.src_url in paths.src_urls_with_thumbs
                if archived_file_path:
                    fout.write(
                        f'["{os.path.basename(archived_file_path)}","{msg.id}",{1 if has_thumb else 0}],\n'
                    )
    fout.write(
        f"    ]\n"
        f"  </script>\n"
        f'  <script src="{resource_dir_rel}/gallery.js"></script>\n'
        f"</body>\n"
        f"</html>\n"
    )


def write_chats_index(
    output_root_dir: Path,
    resource_dir_path: Path,
    export_time: datetime,
    export_duration: timedelta,
    chat_id_to_html_path: dict[ChatID, Path],
    chat_id_to_title: dict[ChatID, str],
    chat_ids: set[ChatID],
    chat_id_to_account_id: dict[ChatID, AccountID],
    account_id_to_name: dict[AccountID, str],
) -> Path:
    index_file_path = output_root_dir / "index.html"
    with open(index_file_path, "w", encoding="utf-8") as fp:
        resource_dir_rel = LQ(resource_dir_path.relative_to(output_root_dir).as_posix())
        hostname = socket.gethostname()
        export_ymd = export_time.strftime("%Y-%m-%d")
        export_hms = export_time.strftime("%H:%M:%S")
        fp.write(
            f"<!DOCTYPE html>\n"
            f'<html lang="en">\n'
            f"<head>\n"
            f'  <meta charset="utf-8">\n'
            f'  <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
            f"  <title>Beeper Chats</title>\n"
            f'  <link rel="stylesheet" href="{resource_dir_rel}/water.css">\n'
            f'  <link rel="stylesheet" href="{resource_dir_rel}/chat.css">\n'
            f"</head>\n"
            f"<body>\n"
            f"<header>\n"
            f'  <div class="chat-header">\n'
            f"    <h1>Beeper Chats</h1>\n"
            f"    <details><summary>Details</summary>\n"
            f'      <div><span class="chat-details-label">beepex Version: </span>{HE(str(__version__))}</div>\n'
            f'      <div><span class="chat-details-label">Export Host: </span>{HE(hostname)}</div>\n'
            f'      <div><span class="chat-details-label">Export Date: </span>{HE(export_ymd)}</div>\n'
            f'      <div><span class="chat-details-label">Export Time: </span>{HE(export_hms)}</div>\n'
            f'      <div><span class="chat-details-label">Export Duration: </span>{HE(str(export_duration))}</div>\n'
            f"    </details>\n"
            f"  </div>\n"
            f"</header>\n"
            f"<main>\n"
        )

        account_id_to_chat_ids = defaultdict(list)
        for chat_id in chat_ids:
            account_id = chat_id_to_account_id[chat_id]
            account_id_to_chat_ids[account_id].append(chat_id)

        fp.write("  <ul>\n")
        for account_id, account_chat_ids in sorted(
            account_id_to_chat_ids.items(),
            key=lambda it: account_id_to_name[it[0]].casefold(),
        ):
            fp.write(f"    <li>{account_id_to_name[account_id]}\n")
            fp.write("      <ul>\n")
            for chat_id in sorted(
                account_chat_ids, key=lambda cid: chat_id_to_title[cid].casefold()
            ):
                chat_html_path = chat_id_to_html_path[chat_id]
                chat_url = chat_html_path.relative_to(output_root_dir)
                fp.write(
                    f'        <li><a href="{LQ(chat_url.as_posix())}">{HE(chat_id_to_title[chat_id])}</a></li>\n'
                )
            fp.write("      </ul>\n")
            fp.write("    </li>\n")
        fp.write("  </ul>\n</main>\n</body></html>\n")
    return index_file_path


def copy_resource_files(target_dir_path: Path) -> Path:
    source_dir_path = Path(__file__).parent / "resources"
    assert source_dir_path.is_dir()
    target_dir_path.mkdir(parents=True, exist_ok=True)
    for source_item in source_dir_path.iterdir():
        if source_item.is_file():
            target_file_path = target_dir_path / source_item.name
            shutil.copy(source_item, target_file_path)
    return target_dir_path


async def export_chat(
    client: AsyncBeeperDesktop,
    work_queue: queue.Queue,
    output_root_dir: Path,
    resource_dir_path: Path,
    chat_id: ChatID,
) -> tuple[str, Path]:
    chat = await client.chats.retrieve(chat_id)
    messages = []
    # BBUG-1: seen_ids and sorting by timestamp and not sort_key is to work around
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
    chat_file_name = sanitize_file_name(chat.id)
    account_dir_name = sanitize_file_name(chat.account_id.lower())
    chats_dir_path = output_root_dir / "chat" / account_dir_name
    chats_dir_path.mkdir(parents=True, exist_ok=True)
    galleries_dir_path = output_root_dir / "gallery" / account_dir_name
    galleries_dir_path.mkdir(parents=True, exist_ok=True)
    media_dir_path = (
        output_root_dir / "media" / "full" / account_dir_name / chat_file_name
    )
    media_dir_path.mkdir(parents=True, exist_ok=True)
    thumb_dir_path = (
        output_root_dir / "media" / "thumb" / account_dir_name / chat_file_name
    )
    thumb_dir_path.mkdir(parents=True, exist_ok=True)

    paths = ExportPaths(
        att_source_to_hydrated=await hydrate_chat_attachments(client, chat, messages),
        att_source_to_archived={},
        src_urls_with_thumbs=set(),
        resource_dir=resource_dir_path,
        chat_html_file=chats_dir_path / (chat_file_name + ".html"),
        gallery_html_file=galleries_dir_path / (chat_file_name + ".html"),
        media_dir=media_dir_path,
        thumb_dir=thumb_dir_path,
    )
    with open(paths.chat_html_file, "w", encoding="utf-8") as fp:
        await write_chat_html(fp, work_queue, paths, chat_title, chat, messages)
    assert len(paths.att_source_to_hydrated) == len(paths.att_source_to_archived)

    with open(paths.gallery_html_file, "w", encoding="utf-8") as fp:
        await write_gallery_html(fp, paths, chat_title, chat, messages)

    if messages:
        mtime = messages[-1].timestamp.astimezone().timestamp()
        os.utime(paths.chat_html_file, times=(mtime, mtime))

    return chat_title, paths.chat_html_file


async def export_chats(
    client: AsyncBeeperDesktop,
    output_root_dir: Path,
    include_exclude_sets: list[IncludeExcludeSet],
) -> Path:
    info(f'Exporting chats to "{output_root_dir}"')
    time_start = datetime.now()

    resource_dir_path = copy_resource_files(output_root_dir / "media/beepex")

    # Chats returned by list don't currently have all info associated with
    # them (e.g. participants list is truncated), so using this just to get
    # the chat IDs, to be filled out with individual chats.retrieve(id) calls.
    all_chat_ids = set()
    chat_id_to_account_id = dict()
    account_id_to_name: dict[AccountID, str] = dict()
    async for chat in client.chats.list():
        all_chat_ids.add(chat.id)
        chat_id_to_account_id[chat.id] = chat.account_id
        account_id_to_name[AccountID(chat.account_id)] = str(chat.network)
    chat_ids = filter_chat_ids(
        all_chat_ids, chat_id_to_account_id, include_exclude_sets
    )

    chat_id_to_title = {}
    chat_id_to_html_path = {}
    with tqdm(chat_ids, leave=False) as progress:
        work_queue = start_work_queue()
        for chat_id in progress:
            progress.set_description(f'Chat "{chat_id}"')
            chat_title, html_path = await export_chat(
                client, work_queue, output_root_dir, resource_dir_path, chat_id
            )
            chat_id_to_title[chat_id] = chat_title
            chat_id_to_html_path[chat_id] = html_path
        progress.set_description("Finishing thumbnail creation")
        work_queue.join()

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
        chat_ids,
        chat_id_to_account_id,
        account_id_to_name,
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
    index_html_path = await export_chats(client, output_root_dir, [])
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


class IncludeExcludeSetArg(argparse.Action):
    arg_to_type = {
        "--include_account_ids": IncludeAccountSet,
        "--exclude_account_ids": ExcludeAccountSet,
        "--include_chat_ids": IncludeChatSet,
        "--exclude_chat_ids": ExcludeChatSet,
    }

    def __call__(self, parser, namespace, values, option_string=None):
        dest_list = getattr(namespace, self.dest, None)
        if dest_list is None:
            dest_list = []
            setattr(namespace, self.dest, dest_list)
        assert option_string in self.arg_to_type
        set_type = self.arg_to_type[option_string]
        dest_list.append(set_type(values))

    def format_usage(self):
        return self.option_strings[0]


async def main():
    parser = argparse.ArgumentParser(
        prog="beepex",
        formatter_class=FlexiFormatter,
        epilog="""
The include/exclude arguments are processed in the order given, and may be used multiple times.  The starting set of chats to include depends upon the first include/exclude argument that is used:
- If the first is an "include_" type, the include/excludes are "building up" the set of chat IDs from nothing.
- If the first is an "exclude_" type, the include/excludes are "pruning down" the set of chat IDs from all possible chats.
- In either case, subsequent includes can re-add chats that were previously excluded, and vice-versa.
""",
    )
    parser.add_argument("output_root_dir", type=Path)
    parser.add_argument("-v", "--version", action="version", version=__version__)
    parser.add_argument(
        "--token",
        help="Beeper Desktop API access token.  If not provided, uses the BEEPER_ACCESS_TOKEN environment variable, potentially read from a .env file if it is next to the beepex executable.",
    )
    parser.add_argument(
        "--env",
        type=Path,
        help="Path to an env file that contains a definition of the BEEPER_ACCESS_TOKEN environment variable.",
    )
    parser.add_argument("--create_example", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--include_account_ids",
        dest="include_exclude_sets",
        action=IncludeExcludeSetArg,
        default=[],
        nargs="+",
        metavar="AccountID",
    )
    parser.add_argument(
        "--exclude_account_ids",
        dest="include_exclude_sets",
        action=IncludeExcludeSetArg,
        default=[],
        nargs="+",
        metavar="AccountID",
    )
    parser.add_argument(
        "--include_chat_ids",
        dest="include_exclude_sets",
        action=IncludeExcludeSetArg,
        default=[],
        nargs="+",
        metavar="ChatID",
    )
    parser.add_argument(
        "--exclude_chat_ids",
        dest="include_exclude_sets",
        action=IncludeExcludeSetArg,
        default=[],
        nargs="+",
        metavar="ChatID",
    )
    args = parser.parse_args()

    init_cfg(args)
    # BBUG-2
    # check_beeper_version()

    if args.create_example:
        await create_example(args.output_root_dir)
    else:
        client = AsyncBeeperDesktop(access_token=cfg().access_token)
        await export_chats(client, args.output_root_dir, args.include_exclude_sets)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        fatal("Manually aborted")
