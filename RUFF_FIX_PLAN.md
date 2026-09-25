# Ruff cleanup implementation plan for GPT-6 Luna

This is an implementation handoff, not an implementation. Work through one numbered task per turn, in order. Each task is deliberately small enough to review and validate before continuing. Only this document was created during planning.

## Measured baseline

Measured on the current working tree, September 24, 2026, using the existing `.venv` on macOS with Python 3.14.5:

| Check | Result |
| --- | --- |
| `ruff check .` | 38 diagnostics, all `C901`, all complexity **11 > 10**; none autofixable |
| `ruff format --check .` | 78 files already formatted |
| `pyright` | 0 errors, 0 warnings, 0 informations |
| `pyrefly check` | 0 errors; reports 1 existing suppression and 60 warnings not shown |
| `ty check` | All checks passed |
| Full pytest suite with package coverage | **1,534 passed**, **80.0016%** combined statement/branch coverage |

Installed versions: Ruff 0.16.7, Pyright 1.1.414, Pyrefly 1.3.1, ty 0.0.82. Reproduce the baseline before implementation; these measurements are not a promise about a future checkout.

The sole pre-existing tracked modification was `pyproject.toml`: `max-complexity` changed from 11 to **10**. Preserve this user change. Do not restore 11. The current Ruff rule selection, ignores, and exclusions define the scope of this cleanup. “All errors” means zero diagnostics from the repository-wide configured `ruff check .`, including tests and entry points. It does not mean enabling currently ignored rules or bringing excluded backends into scope.

## Non-negotiable implementation rules

- Read applicable `AGENTS.md` instructions and `git status --short` before each task. Preserve all unrelated changes. Do not update dependencies, `uv.lock`, lint/type-checker configuration, coverage settings, or CI to obtain passing checks.
- Add no `noqa`, type-checker ignore, file-level suppression, exclusion, skip, xfail, diagnostic downgrade, or complexity-limit increase. Existing suppressions are not permission to add more. Avoid extraction sites that require duplicating suppression comments. Do not hide functions from analysis with dynamic code.
- Refactor behavior-preservingly. Keep public signatures, return sentinels, dictionary mutations, translated messages, confirmation defaults, callback timing, hardware command order, timeout/retry budgets, and cleanup behavior. Distinguish `False`, `None`, zero, and byte counts with the existing identity/type checks.
- Extract a cohesive responsibility containing actual branches. Merely moving a condition into a Boolean helper or replacing `if` chains with `match` does not necessarily reduce C901. A helper that inherits complexity 11 is not a fix. Run Ruff to measure both caller and helper; every new function must meet the current rules, including argument-count and annotation rules.
- Prefer ordinary, named private methods over nested closures, dynamic `getattr` dispatch, or a framework of callbacks. A small typed result is appropriate when several related outputs cross a boundary; do not create a large generic context object to conceal unrelated parameters.
- Annotate new boundaries accurately. Reuse existing `ProgressPayload`, `ProgressUpdate`, `ProgressState`, `ProgressSignal`, device result aliases, mapper protocols, header types, and `_Save…` / `_Flash…` / `_ROM…` records. Preserve optionality and literal sentinels. Use runtime narrowing before access. Do not add `Any`, widen types to `object`, or introduce unchecked casts to silence errors; existing legacy annotations need not be broadly rewritten.
- Narrowing inside a predicate helper does not automatically narrow the caller. Return a properly typed validated value or retain the caller's narrowing. Keep type-only imports under the project's existing `TYPE_CHECKING` convention when appropriate.
- Use existing tests first. Add or strengthen regression tests only where the moved behavior is insufficiently protected; assert externally meaningful bytes, order, results, events, or UI state. Keep refactored methods and their extracted helpers real in integration tests. Do not replace assertions with “helper called once.”
- Keep `tests/conftest.py::prevent_real_hardware`. Reuse `tests/fakes.py` and existing Qt fixtures. No real serial connections, firmware writing, downloads, normal user configuration writes, or application launch. Use temporary files, finite response queues, and controlled clocks for loops.
- If a test exposes an unrelated defect, record it separately rather than silently changing the contract in this cleanup. Do not endorse incorrect behavior in new tests.

## Ordered tasks and complete diagnostic inventory

Line numbers are baseline navigation hints; use function names after edits. The proposed extraction boundaries are grounded in the current code but can be adjusted if a smaller, clearer extraction passes the same checks. The expected remaining count assumes no intervening changes and zero new diagnostics.

### 1. Duration formatting and progress accounting — 2 errors; 36 remain

**Targets:** `Formatter.py::progress_time` (48), `Progress.py::_handle_position_event` (238).

- Move duration-component formatting into a typed helper, or move the unlocalized translation functions to module scope. Preserve literal translation messages and plural interpolation. Nested function decisions currently contribute to the enclosing function's complexity; verify the actual reduction with Ruff.
- Extract speed/ETA accounting from `_handle_position_event` into a method using `ProgressEvent` and `ProgressState`. Keep event rejection, position updates, emission throttling, and final emission order intact. Do not alter when speed timestamps advance or how skipping affects samples.

**Validation:** `tests/test_formatter.py`, `tests/test_progress.py`, `tests/test_i18n.py`. Cover negative/zero/fractional durations, localization and pluralization, mismatched READ/WRITE events, repeated positions, forced emission, warmup, and sector-time ETA.

### 2. CLI progress and camera backup completion — 2 errors; 34 remain

**Targets:** `FlashGBX_CLI.py::_HandleProgressAction` (708), `_FinishBackupRAM` (771).

- Split terminal progress actions (`FINISHED` and `ABORT`) from ordinary progress rendering using a typed handled/not-handled helper; keep unknown actions as no-ops and only finish once.
- Extract the multi-roll camera-picture export path, including its destination validation and nested export loops. Return an explicit outcome so an invalid destination still sets `RETVAL = 1` and prevents the backup-complete message. Preserve single-roll behavior and palette handling.

**Validation:** `tests/test_flashgbx_cli.py`; synthetic camera data and temporary paths. Verify action outputs, abort/finish effects, export filenames and roll offsets, and no completion output on destination rejection.

### 3. CLI save confirmations and dispatch — 3 errors; 31 remain

**Targets:** `FlashGBX_CLI.py::_ConfirmSaveAction` (2126), `BackupRestoreRAM` (2526), `_ConfirmBatterylessFlashWrite` (2712).

- Extract one complete confirmation branch from `_ConfirmSaveAction`, such as backup overwrite handling, with a Boolean continuation result. Preserve prompts, blank lines, default-no behavior, and overwrite bypass.
- Extract the final transfer dispatch from `BackupRestoreRAM` after its configuration, calibration, and file-access gates. Use a small typed request record if necessary to avoid excessive parameters. Keep buffered calibration restore semantics and batteryless routing unchanged.
- Extract the voltage-warning confirmation block from `_ConfirmBatterylessFlashWrite`; keep it after restore/erase confirmation and file validation.

**Validation:** `tests/test_flashgbx_cli.py`. Test yes/no/overwrite paths, inaccessible restore files, every action's transfer payload, calibration buffers, and batteryless voltage gates with no transfer after refusal.

### 4. GUI path opening, discovery, and completion — 4 errors; 27 remain

**Targets:** `FlashGBX_GUI.py::OpenPath` (1998), `FindDevices` (2465), `FinishOperation` (2985), `CartridgeTypeChanged` (3121).

- For `OpenPath`, extract the initial default-path/debug-log resolution, which contains two decisions. Leave the existing platform launcher calls and their existing security annotations in place; do not copy or add suppressions. Preserve Shift-modifier behavior and exception boundaries.
- Extract the scan of one hardware backend from `FindDevices`. Type the backend/factory boundary from existing definitions rather than adding `Any`. Preserve duplicate-port termination, message collection, close/present ordering, and requested-mode selection.
- Extract the ROM-backup finish branch from `FinishOperation`, including report handling and DMG/AGB dispatch. Its result must preserve the early return that bypasses final completion.
- Extract the DMG or AGB profile application from `CartridgeTypeChanged`, retaining invalid-index and active-detection gates, mapper updates, and the `dmg-mmsa-jpn` ROM-size exception.

**Validation:** `tests/test_flashgbx_gui.py`. Mock launchers and platform selection; use existing fake discovery devices. Check default-path logging, duplicate/inactive/error discovery, finish short-circuits, and invalid/valid profile selection.

### 5. GUI flash selection and loading — 2 errors; 25 remain

**Targets:** `FlashGBX_GUI.py::_PrepareFlashCartSelection` (3290), `FlashROM` (3663).

- Extract the auto-detected profile resolution branch. Preserve `WAITING_FLASH`, pending `detect_cartridge_args`, cancellation versus failed detection messaging, index updates, and status cleanup. Return a typed optional index; do not erase the distinction between `False`, `None`, and `0` before existing behavior is applied.
- Extract ROM-file loading/type validation from `FlashROM` behind the non-erase branch. Return `bytearray | None`; retain the string-path check and failure early return. Leave voltage checks, header repair, and final dispatch order unchanged.

**Validation:** `tests/test_flashgbx_gui.py`. Assert detection transitions, invalid profiles, canceled/failed loads, erase-only operation, buffer sizes around `0x1000`, header-repair rejection, and resulting transfer arguments.

### 6. GUI save calibration and writing — 3 errors; 22 remain

**Targets:** `FlashGBX_GUI.py::_prepare_camera_save` (4242), `_select_save_write_path` (4382), `WriteRAM` (4994).

- Extract calibration-dialog response application, including cancel/keep/reset branches. Keep calibration comparison and dialog timing intact, with exact protected-byte slices.
- Extract the explicit dropped-path confirmation/setting update path or the file-picker branch from `_select_save_write_path`. Preserve `None` versus empty-string meanings and when settings are written.
- Extract batteryless save-write preparation/dispatch from `WriteRAM`, reusing `_SaveWritePreparation` where useful. Return an explicit continuation result so rejected ROM size, canceled BL arguments, or declined voltage never reaches `_CompleteSaveWrite`.

**Validation:** `tests/test_flashgbx_gui.py`. Retain existing ordinary/PHOTO! calibration cases, keep/reset/cancel/test flows, erased patterns, path selection, batteryless writes, and buffered restore payload assertions.

### 7. GUI cartridge display and progress — 3 errors; 19 remain

**Targets:** `FlashGBX_GUI.py::_DisplayAgbCartridge` (6107), `_FormatDetectedSaveType` (6382), `_UpdateProgressAction` (7121).

- Extract the nonempty cartridge profile-selection loop or the empty/nonempty display block. Preserve profile precedence: the existing loop may set the index more than once. Keep the later DACS override and firmware warning order.
- Extract AGB save-description construction, including the batteryless details exception handler. Keep unknown save type and unstable SRAM output behavior intact.
- Split terminal progress actions into a helper as in task 2, using GUI-specific types and effects. Avoid a shared CLI/GUI abstraction. Keep error/info text and abortable control behavior unchanged.

**Validation:** `tests/test_flashgbx_gui.py`. Check empty/no cartridge, database defaults, special profiles, DACS overrides, missing batteryless details, unknown save types, all progress actions, and unknown-action no-op.

### 8. Flash erase and firmware-update control — 2 errors; 17 remain

**Targets:** `Flashcart.py::ChipErase` (429), `hw_GBxCartRW.py::UpdateFirmware` (1455, nested firmware window class).

- Extract the status-register command sequence from the erase polling loop. Keep dummy reads, actual reads, write-enable pin restoration, status masking, timeout decrements, progress events, and final reset in their original order. Do not replace the poll loop merely to lower complexity.
- Extract firmware-image load-error reporting and control restoration from the existing exception handler. It contains the “too large” versus checksum-message decision. Preserve cancellation as `None`, validation failure as `False`, retry return code 3, and success as `True`.

**Validation:** `tests/test_flashcart_erase.py`, `tests/test_gbxcartrw_firmware_window.py`, `tests/test_gbxcartrw.py`. Cover erase timeout/short reads, status commands and pins; all firmware choices, malformed image errors, decline, retry, and restored controls with fake I/O only.

### 9. Device serial reads and progress delivery — 2 errors; 15 remain

**Targets:** `LK_Device.py::_read` (1341), `SetProgress` (1932).

- Extract the incomplete-read wait/top-up block with an accurate serial-device type and `bytes` return. Keep the failure-reporting stack inspection in `_read` so caller diagnostics remain meaningful. Preserve the 50-iteration budget, drain/reset behavior, and integer versus bytearray versus `False` results.
- Extract callback/signal delivery from `SetProgress`, including default-signal selection and `.emit` versus callable handling. Keep cancellation and voltage-fallback suppression before delivery, position updates before callbacks, and initialization/finish cleanup after callbacks.

**Validation:** `tests/test_lk_device_protocol.py`, `tests/test_lk_device_transfer.py`, `tests/test_progress.py`. Exercise partial/top-up/timeout reads, finite input draining, both callback forms, no signal, user versus hardware aborts during fallback, and callback-observed state.

### 10. Device headers, special data, and checksums — 3 errors; 12 remain

**Targets:** `LK_Device.py::_ReadDmgSpecialData` (2057), `ReadHeader` (2183), `_CalculateROMChecksums` (4351).

- Extract the Game Boy Camera calibration-read branch from `_ReadDmgSpecialData`, retaining RAM enable/bank/read/disable order and rejection of uniform calibration data.
- Extract AGB header preparation/parsing from `ReadHeader`. Return both updated header bytes and the accurate header type; preserve DACS unlock, bootleg retry, Vast Fame commands, RTC/calibration reads, and final `_StoreHeaderData` inputs.
- Extract AGB save-library/flash-ID/EEPROM metadata work from checksum calculation. Keep buffer/file padding changes before hashes, exception-driven serial resets, and stale metadata removal.

**Validation:** `tests/test_lk_device_memory_io.py`, `tests/test_lk_device_detection.py`, `tests/test_lk_device_rom.py`, `tests/test_lk_device_rom_prepare.py`. Assert header bytes, mapper commands, calibration keys, independent hashes, EEPROM tail restoration, and file/buffer consistency.

### 11. Cartridge save detection and worker cleanup — 2 errors; 10 remain

**Targets:** `LK_Device.py::_PrepareCartridgeSaveDetection` (2336), `_DetectCartridge_Worker` (2601).

- Extract GB-Memory metadata detection from `_PrepareCartridgeSaveDetection`. Do not merge it blindly with `_ReadDmgSpecialData`: the current `GBMEM-MENU 256M` paths deliberately have different bank/address sequences. Preserve those sequences unless a separate bug investigation establishes otherwise.
- Extract the save-type detection phase from `_DetectCartridge_Worker`, returning a typed result that can represent its early-success tuple path and `None` abort. Keep auto-poweroff restoration and its change flag in the existing outer `try/finally`. Do not move the flag assignment past the firmware write: restoration currently also runs if that write raises.

**Validation:** `tests/test_lk_device_detect_worker.py`, `tests/test_lk_device_detection.py`. Assert supported menu layouts, parsed metadata, save-detection bypasses, early return shape, exceptions, and restoration of the original auto-poweroff time on every exit where it was changed.

### 12. Flash size, special probes, and command collection — 3 errors; 7 remain

**Targets:** `LK_Device.py::_detect_flash_size` (3787), `_ProbeSpecialFlashCart` (3996), `_AppendFlashDetectionCommand` (4141).

- Extract supported-cartridge selection by device mode from `_detect_flash_size`, retaining the unsupported-mode exception and the empty-candidate early return before device/profile work.
- Extract one complete inline probe, such as M29W640, from `_ProbeSpecialFlashCart`; its match-only reset must remain conditional. Keep the ordered outer dispatch and `None` for inapplicable profiles.
- Extract reset/unlock command collection from `_AppendFlashDetectionCommand`. Preserve collection order, deduplication, identifier filtering, and which malformed profile accesses currently raise. Do not silently relax required profile keys as part of extraction.

**Validation:** `tests/test_lk_device_detection.py`, `tests/test_lk_device_flash_worker.py`. Check empty/ambiguous size candidates, CFI/banked/header probing, matched/unmatched/inapplicable special profiles, exact commands, and duplicate identifier handling.

### 13. ROM programming and Vast Fame reads — 2 errors; 5 remain

**Targets:** `LK_Device.py::WriteROM` (3259), `_InitializeVastFameRead` (4584).

- Extract the optional rumble-stop boundary handling, returning the updated Boolean flag. It must retain the flag when the boundary has not been reached and clear it only after both writes; keep byte position updates before the check.
- Extract one complete Vast Fame mapping probe, preferably value-bit reordering, into a typed helper. Preserve the 16-mode sequence, source/target iteration order, first-match break, reversal, and final metadata shape.

**Validation:** `tests/test_lk_device_memory_io.py`, `tests/test_lk_device_rom_prepare.py`, `tests/test_lk_device_flash.py`. Check skipped all-FF chunks, final short chunks, ACK 1/3/failure, rumble boundaries, and finite deterministic mapping responses.

### 14. Save chunk routing and worker preparation — 2 errors; 3 remain

**Targets:** `LK_Device.py::_WriteSaveChunk` (6107), `_BackupRestoreRAM_Worker` (6226).

- Extract the DMG special-mapper routing from `_WriteSaveChunk`. Use a handled/result record or other explicit typed distinction between “not handled” and failed write; keep fallback `WriteRAM` reachable for ordinary DMG/AGB paths. Preserve the existing specialized writer return contracts rather than treating every writer as Boolean.
- Extract DMG/AGB configuration selection and normalization from `_BackupRestoreRAM_Worker`, reusing `_DMGSaveConfiguration` and `_AGBSaveConfiguration` and a narrowly scoped typed normalized result if needed. Keep initial firmware writes, transfer action preparation, the existing `try/finally` boundary, verification-only early return, failure cleanup, hardware reset, and completion order unchanged.

**Validation:** `tests/test_lk_device_save_io.py`, `tests/test_lk_device_save_worker.py`, `tests/test_lk_device_save_config.py`, `tests/test_lk_device_save.py`. Check every specialized route, slices/addresses, rejection before writes, failed configuration, backup/restore/verification-only results, and cleanup exactly once.

### 15. Flash method selection and write preparation — 2 errors; 1 remains

**Targets:** `LK_Device.py::_SelectFlashWriteMethod` (6516), `_prepare_flash_write` (7766).

- Separate pure method selection from method emission/command resolution. Return a small named result containing method ID, optional command name, debug information, buffer-size flag, and any literal commands. Preserve special-cart precedence and firmware thresholds before generic buffered/page/single fallbacks. The selection helper itself must stay at complexity 10 or lower.
- Extract post-erase write/verify sector-list resolution from `_prepare_flash_write`, including the chip-erase and empty-write-list branches. Preserve list aliasing/mutation behavior, both progress initialization calls, sector counts, and the final empty-sector rejection.

**Validation:** `tests/test_lk_device_flash_prepare.py`, `tests/test_lk_device_flash_worker.py`. Check all command-set IDs and firmware boundaries, unsupported profiles, real preparation integration, chip/sector erase decisions, delta sectors, and exact progress arguments.

### 16. Flash verification — final error; 0 remain

**Target:** `LK_Device.py::_verify_flash_write_enabled` (7163).

Extract the per-bank CRC decision/classification step or a cohesive bank-verification loop. Prefer the smaller boundary if it can accurately return updated verification state, CRC-error count, and whether the bank loop should break. Reuse `_FlashVerificationContext` and `_FlashVerificationBankContext`; add a named result only for state that actually crosses the boundary. Preserve cross-sector `current_bank`, fallback addresses, `buffer_len`, CRC error accumulation, the five-error cutoff, and `None` cancellation separately from failed verification.

**Validation:** `tests/test_lk_device_flash.py`, `tests/test_lk_device_flash_worker.py`. Verify matching CRCs, mismatch/readback fallback, old firmware, GBAMP exclusion, multiple banks/sectors, the CRC-error cutoff, cancellation, broken-sector reporting, and final verification events. Keep the real outer verification loop and extracted logic active in these tests.

## Validation protocol for every task

1. Read the named methods, callers, type definitions, and relevant tests. Capture current diagnostics and working-tree status.
2. Make the smallest extraction, format only touched Python files, and run the relevant tests listed above. Locate additional existing tests by function name where useful. Add meaningful missing regression cases before declaring the task done.
3. Run these repository-wide checks with the existing environment:

   ```sh
   .venv/bin/ruff check . --output-format json > /private/tmp/flashgbx-ruff-current.json
   .venv/bin/ruff format --check .
   .venv/bin/pyright
   .venv/bin/pyrefly check
   .venv/bin/ty check
   QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
   git diff --check
   ```

   Until task 16, Ruff is expected to exit nonzero only for still-unfinished baseline diagnostics. Compare by file, rule, and function name, not shifted line numbers. The task's targets must be gone, no new rule violations may appear, and both old and new functions must be within the complexity limit. The remaining count should match the task heading when the baseline is unchanged.

4. All three type checkers must continue to report **zero errors** in their configured application scope. Inspect any newly reported warnings as well; do not suppress them. Existing test files are outside that type-checker scope, so these checks are not a claim that every test annotation is strictly checked. Give new helpers and test doubles accurate annotations anyway.
5. Review the diff for lost branches, changed sentinel semantics, new suppressions/casts/type erasure, altered exception/cleanup boundaries, moved I/O, or unrelated edits. A passing type checker alone is not evidence of correct serial or UI behavior.
6. Record the task, changed functions, remaining Ruff count, test results, checker results, any limitations, and next task in the progress log. Stop before the next numbered task. If a check fails, fix or revert only your own task changes before advancing.

The existing pre-commit Ruff hook uses `--fix`, and the formatting hook writes files. Do not use a broad hook run as the first baseline check. Read-only commands above make diagnostics and changes explicit. Locked `uv run --locked …` equivalents are acceptable if dependencies are already available; do not refresh the lockfile to execute the plan.

## Final acceptance

- `ruff check .` exits 0 with no diagnostics; `ruff format --check .` exits 0.
- Pyright, Pyrefly, and ty report zero errors with unchanged configuration.
- Full tests pass, and a final coverage run passes the existing 60% gate:

  ```sh
  COVERAGE_FILE=/private/tmp/flashgbx-ruff-final.coverage QT_QPA_PLATFORM=offscreen \
    .venv/bin/python -m pytest --cov --cov-report=term-missing \
    --cov-report=json:/private/tmp/flashgbx-ruff-final-coverage.json -q
  ```

- Compare final coverage with the measured 80.0016% baseline. Helper extraction can change the denominator, so investigate any drop using changed statements/branches rather than adding artificial tests or lowering a threshold. No previously protected behavior should lose coverage.
- No added suppressions, weakened checks, excluded code, new dependencies, or unrelated implementation changes. Preserve the user's complexity limit of 10.
- Report the exact validation results and any untested platform/hardware behavior. Hardware-free macOS tests do not establish real-hardware or Windows/Linux compatibility. If a PR is later requested, inspect the existing Linux CI results before claiming cross-platform validation; do not change CI as part of this plan.

## Progress log

- Planning complete: 38 C901 diagnostics inventoried across seven files; no implementation changes. All three type checkers and 1,534 tests pass. Next task: **1**.
- Tasks 1–3 complete: extracted duration formatting and progress speed/ETA work; CLI terminal actions and multi-roll camera export; save confirmation, transfer dispatch, and voltage-warning responsibilities. Ruff has **31** baseline C901 diagnostics remaining, and the changed CLI/formatter/progress files add none. Task 1 regression set passed (23 tests); CLI tests passed (199 tests); Pyright, Pyrefly, and ty reported zero errors after task 3. Next task: **4**.
- Tasks 4–7 complete: extracted GUI path/discovery/ROM-finish/profile handling; automatic flash-cart resolution and ROM validation/loading; camera calibration choice, save-file selection, and regular/batteryless save writes; AGB profile selection, save-chip description, and common progress-action display. Ruff has **19** baseline C901 diagnostics remaining. GUI tests passed (270 tests); all three type checkers report zero errors. One intermediate test run exposed existing fixtures that inject a plain tuple for save preparation; helpers now use the tuple-compatible named positions as the previous code did. Next task: **8**.
- Tasks 8–9 complete: isolated erase-status polling and firmware-load error recovery; isolated serial-read completion and progress callback delivery. Ruff has **15** baseline C901 diagnostics remaining. Erase/firmware tests passed (160 tests), device/progress tests passed (59 tests), and all three type checkers report zero errors. Next task: **10**.
- Tasks 10–11 complete: extracted Game Boy Camera and AGB header metadata handling, AGB checksum metadata, GB-Memory detection data, and save-type detection including the DMG early-result record. Ruff has **10** baseline C901 diagnostics remaining. Header/detection/ROM tests passed (171), detect-worker tests passed (108), and all three type checkers report zero errors. Next task: **12**.
- Tasks 12–13 complete: extracted mode-specific flash profile selection, M29W640 probing, and reset/unlock command collection; isolated ROM rumble-stop boundaries and Vast Fame value-bit probing. Ruff has **5** baseline C901 diagnostics remaining. Detection/flash-worker tests passed (156), Pyright, Pyrefly, and ty reported zero errors. Next task: **14**.
- Tasks 14–15 complete: isolated DMG save-chunk routing and normalized DMG/AGB save-worker configuration; extracted flash write-method selection and post-erase sector-list selection. Ruff has **1** baseline C901 diagnostic remaining. Save-worker/config tests passed (153), flash-preparation/worker tests passed (163), and all three type checkers reported zero errors. Next task: **16**.
- Task 16 complete: moved the per-sector bank loop and CRC classification into typed context/result helpers while keeping cancellation, progress, cross-sector bank state, CRC cutoff, and read fallback in the real verification path. Ruff now reports **0 diagnostics**. Flash verification tests passed (94); all three type checkers report zero errors.
- Final acceptance passed: `ruff check .` reports no diagnostics; `ruff format --check .` reports 79 files already formatted; Pyright reports 0 errors, warnings, or informations; Pyrefly reports 0 errors; ty passes. The full suite passes (**1,534 tests**) at **80.10% coverage**, above the **80.0016%** baseline and 60% gate. The edited `hw_GBxCartRW.py` retains CRLF endings, so the whitespace check was run with `git -c core.whitespace=cr-at-eol diff --check` and passed. No hardware-connected or Windows/Linux run was performed; validation used macOS with Qt offscreen.

## Copyable prompt for GPT-6 Luna

> Read `RUFF_FIX_PLAN.md` and applicable `AGENTS.md` instructions. Implement only the next unfinished numbered task in the progress log. Start with `git status --short` and preserve user changes, especially `pyproject.toml`'s complexity limit of 10. Read the target functions, their callers/types, and nearby tests. Make behavior-preserving extractions without adding suppressions, weakening configuration, or using type erasure to pass checks. Run the task's regression tests and the plan's repository-wide validation. Require zero new Ruff diagnostics and zero errors from Pyright, Pyrefly, and ty. Record results, remaining Ruff count, and the next task in the progress log, then stop. Do not implement the entire plan in one turn.
