"""Nonvisual safeguard for the pinned DevUI streaming client; no UI changes."""

from hashlib import sha256
from pathlib import Path

SOURCE_SHA256 = sha256(Path(__file__).read_bytes()).hexdigest()
INSTALLED_BUNDLE_SHA256 = "1bb5d50943f3ba82dc17abb9aa615726ba8ffc746a18fd58e8b273f78fffed9f"
RETRY_ANCHOR = 'if(o++,o>e_)throw Error(`Connection failed after'
INITIAL_POST_GUARD = 'if(!c)throw Error(`Request outcome unknown; automatic resend disabled. Inspect the recorded run before a deliberate new action.`);'


def guarded_bundle(raw):
    if sha256(raw).hexdigest() != INSTALLED_BUNDLE_SHA256:
        raise RuntimeError("Installed DevUI frontend changed; review the nonvisual duplicate-POST safeguard before launching")
    text = raw.decode("utf-8")
    if text.count(RETRY_ANCHOR) != 1:
        raise RuntimeError("DevUI streaming retry anchor changed")
    # A lost initial response must not trigger another potentially billable POST.
    # Known-response GET reconnects and deliberate new Runs remain unchanged.
    return text.replace(RETRY_ANCHOR, INITIAL_POST_GUARD+RETRY_ANCHOR).encode("utf-8")
