# https://developers.beeper.com/desktop-api/
# https://www.beeper.com/download/nightly/now
#
# - Old messages require scrolling back in the UI?

import argparse
from dataclasses import dataclass
from datetime import datetime
import html
import os
import posixpath
import re
import shutil
import sys
import textwrap
import typing

import bleach
from packaging import version
import requests
from rich import traceback
from tqdm import tqdm

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
    css_url_sub_dir: str
    self_id: str


def fatal(msg):
    print(msg)
    sys.exit(1)


def getHtmlHead(title, export_css_sub_dir):
    head = f'''<!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Chat: {title}</title>
        <link rel="stylesheet" href="{export_css_sub_dir}/water.css">
        <link rel="stylesheet" href="{export_css_sub_dir}/extra.css">
    </head>
    <body>
    '''
    return textwrap.dedent(head)


def getHtmlTail():
    return """
    </body>
    </html>
    """


def sanitize_file_name(file_name):
    if file_name.casefold() in FILE_NAME_RESERVED_NAMES:
        file_name = file_name + "_"
    file_name = FILE_NAME_RESERVED_CHARS_RE.sub("", file_name)
    file_name = file_name.strip(" \t\n.")
    return file_name if file_name else "_"


def chat_details_to_html(fout, chat_details):
    fout.write('<section class="chat-header">\n')
    fout.write(f"<h1>{chat_details['title']}</h1>\n")
    fout.write('<section class="chat-details">(')
    fout.write(
        f'<span class="chat-details-label">Network: </span><span>{chat_details.get("network")}</span>, '
    )
    fout.write(
        f'<span class="chat-details-label">Account ID: </span><span>{chat_details.get("accountID")}</span>, '
    )
    fout.write(
        f'<span class="chat-details-label">Chat ID: </span><span>{chat_details.get("id")}</span>'
    )
    fout.write(")</section>\n")
    fout.write("<h2>Participants</h2>\n")
    parts = chat_details.get("participants", {}).get("items", [])
    names = [part.get("fullName", part.get("id")) for part in parts]
    for name in sorted(names):
        fout.write(f"<div>{name}</div>\n")
    fout.write("</section>")


def download_attachment(attachment_dir_path, msg, att):
    source_url = att["srcURL"]
    if source_url.startswith("mxc://"):
        resp = requests.post(
            f"{BEEPER_HOST_URL}/v0/download-asset",
            headers=REQUEST_HEADERS,
            json={"url": source_url},
        )
        resp.raise_for_status()
        data = resp.json()
        source_url = data["srcURL"]
    assert source_url.startswith("file:///")
    source_file_path = source_url[len("file:///") :]
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

    sec_class = "msg-self" if ctx.self_id == msg.get("senderID") else "msg-them"
    message_id = msg["messageID"]
    ts_utc = datetime.fromisoformat(msg["timestamp"])
    ts_local = ts_utc.astimezone()
    ts_utc_str = ts_utc.strftime("%Y-%m-%d %H:%M:%S %Z")
    ts_local_str = ts_local.strftime("%Y-%m-%d %H:%M:%S %Z")
    ctx.fout.write(
        f'<section class="msg {sec_class}"><div id="{message_id}" class="msg-header"><span class="msg-contact-name">{msg["senderName"]}</span><span class="msg-datetime" title="{ts_utc_str}">{ts_local_str}</span><a class="permalink" title="Message {message_id}" href="#{message_id}">&#x1F517;&#xFE0E;</a></div>\n'
    )

    if "text" in msg:
        msg_text = html.escape(msg["text"], quote=False)
        msg_text = msg_text.replace("\n", "<br>\n")
        msg_text = bleach.linkify(msg_text)
        ctx.fout.write(msg_text)

    for att in msg.get("attachments", []):
        att_file_path = download_attachment(ctx.attachment_dir_path, msg, att)
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

    ctx.fout.write(
        getHtmlHead(chat_details["title"], posixpath.join("../..", ctx.css_url_sub_dir))
    )

    ctx.fout.write("<header>\n")
    chat_details_to_html(ctx.fout, chat_details)
    ctx.fout.write("</header>\n")

    ctx.fout.write("<main>\n")
    for msg in tqdm(msgs, desc="Exporting messages", leave=False):
        message_to_html(ctx, chat_details, msg)
    ctx.fout.write("</main>\n")

    ctx.fout.write(getHtmlTail())


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


def dump_html(data, output_root_dir):
    msgs = data["items"]
    chat_to_messages = {}
    for msg in msgs:
        chat_to_messages.setdefault(msg["chatID"], []).append(msg)

    css_url_sub_dir = copy_css_files(output_root_dir, "media")

    chat_id_to_chat_details = data["chats"]

    with tqdm(chat_to_messages.items(), desc="Exporting chats") as progress:
        for chat_id, msgs in progress:
            chat_details = chat_id_to_chat_details[chat_id]
            chat_title = sanitize_file_name(chat_details["title"])
            progress.set_description(f'Exporting chat "{chat_title}"')
            self_id = None
            for part in chat_details.get("participants", {}).get("items", []):
                if part.get("isSelf", False):
                    self_id = part.get("id")
            assert self_id
            msgs.sort(key=lambda it: it["sortKey"])

            network_dir_name = sanitize_file_name(chat_details["network"].lower())
            output_dir_path = os.path.join(output_root_dir, "chats", network_dir_name)
            os.makedirs(output_dir_path, exist_ok=True)

            output_file_path = os.path.join(output_dir_path, chat_title + ".html")
            attachment_dir_path = os.path.join(
                output_root_dir, "media", network_dir_name, chat_title
            )
            with open(output_file_path, "w", encoding="utf-8") as fp:
                context = ExportContext(
                    output_file_path, fp, attachment_dir_path, css_url_sub_dir, self_id
                )
                messages_to_html(context, chat_details, msgs)


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
    resp = requests.get(
        f"{BEEPER_HOST_URL}/v0/search-messages",
        headers=REQUEST_HEADERS,
    )
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


def main():
    check_beeper_version()

    parser = argparse.ArgumentParser()
    parser.add_argument("output_root_dir")
    args = parser.parse_args()

    data = get_all_messages()
    dump_html(data, args.output_root_dir)


if __name__ == "__main__":
    main()
