"""
File Manager — Create, read, delete, move, copy, rename, list files and folders.
V3 Advanced: Find files, get largest files, get disk usage (ported from MK37).
"""

import os
import shutil
import subprocess
from pathlib import Path

# Shortcut paths
SHORTCUTS = {
    "desktop":   Path.home() / "Desktop",
    "downloads": Path.home() / "Downloads",
    "documents": Path.home() / "Documents",
    "home":      Path.home(),
    "c":         Path("C:\\"),
}

def _resolve(path: str) -> Path:
    """Resolve shortcut names to actual paths."""
    lower = path.lower().strip()
    if lower in SHORTCUTS:
        return SHORTCUTS[lower]
    return Path(path).expanduser()

def _format_size(b: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"

def _show_in_explorer(path: Path):
    """Visually open the path in File Explorer so the user sees what's happening."""
    try:
        # Don't try to select if it doesn't exist yet (e.g. create_file)
        if path.exists():
            if path.is_file():
                subprocess.run(f'explorer /select,"{path}"', shell=True)
            else:
                subprocess.run(f'explorer "{path}"', shell=True)
        else:
            # If target doesn't exist, try to open its parent
            if path.parent.exists():
                subprocess.run(f'explorer "{path.parent}"', shell=True)
    except Exception:
        pass

def file_manager(action: str, path: str, content: str = None,
                 destination: str = None, new_name: str = None,
                 query: str = None, extension: str = None,
                 count: int = 10, min_size_gb: float = 0.0) -> str:
    """Execute a file management action."""
    action = action.lower().strip()
    target = _resolve(path)
    
    # Globally provide visual feedback by opening the folder
    _show_in_explorer(target)

    try:
        if action == "create_file":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content or "", encoding="utf-8")
            return f"Created file: {target}"

        elif action == "create_folder":
            target.mkdir(parents=True, exist_ok=True)
            return f"Created folder: {target}"

        elif action == "delete":
            if target.is_file():
                target.unlink()
                return f"Deleted file: {target}"
            elif target.is_dir():
                shutil.rmtree(target)
                return f"Deleted folder: {target}"
            else:
                return f"Not found: {target}"

        elif action == "move":
            if not destination:
                return "Destination path required for move."
            dest = _resolve(destination)
            shutil.move(str(target), str(dest))
            return f"Moved {target.name} to {dest}"

        elif action == "copy":
            if not destination:
                return "Destination path required for copy."
            dest = _resolve(destination)
            if target.is_file():
                shutil.copy2(str(target), str(dest))
            else:
                shutil.copytree(str(target), str(dest / target.name))
            return f"Copied {target.name} to {dest}"

        elif action == "rename":
            if not new_name:
                return "New name required for rename."
            new_path = target.parent / new_name
            target.rename(new_path)
            return f"Renamed to {new_name}"

        elif action == "list":
            if not target.is_dir():
                return f"Not a directory: {target}"
            items = list(target.iterdir())[:30]  # Limit to 30
            lines = []
            for item in sorted(items):
                icon = "📁" if item.is_dir() else "📄"
                size = ""
                if item.is_file():
                    sz = item.stat().st_size
                    size = f" ({_format_size(sz)})"
                lines.append(f"{icon} {item.name}{size}")
            return f"Contents of {target.name}:\n" + "\n".join(lines)

        elif action == "read":
            if not target.is_file():
                return f"Not a file: {target}"
            text = target.read_text(encoding="utf-8", errors="replace")
            if len(text) > 1000:
                text = text[:1000] + "\n... (truncated)"
            return f"Contents of {target.name}:\n{text}"

        elif action == "find":
            if not target.is_dir():
                return f"Not a directory to search: {target}"
            results = []
            max_results = 30
            for item in target.rglob("*"):
                if item.is_file():
                    if extension and not item.suffix.lower().endswith(extension.lower()):
                        continue
                    if query and query.lower() not in item.name.lower():
                        continue
                    results.append(f"📄 {item.name} ({_format_size(item.stat().st_size)}) - {item.parent}")
                    if len(results) >= max_results:
                        break
            if not results:
                return f"No files found matching query '{query}' or extension '{extension}' in {target}"
            return f"Found {len(results)} file(s):\n" + "\n".join(results)

        elif action == "largest":
            if not target.is_dir():
                return f"Not a directory: {target}"
            
            min_size_bytes = int(min_size_gb * 1024 * 1024 * 1024)
            files = []
            # Scan directory up to a limit to prevent memory issues on C:\
            scanned = 0
            max_scan = 50000 
            
            for item in target.rglob("*"):
                scanned += 1
                if scanned > max_scan:
                    break
                if item.is_file():
                    try:
                        sz = item.stat().st_size
                        if sz >= min_size_bytes:
                            files.append((sz, item))
                    except Exception:
                        pass

            files.sort(reverse=True, key=lambda x: x[0])
            top = files[:count]

            if not top:
                return f"No files found larger than {min_size_gb}GB in {target} (scanned {scanned} items)."

            lines = [f"Top {len(top)} largest files in {target} (scanned {scanned} items):"]
            for sz, f in top:
                lines.append(f"  {_format_size(sz):>10}  {f.name}  ({f.parent})")
            return "\n".join(lines)

        elif action == "disk_usage":
            usage = shutil.disk_usage(target)
            pct = usage.used / usage.total * 100
            return (
                f"Disk usage ({target}):\n"
                f"  Total : {_format_size(usage.total)}\n"
                f"  Used  : {_format_size(usage.used)} ({pct:.1f}%)\n"
                f"  Free  : {_format_size(usage.free)}"
            )

        else:
            return f"Unknown file action: {action}"

    except PermissionError:
        return f"Permission denied: {target}"
    except Exception as e:
        return f"File operation failed: {e}"
