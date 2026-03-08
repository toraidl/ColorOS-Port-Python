"""Built-in modifier plugins for ColorOS porting.

This module contains plugins for common modification tasks extracted
from the original SystemModifier class.
"""

import json
import re
import shutil
import tempfile
import zipfile
import concurrent.futures
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.modifiers.plugin_system import ModifierPlugin
from src.core.conditions import ConditionEvaluator, BuildContext
from src.core.config_merger import ConfigMerger


class FileReplacementPlugin(ModifierPlugin):
    """Plugin to handle file/directory replacements from replacements.yml."""

    name = "file_replacement"
    description = "Execute file/directory replacements from replacements.yml"
    priority = 10

    def __init__(self, context, **kwargs):
        super().__init__(context, **kwargs)
        self.merger = ConfigMerger(self.logger)
        self.evaluator = ConditionEvaluator()

    def modify(self) -> bool:
        """Execute file replacements."""
        config = self._load_merged_config("replacements.yml")
        replacements = config.get("replacements", [])

        if not replacements:
            return True

        self.logger.info(f"Processing {len(replacements)} file replacements...")

        # Build context for condition evaluation
        cond_ctx = self._build_condition_context()

        stock_root = self.ctx.stock.extracted_dir
        target_root = self.ctx.target_dir
        target_index = self._build_target_index(target_root)

        copy_tasks = []

        for rule in replacements:
            # Evaluate conditions
            if not self.evaluator.evaluate(rule, cond_ctx):
                self.logger.debug(
                    f"Rule '{rule.get('description', 'unnamed')}' skipped: conditions not met"
                )
                continue

            desc = rule.get("description", "Unknown Rule")
            rtype = rule.get("type", "file")
            self.logger.info(f"Applying replacement rule: {desc}")

            try:
                tasks = self._handle_rule(
                    rule, rtype, stock_root, target_root, target_index
                )
                copy_tasks.extend(tasks)
            except Exception as e:
                self.logger.error(f"Failed to apply rule '{desc}': {e}")

        # Execute copy tasks in parallel
        self._execute_copy_tasks(copy_tasks)

        return True

    def _build_condition_context(self) -> BuildContext:
        """Build a condition context from the current build context."""
        ctx = BuildContext()

        # Copy ROM type flags from portrom
        ctx.port_is_coloros = self.ctx.portrom.is_coloros
        ctx.port_is_coloros_global = self.ctx.portrom.is_coloros_global
        ctx.port_is_oos = self.ctx.portrom.is_oos

        # Copy base ROM type flags from baserom
        ctx.base_is_coloros = self.ctx.baserom.is_coloros
        ctx.base_is_coloros_cn = self.ctx.baserom.is_coloros
        ctx.base_is_coloros_global = self.ctx.baserom.is_coloros_global
        ctx.base_is_oos = self.ctx.baserom.is_oos

        # Copy version info
        port_ver = self.ctx.portrom.android_version
        ctx.port_android_version = int(port_ver) if port_ver else 14
        base_ver = self.ctx.baserom.android_version
        ctx.base_android_version = int(base_ver) if base_ver else 14
        ctx.port_oplusrom_version = str(self.ctx.portrom.oplusrom_version or "")

        # Copy region and chipset info from baserom
        ctx.base_regionmark = str(self.ctx.baserom.region_mark or "")
        ctx.base_chipset_family = str(self.ctx.baserom.chipset_family or "unknown")
        ctx.base_device_code = str(self.ctx.baserom.device_code or "")

        return ctx

    def _load_merged_config(self, filename: str) -> dict:
        """Load and merge configuration from common, chipset and target layers."""
        from src.core.config_schema import validate_config

        paths = [Path("devices/common") / filename]

        if self.ctx.baserom.chipset_family != "unknown":
            paths.append(
                Path(f"devices/chipset/{self.ctx.baserom.chipset_family}") / filename
            )

        if self.ctx.baserom.device_code:
            device_id = self.ctx.baserom.device_code.upper()
            paths.append(Path(f"devices/target/{device_id}") / filename)

        config, report = self.merger.load_and_merge(paths, filename)

        if report.loaded_files:
            self.logger.info(
                f"Config '{filename}' loaded from {len(report.loaded_files)} layer(s)"
            )

        return config

    def _build_target_index(self, root: Path):
        """Build a cache of all files and directories in the target root for faster lookup"""
        self.logger.debug(f"Indexing target directory: {root}")
        index = {}
        if not root.exists():
            return index

        for item in root.rglob("*"):
            name = item.name
            if name not in index:
                index[name] = []
            index[name].append(item)
        return index

    def _handle_rule(
        self,
        rule: Dict,
        rtype: str,
        stock_root: Path,
        target_root: Path,
        target_index: Dict,
    ) -> List:
        """Handle a single replacement rule and return copy tasks."""
        copy_tasks = []

        if rtype == "package":
            self._process_package_replacement(rule, stock_root, target_root)
            return copy_tasks

        desc = rule.get("description", "Unknown Rule")
        search_path = rule.get("search_path", "")
        match_mode = rule.get("match_mode", "exact")
        ensure_exists = rule.get("ensure_exists", False)
        files = rule.get("files", [])

        rule_stock_root = stock_root / search_path
        rule_target_root = target_root / search_path

        if not rule_stock_root.exists():
            self.logger.debug(f"Source path not found: {rule_stock_root}")
            return copy_tasks

        for pattern in files:
            sources = self._find_sources(rule_stock_root, pattern, match_mode)
            if not sources:
                self.logger.debug(f"No source items found for pattern: {pattern}")
                continue

            for src_item in sources:
                task = self._create_copy_task(
                    src_item, rule_target_root, target_index, match_mode, ensure_exists
                )
                if task:
                    copy_tasks.append(task)

        return copy_tasks

    def _find_sources(self, root_path, pattern, match_mode):
        """Find source files based on match mode."""
        if match_mode == "glob":
            return list(root_path.glob(pattern))
        elif match_mode == "recursive":
            return list(root_path.rglob(pattern))
        else:
            exact_file = root_path / pattern
            return [exact_file] if exact_file.exists() else []

    def _create_copy_task(
        self, src_item, rule_target_root, target_index, match_mode, ensure_exists
    ):
        """Create a copy task tuple if conditions are met."""
        rel_name = src_item.name
        target_item, found_in_target = self._resolve_target_path(
            src_item, rule_target_root, target_index, match_mode
        )

        should_copy = found_in_target or ensure_exists

        if (
            should_copy
            and ensure_exists
            and not found_in_target
            and match_mode == "recursive"
        ):
            try:
                rel = src_item.relative_to(rule_target_root.parent)
                target_item = rule_target_root.parent / rel
            except ValueError:
                pass

        if should_copy:
            return (src_item, target_item, rel_name)

        self.logger.debug(
            f"  Skipping {rel_name} (Target missing and ensure_exists=False)"
        )
        return None

    def _resolve_target_path(
        self, src_item, rule_target_root, target_index, match_mode
    ):
        """Resolve target path using index for recursive matches."""
        if match_mode == "recursive":
            candidates = target_index.get(src_item.name, [])
            if candidates:
                best_match = self._find_best_candidate(candidates, rule_target_root)
                if best_match:
                    return best_match, True
                return candidates[0], True
        else:
            if (rule_target_root / src_item.name).exists():
                return rule_target_root / src_item.name, True

        return rule_target_root / src_item.name, False

    def _find_best_candidate(self, candidates, preferred_root):
        """Find the best candidate under the preferred root."""
        for cand in candidates:
            try:
                cand.relative_to(preferred_root)
                return cand
            except ValueError:
                continue
        return None

    def _execute_copy_tasks(self, copy_tasks):
        """Execute copy operations in parallel with progress tracking."""
        if not copy_tasks:
            return

        self.logger.info(
            f"Executing {len(copy_tasks)} replacement tasks in parallel..."
        )

        import os

        cpu_count = os.cpu_count() or 4
        max_workers = min(max(cpu_count, len(copy_tasks) // 5 + 1), 8)

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [
                executor.submit(self._handle_copy_op, *task) for task in copy_tasks
            ]
            self._wait_for_tasks(futures, len(copy_tasks))

    def _handle_copy_op(self, src_item, target_item, rel_name):
        """Execute a single copy operation."""
        self.logger.info(f"  Replacing/Adding: {rel_name}")

        if not target_item.parent.exists():
            target_item.parent.mkdir(parents=True, exist_ok=True)

        if target_item.exists():
            if target_item.is_dir():
                shutil.rmtree(target_item)
            else:
                target_item.unlink()

        if src_item.is_dir():
            shutil.copytree(src_item, target_item, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(src_item, target_item)

    def _wait_for_tasks(self, futures, total_count):
        """Wait for all tasks to complete with progress tracking."""
        completed = 0
        for future in concurrent.futures.as_completed(futures):
            completed += 1
            if completed % 10 == 0 or completed == total_count:
                self.logger.debug(f"  Replacement progress: {completed}/{total_count}")

        for future in futures:
            try:
                future.result()
            except Exception as e:
                self.logger.error(f"Replacement task failed: {e}")
                raise

    def _process_package_replacement(self, rule, stock_root, target_root):
        """Process replacements by APK package name"""
        search_path = rule.get("search_path", "")
        files = rule.get("files", [])

        # scan_apks is cached in RomPackage, so this is fast
        stock_apks = self.ctx.stock.scan_apks()
        target_apks = self.ctx.port.scan_apks()

        rule_target_root = target_root / search_path

        for pkg_name in files:
            stock_apk_info = stock_apks.get(pkg_name)
            target_apk_info = target_apks.get(pkg_name)

            if not stock_apk_info:
                self.logger.debug(f"Package {pkg_name} not found in stock ROM")
                continue

            src_path = stock_apk_info["path"]

            if target_apk_info:
                dst_path = target_apk_info["path"]
                self.logger.info(f"  Replacing {pkg_name}: {dst_path.name}")
            else:
                dst_path = rule_target_root / src_path.name
                self.logger.info(f"  Adding {pkg_name}: {dst_path.name}")

            if not dst_path.parent.exists():
                dst_path.parent.mkdir(parents=True, exist_ok=True)

            if dst_path.exists():
                if dst_path.is_dir():
                    shutil.rmtree(dst_path)
                else:
                    dst_path.unlink()

            shutil.copy2(src_path, dst_path)


class ZipOverridePlugin(ModifierPlugin):
    """Plugin to handle ZIP overrides and file removals from replacements.yml."""

    name = "zip_override"
    description = "Apply ZIP overrides and file removals based on replacements.yml"
    priority = 15
    dependencies = ["file_replacement"]

    def __init__(self, context, **kwargs):
        super().__init__(context, **kwargs)
        self.merger = ConfigMerger(self.logger)
        self.evaluator = ConditionEvaluator()

    def modify(self) -> bool:
        """Apply ZIP overrides and file removals."""
        config = self._load_merged_config("replacements.yml")
        if not config:
            return True

        override_rules = config.get("replacements", [])
        if not override_rules:
            return True

        # Resolve dependencies between rules
        try:
            override_rules = self.merger.resolve_dependencies(override_rules)
        except Exception as e:
            self.logger.warning(f"Dependency resolution warning: {e}")

        self.logger.info("Applying ZIP overrides and file removals...")

        # Build condition context
        cond_ctx = self._build_condition_context()

        applied_count = 0
        skipped_count = 0

        for rule in override_rules:
            rule_type = rule.get("type")
            description = rule.get("description", "Unnamed override")

            # Use enhanced condition evaluator
            if not self.evaluator.evaluate(rule, cond_ctx):
                self.logger.debug(f"Skipping rule '{description}': conditions not met")
                skipped_count += 1
                continue

            self.logger.info(f"Applying: {description}")

            if rule_type == "unzip_override":
                self._execute_unzip_override(rule)
                applied_count += 1
            elif rule_type == "remove_files":
                self._execute_remove_files(rule)
                applied_count += 1
            elif rule_type == "copy_file_internal":
                self._execute_copy_file_internal(rule)
                applied_count += 1
            elif rule_type == "copy_files_from_stock":
                self._execute_copy_files_from_stock(rule)
                applied_count += 1
            elif rule_type == "conditional_copy":
                self._execute_conditional_copy(rule)
                applied_count += 1
            elif rule_type == "overlay_sync":
                self._execute_overlay_sync(rule)
                applied_count += 1
            elif rule_type == "unzip_override_group":
                applied_count += self._execute_override_group(rule, cond_ctx)

        self.logger.info(
            f"Applied {applied_count} override rules, skipped {skipped_count}"
        )
        return True

    def _build_condition_context(self) -> BuildContext:
        """Build a condition context from the current build context."""
        ctx = BuildContext()

        ctx.port_is_coloros = self.ctx.portrom.is_coloros
        ctx.port_is_coloros_global = self.ctx.portrom.is_coloros_global
        ctx.port_is_oos = self.ctx.portrom.is_oos

        ctx.base_is_coloros = self.ctx.baserom.is_coloros
        ctx.base_is_coloros_cn = self.ctx.baserom.is_coloros
        ctx.base_is_coloros_global = self.ctx.baserom.is_coloros_global
        ctx.base_is_oos = self.ctx.baserom.is_oos

        port_ver = self.ctx.portrom.android_version
        ctx.port_android_version = int(port_ver) if port_ver else 14
        base_ver = self.ctx.baserom.android_version
        ctx.base_android_version = int(base_ver) if base_ver else 14
        ctx.port_oplusrom_version = str(self.ctx.portrom.oplusrom_version or "")

        ctx.base_regionmark = str(self.ctx.baserom.region_mark or "")
        ctx.base_chipset_family = str(self.ctx.baserom.chipset_family or "unknown")
        ctx.base_device_code = str(self.ctx.baserom.device_code or "")

        return ctx

    def _load_merged_config(self, filename: str) -> dict:
        """Load and merge configuration from common, chipset and target layers."""
        paths = [Path("devices/common") / filename]

        if self.ctx.baserom.chipset_family != "unknown":
            paths.append(
                Path(f"devices/chipset/{self.ctx.baserom.chipset_family}") / filename
            )

        if self.ctx.baserom.device_code:
            device_id = self.ctx.baserom.device_code.upper()
            paths.append(Path(f"devices/target/{device_id}") / filename)

        config, report = self.merger.load_and_merge(paths, filename)

        if report.loaded_files:
            self.logger.info(
                f"Config '{filename}' loaded from {len(report.loaded_files)} layer(s)"
            )

        return config

    def _execute_unzip_override(self, rule):
        source_zip = Path(rule["source"])
        target_base_dir_config = rule.get("target_base_dir", "")

        # Fix: If target_base_dir starts with "build/", remove it to avoid duplicate paths
        if target_base_dir_config.startswith("build/"):
            target_base_dir_config = target_base_dir_config[6:]

        target_base_dir = self.ctx.work_dir / target_base_dir_config

        # Ensure asset exists (download if missing)
        if not self.ctx.assets.ensure_asset(source_zip):
            self.logger.warning(
                f"Override ZIP not found and download failed: {source_zip}, skipping."
            )
            return

        self.logger.info(f"  Unzipping '{source_zip.name}' to '{target_base_dir}'")
        try:
            with zipfile.ZipFile(source_zip, "r") as z:
                for zinfo in z.infolist():
                    # Check if this is a symlink (Unix mode: 0o120000 = S_IFLNK)
                    is_symlink = False
                    if zinfo.external_attr:
                        unix_mode = (zinfo.external_attr >> 16) & 0o777777
                        is_symlink = (unix_mode & 0o170000) == 0o120000

                    target_path = target_base_dir / zinfo.filename

                    if is_symlink:
                        link_content = z.read(zinfo.filename).decode(
                            "utf-8", errors="ignore"
                        )
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        if target_path.exists() or target_path.is_symlink():
                            target_path.unlink()
                        target_path.symlink_to(link_content)
                        self.logger.debug(
                            f"  Created symlink: {zinfo.filename} -> {link_content}"
                        )
                    else:
                        z.extract(zinfo, target_base_dir)

            self.logger.info(f"  Successfully extracted {source_zip.name}")
        except Exception as e:
            self.logger.error(f"  Failed to extract {source_zip.name}: {e}")
            return

        # Process removals AFTER unzip
        removes = rule.get("removes", [])
        if removes:
            self.logger.info(f"  Removing {len(removes)} patterns after unzip...")
            effective_base_dir_for_removes = target_base_dir
            for pattern in removes:
                matched = (
                    list(effective_base_dir_for_removes.glob(pattern))
                    if "*" in str(pattern)
                    else [effective_base_dir_for_removes / pattern]
                )
                for full_path in matched:
                    if full_path.exists():
                        if full_path.is_dir():
                            shutil.rmtree(full_path)
                            self.logger.debug(f"    Removed directory: {full_path}")
                        else:
                            full_path.unlink()
                            self.logger.debug(f"    Removed file: {full_path}")

        # Process build_props in rule
        rule_props = rule.get("build_props")
        if rule_props:
            self._apply_build_props(rule_props)

    def _execute_remove_files(self, rule):
        files_to_remove = rule.get("files", [])
        if files_to_remove:
            self.logger.info(f"  Removing {len(files_to_remove)} specified patterns...")
            target_base_dir_config = rule.get("target_base_dir", "")

            if target_base_dir_config.startswith("build/"):
                target_base_dir_config = target_base_dir_config[6:]

            effective_base_dir_for_removes = self.ctx.work_dir / target_base_dir_config
            for pattern in files_to_remove:
                matched = (
                    list(effective_base_dir_for_removes.glob(pattern))
                    if "*" in str(pattern)
                    else [effective_base_dir_for_removes / pattern]
                )
                for full_path in matched:
                    if full_path.exists():
                        if full_path.is_dir():
                            shutil.rmtree(full_path)
                            self.logger.debug(f"    Removed directory: {full_path}")
                        else:
                            full_path.unlink()
                            self.logger.debug(f"    Removed file: {full_path}")

    def _execute_copy_file_internal(self, rule):
        src_rel = rule.get("source")
        dst_rel = rule.get("target")
        target_dir = self.ctx.target_dir

        src_path = target_dir / src_rel
        dst_path = target_dir / dst_rel

        cond_dst_exists = rule.get("condition_target_exists", False)
        if cond_dst_exists and not dst_path.exists():
            return

        if src_path.exists():
            self.logger.info(f"  Copying internal: {src_rel} -> {dst_rel}")
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            if src_path.is_dir():
                shutil.copytree(src_path, dst_path, dirs_exist_ok=True)
            else:
                shutil.copy2(src_path, dst_path)
        else:
            self.logger.warning(f"  Internal copy source not found: {src_path}")

    def _execute_copy_files_from_stock(self, rule):
        source_pattern = rule.get("source")
        target_dir_rel = rule.get("target_dir", "")
        description = rule.get("description", "Copy files from stock")

        if not source_pattern:
            self.logger.warning(f"  No source pattern specified for: {description}")
            return

        stock_base = self.ctx.stock.extracted_dir
        target_base = self.ctx.target_dir

        source_path = stock_base / source_pattern

        if source_path.is_dir():
            target_path = target_base / target_dir_rel
            self.logger.info(f"  {description}: copying directory {source_pattern}")
            if target_path.exists():
                shutil.rmtree(target_path)
            shutil.copytree(source_path, target_path, symlinks=True, dirs_exist_ok=True)
            self.logger.info(f"    Copied directory: {source_path.name}")
            return

        target_path = target_base / target_dir_rel
        source_files = list(stock_base.glob(source_pattern))

        if not source_files:
            self.logger.debug(f"  No files matching pattern: {source_pattern}")
            return

        self.logger.info(f"  {description}: copying {len(source_files)} file(s)")
        target_path.mkdir(parents=True, exist_ok=True)

        copied_count = 0
        for src_file in source_files:
            if src_file.is_file():
                dst_file = target_path / src_file.name
                try:
                    shutil.copy2(src_file, dst_file)
                    self.logger.debug(f"    Copied: {src_file.name}")
                    copied_count += 1
                except Exception as e:
                    self.logger.warning(f"    Failed to copy {src_file.name}: {e}")

        self.logger.info(f"  Copied {copied_count} file(s) to {target_dir_rel}")

    def _execute_conditional_copy(self, rule):
        source_rel = rule.get("source")
        target_rel = rule.get("target")
        description = rule.get("description", "Conditional copy")

        if not source_rel or not target_rel:
            self.logger.warning(f"  Missing source or target for: {description}")
            return

        stock_base = self.ctx.stock.extracted_dir
        source_path = stock_base / source_rel

        target_base = self.ctx.target_dir
        target_path = target_base / target_rel

        if source_path.exists():
            self.logger.info(f"  {description}: copying from baserom")
            target_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                if source_path.is_dir():
                    shutil.copytree(source_path, target_path, dirs_exist_ok=True)
                else:
                    shutil.copy2(source_path, target_path)
                self.logger.info(f"    Copied: {source_rel} -> {target_rel}")
            except Exception as e:
                self.logger.warning(f"    Failed to copy {source_rel}: {e}")
        else:
            self.logger.info(
                f"  {description}: source not in baserom, removing from portrom if exists"
            )
            if target_path.exists():
                try:
                    if target_path.is_dir():
                        shutil.rmtree(target_path)
                    else:
                        target_path.unlink()
                    self.logger.info(f"    Removed: {target_rel}")
                except Exception as e:
                    self.logger.warning(f"    Failed to remove {target_rel}: {e}")

    def _execute_overlay_sync(self, rule):
        description = rule.get("description", "Overlay sync")
        copy_vendor = rule.get("copy_vendor", False)
        remove_patterns = rule.get("remove_patterns", [])
        overlay_pattern = rule.get("overlay_pattern", "")

        var_substitutions = {
            "my_product_type": getattr(self.ctx, "base_my_product_type", ""),
            "base_product_device": getattr(self.ctx, "base_product_device", ""),
            "device_code": getattr(self.ctx, "base_device_code", ""),
        }

        if overlay_pattern:
            for var_name, var_value in var_substitutions.items():
                if var_value:
                    placeholder = f"{{{var_name}}}"
                    overlay_pattern = overlay_pattern.replace(
                        placeholder, str(var_value)
                    )

        self.logger.info(f"  {description}")

        stock_base = self.ctx.stock.extracted_dir
        target_base = self.ctx.target_dir

        if copy_vendor:
            vendor_source = stock_base / "my_product" / "vendor"
            vendor_target = target_base / "my_product" / "vendor"

            if vendor_source.exists():
                self.logger.info(f"    Copying vendor directory from baserom")
                try:
                    if vendor_target.exists():
                        shutil.rmtree(vendor_target)
                    shutil.copytree(vendor_source, vendor_target)
                    self.logger.info(f"    Copied vendor directory")
                except Exception as e:
                    self.logger.warning(f"    Failed to copy vendor directory: {e}")

        overlay_target_dir = target_base / "my_product" / "overlay"
        if overlay_target_dir.exists() and remove_patterns:
            for pattern in remove_patterns:
                try:
                    matched_files = list(overlay_target_dir.glob(pattern))
                    for file_path in matched_files:
                        if file_path.exists():
                            if file_path.is_dir():
                                shutil.rmtree(file_path)
                            else:
                                file_path.unlink()
                            self.logger.info(f"    Removed: {file_path.name}")
                except Exception as e:
                    self.logger.warning(
                        f"    Failed to remove files matching {pattern}: {e}"
                    )

        if overlay_pattern:
            copied_count = 0
            for search_dir in stock_base.rglob("my_product/overlay"):
                if search_dir.exists():
                    try:
                        matched_files = list(search_dir.glob(overlay_pattern))
                        for source_file in matched_files:
                            if source_file.is_file():
                                target_file = overlay_target_dir / source_file.name
                                overlay_target_dir.mkdir(parents=True, exist_ok=True)
                                try:
                                    shutil.copy2(source_file, target_file)
                                    self.logger.info(
                                        f"    Copied overlay: {source_file.name}"
                                    )
                                    copied_count += 1
                                except Exception as e:
                                    self.logger.warning(
                                        f"    Failed to copy {source_file.name}: {e}"
                                    )
                    except Exception as e:
                        self.logger.warning(f"    Failed to search {search_dir}: {e}")

            if copied_count > 0:
                self.logger.info(f"    Total overlay files copied: {copied_count}")

    def _execute_override_group(self, rule, cond_ctx):
        self.logger.info(
            f"Processing override group: {rule.get('description', 'unnamed')}"
        )
        group_applied = 0

        for op in rule.get("operations", []):
            op_type = op.get("type")

            if not self.evaluator.evaluate(op, cond_ctx):
                op_desc = op.get("description", "unnamed operation")
                self.logger.debug(
                    f"  Skipping operation '{op_desc}': conditions not met"
                )
                continue

            if op_type == "unzip_override":
                self._execute_unzip_override(op)
                group_applied += 1
            elif op_type == "remove_files":
                self._execute_remove_files(op)
                group_applied += 1
            elif op_type == "copy_file_internal":
                self._execute_copy_file_internal(op)
                group_applied += 1

        return group_applied

    def _apply_build_props(self, props_map):
        for partition, props in props_map.items():
            if partition == "vendor":
                prop_file = self.ctx.target_dir / "vendor/build.prop"
            elif partition == "product":
                prop_file = self.ctx.target_dir / "product/etc/build.prop"
            else:
                continue

            if not prop_file.exists():
                continue

            content = prop_file.read_text(encoding="utf-8", errors="ignore")
            lines = content.splitlines()
            new_lines = []

            existing_keys = set()
            for line in lines:
                if "=" in line and not line.strip().startswith("#"):
                    existing_keys.add(line.split("=")[0].strip())
                new_lines.append(line)

            appended = False
            for key, value in props.items():
                if key not in existing_keys:
                    new_lines.append(f"{key}={value}")
                    self.logger.debug(f"Appended prop to {partition}: {key}={value}")
                    appended = True

            if appended:
                prop_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


class PermissionMigrationPlugin(ModifierPlugin):
    """Plugin to migrate permission and configuration files from baserom to portrom."""

    name = "permission_migration"
    description = "Migrate permission and configuration files from baserom to portrom"
    priority = 20
    dependencies = ["zip_override"]

    def modify(self) -> bool:
        """Migrate permission and configuration files."""
        self.logger.info("Migrating permission and configuration files...")

        target_product_etc = self.ctx.target_dir / "my_product" / "etc"
        stock_product_etc = self.ctx.stock.extracted_dir / "my_product" / "etc"

        if not target_product_etc.exists() or not stock_product_etc.exists():
            self.logger.warning("my_product/etc not found, skipping migration.")
            return True

        with tempfile.TemporaryDirectory(prefix="perm_backup_") as tmp_dir:
            tmp_path = Path(tmp_dir)
            restored_count = self._backup_and_restore_permissions(
                target_product_etc, tmp_path / "permissions"
            )
            self.logger.debug(f"Restored {restored_count} permission files from backup")
            self._copy_extensions_and_configs(stock_product_etc, target_product_etc)

        self._apply_region_specific_fixes()
        return True

    def _backup_and_restore_permissions(self, target_product_etc, tmp_perms):
        """Backup portrom permissions, copy baserom, then restore specific files."""
        tmp_perms.mkdir(parents=True, exist_ok=True)
        target_perms = target_product_etc / "permissions"

        if target_perms.exists():
            for f in target_perms.glob("*.xml"):
                shutil.copy2(f, tmp_perms)

        stock_perms = (
            self.ctx.stock.extracted_dir / "my_product" / "etc" / "permissions"
        )
        if stock_perms.exists():
            target_perms.mkdir(parents=True, exist_ok=True)
            for f in stock_perms.glob("*.xml"):
                shutil.copy2(f, target_perms)

        patterns = [
            "multimedia*.xml",
            "*permissions*.xml",
            "*google*.xml",
            "*configs*.xml",
            "*gsm*.xml",
            "feature_activity_preload.xml",
            "*gemini*.xml",
            "*gms*.xml",
        ]

        restored_count = 0
        for pattern in patterns:
            for f in tmp_perms.glob(pattern):
                shutil.copy2(f, target_perms)
                restored_count += 1

        return restored_count

    def _copy_extensions_and_configs(self, stock_etc, target_etc):
        """Copy extensions and configuration files from stock to target."""
        stock_exts = stock_etc / "extension"
        target_exts = target_etc / "extension"

        if stock_exts.exists():
            target_exts.mkdir(parents=True, exist_ok=True)
            for f in stock_exts.glob("*.xml"):
                shutil.copy2(f, target_exts)

        config_files = {
            "refresh_rate_config.xml": None,
            "sys_resolution_switch_config.xml": None,
            "com.oplus.sensor_config.xml": "permissions",
        }

        for config_file, subdir in config_files.items():
            src = stock_etc / config_file
            if src.exists():
                dst = (
                    target_etc / subdir / config_file
                    if subdir
                    else target_etc / config_file
                )
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)

    def _apply_region_specific_fixes(self):
        """Apply region-specific app_v2.xml edits for non-CN regions."""
        regionmark = getattr(self.ctx, "base_regionmark", "CN")
        if regionmark == "CN":
            return

        app_v2 = self.ctx.target_dir / "my_stock" / "etc" / "config" / "app_v2.xml"
        if not app_v2.exists():
            return

        self.logger.info(f"Applying region-specific fixes (region: {regionmark})")

        pkgs_to_remove = [
            "com.android.contacts",
            "com.android.incallui",
            "com.android.mms",
            "com.oplus.blacklistapp",
            "com.oplus.phonenoareainquire",
            "com.ted.number",
        ]

        content = app_v2.read_text(encoding="utf-8", errors="ignore")
        lines = [
            line
            for line in content.splitlines()
            if not any(pkg in line for pkg in pkgs_to_remove)
        ]

        app_v2.write_text("\n".join(lines) + "\n", encoding="utf-8")


class FeatureHandlerPlugin(ModifierPlugin):
    """Plugin to apply features using the handler-based architecture."""

    name = "feature_handler"
    description = "Apply XML features and build properties from features.yml"
    priority = 30
    dependencies = ["permission_migration"]

    def modify(self) -> bool:
        """Apply features using handlers."""
        self.logger.info("Applying features using handler architecture...")

        # Load features config
        config = self._load_merged_config("features.yml")
        if not config:
            self.logger.info("No features config found, skipping.")
            return True

        # Create handler registry and register handlers
        from src.handlers import XmlFeatureHandler, BuildPropHandler, SmaliHandler, HandlerRegistry

        registry = HandlerRegistry()
        registry.register(XmlFeatureHandler())
        registry.register(BuildPropHandler())
        registry.register(SmaliHandler())

        # Apply all handlers
        registry.apply_all(config, self.ctx)

        # Also apply ColorOS-specific XML features
        self._apply_coloros_xml_features(
            config.get("oplus_features", []), "oplus_feature"
        )
        self._apply_coloros_xml_features(config.get("app_features", []), "app_feature")
        self._apply_coloros_xml_features(
            config.get("permission_features", []), "permission_feature"
        )

        return True

    def _load_merged_config(self, filename: str) -> dict:
        """Load and merge configuration from common, chipset and target layers."""
        from src.core.config_merger import ConfigMerger

        merger = ConfigMerger(self.logger)
        paths = [Path("devices/common") / filename]

        if self.ctx.baserom.chipset_family != "unknown":
            paths.append(
                Path(f"devices/chipset/{self.ctx.baserom.chipset_family}") / filename
            )

        if self.ctx.baserom.device_code:
            device_id = self.ctx.baserom.device_code.upper()
            paths.append(Path(f"devices/target/{device_id}") / filename)

        config, report = merger.load_and_merge(paths, filename)

        if report.loaded_files:
            self.logger.info(
                f"Config '{filename}' loaded from {len(report.loaded_files)} layer(s)"
            )

        return config

    def _apply_coloros_xml_features(self, features: list, feature_type: str):
        """Apply ColorOS XML features - port.sh add_feature_v2 logic"""
        if not features:
            return

        target_dir = self.ctx.target_dir / "my_product" / "etc"

        if feature_type == "oplus_feature":
            xml_dir = target_dir / "extension"
            base_file = "com.oplus.oplus-feature"
            root_tag = "oplus-config"
            node_tag = "oplus-feature"
        elif feature_type == "app_feature":
            xml_dir = target_dir / "extension"
            base_file = "com.oplus.app-features"
            root_tag = "extend_features"
            node_tag = "app_feature"
        elif feature_type == "permission_feature":
            xml_dir = target_dir / "permissions"
            base_file = "com.oplus.android-features"
            root_tag = "permissions"
            node_tag = "feature"
        else:
            return

        xml_dir.mkdir(parents=True, exist_ok=True)
        output_file = xml_dir / f"{base_file}-ext.xml"

        if not output_file.exists():
            content = (
                f'<?xml version="1.0" encoding="UTF-8"?>\n<{root_tag}>\n</{root_tag}>\n'
            )
            output_file.write_text(content, encoding="utf-8")

        content = output_file.read_text(encoding="utf-8")

        for entry in features:
            parts = entry.split("^")
            feature = parts[0].strip()
            comment = parts[1].strip() if len(parts) > 1 and parts[1] else ""
            extra = parts[2].strip() if len(parts) > 2 else ""

            if self._check_feature_exists(feature):
                self.logger.info(f"Feature {feature} already exists, skipping.")
                continue

            self.logger.info(f"Adding feature: {feature}")

            attrs = f'name="{feature}"'
            if extra:
                if extra.startswith("args="):
                    attrs = f"{attrs} {extra}"
                else:
                    attrs = f"{attrs} {extra}"

            if comment:
                comment_line = f"    <!-- {comment} -->\n"
                content = content.replace(
                    f"</{root_tag}>",
                    comment_line + f"    <{node_tag} {attrs}/>\n</{root_tag}>",
                )
            else:
                content = content.replace(
                    f"</{root_tag}>", f"    <{node_tag} {attrs}/>\n</{root_tag}>"
                )

        output_file.write_text(content, encoding="utf-8")

    def _check_feature_exists(self, feature: str) -> bool:
        """Check if feature already exists in any XML file"""
        my_product_etc = self.ctx.target_dir / "my_product" / "etc"
        if not my_product_etc.exists():
            return False

        for xml_file in my_product_etc.rglob("*.xml"):
            try:
                content = xml_file.read_text(encoding="utf-8", errors="ignore")
                if feature in content:
                    return True
            except Exception:
                pass
        return False


class DolbyFixPlugin(ModifierPlugin):
    """Plugin to fix Dolby audio and multi-app volume."""

    name = "dolby_fix"
    description = "Fix Dolby audio and copy dolby configuration files"
    priority = 40

    def modify(self) -> bool:
        """Apply Dolby audio fix."""
        self.logger.info("Checking for Dolby audio fix...")

        baserom = self.ctx.stock.extracted_dir
        target = self.ctx.target_dir

        # Check if baserom has dolby effect type
        dolby_prop = baserom / "my_product" / "build.prop"
        if not dolby_prop.exists():
            return True

        content = dolby_prop.read_text(encoding="utf-8", errors="ignore")
        if "ro.oplus.audio.effect.type=dolby" not in content:
            return True

        self.logger.info("Applying Dolby audio fix...")

        # Copy dolby xml
        dolby_xml = (
            baserom
            / "my_product"
            / "etc"
            / "permissions"
            / "oplus.product.features_dolby_stereo.xml"
        )
        target_xml = (
            target
            / "my_product"
            / "etc"
            / "permissions"
            / "oplus.product.features_dolby_stereo.xml"
        )
        if dolby_xml.exists():
            shutil.copy2(dolby_xml, target_xml)

        # Try to extract dolby_fix.zip from devices/common if exists
        dolby_zip = Path("devices/common/dolby_fix.zip")
        if self.ctx.assets.ensure_asset(dolby_zip):
            try:
                with zipfile.ZipFile(dolby_zip, "r") as z:
                    z.extractall(target)
                self.logger.info("Extracted dolby_fix.zip")
            except Exception as e:
                self.logger.warning(f"Failed to extract dolby_fix.zip: {e}")

        return True


class AIMemoryPlugin(ModifierPlugin):
    """Plugin to apply AI Memory and AppBooster."""

    name = "ai_memory"
    description = "Apply AI Memory and AppBooster enhancements"
    priority = 45

    def modify(self) -> bool:
        """Apply AI Memory and AppBooster."""
        self.logger.info("Applying AI Memory / AppBooster...")

        target = self.ctx.target_dir
        ai_memory_zip = Path("devices/common/ai_memory.zip")
        ai_memory_in_zip = Path("devices/common/ai_memory_in/aimemory.zip")

        # Determine which zip to use based on region
        regionmark = getattr(self.ctx, "regionmark", "CN")

        if regionmark == "CN":
            if self.ctx.assets.ensure_asset(ai_memory_zip):
                try:
                    with zipfile.ZipFile(ai_memory_zip, "r") as z:
                        z.extractall(target)
                    self.logger.info("Extracted ai_memory.zip")
                except Exception as e:
                    self.logger.warning(f"Failed to extract ai_memory.zip: {e}")
        else:
            if self.ctx.assets.ensure_asset(ai_memory_in_zip):
                try:
                    with zipfile.ZipFile(ai_memory_in_zip, "r") as z:
                        z.extractall(target)
                    self.logger.info("Extracted ai_memory_in/aimemory.zip")
                except Exception as e:
                    self.logger.warning(
                        f"Failed to extract ai_memory_in/aimemory.zip: {e}"
                    )

        # Enable AIMemory and AppBooster in app_v2.xml
        app_v2 = target / "my_product" / "etc" / "config" / "app_v2.xml"
        if app_v2.exists():
            content = app_v2.read_text(encoding="utf-8", errors="ignore")
            for pkg in ["com.oplus.aimemory", "com.oplus.appbooster"]:
                if f'<enable pkg="{pkg}"' not in content:
                    content = content.replace(
                        "</app>", f'  <enable pkg="{pkg}" priority="7"/>\n</app>'
                    )
            app_v2.write_text(content, encoding="utf-8")

        return True


class VNDKFixPlugin(ModifierPlugin):
    """Plugin to fix VNDK APEX and VINTF manifest."""

    name = "vndk_fix"
    description = "Fix VNDK APEX and VINTF manifest"
    priority = 50

    def modify(self) -> bool:
        """Apply VNDK fixes."""
        self._fix_vndk_apex()
        self._fix_vintf_manifest()
        return True

    def _fix_vndk_apex(self):
        """Copy missing VNDK APEX from stock."""
        vndk_version = self.ctx.stock.get_prop("ro.vndk.version")

        if not vndk_version:
            for prop in (self.ctx.stock.extracted_dir / "vendor").rglob("*.prop"):
                try:
                    with open(prop, errors="ignore") as f:
                        for line in f:
                            if "ro.vndk.version=" in line:
                                vndk_version = line.split("=")[1].strip()
                                break
                except:
                    pass
                if vndk_version:
                    break

        if not vndk_version:
            return

        apex_name = f"com.android.vndk.v{vndk_version}.apex"
        stock_apex = self._find_file_recursive(
            self.ctx.stock.extracted_dir / "system_ext/apex", apex_name
        )
        target_apex_dir = self.ctx.target_dir / "system_ext/apex"

        if stock_apex and target_apex_dir.exists():
            target_file = target_apex_dir / apex_name
            if not target_file.exists():
                self.logger.info(f"Copying missing VNDK Apex: {apex_name}")
                shutil.copy2(stock_apex, target_file)

    def _fix_vintf_manifest(self):
        """Fix VINTF manifest for VNDK version."""
        self.logger.info("Checking VINTF manifest for VNDK version...")

        vndk_version = self.ctx.stock.get_prop("ro.vndk.version")
        if not vndk_version:
            vendor_prop = self.ctx.target_dir / "vendor/build.prop"
            if vendor_prop.exists():
                try:
                    content = vendor_prop.read_text(encoding="utf-8", errors="ignore")
                    match = re.search(r"ro\.vndk\.version=(.*)", content)
                    if match:
                        vndk_version = match.group(1).strip()
                except:
                    pass

        if not vndk_version:
            self.logger.warning("Could not determine VNDK version")
            return

        self.logger.info(f"Target VNDK Version: {vndk_version}")

        target_xml = self._find_file_recursive(
            self.ctx.target_dir / "system_ext", "manifest.xml"
        )
        if not target_xml:
            return

        original_content = target_xml.read_text(encoding="utf-8")

        if f"<version>{vndk_version}</version>" in original_content:
            self.logger.info(
                f"VNDK {vndk_version} already exists in manifest. Skipping."
            )
            return

        new_block = f"""    <vendor-ndk>
        <version>{vndk_version}</version>
    </vendor-ndk>"""

        if "</manifest>" in original_content:
            new_content = original_content.replace(
                "</manifest>", f"{new_block}\n</manifest>"
            )

            target_xml.write_text(new_content, encoding="utf-8")
            self.logger.info(f"Injected VNDK {vndk_version} into {target_xml.name}")

    def _find_file_recursive(self, root_dir: Path, filename: str) -> Optional[Path]:
        if not root_dir.exists():
            return None
        try:
            return next(root_dir.rglob(filename))
        except StopIteration:
            return None


class DeviceOverridePlugin(ModifierPlugin):
    """Plugin to apply device-specific overrides."""

    name = "device_override"
    description = "Apply device-specific override files"
    priority = 60

    def modify(self) -> bool:
        """Apply device overrides."""
        base_code = self.ctx.stock_rom_code
        port_ver = self.ctx.portrom.android_version

        override_src = Path(f"devices/{base_code}/override/{port_ver}").resolve()

        if not override_src.exists() or not override_src.is_dir():
            self.logger.warning(f"Device overlay dir not found: {override_src}")
            return True

        self.logger.info(f"Applying device overrides from: {override_src}")

        has_nfc_override = False
        for f in override_src.rglob("*.apk"):
            name = f.name.lower()
            if name.startswith("nqnfcnci") or name.startswith("nfc_st"):
                has_nfc_override = True
                break

        if has_nfc_override:
            self.logger.info(
                "Detected NFC override, cleaning old NFC directories in target..."
            )
            for p in self.ctx.target_dir.rglob("*"):
                if p.is_dir():
                    name = p.name.lower()
                    if name.startswith("nqnfcnci") or name.startswith("nfc_st"):
                        self.logger.info(f"Removing old NFC dir: {p}")
                        shutil.rmtree(p)

        self.logger.info("Copying override files...")
        try:
            shutil.copytree(override_src, self.ctx.target_dir, dirs_exist_ok=True)
        except Exception as e:
            self.logger.error(f"Failed to copy overrides: {e}")

        return True


# Export all plugins
__all__ = [
    "FileReplacementPlugin",
    "ZipOverridePlugin",
    "PermissionMigrationPlugin",
    "FeatureHandlerPlugin",
    "DolbyFixPlugin",
    "AIMemoryPlugin",
    "VNDKFixPlugin",
    "DeviceOverridePlugin",
]
