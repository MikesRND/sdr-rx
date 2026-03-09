# SDR Monitor — UI Architecture

## Data Flow

Two API sources drive the UI:

```
/api/runtime   (live state, read-only)
    └→ receiver info, running_channels, effective_settings with source/locked
    └→ fetched once on page load + on modal open
    └→ drives control enable/disable (gain slider, squelch handle, modal fields)

/api/config    (persisted catalog, CRUD)
    └→ all channels, settings, startup_channels
    └→ fetched on modal open
    └→ drives modal forms (channel table, settings tab)
```

The main dashboard also uses existing endpoints (`/api/channels`, `/api/channels/{ch}/config`, `/ws/{ch}`) unchanged.

## State Management

Vanilla JS, no framework. Two state objects:

- `runtimeState` — from `GET /api/runtime`. Contains `receiver`, `running_channels`, `effective_settings`, `channel_runtime`. Refreshed on page load and each modal open.
- `configState` — from `GET /api/config`. Contains `channels`, `settings`, `startup_channels`. Refreshed on each modal open and after any mutation.

Both are plain JS objects stored in the settings modal closure.

## Component Map

```
<header>
    ├── <h1> channel name
    ├── .status-bar (indicators)
    └── #settingsBtn (gear icon) ← opens modal

#settingsModal (overlay)
    └── .settings-modal (container)
        ├── .settings-modal-header
        │   ├── h2 "Settings"
        │   └── close button
        ├── .settings-tabs (tab bar)
        │   ├── [Channels] tab
        │   └── [Settings] tab
        └── .settings-body
            ├── #channelsTab
            │   ├── .receiver-info-bar
            │   ├── .session-info (current vs saved startup)
            │   ├── channel table with inline edit/add forms
            │   └── "Add Channel" button
            └── #settingsTab
                └── settings form with source badges
```

## Control Locking Rules

Source of truth: `GET /api/runtime` → `effective_settings[field].locked` and `channel_runtime[ch].squelch.locked`.

| Control | Lock condition | Behavior when locked |
|---------|---------------|---------------------|
| Main dashboard gain slider | `effective_settings.gain.locked` | Disabled, shows "CLI override" |
| Main dashboard squelch handle | `channel_runtime[ch].squelch.locked` | Disabled (not draggable), shows "CLI override" |
| Settings modal gain field | `effective_settings.gain.locked` | Read-only, "cli" badge |
| Settings modal squelch field | `effective_settings.default_squelch.locked` | Read-only, "cli" badge |
| Channel freq/dcs/mode fields | Channel in `running_channels` | Grayed + lock icon, tooltip |

The `app.js` initialization fetches `/api/runtime` once and applies lock state to both dashboard controls and modal fields.

## Apply Timing

| Field | Effect | UI label |
|-------|--------|----------|
| `gain` | Live | "Applies immediately" |
| `channel.squelch` | Live for running channel | "Applies immediately" |
| `channel.name` | Restart | "Applied on restart" |
| `audio_preset` | Restart | "Applied on restart" |
| `tau` | Restart | "Applied on restart" |
| `record` | Restart | "Applied on restart" |
| `max_audio_mb` | Restart | "Applied on restart" |
| `tx_tail` | Restart | "Applied on restart" |
| `log_days` | Restart | "Applied on restart" |
| `default_squelch` | Startup default | "Applied on restart (new channels only)" |
| `startup_channels` | Startup only | "Monitored on next restart" |

## Running Channel Restrictions

| Field | Running channel | Not running |
|-------|----------------|-------------|
| `name` | Editable (cosmetic) | Editable |
| `squelch` | Editable (live via `set_squelch_threshold`) | Editable |
| `freq_hz` | Rejected (409) | Editable |
| `dcs_code` | Rejected (409) | Editable |
| `dcs_mode` | Rejected (409) | Editable |
| Delete | Rejected (409) | Allowed |

API returns: `{"error": "cannot change freq_hz on running channel", "locked_fields": ["freq_hz", "dcs_code", "dcs_mode"]}`

## Validation

Client-side mirrors server-side:

- **Channel ID**: `^[a-zA-Z0-9_-]+$`, unique
- **freq_hz**: integer, 1 MHz - 6 GHz
- **dcs_code**: integer 0-777, digits 0-7 only
- **dcs_mode**: "advisory" | "strict"
- **name**: non-empty, max 64 chars
- **squelch**: float, -70 to -5
- **startup_channels**: max 2, all IDs exist, no dupes, bandwidth spread <= 216 kHz

Errors displayed inline per field in the form.

## Future

- **Channel activation/switching**: Hot-switch running channels without restart. Requires flowgraph reconfiguration — separate feature, not covered here.
