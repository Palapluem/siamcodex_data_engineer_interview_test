"""Inspect one real page from each source gateway before writing adapters.

Runs inside a throwaway container on de-interview-transit with the client
credentials mounted read-only. This is inspection tooling, not the pipeline:
it exists so the vendor field mapping is confirmed against the live API rather
than against the illustrative payload in the contract.
"""

from __future__ import annotations

import json
import ssl
import urllib.parse
import urllib.request
from pathlib import Path

CA = "/client/ca.crt"
CREDS = Path("/client/source_credentials.json")
EXPECTED = {
    "aster": {"case_id", "unit_id", "event_time", "amount_minor", "status"},
    "birch": {"ticket", "branch", "occurred_at", "amount", "state"},
    "cobalt": {"ref", "site", "timestamp_ms", "value_minor", "status_code"},
}
SHARED = {"op", "version", "schema_version", "classification", "contact", "currency"}


def fetch(url: str, token: str, cursor: str = "0", limit: int = 3) -> dict:
    query = urllib.parse.urlencode({"cursor": cursor, "limit": limit})
    request = urllib.request.Request(
        f"{url}?{query}", headers={"Authorization": f"Bearer {token}"})
    context = ssl.create_default_context(cafile=CA)
    with urllib.request.urlopen(request, context=context, timeout=10) as response:
        return json.load(response)


def main() -> None:
    sources = json.loads(CREDS.read_text())
    for source, config in sources.items():
        page = fetch(config["url"], config["token"])
        first = page["items"][0]
        payload = first["payload"]

        print(f"===== {source} =====")
        print(f"envelope keys : {sorted(first)}")
        print(f"next_cursor   : {page['next_cursor']!r}   has_more: {page['has_more']}"
              f"   items: {len(page['items'])}")
        print(f"payload       : {json.dumps(payload, sort_keys=True)}")

        observed = set(payload)
        missing = EXPECTED[source] - observed
        extra = observed - EXPECTED[source] - SHARED
        verdict = "ALL PRESENT" if not missing else f"MISSING {sorted(missing)}"
        print(f"expected vendor fields {verdict}")
        if extra:
            print(f"unexpected extra fields: {sorted(extra)}")

        # An empty page must leave the cursor unchanged (contract), so confirm it.
        far = fetch(config["url"], config["token"], cursor="999999")
        print(f"empty page    : items={len(far['items'])} "
              f"next_cursor={far['next_cursor']!r} has_more={far['has_more']}")
        print()


if __name__ == "__main__":
    main()
