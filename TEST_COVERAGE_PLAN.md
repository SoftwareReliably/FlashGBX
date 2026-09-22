# Test coverage plan — round 3

Implementation handoff for a smaller model. Rounds 1 and 2 are complete; the completed round-2 plan and progress log are preserved in [TEST_COVERAGE_PLAN_ROUND_2.md](TEST_COVERAGE_PLAN_ROUND_2.md). Implement **one lettered task per turn**, in the order below, and stop after recording its results. This round prioritizes the four lowest-covered included files.

Only planning documents were changed to prepare this handoff. No round-3 tests or production fixes have been implemented.

## Fresh baseline

Measured September 21, 2026, on macOS/Python 3.14.5 using the existing environment:

```sh
COVERAGE_FILE=/private/tmp/flashgbx-round3-plan.coverage QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest --cov --cov-report=json:/private/tmp/flashgbx-round3-plan.json --cov-report=term:skip-covered -q
```

**1,014 passed in 4.60 seconds.** Combined coverage counts covered statements and branch exits together; it is not line coverage alone.

| File/scope | Combined | Statements | Branches | Missing statements / branches | Round-3 direction |
| --- | ---: | ---: | ---: | ---: | ---: |
| `LK_Device.py` | 49.71% | 52.77% | 42.58% | 2,241 / 1,168 | 60% |
| `hw_GBxCartRW.py` | 60.05% | 58.70% | 65.46% | 501 / 105 | 70% |
| `FlashGBX_GUI.py` | 62.86% | 68.16% | 47.78% | 1,267 / 730 | 70% |
| `FlashGBX_CLI.py` | 64.18% | 66.71% | 58.91% | 560 / 332 | 70% |
| Configured package | **68.23%** | **71.55%** | **59.02%** | **5,035 / 2,614** | **70%, then 73%** |

These four files contain approximately 90.3% of the package's missing statement/branch opportunities. Every other included file is at least 80% covered. Prioritize behavior in these four even if polishing another file would be easier.

The denominator is 17,699 statements plus 6,378 branch exits. At this denominator, 70% needs **426** additional covered opportunities, 73% needs **1,149**, and 75% needs **1,630**. Targets are directional checkpoints, not guaranteed outcomes or reasons to expand a task indefinitely. Recompute after production changes. Report module gains separately so package growth cannot hide an untouched low-covered file.

`pyproject.toml` already measures `source = ["FlashGBX"]`, uses branch coverage, and enforces 60%. It omits `hw_GameBub.py`, `hw_GBFlash.py`, and `hw_JoeyJr.py`. Keep that scope. Linux CI already installs locked dependencies, runs ty and Pyrefly, enforces package coverage, and uploads reports. No CI rebuild is needed; no Linux run was performed during planning.

## Rules for every task

- Read applicable `AGENTS.md` files and run `git status --short` first. The planning baseline had no tracked modifications. Preserve subsequent user changes; do not update dependencies or regenerate `uv.lock`.
- Read the named production methods and nearby tests before editing. Function percentages below come from the baseline JSON, not estimates from file size.
- Preserve `tests/conftest.py::prevent_real_hardware`. Never open real serial ports, enumerate actual hardware, update device firmware, contact a network service, launch external applications, or access normal user configuration. Patch only the relevant factory when injecting fake serial. Use `tmp_path` and generated ROM/save/firmware bytes.
- Instantiate `hw_GBxCartRW.GbxDevice()` without `Initialize` for engine tests. Assign fresh instance firmware, mode, `INFO`, counters, and error state. Inspect overrides: do not accidentally test a subclass override when measuring a base implementation, or chase abstract/base placeholders solely because they are uncovered.
- Reuse `tests/fakes.py` and existing file-local fixtures. Helpers stay local until two files need them; then extract the small shared helper to test support. Never import one test module from another. The firmware-window fixture already loads an isolated module with inert Qt classes; `test_flashgbx_gui.py` already has a substantial GUI fixture.
- Named target methods remain real. Replace the boundary collaborators explicitly allowed by the task. Assert exact bytes, addresses, return types, final state, and consequential call order. Call counts or lack of exceptions alone are insufficient.
- Use finite response queues that fail loudly when exhausted. `MockSerial` records writes but does not enforce a protocol script itself. Patch sleeps locally, use advancing fake time for deadlines, and bound retries. Never return success indefinitely from a fake to get past a loop.
- Keep `False`, integer acknowledgements, and `None` distinct. Some successful writers return `None`. Read callers before deciding a return value signals failure. Rejection tests explicitly assert no later write/transfer/success event.
- Restore Qt/import substitutions, stdout, settings, and time patches. Use existing fake widget classes when production uses `isinstance`; a generic mock is not equivalent. Run each modified GUI test file alone and in the suite.
- No added exclusions, `no cover`, blanket skips, permanent xfails, lower thresholds, or refactors for coverage alone. If a test proves a defect, make the smallest necessary fix with its regression test and report it separately. Record ambiguous contracts instead of making unsafe behavior an expected result.
- Parameterize meaningful decision tables, not every Cartesian combination. Complete the task's behavior checks even if a numerical target is reached early.

## Ordered tasks

### 1. Engine I/O beneath the completed worker tests

Earlier rounds tested workers and chunk routing with these lower-level methods replaced. These are new seams, not a request to repeat the completed save/flash worker matrices.

**1a — Acknowledgements and bounded recovery.** New `tests/test_lk_device_protocol.py`. Target real `LK_Device.wait_for_ack` and `_try_write` (both 0%). Read `_read`/`_write` and existing serial tests first.

- `wait_for_ack`: default acknowledgements `1` and `3`, custom accepted set, timeout `False`, device error `2`, other unexpected values, and user cancellation. Assert return identity, `ERROR`, `CANCEL`, error count, and structured cancellation metadata; avoid caller line numbers/full translated prose.
- Exercise the write-delay boundary at the fourth recorded error. User cancellation must not get overwritten with a communication-error message.
- `_try_write`: first-attempt success, failure then successful recovery, exhausted attempts, and cancellation during a write. Assert payload/`wait=True`, recovery zero bytes, input/output resets, and clearing stale error state on success.
- Test `retries=0` rejection and the recovery probe's 20-read budget with finite scripts. Keep `_try_write` real while replacing `_write` and `_read`; test `wait_for_ack` separately with controlled `_read`.

**Done:** success/failure paths for both functions, exact retry counts, and consumed response queues.

**1b — Ordinary RAM reads/writes.** New `tests/test_lk_device_memory_io.py`. Target real `ReadRAM` and `WriteRAM` (both 0%). Replace firmware-variable writes, `_write`, `_read`, and progress only.

- DMG/AGB addresses, default/explicit command, device buffer caps, one/multiple chunks, progress enabled/disabled, and unsupported mode. Assert DMG access mode and CS-pulse setup/reset order.
- Reads: exact concatenation, short reply, timeout, and legitimate one-byte reply. Check a one-byte timeout independently from a valid zero byte.
- Writes: nonzero address, exact payload slices, representative bytes/bytearray/memoryview inputs, and action-dependent progress. A failed payload acknowledgement in the first/later chunk must stop further programming and must not report successful bytes for that chunk.
- Include an uneven final chunk in each direction. Inspect firmware transfer-size assumptions and callers first. If such lengths are accepted, assert exact requested count, final transfer size, and progress; if alignment is required, establish deterministic rejection. Do not bless over-reading or inflated progress.
- Connect one low-level write failure through the real save worker using its existing harness, retaining real `WriteRAM` for that regression. Do not rebuild the successful worker matrix.

**Done:** exact I/O plus timeout/failure propagation. Read the investigation notes before encoding current behavior.

**1c — Ordinary flash programming protocol.** Extend `tests/test_lk_device_memory_io.py`. Target real `WriteROM` (0%); defer specialized mapper writers.

- Empty input; DMG byte/AGB word addresses; transfer/buffer setup versus `skip_init`; multiple chunks and a partial final chunk.
- Acknowledgement `1` sends a fresh `FLASH_PROGRAM` command next time; `3` permits continuation. Invalid/timeout acknowledgement stops immediately and records the failing iteration.
- All-`0xFF` skipping when supported, followed by nonempty data: address reinitialization, `SKIPPING`, exact payloads, and progress. Include buffered writes where skipping must not occur.
- Rumble stop executes once at a completed flash-buffer boundary with the two expected cart writes. Progress suppression does not alter payloads.
- Assert successful `None` distinctly from failure `False`; do not change the return API for consistency alone.

**1d — Remaining flashcart setup.** Extend `tests/test_lk_device_flash_prepare.py`. Target `_configure_flashcart_for_write` (31.65%); retain real profile accessors and use a small recording mapper factory.

- Firmware 7/8 pullup-command and 11/12 acknowledgement/WR-pullup/AGB IRQ boundaries; absent/enabled/disabled settings in targeted cases.
- Forced/manual/default MBC5 selection; unsupported mapper returns `None` before mapper enable. Assert local argument mutations and returned `_FlashConfiguration` fields.
- Exact/partial ROM banks, mapper maximum-size advisory, pulse reset, and AGB default/banked geometry. Assert command and mapper-enable ordering.
- Extend an existing integrated preparation case to retain this helper and verify its configuration is consumed; do not duplicate the entire preparation matrix.

### 2. GBxCart firmware-window decisions without real Qt or flashing

**2a — Modern updater controller.** Extend `tests/test_gbxcartrw_firmware_window.py` with an inert `FirmwareUpdaterWindow` alongside the V13 harness. Target `UpdateFirmware` (0%). Replace `FWUPD.WriteFirmware`, message boxes, app disconnect, and controls; do not construct a real dialog.

- No PCB selection rejects before disconnect/write. v1.4 versus v1.4a/b/c selects the correct archive path. Canceling preparation causes no firmware write.
- Return codes `1`, `2`, `3`: success, update failure, corrupted firmware. Assert exact path/status callback, controls disabled before writing, and restored on terminal results.
- Success clears `DEVICE` and closes the dialog; failures do not report success. A finite writer fake must fail on unexpected retries.

**2b — V13 loading and retry orchestration.** Same test file. Target `FirmwareUpdaterWindowV13.UpdateFirmware` (0%). Keep `_parse_intel_hex` real; replace final `WriteFirmware` transport.

- Generate minimal valid HEX and temporary ZIPs containing `cfw.hex`/`ofw.hex`. Assert custom/official selection, exact decoded writer bytes, and custom-file settings updates.
- File-picker cancellation, unexpected custom filename, and declined confirmation produce no write. Check disconnect ordering at each boundary.
- Malformed HEX, oversized image, non-ASCII data, missing archive member, and missing/unreadable input restore controls/reset progress without disconnecting for programming. Reuse parser patterns, not its whole validation table.
- Writer scripts `[1]`, `[2]`, `[3, 1]` assert terminal result, retry count, and identical retry payload. V13 code `3` requests retry; modern code `3` means corruption. Do not conflate these or assume V13 cleanup belongs to this controller when it belongs to its writer.

**2c — Selection, status, and closing safeguards.** Same file, plus `tests/test_gbxcartrw.py` if appropriate. Target modern `SetPCBVersion`/`SetStatus`, both `CloseDialog` implementations, and `GbxDevice.GetFirmwareUpdaterClass` (all 0%).

- Select each modern PCB with temporary firmware metadata; verify version/build/text and displayed selection. Compare timestamps semantically or freeze timezone.
- Status text, absent/present progress, UI enable behavior: modern progress scales by ten, V13 rounds directly. Modern event processing uses a fake app.
- Enabled close bypasses confirmation; disabled close plus No/Yes returns the correct Boolean with conservative default.
- Supported PCB IDs route to modern/V13 classes; unsupported IDs and unavailable optional Qt names return no updater. Restore patched names within the fixture.

**Checkpoint:** report backend gains. Do not add constructor/layout tests merely to reach 70%.

### 3. GUI decisions currently replaced by test doubles

Extend `tests/test_flashgbx_gui.py` and its existing local fixtures throughout; no new standalone GUI fixture infrastructure.

**3a — Batteryless SRAM parameters.** Target `GetBLArgs` and `_get_default_bl_location_index` (both 0%). Keep selection real; substitute a recording `UserInputDialog` returning existing fake controls and isolated settings.

- DMG/AGB defaults; location just below/at/above the ROM-size boundary; ROM larger than every preset.
- Detected values override remembered/default choices; invalid detected size falls back. DMG header preselection supplies location, size, and layout. Assert indices/numeric results, not the entire introduction.
- Saved locations: valid/malformed JSON, mixed types, duplicates, unavailable remembered location. Assert sorted usable choices and fallback.
- Accepted preset/custom hexadecimal location; DMG includes layout, AGB omits it. Assert exact persisted settings. Cancel returns `False` without settings writes; unset mode raises.
- Investigate invalid custom text without approving an offset-zero write as safe behavior. Do not connect that case to a real writer.

**3b — Save profile detection and resumption.** Target `_prepare_save_backup_cartridge` and `_prepare_save_write_cartridge` (both 9.09%), plus relevant `_ResumeDetectedCartridgeAction` branches (29.41%).

- Ordinary saves bypass detection; batteryless DMG/AGB and PHOTO! require it. Write test mode bypasses it. Legacy firmware refuses before detection/transfer.
- Selected profile plus batteryless metadata proceeds; otherwise assert waiting state, exact `detect_cartridge_args` including erase, and `DetectCartridge(checkSaveType=True)`.
- Detection result: canceled `False`, missing `None`, zero, wrong type, valid index. Assert consumed pending state and selected platform combobox; no transfer after rejection.
- One backup and one restore/erase resumption retain real preparation: correct saved arguments, exactly one resumed action, no duplicate detection. Patch final transfer/dialogs as needed.

**3c — Mapper, profile, and boot-logo choices.** Target `CartridgeTypeChanged` (0%), `_ConfirmFlashMapper` (16.67%), `_ConfirmFlashBootLogo` (6.67%).

- Profile changes: sentinel index, active detection, DMG/AGB, recognized/missing ROM size, DMG memory-cart exception. Assert size, cached profile, and mapper update.
- Mapper confirmation: AGB, matching DMG, compatible pair, incompatible manual keep-selected/use-ROM/cancel, forced incompatible continue/cancel. Use generated headers and real parsers for representative cases.
- Logo: valid header/exempt mappers bypass prompting; temporary DMG/AGB files read exact byte limits. Cover fix, continue without fixing, cancel, missing file. Redirect `AppContext.CONFIG_PATH` to `tmp_path`.
- One declined mapper/logo choice through real `FlashROM` must issue no transfer. Do not retest the existing dispatch matrix.

### 4. CLI branches outside completed save-calibration work

Extend `tests/test_flashgbx_cli.py` with existing CLI/device fixtures.

**4a — Interactive menu and platform selection.** Target `_SelectMenuAction` (0%), `_PrintConfigMessages` (8.33%), `_RunStandaloneAction` (31.82%), uncovered `run` branches (38.79%).

- Menu: empty answer returns `info`; first/last choice; nonnumeric/out-of-range cancellation. Assert displayed item/action mapping without prose snapshots.
- Config messages: malformed ignored; plain/warning/error statuses select correct output/color.
- `run`: canceled menu; zero/one/multiple supported modes; switch/autodetection; DMG/AGB/default/invalid platform answers; interactive-console `KeyboardInterrupt`. Assert mode, exit status, disconnect, no cartridge work after cancellation.
- Standalone firmware action selects the matching fake updater with requested port and no cartridge read; unmatched action returns `None`. No real updater runs or omitted backend is imported just for this test.
- Reuse stdout/Logger handling; restore stdout after each case. Do not repeat command-line backup dispatch already covered.

**4b — Batteryless restore/refusal paths.** Target `_BatterylessSRAM` (46.74%). Retain `_ResolveBLArgs` for at least one path and control profile selection.

- DMG/AGB restore from temporary file; overwrite versus prompted accept/refuse; missing input and permission failure.
- Missing region/profile, unsupported stress-test action, and erase refusal stop before transfer. Do not repeat successful erase coverage without new assertions.
- Fixed-5V DMG with 3.3V/variable-voltage profile: refusal blocks transfer; acceptance forwards exact region/profile. Voltage-capable devices avoid the warning.
- Assert complete transfer args: mode 4, offset/size/layout, verification/comparison flags, path, no chip erase, no header/logo fix. Erase uses exact `0xFF` bytes and empty path. Rejections preserve input files.

**4c — Boot-logo and save-configuration choices.** Target `_PromptBootLogoFix` (12.12%) and `_ResolveSaveConfiguration` (62.20%).

- Logo: valid/exempt header, missing file, exact DMG/AGB lengths, default/yes/no answers with temporary configuration. Assert bytes or `False`, no prompt on bypass.
- Save configuration: default MBC5 when the header mapper is zero/unusable; explicit mapper; MBC2, the two MBC7 title cases, TAMA5, and MBC6 auto types; batteryless selection; PHOTO! profile detection; AGB auto/explicit type and unknown/unset mode. Choose currently uncovered rows after reading existing tests. Assert the exact `(mbc, save_type, cart_type)` tuple or `None`, and forwarding through one accepted `BackupRestoreRAM` case. RTC selection is outside this helper.
- One refusal through a real caller must issue no transfer. Exclude completed e-Reader calibration work.

**Checkpoint:** report GUI/CLI gains independently. Reaching package 70% does not cancel the remaining low-file tasks.

### 5. Cartridge detection with synthetic data

New `tests/test_lk_device_detection.py`. Cover classification/orchestration, not every chip signature or destructive probe algorithm.

**5a — DMG save-size classification.** Target `_DetectDmgSaveType` (0%). Use real `DmgSaveTypes` and generated `INFO["data"]`.

- Invalid/unrecognized size, small/no-save boundary, ordinary sizes, MBC7 256/512-byte distinction.
- 128 KiB candidate: distinct versus mirrored halves, repeated three-byte probes at `0x40` strides, upper-region checks distinguishing 32 KiB, 64 KiB, retained 128 KiB.
- Deliberate early/late mismatches in scanned ranges. Assert `(save_size, save_type)` and unchanged input bytes. Do not duplicate implementation loops to calculate expectations.

**5b — AGB FLASH/SRAM/EEPROM classification.** Target `_DetectAgbFlashSaveType` and `_DetectAgbNonFlashSaveType` (both 0%). Replace `ReadFlashSaveID`, `_BackupRestoreRAM`, `CheckBatterylessSRAM`; retain real tables and `_find_repeating_size`.

- FLASH failure/zero ID, unknown ID, known 64/128 KiB chips, bootleg blank/mirrored/distinct banks. Assert size/type/chip using table-supported IDs.
- Resolved FLASH type bypasses non-FLASH detection. DACS, supported SRAM sizes, unknown SRAM size, batteryless override: assert results and both metadata destinations.
- EEPROM: exact two backup requests, all-zero/all-`0xFF`, matching prefixes for 8 KiB, differing prefixes for 512 bytes. No batteryless probe on EEPROM paths.

**5c — Flash profile matching.** Target `_MatchDetectedFlashTypes` (0%). Keep matching real and record reset commands.

- Empty/non-dictionary profiles; missing IDs/commands; mismatched identifier command; DMG write-pin mismatch; AGB match independent of DMG pin; prefix match/no match.
- Duplicate IDs/probe methods add one index per profile; matching profiles retain catalog order. Reset only when the matched profile defines reset commands.
- Assert indices and reset calls. Use tiny `flashcart_profile` profiles with explicit identifier commands, not the entire special-cartridge catalog.

**5d — Detection worker results and cleanup.** Target `_DetectCartridge_Worker` (0%). Control `ReadHeader`, `DetectFlash`, `_PrepareCartridgeSaveDetection`, save-read, power/I/O boundaries. Keep 5a/5b classifiers real for ordinary DMG/AGB cases.

- Successful DMG/AGB: assert all 11 returned fields, ordered progress, save-read args, selected profile, mapper reset, final action state.
- Detection disabled, retail DMG profile, high mapper ID, 3D-memory no-save path, early MBC6/TAMA5/G-MMC1 returns; no unnecessary save probes.
- Header/flash failure, failed save read, raised collaborator error. Configure fake power support/firmware vars so altered auto-poweroff time is observable.
- Restoration after success and every exit that changed timeout, including early special-mapper returns. If missing, use a narrow `try/finally` or existing cleanup helper plus regressions; do not refactor detection wholesale.

### 6. Zero-covered GUI RTC editing

Use existing GUI fixtures and fake RTC device/dialog; no actual RTC access. Split the large `EditRTC` method across two tasks.

**6a — Guards and DMG editing.** Target `_DmgRtcDialogValues` and DMG `EditRTC` branches (both 0%).

- Failed device/header check, absent/empty RTC data, unsupported mapper, canceled dialog: no `WriteRTC`.
- MBC3/MBC30-family and HuC-3 manual versus system time. Assert spinbox/checkbox extraction, mapper, preserved day fields, exact time with frozen timezone-aware clock.
- TAMA5 manual year offset and preserved RTC buffer. Defer current-time leap-year logic to 6b.
- Successful/failed writes refresh with `resetStatus=False`; assert message category and returned Boolean.

**6b — AGB and calendar boundaries.** Finish remaining `EditRTC` branches.

- AGB manual year conversion, weekday, system override, cancel. Freeze near midnight to test the one-second day/year rollover.
- TAMA5 system time applies two-second adjustment and leap-year state; include a leap boundary and a century that is not leap unless divisible by 400.
- Assert exact `WriteRTC` args, no unexpected AGB mapper field, no write after cancel. Reuse 6a fixtures.

## Investigations, not assumed contracts

These are code-reading concerns, **not confirmed defects from the planning run**:

1. `ReadRAM` converts `int` before checking `False`; Booleans are integers. A one-byte timeout may become a successful zero byte. Test the distinction in 1b.
2. `WriteRAM` ignores payload acknowledgements and returns `True`. Its advertised length/progress and `ReadRAM`'s fixed request size warrant uneven-final-chunk tests. Read callers/protocol constraints before fixing; failure must not silently become success.
3. `_DetectCartridge_Worker` changes `AUTO_POWEROFF_TIME` before several early returns. Restore only when this call changed it; inspect each exit.
4. `GetBLArgs` converts invalid custom text to offset zero. Test selection in isolation and inspect downstream validation before choosing a rejection fix. Do not assert that silently writing to zero is correct.

If a diagnostic can hang, isolate it with a short subprocess timeout and inject doubles inside the child. After a minimal fix, use an ordinary fast regression. Never leave hanging diagnostics in the suite.

## Validation, reporting, and gate ratchet

Per task: affected tests, then one full-suite coverage measurement. Substitute all changed Python files in Ruff commands:

```sh
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q tests/test_lk_device_protocol.py
COVERAGE_FILE=/private/tmp/flashgbx-round3.coverage QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q --cov --cov-report=term:skip-covered --cov-report=json:/private/tmp/flashgbx-round3.json
.venv/bin/ruff check tests/test_lk_device_protocol.py
.venv/bin/ruff format --check tests/test_lk_device_protocol.py
```

If production changes, also run `.venv/bin/ty check`, `.venv/bin/pyrefly check`, `.venv/bin/pyright`, matching current project checks. Report unrelated existing failures separately; no broad auto-fixes. Existing dependencies suffice. Clean environments use `uv sync --locked --group dev` and `uv run --locked`, without regenerating the lockfile.

Compare full-suite JSON results; a targeted run with package-wide source naturally reports lower coverage. Read `totals`, file `summary`, and `files[path].functions[name].summary`. Report combined/statement/branch values and verify named function-body gains. Do not average per-file percentages or label combined coverage as line coverage.

After 2c, 4c, 5d, and 6b:

1. Record package and all four files, including unchanged ones. List remaining uncovered functions in the area finished.
2. Update README measurements/milestone prose with verified results and unchanged omissions. Task completion does not prove numerical targets achieved.
3. The 65% ratchet remains pending Linux evidence. Raise `fail_under` only when local and Linux results for the same implementation exceed the proposed floor with at least 0.2 percentage points of headroom on the lower result. Use 65%, then 70%, as evidence permits. Local-only results leave the floor unchanged with Linux verification reported pending. Never add `--cov-fail-under=0` to CI or change coverage scope.
4. If targets remain unmet, report the shortfall and next functions in these low-covered files. Do not fill it with high-coverage utilities, excluded backends, exhaustive signatures, stress loops, or widget-layout checks.

Real Qt/event-loop behavior, updater construction, physical serial/firmware compatibility, and additional operating systems remain separate work. These tests validate logic against doubles.

Each handoff reports task ID; assertions added; affected/full-suite results; package and relevant module/function coverage before/after; production fixes; commands/checks; unresolved concerns; next ID. Append a short progress entry below. No automatic commits, pushes, or publishing are required.

## Progress log

- Round-3 baseline: 1,014 passed; package 68.23%; engine 49.71%; GBxCart backend 60.05%; GUI 62.86%; CLI 64.18%. All tasks pending. Next: **1a**.
- Task 1a: added 16 acknowledgement and bounded-recovery protocol tests covering default/custom successes, timeout/device/communication failures, user cancellation, the fourth-error write-delay boundary, real `_write` integration, first-attempt and retried payload success, exact retry exhaustion, zero-retry rejection, and the 20-probe recovery cap. 1,030 tests pass. Package coverage rose from 68.23% to 68.59% combined (71.89% statements, 59.44% branches), and `LK_Device.py` rose from 49.71% to 51.00%. `wait_for_ack` and `_try_write` both reached 100% statement/branch coverage. Ruff passes; no production change was needed. Next: **1b**.
- Task 1b: added 22 ordinary RAM I/O and failure-propagation tests covering DMG/AGB addresses and commands, device buffer caps, bytes-like inputs, exact uneven final chunks, timeout versus a valid zero byte, short reads, progress suppression, first/later write failures, hardware-state restoration, all save-write routes, and a real worker abort after a failed payload acknowledgement. A one-byte timeout is no longer converted to zero; reads and writes now update the firmware transfer size for a short final chunk, report its actual progress, and restore DMG pulse/address state on every exit; confirmed write failures stop programming and propagate through `_WriteSaveChunk`. 1,052 tests pass. Package coverage rose from 68.59% to 69.01% combined (72.25% statements, 60.05% branches), and `LK_Device.py` rose from 51.00% to 52.58%. `ReadRAM`, `WriteRAM`, and `_WriteSaveChunk` have 100% statement/branch coverage. Ruff, ty, Pyrefly, and Pyright pass. Next: **1c**.
- Task 1c: added 13 ordinary flash-programming protocol tests covering empty input, DMG byte and AGB word addresses, transfer/buffer initialization and `skip_init`, acknowledgement-driven command continuation, partial final chunks, timeout/invalid acknowledgement failures, erased-data skipping and address reinitialization, unsafe buffered `0xFF` writes, trailing skip state, one-time rumble shutdown, progress, and the distinct successful `None` versus failed `False` returns. `WriteROM` now updates firmware transfer size and progress for a short final chunk and advances by its actual length. 1,065 tests pass. Package coverage rose from 69.01% to 69.33% combined (72.53% statements, 60.49% branches), and `LK_Device.py` rose from 52.58% to 53.72%. `WriteROM` rose from 0% to 98.80% combined coverage (100% statements, 96.67% branches); only the unsupported/unset mode branch remains. Ruff, ty, Pyrefly, and Pyright pass. Next: **1d**.

## Copyable prompt for the implementing model

> Read TEST_COVERAGE_PLAN.md and applicable AGENTS.md instructions. Implement only task **1a**, or the next unfinished lettered task in its progress log. Rounds 1 and 2 are complete; do not resume the archived plan. Preserve user changes. Read named methods and fixtures, keep target methods real, and bound fake I/O/retries. Add the specified assertions; do not expand into neighboring tasks to chase a percentage. If a regression proves a defect, make only the necessary small fix and describe it separately. Run affected tests, one full-suite coverage measurement, and required checks. Append measured results and the next task ID, then stop.
