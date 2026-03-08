"""
Property modification strategies - Function-based design with performance optimization.

High-level strategies grouped by function:
- string_replace: Global string replacements (device code, model, etc.)
- prop_set: Set property values directly
- prop_copy: Copy properties from baserom to target
- magic_model: Set AI magic model properties with template
- watermark: Add version watermark
- fingerprint: Regenerate build fingerprint
"""

import re
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.core.prop_utils import (
    PropCache,
    update_or_append_prop,
    read_prop_value,
    batch_update_props,
    read_prop_to_dict,
)

logger = logging.getLogger(__name__)


class PropStrategy(ABC):
    """Abstract base class for property modification strategies."""

    def __init__(self, config: Dict[str, Any], context: Any):
        self.config = config
        self.ctx = context
        self.name = config.get("name", self.__class__.__name__)
        self.priority = config.get("priority", 50)
        self.enabled = config.get("enabled", True)
        self._cache = None

    @abstractmethod
    def apply(self, target_dir: Path) -> bool:
        pass

    def check_condition(self) -> bool:
        """Check if strategy's condition is satisfied."""
        try:
            condition = self.config.get("condition")
            if not condition:
                return True

            # Simple condition evaluation
            for key, expected in condition.items():
                actual = self._get_context_value(key)
                if actual is None:
                    logger.debug(f"Condition check failed: {key} is None")
                    return False

                if key.endswith("_lt"):
                    if actual >= expected:
                        logger.debug(
                            f"Condition check failed: {key}={actual} >= {expected}"
                        )
                        return False
                elif key.endswith("_lte"):
                    if actual > expected:
                        logger.debug(
                            f"Condition check failed: {key}={actual} > {expected}"
                        )
                        return False
                elif key.endswith("_gt"):
                    if actual <= expected:
                        logger.debug(
                            f"Condition check failed: {key}={actual} <= {expected}"
                        )
                        return False
                elif key.endswith("_gte"):
                    if actual < expected:
                        logger.debug(
                            f"Condition check failed: {key}={actual} < {expected}"
                        )
                        return False
                elif key.endswith("_ne"):
                    if actual == expected:
                        logger.debug(
                            f"Condition check failed: {key}={actual} == {expected}"
                        )
                        return False
                elif actual != expected:
                    logger.debug(
                        f"Condition check failed: {key}={actual} != {expected}"
                    )
                    return False

            return True

        except Exception as e:
            logger.error(f"Failed to check condition for {self.name}: {e}")
            return False

    def _get_context_value(self, key: str) -> Any:
        """Get value from context using key mapping."""
        try:
            mappings = {
                "port_device_code": lambda: self.ctx.portrom.device_code,
                "port_product_model": lambda: self.ctx.portrom.product_model,
                "port_product_name": lambda: self.ctx.portrom.product_name,
                "port_product_device": lambda: self.ctx.portrom.product_device,
                "port_vendor_device": lambda: self.ctx.portrom.vendor_device,
                "port_vendor_model": lambda: self.ctx.portrom.vendor_model,
                "port_vendor_brand": lambda: self.ctx.portrom.vendor_brand,
                "port_android_version": lambda: int(
                    self.ctx.portrom.android_version or 0
                ),
                "port_is_coloros_global": lambda: self.ctx.portrom.is_coloros_global,
                "base_device_code": lambda: self.ctx.baserom.device_code,
                "base_product_model": lambda: self.ctx.baserom.product_model,
                "base_product_name": lambda: self.ctx.baserom.product_name,
                "base_product_device": lambda: self.ctx.baserom.product_device,
                "base_vendor_device": lambda: self.ctx.baserom.vendor_device,
                "base_vendor_model": lambda: self.ctx.baserom.vendor_model,
                "base_vendor_brand": lambda: self.ctx.baserom.vendor_brand,
                "base_market_name": lambda: self.ctx.baserom.market_name,
                "base_market_enname": lambda: self.ctx.baserom.market_enname,
                "base_lcd_density": lambda: self.ctx.baserom.lcd_density,
                "target_display_id": lambda: self.ctx.target_display_id,
            }

            if key in mappings:
                try:
                    return mappings[key]()
                except (AttributeError, TypeError, ValueError):
                    return None
            return getattr(self.ctx, key, None)
        except Exception as e:
            logger.error(f"Failed to get context value for {key}: {e}")
            return None


class StringReplaceStrategy(PropStrategy):
    """
    Global string replacement across all build.prop files.
    Replaces port values with base values (e.g., device code, model).
    """

    def apply(self, target_dir: Path) -> bool:
        mappings = self.config["config"].get("mappings", [])

        # Build replacement pairs
        replacements = []
        for mapping in mappings:
            old_val = self._get_context_value(mapping["from"])
            new_val = self._get_context_value(mapping["to"])
            if old_val and new_val and old_val != new_val:
                replacements.append((old_val, new_val))
                logger.debug(f"String replacement: '{old_val}' -> '{new_val}'")

        if not replacements:
            logger.debug("No string replacements needed")
            return True

        # Use cache for better performance
        if not self._cache:
            self._cache = PropCache(target_dir)

        prop_files = self._cache.get_all_prop_files(("system_dlkm", "odm_dlkm"))
        modified_count = 0

        for prop_file in prop_files:
            try:
                content = self._cache.read_prop_file(prop_file)
                original = content

                for old_val, new_val in replacements:
                    content = content.replace(old_val, new_val)

                if content != original:
                    prop_file.write_text(content, encoding="utf-8")
                    self._cache._prop_content_cache[prop_file] = content  # Update cache
                    modified_count += 1
                    logger.debug(f"Modified: {prop_file}")
            except Exception as e:
                logger.error(f"Failed to process {prop_file}: {e}")

        logger.info(
            f"StringReplace: Modified {modified_count} files with {len(replacements)} replacements"
        )
        return True


class PropSetStrategy(PropStrategy):
    """
    Set property values directly.
    Supports static values, context sources, templates, and partition-specific targets.
    """

    def apply(self, target_dir: Path) -> bool:
        properties = self.config["config"].get("properties", [])
        modified_count = 0

        # Group properties by target file for batch processing
        file_props = {}

        for prop in properties:
            key = prop["key"]

            # Get value: static value, from context, or from template
            value = self._resolve_value(prop)
            if value is None:
                logger.debug(f"Prop {key}: No value resolved, skipping")
                continue

            # Determine target partition
            target_partition = prop.get("target_partition")

            if target_partition:
                # Specific partition
                target_file = target_dir / target_partition / "build.prop"
            else:
                # Default: my_product/etc/bruce/build.prop
                target_file = target_dir / "my_product" / "etc" / "bruce" / "build.prop"
                if not target_file.exists() and not prop.get("create_if_missing", True):
                    logger.debug(
                        f"Prop {key}: Target file not found and create_if_missing=False"
                    )
                    continue

            # Ensure parent directory exists
            target_file.parent.mkdir(parents=True, exist_ok=True)

            # Group for batch processing
            if target_file not in file_props:
                file_props[target_file] = {}
            file_props[target_file][key] = value

        # Batch update files
        for target_file, props in file_props.items():
            try:
                if len(props) == 1:
                    # Single property - use simple update
                    for key, value in props.items():
                        if update_or_append_prop(target_file, key, value):
                            modified_count += 1
                            logger.info(f"PropSet: {key}={value}")
                else:
                    # Multiple properties - batch update
                    changed = batch_update_props(target_file, props)
                    modified_count += changed
                    logger.info(
                        f"PropSet: Batch updated {changed} properties in {target_file.name}"
                    )
            except Exception as e:
                logger.error(f"PropSet: Failed to update {target_file}: {e}")

        return True

    def _resolve_value(self, prop: Dict[str, Any]) -> Optional[str]:
        """Resolve property value from various sources."""
        try:
            # Direct static value
            if "value" in prop:
                return prop["value"]

            # Template with variable substitution
            if "template" in prop:
                template = prop["template"]
                # Collect template variables from context
                vars = {}
                if "{device_code}" in template:
                    vars["device_code"] = self.ctx.baserom.device_code or ""
                if "{product_model}" in template:
                    vars["product_model"] = self.ctx.baserom.product_model or ""
                if "{vendor_brand}" in template:
                    vars["vendor_brand"] = self.ctx.baserom.vendor_brand or ""
                return template.format(**vars)

            # Context source
            if "source" in prop:
                return self._get_context_value(prop["source"])

            return None
        except Exception as e:
            logger.error(f"Failed to resolve value for prop {prop.get('key')}: {e}")
            return None


class PropCopyStrategy(PropStrategy):
    """
    Copy properties from baserom to target partitions.
    Used for critical properties that must match baserom (e.g., first_api_level).
    """

    def __init__(self, config: Dict[str, Any], context: Any):
        super().__init__(config, context)
        self._baserom_props_cache: Dict[str, str] = {}

    def apply(self, target_dir: Path) -> bool:
        properties = self.config["config"].get("properties", [])

        # Pre-load all baserom properties into cache
        if not self._baserom_props_cache:
            self._baserom_props_cache = self._load_baserom_props()

        file_updates = {}

        for prop in properties:
            key = prop["key"]
            to_partition = prop.get("to_partition", "my_manifest")
            fallback_key = prop.get("fallback_key")
            comment = prop.get("comment")

            # Get value from cache
            value = self._baserom_props_cache.get(key)

            # If not found, try fallback key
            if not value and fallback_key:
                value = self._baserom_props_cache.get(fallback_key)
                if value:
                    logger.debug(
                        f"PropCopy: Using fallback {fallback_key}={value} for {key}"
                    )

            if not value:
                logger.warning(f"PropCopy: {key} not found in baserom props")
                continue

            # Write to target partition
            target_partition_dir = target_dir / to_partition
            target_file = self._find_build_prop(target_partition_dir)

            if not target_file.exists():
                # Create target file if needed
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_text("", encoding="utf-8")

            # Group updates by file for batch processing
            if target_file not in file_updates:
                file_updates[target_file] = {}
            file_updates[target_file][key] = value

            if comment:
                logger.info(f"PropCopy: {key}={value} ({comment})")
            else:
                logger.info(f"PropCopy: {key}={value}")

        # Batch update files
        for target_file, props in file_updates.items():
            try:
                changed = batch_update_props(target_file, props)
                logger.debug(
                    f"PropCopy: Updated {changed} properties in {target_file.name}"
                )
            except Exception as e:
                logger.error(f"PropCopy: Failed to update {target_file}: {e}")

        return True

    def _load_baserom_props(self) -> Dict[str, str]:
        """Load all baserom properties into memory cache."""
        props = {}

        # Try direct access from RomPackage.props first
        if hasattr(self.ctx.baserom, "props"):
            props.update(self.ctx.baserom.props)

        # Fallback: read from extracted files
        baserom_dir = self.ctx.baserom.extracted_dir
        if baserom_dir.exists():
            for prop_file in baserom_dir.rglob("build.prop"):
                try:
                    file_props = read_prop_to_dict(prop_file)
                    props.update(file_props)
                except Exception as e:
                    logger.debug(f"Failed to read {prop_file}: {e}")

        logger.debug(f"Loaded {len(props)} properties from baserom")
        return props

    def _find_build_prop(self, partition_dir: Path) -> Path:
        """Find build.prop in partition directory."""
        direct = partition_dir / "build.prop"
        if direct.exists():
            return direct
        nested = partition_dir / "etc" / "build.prop"
        return nested


class WatermarkStrategy(PropStrategy):
    """Add watermark to ROM version display."""

    def apply(self, target_dir: Path) -> bool:
        try:
            cfg = self.config["config"]
            target_key = cfg["target_key"]
            template = cfg["template"]
            author = cfg.get("author", "BT")
            skip_if_contains = cfg.get("skip_if_contains", "")

            my_product = target_dir / "my_product"
            if not my_product.exists():
                return True

            prop_main = my_product / "build.prop"
            prop_bruce = my_product / "etc" / "bruce" / "build.prop"
            target_file = prop_main if prop_main.exists() else prop_bruce

            if not target_file.exists():
                return True

            current_value = read_prop_value(target_file, target_key)
            if not current_value or skip_if_contains in current_value:
                return True

            new_value = template.format(value=current_value, author=author)
            if update_or_append_prop(target_file, target_key, new_value):
                logger.info(f"Watermark: Updated {target_key} = {new_value}")

            return True
        except Exception as e:
            logger.error(f"WatermarkStrategy failed: {e}")
            return False


class FingerprintStrategy(PropStrategy):
    """Regenerate build fingerprint and description."""

    def apply(self, target_dir: Path) -> bool:
        try:
            cfg = self.config["config"]
            priority_partitions = cfg.get(
                "priority_partitions",
                [
                    "my_manifest",
                    "my_product",
                    "odm",
                    "vendor",
                    "product",
                    "system_ext",
                    "system",
                ],
            )

            # Read components
            brand = self._get_prop(
                target_dir, priority_partitions, "ro.product.brand", "OnePlus"
            )
            name = self._get_prop(
                target_dir, priority_partitions, "ro.product.name", ""
            )
            device = self._get_prop(
                target_dir, priority_partitions, "ro.product.device", "oplus"
            )
            version = self._get_prop(
                target_dir, priority_partitions, "ro.build.version.release", ""
            )
            build_id = self._get_prop(
                target_dir, priority_partitions, "ro.build.id", ""
            )
            incremental = self._get_prop(
                target_dir, priority_partitions, "ro.build.version.incremental", ""
            )
            build_type = self._get_prop(
                target_dir, priority_partitions, "ro.build.type", "user"
            )
            tags = self._get_prop(
                target_dir, priority_partitions, "ro.build.tags", "release-keys"
            )

            fingerprint = f"{brand}/{name}/{device}:{version}/{build_id}/{incremental}:{build_type}/{tags}"
            description = (
                f"{name}-{build_type} {version} {build_id} {incremental} {tags}"
            )

            logger.info(f"Fingerprint: {fingerprint}")

            replacements = {
                "ro.build.fingerprint=": f"ro.build.fingerprint={fingerprint}",
                "ro.bootimage.build.fingerprint=": f"ro.bootimage.build.fingerprint={fingerprint}",
                "ro.system.build.fingerprint=": f"ro.system.build.fingerprint={fingerprint}",
                "ro.product.build.fingerprint=": f"ro.product.build.fingerprint={fingerprint}",
                "ro.system_ext.build.fingerprint=": f"ro.system_ext.build.fingerprint={fingerprint}",
                "ro.vendor.build.fingerprint=": f"ro.vendor.build.fingerprint={fingerprint}",
                "ro.odm.build.fingerprint=": f"ro.odm.build.fingerprint={fingerprint}",
                "ro.build.description=": f"ro.build.description={description}",
                "ro.system.build.description=": f"ro.system.build.description={description}",
            }

            # Use cache for better performance
            if not self._cache:
                self._cache = PropCache(target_dir)

            prop_files = self._cache.get_all_prop_files()
            modified_count = 0

            for prop_file in prop_files:
                try:
                    if self._apply_replacements(prop_file, replacements):
                        modified_count += 1
                except Exception as e:
                    logger.error(f"Failed to apply fingerprint to {prop_file}: {e}")

            logger.info(f"Fingerprint: Modified {modified_count} files")
            return True

        except Exception as e:
            logger.error(f"FingerprintStrategy failed: {e}")
            return False

    def _get_prop(
        self, target_dir: Path, partitions: List[str], key: str, default: str
    ) -> str:
        for part in partitions:
            part_dir = target_dir / part
            if not part_dir.exists():
                continue
            for prop_file in part_dir.rglob("build.prop"):
                try:
                    with open(prop_file, "r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            if line.strip().startswith(f"{key}="):
                                return line.split("=", 1)[1].strip()
                except:
                    pass
        return default

    def _apply_replacements(
        self, prop_file: Path, replacements: Dict[str, str]
    ) -> bool:
        try:
            with open(prop_file, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except:
            return False

        lines = content.splitlines()
        new_lines = []
        changed = False

        for line in lines:
            original = line
            stripped = line.strip()
            replaced = False

            for prefix, new_val in replacements.items():
                if stripped.startswith(prefix):
                    if original.strip() != new_val:
                        new_lines.append(new_val)
                        changed = True
                    else:
                        new_lines.append(original)
                    replaced = True
                    break

            if not replaced:
                new_lines.append(original)

        if changed:
            with open(prop_file, "w", encoding="utf-8") as f:
                f.write("\n".join(new_lines) + "\n")
            return True
        return False


# Strategy registry
STRATEGY_REGISTRY = {
    "string_replace": StringReplaceStrategy,
    "prop_set": PropSetStrategy,
    "prop_copy": PropCopyStrategy,
    "watermark": WatermarkStrategy,
    "fingerprint": FingerprintStrategy,
}


def create_strategy(config: Dict[str, Any], context: Any) -> Optional[PropStrategy]:
    """Factory function to create strategy instances from config."""
    name = config.get("name")
    if not name:
        return None

    strategy_class = STRATEGY_REGISTRY.get(name)
    if not strategy_class:
        logger.warning(f"Unknown strategy: {name}")
        return None

    return strategy_class(config, context)
