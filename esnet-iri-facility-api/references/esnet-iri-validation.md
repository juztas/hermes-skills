# ESnet IRI API validation notes (2026-10-14)

Live checks against ESnet Facility East and West using a production MyAmSC PAT
(`iss=https://identity.amsc.ornl.gov/am/oauth2`, `aud=https://api.amsc.energy.gov`,
`client_id=vo-hub-pat-creation-client`, scopes `openid profile amsc:none email`,
`amsc_project_context=genesis-mission-october-2026-hackathon`, 30-day lifetime).
Read-only calls only. No job was submitted and no file was written during this pass.

## How East validates the token (server side)

The core `iri-facility-api-python` `amsc_auth` module, enabled on East v2 with:

- issuer `https://identity.amsc.ornl.gov/am/oauth2` (production PingAM tenant), JWKS from its
  OIDC discovery document; signature + issuer + expiry are enforced;
- audience check **skipped** (`AMSC_TOKEN_SKIP_AUDIENCE_CHECK=true`), so a PAT whose `aud` is
  `https://api.amsc.energy.gov` is accepted. If ESnet turns the audience check back on, PATs stop
  working directly. The symptom is a 401 whose detail starts with `AmSC authentication failed`;
- DNSBL revocation check on `jti` (a revoked token returns a terminal 401);
- `amsc_project_context` → local account via `esnet_amsc_project_mapping.json`
  (`genesis-mission-october-2026-hackathon` → `hackathon2610`). An unmapped project returns 401
  `No local mapping for AmSC project`. An entry may also restrict `allowed_sub`.

West v2 trusts a different issuer (or has AmSC auth disabled), and v1 on both sites has no AmSC
auth, so production PATs are rejected there.

## Results

| # | Call | Result |
|---|------|--------|
| 1 | `GET /status/resources` East v1/v2, West v1/v2 (no auth) | 200 (East 7 resources, West 6, same IDs) |
| 2 | `GET /facility` East v2 (no auth) | 200, "ESnet Facility East" |
| 3 | `GET /openapi.json` | 404; spec is at `/api/v2/openapi.json` |
| 4 | `GET /account/whoami` East v2 | 200 `{"username":"hackathon2610"}` |
| 5 | `GET /account/projects` East v2 | 200, "Hackathon 2610 project" |
| 6 | `GET /account/capabilities` East v2 | 200, cpu / gpu / storage |
| 7 | `GET /account/projects` East **v1** | **401** |
| 8 | `GET /account/whoami` **West** v2 | **401** `Facility Specific authentication failed: 403: Token not accepted by any provider` |
| 9 | `GET /storage/locations/{STORAGE}` | 200, home `/data/home/hackathon2610`, project `/data/projects/hackathon2610-project` (shared) |
| 10 | `POST /filesystem/ls` `/data/home/hackathon2610` | task completed, entries listed |
| 11 | `POST /filesystem/ls` `joblogs` (relative) | task completed (resolved against home) |
| 12 | `POST /filesystem/ls` `/etc` | **403 `Path not permitted: /etc`** (synchronous, no task) |
| 13 | `POST /filesystem/ls` `recursive: true` | completed, but NOT recursive (same output as non-recursive) |
| 14 | `POST /filesystem/stat` | completed (size, uid, gid, mode, times) |
| 15 | `POST /filesystem/view` `size`+`offset` | completed, byte range returned |
| 16 | `POST /filesystem/head` `bytes` | completed |
| 17 | `POST /filesystem/head` / `tail` `lines` | completed with **empty content** (lines ignored) |
| 18 | `POST /filesystem/tail` `bytes` ≤ size | completed |
| 19 | `POST /filesystem/tail` `bytes` > size | **failed `[Errno 22] Invalid argument`** |
| 20 | `POST /filesystem/tail` with neither | 400 `Exactly one of bytes or lines must be specified.` |
| 21 | `POST /filesystem/checksum` | completed, SHA-256 |
| 22 | `GET /task/{bad id}` | 404 `Task ... not found` |
| 23 | `POST /compute/status/{COMPUTE}` (no body) | 200, 6 jobs, **including another account's job** |
| 24 | same, body `{"iri/user-id":"hackathon2610"}` | 200, 5 jobs (every hackathon participant) |
| 25 | same, body `{"amsc/sub":"<sub>"}` | 200, 3 jobs (only this token's jobs) |
| 26 | `POST /compute/job` with `"gpu_cores_per_process": 0` (Hermes agent run) | **rejected**: every ResourceSpec integer has `minimum: 1` in the v2 OpenAPI; no job created |
| 27 | `POST /compute/job` CPU-only, `gpu_cores_per_process` omitted, `ubuntu:22.04` (Hermes agent run, `submit --wait`) | 200, job `88d5f671-…` → `completed`, exit 0; stored spec confirms no GPU field |

Rows 26-27 come from a Hermes Agent in a Box session after this pass. The helper's `bash_job()`
now omits `gpu_cores_per_process` when `gpus=0`.

Evidence from earlier jobs on the same account (read back via `view`): the job ran as
`uid=50008 gid=50008`, `PWD=/data/home/hackathon2610`, `HOME=/`, with env `IRI_USER_ID`, `JOB_ID`,
`WORKSPACE_DIR`. stdout starts with `=== Job started ...` and ends with
`=== Job finished with exit code N ...`. When `stdout_path` was unset, the pod directory under
`joblogs/<job_id>/` holds `stdout`/`stderr`.

## curl recipes

    B=https://iri-dev.ppg.es.net/api/v2
    COMPUTE=fb0aafe1-c780-55c0-b635-a7121f1b0ce5
    STORAGE=b6d59ba3-5c2d-57dd-b3b9-108832da0578
    AUTH="Authorization: Bearer $AMSC_TOKEN"

    curl -s -H "$AUTH" $B/account/whoami
    curl -s -H "$AUTH" $B/storage/locations/$STORAGE

    # submit a hello-world container job
    curl -s -H "$AUTH" -H 'Content-Type: application/json' -X POST $B/compute/job/$COMPUTE -d '{
      "name": "hello-esnet",
      "executable": "bash",
      "arguments": ["-lc", "export HOME=/data/home/$IRI_USER_ID; echo hello from $(hostname)"],
      "container": {"image": "ubuntu:22.04"},
      "resources": {"node_count": 1, "processes_per_node": 1, "cpu_cores_per_process": 1, "memory": 1073741824},
      "attributes": {"duration": 600}
    }'
    curl -s -H "$AUTH" "$B/compute/status/$COMPUTE/<job_id>?include_spec=true"
    curl -s -X DELETE -H "$AUTH" -o /dev/null -w '%{http_code}\n' $B/compute/cancel/$COMPUTE/<job_id>   # 204

    # async filesystem op -> poll the task
    curl -s -H "$AUTH" -H 'Content-Type: application/json' -X POST $B/filesystem/ls/$STORAGE -d '{"path":"joblogs/<job_id>"}'
    curl -s -H "$AUTH" $B/task/<task_id>

## Helper smoke test

    S=/opt/data/skills/research/esnet-iri-facility-api/scripts/esnet_iri_client.py
    python3 $S status                 # no token needed
    python3 $S whoami                 # account + non-secret token facts
    python3 $S jobs                   # only your jobs (amsc/sub filter)
    python3 $S submit --script 'echo hello from $(hostname); nproc; free -g' --wait
    python3 $S output <job_id>
