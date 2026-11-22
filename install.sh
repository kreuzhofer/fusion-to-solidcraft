#!/bin/sh
# macOS/Linux shortcut for install.py (see there; on Windows run `py install.py`).
exec python3 "$(cd "$(dirname "$0")" && pwd)/install.py" "$@"
