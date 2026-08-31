# ASTAR Middleware Workspace Cleanup Design

**Date:** 2026-07-29  
**Status:** Implemented and locally verified; remote delivery pending  
**Target:** `/Volumes/Backup/astar-middleware-main` and `mrz-1713/astar-middleware` branch `main`

## Goal

Turn the current working directory into a maintainable ASTAR middleware repository without changing product scope. Preserve the middleware, DaVinci simulator, SPTS/PTIQ profiles, tests, operator documentation, offline deployment inputs, and final machine-profile assets. Remove generated copies, caches, scratch work, stale deployment snapshots, unreferenced screenshots, runtime logs, and redundant source mirrors.

## Safety and Git Baseline

The workspace currently has no Git metadata. Before deletion, first move the operational production configuration to its ignored local filename, create the sanitized public template, then initialize Git against `origin/main` without overwriting the working tree. Create a local-only `pre-cleanup-local` safety branch containing the current source state but no operational credentials. Generated and ignored artifacts are intentionally excluded because they are reproducible. The cleaned `main` will be a new orphan root commit so the force-pushed branch does not retain the prior or safety-snapshot ancestry. The remote `main` branch is public and unprotected, but every configured GitHub account currently has only `READ` permission; remote delivery remains blocked until one account receives `WRITE` permission.

Force-push authorization applies only to `https://github.com/mrz-1713/astar-middleware.git`, branch `main`. No other branches or repositories are in scope.

## Approved Deletion Set

Delete these reproducible or irrelevant targets:

- `.venv/`;
- `.pytest_cache/`;
- `tmp/`;
- `deploy_out/`;
- every `__pycache__/`, `*.pyc`, and `*.pyo` under the repository;
- every `.DS_Store` under the repository;
- `packaging/secsgem_simulator/logs/`;
- `deploy/source/`, because the deployment builder stages current source directly and does not consume this stale mirror;
- `output/playwright/`, which contains generated visual-QA screenshots;
- root-level unreferenced screenshots `DaVinci_28.png`, `Software_127.png`, and `Software_129.png`;
- `python-manager-26.2.msix`, which is unrelated to the supported offline Python installer and self-contained simulator packaging.

Preserve `output/docx/DaVinci_200_to_ASTAR_Middleware_Connection_Setup.docx` as a final editable operator deliverable. Preserve `output/davinci200_mc4_hc1/` because runtime profiles and deployment packaging consume it.

## Credential and Public-Repository Policy

The current production YAML contains credential-like literals and internal network coordinates. Before Git staging:

1. preserve the current operational file locally as `config/production.local.yaml`;
2. ignore `config/production.local.yaml`;
3. replace tracked `config/production.yaml` with a sanitized template containing disabled upstream transports, empty tokens, disabled machines, documentation-only TEST-NET IP addresses, and placeholder network shares;
4. ensure deployment staging excludes local configuration files; and
5. run a path/value-pattern secret scan without printing secret values.

Because a credential-like value is already present in the public remote history, the operator should revoke/rotate it independently. Force-pushing cleaned content does not guarantee immediate deletion of unreachable Git objects or third-party clones.

## Redundancy and Tooling Changes

- Add `testpaths = ["tests"]` to Pytest configuration so generated deployment copies cannot be collected as tests.
- Expand `.gitignore` for `.build/`, `artifacts/`, simulator logs, visual-QA output, local production configuration, and Python installer-manager downloads.
- Change `scripts/build_deploy_package.sh` to stage only runtime-required machine-profile output instead of copying the entire `output/` tree.
- Keep historical design documents and vendor manuals; they contain requirements and operational evidence rather than generated runtime copies.
- Do not delete Python modules, supported vendor profiles, tests, or public APIs based only on static non-reference guesses.

## Verification

After cleanup:

1. confirm every approved target is absent and every preserved target exists;
2. run `python -m pytest -q tests`;
3. run the focused DaVinci packaging and real-socket tests;
4. validate both simulator YAML templates and the sanitized production template under documented setup conditions;
5. run a secret-pattern scan and inspect only file paths/classifications;
6. run CodeRabbit against the cleaned diff;
7. report before/after size and file-count measurements; and
8. create a cleanup commit.

After GitHub write permission is available, force-push the reviewed cleanup commit to `origin/main` and verify the remote tree and Windows workflow state.

## Recovery

Tracked source and documentation can be restored from the local-only `pre-cleanup-local` branch or the existing remote commit `8a8992f755a4f26e3b07bb3d3e195a1f6d7e66d3`. The safety branch must never be pushed. Deleted cache, virtual-environment, temporary render, deployment-output, and test-output files are regenerated by their owning tools and are not backed up.
