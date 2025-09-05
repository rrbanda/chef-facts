# Chef Facts Extractor

Deterministic, parser-based extraction of **Chef Infra** facts from cookbooks.
No LLM required. Emits JSON with file\:line citations and a coverage summary.

* **Single cookbook** → `bin/extract <cookbook>` (auto-writes to `out/<cookbook>.json`)
* **Bulk across repos** → `batch_runner.py` (auto-names under `out/<host>/<ns>/<project>/<commit>/<cookbook>.json`)
* **Validate** → `bin/check_coverage` (reads every JSON under `out/`)

## Repo layout

```
chef-facts/
├─ extractor.py           # single-cookbook extractor (no manual filenames required via bin/extract)
├─ batch_runner.py        # bulk runner (GitLab group or repos.txt; auto-names outputs)
├─ bin/
│  ├─ extract             # wrapper: python extractor -> out/<cookbook>.json
│  └─ check_coverage      # validates all JSON under out/**/*
├─ requirements.txt
├─ Dockerfile             # optional
└─ README.md
```

> The two tiny scripts in `bin/` are included so you **never** have to type an output filename.

---

## Quick start (no manual filenames)

### 0) Prereqs

* Python 3.9+
* `git` (for cloning cookbooks)
* (Optional) Docker

### 1) Install

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
chmod +x bin/extract bin/check_coverage
```

> We pin Tree-sitter for stability:
>
> ```
> tree_sitter==0.20.4
> tree_sitter_languages==1.10.2
> requests>=2.31
> ```

### 2) Single cookbook (no filenames, ever)

```bash
# Example: clone a couple public cookbooks
test -d apache2 || git clone https://github.com/sous-chefs/apache2.git
test -d chef-os-hardening || git clone https://github.com/dev-sec/chef-os-hardening.git

# Extract (auto-writes to out/<cookbook>.json and prints a summary)
./bin/extract apache2
./bin/extract chef-os-hardening
```

### 3) Validate coverage (no filenames)

```bash
./bin/check_coverage
# prints OK / NOT OK; warns on edge cases
```

---

## Run in bulk (GitLab group or a file of repos)

**Option A — GitLab group** (auto-names all outputs)

```bash
export GITLAB_TOKEN=glpat_xxx   # needs read_api + read_repository
python batch_runner.py \
  --gitlab-base https://gitlab.example.com \
  --group-path my-dept/chef-cookbooks \
  --include-subgroups \
  --out-dir out \
  --work-dir work \
  --concurrency 24 \
  --extractor ./extractor.py

# Validate everything produced
./bin/check_coverage
```

**Option B — From a file of repos**

```bash
# repos.txt: one git URL per line
python batch_runner.py \
  --repos-file repos.txt \
  --out-dir out \
  --work-dir work \
  --concurrency 16 \
  --extractor ./extractor.py

./bin/check_coverage
```

**Where outputs go (auto-named)**

* Single cookbook via wrapper: `out/<cookbook>.json`
* Bulk runner: `out/<host>/<namespace>/<project>/<commit>/<cookbook_relpath>.json`
* Batch logs:

  ```
  out/manifest.jsonl   # per-repo summary with cookbook statuses
  out/errors.jsonl     # clone/extract failures
  ```

---

## What the JSON contains

* `recipes[]` with resources, includes, attributes read, and **template enrichment**:

  * Vars from `variables(...)`
  * ERB `@instance_vars`
  * `node[...]` used inside ERB
* `custom_resources[]` with `provides`, `properties`, and `actions[]` (action blocks parsed like recipes)
* `meta.coverage` with counts + notes (e.g., dynamic includes, unknown titles, file counts)

> If a cookbook uses templates **inside custom-resource actions**, the *recipe-level* `templates` count can be 0 and still be correct.

---

## Validation tests (filename-free)

All tests below operate on **every** JSON under `out/`—no hardcoded names.

### A) Coverage overview for all outputs

```bash
python - <<'PY'
import json,glob
for f in sorted(glob.glob("out/**/*.json", recursive=True)):
    d=json.load(open(f))
    cov=(d.get("meta") or {}).get("coverage") or {}
    print(f, {k: cov.get(k) for k in [
        "recipes","resources_total","custom_resources","properties_total",
        "templates_total","dynamic_includes_total","unknown_names_without_expr"
    ]})
PY
```

### B) Dynamic title handling (`name_expr`) check

```bash
python - <<'PY'
import json,glob
unknown_wo=with_expr=0
for f in glob.glob("out/**/*.json", recursive=True):
    d=json.load(open(f))
    for r in d.get("recipes", []):
        for x in r.get("resources", []):
            if (x.get("name") in (None,"?")) and x.get("name_expr"): with_expr+=1
            if (x.get("name") in (None,"?")) and not x.get("name_expr"): unknown_wo+=1
print("unknown_without_expr:", unknown_wo, "now_with_name_expr:", with_expr)
PY
```

### C) Dynamic `include_recipe` targets (if any)

```bash
python - <<'PY'
import json,glob
for f in glob.glob("out/**/*.json", recursive=True):
    d=json.load(open(f))
    for r in d.get("recipes", []):
        if r.get("includes_dynamic"):
            print(f, "=>", r["file"], ":", r["includes_dynamic"])
PY
```

### D) Template enrichment sanity check (counts across all)

```bash
python - <<'PY'
import json,glob
tmpl=with_vars=0
for f in glob.glob("out/**/*.json", recursive=True):
    d=json.load(open(f))
    for r in d.get("recipes", []):
        for t in r.get("templates", []):
            tmpl+=1
            if t.get("vars"): with_vars+=1
print("templates found:", tmpl, "with vars:", with_vars)
PY
```

### E) Custom-resource property names resolved

```bash
python - <<'PY'
import json,glob
missing=0
for f in glob.glob("out/**/*.json", recursive=True):
    d=json.load(open(f))
    for cr in d.get("custom_resources", []):
        for p in cr.get("properties", []):
            if p.get("name") is None: missing+=1
print("Unresolved properties:", missing)
PY
```

### F) Simple green-gate (pass/fail) for CI

```bash
./bin/check_coverage
# exits 0 on OK; 2 on NOT OK
```

---

## CI examples

**GitLab CI** (single job, minimal)

```yaml
stages: [extract]

extract:
  image: python:3.11-slim
  script:
    - apt-get update && apt-get install -y --no-install-recommends git ca-certificates
    - python -m venv venv && . venv/bin/activate
    - pip install -r requirements.txt
    - chmod +x bin/extract bin/check_coverage
    # run bulk
    - python batch_runner.py --gitlab-base $GITLAB_BASE --group-path $GROUP_PATH --include-subgroups --out-dir out --work-dir work --concurrency 24 --extractor ./extractor.py
    # validate
    - ./bin/check_coverage
  artifacts:
    when: always
    paths:
      - out/
```

---

## Performance & scale tips

* Use fast SSD for `--work-dir`
* Tune `--concurrency` (typical 16–48)
* Reuse `work/` between runs for incremental updates
* Slice very large orgs by subgroup or `--limit`
* Adjust timeouts: `--clone-timeout`, `--extract-timeout`

---

## Docker (optional)

Build:

```bash
docker build -t chef-facts:latest .
```

Run single cookbook (auto-named output):

```bash
docker run --rm -it -v "$PWD:/data" -w /data chef-facts:latest ./bin/extract apache2
```

Run bulk:

```bash
docker run --rm -it -v "$PWD:/data" -w /data chef-facts:latest \
  python batch_runner.py --repos-file repos.txt --out-dir out --work-dir work --concurrency 16 --extractor ./extractor.py && \
  ./bin/check_coverage
```

---

## Troubleshooting

* **Parser init error** → ensure pinned versions in `requirements.txt` are installed in your venv.
* **“No cookbooks found”** → cookbook discovery requires `metadata.rb` plus `recipes/` or `resources/`.
* **Few/zero facts** → check `meta.coverage.notes`; many cookbooks push logic into custom resources.
* **Dynamic includes** are captured under `includes_dynamic` because targets are computed at runtime.

---


### Why you never type output filenames

* **Single cookbook**: `bin/extract` derives `out/<cookbook>.json` automatically.
* **Bulk**: `batch_runner.py` derives `out/<host>/<namespace>/<project>/<commit>/<cookbook_relpath>.json` automatically.
* **Validation**: `bin/check_coverage` scans `out/**/*.json`—no filenames needed.
