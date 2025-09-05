#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
batch_runner.py — Bulk runner for Chef Facts Extractor

This script orchestrates running `extractor.py` across many Git repositories,
discovering *cookbooks* inside each repo, and writing one JSON per cookbook.

Key ideas
---------
- Repos come from either:
  1) A GitLab group (and optionally subgroups) via the GitLab REST API
  2) A plain text file (`--repos-file`) with one Git URL per line

- A "cookbook root" is any directory that contains:
    metadata.rb  and  (recipes/ OR resources/)

- For each cookbook root, we run:
    python extractor.py --cookbook <root> --out <out_json> --summary

- Results:
    out/
      manifest.jsonl            # one JSON line per repo with cookbook-level statuses
      errors.jsonl              # clone/extract failures with reason
      <host>/<namespace>/<project>/<commit_sha>/<cookbook_relpath>.json

- Idempotent & resumable:
  If an output JSON already exists, the run skips it unless you pass --overwrite.

Environment
-----------
- Requires Python 3.9+, `git` CLI, and `requests` (see repo requirements.txt)
- For GitLab API discovery, set env:  GITLAB_TOKEN=glpat_xxx

Typical usage
-------------
# From a GitLab group:
export GITLAB_TOKEN=glpat_xxx
python batch_runner.py \
  --gitlab-base https://gitlab.example.com \
  --group-path my-dept/chef-cookbooks \
  --include-subgroups \
  --out-dir out \
  --work-dir work \
  --concurrency 24 \
  --extractor ./extractor.py

# From a text file of repos:
python batch_runner.py \
  --repos-file repos.txt \
  --out-dir out \
  --work-dir work \
  --concurrency 16 \
  --extractor ./extractor.py
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# External dep kept small + standard: install via requirements.txt
try:
    import requests  # type: ignore
except ImportError as e:
    print("Missing dependency. Please install:\n\n    pip install requests\n", file=sys.stderr)
    raise

# ------------------------------------------------------------------------------
# Small, thread-safe logging and JSONL helpers
# ------------------------------------------------------------------------------

_LOCK = threading.Lock()


def log(msg: str) -> None:
    """Thread-safe console printf with wall-clock timestamp."""
    with _LOCK:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)


def write_jsonl(path: Path, obj: Dict) -> None:
    """Append a single compact JSON object line to a .jsonl file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ------------------------------------------------------------------------------
# Subprocess helpers (git clone, extractor invocation)
# ------------------------------------------------------------------------------

def run(cmd: List[str], cwd: Optional[str] = None, timeout: Optional[int] = None) -> Tuple[int, str, str]:
    """
    Execute a command with optional working dir + timeout.

    Returns
    -------
    (rc, stdout, stderr)
    """
    p = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        return 124, out, err or "TimeoutExpired"
    return p.returncode, out, err


def git_shallow_clone(url: str, dest: Path, branch: Optional[str], timeout: int) -> Tuple[bool, str]:
    """
    Shallow clone a git repo to `dest`. If already present, try a shallow fetch.

    Returns
    -------
    (ok, commit_or_error)
    """
    if dest.exists():
        # If the path exists but isn't a repo, blow it away (defensive).
        if not (dest / ".git").exists():
            shutil.rmtree(dest, ignore_errors=True)

    if not dest.exists():
        cmd = ["git", "clone", "--quiet", "--depth", "1"]
        if branch:
            cmd += ["--branch", branch]
        cmd += [url, str(dest)]
        rc, out, err = run(cmd, timeout=timeout)
        if rc != 0:
            return False, f"git clone failed: {err or out}"
    else:
        # Best-effort fetch in place to keep the checkout fresh.
        rc, out, err = run(["git", "fetch", "--depth", "1", "--all", "--prune"], cwd=str(dest), timeout=timeout)
        if rc != 0:
            log(f"warn: fetch failed in {dest}: {err or out}")

    rc, head, err = run(["git", "rev-parse", "HEAD"], cwd=str(dest), timeout=timeout)
    if rc != 0:
        return False, f"git rev-parse failed: {err or out}"
    return True, head.strip()


# ------------------------------------------------------------------------------
# GitLab discovery (projects under a group)
# ------------------------------------------------------------------------------

def gitlab_iter_projects(
    base: str,
    group_path: str,
    token: Optional[str],
    include_subgroups: bool,
) -> Iterable[Dict]:
    """
    Yield project dicts under a GitLab group.

    Notes
    -----
    - Uses /api/v4/groups/{group_path} to resolve the group id, then
      paginates over /api/v4/groups/{id}/projects.
    - We request 'simple' project entries (lighter payload).
    """
    session = requests.Session()
    if token:
        session.headers["PRIVATE-TOKEN"] = token

    # Resolve group id by path (handles self-managed GitLab too)
    r = session.get(f"{base}/api/v4/groups/{group_path}")
    r.raise_for_status()
    gid = r.json()["id"]

    page = 1
    per_page = 100
    while True:
        params = {
            "per_page": per_page,
            "page": page,
            "include_subgroups": "true" if include_subgroups else "false",
            "simple": "true",
            "archived": "false",
            "with_shared": "false",
            "order_by": "path",
            "sort": "asc",
        }
        r = session.get(f"{base}/api/v4/groups/{gid}/projects", params=params)
        r.raise_for_status()
        items = r.json()
        if not items:
            break
        for pr in items:
            # pr contains fields like: http_url_to_repo, ssh_url_to_repo, path_with_namespace, default_branch, ...
            yield pr
        page += 1


# ------------------------------------------------------------------------------
# Cookbook discovery inside a repo
# ------------------------------------------------------------------------------

def sanitize_path(s: str) -> str:
    """
    Turn a repo URL into a stable filesystem-friendly string for dir layout.

    Example
    -------
    "https://gitlab.example.com/team/proj.git" ->
      "gitlab.example.com/team/proj.git"
    """
    s = s.replace("https://", "").replace("http://", "")
    return re.sub(r"[^A-Za-z0-9._/@:-]+", "_", s)


def find_cookbook_roots(repo_dir: Path, max_depth: int = 6) -> List[Path]:
    """
    Discover cookbook roots by looking for:
      metadata.rb  and  (recipes/ or resources/)
    within a bounded search depth (to avoid crawling huge monorepos).

    Returns
    -------
    List[Path] : sorted, unique paths to cookbook roots
    """
    roots: List[Path] = []
    for p in repo_dir.rglob("metadata.rb"):
        # Skip overly deep matches (defensive for giant repos)
        try:
            rel_parts = p.relative_to(repo_dir).parts
        except Exception:
            continue
        if len(rel_parts) > max_depth:
            continue

        base = p.parent
        if (base / "recipes").exists() or (base / "resources").exists():
            roots.append(base)

    # De-dup and sort by short paths first for deterministic processing
    uniq = sorted(set(roots), key=lambda x: (len(x.as_posix()), x.as_posix()))
    return uniq


# ------------------------------------------------------------------------------
# Extraction (call extractor.py for a single cookbook)
# ------------------------------------------------------------------------------

def extract_cookbook(
    extractor: Path,
    cookbook_root: Path,
    out_file: Path,
    timeout: int,
) -> Tuple[bool, str]:
    """
    Run the single-cookbook extractor and write JSON to `out_file`.

    Returns
    -------
    (ok, detail)
      ok=True  -> detail = extractor stdout (truncated by caller if needed)
      ok=False -> detail = error text (stderr or stdout)
    """
    out_file.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(extractor), "--cookbook", str(cookbook_root), "--out", str(out_file), "--summary"]
    rc, out, err = run(cmd, timeout=timeout)
    if rc != 0:
        return False, (err or out)
    return True, out


# ------------------------------------------------------------------------------
# Per-repo worker: clone, discover cookbooks, extract
# ------------------------------------------------------------------------------

def process_repo(
    repo_url: str,
    out_dir: Path,
    work_dir: Path,
    extractor: Path,
    clone_timeout: int,
    extract_timeout: int,
    overwrite: bool,
    branch: Optional[str] = None,
    dry_run: bool = False,
) -> Dict:
    """
    Process a single repo:
      - shallow clone (or fetch if already present)
      - discover cookbook roots
      - run extractor per cookbook (unless dry_run)
      - return a compact status dict for manifest/errors

    Notes
    -----
    The output JSON path is deterministic:
       out/<host>/<namespace>/<project>/<commit>/<cookbook_relpath>.json
    """
    t0 = time.time()
    host_ns_proj = sanitize_path(repo_url)
    repo_dir = work_dir / host_ns_proj

    ok, head_or_err = git_shallow_clone(repo_url, repo_dir, branch=branch, timeout=clone_timeout)
    if not ok:
        return {"repo": repo_url, "status": "clone_error", "error": head_or_err, "secs": round(time.time() - t0, 2)}

    commit = head_or_err
    cookbooks = find_cookbook_roots(repo_dir)
    if not cookbooks:
        return {"repo": repo_url, "status": "no_cookbooks", "commit": commit, "secs": round(time.time() - t0, 2)}

    results = []
    for root in cookbooks:
        # Relative path for helpful output tree
        try:
            rel = str(root.relative_to(repo_dir))
        except Exception:
            rel = root.name

        out_file = out_dir / host_ns_proj / commit / (rel + ".json")

        if dry_run:
            results.append({"cookbook": rel, "status": "dry_run"})
            continue

        if out_file.exists() and not overwrite:
            results.append({"cookbook": rel, "status": "skipped", "out": str(out_file)})
            continue

        ok, detail = extract_cookbook(extractor, root, out_file, timeout=extract_timeout)
        results.append({
            "cookbook": rel,
            "status": "ok" if ok else "extract_error",
            "out": str(out_file),
            # keep a small snippet of stdout/stderr for triage
            "detail": (detail or "").strip()[:4000],
        })

    return {
        "repo": repo_url,
        "status": "done",
        "commit": commit,
        "cookbooks": results,
        "secs": round(time.time() - t0, 2),
    }


# ------------------------------------------------------------------------------
# Main CLI
# ------------------------------------------------------------------------------

def main() -> None:
    """
    Orchestrate discovery + parallel extraction across many repositories.

    Two mutually exclusive input modes:
      --repos-file      (list of URLs)
      --group-path      (GitLab group path, optionally including subgroups)

    Writes:
      - out_dir/manifest.jsonl  : per-repo summary including cookbook statuses
      - out_dir/errors.jsonl    : clone/extract failures
      - out_dir/<...>.json      : per-cookbook facts (one file per cookbook)
    """
    ap = argparse.ArgumentParser(
        description="Bulk-run Chef facts extractor across many Git repos (GitLab-friendly)."
    )

    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--repos-file", help="Text file with one Git URL per line")
    src.add_argument("--group-path", help="GitLab group path (e.g., team/platform)")

    ap.add_argument("--gitlab-base", default="https://gitlab.com", help="GitLab base URL (default: https://gitlab.com)")
    ap.add_argument("--include-subgroups", action="store_true", help="Include subgroups when using --group-path")

    ap.add_argument("--out-dir", required=True, help="Directory to write JSON outputs + logs")
    ap.add_argument("--work-dir", required=True, help="Directory to clone repositories (can be reused)")
    ap.add_argument("--extractor", required=True, help="Path to extractor.py")

    ap.add_argument("--concurrency", type=int, default=8, help="Parallelism for clones/extractions")
    ap.add_argument("--clone-timeout", type=int, default=900, help="Seconds allowed for git clone (default 900)")
    ap.add_argument("--extract-timeout", type=int, default=600, help="Seconds allowed per cookbook extraction (default 600)")
    ap.add_argument("--overwrite", action="store_true", help="Overwrite existing outputs instead of skipping")
    ap.add_argument("--branch", default=None, help="Optional git branch to clone (default: repo default)")
    ap.add_argument("--limit", type=int, default=0, help="Process only the first N repos (0 = no limit)")
    ap.add_argument("--dry-run", action="store_true", help="Do not run extractor; just list discovered cookbooks")

    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    work_dir = Path(args.work_dir)
    extractor = Path(args.extractor)

    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = out_dir / "manifest.jsonl"
    errors_path = out_dir / "errors.jsonl"

    # --------------------------------------------------------------------------
    # Build the list of repos to process
    # --------------------------------------------------------------------------
    repos: List[str] = []
    if args.repos_file:
        for line in Path(args.repos_file).read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            repos.append(s)
    else:
        token = os.getenv("GITLAB_TOKEN", "")
        if not token:
            log("WARN: GITLAB_TOKEN not set; public/anonymous API access may be limited.")
        # Fetch projects via GitLab API
        for pr in gitlab_iter_projects(args.gitlab_base, args.group_path, token if token else None, args.include_subgroups):
            # Prefer https clone URL; fall back to SSH if needed
            url = pr.get("http_url_to_repo") or pr.get("ssh_url_to_repo")
            if url:
                repos.append(url)

    if args.limit and len(repos) > args.limit:
        repos = repos[:args.limit]

    log(f"Total repos to process: {len(repos)} (concurrency={args.concurrency})")
    started = time.time()

    # --------------------------------------------------------------------------
    # Thread pool to process repos in parallel
    # --------------------------------------------------------------------------
    def _work(url: str) -> Dict:
        try:
            return process_repo(
                repo_url=url,
                out_dir=out_dir,
                work_dir=work_dir,
                extractor=extractor,
                clone_timeout=args.clone_timeout,
                extract_timeout=args.extract_timeout,
                overwrite=args.overwrite,
                branch=args.branch,
                dry_run=args.dry_run,
            )
        except Exception as e:
            # Capture unexpected exceptions so the runner keeps going
            return {"repo": url, "status": "fatal_error", "error": repr(e)}

    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futs = [ex.submit(_work, u) for u in repos]
        for fut in cf.as_completed(futs):
            res = fut.result()

            # Clone-level or unexpected failure
            if res.get("status") in ("fatal_error", "clone_error"):
                write_jsonl(errors_path, res)
                log(f"ERR: {res.get('repo')} -> {res.get('status')}: {res.get('error')}")
                continue

            # Repo had no cookbooks; log to manifest for traceability
            if res.get("status") == "no_cookbooks":
                write_jsonl(manifest_path, res)
                log(f"NO-CKBK: {res.get('repo')} (commit {res.get('commit')})")
                continue

            # Normal completion: write a single summary line for the repo
            write_jsonl(manifest_path, res)

            # Friendly console summary per repo
            ok_count = sum(1 for c in res.get("cookbooks", []) if c.get("status") in ("ok", "skipped", "dry_run"))
            err_count = sum(1 for c in res.get("cookbooks", []) if c.get("status") == "extract_error")
            log(f"DONE: {res.get('repo')} cookbooks={ok_count}+{err_count} in {res.get('secs')}s")

    log(f"All done in {round(time.time() - started, 2)}s. "
        f"Manifest: {manifest_path}  Errors: {errors_path}")


if __name__ == "__main__":
    main()
