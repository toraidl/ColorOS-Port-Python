import logging
import os
import re
import shutil
import zipfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

from src.utils.shell import ShellRunner


class BaseExtractor(ABC):
    def __init__(self, rom_path: Path, images_dir: Path, label: str = "Extractor"):
        self.rom_path = rom_path
        self.images_dir = images_dir
        self.label = label
        self.logger = logging.getLogger(label)
        self.shell = ShellRunner()

    @abstractmethod
    def extract(self, partitions: Optional[List[str]] = None) -> None:
        """Extract images from the ROM package."""
        pass


class PayloadExtractor(BaseExtractor):
    def extract(self, partitions: Optional[List[str]] = None) -> None:
        self.logger.info(f"[{self.label}] Starting Payload extraction...")
        cmd = ["payload-dumper", "--out", str(self.images_dir)]

        if partitions:
            self.logger.info(
                f"[{self.label}] Extracting specific images: {partitions} ..."
            )
            cmd.extend(["--partitions", ",".join(partitions)])
        else:
            self.logger.info(
                f"[{self.label}] Extracting ALL images (Firmware + Logical) ..."
            )

        cmd.append(str(self.rom_path))

        if not any(self.images_dir.iterdir()):
            self.shell.run(cmd)
        else:
            self.logger.info(
                f"[{self.label}] Images directory not empty, assuming extracted."
            )


class BrotliExtractor(BaseExtractor):
    def extract(self, partitions: Optional[List[str]] = None) -> None:
        self.logger.info(f"[{self.label}] Starting Brotli extraction...")
        # 1. Extract zip content
        with zipfile.ZipFile(self.rom_path, "r") as z:
            for f in z.namelist():
                should_extract = False
                if f.endswith(".img"):
                    part_name = Path(f).stem
                    if not partitions or part_name in partitions:
                        should_extract = True
                elif f.endswith(".new.dat.br") or f.endswith(".transfer.list"):
                    part_name = Path(f).name.split(".")[0]
                    if not partitions or part_name in partitions:
                        should_extract = True
                elif (
                    f.startswith("storage-fw/")
                    or f == "ffu_tool"
                    or f.startswith("ffu_tool")
                ):
                    should_extract = True

                if should_extract:
                    self.logger.info(f"[{self.label}] Extracting {f}...")
                    z.extract(f, self.images_dir)

        self._fix_br_filenames()

        # 2. Process .br files
        for br_file in self.images_dir.glob("*.new.dat.br"):
            prefix = br_file.name.replace(".new.dat.br", "")
            new_dat = self.images_dir / f"{prefix}.new.dat"
            transfer_list = self.images_dir / f"{prefix}.transfer.list"
            output_img = self.images_dir / f"{prefix}.img"

            if output_img.exists():
                continue

            if not transfer_list.exists():
                self.logger.warning(f"Transfer list for {prefix} not found, skipping.")
                continue

            # 3. Decompress
            self.logger.info(f"[{self.label}] Decompressing {br_file.name}...")
            try:
                self.shell.run(["brotli", "-d", "-f", str(br_file), "-o", str(new_dat)])
            except Exception as e:
                self.logger.error(f"Brotli failed for {prefix}: {e}")
                continue

            # 4. sdat2img
            self.logger.info(f"[{self.label}] Converting {prefix} to raw image...")
            from src.utils.sdat2img import run_sdat2img

            if run_sdat2img(str(transfer_list), str(new_dat), str(output_img)):
                if new_dat.exists():
                    os.remove(new_dat)
                if br_file.exists():
                    os.remove(br_file)
                if transfer_list.exists():
                    os.remove(transfer_list)

    def _fix_br_filenames(self) -> None:
        for file_path in self.images_dir.iterdir():
            if not file_path.is_file():
                continue
            name, ext = file_path.stem, file_path.suffix
            if re.search(r"\d", name):
                new_name = re.sub(r"\d+(\.[^\d.]+)", r"\1", name).replace("..", ".")
                if new_name != name:
                    shutil.move(
                        str(file_path), str(self.images_dir / f"{new_name}{ext}")
                    )


class FastbootExtractor(BaseExtractor):
    def extract(self, partitions: Optional[List[str]] = None) -> None:
        self.logger.info(f"[{self.label}] Starting Fastboot extraction...")
        with zipfile.ZipFile(self.rom_path, "r") as z:
            for f in z.namelist():
                if f.endswith("super.img") or f.endswith("images/super.img"):
                    z.extract(f, self.images_dir)
                    continue
                if not f.endswith(".img"):
                    continue
                part_name = Path(f).stem
                if partitions and part_name not in partitions:
                    continue

                self.logger.info(f"[{self.label}] Extracting {f}...")
                with (
                    z.open(f) as source,
                    open(self.images_dir / Path(f).name, "wb") as target,
                ):
                    shutil.copyfileobj(source, target)

        self._process_sparse_images()
        self._unpack_super(partitions)

    def _process_sparse_images(self) -> None:
        simg2img_bin = Path("bin/linux/x86_64/simg2img").resolve()
        if not simg2img_bin.exists():
            simg2img_bin = "simg2img"

        for target in ["super.img", "cust.img"]:
            chunks = sorted(list(self.images_dir.glob(f"{target}.*")))
            target_path = self.images_dir / target
            if chunks:
                self.shell.run(
                    [str(simg2img_bin)] + [str(c) for c in chunks] + [str(target_path)]
                )
                for c in chunks:
                    os.unlink(c)
            elif target_path.exists():
                temp_raw = self.images_dir / f"{target}.raw.img"
                try:
                    self.shell.run([str(simg2img_bin), str(target_path), str(temp_raw)])
                    shutil.move(temp_raw, target_path)
                except:
                    if temp_raw.exists():
                        os.unlink(temp_raw)

    def _unpack_super(self, partitions: Optional[List[str]]) -> None:
        super_img = self.images_dir / "super.img"
        if not super_img.exists():
            return

        self.logger.info(f"[{self.label}] Unpacking super.img...")
        try:
            if partitions:
                for part in partitions:
                    for p in [part, f"{part}_a"]:
                        self.shell.run(
                            ["lpunpack", "-p", p, str(super_img), str(self.images_dir)],
                            check=False,
                            silent=True,
                        )
            else:
                self.shell.run(["lpunpack", str(super_img), str(self.images_dir)])
        finally:
            if super_img.exists():
                os.remove(super_img)


class LocalDirExtractor(BaseExtractor):
    def extract(self, partitions: Optional[List[str]] = None) -> None:
        self.logger.info(
            f"[{self.label}] Local directory mode, skipping image extraction."
        )

class RomExtractorFactory:
    @staticmethod
    def get_extractor(
        rom_type, rom_path: Path, images_dir: Path, label: str
    ) -> BaseExtractor:
        from src.core.rom import RomType

        if rom_type == RomType.PAYLOAD:
            return PayloadExtractor(rom_path, images_dir, label)
        elif rom_type == RomType.BROTLI:
            return BrotliExtractor(rom_path, images_dir, label)
        elif rom_type == RomType.FASTBOOT:
            return FastbootExtractor(rom_path, images_dir, label)
        elif rom_type == RomType.LOCAL_DIR:
            return LocalDirExtractor(rom_path, images_dir, label)
        raise ValueError(f"Unsupported ROM type: {rom_type}")
