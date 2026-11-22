"""One-time OAuth setup.

  uv run python extraction/moka/auth.py implicit-url
      -> authorize URL for the implicit flow (response_type=token)
  uv run python extraction/moka/auth.py save-token '<full redirected URL with #access_token=...>'
      -> stores the access token from the URL fragment (no refresh token in this flow)

  uv run python extraction/moka/auth.py url
      -> prints the authorize URL; open it, log in as the Moka owner and approve.
  uv run python extraction/moka/auth.py exchange '<code or full redirected URL>'
      -> swaps the code for access + refresh token (saved to extraction/moka/.token.json)
"""

import sys
import urllib.parse

import moka_client as moka

if len(sys.argv) >= 2 and sys.argv[1] == "url":
    print(moka.authorize_url())
elif len(sys.argv) >= 3 and sys.argv[1] == "exchange":
    arg = sys.argv[2]
    if arg.startswith("http"):
        arg = urllib.parse.parse_qs(urllib.parse.urlparse(arg).query)["code"][0]
    tok = moka.exchange_code(arg)
    print("OK, scopes:", tok.get("scope"), "| expires_in:", tok.get("expires_in"))
elif len(sys.argv) >= 2 and sys.argv[1] == "implicit-url":
    print(moka.authorize_url().replace("response_type=code", "response_type=token"))
elif len(sys.argv) >= 3 and sys.argv[1] == "save-token":
    import json, time
    frag = urllib.parse.parse_qs(urllib.parse.urlparse(sys.argv[2]).fragment)
    tok = {k: v[0] for k, v in frag.items()}
    tok.setdefault("created_at", int(time.time()))
    moka.TOKEN_FILE.write_text(json.dumps(tok, indent=2))
    moka.TOKEN_FILE.chmod(0o600)
    print("Saved token, scope:", tok.get("scope"))
else:
    print(__doc__)
