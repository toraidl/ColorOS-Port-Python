import concurrent.futures
import hashlib
import os
import time
import math
import logging
import shutil
import subprocess
import zipfile
from pathlib import Path
from src.utils.shell import ShellRunner
from src.utils.fspatch import patch_fs_config
from src.utils.contextpatch import ContextPatcher
from src.core.rom import ANDROID_LOGICAL_PARTITIONS
from datetime import datetime


class Repacker:
    def __init__(self, context):
        """
        :param context: PortingContext object containing target_dir and other info
        """
        self.ctx = context
        self.logger = logging.getLogger("Packer")
        self.shell = ShellRunner()

        # Define tool paths (assumed in bin directory or system commands)
        self.bin_dir = Path("bin").resolve()

        self.selinux_patcher = ContextPatcher()
        # Fixed timestamp from shell script
        self.fix_timestamp = "1230768000"
        # Define OTA output directory structure
        self.out_dir = Path("out").resolve()
        self.product_out = (
            self.out_dir / "target" / "product" / self.ctx.baserom.vendor_device
        )
        self.images_out = self.product_out / "IMAGES"
        self.meta_out = self.product_out / "META"
        self.ota_tools_dir = Path("otatools").resolve()

    def pack_all(self, pack_type="EROFS", is_rw=False):
        """
        Pack all partitions under target directory (parallel optimization)
        :param pack_type: "EXT" (ext4) or "EROFS"
        :param is_rw: Read-write mode (only valid for EXT4)
        """
        self.logger.info(f"Starting repack with format: {pack_type}")

        # Get list of partitions to pack (exclude config and repack_images)
        partitions = [
            item.name
            for item in self.ctx.target_dir.iterdir()
            if item.is_dir() and item.name not in ["config", "repack_images"]
        ]

        if not partitions:
            self.logger.warning("No partitions found to pack")
            return

        # Dynamic worker count based on CPU cores and partition count
        # EROFS/EXT4 packing is CPU and I/O intensive, use fewer workers
        cpu_count = os.cpu_count() or 4
        partition_count = len(partitions)

        # For packing tasks, use fewer workers to avoid I/O contention
        # Large partitions benefit from dedicated resources
        max_workers = min(max(cpu_count // 4 + 1, 2), partition_count, 4)

        self.logger.info(
            f"[Packer] Using {max_workers} workers for packing (CPU: {cpu_count}, Partitions: {partition_count})"
        )

        # Sort partitions by estimated size (larger first) for better load balancing
        # This helps distribute large partitions evenly across workers
        partition_sizes = []
        for part_name in partitions:
            src_dir = self.ctx.target_dir / part_name
            try:
                size = sum(f.stat().st_size for f in src_dir.rglob("*") if f.is_file())
            except Exception:
                size = 0
            partition_sizes.append((part_name, size))

        # Sort by size descending
        partition_sizes.sort(key=lambda x: x[1], reverse=True)
        sorted_partitions = [p[0] for p in partition_sizes]

        self.logger.debug(f"[Packer] Partition order (by size): {sorted_partitions}")

        # Use ThreadPoolExecutor for parallel packing with progress tracking
        completed = 0
        total = len(sorted_partitions)
        failed_partitions = []

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all tasks with partition names for better error reporting
            future_to_part = {
                executor.submit(
                    self._pack_partition, part_name, pack_type, is_rw
                ): part_name
                for part_name in sorted_partitions
            }

            for future in concurrent.futures.as_completed(future_to_part):
                part_name = future_to_part[future]
                try:
                    future.result()
                    completed += 1
                    self.logger.info(
                        f"[Packer] Progress: {completed}/{total} partitions packed ({part_name})"
                    )
                except Exception as e:
                    failed_partitions.append(part_name)
                    self.logger.error(
                        f"[Packer] Partition packing failed for {part_name}: {e}"
                    )

        if failed_partitions:
            self.logger.error(
                f"[Packer] Failed to pack {len(failed_partitions)} partition(s): {failed_partitions}"
            )
            raise RuntimeError(f"Packing failed for: {', '.join(failed_partitions)}")

        self.logger.info(f"[Packer] Packing completed: {completed}/{total} partitions")

    def _pack_partition(self, part_name, pack_type, is_rw):
        src_dir = self.ctx.target_dir / part_name
        img_output = self.ctx.target_dir / f"{part_name}.img"
        fs_config = self.ctx.target_config_dir / f"{part_name}_fs_config"
        file_contexts = self.ctx.target_config_dir / f"{part_name}_file_contexts"

        self.logger.info(f"Packing [{part_name}] as {pack_type}...")

        self._run_patch_tools(src_dir, fs_config, file_contexts)

        if pack_type == "EXT":
            self._pack_ext4(
                part_name, src_dir, img_output, fs_config, file_contexts, is_rw
            )
        else:
            self._pack_erofs(part_name, src_dir, img_output, fs_config, file_contexts)

    def _run_patch_tools(self, src_dir, fs_config, file_contexts):
        """Call patching tools from utils"""

        if fs_config.exists():
            try:
                patch_fs_config(src_dir, fs_config)
            except Exception as e:
                self.logger.error(f"Error patching fs_config: {e}")
        else:
            self.logger.warning(
                f"fs_config not found for {src_dir.name}, skipping fspatch."
            )

        if file_contexts.exists():
            try:
                self.selinux_patcher.patch(src_dir, file_contexts)
            except Exception as e:
                self.logger.error(f"Error patching file_contexts: {e}")
        else:
            self.logger.warning(
                f"file_contexts not found for {src_dir.name}, skipping contextpatch."
            )

    def _pack_erofs(self, part_name, src_dir, img_output, fs_config, file_contexts):
        """Pack EROFS image"""
        cmd = [
            "mkfs.erofs",
            "-zlz4hc,9",
            "-T",
            self.fix_timestamp,
            "--mount-point",
            f"/{part_name}",
            "--fs-config-file",
            str(fs_config),
            "--file-contexts",
            str(file_contexts),
            str(img_output),
            str(src_dir),
        ]
        try:
            self.shell.run(cmd)
            self.logger.info(f"Successfully packed {part_name}.img (EROFS)")
        except Exception as e:
            self.logger.error(f"Failed to pack {part_name}: {e}")

    def _pack_ext4(
        self, part_name, src_dir, img_output, fs_config, file_contexts, is_rw
    ):
        """Pack EXT4 image with size calculation and regeneration"""

        # A. Calculate directory size (du -sb)
        size_orig = self._get_dir_size(src_dir)

        # B. Calculate target size
        if size_orig < 1048576:  # 1MB
            size = 1048576
        elif size_orig < 104857600:  # 100MB
            size = int(size_orig * 1.15)
        elif size_orig < 1073741824:  # 1GB
            size = int(size_orig * 1.08)
        else:
            size = int(size_orig * 1.03)

        # Align to 4K
        size = (size // 4096) * 4096

        # C. Prepare lost+found
        lost_found = src_dir / "lost+found"
        lost_found.mkdir(exist_ok=True)

        # D. Calculate Inode count
        try:
            with open(fs_config, "r") as f:
                inode_count = sum(1 for _ in f) + 8
        except:
            inode_count = 5000  # Fallback

        # E. First generation
        self._make_ext4_image(
            part_name,
            src_dir,
            img_output,
            size,
            inode_count,
            fs_config,
            file_contexts,
            is_rw,
        )

        # F. Shrink size (resize2fs -M)
        self.shell.run(["resize2fs", "-f", "-M", str(img_output)])

        # Get Free blocks after resize
        free_blocks = self._get_free_blocks(img_output)

        # If there is free space and not Readaw
        if free_blocks > 0:
            free_size = free_blocks * 4096
            current_img_size = img_output.stat().st_size

            # Calculate new compact size
            new_size = current_img_size - free_size
            new_size = (new_size // 4096) * 4096

            self.logger.info(
                f"Regenerating {part_name}.img with optimized size: {new_size}"
            )
            img_output.unlink()  # Delete old

            # Second generation
            self._make_ext4_image(
                part_name,
                src_dir,
                img_output,
                new_size,
                inode_count,
                fs_config,
                file_contexts,
                is_rw,
            )
            self.shell.run(["resize2fs", "-f", "-M", str(img_output)])

    def _make_ext4_image(
        self,
        part_name,
        src_dir,
        img_path,
        size,
        inodes,
        fs_config,
        file_contexts,
        is_rw,
    ):
        """Execute mke2fs and e2fsdroid"""
        # 1. mke2fs (create empty image)
        mkfs_cmd = [
            "mke2fs",
            "-O",
            "^has_journal",
            "-L",
            part_name,
            "-I",
            "256",
            "-N",
            str(inodes),
            "-M",
            f"/{part_name}",
            "-m",
            "0",
            "-t",
            "ext4",
            "-b",
            "4096",
            str(img_path),
            str(size // 4096) + "K",
        ]
        mkfs_cmd[-1] = str(size // 4096)

        self.shell.run(mkfs_cmd)

        # 2. e2fsdroid (write files)
        e2fs_cmd = [
            "e2fsdroid",
            "-e",
            "-T",
            self.fix_timestamp,
            "-C",
            str(fs_config),
            "-S",
            str(file_contexts),
            "-f",
            str(src_dir),
            "-a",
            f"/{part_name}",
            str(img_path),
        ]

        # If not RW mode, add -s (share_dupe)
        if not is_rw:
            e2fs_cmd.insert(-1, "-s")

        self.shell.run(e2fs_cmd)

    def _get_dir_size(self, path):
        """
        Calculate directory size using du -sb (much faster than Python rglob)
        """
        try:
            output = subprocess.check_output(["du", "-sb", str(path)], text=True)
            return int(output.split()[0])
        except Exception as e:
            self.logger.warning(f"du command failed, falling back to python: {e}")
            total = 0
            for p in path.rglob("*"):
                if p.is_file() and not p.is_symlink():
                    total += p.stat().st_size
            return total if total > 0 else 4096

    def _get_free_blocks(self, img_path):
        """Parse tune2fs -l output to get Free blocks"""
        try:
            output = subprocess.check_output(
                ["tune2fs", "-l", str(img_path)], text=True
            )
            for line in output.splitlines():
                if "Free blocks:" in line:
                    return int(line.split(":")[1].strip())
        except:
            return 0
        return 0

        return 0

    def pack_super_image(self):
        """
        Pack super.img for non-payload.bin ROMs
        """
        self.logger.info("Packing super.img...")

        # 1. Define paths
        lpmake_path = self.ota_tools_dir / "bin" / "lpmake"
        if not lpmake_path.exists():
            self.logger.error(f"lpmake not found at {lpmake_path}")
            return

        super_img = self.ctx.target_dir / "super.img"
        super_size = self._get_super_size()

        # 2. Base arguments
        # --metadata-size 65536 --super-name super --block-size 4096
        base_args = [
            str(lpmake_path),
            "--metadata-size",
            "65536",
            "--super-name",
            "super",
            "--block-size",
            "4096",
            "--device",
            f"super:{super_size}",
            "--output",
            str(super_img),
        ]

        # 3. Handle A-only vs V-AB
        is_ab = self.ctx.is_ab_device

        if not is_ab:
            self.logger.info("Packing A-only super.img")
            # --metadata-slots 2 --group=qti_dynamic_partitions:$superSize
            base_args.extend(["--metadata-slots", "2"])
            base_args.extend(["--group", f"qti_dynamic_partitions:{super_size}"])
            base_args.append("-F")  # Sparse

            partitions = [
                "system",
                "vendor",
                "product",
                "system_ext",
                "odm",
                "my_product",
                "my_manifest",
                "my_stock",
                "my_region",
                "my_carrier",
                "my_heytap",
                "my_bigball",
                "my_engineering",
            ]

            for part in partitions:
                img_path = self.ctx.target_dir / f"{part}.img"
                if img_path.exists():
                    size = img_path.stat().st_size
                    self.logger.info(f"Partition [{part}]: {size} bytes")
                    # --partition name:attributes:size:group --image name=path
                    # attributes: none (or readonly)
                    base_args.extend(
                        [
                            "--partition",
                            f"{part}:none:{size}:qti_dynamic_partitions",
                            "--image",
                            f"{part}={img_path}",
                        ]
                    )
        else:
            self.logger.info("Packing V-AB super.img")
            # --virtual-ab --metadata-slots 3
            # --group=qti_dynamic_partitions_a:$superSize --group=qti_dynamic_partitions_b:$superSize
            base_args.extend(["--virtual-ab", "--metadata-slots", "3"])
            base_args.extend(["--group", f"qti_dynamic_partitions_a:{super_size}"])
            base_args.extend(["--group", f"qti_dynamic_partitions_b:{super_size}"])
            base_args.append("-F")

            # Scan partitions
            # Use super_list from context if available, or scan standard names
            partitions = [
                "system",
                "vendor",
                "product",
                "system_ext",
                "odm",
                "my_product",
                "my_manifest",
                "my_stock",
                "my_region",
                "my_carrier",
                "my_heytap",
                "my_bigball",
                "my_engineering",
                "my_preload",
                "my_company",
            ]

            for part in partitions:
                img_path = self.ctx.target_dir / f"{part}.img"
                if img_path.exists():
                    size = img_path.stat().st_size
                    self.logger.info(f"Partition [{part}]: {size} bytes")
                    # --partition name_a:none:size:group_a --image name_a=path
                    # --partition name_b:none:0:group_b
                    base_args.extend(
                        [
                            "--partition",
                            f"{part}_a:none:{size}:qti_dynamic_partitions_a",
                            "--image",
                            f"{part}_a={img_path}",
                            "--partition",
                            f"{part}_b:none:0:qti_dynamic_partitions_b",
                        ]
                    )

        # 4. Run lpmake
        try:
            self.shell.run(base_args)
            self.logger.info("super.img generated successfully.")
        except Exception as e:
            self.logger.error(f"Failed to generate super.img: {e}")
            return

        # 5. Compress to super.zst
        self.logger.info("Compressing super.img to super.zst...")
        zst_path = self.ctx.target_dir / "super.zst"
        try:
            # Try to use zstd from system, or bin/zstd
            # Assume system zstd is available or copy it
            self.shell.run(["zstd", "--rm", str(super_img), "-o", str(zst_path)])
            self.logger.info("Compressed super.zst generated.")
        except Exception as e:
            self.logger.warning(f"zstd compression failed: {e}. Keeping super.img")
            # Fallback: if zstd fails, keep super.img?
            # The flash script expects super.zst usually.

        # 6. Generate Flashing Script (Output folder)
        self._generate_flash_script(zst_path if zst_path.exists() else super_img)

    def _generate_flash_script(self, super_image_path):
        """
        Generate hybrid flashing scripts (Fastboot + Recovery)
        Structure:
        /
        ├── super.zst
        ├── firmware-update/
        ├── META-INF/
        │   ├── com/google/android/update-binary
        │   ├── com/google/android/updater-script
        │   └── zstd
        ├── bin/
        │   └── windows/ (adb, fastboot, zstd.exe)
        ├── windows_flash_script.bat
        └── mac_linux_flash_script.sh
        """
        self.logger.info("Generating hybrid flashing scripts...")

        # Prepare output directory
        out_name = (
            f"{self.ctx.baserom.vendor_device}_{self.ctx.target_rom_version}_hybrid"
        )
        out_path = self.out_dir / out_name

        if out_path.exists():
            shutil.rmtree(out_path)
        out_path.mkdir(parents=True, exist_ok=True)

        # 1. Create directory structure
        bin_windows = out_path / "bin/windows"
        bin_windows.mkdir(parents=True, exist_ok=True)

        firmware_update = out_path / "firmware-update"
        firmware_update.mkdir(parents=True, exist_ok=True)

        meta_inf = out_path / "META-INF/com/google/android"
        meta_inf.mkdir(parents=True, exist_ok=True)

        # 2. Copy super image (must be zst for hybrid)
        self.logger.info(f"Copying {super_image_path.name}...")
        shutil.copy2(super_image_path, out_path / "super.zst")

        # 3. Copy firmware images
        if self.ctx.repack_images_dir.exists():
            for fw in self.ctx.repack_images_dir.glob("*.img"):
                # Special handling for boot.img: place in root
                if fw.name == "boot.img":
                    shutil.copy2(fw, out_path / "boot.img")
                else:
                    shutil.copy2(fw, firmware_update)

        # 4. Copy tools and scripts
        flash_template = Path("bin/flash")

        if flash_template.exists():
            # A. Windows Tools
            if (flash_template / "platform-tools-windows").exists():
                shutil.copytree(
                    flash_template / "platform-tools-windows",
                    bin_windows,
                    dirs_exist_ok=True,
                )

            # B. Recovery Tools (zstd)
            # The update-binary expects META-INF/zstd
            zstd_bin = flash_template / "zstd"
            if zstd_bin.exists():
                shutil.copy2(zstd_bin, out_path / "META-INF/zstd")

            # C. Scripts & Update Binary
            files_to_process = {
                "windows_flash_script.bat": out_path / "windows_flash_script.bat",
                "mac_linux_flash_script.sh": out_path / "mac_linux_flash_script.sh",
                "update-binary": meta_inf / "update-binary",
            }

            # Create dummy updater-script (required by TWRP)
            (meta_inf / "updater-script").write_text("# dummy\n", encoding="utf-8")

            for src_name, dest_path in files_to_process.items():
                src_file = flash_template / src_name
                if src_file.exists():
                    shutil.copy2(src_file, dest_path)
                    self._process_script_placeholders(dest_path)

                    # Specific handling for Fastboot scripts
                    if "flash_script" in src_name:
                        if not self.ctx.is_ab_device:
                            self._patch_script_for_a_only(dest_path)
                        self._patch_script_for_firmware(dest_path, firmware_update)

                    # Specific handling for Recovery script
                    if src_name == "update-binary":
                        if not self.ctx.is_ab_device:
                            self._patch_update_binary_for_a_only(dest_path)
                        self._patch_update_binary_firmware(dest_path, firmware_update)

        # 5. Zip the package
        self.logger.info("Zipping hybrid package...")
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        final_zip_name = f"{self.ctx.baserom.vendor_device}-hybrid-{self.ctx.target_rom_version}-{timestamp}.zip"
        final_zip_path = self.out_dir / final_zip_name

        # Create zip manually to control compression
        with zipfile.ZipFile(
            final_zip_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as zf:
            for root, dirs, files in os.walk(out_path):
                for file in files:
                    file_path = Path(root) / file
                    arcname = file_path.relative_to(out_path)

                    if file == "super.zst":
                        self.logger.info(f"Adding super.zst (STORED)...")
                        zf.write(file_path, arcname, compress_type=zipfile.ZIP_STORED)
                    else:
                        zf.write(file_path, arcname)

        # Compute MD5
        md5 = hashlib.md5(open(final_zip_path, "rb").read()).hexdigest()[:10]
        # Rename to match update-binary expectation: Device_Version_Date_MD5_Type.zip
        # update-binary uses `cut -d '_' -f 5` to get MD5
        # So format should be: Part1_Part2_Part3_Part4_MD5_Part6.zip
        # Mapping: Device_Hybrid_Version_SecurityPatch_MD5_Timestamp.zip

        renamed_zip_name = f"{self.ctx.baserom.vendor_device}_Hybrid_{self.ctx.target_rom_version}_{self.ctx.security_patch}_{md5}_{timestamp}.zip"
        renamed_zip_path = self.out_dir / renamed_zip_name
        final_zip_path.rename(renamed_zip_path)

        self.logger.info(f"Hybrid ROM generated: {renamed_zip_path}")

        # Clean up temporary output directory
        shutil.rmtree(out_path)

    def _process_script_placeholders(self, file_path):
        """Replace placeholders in scripts/update-binary"""
        content = file_path.read_text(encoding="utf-8", errors="ignore")

        replacements = {
            "device_code": self.ctx.baserom.vendor_device,
            "baseversion": self.ctx.baserom.android_version,  # Or full version string?
            "portversion": self.ctx.target_rom_version,
        }

        for key, value in replacements.items():
            content = content.replace(key, str(value))

        file_path.write_text(content, encoding="utf-8")

    def _patch_script_for_a_only(self, script_path):
        """Remove _a/_b references for A-only devices (Fastboot)"""
        content = script_path.read_text(encoding="utf-8", errors="ignore")

        # Simple replacements
        content = content.replace("_a", "")
        content = content.replace("_b", "")

        lines = content.splitlines()
        new_lines = [line for line in lines if "_b" not in line]

        script_path.write_text("\n".join(new_lines), encoding="utf-8")

    def _patch_update_binary_for_a_only(self, script_path):
        """Patch update-binary for A-only devices (Recovery)"""
        content = script_path.read_text(encoding="utf-8", errors="ignore")

        # 1. Replace partition names
        # boot_a/boot_b -> boot
        # dtbo_a/dtbo_b -> dtbo
        content = content.replace("boot_a", "boot").replace("boot_b", "boot")
        content = content.replace("dtbo_a", "dtbo").replace("dtbo_b", "dtbo")

        # 2. Remove A/B specific commands
        # Remove bootctl set-active-boot-slot a
        content = content.replace("bootctl set-active-boot-slot a", "")

        # 3. Remove/Comment out lptools unmap commands (usually for V-AB)
        # The template has #REMAP_START / #REMAP_END blocks
        # We can just remove lines containing "lptools unmap"
        lines = content.splitlines()
        new_lines = []
        for line in lines:
            if "lptools unmap" in line:
                continue
            new_lines.append(line)

        script_path.write_text("\n".join(new_lines), encoding="utf-8")

    def _patch_update_binary_firmware(self, script_path, firmware_dir):
        """Inject firmware flashing commands into update-binary"""
        fw_files = [f.name for f in firmware_dir.glob("*")]
        # Also check root for boot.img (as we moved it there)
        root_dir = firmware_dir.parent
        if (root_dir / "boot.img").exists():
            # boot.img is handled statically in update-binary template now,
            # but we should ensure we don't double flash if it was somehow in firmware-update too
            pass

        if not fw_files:
            return

        content = script_path.read_text(encoding="utf-8", errors="ignore")
        insertion = []

        for fw in fw_files:
            # Map filename to partition name
            part = fw.split(".")[0]
            if fw == "uefi_sec.mbn":
                part = "uefisecapp"
            elif fw == "qupv3fw.elf":
                part = "qupfw"
            elif fw == "NON-HLOS.bin":
                part = "modem"
            elif fw == "km4.mbn":
                part = "keymaster"
            elif fw == "BTFM.bin":
                part = "bluetooth"
            elif fw == "dspso.bin":
                part = "dsp"

            # Skip dtbo/cust if needed (already handled or custom)
            if "dtbo" in fw or "cust" in fw:
                continue

            # Skip boot.img if it ended up here (should be in root)
            if fw == "boot.img":
                continue

            # Generate shell command for update-binary
            # package_extract_file "firmware-update/fw.img" "/dev/block/bootdevice/by-name/part"

            if self.ctx.is_ab_device:
                insertion.append(
                    f'package_extract_file "firmware-update/{fw}" "/dev/block/bootdevice/by-name/{part}_a"'
                )
                insertion.append(
                    f'package_extract_file "firmware-update/{fw}" "/dev/block/bootdevice/by-name/{part}_b"'
                )
            else:
                insertion.append(
                    f'package_extract_file "firmware-update/{fw}" "/dev/block/bootdevice/by-name/{part}"'
                )

        # Insert after "# firmware" marker
        marker = "# firmware"
        if marker in content:
            parts = content.split(marker)
            new_content = parts[0] + marker + "\n" + "\n".join(insertion) + parts[1]
            script_path.write_text(new_content, encoding="utf-8")
        else:
            # If marker not found, append before super flash?
            # Or just warn. The template should have the marker.
            self.logger.warning(
                f"Marker '{marker}' not found in update-binary, firmware flashing might be missing."
            )

    def _patch_script_for_firmware(self, script_path, firmware_dir):
        """Inject firmware flash commands"""
        # Read firmware files
        fw_files = [f.name for f in firmware_dir.glob("*")]
        if not fw_files:
            return

        content = script_path.read_text(encoding="utf-8", errors="ignore")

        # Generate insertion block
        is_windows = script_path.suffix == ".bat"
        insertion = []

        for fw in fw_files:
            # Map filename to partition name
            # mapping logic from port.sh lines 1761+
            part = fw.split(".")[0]  # Default
            if fw == "uefi_sec.mbn":
                part = "uefisecapp"
            elif fw == "qupv3fw.elf":
                part = "qupfw"
            elif fw == "NON-HLOS.bin":
                part = "modem"
            elif fw == "km4.mbn":
                part = "keymaster"
            elif fw == "BTFM.bin":
                part = "bluetooth"
            elif fw == "dspso.bin":
                part = "dsp"

            # Skip dtbo/cust if needed (port.sh line 1759)
            if "dtbo" in fw or "cust" in fw:
                continue

            # Skip boot.img (handled at root)
            if fw == "boot.img":
                continue

            if self.ctx.is_ab_device:
                if is_windows:
                    insertion.append(
                        f"bin\\windows\\fastboot.exe flash {part}_a %~dp0firmware-update\\{fw}"
                    )
                    insertion.append(
                        f"bin\\windows\\fastboot.exe flash {part}_b %~dp0firmware-update\\{fw}"
                    )
                else:
                    insertion.append(f"fastboot flash {part}_a firmware-update/{fw}")
                    insertion.append(f"fastboot flash {part}_b firmware-update/{fw}")
            else:
                # A-only
                if is_windows:
                    insertion.append(
                        f"bin\\windows\\fastboot.exe flash {part} %~dp0firmware-update\\{fw}"
                    )
                else:
                    insertion.append(f"fastboot flash {part} firmware-update/{fw}")

        # Insert after "# firmware" marker
        marker = "REM firmware" if is_windows else "# firmware"

        if marker in content:
            parts = content.split(marker)
            new_content = parts[0] + marker + "\n" + "\n".join(insertion) + parts[1]
            script_path.write_text(new_content, encoding="utf-8")

    def _generate_map_files(self):
        """Generate map files for dynamic partitions using map_file_generator"""
        partitions = [
            "system",
            "vendor",
            "product",
            "system_ext",
            "odm",
            "my_product",
            "my_manifest",
            "my_stock",
            "my_region",
            "my_carrier",
            "my_heytap",
            "my_bigball",
            "my_engineering",
            "my_preload",
            "my_company",
        ]

        map_tool = str(Path("otatools/bin/map_file_generator"))

        for part_name in partitions:
            img_path = self.ctx.target_dir / f"{part_name}.img"
            map_path = self.ctx.target_dir / f"{part_name}.map"

            if not img_path.exists():
                continue

            try:
                self.shell.run([map_tool, str(img_path), str(map_path)])
                self.logger.info(f"Generated map for {part_name}.img")
            except Exception as e:
                self.logger.warning(f"Failed to generate map for {part_name}: {e}")

    def pack_ota_package(self):
        """
        Pack OTA package (supports both A/B payload and A-only formats)
        """
        is_ab = self.ctx.is_ab_device
        if is_ab:
            self.logger.info("Starting A/B OTA (payload) packing...")
        else:
            self.logger.info("Starting A-only OTA packing...")

        if self.product_out.exists():
            shutil.rmtree(self.product_out)

        self.images_out.mkdir(parents=True, exist_ok=True)
        self.meta_out.mkdir(parents=True, exist_ok=True)

        # Base partitions for all OTA types
        base_parts = [
            "SYSTEM",
            "SYSTEM_EXT",
            "PRODUCT",
            "VENDOR",
            "ODM",
            "MY_PRODUCT",
            "MY_MANIFEST",
            "MY_STOCK",
            "MY_REGION",
            "MY_CARRIER",
            "MY_HEYTAP",
            "MY_BIGBALL",
            "MY_ENGINEERING",
        ]

        # A/B devices need additional partitions for third-party super image support
        if is_ab:
            base_parts.extend(["MY_PRELOAD", "MY_COMPANY"])

        for part in base_parts:
            (self.product_out / part).mkdir(exist_ok=True)

        if not is_ab:
            self.logger.info("Generating map files for A-only OTA...")
            self._generate_map_files()

        self.logger.info("Collecting logical partition images...")
        for img in self.ctx.target_dir.glob("*.img"):
            shutil.move(str(img), str(self.images_out / img.name))
            # Also copy corresponding .map file if exists (for A-only OTA)
            map_file = img.with_suffix(".map")
            if map_file.exists():
                shutil.copy2(str(map_file), str(self.images_out / f"{img.stem}.map"))
                self.logger.debug(f"Copied {img.stem}.map to IMAGES/")

        self.logger.info("Collecting firmware images...")
        if self.ctx.repack_images_dir.exists():
            for img in self.ctx.repack_images_dir.glob("*.img"):
                shutil.move(str(img), str(self.images_out / img.name))

        device_custom_dir = Path(f"devices/target/{self.ctx.baserom.vendor_device}")
        if device_custom_dir.exists():
            # Handle boot/dtbo replacement
            ksu_boot = list(device_custom_dir.glob("boot*.img"))
            if ksu_boot:
                shutil.copy2(ksu_boot[0], self.images_out / "boot.img")
                self.logger.info(f"Replaced boot.img with {ksu_boot[0].name}")

            dtbo = list(device_custom_dir.glob("dtbo*.img"))
            if dtbo:
                shutil.copy2(dtbo[0], self.images_out / "dtbo.img")

            # Handle recovery
            rec = device_custom_dir / "recovery.img"
            if rec.exists():
                shutil.copy2(rec, self.images_out)

            # Handle init_boot
            init_boot = device_custom_dir / "init_boot-kernelsu.img"
            if init_boot.exists():
                shutil.copy2(init_boot, self.images_out / "init_boot.img")

        # Handle my_preload.img and my_company.img for third-party packs
        self._handle_special_partitions()

        # Generate META info
        self._generate_meta_info()

        # Copy build.prop to corresponding directories (for OTA tool to read fingerprint info)
        self._copy_build_props()

        # Call ota_from_target_files
        self._run_ota_tool()

    def _handle_special_partitions(self):
        """
        Handle my_preload.img and my_company.img for third-party packs.
        Third-party super partitions must contain these two partitions to boot properly.
        Official OTA packs are modified and usually don't contain these images.
        """
        is_ab = getattr(self.ctx, "is_ab_device", False)

        preload_empty = Path("devices/common/my_preload_empty.img")
        company_empty = Path("devices/common/my_company_empty.img")

        # Ensure assets are present
        if is_ab:
            if hasattr(self.ctx, "assets"):
                self.ctx.assets.ensure_asset(preload_empty)
                self.ctx.assets.ensure_asset(company_empty)

        preload_img = self.images_out / "my_preload.img"
        company_img = self.images_out / "my_company.img"

        if is_ab:
            if not preload_img.exists() and preload_empty.exists():
                shutil.copy2(preload_empty, preload_img)
                self.logger.info("Added missing my_preload.img from devices/common")

            if not company_img.exists() and company_empty.exists():
                shutil.copy2(company_empty, company_img)
                self.logger.info("Added missing my_company.img from devices/common")
        else:
            if company_img.exists():
                company_img.unlink()
                self.logger.info("Removed my_company.img for non-AB device")

            if preload_img.exists():
                preload_img.unlink()
                self.logger.info("Removed my_preload.img for non-AB device")

    def _generate_meta_info(self):
        """Generate ab_partitions.txt, dynamic_partitions_info.txt, misc_info.txt"""
        self.logger.info("Generating META info...")
        is_ab = self.ctx.is_ab_device

        # --- ab_partitions.txt ---
        ab_txt = self.meta_out / "ab_partitions.txt"
        partition_list = []

        # Scan all img under IMAGES
        for img in self.images_out.glob("*.img"):
            if img.stem == "cust":
                continue
            partition_list.append(img.stem)

        with open(ab_txt, "w") as f:
            for p in sorted(partition_list):
                f.write(f"{p}\n")

        # --- dynamic_partitions_info.txt ---
        super_size = self._get_super_size()
        group_size = super_size - 1048576  # Reserve 1MB

        super_parts = [
            p
            for p in partition_list
            if p
            in [
                "system",
                "vendor",
                "product",
                "system_ext",
                "odm",
                "odm_dlkm",
                "vendor_dlkm",
                "system_dlkm",
                "product_dlkm",
                "my_manifest",
                "my_product",
                "my_stock",
                "my_region",
                "my_carrier",
                "my_heytap",
                "my_bigball",
                "my_engineering",
                "my_preload",
                "my_company",
            ]
        ]
        super_parts_str = " ".join(super_parts)

        dyn_txt = self.meta_out / "dynamic_partitions_info.txt"
        with open(dyn_txt, "w") as f:
            f.write(f"super_partition_size={super_size}\n")
            f.write(f"super_partition_groups=qti_dynamic_partitions\n")
            f.write(f"super_qti_dynamic_partitions_group_size={group_size}\n")
            f.write(f"super_qti_dynamic_partitions_partition_list={super_parts_str}\n")
            if is_ab:
                f.write(f"virtual_ab=true\n")
                f.write(f"virtual_ab_compression=true\n")

        # --- misc_info.txt ---
        misc_txt = self.meta_out / "misc_info.txt"
        with open(misc_txt, "w") as f:
            f.write("recovery_api_version=3\n")
            f.write("fstab_version=2\n")
            if is_ab:
                f.write("ab_update=true\n")
            else:
                # A-only specific config
                f.write("ab_update=false\n")
                f.write("blockimgdiff_versions=3,4\n")
                f.write("use_dynamic_partitions=true\n")
                f.write(f"dynamic_partition_list={super_parts_str}\n")
                f.write("super_partition_groups=qti_dynamic_partitions\n")
                f.write(f"super_qti_dynamic_partitions_group_size={super_size}\n")
                f.write(
                    f"super_qti_dynamic_partitions_partition_list={super_parts_str}\n"
                )
                f.write("board_uses_vendorimage=true\n")
                f.write("cache_size=402653184\n")
            # Specify the key path for ota_from_target_files
            f.write(
                "default_system_dev_certificate=build/make/target/product/security/testkey\n"
            )

        # --- update_engine_config.txt ---
        ue_txt = self.meta_out / "update_engine_config.txt"
        with open(ue_txt, "w") as f:
            f.write("PAYLOAD_MAJOR_VERSION=2\n")
            f.write("PAYLOAD_MINOR_VERSION=8\n")

        # --- A-only specific setup ---
        if not is_ab:
            self._setup_a_only_ota_structure(partition_list)

    def _copy_build_props(self):
        """Copy build.prop of each partition to directories required by META structure - port.sh logic"""

        # Mapping from port.sh: prop_paths
        prop_mapping = {
            "system": "SYSTEM",
            "product": "PRODUCT",
            "system_ext": "SYSTEM_EXT",
            "vendor": "VENDOR",
            "my_manifest": "ODM",
        }

        for part_lower, part_upper in prop_mapping.items():
            # Find build.prop in partition directory (similar to port.sh find command)
            src_prop = None

            # Search in target_dir/part_lower directory for build.prop
            search_dir = self.ctx.target_dir / part_lower
            if search_dir.exists():
                # Find build.prop directly in partition dir (not in subdirs like system_dlkm, odm_dlkm)
                for f in search_dir.rglob("build.prop"):
                    # Skip system_dlkm and odm_dlkm subdirs
                    if "system_dlkm" not in str(f) and "odm_dlkm" not in str(f):
                        src_prop = f
                        break

            if src_prop and src_prop.exists():
                shutil.copy2(src_prop, self.product_out / part_upper / "build.prop")
                self.logger.info(f"Copied {part_lower} build.prop to {part_upper}")
            else:
                self.logger.warning(f"build.prop for {part_lower} not found")

    def _run_ota_tool(self):
        """Call ota_from_target_files to generate ZIP"""

        # Check if otatools is available
        ota_tool = self.ota_tools_dir / "bin" / "ota_from_target_files"
        if not ota_tool.exists():
            self.logger.error(f"ota_from_target_files not found at {ota_tool}")
            self.logger.error(
                "OTA tools (otatools) not found. Please install Android build tools."
            )
            self.logger.info("Falling back to super.img format instead of payload.bin")
            return

        self.logger.info("Running ota_from_target_files...")

        # Construct output filename
        now = datetime.now()

        # Format to specified string structure
        timestamp = now.strftime("%Y%m%d%H%M%S")
        output_zip = (
            self.out_dir / f"{self.ctx.baserom.vendor_device}-ota_full-{timestamp}.zip"
        )

        key_path = self.ota_tools_dir / "key" / "testkey"

        # Simple check if key exists
        if not (self.ota_tools_dir / "key" / "testkey.pk8").exists():
            self.logger.warning(
                f"Signature key not found at {key_path}.pk8! Please check your otatools/key folder."
            )

        custom_tmp_dir = self.out_dir / "tmp"

        if custom_tmp_dir.exists():
            shutil.rmtree(custom_tmp_dir)
        custom_tmp_dir.mkdir(parents=True, exist_ok=True)

        self.logger.info(f"Using custom TMPDIR: {custom_tmp_dir}")
        env = os.environ.copy()
        env["PATH"] = f"{self.ota_tools_dir}/bin:{env['PATH']}"
        env["PYTHONPATH"] = f"{self.ota_tools_dir}/releasetools"

        env["TMPDIR"] = str(custom_tmp_dir)

        # Set OUT environment variable for releasetools.py to locate firmware files
        env["OUT"] = str(self.product_out)
        self.logger.info(f"Setting OUT={self.product_out} for releasetools")

        cmd = [
            str(self.ota_tools_dir / "bin" / "ota_from_target_files"),
            "-v",
            "-k",
            str(key_path),
            str(self.product_out),
            str(output_zip),
        ]

        try:
            # Run with project root as cwd so relative paths work correctly
            project_root = Path(__file__).resolve().parent.parent.parent
            self.shell.run(cmd, env=env, cwd=project_root)
            self.logger.info(f"OTA Zip generated: {output_zip}")

            md5 = hashlib.md5(open(output_zip, "rb").read()).hexdigest()[:10]

            final_name = f"{self.ctx.baserom.vendor_device}-ota_full-{self.ctx.target_rom_version}-{self.ctx.security_patch}-{timestamp}-{md5}-{self.ctx.portrom.android_version}.zip"
            final_path = self.out_dir / final_name
            output_zip.rename(final_path)
            self.logger.info(f"Final OTA Package: {final_path}")

        except Exception as e:
            self.logger.error(f"OTA generation failed: {e}")

    def _get_super_size(self):
        """
        Get Super partition size
        Logic: Try matching both Device Code and Model
        """
        device_code = (
            self.ctx.baserom.vendor_device.upper()
            if self.ctx.baserom.vendor_device
            else ""
        )
        product_model = (
            self.ctx.baserom.product_model.upper()
            if self.ctx.baserom.product_model
            else ""
        )

        self.logger.info(
            f"Determining Super size for: Device={device_code}, Model={product_model}"
        )

        size_map = {
            # 15GB - OP8/8Pro and General Default
            15032385536: ["ONEPLUS8", "ONEPLUS8PRO", "INSTANTNOODLE", "INSTANTNOODLEP"],
            # 7.5GB - OP8T
            7516192768: ["ONEPLUS8T", "KEBAB"],
            # 9.9GB - OP9R
            9932111872: ["ONEPLUS9R"],
            # 11.19GB - Find X3 / OP9 / OP9P
            11190403072: ["OP4E5D", "ONEPLUS9", "ONEPLUS9PRO", "LEMONADE", "LEMONADEP"],
            # 11.18GB - Find X3 Pro
            11186208768: ["OP4E3F"],
            # 11.27GB - Realme GT Neo 3T etc
            11274289152: ["RE54E4L1", "RMX3371"],
            # 10.2GB - GT Neo 2
            10200547328: ["RE5473", "RE879AL1"],
            # 16.1GB - Ace 3V
            16106127360: ["OP5CFBL1"],
            # 14.5GB - Ace 5
            14574100480: ["OP5D2BL1"],
            # 14.95GB - OnePlus 13T
            14952693760: ["OP60F5L1", "PKX110"],
            # Xiaomi High-end
            9663676416: ["FUXI", "NUWA", "ISHTAR", "MARBLE", "SOCRATES", "BABYLON"],
        }

        # Try to match device_code first, then product_model
        for size, identifiers in size_map.items():
            if device_code in identifiers or product_model in identifiers:
                matched = device_code if device_code in identifiers else product_model
                self.logger.info(f"Matched {matched}, using size: {size}")
                return size

        # Default size
        default_size = 15032385536
        self.logger.info(f"No specific match found, using default size: {default_size}")

        return default_size

    def _setup_a_only_ota_structure(self, partition_list):
        """
        Setup A-only specific OTA structure:
        - OTA/bin/updater
        - MY_PRODUCT, MY_BIGBALL, MY_CARRIER, etc directories
        - RECOVERY/RAMDISK/etc/recovery.fstab
        - releasetools.py
        - firmware-update handling
        """
        self.logger.info("Setting up A-only OTA structure...")

        # 1. Create OTA/bin directory and copy updater
        ota_bin = self.product_out / "OTA" / "bin"
        ota_bin.mkdir(parents=True, exist_ok=True)

        updater_src = Path(
            f"devices/target/{self.ctx.baserom.vendor_device}/OTA/bin/updater"
        )
        updater_dst = ota_bin / "updater"

        if updater_src.exists():
            shutil.copy2(updater_src, updater_dst)
            self.logger.info(f"Copied custom updater from {updater_src}")
        else:
            default_updater = Path("devices/common/non-ab/OTA/updater")
            if default_updater.exists():
                shutil.copy2(default_updater, updater_dst)
                self.logger.info("Copied default A-only updater")
            else:
                self.logger.warning("Default A-only updater not found")

        # 2. Create ColorOS specific partition directories
        a_only_parts = [
            "MY_PRODUCT",
            "MY_BIGBALL",
            "MY_CARRIER",
            "MY_ENGINEERING",
            "MY_HEYTAP",
            "MY_MANIFEST",
            "MY_REGION",
            "MY_STOCK",
        ]
        for part in a_only_parts:
            (self.product_out / part).mkdir(exist_ok=True)

        # 3. Setup RECOVERY/RAMDISK/etc/recovery.fstab
        recovery_etc = self.product_out / "RECOVERY" / "RAMDISK" / "etc"
        recovery_etc.mkdir(parents=True, exist_ok=True)

        fstab_src = Path(
            f"devices/target/{self.ctx.baserom.vendor_device}/recovery.fstab"
        )
        fstab_dst = recovery_etc / "recovery.fstab"

        if fstab_src.exists():
            shutil.copy2(fstab_src, fstab_dst)
        else:
            default_fstab = Path("devices/common/recovery.fstab")
            if default_fstab.exists():
                shutil.copy2(default_fstab, fstab_dst)
                self.logger.info("Copied default recovery.fstab")

        # 4. Copy releasetools.py
        releasetools_src = Path(
            f"devices/target/{self.ctx.baserom.vendor_device}/releasetools.py"
        )
        releasetools_dst = self.meta_out / "releasetools.py"

        if releasetools_src.exists():
            shutil.copy2(releasetools_src, releasetools_dst)
        else:
            default_releasetools = Path("devices/common/releasetools.py")
            if default_releasetools.exists():
                shutil.copy2(default_releasetools, releasetools_dst)
                self.logger.info("Copied default releasetools.py")

        # 5. Handle firmware-update from baserom
        self._handle_a_only_firmware()

    def _handle_a_only_firmware(self):
        """
        Handle firmware-update directory for A-only devices.
        Port.sh logic:
        1. If build/baserom/images/firmware-update exists, copy it
        2. Otherwise, find .elf/.mdn/.bin files and move to firmware-update
        3. Copy boot.img, dtbo.img, vbmeta.img to appropriate locations
        4. Handle storage-fw and ffu_tool
        """
        self.logger.info("Handling A-only firmware files...")

        firmware_out = self.product_out / "firmware-update"
        firmware_out.mkdir(parents=True, exist_ok=True)

        baserom_work_dir = (
            self.ctx.baserom.work_dir if hasattr(self.ctx, "baserom") else None
        )
        repack_images_dir = self.ctx.work_dir / "repack_images"

        if baserom_work_dir and baserom_work_dir.exists():
            baserom_images = baserom_work_dir / "images"
            baserom_fw = baserom_images / "firmware-update"
            if baserom_fw.exists() and baserom_fw.is_dir():
                shutil.copytree(baserom_fw, firmware_out, dirs_exist_ok=True)
                self.logger.info(f"Copied firmware-update from {baserom_fw}")
            else:
                self.logger.info(
                    "No firmware-update directory found, searching for firmware files..."
                )

                for pattern in ["*.elf", "*.mdn", "*.bin"]:
                    for fw_file in baserom_work_dir.rglob(pattern):
                        if fw_file.is_file():
                            dest = firmware_out / fw_file.name
                            if not dest.exists():
                                shutil.copy2(fw_file, dest)
                                self.logger.info(f"Copied firmware: {fw_file.name}")

                # Copy other firmware-related .img files from baserom/images or repack_images_dir
                search_dirs = []
                if baserom_images.exists():
                    search_dirs.append(baserom_images)
                if repack_images_dir.exists():
                    search_dirs.append(repack_images_dir)

                for s_dir in search_dirs:
                    for img_file in s_dir.glob("*.img"):
                        part_name = img_file.stem
                        # Skip logical partitions, boot, and those already handled explicitly
                        if part_name not in ANDROID_LOGICAL_PARTITIONS and part_name not in ["boot", "dtbo", "vbmeta", "vbmeta_system"]:
                            dest = firmware_out / img_file.name
                            if not dest.exists():
                                shutil.copy2(img_file, dest)
                                self.logger.info(f"Copied firmware image: {img_file.name} from {s_dir.name}")

            for img_name in ["dtbo.img", "vbmeta.img", "vbmeta_system.img"]:
                src = baserom_images / img_name
                if src.exists():
                    shutil.copy2(src, firmware_out / img_name)
                    self.logger.info(
                        f"Copied {img_name} to firmware-update from baserom/images"
                    )
                elif repack_images_dir.exists():
                    src = repack_images_dir / img_name
                    if src.exists():
                        shutil.copy2(src, firmware_out / img_name)
                        self.logger.info(
                            f"Copied {img_name} to firmware-update from repack_images"
                        )

                target_boot_img = self.images_out / "boot.img"
                if target_boot_img.exists():
                    self.logger.info(
                        "boot.img already exists in IMAGES (possibly patched), skipping"
                    )
                else:
                    boot_img = baserom_images / "boot.img"
                    if not boot_img.exists() and repack_images_dir.exists():
                        boot_img = repack_images_dir / "boot.img"

                    if boot_img.exists():
                        self.images_out.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(boot_img, target_boot_img)
                        self.logger.info("Copied boot.img to IMAGES")

            storage_fw = baserom_work_dir / "images" / "storage-fw"
            if storage_fw.exists():
                storage_out = self.product_out / "storage-fw"
                storage_out.mkdir(parents=True, exist_ok=True)
                shutil.copytree(storage_fw, storage_out, dirs_exist_ok=True)

                ffu_tool = baserom_work_dir / "images" / "ffu_tool"
                if ffu_tool.exists():
                    shutil.copy2(ffu_tool, storage_out / "ffu_tool")
                    self.logger.info("Copied ffu_tool to storage-fw")
            else:
                ffu_tool = baserom_work_dir / "images" / "ffu_tool"
                if ffu_tool.exists():
                    storage_out = self.product_out / "storage-fw"
                    storage_out.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(ffu_tool, storage_out / "ffu_tool")
                    self.logger.info("Copied ffu_tool to storage-fw")
