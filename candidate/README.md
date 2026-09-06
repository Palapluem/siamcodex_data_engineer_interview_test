# Data engineer take home assessment

Build a small production-minded data pipeline for the fictional Meridian Service Cooperative. Start with [REQUIREMENTS.md](REQUIREMENTS.md), also provided as [REQUIREMENTS.pdf](REQUIREMENTS.pdf). Read [TECHNICAL_CONTRACT.md](TECHNICAL_CONTRACT.md) before implementation. The brief describes business needs; the contract fixes integration and evaluation interfaces without selecting your architecture.

This is a senior assessment with a 1-2 day take-home window. Spend approximately 10-14 focused hours, record your time and tradeoffs, and prepare a 60-minute review. Prefer a complete, explainable implementation of the core path over a large unfinished platform. No cloud subscription or paid service is required. Disclose AI assistance and be ready to explain and modify everything you submit.

## Start the fixed lab

Prerequisites: Docker Engine or Docker Desktop with Compose v2, Python 3.10 or newer, and OpenSSL on the host. Reserve at least 4 CPU cores, 6 GiB RAM and 8 GiB disk for the whole lab. Image builds need internet access; source runtime networks are isolated. These host reservations are not your application's budget.

From this candidate directory:

```sh
python3 scripts/bootstrap.py
docker compose -f infrastructure/compose.yaml up -d --build --wait
docker compose -f infrastructure/compose.yaml --profile tools run --rm probe
```

Bootstrap generates random local credentials and a lab certificate authority in .runtime. It refuses to overwrite existing state. A successful probe verifies each source gateway, TLS, authentication, identity introspection and private DNS isolation. Ask the interviewer about lab setup failures; setup troubleshooting outside your code does not count against the assessment.

Build your solution inside submission/. The example Compose file is a starting point, not a working application:

```sh
cp submission/compose.example.yaml submission/compose.yaml
# Implement your application and Dockerfile, then:
docker compose -f submission/compose.yaml up -d --build
```

Use project name de-interview-submission. Stop only this exercise's resources after use:

```sh
docker compose -f submission/compose.yaml down
docker compose -f infrastructure/compose.yaml down
```

Volumes remain for recovery. Do not delete volumes during restart testing. Only one copy of this named lab should run on a Docker daemon at a time. Never use broad Docker prune commands.

## Files you may change

Implement everything under submission/. You may replace its example and choose your own stack. infrastructure/, data/, scripts/, this README and the technical contract are fixed exercise inputs. Do not change or override supplied Dockerfiles, Compose services, networks, source data, rate limits or failure behavior. Interviewer-controlled scenario changes are the only exception.

Only mount individual client credential files and the CA from .runtime/client as read-only inputs. Do not mount test_tokens.json into the service; those tokens belong to API callers. Never access .runtime/server, the CA private key or the host Docker socket from your application. Keep all generated credentials out of your submission and image layers. The interviewer provisions fresh credentials for evaluation.

Submit source code, dependency pins, Dockerfiles, Compose configuration, tests, a short runbook, architecture and trust-boundary diagram, resource budget, decision log and known limitations. Include exact build, startup, test and recovery commands. Do not send .runtime, downloaded dependencies or database volumes.
