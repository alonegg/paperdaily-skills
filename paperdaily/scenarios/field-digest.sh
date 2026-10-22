#!/usr/bin/env bash
# Compatibility entrypoint; see --help for current options.
set -euo pipefail
exec python3 "$(dirname "${BASH_SOURCE[0]}")/pd.py" field "$@"
