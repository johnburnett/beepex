from datetime import datetime
import json
from pathlib import Path
from typing import AsyncGenerator


class MockData:
    def __init__(self, data: dict):
        self._data = data

    def __getattr__(self, name):
        value = self._data.get(name)
        if name == "timestamp":
            value = datetime.fromisoformat(value)
        if isinstance(value, dict):
            return MockData(value)
        elif isinstance(value, list):
            return [MockData(it) for it in value]
        else:
            return value


class MockDownloadResponse:
    def __init__(self, src_url: str):
        self.src_url = src_url
        self.error = None


class MockAssets:
    def __init__(self, test_data_path: Path):
        self._test_data_path = test_data_path

    async def download(self, *, url: str) -> MockDownloadResponse:
        test_file = self._test_data_path / "goodgood.png"
        return MockDownloadResponse(test_file.as_uri())


class MockChats:
    def __init__(self, chat: dict):
        self._chat = MockData(chat)

    async def retrieve(self, id: str) -> MockData:
        return self._chat

    async def list(self) -> AsyncGenerator[MockData]:
        yield self._chat


class MockMessages:
    def __init__(self, messages: list[dict]):
        self._messages = [MockData(msg) for msg in messages]

    async def list(self, id: str) -> AsyncGenerator[MockData]:
        for message in self._messages:
            yield message


class MockAsyncBeeperDesktop:
    def __init__(self, test_data_path: Path):
        with open(test_data_path / "chat.json", encoding="utf-8") as fp:
            data = json.load(fp)
        self.assets = MockAssets(test_data_path)
        self.chats = MockChats(data["chat"])
        self.messages = MockMessages(data["messages"])
