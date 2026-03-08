import yaml
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


class Config:
    def __init__(self, config_data):
        self.partition_to_port = config_data.get("partition_to_port", [])
        self.baserom_partitions = config_data.get(
            "baserom_partitions",
            ["system", "product", "system_ext", "my_product", "my_manifest"],
        )
        self.possible_super_list = config_data.get("possible_super_list", [])
        self.repack_with_ext4 = config_data.get("repack_with_ext4", True)
        self.super_extended = config_data.get("super_extended", False)
        self.pack_with_dsu = config_data.get("pack_with_dsu", False)
        self.pack_method = config_data.get("pack_method", "erofs")
        self.ddr_type = config_data.get("ddr_type", "ddr4")
        self.reusabe_partition_list = config_data.get("reusabe_partition_list", [])
        self.system_dlkm_enabled = config_data.get("system_dlkm_enabled", False)
        self.vendor_dlkm_enabled = config_data.get("vendor_dlkm_enabled", False)
        self.enable_ksu = config_data.get("enable_ksu", False)
        self.ksu_type = config_data.get("ksu_type", "gki")
        self.disable_vbmeta = config_data.get("disable_vbmeta", False)
        self.assets_base_url = config_data.get(
            "assets_base_url",
            "https://github.com/toraidl/ColorOS-Port-Python/releases/download/assets",
        )

    @classmethod
    def load(cls, device_code=None):
        base_config_path = Path("devices/common/port_config.yml")
        if not base_config_path.exists():
            raise FileNotFoundError(
                "Base configuration not found in devices/common/port_config.yml"
            )

        with open(base_config_path, "r", encoding="utf-8") as f:
            config_data = yaml.safe_load(f)

        if device_code:
            device_config_path = Path(f"devices/target/{device_code}/port_config.yml")
            if not device_config_path.exists():
                device_config_path = Path(f"devices/{device_code}/port_config.yml")

            if device_config_path.exists():
                with open(device_config_path, "r", encoding="utf-8") as f:
                    device_data = yaml.safe_load(f)
                    config_data.update(device_data)

        return cls(config_data)

    @classmethod
    def load_safe(cls, device_code: str | None = None, is_required: bool = True):
        try:
            config = cls.load(device_code)
            label = device_code if device_code else "common (initial)"
            logger.info(f"Loaded configuration for device: {label}")
            return config
        except Exception as e:
            if is_required:
                logger.error(f"Failed to load configuration: {e}")
                sys.exit(1)
            else:
                logger.warning(
                    f"No specific config for {device_code}, continuing with current config."
                )
                return None
