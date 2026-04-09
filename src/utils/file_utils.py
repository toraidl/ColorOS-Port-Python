import logging
import shutil
import subprocess
import os
import hashlib
from pathlib import Path
from typing import Union, Optional

logger = logging.getLogger(__name__)

def calculate_file_hash(file_path: Union[str, Path], hash_algo="sha256") -> Optional[str]:
    """
    Calculate the hash of a single file.
    """
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return None
    
    h = hashlib.new(hash_algo)
    with open(path, "rb") as f:
        # Read in 64KB chunks
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def calculate_dir_hash(dir_path: Union[str, Path], hash_algo="sha256") -> Optional[str]:
    """
    Calculate a hash representing a directory's state based on metadata of all its files.
    This includes relative path, size, and modification time for performance.
    """
    path = Path(dir_path)
    if not path.exists() or not path.is_dir():
        return None

    h = hashlib.new(hash_algo)
    # Sort paths for deterministic hash
    try:
        # Using a simple recursive walk for consistent metadata-based hashing
        for root, dirs, files in os.walk(path):
            # Sort for determinism
            dirs.sort()
            files.sort()
            
            for name in files:
                file_path = Path(root) / name
                rel_path = file_path.relative_to(path)
                
                try:
                    stat = file_path.stat()
                    # Hash path, size, and mtime
                    h.update(str(rel_path).encode())
                    h.update(str(stat.st_size).encode())
                    h.update(str(stat.st_mtime).encode())
                except (OSError, FileNotFoundError):
                    # Skip files that disappeared during walk
                    continue
            
            for name in dirs:
                dir_path_sub = Path(root) / name
                rel_path = dir_path_sub.relative_to(path)
                h.update(str(rel_path).encode())
                h.update(b"dir")
                
    except Exception as e:
        logger.error(f"Error calculating directory hash for {dir_path}: {e}")
        return None

    return h.hexdigest()

def clean_work_dir(work_dir: Path) -> None:
    """Clean and recreate working directory."""
    if work_dir.exists():
        logger.warning(f"Cleaning working directory: {work_dir}")
        remove_path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

def copy_dir(src: Union[str, Path], dst: Union[str, Path], symlinks: bool = True) -> bool:
    """
    High-performance directory copy using native 'cp' command.
    Falls back to shutil.copytree if native command fails.
    """
    src_path = Path(src).resolve()
    dst_path = Path(dst).resolve()

    if not src_path.exists():
        logger.error(f"Source directory does not exist: {src_path}")
        return False

    # Try native cp -af for performance and attribute preservation
    try:
        # -a: archive (preserve attributes, recursion, symlinks)
        # -f: force
        cmd = ["cp", "-af", f"{src_path}/.", str(dst_path)]
        dst_path.mkdir(parents=True, exist_ok=True)
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        logger.debug(f"Native cp failed, falling back to shutil: {e}")
        try:
            if dst_path.exists():
                shutil.rmtree(dst_path)
            shutil.copytree(src_path, dst_path, symlinks=symlinks, dirs_exist_ok=True)
            return True
        except Exception as e2:
            logger.error(f"Failed to copy directory {src} to {dst}: {e2}")
            return False

def copy_file(src: Union[str, Path], dst: Union[str, Path]) -> bool:
    """
    Copy a single file using native 'cp' or shutil.copy2.
    """
    src_path = Path(src).resolve()
    dst_path = Path(dst).resolve()

    try:
        if dst_path.is_dir():
            dst_path = dst_path / src_path.name
        
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Using cp -f for simple file copy
        subprocess.run(["cp", "-f", str(src_path), str(dst_path)], check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        try:
            shutil.copy2(src_path, dst_path)
            return True
        except Exception as e:
            logger.error(f"Failed to copy file {src} to {dst}: {e}")
            return False

def move_path(src: Union[str, Path], dst: Union[str, Path]) -> bool:
    """
    Move a file or directory using native 'mv' or shutil.move.
    """
    src_path = Path(src).resolve()
    dst_path = Path(dst).resolve()

    try:
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["mv", "-f", str(src_path), str(dst_path)], check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        try:
            shutil.move(str(src_path), str(dst_path))
            return True
        except Exception as e:
            logger.error(f"Failed to move {src} to {dst}: {e}")
            return False

def remove_path(path: Union[str, Path]) -> bool:
    """
    Remove a file or directory using native 'rm -rf' or shutil.
    """
    target_path = Path(path).resolve()
    if not target_path.exists():
        return True

    try:
        subprocess.run(["rm", "-rf", str(target_path)], check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        try:
            if target_path.is_dir():
                shutil.rmtree(target_path)
            else:
                target_path.unlink()
            return True
        except Exception as e:
            logger.error(f"Failed to remove {path}: {e}")
            return False
