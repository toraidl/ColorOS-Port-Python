"""
Smali Handler Module for automated APK patching.

This handler handles decompiling APKs, applying smali patches via SmaliKit,
and recompiling them using apktool.
"""

import os
import shutil
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

from .base import BaseHandler
from src.utils.smalikit import SmaliKit, SmaliArgs
from src.utils.shell import ShellRunner

class SmaliHandler(BaseHandler):
    """
    Handler for Smali byte-code modifications.
    
    Configuration format in features.yml:
    "smali_patches": [
        {
            "apk_name": "framework-res",
            "package_name": "com.android.frameworkres",
            "description": "Disable signature verification",
            "patches": [
                {
                    "method": "verifySignature",
                    "remake": "const/4 v0, 0x1\nreturn v0"
                }
            ]
        }
    ]
    """

    def __init__(self):
        super().__init__()
        self.shell = ShellRunner()
        self.apktool_jar = Path("bin/apktool/apktool.jar").resolve()

    def can_handle(self, config: Dict[str, Any]) -> bool:
        return "smali_patches" in config

    def validate(self, config: Dict[str, Any]) -> List[str]:
        errors = []
        patches = config.get("smali_patches", [])
        if not isinstance(patches, list):
            errors.append("smali_patches must be a list")
            return errors

        for i, patch_group in enumerate(patches):
            if "apk_name" not in patch_group and "package_name" not in patch_group:
                errors.append(f"smali_patches[{i}]: missing apk_name or package_name")
            if "patches" not in patch_group or not isinstance(patch_group["patches"], list):
                errors.append(f"smali_patches[{i}]: missing or invalid 'patches' list")
        
        return errors

    def apply(self, config: Dict[str, Any], context: Any) -> None:
        patch_groups = config.get("smali_patches", [])
        self.logger.info(f"Processing {len(patch_groups)} Smali patch groups")

        for group in patch_groups:
            self._process_patch_group(group, context)

    def _process_patch_group(self, group: Dict[str, Any], context: Any) -> None:
        apk_name = group.get("apk_name")
        package_name = group.get("package_name")
        description = group.get("description", "Unnamed patch")
        
        self.logger.info(f"Applying Smali patch: {description} for {apk_name or package_name}")

        # 1. Locate APK
        apk_path = self._find_apk(context, apk_name, package_name)
        if not apk_path:
            self.logger.warning(f"Could not find APK for {apk_name or package_name}, skipping.")
            return

        # 2. Setup temp work dir
        temp_dir = context.work_dir / "temp_smali" / (apk_name or package_name or "unknown")
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        temp_dir.mkdir(parents=True, exist_ok=True)

        try:
            # 3. Decompile
            self.logger.info(f"Decompiling {apk_path.name}...")
            decompile_cmd = ["java", "-jar", str(self.apktool_jar), "d", "-f", str(apk_path), "-o", str(temp_dir)]
            self.shell.run(decompile_cmd)

            # 4. Apply Patches
            applied_any = False
            for patch_cfg in group["patches"]:
                # Map config to SmaliArgs
                args = SmaliArgs(**patch_cfg)
                # If path is not set in patch, default to decompiled dir
                if not args.path:
                    args.path = str(temp_dir)
                
                patcher = SmaliKit(args)
                # SmaliKit.walk_and_patch returns None but logs results. 
                # We might want to improve SmaliKit to return status.
                patcher.walk_and_patch(args.path)
                applied_any = True

            # 5. Recompile
            if applied_any:
                self.logger.info(f"Recompiling {apk_path.name}...")
                unsigned_apk = temp_dir.parent / f"{apk_path.stem}_patched.apk"
                recompile_cmd = ["java", "-jar", str(self.apktool_jar), "b", str(temp_dir), "-o", str(unsigned_apk)]
                self.shell.run(recompile_cmd)

                # 6. Replace original (Back up first)
                bak_path = apk_path.with_suffix(".apk.bak")
                if not bak_path.exists():
                    shutil.copy2(apk_path, bak_path)
                
                shutil.move(str(unsigned_apk), str(apk_path))
                self.logger.info(f"Successfully patched and replaced {apk_path.name}")
            
        except Exception as e:
            self.logger.error(f"Failed to patch {apk_path.name}: {e}", exc_info=True)
        finally:
            # Cleanup
            if temp_dir.exists():
                shutil.rmtree(temp_dir)

    def _find_apk(self, context: Any, apk_name: Optional[str], package_name: Optional[str]) -> Optional[Path]:
        """Search for APK in target directory."""
        # Try package name first if we have a scan cache
        if package_name and hasattr(context.portrom, "_apk_cache"):
            apk_info = context.portrom._apk_cache.get(package_name)
            if apk_info:
                # Need to map from portrom's extracted path to context's target_dir
                rel_path = apk_info["relative_path"]
                target_apk = context.target_dir / rel_path
                if target_apk.exists():
                    return target_apk

        # Search by filename
        if apk_name:
            search_name = apk_name if apk_name.endswith(".apk") else f"{apk_name}.apk"
            # Fast scan common locations
            for part in ["system", "system_ext", "product", "vendor", "my_product"]:
                part_dir = context.target_dir / part
                if not part_dir.exists():
                    continue
                # Look in app, priv-app, framework
                for sub in ["app", "priv-app", "framework"]:
                    found = list((part_dir / sub).rglob(search_name))
                    if found:
                        return found[0]
            
            # Slow scan as last resort
            found = list(context.target_dir.rglob(search_name))
            if found:
                return found[0]

        return None
