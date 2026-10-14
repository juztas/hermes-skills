#!/usr/bin/env python3
"""Thin client + CLI for the ESnet IRI Facility API (ESnet East, API v2).

ESnet East validates the AmSC token (a MyAmSC Personal Access Token) itself:
send it as a Bearer token, no Globus login, no token exchange, no RIG. The
token's amsc_project_context picks the ESnet local account you act as.

Token resolution (first hit wins; the token is never printed):
  1. ESnetIRI(token="...")
  2. $AMSC_TOKEN
  3. $INFERENCE_TOKEN, ONLY when it decodes as an AmSC-issued JWT (the Agent
     in a Box MAG setup passes the PAT there). Any other inference key is
     ignored so it is never sent to ESnet.

Library use:
    from esnet_iri_client import ESnetIRI
    api = ESnetIRI.from_env()
    api.whoami()                         # {"username": "hackathon2610"}
    code, job = api.submit_job(ESnetIRI.bash_job("echo hello"))
    api.wait_job(job["id"]); api.job_output(job["id"])

CLI use (python3 esnet_iri_client.py -h):
    status | whoami | projects | locations | jobs [--all] | job ID
    submit --script '...' [--wait] | cancel ID | output ID
    ls | stat | view | head | tail | checksum | mkdir | rm | cp | mv | upload
"""
import argparse
import base64
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

DEFAULT_BASE = "https://iri-dev.ppg.es.net/api/v2"

# Deterministic UUIDv5 ids (identical on ESnet East and West). Re-verify with
# `status` if a call returns 404 "Resource not found".
RESOURCES = {
    "compute": "fb0aafe1-c780-55c0-b635-a7121f1b0ce5",        # Low Priority compute (EKS)
    "storage": "b6d59ba3-5c2d-57dd-b3b9-108832da0578",        # Storage (EFS): all /filesystem ops
    "home_fs": "9df843df-0c83-55ec-8da2-02f5adf32347",
    "home_mount": "dcae60ec-5842-5c76-9c91-8248cfb7720a",
    "project_fs": "9a7ca41b-47e9-5e4d-add1-99d5d7eef779",
    "project_mount": "824ec90c-d379-5d6d-9ad3-dc878e750d42",
    "globus_dtn": "42787ca0-364d-51ec-92e2-4f1fb809cad2",     # East only
}

# Only tokens from these PingAM hosts may be picked up from $INFERENCE_TOKEN.
AMSC_ISSUER_HOSTS = {"identity.amsc.ornl.gov", "identity.dev.amsc.ornl.gov"}

TASK_TERMINAL = {"completed", "failed", "canceled"}
JOB_TERMINAL = {"completed", "failed", "canceled"}


def jwt_claims(token):
    """Decode a JWT payload WITHOUT verifying it (for display/routing only)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return {}


def _is_amsc_token(token):
    iss = jwt_claims(token).get("iss") or ""
    return urllib.parse.urlsplit(iss).hostname in AMSC_ISSUER_HOSTS


def resolve_token():
    """Return (token, source_env_name) or (None, None)."""
    tok = os.environ.get("AMSC_TOKEN", "").strip()
    if tok:
        return tok, "AMSC_TOKEN"
    tok = os.environ.get("INFERENCE_TOKEN", "").strip()
    if tok and _is_amsc_token(tok):
        return tok, "INFERENCE_TOKEN"
    return None, None


class ESnetIRI:
    COMPUTE = RESOURCES["compute"]
    STORAGE = RESOURCES["storage"]

    def __init__(self, token=None, base=None, token_source="argument"):
        self.base = (base or os.environ.get("ESNET_IRI_BASE_URL") or DEFAULT_BASE).rstrip("/")
        self.token = token
        self.token_source = token_source if token else None

    @classmethod
    def from_env(cls, base=None):
        token, source = resolve_token()
        return cls(token=token, base=base, token_source=source)

    # ---- transport ----
    def _req(self, method, path, params=None, body=None, auth=True, data=None, content_type=None):
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        if body is not None:
            data = json.dumps(body).encode()
            content_type = "application/json"
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("User-Agent", "esnet-iri-client/1.0")
        req.add_header("Accept", "application/json")
        if auth:
            if not self.token:
                raise RuntimeError(
                    "No AmSC token: set AMSC_TOKEN to a MyAmSC Personal Access Token "
                    "(https://my.amsc.energy.gov -> select project -> Personal Access Tokens)."
                )
            req.add_header("Authorization", f"Bearer {self.token}")
        if content_type:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            # Keep the full body: the problem+json `detail` names the real cause
            # (expired token, unmapped project, path not permitted, ...).
            raw = e.read().decode()
            try:
                err = json.loads(raw)
            except ValueError:
                err = raw
            return e.code, {"error": err, "http_status": e.code}

    def _get(self, path, **kw):
        return self._req("GET", path, **kw)[1]

    # ---- no-auth status ----
    def resources(self):
        return self._get("/status/resources", auth=False)

    def resource(self, resource_id):
        return self._get(f"/status/resources/{resource_id}", auth=False)

    def facility(self):
        return self._get("/facility", auth=False)

    def incidents(self):
        return self._get("/status/incidents", auth=False)

    def events(self):
        return self._get("/status/events", auth=False)

    # ---- identity / account ----
    def token_info(self):
        """Non-secret facts about the token in use (never the token itself)."""
        c = jwt_claims(self.token or "")
        exp = c.get("exp")
        return {
            "source": self.token_source,
            "issuer": c.get("iss"),
            "sub": c.get("sub"),
            "amsc_project_context": c.get("amsc_project_context"),
            "expires_utc": dt.datetime.fromtimestamp(exp, dt.timezone.utc).isoformat() if exp else None,
            "expires_in_hours": round((exp - time.time()) / 3600, 1) if exp else None,
        }

    def whoami(self):
        return self._get("/account/whoami")

    def projects(self):
        return self._get("/account/projects")

    def allocations(self, project_id):
        return self._get(f"/account/projects/{project_id}/project_allocations")

    def capabilities(self):
        return self._get("/account/capabilities")

    def storage_locations(self, resource_id=STORAGE, intent=None):
        return self._get(f"/storage/locations/{resource_id}", params={"intent": intent} if intent else None)

    # ---- compute (Kubernetes Jobs) ----
    @staticmethod
    def bash_job(script, name=None, image="ubuntu:22.04", cpus=1, memory_gib=1.0, gpus=0,
                 duration=3600, nodes=1, directory=None, environment=None,
                 stdout_path=None, stderr_path=None):
        """Job spec that runs `script` with `bash -lc` inside `image` (image must ship bash)."""
        # Pods run as a uid with no passwd entry, so HOME is "/" (not writable);
        # point it at the user's EFS home. IRI_USER_ID is set by the facility.
        script = 'export HOME="/data/home/${IRI_USER_ID}"\n' + script
        resources = {
            "node_count": nodes,
            "processes_per_node": 1,
            "cpu_cores_per_process": cpus,
            "memory": int(memory_gib * 1024 ** 3),
        }
        # Every ResourceSpec integer has minimum 1, so gpu_cores_per_process: 0
        # is rejected (422). Omit it for CPU-only jobs.
        if gpus:
            resources["gpu_cores_per_process"] = gpus
        spec = {
            "name": name or f"hermes-{dt.datetime.now(dt.timezone.utc):%Y%m%d-%H%M%S}",
            "executable": "bash",
            "arguments": ["-lc", script],
            "container": {"image": image},
            "environment": environment or {},
            "resources": resources,
            "attributes": {"duration": duration},
        }
        for key, val in (("directory", directory), ("stdout_path", stdout_path), ("stderr_path", stderr_path)):
            if val:
                spec[key] = val
        return spec

    def submit_job(self, spec, resource_id=COMPUTE):
        return self._req("POST", f"/compute/job/{resource_id}", body=spec)

    def job(self, job_id, resource_id=COMPUTE, include_spec=False):
        return self._get(f"/compute/status/{resource_id}/{job_id}",
                         params={"include_spec": str(include_spec).lower()})

    def jobs(self, filters=None, resource_id=COMPUTE, historical=False, limit=100, offset=0, include_spec=False):
        """List jobs. WITHOUT filters this returns every user's jobs in the shared namespace."""
        params = {"historical": str(historical).lower(), "limit": limit, "offset": offset,
                  "include_spec": str(include_spec).lower()}
        return self._req("POST", f"/compute/status/{resource_id}", params=params, body=filters)[1]

    def my_jobs(self, **kw):
        """Jobs submitted with THIS token's AmSC identity (label amsc/sub)."""
        sub = jwt_claims(self.token or "").get("sub")
        if not sub:
            raise RuntimeError("Token has no sub claim; cannot filter to your jobs")
        return self.jobs(filters={"amsc/sub": sub}, **kw)

    def cancel_job(self, job_id, resource_id=COMPUTE):
        return self._req("DELETE", f"/compute/cancel/{resource_id}/{job_id}")[0]  # 204 = accepted

    def wait_job(self, job_id, timeout=900, interval=5, resource_id=COMPUTE):
        last = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            last = self.job(job_id, resource_id)
            if (last.get("status") or {}).get("state") in JOB_TERMINAL or "error" in last:
                return last
            time.sleep(interval)
        return last

    def job_output(self, job_id, nbytes=8192, resource_id=COMPUTE):
        """Tail stdout/stderr of a job: explicit stdout_path/stderr_path from the
        job spec, else the default <home>/joblogs/<job_id>/<pod>/{stdout,stderr}."""
        info = self.job(job_id, resource_id, include_spec=True)
        spec = info.get("job_spec") or {}
        paths = [p for p in (spec.get("stdout_path"), spec.get("stderr_path")) if p]
        if not paths:
            listing = self.ls(f"joblogs/{job_id}")
            pods = [e["name"] for e in ((listing.get("result") or {}).get("output") or [])
                    if isinstance(e, dict) and e.get("type") == "directory"]
            paths = [f"joblogs/{job_id}/{pod}/{stream}" for pod in pods for stream in ("stdout", "stderr")]
        out = {}
        for p in paths:
            task = self.tail(p, nbytes)
            res = (task.get("result") or {}).get("output")
            out[p] = res.get("content") if isinstance(res, dict) else {"status": task.get("status"), "result": res}
        return out

    # ---- async filesystem (submit -> poll /task/{id}) ----
    def task(self, task_id):
        return self._get(f"/task/{task_id}")

    def wait_task(self, task_id, timeout=60, interval=1.0):
        last = None
        deadline = time.time() + timeout
        while time.time() < deadline:
            last = self.task(task_id)
            if last.get("status") in TASK_TERMINAL or "error" in last:
                return last
            time.sleep(interval)
        return last

    def _fs(self, verb, body, resource_id=STORAGE, params=None, data=None, content_type=None):
        """POST a filesystem op and poll its task. A 2xx submit is NOT success:
        check the returned task's `status`; on failure `result.output` holds the error."""
        if data is not None:
            code, resp = self._req("POST", f"/filesystem/{verb}/{resource_id}", params=params,
                                   data=data, content_type=content_type)
        else:
            code, resp = self._req("POST", f"/filesystem/{verb}/{resource_id}", params=params, body=body)
        if code >= 400:
            return {"submit_http": code, "error": resp.get("error", resp)}
        tid = resp.get("task_id")
        return self.wait_task(tid) if tid else {"submit_http": code, "resp": resp}

    # Paths: absolute under /data/home/<user> or /data/projects/<project_id>, or
    # relative (resolved against your home).
    def ls(self, path, show_hidden=False):
        return self._fs("ls", {"path": path, "showHidden": show_hidden})

    def stat(self, path):
        return self._fs("stat", {"path": path})

    def view(self, path, size=4096, offset=0):
        return self._fs("view", {"path": path, "size": size, "offset": offset})

    def head(self, path, nbytes=4096):
        # `lines` is accepted but ignored by ESnet (returns empty content): use bytes.
        return self._fs("head", {"path": path, "bytes": nbytes})

    def tail(self, path, nbytes=4096):
        """Last nbytes of a file. ESnet's /filesystem/tail ignores `lines` and fails
        with "[Errno 22] Invalid argument" when bytes > file size, so stat the file
        and read the range with /filesystem/view instead."""
        st = self.stat(path)
        if st.get("status") != "completed":
            return st
        size = int(((st.get("result") or {}).get("output") or {}).get("size") or 0)
        n = min(nbytes, size)
        return self.view(path, size=n, offset=size - n)

    def checksum(self, path):
        return self._fs("checksum", {"path": path})

    def mkdir(self, path, parent=True):
        return self._fs("mkdir", {"path": path, "parent": parent})

    def rm(self, path):
        return self._fs("rm", {"path": path})

    def cp(self, src, dst):
        return self._fs("cp", {"path": src, "target_path": dst})

    def mv(self, src, dst):
        return self._fs("mv", {"path": src, "target_path": dst})

    def upload(self, remote_path, content, filename="upload.bin"):
        """multipart/form-data upload of `content` (bytes) to `remote_path`."""
        boundary = uuid.uuid4().hex
        data = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        return self._fs("upload", None, params={"path": remote_path}, data=data,
                        content_type=f"multipart/form-data; boundary={boundary}")


def _print(obj):
    print(json.dumps(obj, indent=2, default=str))


def main(argv=None):
    ap = argparse.ArgumentParser(description="ESnet IRI Facility API (East, v2) via AmSC token")
    ap.add_argument("--base", help=f"API base URL (default $ESNET_IRI_BASE_URL or {DEFAULT_BASE})")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("status", help="resources + current status (no token needed)")
    sp.add_parser("whoami", help="ESnet account + non-secret token facts")
    sp.add_parser("projects")
    sp.add_parser("locations", help="your home/project paths")
    p = sp.add_parser("jobs", help="your jobs (filtered by your AmSC sub)")
    p.add_argument("--all", action="store_true", help="every user's jobs in the namespace")
    p.add_argument("--historical", action="store_true")
    p.add_argument("--spec", action="store_true", help="include job specs")
    p = sp.add_parser("job")
    p.add_argument("job_id")
    p.add_argument("--spec", action="store_true")
    p = sp.add_parser("submit", help="run a bash script in a container")
    p.add_argument("--script", required=True)
    p.add_argument("--image", default="ubuntu:22.04", help="must contain bash")
    p.add_argument("--name")
    p.add_argument("--cpus", type=int, default=1)
    p.add_argument("--memory-gib", type=float, default=1.0)
    p.add_argument("--gpus", type=int, default=0)
    p.add_argument("--nodes", type=int, default=1, help="N independent pods (not MPI)")
    p.add_argument("--duration", type=int, default=3600, help="seconds")
    p.add_argument("--wait", action="store_true", help="wait for a terminal state, then print output")
    p.add_argument("--timeout", type=int, default=900)
    p = sp.add_parser("cancel")
    p.add_argument("job_id")
    p = sp.add_parser("output", help="tail a job's stdout/stderr")
    p.add_argument("job_id")
    p.add_argument("--bytes", type=int, default=8192)
    for verb in ("ls", "stat", "checksum", "rm", "mkdir"):
        sp.add_parser(verb).add_argument("path")
    for verb in ("view", "head", "tail"):
        p = sp.add_parser(verb)
        p.add_argument("path")
        p.add_argument("--bytes", type=int, default=4096)
        if verb == "view":
            p.add_argument("--offset", type=int, default=0)
    for verb in ("cp", "mv"):
        p = sp.add_parser(verb)
        p.add_argument("src")
        p.add_argument("dst")
    p = sp.add_parser("upload")
    p.add_argument("local")
    p.add_argument("remote")
    a = ap.parse_args(argv)

    api = ESnetIRI.from_env(base=a.base)
    if a.cmd == "status":
        for r in api.resources():
            print(f"{r['name']:22s} {r['id']}  {r['current_status']:9s} {r['resource_type']}")
        return 0
    if a.cmd == "whoami":
        _print({"whoami": api.whoami(), "token": api.token_info(), "base": api.base})
    elif a.cmd == "projects":
        _print(api.projects())
    elif a.cmd == "locations":
        _print(api.storage_locations())
    elif a.cmd == "jobs":
        kw = {"historical": a.historical, "include_spec": a.spec}
        _print(api.jobs(**kw) if a.all else api.my_jobs(**kw))
    elif a.cmd == "job":
        _print(api.job(a.job_id, include_spec=a.spec))
    elif a.cmd == "submit":
        spec = ESnetIRI.bash_job(a.script, name=a.name, image=a.image, cpus=a.cpus,
                                 memory_gib=a.memory_gib, gpus=a.gpus, duration=a.duration, nodes=a.nodes)
        code, resp = api.submit_job(spec)
        _print({"http": code, "response": resp})
        if code >= 400 or not a.wait:
            return 0 if code < 400 else 1
        final = api.wait_job(resp["id"], timeout=a.timeout)
        _print({"final": final, "output": api.job_output(resp["id"])})
    elif a.cmd == "cancel":
        code = api.cancel_job(a.job_id)
        print(f"HTTP {code}" + (" (accepted)" if code == 204 else ""))
    elif a.cmd == "output":
        _print(api.job_output(a.job_id, a.bytes))
    elif a.cmd == "view":
        _print(api.view(a.path, a.bytes, a.offset))
    elif a.cmd in ("head", "tail"):
        _print(getattr(api, a.cmd)(a.path, a.bytes))
    elif a.cmd in ("ls", "stat", "checksum", "rm", "mkdir"):
        _print(getattr(api, a.cmd)(a.path))
    elif a.cmd in ("cp", "mv"):
        _print(getattr(api, a.cmd)(a.src, a.dst))
    elif a.cmd == "upload":
        with open(a.local, "rb") as f:
            _print(api.upload(a.remote, f.read(), os.path.basename(a.local)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
