import hashlib
import json
import logging
import os
import re
import tarfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum, auto
from pathlib import Path
from subprocess import CalledProcessError
from typing import Any

from src.utils.imgextractor.imgextractor import Extractor
from src.utils.shell import ShellRunner
from src.utils.file_utils import remove_path, move_path

ANDROID_LOGICAL_PARTITIONS = [
    "system",
    "system_ext",
    "product",
    "vendor",
    "odm",
    "my_product",
    "my_manifest",
    "my_stock",
    "my_region",
    "my_carrier",
    "my_company",
    "my_preload",
    "my_bigball",
    "my_engineering",
    "my_heytap",
    "system_dlkm",
    "vendor_dlkm",
    "odm_dlkm",
    "product_dlkm",
]


class RomType(Enum):
    UNKNOWN = auto()
    PAYLOAD = auto()  # payload.bin
    BROTLI = auto()  # new.dat.br
    FASTBOOT = auto()  # super.img or tgz
    LOCAL_DIR = auto()  # Pre-extracted directory


class RomPackage:
    def __init__(self, file_path: str | Path, work_dir: str | Path, label: str = "Rom"):
        self.props = {}
        self.prop_history = {}  # Tracks property history: {key: [(file, value), ...]}
        self.path = Path(file_path).resolve()
        self.work_dir = Path(work_dir).resolve()
        self.label = label
        self.logger = logging.getLogger(label)
        self.shell = ShellRunner()

        # Directory structure definition
        self.images_dir = self.work_dir / "images"  # Stores .img files
        self.extracted_dir = (
            self.work_dir / "extracted"
        )  # Stores extracted folders (system, vendor...)
        self.config_dir = (
            self.work_dir / "extracted" / "config"
        )  # Stores fs_config and file_contexts

        self.rom_type = RomType.UNKNOWN
        self.props = {}

        self._detect_type()

    def _safe_remove(self, path: Path) -> None:
        """Safely remove a file or directory, logging any errors."""
        remove_path(path)

    def _cleanup_extraction_dirs(self) -> None:
        """Clean up old extraction directories and images for fresh extraction."""
        if self.extracted_dir.exists():
            self.logger.info(f"[{self.label}] Cleaning up old extracted directory...")
            remove_path(self.extracted_dir)
        if self.config_dir.exists():
            remove_path(self.config_dir)
        if any(self.images_dir.iterdir()):
            self.logger.info(f"[{self.label}] Cleaning up old images directory...")
            for item in self.images_dir.iterdir():
                remove_path(item)

    def _move_config_file(
        self, src: Path, dst: Path, file_type: str, part_name: str
    ) -> bool:
        """Move a config file from source to destination safely."""
        if src.resolve() == dst.resolve():
            return False
        if move_path(src, dst):
            self.logger.debug(f"[{self.label}] Saved {file_type} for {part_name}")
            return True
        return False

    def _detect_type(self) -> None:
        """Detects ROM type (Zip, Payload, or Local Directory)"""
        if not self.path.exists():
            raise FileNotFoundError(f"Path not found: {self.path}")

        if self.path.is_dir():
            self.rom_type = RomType.LOCAL_DIR
            self.logger.info(f"[{self.label}] Source is a local directory.")
            # If in directory mode, assume it's the working directory
            self.work_dir = self.path
            self.images_dir = self.path / "images"  # Adapting to AOSP structure
            if not self.images_dir.exists():
                self.images_dir = self.path  # Compatible if img is in root
            return

        # Simple Zip detection logic
        if zipfile.is_zipfile(self.path):
            with zipfile.ZipFile(self.path, "r") as z:
                namelist = z.namelist()
                if "payload.bin" in namelist:
                    self.rom_type = RomType.PAYLOAD
                elif any(x.endswith("new.dat.br") for x in namelist):
                    self.rom_type = RomType.BROTLI
                elif "images/super.img" in namelist or "super.img" in namelist:
                    self.rom_type = RomType.FASTBOOT
        elif self.path.suffix == ".tgz":
            self.rom_type = RomType.FASTBOOT

        self.logger.info(f"[{self.label}] Detected Type: {self.rom_type.name}")

    def extract_images(self, partitions: list[str] | None = None) -> None:
        """
        Level 1 Extraction: Convert Zip/Payload to Img
        """
        from src.core.extractors import RomExtractorFactory

        self.images_dir.mkdir(parents=True, exist_ok=True)

        # === Check if source has changed and should extract new images ===
        source_hash_path = self.work_dir / "source_file.hash"
        source_changed = True
        current_source_hash = self._compute_file_hash(self.path)

        if source_hash_path.exists():
            try:
                with open(source_hash_path, "r") as f:
                    saved_hash = f.read().strip()
                source_changed = saved_hash != current_source_hash
            except (IOError, OSError):
                self.logger.warning(
                    f"[{self.label}] Could not read hash file, re-extracting."
                )
                source_changed = True
        else:
            source_changed = True

        if source_changed:
            self.logger.info(
                f"[{self.label}] Source file changed, starting re-extraction..."
            )
            self._cleanup_extraction_dirs()
        else:
            self.logger.info(
                f"[{self.label}] Source file unchanged, checking cached data..."
            )
            if any(self.images_dir.iterdir()):
                self.logger.info(
                    f"[{self.label}] Using cached images from previous extraction."
                )
                self._batch_extract_files(partitions or ANDROID_LOGICAL_PARTITIONS)
                return
            else:
                self.logger.info(
                    f"[{self.label}] Source unchanged but images missing, re-extracting..."
                )
                source_changed = True

        # === Step 1: Extract Images via Factory ===
        try:
            extractor = RomExtractorFactory.get_extractor(
                self.rom_type, self.path, self.images_dir, self.label
            )
            extractor.extract(partitions)
        except (OSError, RuntimeError) as e:
            self.logger.error(f"[{self.label}] Image extraction failed: {e}")
            raise

        self._batch_extract_files(partitions or ANDROID_LOGICAL_PARTITIONS)

        # Save source hash after successful extraction
        if source_changed:
            try:
                with open(source_hash_path, "w") as f:
                    f.write(current_source_hash)
                self.logger.info(
                    f"[{self.label}] Saved source file hash for future change detection."
                )
            except (IOError, OSError) as e:
                self.logger.warning(
                    f"[{self.label}] Could not save source hash file: {e}"
                )

    def _compute_file_hash(self, file_path: Path) -> str:
        """Compute SHA-256 hash of a file for change detection."""
        hash_sha256 = hashlib.sha256()

        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_sha256.update(chunk)

        return hash_sha256.hexdigest()[:16]

    def _batch_extract_files(self, candidates: list[str]) -> None:
        """
        Batch call extract_partition_to_file (Parallel optimization)
        Automatically checks if img exists, skips if not (e.g., Base ROM might not have mi_ext)
        """
        self.logger.info(
            f"[{self.label}] Processing file extraction for logical partitions..."
        )

        # Dynamic worker count based on CPU cores and partition count
        cpu_count = os.cpu_count() or 4
        partition_count = len(candidates)
        # Use fewer workers for I/O bound tasks to avoid contention
        max_workers = min(cpu_count // 2 + 1, partition_count, 6)

        self.logger.debug(
            f"[{self.label}] Using {max_workers} workers for extraction (CPU: {cpu_count}, Partitions: {partition_count})"
        )

        # Pre-filter valid partitions to avoid unnecessary thread overhead
        valid_partitions = []
        for part in candidates:
            img_path = self.images_dir / f"{part}.img"
            if not img_path.exists():
                img_path = self.images_dir / f"{part}_a.img"

            if img_path.exists():
                # Check if already extracted (cache hit)
                target_dir = self.extracted_dir / part
                config_exists = (self.config_dir / f"{part}_fs_config").exists()
                has_content = target_dir.exists() and any(target_dir.iterdir())

                if has_content and config_exists:
                    self.logger.debug(
                        f"[{self.label}] Partition {part} already extracted, skipping"
                    )
                else:
                    valid_partitions.append(part)
            else:
                self.logger.debug(
                    f"[{self.label}] Partition image {part} not found, skipping extract."
                )

        if not valid_partitions:
            self.logger.info(
                f"[{self.label}] All partitions already extracted, skipping extraction"
            )
            return

        # Use ThreadPoolExecutor for parallel extraction with progress tracking
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all tasks with partition names for better error reporting
            future_to_part = {
                executor.submit(self.extract_partition_to_file, part): part
                for part in valid_partitions
            }

            completed = 0
            total = len(valid_partitions)

            for future in as_completed(future_to_part):
                part = future_to_part[future]
                try:
                    future.result()
                    completed += 1
                    if completed % 2 == 0 or completed == total:
                        self.logger.info(
                            f"[{self.label}] Extraction progress: {completed}/{total} partitions"
                        )
                except (OSError, RuntimeError) as e:
                    self.logger.error(
                        f"[{self.label}] Partition extraction failed for {part}: {e}"
                    )
                    raise

        self.logger.info(
            f"[{self.label}] Extraction completed: {completed}/{total} partitions"
        )

    def extract_partition_to_file(self, part_name: str) -> Path | None:
        """
        Level 2 Extraction: Extract Img to folder, preserving SELinux config
        :return: Path to extracted folder (e.g., build/stock/extracted/)
        """
        target_dir = self.extracted_dir / part_name

        # === Modification: Stricter cache check ===
        # Check if dir has content AND fs_config exists to consider it "extracted"
        # Otherwise consider incomplete, re-extract
        config_exists = (self.config_dir / f"{part_name}_fs_config").exists()
        has_content = target_dir.exists() and any(target_dir.iterdir())

        if has_content and config_exists:
            self.logger.info(
                f"[{self.label}] Partition {part_name} already extracted (verified)."
            )
            return target_dir

        # 2. Check if img exists
        img_path = self.images_dir / f"{part_name}.img"
        if not img_path.exists():
            # Try finding _a.img (for V-AB)
            img_path = self.images_dir / f"{part_name}_a.img"
            if not img_path.exists():
                self.logger.warning(f"[{self.label}] Image {part_name}.img not found.")
                return None

        self.logger.info(f"[{self.label}] Extracting {part_name}.img to filesystem...")
        target_dir.mkdir(parents=True, exist_ok=True)
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # 3. Detect filesystem type
        fs_type = self._detect_filesystem(img_path)
        self.logger.info(
            f"[{self.label}] Detected filesystem for {part_name}: {fs_type}"
        )

        if fs_type == "sparse_image":
            self.logger.info(
                f"[{self.label}] {part_name}.img is sparse, converting to raw..."
            )
            # We already have simg2img detection in _process_sparse_images,
            # but let's do a quick local conversion if needed.
            # Usually images from lpunpack are already raw.
            # But if somehow it's still sparse:
            try:
                from src.utils.imgextractor.imgextractor import simg2img

                simg2img(str(img_path))
                fs_type = self._detect_filesystem(img_path)
            except (OSError, RuntimeError) as e:
                self.logger.warning(f"Sparse conversion failed: {e}")

        # 4. Extract based on filesystem
        if fs_type == "erofs":
            try:
                cmd = [
                    "extract.erofs",
                    "-x",
                    "-i",
                    str(img_path),
                    "-o",
                    str(self.extracted_dir),
                ]
                self.shell.run(cmd, capture_output=True)
            except (CalledProcessError, OSError) as e:
                self.logger.error(f"EROFS extraction failed: {e}")
                # Fallback to 7z if erofs extraction tool fails unexpectedly
                try:
                    cmd = ["7z", "x", str(img_path), f"-o{self.extracted_dir}", "-y"]
                    subprocess.run(cmd, check=True)
                except (CalledProcessError, FileNotFoundError):
                    return None
        elif fs_type == "ext4":
            try:
                self.logger.info(
                    f"[{self.label}] Using Extractor for ext4 partition {part_name}"
                )
                extractor = Extractor()
                # Extractor.main handles both extraction and config generation
                extractor.main(str(img_path), str(target_dir))
            except (OSError, RuntimeError) as e:
                self.logger.error(f"EXT4 extraction failed via Extractor: {e}")
                # Fallback to 7z
                try:
                    cmd = ["7z", "x", str(img_path), f"-o{self.extracted_dir}", "-y"]
                    subprocess.run(cmd, check=True)
                except (CalledProcessError, FileNotFoundError):
                    return None
        else:
            # Unknown filesystem, try 7z as last resort
            self.logger.warning(
                f"[{self.label}] Unknown filesystem {fs_type} for {part_name}, trying 7z"
            )
            try:
                cmd = ["7z", "x", str(img_path), f"-o{self.extracted_dir}", "-y"]
                subprocess.run(cmd, check=True)
            except (CalledProcessError, FileNotFoundError) as e:
                self.logger.error(
                    f"Final extraction attempt failed for {part_name}: {e}"
                )
                return None

        # 5. [Critical] Process config files (fs_config / file_contexts)
        # Move generated config files to self.config_dir if they were not already placed there
        # Extractor.main already places them in self.config_dir (which is its self.CONFING_DIR)

        # Find potentially generated context files
        possible_contexts = list(
            target_dir.parent.glob(f"{part_name}*_file_contexts")
        ) + list(target_dir.glob("*_file_contexts"))

        possible_fs_config = list(
            target_dir.parent.glob(f"{part_name}*_fs_config")
        ) + list(target_dir.glob("*_fs_config"))

        for src in possible_contexts:
            dst = self.config_dir / f"{part_name}_file_contexts"
            if self._move_config_file(src, dst, "file_contexts", part_name):
                break

        for src in possible_fs_config:
            dst = self.config_dir / f"{part_name}_fs_config"
            if self._move_config_file(src, dst, "fs_config", part_name):
                break

        return target_dir

    def get_config_files(self, part_name: str) -> tuple[Path, Path]:
        """Get config file paths for a partition"""
        return (
            self.config_dir / f"{part_name}_fs_config",
            self.config_dir / f"{part_name}_file_contexts",
        )

    def _detect_filesystem(self, img_path: Path) -> str:
        """Detect filesystem type by reading magic bytes at standard offsets"""
        try:
            with open(img_path, "rb") as f:
                header = f.read(4096)

            if len(header) < 2048:
                self.logger.warning(f"File too small to detect filesystem: {img_path}")
                return "unknown"

            if header[0x400:0x404] == bytes([0xE2, 0xE1, 0xF5, 0xE0]):
                return "erofs"

            if header[0x438:0x43A] == bytes([0x53, 0xEF]):
                return "ext4"

            if header[0x400:0x404] == bytes([0x10, 0x20, 0xF5, 0xF2]):
                return "f2fs"

            if header[0:4] == bytes([0x3A, 0xFF, 0x26, 0xED]):
                return "sparse_image"

            return "unknown"

        except (IOError, OSError) as e:
            self.logger.warning(f"Failed to detect filesystem for {img_path}: {e}")
            return "unknown"

    def parse_all_props(self) -> None:
        """
        [Optimization] Find all build.prop files in known partition locations
        Avoids slow rglob through thousands of app/lib directories.
        """
        if not self.extracted_dir.exists():
            self.logger.warning(
                f"[{self.label}] Extracted dir not found, skipping props parsing."
            )
            return

        self.props = {}
        self.prop_history = {}

        self.logger.info(f"[{self.label}] Scanning and parsing build.prop files...")

        # 1. Define known locations for build.prop
        # Structure: <partition>/build.prop or <partition>/etc/build.prop
        partitions = [
            "system/system",
            "system",
            "vendor",
            "product",
            "odm",
            "system_ext",
            "my_product",
            "my_manifest",
            "my_stock",
            "my_region",
            "my_carrier",
            "mi_ext",
        ]

        prop_files = []
        for part in partitions:
            # Check partition root
            p1 = self.extracted_dir / part / "build.prop"
            if p1.exists():
                prop_files.append(p1)

            # Check etc directory
            p2 = self.extracted_dir / part / "etc" / "build.prop"
            if p2.exists():
                prop_files.append(p2)

        if not prop_files:
            # Fallback only if no standard props found
            self.logger.debug(
                f"[{self.label}] No standard build.prop found, falling back to limited scan."
            )
            prop_files = list(self.extracted_dir.glob("*/build.prop")) + list(
                self.extracted_dir.glob("*/etc/build.prop")
            )

        # 2. Sort (System -> Vendor -> ... -> my_product -> my_manifest)
        # Higher index means parsed LATER and thus has HIGHER priority (overwrites previous)
        def sort_priority(path: Path) -> int:
            p = str(path).lower()
            if "system/system" in p:
                return 0
            if "system/" in p:
                return 1
            if "vendor" in p:
                return 2
            if "product" in p:
                return 3
            if "odm" in p:
                return 4
            if "my_product" in p:
                return 10
            if "my_manifest" in p:
                return 11
            return 50

        # Remove duplicates while preserving order (using dict)
        prop_files = list(dict.fromkeys(prop_files))
        prop_files.sort(key=sort_priority)

        # 3. Parse one by one
        for prop_file in prop_files:
            self._load_single_prop_file(prop_file)

        self.logger.info(
            f"[{self.label}] Loaded {len(self.props)} properties from {len(prop_files)} files."
        )

    def _load_single_prop_file(self, file_path: Path) -> None:
        """Helper: Parse single file and update self.props"""
        # Calculate relative path for display (e.g. system/build.prop)
        try:
            rel_path = file_path.relative_to(self.extracted_dir)
        except ValueError:
            rel_path = file_path.name  # Fallback

        self.logger.debug(f"Parsing: {rel_path}")

        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue

                    key, value = line.split("=", 1)
                    key = key.strip()
                    value = value.strip()

                    # [Core Mod] Track history
                    if key not in self.prop_history:
                        self.prop_history[key] = []

                    # Add (source file, value) to history list
                    self.prop_history[key].append((str(rel_path), value))

                    # Update current effective value (Last-win strategy)
                    self.props[key] = value

        except (IOError, OSError) as e:
            self.logger.error(f"Error reading {rel_path}: {e}")

    def export_props(self, output_path: str | Path):
        """
        [New] Export all props to file, including Override debug info
        """
        out_file = Path(output_path)
        out_file.parent.mkdir(parents=True, exist_ok=True)

        self.logger.info(f"[{self.label}] Exporting debug props to {out_file} ...")

        # Ensure loaded
        if not self.props:
            self.parse_all_props()

        content = []
        content.append(f"# DEBUG DUMP for {self.label}")
        content.append(f"# Generated by HyperOS Porting Tool")
        content.append(f"# ==========================================\n")

        # Sort by Key for easy viewing
        for key in sorted(self.props.keys()):
            history = self.prop_history.get(key, [])
            final_val = self.props[key]

            # Check for Override (history > 1 and value changed)
            # Note: Sometimes different files define same value, counts as "override" but value unchanged
            if len(history) > 1:
                content.append(f"# [OVERRIDE DETECTED]")
                content.append(f"# {key}")
                # Print change trajectory
                for source, val in history:
                    content.append(f"#   - {source}: {val}")
                content.append(f"#   -> Final: {final_val}")

            # Write actual key-value pair
            content.append(f"{key}={final_val}")

        with open(out_file, "w", encoding="utf-8") as f:
            f.write("\n".join(content))

        self.logger.info(f"[{self.label}] Debug props saved.")

    def get_prop(self, key: str, default: str | None = None) -> str | None:
        """
        Get property value.
        Triggers full load if cache is empty.
        """
        if not self.props:
            self.parse_all_props()
        return self.props.get(key, default)

    # === ROM Properties (moved from Context) ===

    @property
    def android_version(self) -> str | None:
        """Android version (e.g., 14, 15)"""
        return self.get_prop("ro.build.version.release")

    @property
    def android_sdk(self) -> str | None:
        """Android SDK version"""
        return self.get_prop("ro.system.build.version.sdk")

    @property
    def product_device(self) -> str | None:
        """Product device name"""
        return self.get_prop("ro.product.device")

    @property
    def product_name(self) -> str | None:
        """Product name"""
        return self.get_prop("ro.product.name")

    @property
    def product_model(self) -> str | None:
        """Product model"""
        return self.get_prop("ro.product.model")

    @property
    def vendor_device(self) -> str | None:
        """Vendor device (reliable unique identifier)"""
        return self.get_prop("ro.product.vendor.device")

    @property
    def vendor_model(self) -> str | None:
        """Vendor model"""
        return self.get_prop("ro.product.vendor.model")

    @property
    def vendor_brand(self) -> str | None:
        """Vendor brand"""
        return self.get_prop("ro.product.vendor.brand")

    @property
    def device_code(self) -> str:
        """
        Device code for configuration loading.
        Uses vendor.device as primary identifier.
        """
        vendor_dev = self.vendor_device
        if vendor_dev:
            return vendor_dev.strip().replace(" ", "").upper()

        # Fallback to my_manifest version
        manifest_ver = self.get_prop("ro.oplus.version.my_manifest")
        if manifest_ver:
            return manifest_ver.split("_")[0].upper()

        # Fallback to product device
        prod_dev = self.product_device
        if prod_dev:
            return prod_dev.upper()

        return "UNKNOWN"

    @property
    def chipset_family(self) -> str:
        """Chipset family (e.g., OPSM8250)"""
        return self.get_prop("ro.build.device_family") or "unknown"

    @property
    def market_name(self) -> str | None:
        """Market name"""
        return self.get_prop("ro.vendor.oplus.market.name") or self.get_prop(
            "ro.oplus.market.name"
        )

    @property
    def market_enname(self) -> str | None:
        """English market name"""
        return self.get_prop("ro.vendor.oplus.market.enname") or self.get_prop(
            "ro.oplus.market.enname"
        )

    @property
    def region_mark(self) -> str:
        """Region mark (default: CN)"""
        return (
            self.get_prop("ro.vendor.oplus.regionmark")
            or self.get_prop("ro.oplus.regionmark")
            or "CN"
        )

    @property
    def lcd_density(self) -> str:
        """LCD density (default: 480)"""
        return self.get_prop("ro.sf.lcd_density") or "480"

    @property
    def my_product_type(self) -> str | None:
        """My product type"""
        return self.get_prop("ro.oplus.image.my_product.type")

    @property
    def security_patch(self) -> str | None:
        """Security patch level"""
        return self.get_prop("ro.build.version.security_patch")

    @property
    def display_id(self) -> str | None:
        """Build display ID"""
        return self.get_prop("ro.build.display.id")

    @property
    def display_ota(self) -> str | None:
        """Build display OTA version"""
        return self.get_prop("ro.build.display.ota")

    @property
    def oplusrom_version(self) -> str | None:
        """OPLUS ROM version"""
        return self.get_prop("ro.build.version.oplusrom")

    @property
    def area(self) -> str | None:
        """System ext area"""
        return self.get_prop("ro.oplus.image.system_ext.area")

    @property
    def brand(self) -> str | None:
        """System ext brand"""
        return self.get_prop("ro.oplus.image.system_ext.brand")

    @property
    def is_ab_device(self) -> bool:
        """Whether this is an A/B device"""
        return self.get_prop("ro.build.ab_update") == "true"

    @property
    def is_realme_ui(self) -> bool:
        """Whether this is Realme UI"""
        return self.brand == "realme"

    @property
    def is_coloros_global(self) -> bool:
        """Whether this is ColorOS Global"""
        return self.area == "gdpr" and self.brand != "oneplus"

    @property
    def is_oos(self) -> bool:
        """Whether this is OxygenOS"""
        return self.area == "gdpr" and self.brand == "oneplus"

    @property
    def is_coloros(self) -> bool:
        """Whether this is ColorOS (China)"""
        return not (self.is_coloros_global or self.is_oos or self.is_realme_ui)

    def scan_apks(self) -> dict:
        """
        Scan all APK files in extracted directory and extract metadata.
        Returns dict: {package_name: {'path': Path, 'version_code': int, 'version_name': str}}
        """
        if hasattr(self, "_apk_cache") and self._apk_cache:
            return self._apk_cache

        self._apk_cache = {}
        self.logger.info(f"[{self.label}] Scanning APKs...")

        if not self.extracted_dir.exists():
            return self._apk_cache

        apk_files = list(self.extracted_dir.rglob("*.apk"))

        def _parse_apk(apk: Path) -> tuple[str | None, dict[str, Any] | None]:
            try:
                # Use self.shell.run to handle binary pathing and LD_LIBRARY_PATH
                # Use aapt2 as it is more modern and available in the project
                # Set silent=True to avoid flooding debug log with every APK command
                result = self.shell.run(
                    ["aapt2", "dump", "badging", str(apk)],
                    capture_output=True,
                    check=False,
                    silent=True,
                )

                if result.returncode != 0:
                    return None, None

                output = result.stdout

                package_name = None
                version_code = None
                version_name = None

                for line in output.split("\n"):
                    if line.startswith("package: name='"):
                        # Format: package: name='com.foo' versionCode='123' versionName='1.0' ...
                        pkg = line.split("name='")[1].split("'")[0]
                        package_name = pkg

                        if "versionCode='" in line:
                            vc = line.split("versionCode='")[1].split("'")[0]
                            version_code = int(vc) if vc.isdigit() else None

                        if "versionName='" in line:
                            version_name = line.split("versionName='")[1].split("'")[0]
                        break  # Optimization: package info is usually on first line

                if package_name:
                    return package_name, {
                        "path": apk,
                        "relative_path": apk.relative_to(self.extracted_dir),
                        "version_code": version_code,
                        "version_name": version_name,
                    }
            except (OSError, ValueError, AttributeError) as e:
                self.logger.debug(f"Failed to parse {apk.name}: {e}")
            return None, None

        with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as executor:
            results = executor.map(_parse_apk, apk_files)

            for pkg_name, info in results:
                if pkg_name:
                    self._apk_cache[pkg_name] = info

        self.logger.info(f"[{self.label}] Found {len(self._apk_cache)} APKs")

        # Save results to file for manual inspection/debugging
        self._save_apk_scan_results()

        return self._apk_cache

    def _save_apk_scan_results(self) -> None:
        """Save APK scan cache to a JSON file for user inspection"""
        if not self._apk_cache:
            return

        # Save to build directory instead of extracted config
        project_root = Path(__file__).resolve().parent.parent.parent
        output_dir = project_root / "build"
        output_dir.mkdir(parents=True, exist_ok=True)

        output_file = output_dir / f"apk_scan_{self.label.lower()}.json"

        serializable = {}
        # Path objects are not JSON serializable, convert to strings
        for pkg, info in sorted(self._apk_cache.items()):
            serializable[pkg] = {
                "path": str(info["path"]),
                "relative_path": str(info["relative_path"]),
                "version_code": info["version_code"],
                "version_name": info["version_name"],
            }

        try:
            with open(output_file, "w", encoding="utf-8") as f:
                json.dump(serializable, f, indent=4, ensure_ascii=False)
            self.logger.info(f"[{self.label}] APK scan results saved to: {output_file}")
        except (IOError, OSError) as e:
            self.logger.warning(f"[{self.label}] Failed to save APK scan results: {e}")

    def _find_aapt(self) -> Path:
        """Find aapt binary in bin directory (Deprecated: use self.shell.run)"""
        return Path("aapt")

    @classmethod
    def detect_device_code(
        cls, rom_path: str, args_device_code: str | None = None
    ) -> str | None:
        """Detect device code from ROM metadata, filename, or arguments.

        Priority:
        1. User provided device code (args_device_code)
        2. pre-device from ZIP metadata
        3. Filename pattern "ColorOS_<CODE>_..."
        4. Return None as fallback

        Args:
            rom_path: Path to ROM file
            args_device_code: Optional user-provided device code

        Returns:
            Detected device code or None
        """
        logger = logging.getLogger(cls.__name__)

        if args_device_code:
            return args_device_code

        try:
            with zipfile.ZipFile(rom_path, "r") as zf:
                metadata_path = "META-INF/com/android/metadata"
                if metadata_path in zf.namelist():
                    with zf.open(metadata_path) as f:
                        content = f.read().decode("utf-8")
                        match = re.search(r"pre-device=(\S+)", content)
                        if match:
                            code = match.group(1)
                            logger.info(f"Detected device code from metadata: {code}")
                            return code
        except Exception as e:
            logger.debug(f"Failed to read metadata from ZIP: {e}")

        filename = Path(rom_path).name
        match = re.search(r"ColorOS_([^_]+)_", filename)
        if match:
            code = match.group(1)
            logger.info(f"Detected device code from filename: {code}")
            return code

        return None
