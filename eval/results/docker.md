# D1 — Docker / runtime packaging (spec 001-token-estimator v5.0, §10–§11)

Measured on the development host (x86_64, Docker 29.7.2, BuildKit, no cache priming
beyond the base image pull). Every number below comes from a command that was
actually run; nothing is estimated unless labelled as an estimate.

## 1. Changed / added files

| File | Change |
|---|---|
| `Dockerfile` | Rewritten: digest-pinned base, hashed-lock install, cache cleanup, `entrypoint.sh` as ENTRYPOINT, runtime CMD `python /app/run_agent.py --tasks-dir /data/tasks`, `PIP_NO_INDEX=1`, `HOME=/tmp`. |
| `.dockerignore` | Rewritten as deny-by-default whitelist (context = `run_agent.py`, `harness/**`, `requirements.lock`, `entrypoint.sh`); explicit exclusions for caches, answers, logs, env files, keys, swap files. |
| `entrypoint.sh` | New. Normalises `HOME` (`/tmp`), then `exec "$@"`. No downloads, no config rewriting, no writes. |
| `requirements.lock` | New. 171 pinned packages, each with a `--hash=sha256:` digest (full `openjiuwen==0.1.20.post1` runtime closure). |
| `scripts/build.sh` | Rewritten: disk preflight, measured build, size report, hard checks against the §11.1 limits. |
| `scripts/run_local.sh` | Rewritten: judge-shaped run on a throw-away copy of `examples/task-dir`, offline smoke assertions, answer-placement check. |

No Python code, tests or docs were modified.

## 2. Base image pinning

```
python:3.11-slim-bookworm
  index digest   : sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39
  amd64 manifest : sha256:e8c69d86735f6df3efa06b324694fe51f0422785f83a3bd463052636c2421944
  python 3.11.18, debian bookworm-slim
```

Resolved with `docker buildx imagetools inspect python:3.11-slim-bookworm` and confirmed
by the pull (`Digest: sha256:a83ba18a…`). The tag is kept next to the digest for
readability; the digest is authoritative. The `# syntax=docker/dockerfile:1` directive
was removed so the build does not fetch an unpinned BuildKit frontend image.

## 3. Dependency lock

`requirements.lock` holds the full runtime closure of `openjiuwen==0.1.20.post1`
(171 packages) as `name==version --hash=sha256:<wheel digest>`; the digests were computed
from the downloaded wheels. The image installs it with:

```
pip install --no-cache-dir --no-deps --require-hashes --root-user-action=ignore \
            --no-warn-script-location -r /tmp/requirements.lock
```

`--require-hashes` makes any substituted or tampered artifact fail the build;
`--no-deps` forbids ad-hoc resolution. The lock file and every pip/build cache are
deleted in the same layer. No `pip install -U` anywhere.

## 4. Build measurement

| Run | `build_seconds` | Notes |
|---|---|---|
| Cold (all layers, dependencies downloaded) | **1015.0 s** | install layer alone 963.9 s; 166 MB of wheels pulled at ≈0.17 MB/s from PyPI |
| Warm rebuild (all layers cached) | **0.4 s** | after the Dockerfile comment-only change |

`docker images` after the build:

```
IMAGE                    ID             DISK USAGE   CONTENT SIZE   EXTRA
openjiuwen-hack:latest   fd4041635e8c       1.38GB          306MB
hack:latest              fd4041635e8c       1.38GB          306MB
```

- `docker image inspect` size: 306 384 228 bytes = **292.2 MiB** — well below the 4 GB limit.
- Build context actually sent: 28 files, 360 KB (verified with a scratch `type=tar`
  output build: no `__pycache__`, no `.git`, no tests, no examples, no archives).

## 5. Offline smoke

Command: `bash scripts/run_local.sh`
(`--network none --read-only --tmpfs /tmp:rw,size=2g -e HOME=/tmp --cpus 4 --memory 16g
--cap-drop ALL --security-opt no-new-privileges -v "$COPY:/data/tasks:rw"`,
then `python /app/run_agent.py --tasks-dir /data/tasks --offline --mutation-repair --max-workers 1`).

```
summary: {"tasks": 8, "solved": 8, "failed": 0, "errors": 0, "score": 20.0, "tokens_spent": 0.0, "offline": true}
OK: 8/8 solved, score 20, tokens 0
answer.txt in tasks copy : 14
answer.txt in repo       : 0
RESULT: PASS (offline smoke, 4 CPU / 16 GiB / read-only rootfs, no network)
```

14 files = 8 × `<task>/answer.txt` + 6 × `<task>/workspace/answer.txt` (the harness
writes both locations when a workspace exists). Wall time ≈3.2 s. The repository's
`examples/` tree is untouched: the run operates on a `mktemp -d` copy, and the script
fails if any `answer.txt` appears under `examples/`.

Diff of the copy against the source shows only `answer.txt` files plus the two
`workspace/mylib.py` files rewritten by `--mutation-repair` — as expected, and only
inside the copy.

## 6. Security verification

| Check | Result |
|---|---|
| `grep -R "cpus 8\|per-task" Dockerfile entrypoint.sh scripts` | no matches |
| `docker history --no-trunc` secret grep (`sk-…`, `oc_sk_`, `API_KEY=`, `token=`, `password`, `secret`) | no matches; only the base image's public `GPG_KEY` |
| Filesystem scan for `.git`, `id_rsa*`, private keys, weights (`*.safetensors/*.gguf/*.onnx/*.ckpt/*.bin > 1 MB`) | none; the only `*.pem` files are `ca-certificates` trust anchors |
| `/root`, `/tmp` in image | 12 KB / 0 KB; no `~/.cache`, no HuggingFace cache |
| Post-build install attempt (`pip install httpie` in a read-only container) | fails: `No matching distribution found` — `PIP_NO_INDEX=1` |
| Runtime writes | only the bind-mounted task tree (`answer.txt`), `/tmp` tmpfs (`$OUT` default `/tmp/openjiuwen-out`) |
| Entry point | `exec "$@"`, `sh` only, no network, no writes, no package logic |

Container is intentionally root: `/data/tasks` is bind-mounted with unknown host
ownership and the harness must create `answer.txt` there. `--cap-drop ALL` +
`no-new-privileges` remove `CAP_DAC_OVERRIDE`, so root behaves like an unprivileged uid
with respect to the mount. No capabilities are added anywhere.

## 7. Required variables at runtime

- `LLM_API_KEY`, `LLM_BASE_URL` — passed as `-e` only, never baked into the image.
- Optional: `LLM_MODEL`, `LLM_ESCALATION_MODEL`, `LLM_MAX_TOKENS`, `OJ_TOKEN_BUDGET`/`BUDGET`
  (global budget only — no per-task cap).

## 8. Blockers and risks

1. **Cold build exceeds 1000 s on this host (1015.0 s).** The bottleneck is PyPI
   throughput (166 MB of wheels at ≈0.17 MB/s); the build itself is CPU-light. The warm
   rebuild is 0.4 s, so any machine with the BuildKit cache or a normal mirror is well
   inside the limit. Mitigations, in order of preference: build once on the submission
   host and keep the cache; use a fast PyPI mirror; pre-seed the wheel cache. No fake
   numbers are reported here.
2. **SDK pulls DB / browser / ML-adjacent transitive packages.** `openjiuwen==0.1.20.post1`
   depends on SQLAlchemy, alembic, pymilvus, pyoxigraph, pandas, matplotlib, transformers,
   pdfplumber, fastapi/uvicorn, tiktoken. They cannot be dropped without breaking SDK
   imports (no code changes allowed for an infra problem). Image stays at 292 MiB, so the
   4 GB limit is safe. Flagged for the integrator: if the organizers object to DB/UI
   packages in the image, the only clean fix is an organizers-approved slimmer SDK install,
   not hand-deleting site-packages.
3. **Task-directory permission risk (judge side).** With a 0755 `<task>/` owned by a foreign
   uid, the container (root without `CAP_DAC_OVERRIDE`) cannot write `answer.txt`.
   Reproduced: 0/8 solved, 8 failed with `PermissionError … .mylib.py.<rand>.tmp`.
   `run_local.sh` therefore makes its copy world-writable, which mirrors a judge mount that
   is writable by the container uid; the spec (§11.2) requires checking this in the final
   command and forbids widening Docker restrictions to work around it. Needs organizer
   confirmation or an integrator decision on `--user`.
4. **Silent exit code on total failure.** In the permission experiment the process exited 0
   while every task failed. Exit status must not be the only success signal; the smoke
   script asserts the JSON summary instead. The runner's exit-code policy is Python code —
   reported, not changed here.

## 9. Rollback

```bash
# code: revert the packaging files (previous Dockerfile/.dockerignore/build.sh/run_local.sh
# are in HEAD; entrypoint.sh and requirements.lock are new)
git checkout HEAD -- Dockerfile .dockerignore scripts/build.sh scripts/run_local.sh
rm -f entrypoint.sh requirements.lock
# images: remove only after approval, they are ~1.38 GB each
docker rmi openjiuwen-hack:latest hack:latest
```

No `docker prune`, image removal, `git push` or production deploy was performed. Disk
before/after: 18 GB → 17 GB free on the Docker root; the preflight in `scripts/build.sh`
aborts with exit 77 and an explicit size estimate if free space is insufficient.

## 10. Reproduce

```bash
bash scripts/build.sh      # preflight, build, build_seconds, size, limit checks
bash scripts/run_local.sh  # judge-shaped offline smoke, 8/8 + score 20 + 0 tokens
docker history --no-trunc openjiuwen-hack:latest | grep -iE 'sk-|API_KEY=|token=|password=|secret'
```
