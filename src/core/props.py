"""
Property modification module - Configuration-driven build.prop modifications.
Uses strategy pattern for extensible and testable prop modifications.
"""

import yaml
import shutil
import logging
from pathlib import Path
from typing import Dict, List, Any, Optional

from src.core.context import Context
from src.core.prop_strategies import create_strategy, PropStrategy

logger = logging.getLogger(__name__)


class PropertyModifier:
    """
    Configuration-driven property modifier.
    
    Loads modification rules from YAML config and applies them using
    pluggable strategies.
    """
    
    DEFAULT_CONFIG_PATH = Path("devices/common/props.yml")
    
    def __init__(self, context: Context, config_path: Optional[Path] = None):
        self.ctx = context
        self.target_dir = self.ctx.target_dir
        self.config_path = config_path or self.DEFAULT_CONFIG_PATH
        self._strategies: List[PropStrategy] = []
        self._config: Dict[str, Any] = {}
    
    def run(self):
        """
        Execute property modifications based on configuration.
        
        Workflow:
        1. Fetch ROM info
        2. Reconstruct my_product props
        3. Load and sort strategies by priority
        4. Apply each strategy
        5. Regenerate fingerprint
        """
        logger.info("Starting Property Modification...")
        
        # 1. Fetch ROM Info
        self.ctx.fetch_rom_info()
        
        # 2. Reconstruct my_product props (Base-led strategy)
        self._reconstruct_my_product_props()
        
        # 3. Load configuration and strategies
        self._load_config()
        self._build_strategies()
        
        # 4. Apply strategies in priority order
        self._apply_strategies()
        
        logger.info("Property modifications complete.")
    
    def _load_config(self) -> bool:
        """Load configuration from YAML file."""
        if not self.config_path.exists():
            # Try .json if .yml not found (legacy support during migration)
            legacy_path = self.config_path.with_suffix(".json")
            if legacy_path.exists():
                self.config_path = legacy_path
            else:
                logger.warning(f"Config file not found: {self.config_path}")
                return False
        
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                if self.config_path.suffix == ".json":
                    import json
                    self._config = json.load(f)
                else:
                    self._config = yaml.safe_load(f)
            logger.debug(f"Loaded config from {self.config_path}")
            return True
        except yaml.YAMLError as e:
            logger.error(f"Invalid YAML in config: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            return False
    
    def _build_strategies(self):
        """Build and sort strategy instances from config."""
        # Support both "strategies" (v1) and "rules" (v2) naming
        strategy_configs = self._config.get("rules", self._config.get("strategies", []))
        
        for config in strategy_configs:
            if not config.get("enabled", True):
                continue
            
            strategy = create_strategy(config, self.ctx)
            if strategy:
                self._strategies.append(strategy)
        
        # Sort by priority (lower = higher priority)
        self._strategies.sort(key=lambda s: s.priority)
        
        logger.debug(f"Loaded {len(self._strategies)} strategies")
    
    def _apply_strategies(self):
        """Apply all enabled strategies in priority order."""
        for strategy in self._strategies:
            try:
                # Check condition
                if not strategy.check_condition():
                    logger.debug(f"Strategy '{strategy.name}' skipped (condition not met)")
                    continue
                
                logger.debug(f"Applying strategy: {strategy.name}")
                success = strategy.apply(self.target_dir)
                
                if not success:
                    logger.warning(f"Strategy '{strategy.name}' failed")
                    
            except Exception as e:
                logger.error(f"Strategy '{strategy.name}' raised exception: {e}")
    
    def _reconstruct_my_product_props(self):
        """
        Reconstructs my_product/build.prop by using baserom as base
        and moving portrom-specific props to etc/bruce/build.prop.
        """
        target_my_product = self.target_dir / "my_product"
        if not target_my_product.exists():
            return
        
        logger.info("Reconstructing my_product properties...")
        
        # Load my_product config
        my_product_config = self._config.get("my_product", {})
        force_keys = my_product_config.get("force_keys", [
            "ro.build.version.oplusrom",
            "ro.build.version.oplusrom.display",
            "ro.build.version.oplusrom.confidential",
            "ro.build.version.realmeui",
        ])
        import_line = my_product_config.get("import_line", 
            "import /mnt/vendor/my_product/etc/bruce/build.prop")
        
        # 1. Paths
        base_prop_file = self._find_build_prop(self.ctx.baserom.extracted_dir / "my_product")
        port_prop_file = self._find_build_prop(self.ctx.portrom.extracted_dir / "my_product")
        
        target_prop_main = target_my_product / "build.prop"
        target_prop_bruce = target_my_product / "etc" / "bruce" / "build.prop"
        
        # 2. Parse Props
        base_props = self._read_prop_to_dict(base_prop_file)
        port_props = self._read_prop_to_dict(port_prop_file)
        
        # 3. Calculate Bruce Props (Port-only props + Force keys)
        bruce_props = {}
        for key, value in port_props.items():
            if key in force_keys or key not in base_props:
                bruce_props[key] = value
                logger.debug(f"Adding to bruce.prop: {key}={value}")
        
        # 4. Overwrite target main prop with Base content
        if base_prop_file.exists():
            shutil.copy2(base_prop_file, target_prop_main)
        
        # 5. Ensure Import statement in main prop
        content = target_prop_main.read_text(encoding="utf-8", errors="ignore")
        if import_line not in content:
            with open(target_prop_main, "a", encoding="utf-8") as f:
                f.write(f"\n\n# Bruce Property Patch\n{import_line}\n")
        
        # 6. Write Bruce Props
        target_prop_bruce.parent.mkdir(parents=True, exist_ok=True)
        with open(target_prop_bruce, "w", encoding="utf-8") as f:
            f.write("# Properties added from Port ROM\n")
            for key in sorted(bruce_props.keys()):
                f.write(f"{key}={bruce_props[key]}\n")
        
        logger.info(f"Reconstruction complete. {len(bruce_props)} props moved to bruce/build.prop")
    
    def _find_build_prop(self, partition_dir: Path) -> Path:
        """Find build.prop in partition directory (handling etc/ subdirectory)."""
        direct = partition_dir / "build.prop"
        if direct.exists():
            return direct
        nested = partition_dir / "etc" / "build.prop"
        return nested
    
    def _read_prop_to_dict(self, file_path: Path) -> Dict[str, str]:
        """Read properties file into dictionary."""
        props = {}
        if not file_path.exists():
            return props
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, val = line.split("=", 1)
                    props[key.strip()] = val.strip()
        except Exception as e:
            logger.error(f"Failed to read {file_path}: {e}")
        return props
    
    # === Legacy methods for backward compatibility ===
    
    def _modify_build_props(self):
        """Legacy method - now handled by strategies."""
        logger.debug("_modify_build_props is deprecated, use strategies instead")
    
    def _modify_all_build_props(self):
        """Legacy method - now handled by strategies."""
        pass
    
    def _modify_my_product_props(self):
        """Legacy method - now handled by strategies."""
        pass
    
    def _modify_system_ext_props(self):
        """Legacy method - now handled by strategies."""
        pass
    
    def _regenerate_fingerprint(self):
        """Legacy method - now handled by FingerprintStrategy."""
        pass
