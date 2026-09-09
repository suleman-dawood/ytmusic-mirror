#!/usr/bin/env bash
# Bumps Formula/ytmusic-mirror.rb in suleman-dawood/homebrew-ytmusic to the
# tagged release. Requires $HOMEBREW_PAT (write access to that repo) and is
# meant to run in CI on v* tags.
set -euo pipefail

REF="${GITHUB_REF_NAME:?GITHUB_REF_NAME is not set}"   # e.g. v0.9.0
SHA="$(curl -fsSL "https://codeload.github.com/suleman-dawood/ytmusic-mirror/tar.gz/refs/tags/${REF}" | sha256sum | awk '{print $1}')"
FORMULA="Formula/ytmusic-mirror.rb"

rm -rf /tmp/homebrew-ytmusic
git clone --depth 1 \
  "https://x-access-token:${HOMEBREW_PAT}@github.com/suleman-dawood/homebrew-ytmusic" \
  /tmp/homebrew-ytmusic
cd /tmp/homebrew-ytmusic

python3 - "$REF" "$SHA" "$FORMULA" <<'PY'
import re
import sys

ref, sha, formula = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(formula).read()
text = re.sub(
    r'url ".*"',
    f'url "https://github.com/suleman-dawood/ytmusic-mirror/archive/refs/tags/{ref}.tar.gz"',
    text,
    count=1,
)
text = re.sub(r'sha256 ".*"', f'sha256 "{sha}"', text, count=1)
open(formula, "w").write(text)
PY

git -c user.name="github-actions[bot]" \
    -c user.email="github-actions[bot]@users.noreply.github.com" \
    commit -am "ytmusic-mirror ${REF}" >/dev/null
git push
echo "Homebrew tap updated to ${REF} (${SHA})"
