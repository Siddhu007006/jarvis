"""
system/single_instance.py — Windows Named Mutex Single-Instance Guard.

Concept:
    Windows Named Mutexes are kernel objects owned by a process.
    When we call CreateMutexW with a unique name:
      - First call  → mutex is CREATED and owned by us → proceed normally.
      - Second call → mutex ALREADY EXISTS (error 183) → another Jarvis
                      is running → we exit immediately.

    The OS automatically releases the mutex when the process exits, even
    on crash — no stale lock files to clean up.

Usage:
    guard = SingleInstanceGuard()
    if not guard.acquire():
        sys.exit(0)   # Another instance is already running
"""

import ctypes
import ctypes.wintypes
import logging
import sys

log = logging.getLogger(__name__)

# Win32 error code returned when a named object already exists
ERROR_ALREADY_EXISTS = 183

# Unique mutex name for this application — use a GUID-like string so it
# never collides with another application's mutex.
MUTEX_NAME = "Global\\JarvisAssistant_SingleInstance_v4"


class SingleInstanceGuard:
    """
    Holds a Windows Named Mutex for the lifetime of the process.

    Call acquire() once at startup. If it returns False, another instance
    is already running and the caller should exit.

    The mutex handle is stored on the instance so it is NOT garbage-collected
    and stays locked for the entire process lifetime.
    """

    def __init__(self):
        self._mutex_handle = None

    def acquire(self) -> bool:
        """
        Try to become the single running instance.

        Returns:
            True  — we are the first instance; safe to continue.
            False — another instance already owns the mutex; caller should exit.
        """
        kernel32 = ctypes.windll.kernel32

        # CreateMutexW(lpMutexAttributes, bInitialOwner, lpName)
        handle = kernel32.CreateMutexW(
            None,   # default security attributes
            True,   # request immediate ownership
            MUTEX_NAME,
        )

        last_error = kernel32.GetLastError()

        if handle == 0:
            # CreateMutex itself failed (very unusual)
            log.warning("SingleInstanceGuard: CreateMutexW returned NULL (err=%d)", last_error)
            # Allow launch anyway so the app isn't permanently broken
            return True

        if last_error == ERROR_ALREADY_EXISTS:
            # Mutex already owned by another Jarvis process
            kernel32.CloseHandle(handle)
            log.info("SingleInstanceGuard: Another Jarvis instance is already running. Exiting.")
            return False

        # We created and own the mutex — store handle to prevent GC
        self._mutex_handle = handle
        log.info("SingleInstanceGuard: Mutex acquired — this is the primary instance.")
        return True

    def release(self):
        """Explicitly release the mutex (called on clean shutdown)."""
        if self._mutex_handle:
            ctypes.windll.kernel32.ReleaseMutex(self._mutex_handle)
            ctypes.windll.kernel32.CloseHandle(self._mutex_handle)
            self._mutex_handle = None
            log.info("SingleInstanceGuard: Mutex released.")


# Module-level guard instance — held for process lifetime
_guard: SingleInstanceGuard | None = None


def ensure_single_instance() -> bool:
    """
    Convenience function. Call once at program entry.

    Returns True if we should continue, False if we should exit.
    Keeps the guard alive at module level so it isn't garbage-collected.
    """
    global _guard
    _guard = SingleInstanceGuard()
    return _guard.acquire()
