---
name: esnet-iri-facility-api
description: Interact with the ESnet IRI Facility API (ESnet East, iri-dev.ppg.es.net/api/v2) — container compute jobs on Kubernetes, filesystem operations on EFS home/project storage, account data, and facility status. Authenticates with the AmSC token (MyAmSC Personal Access Token) directly; no Globus login.
category: research
required_environment_variables:
  - name: AMSC_TOKEN
    prompt: AmSC Personal Access Token (MyAmSC PAT)
    help: "https://my.amsc.energy.gov -> select your project in the left sidebar -> Personal Access Tokens -> New Token. Pass it with docker run -e AMSC_TOKEN=... (the client also accepts the same PAT from INFERENCE_TOKEN when that holds an AmSC-issued token)."
    optional: true
---

# ESnet IRI Facility API

ESnet runs a DOE Integrated Research Infrastructure (IRI) Facility API implementation on AWS:
compute is **Kubernetes batch Jobs on EKS** (Kueue scheduler, Karpenter nodes, container images
pulled directly), and storage is **AWS EFS** (per-user home + per-project directories).

- **Base URL:** `https://iri-dev.ppg.es.net/api/v2` (ESnet Facility East)
- **OpenAPI spec:** `https://iri-dev.ppg.es.net/api/v2/openapi.json` (title "ESnet implementation
  of the IRI api", v2.0.0). `https://iri-dev.ppg.es.net/openapi.json` is a **404**.
- **Interactive docs:** `https://iri-dev.ppg.es.net/api/v2`
- **Auth:** `Authorization: Bearer <AmSC token>`. That's all. One HTTPBearer scheme, no scopes.
- **Helper:** `scripts/esnet_iri_client.py` (stdlib only; library + CLI).

## Authentication: the AmSC token directly

Unlike ALCF (Globus login, `alcf-tokens`), ESnet East validates the **AmSC token itself**:
signature against the production AmSC PingAM tenant (`https://identity.amsc.ornl.gov/am/oauth2`),
expiry, revocation, and `amsc_project_context`. There is NO interactive login, NO Globus,
NO token exchange, and NO RIG hop. Send the MyAmSC PAT as the Bearer token.

- **Which token:** a MyAmSC Personal Access Token (`https://my.amsc.energy.gov` -> select project ->
  Personal Access Tokens -> New Token). The same PAT used for MAG (`INFERENCE_TOKEN`) works.
  PATs last 30 days and are static: the agent cannot refresh one. When it expires, the user
  makes a new PAT and restarts the container.
- **Where the client finds it:** `$AMSC_TOKEN`, else `$INFERENCE_TOKEN` only if it decodes as an
  AmSC-issued JWT (so a non-AmSC LLM key is never sent to ESnet).
- **Project → account mapping:** the token's `amsc_project_context` selects the ESnet local
  account. Example (verified 2026-10-14): `genesis-mission-october-2026-hackathon` →
  `hackathon2610` (a **shared** account: every hackathon participant gets the same uid, home
  directory, and project directory). A project with no ESnet mapping fails with 401.
- **Never print, echo, or log the token.** Reference it as `$AMSC_TOKEN` in commands. Use
  `esnet_iri_client.py whoami` to show non-secret facts (sub, project, expiry).

Quick check:

    python3 /opt/data/skills/research/esnet-iri-facility-api/scripts/esnet_iri_client.py whoami
    # or
    curl -s -H "Authorization: Bearer $AMSC_TOKEN" https://iri-dev.ppg.es.net/api/v2/account/whoami
    # -> {"username":"hackathon2610"}

### Endpoints that do NOT accept the AmSC PAT (verified 2026-10-14)

| Endpoint | Result with a production AmSC PAT |
|---|---|
| `https://iri-dev.ppg.es.net/api/v2` (East v2) | **200**, use this one |
| `https://iri-dev.ppg.es.net/api/v1` (East v1) | 401 (v1 has no AmSC auth) |
| `https://esnet-west.sdn-sense.net/api/v2` (West v2) | 401 `Token not accepted by any provider` |

Public status endpoints work on all of them without a token.

## Stable resource IDs (verified live 2026-10-14)

    Low Priority compute  fb0aafe1-c780-55c0-b635-a7121f1b0ce5  (compute: all /compute calls)
    Storage               b6d59ba3-5c2d-57dd-b3b9-108832da0578  (storage system: all /filesystem calls)
    Home Filesystem       9df843df-0c83-55ec-8da2-02f5adf32347  (/data/home/<user>)
    Home Mount            dcae60ec-5842-5c76-9c91-8248cfb7720a
    Project Filesystem    9a7ca41b-47e9-5e4d-add1-99d5d7eef779  (/data/projects/<project_id>)
    Project Mount         824ec90c-d379-5d6d-9ad3-dc878e750d42
    Globus DTN            42787ca0-364d-51ec-92e2-4f1fb809cad2  (East only)

IDs are deterministic UUIDv5 values, so they are identical on East and West. Filesystem calls go to
the **Storage** resource, not the Home/Project Filesystem entries (those have no endpoints).
Re-verify with `GET /status/resources` (no auth) if you get a 404.

## Endpoint groups (public vs authenticated)

- **No auth:** `GET /status/resources`, `/status/resources/{id}`, `/status/incidents`,
  `/status/events`, `/facility`, `/facility/sites`. Good smoke tests.
- **Auth (Bearer AmSC token):** `/account/*` (incl. `GET /account/whoami`), `/compute/*`,
  `/filesystem/*`, `/storage/*`, `/task/*`.
- `GET /storage/locations/{STORAGE_ID}` returns your concrete paths, e.g.
  `home -> /data/home/hackathon2610`, `project -> /data/projects/hackathon2610-project` (shared).

## Compute: Kubernetes container jobs

Every job is a Kubernetes Job in namespace `compute`. Your command runs **inside a container image
you name** (`container.image`, default `ubuntu:22.04`), pulled straight from the registry, as your
ESnet account's uid (non-root, no privilege escalation). Your home is mounted at
`/data/home/<user>` and is the working directory.

- **Submit:** `POST /compute/job/{COMPUTE_ID}` → **200** `{"id": "<uuid>", "status": {"state": "new"}}`.
  No `account` or `queue_name` is needed (both ignored). Proven body shape:

      {
        "name": "hello-esnet",
        "executable": "bash",
        "arguments": ["-lc", "echo hello from $(hostname); date"],
        "container": {"image": "ubuntu:22.04"},
        "resources": {"node_count": 1, "processes_per_node": 1,
                      "cpu_cores_per_process": 1, "memory": 1073741824},
        "attributes": {"duration": 600}
      }

  CLI: `esnet_iri_client.py submit --script 'echo hello from $(hostname)' --wait`
- **Get one job (reliable):** `GET /compute/status/{COMPUTE_ID}/{job_id}` (`?include_spec=true`
  returns the submitted spec). States: `new` → `queued` → `active` → `completed` | `failed` |
  `canceled`.
- **List YOUR jobs:** `POST /compute/status/{COMPUTE_ID}` with JSON body
  `{"amsc/sub": "<your token's sub>"}` (CLI: `jobs`). **Without a body the list contains every
  user's jobs in the namespace**: never present an unfiltered list as "your jobs". Jobs are
  garbage-collected 24h after finishing.
- **Cancel:** `DELETE /compute/cancel/{COMPUTE_ID}/{job_id}` → **204** (accepted, async).
- **Output:** with no `stdout_path`/`stderr_path`, output goes to
  `/data/home/<user>/joblogs/<job_id>/<pod-name>/{stdout,stderr}` (one pod directory per pod).
  If you set them, relative paths resolve against the job directory (default: home) and the parent
  directory must already exist. CLI `output <job_id>` finds and tails either form.

Execution details that matter:

- The image **must contain bash** (the facility wrapper is `#!/usr/bin/env bash`): ubuntu, debian,
  python, nvidia/cuda images work; alpine/distroless do not.
- With `arguments`, `executable` is exec'd directly; with no `arguments`, `executable` is run as
  `bash -lc "<executable>"`. Prefer `executable: "bash"` + `arguments: ["-lc", "<script>"]`.
- `HOME` is `/` (unwritable) because the uid has no passwd entry; `whoami` prints
  `cannot find name for user ID ...`. Export `HOME=/data/home/$IRI_USER_ID` first (the helper's
  `bash_job()`/`submit` does). Env vars available: `IRI_USER_ID`, `JOB_ID`, `WORKSPACE_DIR`.
- `resources.memory` is **bytes per process** (default 256 MiB); CPU and memory scale with
  `processes_per_node`. East limits: 32 cores / 64 GiB per container, 50 concurrent jobs.
- `node_count: N` creates N **independent** identical pods (Job completions). There is no MPI or
  multi-node launcher.
- `attributes.duration` (seconds) becomes the pod deadline (+10 min grace). Always set one.
- GPUs: `gpu_cores_per_process` >= 1 requests `nvidia.com/gpu` (East advertises 8, CUDA,
  injected automatically). For CPU-only jobs **omit the field**; `0` is rejected (see Pitfalls).
  GPU jobs are not live-tested; expect extra wait for a GPU node to provision.
- The pool is **preemptible / low priority**: a job can sit in `queued` while nodes scale up.

## Filesystem ops: ALL ASYNCHRONOUS, ALL POST + JSON (v2)

Every `/filesystem/<op>/{STORAGE_ID}` call is a `POST` with a JSON body (v1-style GET with query
params does not exist in v2) and returns `{"task_id", "task_uri"}`. Poll `GET /task/{task_id}`
until `status` is `completed`, `failed`, or `canceled`. Results and errors live in the task's
`result.output`.

- Body fields: `{"path": ...}`. ls adds `showHidden`; view uses `size` + `offset`; head/tail use
  `bytes`; cp/mv use `path` + `target_path`; mkdir takes `parent`.
- **Path allowlist:** only `/data/home/<you>` and `/data/projects/<your project id>`. Relative
  paths resolve against your home. Anything else is rejected **synchronously** with **403
  `Path not permitted: <path>`** (no task is created). Expected, not a bug.
- A failed task looks like `{"status": "failed", "result": {"output": "[Errno 2] No such file or
  directory: '...'"}}`.
- Verified working (read-only, 2026-10-14): `ls`, `stat`, `view`, `head` (bytes), `checksum`
  (SHA-256), `GET /task/{id}`, `GET /task`.

## Pitfalls

- **Use East v2.** v1 and West reject the AmSC PAT (table above). Don't "fix" a 401 there by
  re-authenticating.
- **Don't wrap the PAT in anything.** No Globus flow, no PingAM token exchange, no RIG. The raw
  PAT in the Bearer header is the whole auth story.
- **401 reasons, by `detail`:** `AmSC authentication failed: Signature has expired` → PAT expired,
  user must create a new one; `... No local mapping for AmSC project '<p>'` → the PAT's project has
  no ESnet access (ask the user for a PAT from a mapped project, or ask the project VO);
  `... revoked` → terminal, new PAT needed; `Token not accepted by any provider` with no AmSC
  part → wrong endpoint (v1/West) or not an AmSC token.
- **403 `User does not have access to the resource`** on compute → the mapped account has no
  project on that resource. **403 `No compute identity configured for user ...`** → ESnet has not
  provisioned a uid for the account. Both are facility-side; report them, don't retry.
- **Never send `0` for a job integer.** Every `resources` and `attributes` integer
  (`node_count`, `processes_per_node`, `cpu_cores_per_process`, `gpu_cores_per_process`,
  `memory`, `duration`, ...) has `minimum: 1`, so `"gpu_cores_per_process": 0` fails validation
  and the job is not submitted. Leave the field out instead (the helper's `bash_job()` omits
  `gpu_cores_per_process` when `gpus=0`).
- **head/tail `lines` is silently ignored** (completed task, empty content). Use `bytes`.
- **`/filesystem/tail` fails `[Errno 22] Invalid argument` when `bytes` > file size.** Use
  `stat` + `view` with `offset = size - n` (the helper's `tail()` does this).
- **`view` needs `size`** (default 0 returns nothing). **`ls` `recursive` is ignored**: list
  level by level.
- **A 200 on a filesystem submit is not success**: poll the task.
- **Shared account:** in the hackathon mapping everyone is `hackathon2610`, so files in home and
  project are readable and writable by all participants. Use unique names (for example, prefix
  them with your sub) and never write secrets (tokens, keys) to ESnet storage.
- **`include_spec=true` on a job returns its environment.** Don't put secrets in `environment`.
- **Untested here (exist in OpenAPI):** `mkdir`, `rm`, `cp`, `mv`, `upload` (multipart field
  `file` + `?path=`), `download`, `compress`, `extract`, `symlink`, `chmod`, `chown`, GPU jobs.
  Probe before relying on them. CPU job submission via `submit --wait` is proven (2026-10-14,
  `ubuntu:22.04`, exit 0). `PUT /compute/job/...` (update) is not supported by ESnet (404):
  cancel and resubmit instead.
- **Not ALCF, not MAG.** ALCF IRI needs a Globus token from `alcf-tokens`; ESnet IRI takes the
  AmSC PAT. The same PAT also authenticates MAG inference; an ALCF Globus token sent to ESnet
  fails with 401.

## Files

- `scripts/esnet_iri_client.py`: client + CLI (`status`, `whoami`, `projects`, `locations`,
  `jobs`, `job`, `submit [--wait]`, `cancel`, `output`, `ls`, `stat`, `view`, `head`, `tail`,
  `checksum`, `mkdir`, `rm`, `cp`, `mv`, `upload`). Run `-h` for options.
- `references/esnet-iri-validation.md`: live validation log (2026-10-14), curl recipes, and how
  the token is validated server-side.
