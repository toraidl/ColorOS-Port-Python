"""
Space Manager for ROM Porting Tool

Manages disk space during ROM porting process by implementing
selective cleanup while preserving debugging capabilities.
"""

import json
import hashlib
import logging
from pathlib import Path
from typing import Optional, Dict, List
import shutil
from datetime import datetime
from src.core.rom import ANDROID_LOGICAL_PARTITIONS

logger = logging.getLogger(__name__)


class FileManifest:
    """File manifest used to replace keeping the full extracted directory"""

    def __init__(self, rom_label: str, work_dir: Path):
        self.rom_label = rom_label
        self.work_dir = Path(work_dir).resolve()
        self.manifest_dir = self.work_dir / "manifests"
        self.manifest_file = self.manifest_dir / f"{rom_label}_manifest.json"

    def generate(self, extracted_dir: Path) -> Path:
        """
        Generate file manifest from the extracted directory

        Args:
            extracted_dir: Extracted partition directory

        Returns:
            Path to the manifest file
        """
        if not extracted_dir.exists():
            logger.warning(f"[Manifest] {extracted_dir} not found, skipping")
            return None

        self.manifest_dir.mkdir(parents=True, exist_ok=True)

        manifest = {
            "rom_label": self.rom_label,
            "generated_at": datetime.now().isoformat(),
            "partitions": {},
            "summary": {"total_files": 0, "total_size": 0, "partition_count": 0},
        }

        # Iterate through all partitions
        for part_dir in sorted(extracted_dir.iterdir()):
            if not part_dir.is_dir() or part_dir.name == "config":
                continue

            partition_name = part_dir.name
            files_info = []
            part_size = 0

            logger.debug(f"[Manifest] Scanning partition: {partition_name}")

            for file_path in part_dir.rglob("*"):
                if not file_path.is_file():
                    continue

                rel_path = str(file_path.relative_to(part_dir))
                stat = file_path.stat()
                file_size = stat.st_size
                part_size += file_size

                # Calculate hash for small files (for precise comparison)
                file_hash = None
                if file_size < 10 * 1024 * 1024:  # < 10MB
                    try:
                        file_hash = self._compute_hash(file_path)
                    except Exception as e:
                        logger.debug(f"[Manifest] Hash failed for {rel_path}: {e}")

                files_info.append(
                    {
                        "path": rel_path,
                        "size": file_size,
                        "mtime": stat.st_mtime,
                        "hash": file_hash,
                        "mode": stat.st_mode,
                    }
                )

            manifest["partitions"][partition_name] = {
                "file_count": len(files_info),
                "total_size": part_size,
                "files": files_info,
            }

            manifest["summary"]["total_files"] += len(files_info)
            manifest["summary"]["total_size"] += part_size
            manifest["summary"]["partition_count"] += 1

            logger.info(
                f"[Manifest] {self.rom_label}/{partition_name}: "
                f"{len(files_info)} files, {self._human_readable(part_size)}"
            )

        # Save manifest
        with open(self.manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)

        logger.info(
            f"[Manifest] Generated: {self.manifest_file} "
            f"({manifest['summary']['total_files']} files, "
            f"{self._human_readable(manifest['summary']['total_size'])})"
        )
        return self.manifest_file

    def _compute_hash(self, file_path: Path) -> str:
        """Compute file MD5 hash"""
        h = hashlib.md5()
        with open(file_path, "rb") as f:
            while chunk := f.read(8192):
                h.update(chunk)
        return h.hexdigest()

    def _human_readable(self, size: int) -> str:
        """Convert size to human-readable format"""
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024:
                return f"{size:.1f}{unit}"
            size /= 1024
        return f"{size:.1f}PB"


class DiffReporter:
    """Difference reporter generator, compares using manifests instead of full extracted directories"""

    def __init__(self, work_dir: Path):
        self.work_dir = Path(work_dir).resolve()
        self.manifest_dir = self.work_dir / "manifests"
        self.target_dir = self.work_dir / "target"

    def generate_report(self, output_file: Optional[Path] = None) -> Path:
        """
        Generate detailed difference report between target and baserom/portrom

        Returns:
            Path to the report file
        """
        if output_file is None:
            output_file = self.work_dir / "modification_report.json"

        baserom_manifest = self._load_manifest("baserom")
        portrom_manifest = self._load_manifest("portrom")
        target_files = self._scan_target_files()

        report = {
            "generated_at": datetime.now().isoformat(),
            "summary": {
                "total_files_in_target": len(target_files),
                "files_from_baserom": 0,
                "files_from_portrom": 0,
                "modified_files": 0,
                "new_files": 0,
                "unknown_source": 0,
            },
            "partitions": {},
            "large_changes": [],
            "modified_file_list": [],
        }

        # Analyze each file
        for file_info in target_files:
            rel_path = file_info["rel_path"]
            part_name = file_info["partition"]
            file_size = file_info["size"]

            baserom_file = self._find_in_manifest(baserom_manifest, part_name, rel_path)
            portrom_file = self._find_in_manifest(portrom_manifest, part_name, rel_path)

            source = "unknown"

            if baserom_file and portrom_file:
                # Exists in both, determine source
                if self._files_equal(file_info, portrom_file):
                    source = "portrom"
                    report["summary"]["files_from_portrom"] += 1
                elif self._files_equal(file_info, baserom_file):
                    source = "baserom"
                    report["summary"]["files_from_baserom"] += 1
                else:
                    source = "modified"
                    report["summary"]["modified_files"] += 1
                    report["modified_file_list"].append(
                        {
                            "path": str(rel_path),
                            "partition": part_name,
                            "size": file_size,
                        }
                    )
                    if file_size > 10 * 1024 * 1024:  # > 10MB
                        report["large_changes"].append(
                            {
                                "path": str(rel_path),
                                "partition": part_name,
                                "size": file_size,
                                "baserom_size": baserom_file["size"],
                                "portrom_size": portrom_file["size"],
                            }
                        )
            elif portrom_file:
                source = "portrom"
                report["summary"]["files_from_portrom"] += 1
            elif baserom_file:
                source = "baserom"
                report["summary"]["files_from_baserom"] += 1
            else:
                source = "new"
                report["summary"]["new_files"] += 1

            # Statistics by partition
            if part_name not in report["partitions"]:
                report["partitions"][part_name] = {
                    "total_files": 0,
                    "from_baserom": 0,
                    "from_portrom": 0,
                    "modified": 0,
                    "new": 0,
                    "unknown": 0,
                }

            report["partitions"][part_name]["total_files"] += 1
            if source == "baserom":
                report["partitions"][part_name]["from_baserom"] += 1
            elif source == "portrom":
                report["partitions"][part_name]["from_portrom"] += 1
            elif source == "modified":
                report["partitions"][part_name]["modified"] += 1
            elif source == "new":
                report["partitions"][part_name]["new"] += 1
            else:
                report["partitions"][part_name]["unknown"] += 1
                report["summary"]["unknown_source"] += 1

        # Save report
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

        logger.info(f"[DiffReport] Generated: {output_file}")
        logger.info(f"  Files from BaseROM: {report['summary']['files_from_baserom']}")
        logger.info(f"  Files from PortROM: {report['summary']['files_from_portrom']}")
        logger.info(f"  Modified files: {report['summary']['modified_files']}")
        logger.info(f"  New files: {report['summary']['new_files']}")

        if report["large_changes"]:
            logger.info(f"  Large file changes (>10MB): {len(report['large_changes'])}")
            for change in report["large_changes"][:5]:  # Show only the first 5
                logger.info(
                    f"    - {change['path']}: {self._human_readable(change['size'])}"
                )

        return output_file

    def _load_manifest(self, rom_label: str) -> Optional[Dict]:
        """Load manifest file"""
        manifest_file = self.manifest_dir / f"{rom_label}_manifest.json"
        if not manifest_file.exists():
            return None
        try:
            with open(manifest_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[DiffReport] Failed to load {manifest_file}: {e}")
            return None

    def _scan_target_files(self) -> List[Dict]:
        """Scan all files in the target directory"""
        files = []
        if not self.target_dir.exists():
            return files

        for part_dir in self.target_dir.iterdir():
            if not part_dir.is_dir() or part_dir.name == "config":
                continue

            partition_name = part_dir.name
            for file_path in part_dir.rglob("*"):
                if file_path.is_file():
                    stat = file_path.stat()
                    files.append(
                        {
                            "rel_path": str(file_path.relative_to(part_dir)),
                            "partition": partition_name,
                            "size": stat.st_size,
                            "mtime": stat.st_mtime,
                        }
                    )
        return files

    def _find_in_manifest(
        self, manifest: Dict, partition: str, rel_path: str
    ) -> Optional[Dict]:
        """Find file in the manifest"""
        if not manifest:
            return None

        part_files = manifest.get("partitions", {}).get(partition, {}).get("files", [])
        for f in part_files:
            if f["path"] == rel_path:
                return f
        return None

    def _files_equal(self, file1: Dict, file2: Dict) -> bool:
        """Compare if two files are identical"""
        # Prefer using hashes
        if file1.get("hash") and file2.get("hash"):
            return file1["hash"] == file2["hash"]
        # Fallback to size and timestamp
        return file1["size"] == file2["size"] and file1["mtime"] == file2["mtime"]

    def _human_readable(self, size: int) -> str:
        """Convert size to human-readable format"""
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024:
                return f"{size:.1f}{unit}"
            size /= 1024
        return f"{size:.1f}PB"


class SpaceManager:
    """
    Disk Space Manager

    Core Features:
    1. Phased cleanup (after extraction, after installation, before packaging)
    2. Generate file manifests instead of keeping full directories
    3. Generate difference reports for debugging needs
    """

    def __init__(self, work_dir: Path):
        self.work_dir = Path(work_dir).resolve()
        self.baserom_dir = self.work_dir / "baserom"
        self.portrom_dir = self.work_dir / "portrom"
        self.target_dir = self.work_dir / "target"

        self.baserom_manifest = FileManifest("baserom", work_dir)
        self.portrom_manifest = FileManifest("portrom", work_dir)
        self.diff_reporter = DiffReporter(work_dir)

    def cleanup_after_extraction(self, rom_label: str = "both") -> int:
        """
        Clean up images directory after extraction is complete

        Args:
            rom_label: "baserom", "portrom", "both"

        Returns:
            Number of bytes freed
        """
        freed = 0

        if rom_label in ("baserom", "both"):
            freed += self._cleanup_images(self.baserom_dir, "BaseROM")

        if rom_label in ("portrom", "both"):
            freed += self._cleanup_images(self.portrom_dir, "PortROM")

        logger.info(
            f"[SpaceManager] Images cleanup freed: {self._human_readable(freed)}"
        )
        return freed

    def _cleanup_images(self, rom_dir: Path, label: str) -> int:
        """Clean up the images directory of a specified ROM"""
        images_dir = rom_dir / "images"
        if not images_dir.exists():
            return 0

        freed = self._get_dir_size(images_dir)

        # Preserve critical images (for firmware modification stage and packaging)
        # Any .img file that is not a logical partition is considered a firmware image to be preserved
        (self.work_dir / "repack_images").mkdir(parents=True, exist_ok=True)
        for img_path in images_dir.glob("*.img"):
            part_name = img_path.stem
            if part_name not in ANDROID_LOGICAL_PARTITIONS:
                # Move to repack_images_dir
                dest = self.work_dir / "repack_images" / img_path.name
                if not dest.exists():
                    shutil.move(str(img_path), str(dest))
                    logger.debug(f"[SpaceManager] Preserved firmware {img_path.name} for later stages")

        # Delete remaining images
        shutil.rmtree(images_dir)
        logger.info(
            f"[SpaceManager] Cleaned {label}/images, freed ~{self._human_readable(freed)}"
        )
        return freed

    def generate_manifests(self) -> Dict[str, Path]:
        """
        Generate file manifests for baserom and portrom

        Returns:
            {"baserom": Path, "portrom": Path}
        """
        baserom_extracted = self.baserom_dir / "extracted"
        portrom_extracted = self.portrom_dir / "extracted"

        baserom_manifest = self.baserom_manifest.generate(baserom_extracted)
        portrom_manifest = self.portrom_manifest.generate(portrom_extracted)

        return {"baserom": baserom_manifest, "portrom": portrom_manifest}

    def cleanup_after_install(
        self, keep_baserom: bool = True, keep_portrom: bool = False
    ) -> int:
        """
        Clean up the extracted directory after partition installation is complete

        Args:
            keep_baserom: Whether to keep baserom/extracted (for deep debugging)
            keep_portrom: Whether to keep portrom/extracted

        Returns:
            Number of bytes freed
        """
        freed = 0

        if not keep_portrom:
            freed += self._cleanup_extracted(self.portrom_dir, "PortROM")

        if not keep_baserom:
            freed += self._cleanup_extracted(self.baserom_dir, "BaseROM")

        logger.info(
            f"[SpaceManager] Post-install cleanup freed: {self._human_readable(freed)}"
        )
        return freed

    def _cleanup_extracted(self, rom_dir: Path, label: str) -> int:
        """Safely clean up the extracted directory"""
        extracted_dir = rom_dir / "extracted"
        if not extracted_dir.exists():
            return 0

        # Ensure manifest has been generated
        manifest_file = self.work_dir / "manifests" / f"{label.lower()}_manifest.json"
        if not manifest_file.exists():
            logger.warning(
                f"[SpaceManager] Manifest not found for {label}, skipping cleanup"
            )
            return 0

        freed = self._get_dir_size(extracted_dir)
        shutil.rmtree(extracted_dir)
        logger.info(
            f"[SpaceManager] Cleaned {label}/extracted, freed ~{self._human_readable(freed)}"
        )
        return freed

    def generate_diff_report(self, output_file: Optional[Path] = None) -> Path:
        """Generate a detailed difference report"""
        return self.diff_reporter.generate_report(output_file)

    def get_space_report(self) -> Dict:
        """Get current space usage report"""
        report = {
            "baserom": self._get_dir_size(self.baserom_dir),
            "portrom": self._get_dir_size(self.portrom_dir),
            "target": self._get_dir_size(self.target_dir),
            "manifests": self._get_dir_size(self.work_dir / "manifests"),
            "total": 0,
        }
        report["total"] = sum(report.values())

        # Convert to readable format
        readable = {k: self._human_readable(v) for k, v in report.items()}

        logger.info("[SpaceManager] Current space usage:")
        for key, size in readable.items():
            logger.info(f"  {key}: {size}")

        return report

    def _get_dir_size(self, path: Path) -> int:
        """Calculate directory size"""
        if not path.exists():
            return 0

        total = 0
        try:
            if path.is_file():
                return path.stat().st_size
            for f in path.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
        except Exception as e:
            logger.debug(f"[SpaceManager] Error calculating size for {path}: {e}")
        return total

    def _human_readable(self, size: int) -> str:
        """Convert size to human-readable format"""
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024:
                return f"{size:.1f}{unit}"
            size /= 1024
        return f"{size:.1f}PB"


# Utility functions
def cleanup_and_report(work_dir: Path, stage: str = "after_install") -> Dict:
    """
    Convenient cleanup and reporting function

    Args:
        work_dir: Working directory
        stage: "after_extraction", "after_install", "after_pack"

    Returns:
        Space usage report
    """
    manager = SpaceManager(work_dir)

    if stage == "after_extraction":
        manager.cleanup_after_extraction("both")
        manager.generate_manifests()
    elif stage == "after_install":
        manager.cleanup_after_install(keep_baserom=True, keep_portrom=False)
    elif stage == "after_pack":
        manager.generate_diff_report()
        manager.cleanup_after_install(keep_baserom=False, keep_portrom=False)

    return manager.get_space_report()
