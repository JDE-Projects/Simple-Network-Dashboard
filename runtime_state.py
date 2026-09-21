"""Shared mutable runtime state: device inventory and recovery flags.

Held in one module so each value has a single binding that every reader and
writer reaches by attribute (`runtime_state._devices_cache`). A copied
`from runtime_state import _devices_cache` would capture a stale value once
the name is reassigned, so always access these through the module.
"""

_devices_cache: list = []
_storage_warning = False
_recovery_mode = False
_recovered_notice = False
_recovery_backup_contents: str | None = None
