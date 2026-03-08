"""Firmware Modifier - Firmware-level modifications (vbmeta, kernel, KSU)."""

import json
import shutil
import tempfile
import zipfile
import urllib.request
import logging
from pathlib import Path

from src.core.modifiers.base_modifier import BaseModifier
from src.utils.shell import ShellRunner


class FirmwareModifier(BaseModifier):
    """Firmware-level modifications including vbmeta and kernel patching."""

    def __init__(self, context):
        super().__init__(context, "FirmwareModifier")
        self.shell = ShellRunner()
        self.bin_dir = Path("bin").resolve()

        if not self.ctx.tools.magiskboot.exists():
            self.logger.error(
                f"magiskboot binary not found at {self.ctx.tools.magiskboot}"
            )
            return

        self.assets_dir = self.bin_dir.parent / "assets"
        self.ksu_version_file = self.assets_dir / "ksu_version.txt"
        self.repo_owner = "tiann"
        self.repo_name = "KernelSU"

    def run(self):
        self.logger.info("Starting Firmware Modification...")

        if getattr(self.ctx, "disable_vbmeta", False):
            self._patch_vbmeta()

        if getattr(self.ctx, "enable_ksu", False):
            if getattr(self.ctx, "ksu_type", "gki") == "gki":
                self._patch_ksu()
            else:
                self._patch_non_gki_kernel()

        self.logger.info("Firmware Modification Completed.")

    def _patch_vbmeta(self):
        self.logger.info("Patching vbmeta images (Disabling AVB)...")

        repack_images_dir = self.ctx.work_dir / "repack_images"
        vbmeta_images = list(repack_images_dir.glob("vbmeta*.img"))
        vbmeta_images.extend(list(self.ctx.target_dir.rglob("vbmeta*.img")))

        if not vbmeta_images:
            self.logger.warning("No vbmeta images found in target directory.")
            return

        AVB_MAGIC = b"AVB0"
        FLAGS_OFFSET = 123
        FLAGS_TO_SET = b"\x03"

        for img_path in vbmeta_images:
            try:
                with open(img_path, "r+b") as f:
                    magic = f.read(4)
                    if magic != AVB_MAGIC:
                        self.logger.warning(
                            f"Skipping {img_path.name}: Invalid AVB Magic"
                        )
                        continue

                    f.seek(FLAGS_OFFSET)
                    f.write(FLAGS_TO_SET)
                    self.logger.info(f"Successfully patched: {img_path.name}")

            except Exception as e:
                self.logger.error(f"Failed to patch {img_path.name}: {e}")

    def _patch_ksu(self):
        self.logger.info("Attempting to patch KernelSU...")

        target_init_boot = self.ctx.repack_images_dir / "init_boot.img"
        target_boot = self.ctx.repack_images_dir / "boot.img"

        if not target_init_boot.exists():
            self.logger.warning("init_boot.img not found, skipping KSU patch.")
            return
        if not target_boot.exists():
            self.logger.warning(
                "boot.img not found (needed for KMI check), skipping KSU patch."
            )
            return

        if not self.ctx.tools.magiskboot or not self.ctx.tools.magiskboot.exists():
            self.logger.error("magiskboot binary not found!")
            return

        kmi_version = self._analyze_kmi(target_boot)
        if not kmi_version:
            self.logger.error("Failed to determine KMI version.")
            return

        self.logger.info(f"Detected KMI Version: {kmi_version}")

        if not self._prepare_ksu_assets(kmi_version):
            self.logger.error("Failed to prepare KSU assets.")
            return

        self._apply_ksu_patch(target_init_boot, kmi_version)

    def _patch_non_gki_kernel(self):
        self.logger.info("Integrating non-GKI custom kernel (AnyKernel)...")
        device_code = getattr(self.ctx, "device_code", "unknown")
        device_dir = Path(f"devices/{device_code}")
        if not device_dir.exists():
            device_dir = Path(f"devices/target/{device_code}")

        if not device_dir.exists():
            self.logger.warning(
                f"Device directory not found for {device_code}, skipping kernel patch."
            )
            return

        # Find kernel zip (KSU or NoKSU) - look in device_dir and its assets/ subdirectory
        ksu_zips = list(device_dir.glob("*-KSU*.zip"))
        noksu_zips = list(device_dir.glob("*-NoKSU*.zip"))

        if not ksu_zips and not noksu_zips:
            ksu_zips = list((device_dir / "assets").glob("*-KSU*.zip"))
            noksu_zips = list((device_dir / "assets").glob("*-NoKSU*.zip"))

        target_zip = None
        if ksu_zips:
            target_zip = ksu_zips[0]
            self.logger.info(f"Found KSU kernel zip: {target_zip.name}")
        elif noksu_zips:
            target_zip = noksu_zips[0]
            self.logger.info(f"Found NoKSU kernel zip: {target_zip.name}")

        if not target_zip:
            self.logger.warning("No custom kernel zip found in device directory.")
            return

        # Find boot.img in repack_images_dir (it should have been copied there by workflow.py)
        target_boot = self.ctx.repack_images_dir / "boot.img"
        if not target_boot.exists():
            self.logger.error(f"boot.img not found in {self.ctx.repack_images_dir}")
            return

        with tempfile.TemporaryDirectory(prefix="anykernel_") as tmp:
            tmp_path = Path(tmp)
            ak_path = tmp_path / "anykernel"
            ak_path.mkdir()

            with zipfile.ZipFile(target_zip, "r") as zip_ref:
                zip_ref.extractall(ak_path)

            # Find kernel, dtb, dtbo
            kernel_file = next(
                (
                    f
                    for f in ak_path.glob("*")
                    if f.name.lower()
                    in [
                        "image",
                        "zimage",
                        "kernel",
                        "image.gz",
                        "image.lz4",
                        "boot.img",
                    ]
                ),
                None,
            )
            dtb_file = next(
                (
                    f
                    for f in ak_path.glob("*")
                    if f.name.lower() in ["dtb", "dtb.img"]
                    or f.suffix.lower() == ".dtb"
                ),
                None,
            )
            dtbo_file = next((f for f in ak_path.glob("dtbo.img")), None)

            if not kernel_file:
                self.logger.error("No kernel image found in zip.")
                return

            boot_tmp = tmp_path / "boot"
            boot_tmp.mkdir()
            shutil.copy(target_boot, boot_tmp / "boot.img")

            self.shell.run(
                [str(self.ctx.tools.magiskboot), "unpack", "-h", "boot.img"],
                cwd=boot_tmp,
            )

            # Replace kernel
            if kernel_file.name == "boot.img":
                inner_tmp = tmp_path / "inner"
                inner_tmp.mkdir()
                shutil.copy(kernel_file, inner_tmp / "boot.img")
                self.shell.run(
                    [str(self.ctx.tools.magiskboot), "unpack", "-h", "boot.img"],
                    cwd=inner_tmp,
                )
                if (inner_tmp / "kernel").exists():
                    shutil.copy(inner_tmp / "kernel", boot_tmp / "kernel")
                if (inner_tmp / "dtb").exists():
                    shutil.copy(inner_tmp / "dtb", boot_tmp / "dtb")
            else:
                if kernel_file.suffix.lower() == ".gz":
                    import gzip

                    with (
                        gzip.open(kernel_file, "rb") as f_in,
                        open(boot_tmp / "kernel", "wb") as f_out,
                    ):
                        shutil.copyfileobj(f_in, f_out)
                elif kernel_file.suffix.lower() == ".lz4":
                    self.shell.run(
                        ["lz4", "-d", str(kernel_file), str(boot_tmp / "kernel")]
                    )
                else:
                    shutil.copy(kernel_file, boot_tmp / "kernel")

            if dtb_file:
                shutil.copy(dtb_file, boot_tmp / "dtb")

            self.shell.run(
                [str(self.ctx.tools.magiskboot), "repack", "boot.img", "boot_new.img"],
                cwd=boot_tmp,
            )

            if (boot_tmp / "boot_new.img").exists():
                shutil.move(boot_tmp / "boot_new.img", target_boot)
                self.logger.info("Custom kernel integrated successfully into boot.img")
            else:
                self.logger.error("Failed to repack boot.img with custom kernel.")

            if dtbo_file:
                target_dtbo = self.ctx.work_dir / "repack_images" / "dtbo.img"
                shutil.copy(dtbo_file, target_dtbo)
                self.logger.info("Custom dtbo.img integrated.")

    def _analyze_kmi(self, boot_img):
        import re

        with tempfile.TemporaryDirectory(prefix="ksu_kmi_") as tmp:
            tmp_path = Path(tmp)
            shutil.copy(boot_img, tmp_path / "boot.img")

            try:
                self.shell.run(
                    [str(self.ctx.tools.magiskboot), "unpack", "boot.img"], cwd=tmp_path
                )
            except Exception:
                return None

            kernel_file = tmp_path / "kernel"
            if not kernel_file.exists():
                return None

            try:
                with open(kernel_file, "rb") as f:
                    content = f.read()

                strings = []
                current = []
                for b in content:
                    if 32 <= b <= 126:
                        current.append(chr(b))
                    else:
                        if len(current) >= 4:
                            strings.append("".join(current))
                        current = []

                pattern = re.compile(r"(?:^|\s)(\d+\.\d+)\S*(android\d+)")
                for s in strings:
                    if "Linux version" in s or "android" in s:
                        match = pattern.search(s)
                        if match:
                            return f"{match.group(2)}-{match.group(1)}"
            except Exception:
                pass
        return None

    def _prepare_ksu_assets(self, kmi_version):
        self.assets_dir.mkdir(parents=True, exist_ok=True)

        target_ko = self.assets_dir / f"{kmi_version}_kernelsu.ko"
        target_init = self.assets_dir / "ksuinit"

        if target_ko.exists() and target_init.exists():
            return True

        self.logger.info("Downloading KernelSU assets...")
        try:
            api_url = f"https://api.github.com/repos/{self.repo_owner}/{self.repo_name}/releases/latest"
            with urllib.request.urlopen(api_url, timeout=10) as resp:
                data = json.loads(resp.read().decode())

            assets = data.get("assets", [])

            for asset in assets:
                name = asset["name"]
                url = asset["browser_download_url"]

                if name == "ksuinit" and not target_init.exists():
                    self._download_file(url, target_init)
                elif name == f"{kmi_version}_kernelsu.ko" and not target_ko.exists():
                    self._download_file(url, target_ko)

            return target_ko.exists() and target_init.exists()

        except Exception as e:
            self.logger.error(f"Download failed: {e}")
            return False

    def _download_file(self, url, dest):
        self.logger.info(f"Downloading {dest.name}...")
        with urllib.request.urlopen(url) as remote, open(dest, "wb") as local:
            shutil.copyfileobj(remote, local)

    def _apply_ksu_patch(self, init_boot_img, kmi_version):
        self.logger.info(f"Patching {init_boot_img.name} with KernelSU...")

        ko_file = self.assets_dir / f"{kmi_version}_kernelsu.ko"
        init_file = self.assets_dir / "ksuinit"

        with tempfile.TemporaryDirectory(prefix="ksu_patch_") as tmp:
            tmp_path = Path(tmp)
            shutil.copy(init_boot_img, tmp_path / "init_boot.img")

            self.shell.run(
                [str(self.ctx.tools.magiskboot), "unpack", "init_boot.img"],
                cwd=tmp_path,
            )

            ramdisk = tmp_path / "ramdisk.cpio"
            if not ramdisk.exists():
                self.logger.error("ramdisk.cpio not found")
                return

            self.shell.run(
                [
                    str(self.ctx.tools.magiskboot),
                    "cpio",
                    "ramdisk.cpio",
                    "mv init init.real",
                ],
                cwd=tmp_path,
            )

            shutil.copy(init_file, tmp_path / "init")
            self.shell.run(
                [
                    str(self.ctx.tools.magiskboot),
                    "cpio",
                    "ramdisk.cpio",
                    "add 0755 init init",
                ],
                cwd=tmp_path,
            )

            shutil.copy(ko_file, tmp_path / "kernelsu.ko")
            self.shell.run(
                [
                    str(self.ctx.tools.magiskboot),
                    "cpio",
                    "ramdisk.cpio",
                    "add 0755 kernelsu.ko kernelsu.ko",
                ],
                cwd=tmp_path,
            )

            self.shell.run(
                [str(self.ctx.tools.magiskboot), "repack", "init_boot.img"],
                cwd=tmp_path,
            )

            new_img = tmp_path / "new-boot.img"
            if new_img.exists():
                shutil.move(new_img, init_boot_img)
                self.logger.info("KernelSU injected successfully.")
            else:
                self.logger.error("Failed to repack init_boot.img")
