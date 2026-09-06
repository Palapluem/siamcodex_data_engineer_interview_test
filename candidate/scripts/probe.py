"""Connectivity smoke check, intentionally not an ingestion solution."""
import json
import socket
import ssl
import urllib.error
import urllib.request
from pathlib import Path

context = ssl.create_default_context(cafile="/client/ca.crt")
sources = json.loads(Path("/client/source_credentials.json").read_text())
for source, config in sources.items():
    request = urllib.request.Request(config["url"] + "?cursor=0&limit=1",
                                     headers={"Authorization": "Bearer " + config["token"]})
    with urllib.request.urlopen(request, context=context, timeout=10) as response:
        page = json.load(response)
        assert len(page["items"]) == 1
        print(source, "TLS + auth + first page OK")
    try:
        urllib.request.urlopen(config["url"], context=context, timeout=10)
        raise AssertionError("Unauthenticated source access succeeded")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
try:
    socket.getaddrinfo("source.internal", 8080)
    raise AssertionError("Private source DNS leaked into transit")
except socket.gaierror:
    print("Private source DNS is unavailable from transit, as expected")
token = json.loads(Path("/client/test_tokens.json").read_text())["analyst_aster"]
request = urllib.request.Request("https://identity:8443/introspect", data=json.dumps({"token": token}).encode(),
                                 headers={"Authorization": "Bearer " + Path("/client/identity_service.key").read_text().strip(),
                                          "Content-Type": "application/json"})
with urllib.request.urlopen(request, context=context, timeout=10) as response:
    assert json.load(response)["units"] == ["AST-1"]
print("Identity introspection OK")
