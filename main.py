#!/usr/bin/env -S uv --quiet run --env-file .env --script
# /// script
# dependencies = [
#     "rich",
#     "requests",
# ]
# ///
# https://developers.beeper.com/desktop-api/
# https://www.beeper.com/download/nightly/now
#
# - Old messages require scrolling back in the UI?

from dataclasses import dataclass
import html
import json
import os
import posixpath
import re
import shutil
import sys
import textwrap
import typing

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

@dataclass
class ExportContext:
    fout: typing.TextIO
    self_id: str
    attachment_dir_path: str
    css_url_sub_dir: str

def getHtmlHead(exportCssSubDir):
    head = f'''<!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Chat</title>
        <link rel="stylesheet" href="{exportCssSubDir}/water.css">
        <link rel="stylesheet" href="{exportCssSubDir}/extra.css">
    </head>
    <body>
    '''
    return textwrap.dedent(head)

def getHtmlTail():
    return '''
    </body>
    </html>
    '''

def sanitize_file_name(file_name):
    if file_name.casefold() in FILE_NAME_RESERVED_NAMES:
        file_name = file_name + '_'
    file_name = FILE_NAME_RESERVED_CHARS_RE.sub('', file_name)
    file_name = file_name.strip(' \t\n.')
    return file_name if file_name else '_'

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
    source_file_path = source_url[len('file:///'):]
    if os.path.sep != '/':
        source_file_path = source_file_path.replace('/', os.path.sep)
    os.makedirs(attachment_dir_path, exist_ok=True)
    target_file_name = sanitize_file_name(os.path.basename(source_file_path))
    target_file_path = os.path.join(attachment_dir_path, target_file_name)
    if not os.path.exists(target_file_path):
        shutil.copy2(source_file_path, target_file_path)
    if os.path.sep != '/':
        target_file_path = target_file_path.replace(os.path.sep, '/')
    target_file_path = 'file:///' + target_file_path
    return target_file_path


def message_to_html(ctx: ExportContext, chat_details, msg):
    # from pprint import pformat
    # ctx.fout.write(f'<section><pre>{pformat(msg)}</pre></section>\n')
    # return

    sec_class = 'you' if self_id == msg.get('senderID') else 'them'
    fout.write(f'<section class="{sec_class}">{msg["senderName"]}:<br>\n')
    if 'text' in msg:
        msg_text = html.escape(msg['text'], quote=False)
        ctx.fout.write(msg_text)
    for att in msg.get('attachments', []):
        att_url = download_attachment(ctx.attachment_dir_path, chat_details, msg, att)

        dim_attr = ''
        size = att.get('size', {})
        if 'width' in size and 'height' in size:
            dim_attr = f' width="{size["width"]}" height="{size["height"]}"'

        att_type = att.get('type')
        if att_type == 'img':
            ctx.fout.write(f'<img loading="lazy"{dim_attr} src="{att_url}"/>\n')
        elif att_type == 'video':
            ctx.fout.write(f'<video controls loop playsinline{dim_attr} src="{att_url}"/>\n')
        elif att_type == 'audio':
            ctx.fout.write(f'<audio controls src="{att_url}"/>\n')
    ctx.fout.write(f'<span>{msg["timestamp"]}</span></section>\n')


def messages_to_html(ctx: ExportContext, chat_details, msgs):
    if not msgs:
        return

    chat_id = chat_details['id']
    for msg in msgs:
        assert msg['chatID'] == chat_id

    ctx.fout.write(getHtmlHead(posixpath.join('../..', ctx.css_url_sub_dir)))

    ctx.fout.write('<header>\n')
    chat_details_to_html(ctx.fout, chat_details)
    ctx.fout.write('</header>\n')

    ctx.fout.write('<main>\n')
    for msg in msgs:
        message_to_html(ctx, chat_details, msg)
    ctx.fout.write('</main>\n')

    ctx.fout.write(getHtmlTail())


def copy_css_files(output_root_dir_path, data_dir_name):
    source_dir_path = os.path.join(os.path.dirname(__name__), 'css')
    assert os.path.isdir(source_dir_path)
    css_url_sub_dir = posixpath.join(data_dir_name, 'css') # relative to root output dir
    target_dir_path = os.path.join(output_root_dir_path, css_url_sub_dir)
    os.makedirs(target_dir_path, exist_ok=True)
    for file_name in ('water.css', 'extra.css'):
        source_file_path = os.path.join(source_dir_path, file_name)
        target_file_path = os.path.join(target_dir_path, file_name)
        shutil.copy(source_file_path, target_file_path)
    return css_url_sub_dir


def dump_data(data, output_root_dir):
    msgs = data['items']
    chat_to_messages = {}
    for msg in msgs:
        chat_to_messages.setdefault(msg['chatID'], []).append(msg)

    data_dir_name = '_data'
    css_url_sub_dir = copy_css_files(output_root_dir, data_dir_name)

    chat_id_to_chat_details = data['chats']

    chat_index = 0
    for chat_id, msgs in chat_to_messages.items():
        chat_details = chat_id_to_chat_details[chat_id]
        self_id = None
        for part in chat_details.get('participants', {}).get('items', []):
            if part.get('isSelf', False):
                self_id = part.get('id')
        assert self_id
        msgs.sort(key=lambda it: it['sortKey'])

        network_dir_name = sanitize_file_name(chat_details['network'].lower())
        output_dir_name = sanitize_file_name(chat_details['title'])
        output_dir_path = os.path.join(output_root_dir, network_dir_name, output_dir_name)
        os.makedirs(output_dir_path, exist_ok=True)

        attachment_dir_path = os.path.join(output_dir_path, 'attachments')

        output_file_path = os.path.join(output_dir_path, 'chat.html')
        with open(output_file_path, 'w', encoding='utf-8') as fp:
            context = ExportContext(fp, self_id, attachment_dir_path, css_url_sub_dir)
            messages_to_html(context, chat_details, msgs)
        chat_index += 1


def get_all_messages():
    cursor = None
    all_data = {
        'items': [],
        'chats': {},
    }
    while True:
        params = {'limit': 20}  # 20 is the cap per page
        params['excludeLowPriority'] = False
        params['includeMuted'] = True
        if cursor:
            params['cursor'] = cursor
            params['direction'] = 'before'

        resp = requests.get(f'{BEEPER_HOST_URL}/v0/search-messages', headers=REQUEST_HEADERS, params=params)
        resp.raise_for_status()
        data = resp.json()

        all_data['chats'].update(data.get('chats', {}))
        all_data['items'].extend(data.get('items', []))
        if not data.get('hasMore') or not data.get('oldestCursor'):
            break
        cursor = data['oldestCursor']
    return all_data


def main():
    output_root_dir = sys.argv[1]
    data = get_all_messages()
    dump_data(data, output_root_dir)


if __name__ == '__main__':
    main()
