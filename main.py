import argparse
import logging
import sys
from pathlib import Path

from src.core.config import Config
from src.core.rom import RomPackage
from src.core.context import Context
from src.core.props import PropertyModifier
from src.core.modifiers.unified_modifier import UnifiedModifier
from src.core.modifiers.framework_modifier import FrameworkModifier
from src.core.modifiers.firmware_modifier import FirmwareModifier
from src.core.packer import Repacker
from src.core.workflow import PortingWorkflow
from src.utils.logging_config import setup_logging
from src.utils.file_utils import clean_work_dir
from src.utils.progress import timed_stage, get_timer, reset_timer
from src.utils.perf_monitor import get_monitor, reset_monitor
from src.utils.space_manager import SpaceManager

logger = logging.getLogger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(description="ColorOS Porting Tool")
    parser.add_argument("--baserom", required=True, help="Path to Base ROM")
    parser.add_argument("--portrom", required=True, help="Path to Port ROM")
    parser.add_argument("--device_code", help="Device code for configuration override")
    parser.add_argument("--work_dir", default="build", help="Working directory")
    parser.add_argument(
        "--clean", action="store_true", help="Clean working directory before starting"
    )
    parser.add_argument(
        "--pack_type",
        choices=["super", "payload"],
        default="payload",
        help="Output format: super (Fastboot) or payload (OTA). Default: payload",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument(
        "--keep_extracted",
        action="store_true",
        help="Keep extracted directories for debugging (uses more disk space)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    work_dir = Path(args.work_dir).resolve()

    if args.clean:
        clean_work_dir(work_dir)

    setup_logging(work_dir, args.debug)

    reset_timer()
    timer = get_timer()

    reset_monitor()
    monitor = get_monitor()

    try:
        with timed_stage("Initialization"):
            monitor.log_resource_status("Initialization")

            device_code = RomPackage.detect_device_code(args.baserom, args.device_code)
            config = Config.load_safe(device_code, is_required=True)

            logger.info("Initializing ROM packages...")
            baserom = RomPackage(args.baserom, work_dir / "baserom", "BaseROM")
            portrom = RomPackage(args.portrom, work_dir / "portrom", "PortROM")

        workflow = PortingWorkflow(work_dir)

        with timed_stage("ROM Extraction"):
            try:
                workflow.run_extraction(baserom, portrom, config)
            except Exception as e:
                logger.error(f"Failed to extract ROMs: {e}")
                sys.exit(1)

            logger.info("Optimizing disk space after extraction...")
            space_mgr = SpaceManager(work_dir)
            freed = space_mgr.cleanup_after_extraction("both")
            if freed > 0:
                logger.info(f"Freed {freed / (1024**3):.2f}GB by cleaning up images")
            space_mgr.get_space_report()

        with timed_stage("Device Detection & Context Setup"):
            device_code, config = workflow.refine_device_detection(
                baserom, device_code, config
            )
            ctx = workflow.create_context(config, baserom, portrom, device_code)

        with timed_stage("Stage 1: Partition Installation"):
            logger.info("Starting Stage 1: Partition Installation...")
            ctx.install_partitions()

        with timed_stage("Export Build Props"):
            logger.info("Exporting build.prop for debugging...")
            baserom.export_props(work_dir / "build_props" / "baserom_build.prop")
            portrom.export_props(work_dir / "build_props" / "portrom_build.prop")

        with timed_stage("Stage 2: Property Modification"):
            logger.info("Starting Stage 2: Property Modification...")
            PropertyModifier(ctx).run()

        with timed_stage("Stage 3: System Modification"):
            logger.info("Starting Stage 3: System Modification...")
            unified = UnifiedModifier(ctx)
            unified.run()

        with timed_stage("Stage 3.5: Framework Smali Patching"):
            logger.info("Starting Stage 3.5: Framework Smali Patching...")
            FrameworkModifier(ctx).run()

        with timed_stage("Stage 3.5: Firmware Modification"):
            logger.info("Starting Stage 3.5: Firmware Modification (KSU/VBMeta)...")
            FirmwareModifier(ctx).run()

        with timed_stage("Stage 3.6: Feature Modules"):
            logger.info("Starting Stage 3.6: Feature Modules...")
            workflow.run_modules(ctx)

        logger.info("Preparing for final packing...")
        space_mgr = SpaceManager(work_dir)

        logger.info("Generating file manifests for debugging...")
        space_mgr.generate_manifests()

        freed = space_mgr.cleanup_after_install(
            keep_baserom=args.keep_extracted, keep_portrom=args.keep_extracted
        )
        if freed > 0:
            logger.info(
                f"Freed {freed / (1024**3):.2f}GB by cleaning up extracted directories"
            )
        space_mgr.get_space_report()

        with timed_stage("Stage 4: Repacking"):
            logger.info("Starting Stage 4: Repacking...")
            packer = Repacker(ctx)

            pack_type = args.pack_type.upper()
            if pack_type == "SUPER":
                packer.pack_all(pack_type="EROFS", is_rw=False)
                packer.pack_super_image()
                logger.info("Super image packing complete.")
            else:
                packer.pack_all(pack_type="EROFS", is_rw=False)
                packer.pack_ota_package()
                logger.info("OTA package packing complete.")

            logger.info("Generating modification diff report...")
            space_mgr = SpaceManager(work_dir)
            report_path = space_mgr.generate_diff_report()
            logger.info(f"Diff report saved to: {report_path}")

        logger.info("Porting process (Stage 1-4) complete.")

        monitor.log_resource_status("Completed")
        timer.print_summary()
        monitor.print_summary()

    except KeyboardInterrupt:
        logger.warning("Process interrupted by user")
        timer.print_summary()
        monitor.print_summary()
        sys.exit(130)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        timer.print_summary()
        monitor.print_summary()
        sys.exit(1)


if __name__ == "__main__":
    main()
