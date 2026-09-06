"""Deterministic, entirely fictional 10,000-event source fixture. Standard library only."""
import argparse
import hashlib
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

SOURCES = ("aster", "birch", "cobalt")
UNITS = {"aster": ("AST-1", "AST-2"), "birch": ("BIR-1", "BIR-2"), "cobalt": ("COB-1", "COB-2")}


def generate(output, seed=73129):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    streams = {s: [] for s in SOURCES}
    originals = []

    def append(source, payload, phase=1, event_id=None):
        seq = len(streams[source]) + 1
        event = {"seq": seq, "event_id": event_id or f"{source}-e{seq:05}",
                 "phase": phase, "payload": payload}
        streams[source].append(event)
        return event

    for i in range(9000):
        source = SOURCES[i % 3]
        index = i // 3
        unit = UNITS[source][index % 2]
        case_id = f"C{index + 1:05}"  # IDs deliberately overlap between vendors.
        dt = datetime(2026, 6, 1, tzinfo=timezone.utc) + timedelta(minutes=rng.randrange(43200))
        amount = rng.randrange(500, 150001)
        status = rng.choice(("open", "completed", "cancelled"))
        common = {"version": 1, "op": "upsert", "schema_version": 1,
                  "classification": "restricted" if index % 5 == 0 else "internal",
                  "contact": f"person{index:05}@{source}.example.invalid"}
        if source == "aster":
            payload = dict(common, case_id=case_id, unit_id=unit, event_time=dt.isoformat(),
                           amount_minor=amount, currency="THB", status=status)
        elif source == "birch":
            payload = dict(common, ticket=case_id, branch=unit,
                           occurred_at=dt.astimezone(timezone(timedelta(hours=7))).isoformat(),
                           amount=f"{amount // 100}.{amount % 100:02}", currency="THB",
                           state={"open": "O", "completed": "D", "cancelled": "X"}[status])
        else:
            payload = dict(common, ref=case_id, site=unit, timestamp_ms=int(dt.timestamp() * 1000),
                           value_minor=amount, currency="THB",
                           status_code={"open": 10, "completed": 20, "cancelled": 90}[status])
        originals.append((source, append(source, payload)))

    for source, event in originals[:300]:
        append(source, dict(event["payload"]), event_id=event["event_id"])
    for i in range(100):
        source = SOURCES[i % 3]
        append(source, {"op": "upsert", "version": 1, "schema_version": 1,
                        "case_id": f"INVALID-{i}", "amount_minor": "broken",
                        "event_time": "not-a-time"})
    for source, event in originals[300:750]:
        p = dict(event["payload"], version=2)
        if source == "aster":
            p.update(amount_minor=p["amount_minor"] + 1250, status="completed")
        elif source == "birch":
            # v2 deliberately moves the monetary field; never use a binary float.
            from decimal import Decimal
            p.update(net_amount=str(Decimal(p.pop("amount")) + Decimal("12.50")),
                     state="D", schema_version=2)
        else:
            p.update(value_minor=p["value_minor"] + 1250, status_code=20)
        append(source, p, phase=2)
    for source, event in originals[750:900]:
        p = event["payload"]
        key = {"aster": "case_id", "birch": "ticket", "cobalt": "ref"}[source]
        append(source, {"op": "delete", key: p[key], "version": 2, "schema_version": 1}, phase=2)

    manifest = {"seed": seed, "total_events": 10000, "sources": {}}
    for source, events in streams.items():
        path = output / f"{source}.jsonl"
        path.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in events))
        manifest["sources"][source] = {
            "events": len(events), "phase1": sum(e["phase"] == 1 for e in events),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    p.add_argument("--seed", type=int, default=73129)
    args = p.parse_args()
    print(json.dumps(generate(args.output, args.seed), indent=2))
