import sys
from pathlib import Path

import requests

VALIDATOR_URL = "https://validator.nu/"


def validate_file(path: Path) -> int:
    print(f'Validating "{path}"')
    with path.open("rb") as f:
        response = requests.post(
            VALIDATOR_URL,
            params={"out": "json"},
            headers={"Content-Type": "text/html; charset=utf-8"},
            data=f,
            timeout=30,
        )
    response.raise_for_status()
    data = response.json()
    messages = data.get("messages", [])
    errors = 0
    for msg in messages:
        msg_type = msg.get("type")
        if msg_type == "error" or msg_type == "info":
            errors += 1
        line = msg.get("lastLine")
        col = msg.get("lastColumn")
        location = f"{line}:{col}" if line and col else "unknown location"
        print(f"[{msg_type.upper()}] {location} - {msg.get('message')}")
    return errors


def main():
    total_errors = 0
    for file_path in sys.argv[1:]:
        file_path = Path(file_path)
        if not file_path.exists():
            print(f"File not found: {file_path}", file=sys.stderr)
            total_errors += 1
            continue
        try:
            total_errors += validate_file(file_path)
        except requests.RequestException as ex:
            print(f'Error validating "{file_path}": {ex}', file=sys.stderr)
            total_errors += 1
    sys.exit(1 if total_errors > 0 else 0)


if __name__ == "__main__":
    main()
