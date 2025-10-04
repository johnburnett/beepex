# -*- coding: utf-8 -*-
# https://www.beeper.com/download/nightly/now
# https://developers.beeper.com/desktop-api/
# https://developers.beeper.com/desktop-api-reference/resources/$shared
# https://developers.beeper.com/desktop-api-reference/resources/chats#(resource)%20chats%20%3E%20(model)%20chat%20%3E%20(schema)
#
# - Show replies somehow?
#   - Will add linkedMessageID field to message
# - Old messages require scrolling back in the UI?
#   there is going to be two new endpoints:
#   - list-chats (no filters, only timestamp-based cursor)
#   - list-messages (for each chat, same timestamp based cursor, no other filter) for paginating everything
#     list-messages will try to load more messages from the network, so it'll be like scrolling back, but programmatically

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime
import html
import os
import posixpath
import re
import shutil
import socket
import sys
import typing

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

BEEPER_HOST_URL = "http://localhost:23373/"
BEEPER_ACCESS_TOKEN = os.environ["BEEPER_ACCESS_TOKEN"]
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
class ExportContext:
    output_file_path: str
    fout: typing.TextIO
    attachment_dir_path: str
    # Map attachment srcURL to local hydrated file:/// URL
    att_source_to_hydrated: dict[str, str]
    css_url_sub_dir: str


def fatal(msg):
    print(msg)
    sys.exit(1)


def sanitize_file_name(file_name):
    if file_name.casefold() in FILE_NAME_RESERVED_NAMES:
        file_name = file_name + "_"
    file_name = FILE_NAME_RESERVED_CHARS_RE.sub("", file_name)
    file_name = file_name.strip(" \t\n.")
    return file_name if file_name else "_"


def chat_details_to_html(fout, chat_details):
    fout.write('<section class="chat-header">\n')
    fout.write(f"<h1>{chat_details['title']}</h1>\n")
    fout.write("<details>\n")
    fout.write(
        f'<div><span class="chat-details-label">Network: </span><span>{chat_details.get("network")}</span></div>\n'
    )
    fout.write(
        f'<div><span class="chat-details-label">Account ID: </span><span>{chat_details.get("accountID")}</span></div>\n'
    )
    fout.write(
        f'<div><span class="chat-details-label">Chat ID: </span><span>{chat_details.get("id")}</span></div>\n'
    )
    fout.write('<div><span class="chat-details-label">Participants:</span></div>\n')
    parts = chat_details.get("participants", {}).get("items", [])
    names = [part.get("fullName", part.get("id")) for part in parts]
    for name in sorted(names, key=lambda it: it.casefold()):
        fout.write(f"<div>{name}</div>\n")
    fout.write("</details>\n")
    fout.write("</section>")


async def hydrate_attachment(url):
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


async def hydrate_all_attachments(urls):
    tasks = [hydrate_attachment(url) for url in urls]
    return await tqdm_asyncio.gather(
        *tasks, total=len(tasks), desc="Downloading attachments"
    )


def get_attachment_urls(msgs):
    urls = []
    for msg in msgs:
        for att in msg.get("attachments", []):
            urls.append(att["srcURL"])
    return urls


def archive_attachment(attachment_dir_path, att_source_to_hydrated, msg, att):
    source_url = att["srcURL"]
    hydrated_url = att_source_to_hydrated[source_url]
    source_file_path = hydrated_url[len("file:///") :]
    if os.path.sep != "/":
        source_file_path = source_file_path.replace("/", os.path.sep)
    os.makedirs(attachment_dir_path, exist_ok=True)
    target_file_name, target_file_ext = os.path.splitext(att["fileName"])
    target_file_name = (
        sanitize_file_name(f"{msg['timestamp']}_{target_file_name}") + target_file_ext
    )
    target_file_path = os.path.join(attachment_dir_path, target_file_name)
    if not os.path.exists(target_file_path):
        shutil.copy2(source_file_path, target_file_path)
    return target_file_path


def message_to_html(ctx: ExportContext, chat_details, msg):
    # from pprint import pformat
    # ctx.fout.write(f'<section><pre>{pformat(msg)}</pre></section>\n')
    # return

    sec_class = "msg-self" if msg.get("isSender") else "msg-them"
    message_id = msg["messageID"]
    ts_utc = datetime.fromisoformat(msg["timestamp"])
    ts_local = ts_utc.astimezone()
    ts_utc_str = ts_utc.strftime("%Y-%m-%d %H:%M:%S %Z")
    ts_local_str = ts_local.strftime("%Y-%m-%d %H:%M:%S %Z")
    ctx.fout.write(
        f'<section class="msg {sec_class}">'
        f'<div id="{message_id}" class="msg-header">'
        f'<span class="msg-contact-name">{msg["senderName"]}</span>'
        f'<span class="msg-datetime" title="{ts_utc_str}">{ts_local_str}</span>'
        f'<a class="permalink" title="Message {message_id}" href="#{message_id}">&#x1F517;&#xFE0E;'
        f"</a></div>\n"
    )

    if "text" in msg:
        msg_text = html.escape(msg["text"], quote=False)
        msg_text = msg_text.replace("\n", "<br>\n")
        msg_text = bleach.linkify(msg_text)
        ctx.fout.write(msg_text)

    for att in msg.get("attachments", []):
        att_file_path = archive_attachment(
            ctx.attachment_dir_path, ctx.att_source_to_hydrated, msg, att
        )
        att_url = os.path.relpath(
            att_file_path, start=os.path.dirname(ctx.output_file_path)
        )
        if os.path.sep != "/":
            att_url = att_url.replace("\\", "/")

        dim_attr = ""
        size = att.get("size", {})
        if "width" in size and "height" in size:
            dim_attr = f' width="{size["width"]}" height="{size["height"]}"'

        att_type = att.get("type")
        if att_type == "img":
            ctx.fout.write(
                f'<a href="{att_url}"><img loading="lazy"{dim_attr} src="{att_url}"/></a>\n'
            )
        elif att_type == "video":
            ctx.fout.write(
                f'<video controls loop playsinline{dim_attr} src="{att_url}"/>\n'
            )
        elif att_type == "audio":
            ctx.fout.write(f'<audio controls src="{att_url}"/>\n')

    ctx.fout.write("</section>\n")


def messages_to_html(ctx: ExportContext, chat_details, msgs):
    if not msgs:
        return

    chat_id = chat_details["id"]
    for msg in msgs:
        assert msg["chatID"] == chat_id

    css_dir = posixpath.join("../..", ctx.css_url_sub_dir)
    ctx.fout.write(
        f"<!DOCTYPE html>\n"
        f'<html lang="en">\n'
        f"<head>\n"
        f'    <meta charset="UTF-8">\n'
        f'    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
        f"    <title>Chat: {chat_details['title']}</title>\n"
        f'    <link rel="stylesheet" href="{css_dir}/water.css">\n'
        f'    <link rel="stylesheet" href="{css_dir}/extra.css">\n'
        f"</head>\n"
        f"<body>\n"
    )

    ctx.fout.write("<header>\n")
    chat_details_to_html(ctx.fout, chat_details)
    ctx.fout.write("</header>\n")

    ctx.fout.write("<main>\n")
    for msg in tqdm(msgs, desc="Exporting messages", leave=False):
        message_to_html(ctx, chat_details, msg)
    ctx.fout.write("</main>\n")

    ctx.fout.write("</body></html>")


def write_chats_index(output_root_dir, css_url_sub_dir, network_to_chats):
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
        for network_name, chat_to_file_path in sorted(
            network_to_chats.items(), key=lambda it: it[0].casefold()
        ):
            fp.write(f"<li>{network_name}\n")
            fp.write("<ul>\n")
            for chat_title, chat_file_path in sorted(
                chat_to_file_path.items(), key=lambda it: it[0].casefold()
            ):
                chat_url = os.path.relpath(chat_file_path, start=output_root_dir)
                if os.path.sep != "/":
                    chat_url = chat_url.replace("/", os.path.sep)
                fp.write(f'<li><a href="{chat_url}">{chat_title}</a></li>\n')
            fp.write("</ul>\n")
            fp.write("</li>\n")
        fp.write("</ul>\n")
        fp.write("</body></html>")


def copy_css_files(output_root_dir_path, data_dir_name):
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


def dump_html(data, att_source_to_hydrated, output_root_dir):
    msgs = data["items"]
    chat_to_messages = {}
    for msg in msgs:
        chat_to_messages.setdefault(msg["chatID"], []).append(msg)

    css_url_sub_dir = copy_css_files(output_root_dir, "media")

    chat_id_to_chat_details = data["chats"]

    network_to_chats = {}
    with tqdm(chat_to_messages.items(), desc="Exporting chats") as progress:
        for chat_id, msgs in progress:
            chat_details = chat_id_to_chat_details[chat_id]
            chat_title = sanitize_file_name(chat_details["title"])
            progress.set_description(f'Exporting chat "{chat_title}"')
            msgs.sort(key=lambda it: it["sortKey"])

            network_name = chat_details["network"]
            network_dir_name = sanitize_file_name(network_name.lower())
            output_dir_path = os.path.join(output_root_dir, "chats", network_dir_name)
            os.makedirs(output_dir_path, exist_ok=True)

            output_file_path = os.path.join(output_dir_path, chat_title + ".html")
            attachment_dir_path = os.path.join(
                output_root_dir, "media", network_dir_name, chat_title
            )
            with open(output_file_path, "w", encoding="utf-8") as fp:
                context = ExportContext(
                    output_file_path,
                    fp,
                    attachment_dir_path,
                    att_source_to_hydrated,
                    css_url_sub_dir,
                )
                messages_to_html(context, chat_details, msgs)

            chat_to_file_path = network_to_chats.setdefault(network_name, {})
            chat_to_file_path[chat_title] = output_file_path

    write_chats_index(output_root_dir, css_url_sub_dir, network_to_chats)


def get_all_messages():
    cursor = None
    all_data = {
        "items": [],
        "chats": {},
    }
    with tqdm(desc="Gathering Beeper messages") as progress:
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

            all_data["chats"].update(data.get("chats", {}))
            all_data["items"].extend(data.get("items", []))
            if not data.get("hasMore") or not data.get("oldestCursor"):
                break
            cursor = data["oldestCursor"]
            progress.update()
    return all_data


def check_beeper_version():
    try:
        resp = requests.get(
            f"{BEEPER_HOST_URL}/v0/search-messages",
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
    min_version = version.parse("4.1.244")
    if beeper_version < min_version:
        fatal(
            f"Installed Beeper {beeper_version} is too old, version {min_version} is required."
        )


async def main():
    try:
        check_beeper_version()

        parser = argparse.ArgumentParser()
        parser.add_argument("output_root_dir")
        args = parser.parse_args()

        data = get_all_messages()
        att_source_urls = get_attachment_urls(data["items"])
        att_hydrated_urls = await hydrate_all_attachments(att_source_urls)
        assert len(att_source_urls) == len(att_hydrated_urls)
        for url in att_hydrated_urls:
            assert url.startswith("file:///")
        att_source_to_hydrated = dict(zip(att_source_urls, att_hydrated_urls))
        dump_html(data, att_source_to_hydrated, args.output_root_dir)
    except KeyboardInterrupt:
        fatal("Manually aborted")


if __name__ == "__main__":
    asyncio.run(main())
