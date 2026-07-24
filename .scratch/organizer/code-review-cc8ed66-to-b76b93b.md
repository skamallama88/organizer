# Code Review — cc8ed66 to b76b93b

**Review date**: 2026-01-XX  
**Fixed point**: `cc8ed66` (Implement safe Organizer vertical slice)  
**HEAD**: `b76b93b` (Add ZIP archive action)

## Commits reviewed

- `23c5b49` Fix CLI check subcommand parsing
- `076eb4c` Document current Organizer usage
- `7adb273` Enforce watch and destination boundaries
- `e5b3854` Add rule revisions and named captures
- `29c4426` Add copy rename and action chains
- `0116dca` Add durable leases, completion skipping, and trigger processing
- `94b3fc1` Add collision suppression and explicit reprocessing
- `4fbed6d` Add safe delete and quarantine actions
- `b76b93b` Add ZIP archive action

## Tooling baseline

- `pytest`: 75 passed ✓
- `mypy src tests`: **5 errors** ✗ (see Standards #1)
- `ruff check`: clean ✓
- `git diff --check`: clean ✓

---

## Standards findings

References: `CONTEXT.md`, `docs/design/architecture.md`, ADRs 0001–0007, `pyproject.toml`

### S1. mypy strict failure [HIGH]

**Location**: `tests/test_item_processor.py:153, 154, 177, 214, 215`  
**Standard violated**: `pyproject.toml:28` configures `strict = true`, `files = ["src", "tests"]`

**Issue**: `attempts()` returns `list[dict[str, object]]`, so `resulting_paths = attempts[0]["resulting_paths"]` is typed as `object`. The code then calls `len(resulting_paths)`, indexes it, and passes it to `Path()`, all of which fail mypy's strict checks.

**Impact**: Documented tooling check is broken. Cannot enforce type safety guarantees.

**Fix**: Cast the return value or narrow the type of `attempts()` to return `list[dict[str, list[str]]]` for `resulting_paths`.

---

### S2. Divergent Change / Duplicated Code in execute() [MEDIUM]

**Location**: `src/organizer/item_processor.py:277-387`  
**Smell**: Duplicated Code, Divergent Change

**Issue**: The per-action block for `delete`/`quarantine`, `copy`, `archive`, and the move/rename fall-through each inline their own fingerprint re-checks, staging helpers, and UNCERTAIN result wiring. The copy and archive branches duplicate the exact `_validate_source` + staging-cleanup pattern (lines 339-353).

**Impact**: Maintenance burden. Four near-parallel action handlers make it easy to introduce inconsistencies (and indeed, some exist — see S5, Sp1).

**Note**: ADR-0003 ("planning and execution seam") endorses one executor, but the body has grown complex. Consider extracting action-specific execution strategies.

---

### S3. Web endpoint parameter style [MEDIUM]

**Location**: `src/organizer/web.py:23-25`  
**ADR violated**: ADR-0006 (trusted control boundary) — implicit binding for security-sensitive parameters

**Issue**: `save_rules(watch_id, rules_path: Path, expected_revision: str, body: bytes = Body(...))` uses bare `Path`/`str` parameters without `Query()` annotations. FastAPI coerces them into query params (the test passes), but the contract is implicit.

**Impact**: Fragile. A future FastAPI version could change parameter-binding behavior. Filesystem paths deserve explicit `Query()` annotation for clarity and security auditability.

**Fix**: Use `rules_path: Path = Query(...)` and `expected_revision: str = Query(...)`.

---

### S4. _fingerprint does full content I/O during planning [LOW]

**Location**: `src/organizer/item_processor.py:622-637`  
**Context**: `CONTEXT.md` defines "versioned fingerprint" as including content hash, so this is spec-faithful

**Issue**: Reads the entire file (and, recursively, every regular file under a directory) on every `plan()` call. `plan()` is invoked per-snapshot inside `process_batch` (line 922), which means a batch hashes every file twice (once in `plan`, once in `execute`).

**Impact**: Performance. For large watch folders with large files, planning dominates runtime. Not a correctness issue, but will surface as users scale.

**Mitigation**: Cache fingerprints across plan/execute for the same source identity, or accept the I/O cost as the price of safety.

---

### S5. Quarantine path uses post-chain plan.source [HIGH]

**Location**: `src/organizer/item_processor.py:327`  
**Spec violated**: `CONTEXT.md` (Quarantine): "preserves the original relative path from the watch root"

**Issue**: After a prior `rename`, `plan.source` is the renamed name, not the original. `relative = plan.source.relative_to(plan.watch_root)` computes the renamed relative path, so quarantine embeds `new_name.mkv` instead of `old_name.mkv`.

**Test mask**: `tests/test_item_processor.py:216` asserts `quarantined.name == "old_name.mkv"`, which is a substring of the *actual* stored name `new_name.mkv`. The assertion passes while the full path is wrong.

**Impact**: Quarantine paths do not preserve the original relative path as documented. Recovery workflows that rely on quarantine paths to reconstruct original item identity will fail.

**Fix**: Capture the original source path before any chain mutations and use it for quarantine relative-path computation.

---

### S6. Silent match → conditions fallback [LOW]

**Location**: `src/organizer/item_processor.py:550-553`  
**Smell**: Speculative Generality

**Issue**: When a rule has `match` but no `conditions`, the code silently wraps `match` into `{"match": match}`. The canonical key is `conditions` / named conditions (`docs/design/architecture.md:56-65`, `CONTEXT.md` "Named match condition"); the legacy `match` shape is undocumented.

**Impact**: Hides configuration drift. Users can write rules with the old `match` key and not realize they're using a deprecated fallback.

**Fix**: Either document the fallback or reject rules with `match` but no `conditions`.

---

## Spec findings

Primary source: `.scratch/organizer/spec.md`. Line quotes reference that file.

### Sp1. Pre-mutation fingerprint revalidation missing for non-destructive actions [CRITICAL]

**Location**: `src/organizer/item_processor.py:272-275`  
**Spec violated**: Line 87: "Execution performs internal preflight validation, **revalidates source and destination state immediately before each mutation**"

**Issue**:
```python
def _validate_plan_source(self, plan: Plan) -> None:
    has_destructive = self._has_destructive_action(plan)
    if not has_destructive:
        self._validate_source(plan)
```

`_validate_source` (which includes fingerprint) runs only when the plan contains `delete`/`quarantine`. Move, copy, rename, and archive never re-fingerprint the source before mutation.

**Impact**: If a file's content changes without altering size/mtime between `plan()` and `execute()`, the move is marked `completed` and `has_completed_attempt` will skip the now-modified file forever. Silent data corruption of the completion-skip invariant.

**Fix**: Always call `_validate_source(plan)` before execution, regardless of action type.

---

### Sp2. Archive output name does not match the spec [HIGH]

**Location**: `src/organizer/item_processor.py:511-516`  
**Spec violated**: Line 99: "Archive output names strip one recognized input archive extension before appending the requested output extension. For example, `project.zip` becomes `project.zip` for ZIP output and `project.7z` for 7z output."

**Issue**: Code produces `movie.mkv.zip`, not `movie.zip`, for `movie.mkv` → `.zip`. The test at `tests/test_item_processor.py:924` (`archive = destination / "movie.mkv.zip"`) asserts the *wrong* behavior.

**Impact**: Archive names are verbose and do not match user expectations or the spec.

**Fix**:
```python
@staticmethod
def _archive_output_name(source: Path, extension: str) -> str:
    recognized = {".zip", ".7z", ".rar"}  # spec-recognized only
    suffix = source.suffix.lower()
    stem = source.name[:-len(suffix)] if suffix in recognized else source.name
    return stem + extension
```

Update test to expect `movie.zip`.

---

### Sp3. preserve_originals default is inverted [HIGH]

**Location**: `src/organizer/item_processor.py:215`  
**Spec violated**: Line 98: "Originals are removed only after successful archiving **unless `preserve_originals` is enabled**"

**Issue**: `preserve_original = archive.get("preserve_originals", True)` — default is True (preserve), opposite of the spec. The test at `tests/test_item_processor.py:919` uses the default and asserts `item.exists()`, which is consistent with the *implementation* but contradicts the spec.

**Impact**: Users who omit `preserve_originals` get the opposite behavior from what the spec promises.

**Fix**: Change default to `False`:
```python
preserve_original = archive.get("preserve_originals", False)
```

Update tests that rely on the wrong default.

---

### Sp4. 7z archive output not implemented [HIGH]

**Location**: `src/organizer/item_processor.py:502`  
**Spec violated**: Line 98: "supports ZIP and 7z output"

**Issue**: Code uses `zipfile.ZipFile` unconditionally. `format: 7z` in YAML produces a `.7z`-named file that is actually ZIP bytes. The YAML schema accepts `format` (spec line 98) but the code never reads it.

**Impact**: Users requesting 7z output get invalid archives (ZIP format with 7z extension).

**Fix**: Either implement 7z output using `py7zr` (already a dependency per ADR-0001) or reject `format: 7z` with a clear error. The README at `.scratch/organizer/README.md:36` says "Archive and unarchive actions" are "Not implemented yet," which contradicts the fact that archive *is* implemented (just ZIP-only). Update the README to clarify: "Archive action (ZIP only)" or implement 7z.

---

### Sp5. Copy does not update the chain's current item [HIGH]

**Location**: `src/organizer/item_processor.py:338-346`  
**Spec violated**: Line 166 (Primary resulting item): "The single item produced by an action and **used as the next action's input**"

**Issue**: After copy, `source` (the chain cursor) is not reassigned to `action.target`. A subsequent `rename` therefore operates on the original source, not the copy.

**Test mask**: The test at `tests/test_item_processor.py:848-852` asserts this behavior (`(destination / "renamed.mkv")` does NOT exist), so the bug is encoded as passing behavior.

**Impact**: Action chains with copy + subsequent mutations do not behave as documented. The README says "ordered action chains using the primary resulting item," which implies copy should update the chain cursor.

**Fix**: Add `source = action.target` after the copy block. Update the test to reflect correct chain semantics.

**Note**: This may require rethinking the test `test_action_chain_uses_primary_result_and_stops_after_failure`. If copy updates the cursor, then rename applies to the copy, and delete removes the copy — leaving only the original source. The test's current assertion (`assert not (destination / "renamed.mkv").exists()`) would fail. Clarify the intended semantics: does copy produce a new primary item, or does it leave the original as the chain cursor?

---

### Sp6. Unarchive action absent [MEDIUM]

**Location**: Not implemented  
**Spec violated**: Lines 101–104

**Issue**: Spec requires ZIP/7z/RAR extraction, bounded preview, staged extraction, traversal/symlink protection, password classification. None of it is implemented.

**Impact**: Major feature gap. The README notes this (`README.md:36`), so it's a known gap rather than a regression.

**Status**: Deferred. Document in README as "not yet implemented" (already done).

---

### Sp7. Quarantine relative path uses the post-rename source [HIGH]

**Location**: `src/organizer/item_processor.py:327`  
**Spec violated**: Line 89 / `CONTEXT.md` (Quarantine): "preserves the original relative path from the watch root"

**Issue**: After a rename, `plan.source.relative_to(plan.watch_root)` produces the renamed name. Quarantine embeds `new_name.mkv` instead of `old_name.mkv`.

**Test mask**: The test at `tests/test_item_processor.py:216` (`assert quarantined.name == "old_name.mkv"`) is a substring-level coincidence that hides the defect. The full path is `quarantine/downloads/<attempt_id>/subdir/new_name.mkv`, not `.../old_name.mkv`.

**Impact**: Quarantine paths do not preserve the original relative path. Recovery workflows that rely on quarantine paths to reconstruct original item identity will fail.

**Note**: This is the same issue as S5, but framed as a spec violation rather than a standards violation.

---

### Sp8. Stale README.md claims [LOW]

**Location**: `.scratch/organizer/README.md:9-42`

**Issue**: 
- Line 36: "Not implemented yet: … Archive and unarchive actions" — actually only ZIP archive is implemented, no unarchive
- Line 9: "Implemented: …" does not list archive, but archive *is* partially implemented

**Impact**: User-facing documentation is stale. Users cannot determine what is actually implemented.

**Fix**: Update the README to clarify:
- "Implemented: … ZIP archive action (7z and unarchive not yet implemented)"
- Move "Archive (ZIP only)" to the "Implemented" section
- Keep "Unarchive" in "Not implemented yet"

---

## Test findings

### T1. Weak assertions mask bugs [HIGH]

**Locations**: 
- `tests/test_item_processor.py:216` — `assert quarantined.name == "old_name.mkv"` (substring coincidence)
- `tests/test_item_processor.py:848-852` — asserts buggy copy-chain behavior

**Issue**: Tests assert partial or incorrect behavior, masking the defects described in S5/Sp7 and Sp5.

**Impact**: False confidence. The test suite passes while the implementation violates the spec.

**Fix**: Strengthen assertions to check full paths and correct chain semantics.

---

### T2. Inline sqlite3 imports in tests [LOW]

**Locations**: `tests/test_item_processor.py:1113, 1145, 1175, 1205, 1235`

**Issue**: Tests import `sqlite3` inline (`import sqlite3`) rather than at the module level.

**Impact**: Minor style issue. No functional impact.

**Fix**: Move `import sqlite3` to the top of the test file.

---

## Summary

**Total findings**: 16  
- Standards: 6 (1 critical, 2 high, 2 medium, 1 low)
- Spec: 8 (1 critical, 4 high, 1 medium, 2 low)
- Tests: 2 (1 high, 1 low)

**Worst issue**: `_validate_plan_source` (`src/organizer/item_processor.py:272-275`) skips fingerprint revalidation for non-destructive actions, violating spec line 87 and silently corrupting the completion-skip invariant.

**Recommended priority**:
1. Sp1: Fix pre-mutation fingerprint validation (silent data corruption)
2. S1: Fix mypy errors (broken tooling)
3. S5/Sp7: Fix quarantine relative-path computation
4. Sp5: Clarify copy-chain semantics and fix implementation
5. Sp2/Sp3/Sp4: Fix archive naming, defaults, and 7z support
6. T1: Strengthen test assertions
7. S2-S4, S6, Sp6, Sp8, T2: Lower-priority cleanup
