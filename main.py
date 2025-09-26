#!/usr/bin/env -S uv --quiet run --env-file .env --script
# /// script
# dependencies = [
#     "rich",
#     "requests",
# ]
# ///
# - Old messages require scrolling back in the UI?
# - Message.text is truncated ("insid... [+78 chars]")
import html
import json
import os
import re
import shutil
import sys

import requests
from rich import traceback

traceback.install(show_locals=True, locals_max_length=3, locals_max_string=148, locals_hide_dunder=False, width=160)

BEEPER_HOST_URL = 'http://localhost:23373/'
BEEPER_ACCESS_TOKEN = os.environ['BEEPER_ACCESS_TOKEN']
REQUEST_HEADERS = {'Authorization': f'Bearer {BEEPER_ACCESS_TOKEN}'}

FILE_NAME_RESERVED_NAMES = {
    'aux', 'con', 'nul', 'prn',
    'com0', 'com1', 'com2', 'com3', 'com4', 'com5', 'com6', 'com7', 'com8', 'com9',
    'lpt0', 'lpt1', 'lpt2', 'lpt3', 'lpt4', 'lpt5', 'lpt6', 'lpt7', 'lpt8', 'lpt9',
}
FILE_NAME_RESERVED_CHARS_RE = re.compile(r'["*/:<>?\\|]')

HTML_HEAD = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Chat</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/water.css@2/out/water.css">
    <style>
        section {
            margin-bottom: 1rem;
            width: 66%;
            background-color: #3b3b3bff;
            padding: 0.5rem;
            border-radius: 5px;;
        }
        .you {
            margin-left: 33%;
            background-color: #195feeff;
        }
        span {
            font-size: 90%;
            display: flex;
            justify-content: end;
        }
    </style>
</head>
<body>
'''

HTML_TAIL = '''
</body>
</html>
'''

def sanitize_filename(name):
    if name.casefold() in FILE_NAME_RESERVED_NAMES:
        name = name + '_'
    name = FILE_NAME_RESERVED_CHARS_RE.sub('', name)
    name = name.strip(' \t\n.')
    return name if name else '_'


def chat_details_to_html(fout, chat_details):
    fout.write(f'<dl>\n')
    fout.write(f'<dt>title</dt><dd>{chat_details["title"]}</dd>\n')
    fout.write(f'</dl>\n')
    fout.write(f'<dt>accountID</dt><dd>{chat_details.get("accountID")}</dd>\n')
    fout.write(f'<dt>network</dt><dd>{chat_details.get("network")}</dd>\n')
    fout.write(f'<dt>chatID</dt><dd>{chat_details.get("id")}</dd>\n')
    fout.write(f'<dt>participants</dt><dd>\n')
    fout.write(f'<ul>\n')
    for part in chat_details.get('participants', {}).get('items', []):
        fout.write(f'  <li>{part.get("fullName", part.get("id", "Unknown"))}</li>\n')
    fout.write(f'</ul>\n')
    fout.write(f'</dd>\n')
    fout.write(f'</dl>\n')


def download_attachment(attachment_dir_path, chat_details, item, att):
    source_url = att['srcURL']
    if source_url.startswith('mxc://'):
        resp = requests.post(f'{BEEPER_HOST_URL}/v0/download-asset', headers=REQUEST_HEADERS, json={'url': source_url})
        resp.raise_for_status()
        data = resp.json()
        source_url = data['srcURL']

    assert source_url.startswith('file:///')
    source_path = source_url[len('file:///'):]
    if os.path.sep != '/':
        source_path = source_path.replace('/', os.path.sep)
    os.makedirs(attachment_dir_path, exist_ok=True)
    target_path = os.path.join(attachment_dir_path, os.path.basename(source_path))
    shutil.copy2(source_path, target_path)
    if os.path.sep != '/':
        target_path = target_path.replace(os.path.sep, '/')
    target_path = 'file:///' + target_path
    return target_path


def message_to_html(fout, chat_details, msg, self_id, attachment_dir_path):
    # from pprint import pformat
    # fout.write(f'<section><pre>{pformat(msg)}</pre></section>\n')
    # return

    sec_class = 'you' if self_id == msg.get('senderID') else 'them'
    fout.write(f'<section class="{sec_class}">{msg["senderName"]}:<br>\n')
    if 'text' in msg:
        msg_text = html.escape(msg['text'], quote=False)
        fout.write(msg_text)
    for att in msg.get('attachments', []):
        # fout.write(f'<div>Attachment: type="{att["type"]}", fileSize={att["fileSize"]}, fileName="{att["fileName"]}"</div>\n')
        att_url = download_attachment(attachment_dir_path, chat_details, msg, att)

        dim_attr = ''
        size = att.get('size', {})
        if 'width' in size and 'height' in size:
            dim_attr = f' width="{size["width"]}" height="{size["height"]}"'

        att_type = att.get('type')
        if att_type == 'img':
            fout.write(f'<img loading="lazy"{dim_attr} src="{att_url}"/>\n')
        elif att_type == 'video':
            fout.write(f'<video controls loop playsinline{dim_attr} src="{att_url}"/>\n')
        elif att_type == 'audio':
            fout.write(f'<audio controls src="{att_url}"/>\n')
    fout.write(f'<span>{msg["timestamp"]}</span></section>\n')


def messages_to_html(fout, chat_details, items, attachment_dir_path):
    if not items:
        return

    chat_id = chat_details['id']
    for item in items:
        assert item['chatID'] == chat_id

    self_id = None
    for part in chat_details.get('participants', {}).get('items', []):
        if part.get('isSelf', False):
            self_id = part.get('id')

    fout.write(HTML_HEAD)

    fout.write('<header>\n')
    chat_details_to_html(fout, chat_details)
    fout.write('</header>\n')

    fout.write('<main>\n')
    for item in items:
        message_to_html(fout, chat_details, item, self_id, attachment_dir_path)
    fout.write('</main>\n')

    fout.write(HTML_TAIL)


def dump_data(data, output_root_dir):
    chat_to_messages = {}
    for item in data['items']:
        chat_to_messages.setdefault(item['chatID'], []).append(item)

    chat_id_to_chat_details = data['chats']

    chat_index = 0
    for chat_id, items in chat_to_messages.items():
        chat_details = chat_id_to_chat_details[chat_id]
        items.sort(key=lambda it: it['sortKey'])

        output_dir_name = sanitize_filename(chat_details['title'])
        output_dir_path = os.path.join(output_root_dir, chat_details['network'].lower(), output_dir_name)
        os.makedirs(output_dir_path, exist_ok=True)

        attachment_dir_path = os.path.join(output_dir_path, 'attachments')

        output_file_path = os.path.join(output_dir_path, 'chat.html')
        with open(output_file_path, 'w', encoding='utf-8') as fp:

            messages_to_html(fp, chat_details, items, attachment_dir_path)
        chat_index += 1


def get_all_messages():
    cursor = None
    all_data = {
        'items': [],
        'chats': {},
    }
    while True:
        params = {"limit": 20}  # 20 is the cap per page
        if cursor:
            params["cursor"] = cursor
            params["direction"] = "before"

        resp = requests.get(f'{BEEPER_HOST_URL}/v0/search-messages', headers=REQUEST_HEADERS, params=params)
        resp.raise_for_status()
        data = resp.json()

        all_data['chats'].update(data.get('chats', {}))
        all_data['items'].extend(data.get('items', []))
        # print(f"Fetched {len(items)} messages (total {len(all_messages)})")
        if not data.get("hasMore") or not data.get("oldestCursor"):
            break
        cursor = data["oldestCursor"]
    return all_data


def main():
    output_root_dir = sys.argv[1]
    data = get_all_messages()
    dump_data(data, output_root_dir)


if __name__ == '__main__':
    main()
