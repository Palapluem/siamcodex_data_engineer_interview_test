"""Create local-only random credentials and a lab CA. Requires Python 3.10+ and openssl."""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ("aster", "birch", "cobalt")


def setup(root=ROOT):
    runtime = root / ".runtime"
    if runtime.exists():
        raise SystemExit(".runtime already exists; keep it for resume, or archive it before an intentional fresh setup.")
    server = runtime / "server"
    client = runtime / "client"
    tls = server / "tls"
    tls.mkdir(parents=True)
    client.mkdir()
    os.chmod(runtime, 0o700)
    credentials = {}
    for source in SOURCES:
        token = secrets.token_urlsafe(32)
        (server / f"{source}.key").write_text(token)
        credentials[source] = {"url": f"https://vpn-{source}:8443/v1/changes", "token": token}
    (client / "source_credentials.json").write_text(json.dumps(credentials, indent=2))
    service_key = secrets.token_urlsafe(32)
    for folder in (server, client):
        (folder / "identity_service.key").write_text(service_key)
    profiles = {
        "analyst_aster": {"sub": "u-aster", "role": "analyst", "units": ["AST-1"], "clearance": "internal", "purpose": "operations"},
        "analyst_all": {"sub": "u-all", "role": "analyst", "units": ["*"], "clearance": "restricted", "purpose": "operations"},
        "auditor": {"sub": "u-audit", "role": "auditor", "units": ["*"], "clearance": "restricted", "purpose": "audit"},
        "operator": {"sub": "u-operator", "role": "operator", "units": [], "clearance": "internal", "purpose": "operations"},
        "wrong_purpose": {"sub": "u-purpose", "role": "analyst", "units": ["*"], "clearance": "restricted", "purpose": "marketing"},
    }
    tokens = {name: secrets.token_urlsafe(32) for name in profiles}
    (server / "principals.json").write_text(json.dumps({tokens[n]: p for n, p in profiles.items()}, indent=2))
    (client / "test_tokens.json").write_text(json.dumps(tokens, indent=2))
    (server / "control").mkdir()
    # Fixed length also avoids stale-size metadata during Docker Desktop host-file propagation.
    (server / "control/control.json").write_text(json.dumps({"phase": 1, "outages": {}, "revoked_tokens": [], "transient_errors": True}).ljust(4096))
    ca_key = runtime / "ca.key"
    ca_crt = client / "ca.crt"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
                    "-keyout", str(ca_key), "-out", str(ca_crt), "-subj", "/CN=Interview Local CA"], check=True, capture_output=True)
    csr = runtime / "server.csr"
    subprocess.run(["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", str(tls / "server.key"),
                    "-out", str(csr), "-subj", "/CN=Interview Gateway"], check=True, capture_output=True)
    ext = runtime / "tls.ext"
    ext.write_text("subjectAltName=DNS:vpn-aster,DNS:vpn-birch,DNS:vpn-cobalt,DNS:identity\nextendedKeyUsage=serverAuth\n")
    subprocess.run(["openssl", "x509", "-req", "-in", str(csr), "-CA", str(ca_crt), "-CAkey", str(ca_key),
                    "-CAcreateserial", "-out", str(tls / "server.crt"), "-days", "3650", "-extfile", str(ext)], check=True, capture_output=True)
    # Parent .runtime is 0700 on host. Files must be readable by rootless container UID 65532.
    for path in server.rglob("*"):
        if path.is_file():
            path.chmod(0o644)
    ca_key.chmod(0o600)
    print("Created local lab credentials and CA under .runtime. Do not commit or distribute .runtime.")


if __name__ == "__main__":
    setup()
