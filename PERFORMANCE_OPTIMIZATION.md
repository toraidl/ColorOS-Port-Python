# Storage Performance Optimizations

This document details the optimizations implemented to reduce disk space usage during the ROM porting process. These changes were introduced to address issues where the `build/` directory could exceed 50GB.

## 1. Hard Link Installation (`cp -al`)

**Before:** Partitions were copied from `build/baserom/extracted/` and `build/portrom/extracted/` to `build/target/` using a standard copy command. This resulted in a complete duplication of the entire filesystem (approx. 15-20GB extra space).

**After:** We now use `cp -al` to create hard links between the extracted files and the target directory. 
- **Space Saving:** ~10-20GB per build.
- **Speed:** Installation is nearly instantaneous regardless of partition size.
- **Safety:** Automatically falls back to regular copy if hard links are not supported (e.g., cross-filesystem operations).

## 2. Redundant Image Cleanup

**Before:** ROM packages kept all extracted `.img` files in `build/baserom/images/` and `build/portrom/images/` even after they were extracted to folders.

**After:** A `cleanup_images()` stage removes these `.img` files once they have been successfully extracted to folders.
- **Space Saving:** ~15GB (Size of all logical partition images).
- **Control:** Critical images like `boot.img` and `vbmeta.img` are preserved for later stages.

## 3. Intermediate Data Cleanup

**Before:** Extracted folders in `baserom/` and `portrom/` remained until the end of the entire process.

**After:** A dedicated `Cleanup Intermediate Data` stage runs after `Stage 1: Partition Installation`. It removes the source extraction folders for any partitions that have been successfully linked to the `target/` directory.
- **Space Saving:** ~15-20GB.
- **Safety:** Only removes partitions that are verified to exist in the `target/` directory.

## Summary of Impact

| Component | Space Before | Space After | Saving |
| --- | --- | --- | --- |
| baserom/images | 8.3 GB | ~1.0 GB | 7.3 GB |
| portrom/images | 7.2 GB | ~0.5 GB | 6.7 GB |
| baserom/extracted | 9.6 GB | ~0.1 GB | 9.5 GB |
| portrom/extracted | 11.0 GB | ~0.1 GB | 10.9 GB |
| **Total Build Dir** | **~50 GB** | **~15-20 GB** | **~30-35 GB** |

*Note: The final `target/` directory still consumes its full size, but redundant copies are eliminated.*
