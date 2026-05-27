# Agent Context: doppelxyz/ariadne-codegen fork

This is a **performance-focused fork** of
[mirumee/ariadne-codegen](https://github.com/mirumee/ariadne-codegen).
It is not intended to diverge on features — the goal is to stay close to
upstream and periodically rebase/merge upstream changes.

## Why this fork exists

The monorepo runs `ariadne-codegen` to regenerate the GraphQL client in
`backend/functions/graphql_client/` (async) and
`backend/functions/graphql_client_sync/` (sync). With the production
schema (~500+ operations), a cold codegen run took **60–90 seconds**.
The bottleneck was two things:

1. **Sequential operation processing** — each operation's result-type AST
   was built one after the other on a single core, with no parallelism.
2. **O(n²) regex in `format_multiline_strings`** — on the 1.4 MB
   `client.py`, the pattern `r".*?=.*?('.*?'\s*){2,}"` caused catastrophic
   backtracking (4+ seconds) even when there were zero matches.

## What was changed (and why)

### 1. `ariadne_codegen/client_generators/package.py` — parallel codegen

Added `parallel_compute_operations()` (called from `main.py`) that fans
out `_compute_operation()` across forked worker processes using
`ProcessPoolExecutor(mp_context="fork")`.

**Why fork and not threads or spawn?**
- Threads are GIL-bound for CPU work — no speedup.
- Spawn requires pickling the full `PackageGenerator` including the
  `GraphQLSchema` (~MB of data) across a pipe — too slow.
- Fork is copy-on-write: the child inherits the entire process image
  instantly. Only the `OperationDefinitionNode` arg (~8 KB) and the
  returned `dict` cross the process boundary via pickle.

**Critical constraint — plugins use a sequential fallback:**
Plugin hooks like `generate_result_class`, `generate_operation_str`, and
`generate_fragments_module` mutate plugin-internal state (e.g.
`ShorterResultsPlugin.class_dict`, `ExtractOperationsPlugin._operations_variables`).
These hooks are called inside `_compute_operation` (child process), but
the parent needs that state in `generate_client_module` /
`generate_client_method`. Fork does not propagate mutations from child
back to parent, so plugins silently produce wrong output.

**Fix:** `parallel_compute_operations` falls back to sequential when:
- `queries` is empty (avoids `max_workers=0` ValueError)
- `plugin_manager.plugins` is non-empty (any active plugin)
- Running on Windows (no fork)

The production config (`pyproject.async.toml`) currently only uses
`ClientForwardRefsPlugin` and `NoReimportsPlugin`, so it hits the plugin
fallback. If those plugins are ever refactored to be stateless (or moved
out of the critical path), the sequential guard can be relaxed.

### 2. `ariadne_codegen/utils.py` — fix O(n²) regex

`format_multiline_strings` converts adjacent implicit string literals
(`'a\n''b\n'`) into triple-quoted strings. The old regex
`r".*?=.*?('.*?'\s*){2,}"` backtracked catastrophically on long
non-matching lines (every line in a large `client.py`).

Two fixes:
- **Fast-path exit**: `if "''" not in source: return source` —
  adjacent string literals always produce `''` in `ast.unparse` output;
  absence guarantees no work needed.
- **Non-backtracking pattern**: `r"[^=\n]+=.*?('.*?'\s*){2,}"` — the
  `[^=\n]+` anchor prevents the `.*?` from backtracking across `=` signs.

### 3. `ariadne_codegen/utils.py` — batch ruff formatting

Added `ast_to_raw_str()` (emit unformatted code) and `batch_format_files()`
(run ruff once over all generated files). Previously each file called
`ast_to_str()` which spawned a separate `ruff` subprocess. With 500+
operations that was 500+ `ruff` invocations; batching them into 2–3 total
subprocess calls saves several seconds.

### 4. `.github/workflows/release-pex.yml` — versioned PEX releases

Builds platform-specific PEX executables (linux-x86_64, linux-arm64,
macos-arm64) on every push to `main`. Releases are tagged `v{run_number}`
(e.g. `v42`) and marked pre-release. Old releases beyond the newest 5 are
pruned automatically.

Consumers use `tools/bin/ariadne-codegen` in the monorepo, which is a
[dotslash](https://github.com/facebook/dotslash) manifest pointing at the
appropriate platform tarball from the latest release.

## How to consume the fork in the monorepo

The monorepo uses the fork via dotslash. When a new PEX is released:

1. Note the new tag (e.g. `v42`) and the three platform URLs from the
   GitHub release page.
2. For each platform, get the `sha256` digest and byte `size` from the
   release workflow's "Print dotslash checksums" step output.
3. Update `tools/bin/ariadne-codegen` (the dotslash JSON manifest) with
   the new tag URLs, sizes, and digests.

The Makefile target that invokes codegen:

```bash
# from backend/functions/
make ariadne-async   # regenerates graphql_client/ from pyproject.async.toml
make ariadne-sync    # regenerates graphql_client_sync/ from pyproject.toml
```

Both targets call `tools/bin/ariadne-codegen` with
`PYTHONPATH=dev_tools/ariadne_plugins/src PEX_INHERIT_PATH=fallback`
so that the `ariadne-plugins` package (containing `ClientForwardRefsPlugin`)
is available at runtime without being bundled into the PEX.

## How to benchmark locally

```bash
# Build the PEX from the fork checkout
cd .local/ariadne-codegen
pip install "pex>=2.20,<3"
pex . \
  --interpreter-constraint "CPython==3.11.*" \
  --python-shebang "/usr/bin/env python3" \
  -m ariadne_codegen \
  --no-emit-warnings --compress --inherit-path=fallback --compile \
  -o /tmp/ariadne-codegen-dev
chmod +x /tmp/ariadne-codegen-dev

# Run against the async config
cd backend/functions
PYTHONPATH=dev_tools/ariadne_plugins/src PEX_INHERIT_PATH=fallback \
  /tmp/ariadne-codegen-dev client --config pyproject.async.toml

# Compare against current production binary
time PYTHONPATH=dev_tools/ariadne_plugins/src PEX_INHERIT_PATH=fallback \
  tools/bin/ariadne-codegen client --config backend/functions/pyproject.async.toml
```

Diff the output directories to confirm correctness:
```bash
diff -rq /tmp/graphql_client_old /tmp/graphql_client_new
```

## How to run the fork's own test suite

The fork uses `hatch` for test orchestration. If you don't have hatch,
install it with `pip install hatch` or use `uvx hatch`.

```bash
cd .local/ariadne-codegen
hatch test            # all Python versions in the matrix
hatch test -py 3.11   # single version
hatch run lint        # ruff + ty static analysis
```

The CI matrix runs 3.10–3.14. Tests live in `tests/` and the important
integration tests are in `tests/main/test_main.py` — these run the full
codegen pipeline against fixture schemas and compare output to golden
files in `tests/main/clients/*/expected_client/`.

## How to rebase from upstream

The fork tracks `mirumee/ariadne-codegen` main. To pull in upstream changes:

```bash
cd .local/ariadne-codegen
git remote add upstream https://github.com/mirumee/ariadne-codegen.git  # once
git fetch upstream
git rebase upstream/main

# Conflicts to watch for:
# - ariadne_codegen/utils.py        (format_multiline_strings fast-path + batch ruff)
# - ariadne_codegen/client_generators/package.py  (parallel_compute_operations)
# - ariadne_codegen/main.py         (parallel_compute_operations call site)
# - pyproject.toml                  (ruff added as a direct dependency)
# - .github/workflows/release-pex.yml  (new file, no upstream conflict)
```

After rebasing:
1. Run the test suite (`hatch test -py 3.11`) to check for regressions.
2. Update golden fixtures if upstream changed expected output:
   `hatch run update-snapshots` or regenerate manually.
3. Re-verify the parallel vs sequential correctness by running
   `make ariadne-async` and diffing against the old output.
4. Build a new PEX and update the dotslash manifest in the monorepo.

## Key invariants to preserve

- **Plugins must always use the sequential path.** If you add or change
  parallelism, ensure `has_plugins` check remains in
  `parallel_compute_operations`. Any plugin that calls a hook during
  `_compute_operation` and relies on that state in `generate_client_module`
  or `generate_client_method` will silently produce wrong output without it.
- **`format_multiline_strings` must stay O(n).** The `[^=\n]+` non-backtracking
  prefix and the `if "''" not in source` fast-path are both needed for
  correctness on large files.
- **Batch ruff calls.** The `_write_generated_file` method queues files for
  batch formatting in `generate()`. Do not reintroduce per-file `ast_to_str()`
  calls in hot paths.
