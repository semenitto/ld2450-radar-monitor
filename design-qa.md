# Design QA — LD2450 Monitor: прямоугольные зоны

- Source visual truth: `C:\Users\user\.codex\generated_images\01a0261d-c44d-7a72-ad43-10ca3ab72249\exec-54a10fcd-674f-4ad5-9205-44d5a8c000f6.png`
- Implementation screenshot: `C:\Users\user\Documents\Codex\2026-08-21\new-chat\outputs\ld2450_radar_esp32\audit\04-room-map.png`
- Viewport: native Windows application, 1120 × 760 logical content pixels; screenshot 1122 × 792 including frame and title bar.
- State: live CP210x/COM9 data, one current target, six-hour history, recording enabled.

## Findings

No actionable P0, P1, or P2 differences remain.

- Layout: the square room plan is the stable upper-left visualization. Its bounds depend only on the canvas size, so changing values in the right sidebar does not resize or move the room.
- Zones: all three semantic regions are real rectangular data zones. Edit mode shows the selected rectangle, four resize handles, its dimensions, and a clear “Готово” exit state.
- Calibration: the settings dialog exposes room width/depth, radar X/Y, rotation, optional X reflection, and smoothing without turning setup into a heavy calibration workflow.
- Data rendering: the radar field of view is clipped to the room; live positions and trails use calibrated room coordinates. A 15 cm zone hysteresis and adjustable exponential smoothing reduce boundary chatter and marker jitter.
- Visual system: the implementation consistently uses the selected `#213843`, `#468D8B`, `#74B3A8`, `#F6DAC0`, `#FEAF76`, and `#DA6D58` palette plus derived surface shades.
- History: legacy occupancy and activity are migrated, while legacy angular-zone labels are reset so they cannot be misrepresented as rectangular-room history.

## Full-view comparison evidence

The reference and final implementation were reviewed together. Both retain the same hierarchy: room map at upper-left, occupancy summary at upper-right, full-width activity/occupancy timeline below, and persistent device status at the bottom. The implementation uses a native Windows title bar and a denser 1120 × 760 production viewport; these are intentional desktop constraints.

The reference uses synthetic data with two people and a selected work-zone event. The implementation screenshot uses the actual live radar state, so the count and chart shape differ while the layout, palette, mappings, and interactions remain faithful.

## Primary interactions tested

- Live COM9 frames update the calibrated target marker and status-frame counter.
- “Редактировать зоны” enters the selected-rectangle state with handles and exits through “Готово”.
- The calibration dialog opens at the correct size, keeps every field visible, and closes without layout overflow.
- The six-hour history renders after migration without assigning old angular-zone samples to new rectangular zones.
- Eight automated tests pass: UART parsing, room transform, mirror/rotation, rectangle mapping, boundary hysteresis, calibration persistence, history migration/storage, and Russian count forms.

## Follow-up polish

- [P3] Tkinter canvases expose limited semantic detail to Windows accessibility tools. A later accessibility iteration could add keyboard movement/resizing for zones and a text-mode chart summary.
- [P3] A guided two-point calibration can be added later only if real measurements show systematic scale or perspective error; current evidence does not justify that complexity.

final result: passed
