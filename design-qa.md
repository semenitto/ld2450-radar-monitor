# Design QA — LD2450 Monitor: прямоугольные зоны

- Source visual truth: `C:\Users\user\.codex\generated_images\01a0261d-c44d-7a72-ad43-10ca3ab72249\exec-54a10fcd-674f-4ad5-9205-44d5a8c000f6.png`
- Implementation screenshot: `C:\Users\user\Documents\Codex\2026-08-21\new-chat\outputs\ld2450_radar_esp32\audit\05-readme-interface.png`
- Viewport: native Windows application, 1120 × 760 logical content pixels; the README image contains only application content, without the mouse pointer or operating-system frame.
- State: deterministic two-person demo with populated rectangular zones and six-hour history, chosen to explain the interface at a glance.

## Findings

No actionable P0, P1, or P2 differences remain.

- Layout: the square room plan is the stable upper-left visualization. Its bounds depend only on the canvas size, so changing values in the right sidebar does not resize or move the room.
- Zones: all three semantic regions are real rectangular data zones. Edit mode shows the selected rectangle, four resize handles, its dimensions, and a clear “Готово” exit state.
- Calibration: the settings dialog exposes room width/depth, radar X/Y, rotation, optional X reflection, and smoothing without turning setup into a heavy calibration workflow.
- Data rendering: the radar field of view is clipped to the room; live positions and trails use calibrated room coordinates. A 15 cm zone hysteresis and adjustable exponential smoothing reduce boundary chatter and marker jitter.
- Visual system: the implementation consistently uses the selected `#213843`, `#468D8B`, `#74B3A8`, `#F6DAC0`, `#FEAF76`, and `#DA6D58` palette plus derived surface shades.
- Window chrome: on Windows the native title bar is tinted to the dark app palette, while native resizing, snapping, minimize/maximize, and accessibility behavior remain intact.
- History: legacy occupancy and activity are migrated, while legacy angular-zone labels are reset so they cannot be misrepresented as rectangular-room history.

## Full-view comparison evidence

The reference and final implementation were reviewed together. Both retain the same hierarchy: room map at upper-left, occupancy summary at upper-right, full-width activity/occupancy timeline below, and persistent device status at the bottom. The application uses a palette-tinted native Windows title bar; the README crop deliberately excludes all operating-system chrome.

Both the reference and README screenshot use synthetic two-person data, making the occupancy states and timeline comparable while preserving the production layout, palette, mappings, and interactions.

## Primary interactions tested

- Live COM9 frames update the calibrated target marker and status-frame counter.
- “Редактировать зоны” enters the selected-rectangle state with handles and exits through “Готово”.
- The calibration dialog opens at the correct size, keeps every field visible, and closes without layout overflow.
- The six-hour history renders after migration without assigning old angular-zone samples to new rectangular zones.
- Nine automated tests pass: UART parsing, room transform, mirror/rotation, rectangle mapping, boundary hysteresis, calibration persistence, history migration/storage, Russian count forms, and Windows title-bar color conversion.

## Follow-up polish

- [P3] Tkinter canvases expose limited semantic detail to Windows accessibility tools. A later accessibility iteration could add keyboard movement/resizing for zones and a text-mode chart summary.
- [P3] A guided two-point calibration can be added later only if real measurements show systematic scale or perspective error; current evidence does not justify that complexity.

final result: passed
