import logging
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.config import Config
    from src.core.context import Context
    from src.core.rom import RomPackage

from src.utils.progress import create_progress_tracker
from src.core.rom import ANDROID_LOGICAL_PARTITIONS
from src.utils.file_utils import copy_file

logger = logging.getLogger(__name__)


class PortingWorkflow:
    """High-level workflow orchestrating the entire ROM porting process."""

    def __init__(self, work_dir: Path):
        self.work_dir = work_dir
        self.repack_images_dir = work_dir / "repack_images"

    def copy_firmware_images(
        self, baserom: "RomPackage", repack_images_dir: Path
    ) -> int:
        """Copy firmware images from baserom to repack_images directory.

        Returns:
            Number of images copied
        """
        logger.info("Copying baserom firmware images...")

        fw_images = []

        firmware_update_dir = baserom.images_dir / "firmware-update"
        if firmware_update_dir.exists():
            fw_images.extend(firmware_update_dir.glob("*.img"))

        storage_fw = baserom.images_dir / "storage-fw.img"
        if storage_fw.exists():
            fw_images.append(storage_fw)

        for img in baserom.images_dir.glob("*.img"):
            part_name = img.stem.replace("_a", "").replace("_b", "")
            if part_name not in ANDROID_LOGICAL_PARTITIONS:
                fw_images.append(img)

        if not fw_images:
            logger.warning("No firmware images found in baserom images directory")
            return 0

        tracker = create_progress_tracker(
            total=len(fw_images), description="Copying firmware", unit="images"
        )

        copied_count = 0
        for fw_img in fw_images:
            try:
                dest = repack_images_dir / fw_img.name
                if copy_file(fw_img, dest):
                    copied_count += 1
                    tracker.update(message=f"Copied {fw_img.name}")
                else:
                    tracker.update(message=f"Failed {fw_img.name}")
            except Exception as e:
                logger.error(f"Failed to copy {fw_img.name}: {e}")
                tracker.update(message=f"Failed {fw_img.name}: {e}")

        tracker.finish()
        return copied_count

    def extract_partitions(self, baserom: "RomPackage", partitions: list[str]):
        """Extract baserom partitions with progress tracking."""
        tracker = create_progress_tracker(
            total=len(partitions),
            description="Extracting BaseROM partitions",
            unit="partitions",
        )

        for part in partitions:
            try:
                result = baserom.extract_partition_to_file(part)
                if result:
                    tracker.update(message=f"Extracted {part}")
                else:
                    tracker.update(message=f"Skipped {part} (not found)")
            except Exception as e:
                logger.warning(f"Failed to extract {part}: {e}")
                tracker.update(message=f"Failed {part}: {e}")

        tracker.finish()

    def run_extraction(
        self,
        baserom: "RomPackage",
        portrom: "RomPackage",
        config: "Config",
    ):
        """Run ROM extraction phase.

        Args:
            baserom: Base ROM package
            portrom: Port ROM package
            config: Configuration object
        """
        logger.info("Extracting ROMs...")

        baserom.extract_images()

        self.repack_images_dir.mkdir(parents=True, exist_ok=True)

        # Always try to copy firmware images from baserom to repack_images
        # as they might be needed for patching (e.g. vbmeta)
        copied_count = self.copy_firmware_images(baserom, self.repack_images_dir)
        if copied_count > 0:
            logger.info(f"Copied {copied_count} firmware images to repack_images")
        else:
            logger.info("No firmware images copied to repack_images")

        portrom.extract_images(config.partition_to_port)
        self.extract_partitions(baserom, config.baserom_partitions)

    def refine_device_detection(
        self,
        baserom: "RomPackage",
        device_code: str | None,
        config: "Config",
    ) -> tuple[str | None, "Config"]:
        """Refine device detection after ROM extraction.

        Args:
            baserom: Base ROM package
            device_code: Current device code (may be None)
            config: Current configuration

        Returns:
            Tuple of (final_device_code, final_config)
        """
        if not device_code or device_code == "common":
            base_device = baserom.get_prop("ro.product.vendor.device")
            if base_device:
                device_code = base_device.strip().replace(" ", "").upper()
                logger.info(
                    f"Detected base_device from ro.product.vendor.device: {device_code}"
                )

            if device_code:
                from src.core.config import Config

                new_config = Config.load_safe(device_code, is_required=False)
                if new_config:
                    config = new_config

        return device_code, config

    def create_context(
        self,
        config: "Config",
        baserom: "RomPackage",
        portrom: "RomPackage",
        device_code: str | None,
    ) -> "Context":
        """Create the porting context.

        Args:
            config: Configuration object
            baserom: Base ROM package
            portrom: Port ROM package
            device_code: Device code

        Returns:
            Context instance
        """
        from src.core.context import Context

        logger.info("Initializing Porting Context...")
        return Context(config, baserom, portrom, self.work_dir, device_code)

    def run_modules(self, ctx: "Context"):
        """Run high-level feature modules."""
        from src.modules.registry import ModuleRegistry

        logger.info("Starting High-level Feature Modules...")
        registry = ModuleRegistry(ctx)
        registry.discover_and_register()
        results = registry.run_all()

        succeeded = sum(1 for r in results.values() if r is True)
        failed = sum(1 for r in results.values() if r is False)
        logger.info(f"Feature Modules execution complete: {succeeded} succeeded, {failed} failed.")
        return results
