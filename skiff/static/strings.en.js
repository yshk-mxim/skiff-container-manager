// SPDX-License-Identifier: MIT
// Copyright 2026 Yakov Shkolnikov and contributors
//
// User-facing UI strings — English.
//
// Purpose (pre-i18n infrastructure):
//   Every user-visible string lives here instead of scattered across the
//   per-page JS files. A future `strings.<lang>.js` bundle + a language
//   picker is then an incremental library-add rather than a grep-across-
//   files refactor. The server-side error catalogue
//   (`skiff/contract/errors.py`) plays the equivalent role for HTTP
//   response messages.
//
// Lookup convention:
//   Use the `t(key)` helper defined in `ui.js`. Nested dots navigate the
//   tree: `t("containers.action.start")`. If the key is missing, `t()`
//   returns the key itself (makes misses obvious in the UI).
//
// Adding a string:
//   1. Pick the narrowest namespace that fits (ui.buttons > containers.actions).
//   2. Keep the English value concise — long help text stays in-source as a
//      paragraph. This dict is for labels, buttons, toasts, confirmations.
//   3. No interpolation framework yet — use placeholders like `{count}` and
//      do the substitution at the call site. Keep pluralisation simple;
//      Intl.PluralRules lands with the language picker.
//
// Out of scope (for now):
//   - Runtime language switching (ship-ready when a second locale lands).
//   - RTL text direction (`dir="rtl"` + styles.css audit) — Arabic/Hebrew
//     adopters will need a CSS pass.
//   - Server-emitted strings (error envelope `message`, audit log lines).
//     Those stay English: audit logs must be grep-friendly across
//     deployments and the structured `code` field is the machine-readable
//     identity.
window.SKIFF_STRINGS = {
  common: {
    cancel: "Cancel",
    confirm: "Confirm",
    close: "Close",
    copy: "Copy",
    copied: "Copied",
    save: "Save",
    delete: "Delete",
    refresh: "Refresh",
    loading: "Loading…",
    none: "None",
    unknown: "unknown",
    search: "Search",
    submit: "Submit",
    yes: "Yes",
    no: "No",
    error: "Error",
    success: "Success",
    sign_out: "Sign out",
    sign_in: "Sign in",
  },

  nav: {
    containers: "Containers",
    images: "Images",
    volumes: "Volumes",
    networks: "Networks",
    compose: "Compose",
    system: "System",
  },

  auth: {
    insecure_banner:
      "Insecure mode — bound to {bindHost} without an API token. " +
      "Anyone with network access to this port has full control.",
    session_expired: "Session expired — please sign in again.",
    reconnect: "Reconnect",
    token_label: "API token",
    token_placeholder: "Paste your API_TOKEN",
  },

  ws: {
    disconnected: "Disconnected",
    reconnecting: "Reconnecting…",
    idle_timeout: "Session idle — no new output for 5 minutes.",
    exec_idle_timeout: "Exec session closed after 10 minutes idle.",
  },

  containers: {
    title: "Containers",
    empty: "No containers yet. Click Run to create one.",
    columns: {
      name: "Name",
      image: "Image",
      status: "Status",
      ports: "Ports",
      created: "Created",
      actions: "Actions",
    },
    actions: {
      run: "Run",
      start: "Start",
      stop: "Stop",
      restart: "Restart",
      pause: "Pause",
      unpause: "Unpause",
      kill: "Kill",
      rename: "Rename",
      remove: "Remove",
      inspect: "Inspect",
      logs: "Logs",
      exec: "Exec",
      stats: "Stats",
      top: "Top",
      diff: "Diff",
    },
    confirm: {
      remove: "Remove container {name}? This cannot be undone after the undo window expires.",
      force_remove: "Force-remove running container {name}?",
      kill: "Send {signal} to container {name}?",
    },
    toast: {
      started: "Container started.",
      stopped: "Container stopped.",
      restarted: "Container restarted.",
      paused: "Container paused.",
      unpaused: "Container unpaused.",
      killed: "Container killed.",
      renamed: "Container renamed.",
      removed: "Container removed.",
      remove_undo: "Container will be removed in {seconds}s — click to undo.",
    },
    stats: {
      cpu: "CPU",
      memory: "Memory",
      mem_limit: "Mem limit",
      mem_percent: "Mem %",
      net_rx: "Net RX",
      net_tx: "Net TX",
      disk_read: "Disk read",
      disk_write: "Disk write",
    },
  },

  images: {
    title: "Images",
    empty: "No images yet. Pull one to get started.",
    actions: {
      pull: "Pull",
      push: "Push",
      tag: "Tag",
      remove: "Remove",
      inspect: "Inspect",
    },
    toast: {
      pulled: "Image pulled.",
      pushed: "Image pushed.",
      tagged: "Image tagged.",
      removed: "Image removed.",
    },
  },

  volumes: {
    title: "Volumes",
    empty: "No volumes yet.",
    in_use: "In use",
    unused: "Unused",
    description:
      "Named volumes persist data across container restarts. " +
      "Host-path mounts are not permitted. Volumes live on the Docker engine.",
    columns: {
      name: "Name",
      driver: "Driver",
      mountpoint: "Mountpoint",
      created: "Created",
      actions: "Actions",
    },
    inspect: {
      scope: "Scope",
      usage_bytes: "Usage (bytes)",
      ref_count: "Ref count",
      labels: "Labels",
      options: "Options",
      status: "Status",
      used_by: "Used by",
      local_default: "(local)",
      driver_not_reported: "(not reported by driver)",
    },
    actions: {
      create: "Create",
      remove: "Remove",
      prune: "Prune",
      inspect: "Inspect",
    },
    create_placeholder: "volume-name",
    confirm: {
      remove: "Remove volume {name}? Data on this volume will be permanently lost.",
      prune: "Prune all unused volumes? This permanently deletes volume data.",
    },
    toast: {
      created: "Volume created.",
      removed: "Volume removed.",
      pruned: "Pruned {count} volumes, reclaimed {size}.",
    },
  },

  networks: {
    title: "Networks",
    empty: "No networks yet.",
    actions: {
      create: "Create",
      remove: "Remove",
      connect: "Connect",
      disconnect: "Disconnect",
      inspect: "Inspect",
    },
    toast: {
      created: "Network created.",
      removed: "Network removed.",
      connected: "Container connected.",
      disconnected: "Container disconnected.",
    },
  },

  compose: {
    title: "Compose",
    upload_label: "Upload docker-compose.yml",
    project_label: "Project name",
    actions: {
      up: "Deploy",
      down: "Tear down",
      logs: "Logs",
      restart_service: "Restart service",
    },
    toast: {
      up: "Stack deployed.",
      down: "Stack torn down.",
      restarted: "Service restarted.",
    },
  },

  system: {
    title: "System",
    info_label: "Engine info",
    df_label: "Disk usage",
    prune_all_label: "Prune everything",
    confirm: {
      prune_all: "Prune all unused containers, images, volumes, networks, and build cache?",
    },
    connect: {
      header: "Connect an external tool",
      copy_hint: "Copy the snippet into your tool's config.",
    },
    account: {
      rotate_token_label: "Rotate API token",
      new_token_placeholder: "New token (min 16 chars)",
      reset_config_label: "Reset configuration",
      reset_confirm:
        "Reset config? This clears the token, Docker host, and registry list, " +
        "and reopens the 5-minute setup window.",
    },
  },

  wizard: {
    title: "Welcome to SKIFF",
    subtitle: "Configure a Docker host to get started.",
    local_tab: "Local socket",
    ssh_tab: "SSH tunnel",
    ssh_label: "SSH target",
    ssh_placeholder: "user@docker-host.example.com",
    connect_button: "Connect",
    token_label: "API token",
    token_min_hint: "Minimum 16 characters.",
    generate_token: "Generate",
    copy_token: "Copy",
    finish_button: "Finish setup",
    toast: {
      tunnel_ok: "Tunnel connected.",
      config_saved: "Configuration saved.",
    },
  },

  undo: {
    toast: "Operation undone.",
    confirm_toast: "Click to undo.",
    expired: "Undo window expired.",
    button: "Undo",
    window_passed: "Undo window has passed",
    deleted_suffix: " deleted",
    action_in_progress: "Action already in progress",
  },
};
