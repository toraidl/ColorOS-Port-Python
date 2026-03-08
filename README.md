<div align="center">

# ColorOS Port Python

A Python-based porting tool for ColorOS, created with AI Large Language Models (Gemini, Qwen, etc.).

</div>

<p align="center">
  <a href="./README.md">English</a> | <a href="./README_zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="https://img.shields.io/github/stars/toraidl/ColorOS-Port-Python?style=flat&logo=github" alt="Stars">
  <img src="https://img.shields.io/github/forks/toraidl/ColorOS-Port-Python?style=flat&logo=github" alt="Forks">
  <img src="https://img.shields.io/github/issues/toraidl/ColorOS-Port-Python" alt="Issues">
  <img src="https://img.shields.io/github/license/toraidl/ColorOS-Port-Python" alt="License">
</p>

## ✨ Features

- **Context-Aware Architecture**: Uses a `Context` object to manage the entire porting lifecycle.
- **Modular Design**: Separates concerns into distinct modules (`rom`, `props`, `patcher`, `packer`).
- **Configuration Driven**: Uses JSON configuration files for device-specific settings.
- **Performance Optimized**:
    - Property file caching with `PropCache` class
    - Batch property updates reducing file I/O
    - Baserom property pre-loading for faster access
    - Exclusion patterns for unnecessary directories
- **Automated Patching**:
    - `PropertyModifier`: Automatically syncs properties between Base and Port ROMs.
    - `SmaliPatcher`: Decompiles and patches `services.jar` and `framework.jar`.
- **Advanced Repacking**: Supports packing partitions as EROFS or EXT4, and generating `super.img` or OTA `payload.bin`.
- **Enhanced Configuration Framework**: New validation, flexible conditions, and dependency management.
- **Comprehensive Error Handling**: Graceful error recovery with detailed logging.

## 📱 Supported Devices

This tool is designed to theoretically support Qualcomm Snapdragon chips beyond **SM8250**.

**Currently Expected Supported Models:**
-   **OnePlus SM8250 Series**: OnePlus 8, OnePlus 8 Pro, OnePlus 8T
-   **Oppo Find X3** (SM8350)
-   **OnePlus SM8350 Series**: OnePlus 9, OnePlus 9 Pro

**Important Note for ColorOS 16:**
ColorOS 16 requires specific kernel support. Please ensure your device's kernel is compatible if attempting to port ColorOS 16.


## 🚀 Getting Started

This section will guide you on how to set up and run the tool.

### Prerequisites

- **Operating System**: A Linux distribution (e.g., Ubuntu, Arch) on an x86_64 architecture.
- **Python**: Python 3.10 or newer.
- **Java**: Java Development Kit (JDK) 11 or newer.
- **Docker**: (Recommended) For a hassle-free setup.

### Option 1: Deploying with Docker (Recommended)

Using Docker is the recommended way to run this tool. It creates a self-contained environment with all the necessary dependencies pre-installed.

1.  **Build the Docker image:**
    ```bash
    docker build -t coloros-port .
    ```

2.  **Run the container:**
    Mount your local folders into the container so the script can access your ROMs and write the output back to your machine.
    ```bash
    # Example:
    docker run --rm -it \
      -v /path/to/your/roms:/roms \
      -v $(pwd)/build:/app/build \
      coloros-port \
      python3 main.py --baserom /roms/base_rom.zip --portrom /roms/port_rom.zip
    ```
    - **Remember to replace `/path/to/your/roms` with the actual path on your computer.**
    - The output will be in the `build` directory on your host machine.

### Option 2: Manual Setup

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/toraidl/ColorOS-Port-Python.git
    cd ColorOS-Port-Python
    ```

2.  **Set file permissions:**
    ```bash
    chmod +x -R bin/linux/x86_64/
    ```

3.  **Run the script:**
    - **Basic Usage:**
        ```bash
        python3 main.py --baserom <path/to/base.zip> --portrom <path/to/port.zip>
        ```
    - **Advanced Usage (with arguments):**
        ```bash
        # Specify device code, pack type, and enable debug logging
        python3 main.py \
          --baserom <path/to/base.zip> \
          --portrom <path/to/port.zip> \
          --device_code OP4E7L1 \
          --pack_type super \
          --debug
        ```
    - The output will be in the `build` directory by default.

## 🛠️ Hierarchical Configuration System

The project uses a powerful three-layer inheritance system for ROM modifications, allowing for easy expansion and multi-device support without duplicate logic. Modifications are loaded and merged in the following order:

1.  **Common Layer (`devices/common/`)**: Global patches for all devices.
2.  **Chipset Layer (`devices/chipset/<FAMILY>/`)**: Chipset-specific modifications.
3.  **Target Layer (`devices/target/<DEVICE>/`)**: Device-specific hardware patches.

> See the `devices` directory for examples like `features.yml` and `replacements.yml`.

---

## 📖 Enhanced Configuration Framework Guide

The new framework provides powerful features for defining device-specific modifications with validation, flexible conditions, and dependency management.

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    Configuration Layers                      │
├─────────────────────────────────────────────────────────────┤
│  Common Layer    →  Chipset Layer   →   Target Layer        │
│  (All devices)      (SM8250/8350)       (Specific device)   │
└─────────────────────────────────────────────────────────────┘
         ↓                    ↓                      ↓
         └────────────────────┴──────────────────────┘
                              ↓
                    Merged Configuration
                              ↓
              ┌───────────────────────────────┐
              │   New Framework Components    │
              ├───────────────────────────────┤
              │  • Schema Validation          │
              │  • Condition Evaluation       │
              │  • Dependency Resolution      │
              │  • Merge Strategies           │
              └───────────────────────────────┘
```

### 1. JSON Schema Validation

All configuration files are automatically validated against defined schemas to catch errors early.

**Supported Config Files:**
- `replacements.yml` - File replacement rules
- `features.yml` - Feature flags and build properties
- `port_config.yml` - Port configuration settings

**Example Validation Error:**
```
✗ devices/target/DEVICE/replacements.yml
  - [1:5] Missing required field 'type'
  - [3:10] Unknown field 'condtion' (did you mean 'condition'?)
```

### 2. Composite Conditions

Replace simple boolean flags with powerful composite conditions using `and`, `or`, `not` operators.

#### Condition Types

| Condition | Description | Example |
|-----------|-------------|---------|
| `android_version` | Base Android version range | `{"min": 13, "max": 14}` |
| `port_android_version` | Port ROM Android version | `{"min": 15, "max": 15}` |
| `rom_type` | ROM type check | `"ColorOS"`, `"OxygenOS"` |
| `rom_version` | ROM version matching | `{"contains": "16.0.1"}` |
| `region` | Region check | `"CN"`, `"Global"` |
| `file_exists` | File existence check | `"path/to/file.zip"` |
| `target_exists` | Target path exists | `true` |

#### Legacy Format (Still Supported)
```json
{
  "condition_android_version": 13,
  "condition_port_is_coloros": true
}
```

#### New Composite Format
```json
{
  "condition": {
    "and": [
      {"rom_type": "ColorOS"},
      {"port_android_version": {"min": 15, "max": 15}},
      {"region": "CN"}
    ]
  }
}
```

#### Advanced Examples

**Multiple ROM Types (OR):**
```json
{
  "condition": {
    "or": [
      {"rom_type": "ColorOS"},
      {"rom_type": "OxygenOS"}
    ]
  }
}
```

**Exclude Region (NOT):**
```json
{
  "condition": {
    "and": [
      {"port_android_version": {"min": 16, "max": 16}},
      {"not": {"region": "CN"}}
    ]
  }
}
```

**Version Range:**
```json
{
  "condition": {
    "android_version": {"min": 13, "max": 14}
  }
}
```

### 3. Dependency Management

Rules can declare dependencies to ensure correct execution order.

```json
{
  "replacements": [
    {
      "id": "ril_fix_sm8350",
      "description": "RIL Fix for SM8350",
      "type": "unzip_override",
      "source": "devices/common/ril_fix_sm8350.zip"
    },
    {
      "id": "aon_fix_sm8350",
      "description": "AON Fix for SM8350",
      "type": "unzip_override",
      "source": "devices/common/aon_fix_sm8350.zip",
      "depends_on": ["ril_fix_sm8350"]
    }
  ]
}
```

**Benefits:**
- Automatic topological sorting
- Circular dependency detection
- Clear error messages

### 4. Merge Strategies

Control how configurations are merged across layers.

#### Strategies

| Strategy | Behavior | Use Case |
|----------|----------|----------|
| `append` (default) | Add new items, deduplicate | Most cases |
| `override` | Replace parent entirely | Device-specific override |
| `remove` | Remove from parent | Disable inherited rule |

#### Examples

**Override Parent Configuration:**
```json
{
  "replacements": [
    {
      "description": "Custom Camera Fix",
      "merge_strategy": "override",
      "type": "unzip_override",
      "source": "devices/target/MYDEVICE/camera_custom.zip"
    }
  ]
}
```

**Remove Inherited Rule:**
```json
{
  "replacements": [
    {
      "merge_strategy": "remove",
      "remove_by_description": "Stock NFC Fix"
    }
  ]
}
```

### 5. Complete Example

```json
{
  "replacements": [
    {
      "id": "camera_fix_group",
      "description": "Camera Fix for ColorOS 15",
      "type": "unzip_override_group",
      "condition": {
        "and": [
          {"rom_type": "ColorOS"},
          {"port_android_version": {"min": 15, "max": 15}},
          {"file_exists": "devices/target/MYDEVICE/camera_fix.zip"}
        ]
      },
      "operations": [
        {
          "id": "camera_framework",
          "description": "Camera Framework",
          "type": "unzip_override",
          "source": "devices/target/MYDEVICE/camera_fix.zip",
          "target_base_dir": "build/target/",
          "removes": [
            "my_product/app/OplusCamera",
            "my_product/product_overlay/framework/com.oplus.camera.*.jar"
          ],
          "build_props": {
            "my_product": {
              "ro.vendor.oplus.camera.isSupportLumo": "1"
            }
          }
        },
        {
          "id": "camera_odm",
          "description": "ODM Files",
          "type": "unzip_override",
          "source": "devices/target/MYDEVICE/camera_odm.zip",
          "target_base_dir": "build/target/"
        }
      ]
    }
  ]
}
```

### 6. Validation and Debugging

#### Validate All Configs
```bash
python3 -c "
from src.core.config_schema import validate_all_configs
results = validate_all_configs('devices')
for path, (valid, errors) in results.items():
    status = '✓' if valid else '✗'
    print(f'{status} {path}')
"
```

#### Test Condition Evaluation
```bash
python3 -c "
from src.core.conditions import ConditionEvaluator, BuildContext
evaluator = ConditionEvaluator()
ctx = BuildContext()
ctx.base_android_version = 13
ctx.portIsColorOS = True

rule = {'condition': {'android_version': {'min': 13, 'max': 13}}}
print(f'Condition passes: {evaluator.evaluate(rule, ctx)}')
"
```

#### Merge Report
The framework generates detailed reports during config loading:
```
[INFO] Config 'replacements.yml' loaded from 3 layer(s)
[DEBUG] Config 'replacements.yml' missing (expected): 0 file(s)
[INFO] Applied 12 override rules, skipped 5
```

---

## 🔧 Configuration-Driven Property Modification

The property modification system (`PropertyModifier`) uses a configuration-driven architecture with pluggable strategies. Instead of hardcoding modification logic in Python, you define rules in `devices/common/props.yml`.

### Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│              Property Modification System                    │
├─────────────────────────────────────────────────────────────┤
│  Configuration File (props.yml)                             │
│       ↓                                                      │
│  Strategy Registry                                           │
│       ↓                                                      │
│  Priority-based Execution                                    │
│       ↓                                                      │
│  Target build.prop Files                                     │
└─────────────────────────────────────────────────────────────┘
```

### Performance Optimizations

The property modification system includes several performance optimizations:

| Optimization | Description | Impact |
|--------------|-------------|---------|
| **File Caching** | `PropCache` class with `lru_cache` for build.prop file paths and contents | Reduces file system scans from O(n) to O(1) |
| **Batch Processing** | Group properties by target file for batch updates | Reduces file I/O operations |
| **Baserom Property Cache** | Pre-load all baserom properties into memory | Eliminates repeated file searches |
| **Exclusion Patterns** | Skip system_dlkm/odm_dlkm directories | Reduces unnecessary processing |
| **Error Recovery** | Continue processing other files if one fails | Improves reliability |

### Enhanced Error Handling

All strategies now include comprehensive error handling:

- **Unified Exception Handling**: All strategies catch and log exceptions gracefully
- **Detailed Debug Logging**: Condition checks and operations are fully logged
- **Operation Statistics**: Track number of modified files and properties
- **Context Value Error Recovery**: Return None instead of crashing on missing attributes

### Configuration Validation

Validate your props.yml configuration:

```bash
python3 -c "
from src.core.config_schema import validate_config
valid, errors = validate_config('devices/common/props.yml')
if valid:
    print('✓ Configuration is valid')
else:
    for error in errors:
        print(f'✗ {error}')
"
```

Validation includes:
- Version field check
- Strategy type validation (string_replace, prop_set, prop_copy, watermark, fingerprint)
- Config structure validation per strategy type
- Property definition validation (key, value/source/template)

### Built-in Strategies

| Strategy | Function | Description |
|----------|----------|-------------|
| `string_replace` | Global Replacement | Replace device code, model, name strings |
| `prop_set` | Property Setting | Set properties from static values, templates, or context |
| `prop_copy` | Property Copy | Copy critical properties from baserom |
| `watermark` | Version Watermark | Add "Ported By" watermark |
| `fingerprint` | Fingerprint | Regenerate build fingerprint |

### Configuration Format

Rules are grouped by **function**, not by individual property:

```json
{
  "version": 2,
  "rules": [
    {
      "name": "string_replace",
      "enabled": true,
      "priority": 10,
      "config": {
        "mappings": [
          {"from": "port_device_code", "to": "base_device_code"},
          {"from": "port_product_model", "to": "base_product_model"}
        ]
      }
    },
    {
      "name": "prop_set",
      "enabled": true,
      "priority": 20,
      "config": {
        "properties": [
          {"key": "persist.sys.timezone", "value": "Asia/Shanghai"},
          {"key": "ro.build.display.id", "source": "target_display_id"},
          {"key": "ro.sf.lcd_density", "source": "base_lcd_density", "target_partition": "my_product"},
          {"key": "persist.oplus.prophook.ai.magicstudio", "template": "MODEL:{device_code},BRAND:{product_model}"}
        ]
      }
    },
    {
      "name": "prop_copy",
      "enabled": true,
      "priority": 25,
      "config": {
        "properties": [
          {
            "key": "ro.product.first_api_level",
            "to_partition": "my_manifest",
            "comment": "Critical for boot"
          }
        ]
      }
    }
  ]
}
```

### Conditions

Strategies support conditional execution using comparison operators:

| Operator | Suffix | Example |
|----------|--------|---------|
| Less Than | `_lt` | `"port_android_version_lt": 16` |
| Less Than or Equal | `_lte` | `"base_android_version_lte": 14` |
| Greater Than | `_gt` | `"port_android_version_gt": 14` |
| Greater Than or Equal | `_gte` | `"base_android_version_gte": 13` |
| Not Equal | `_ne` | `"region_ne": "CN"` |

### Context Variables

Available in configuration mappings and templates:

**Port ROM Properties:**
- `port_device_code`, `port_product_model`, `port_product_name`
- `port_product_device`, `port_vendor_device`, `port_vendor_model`
- `port_vendor_brand`, `port_android_version`, `port_is_coloros_global`

**Base ROM Properties:**
- `base_device_code`, `base_product_model`, `base_product_name`
- `base_product_device`, `base_vendor_device`, `base_vendor_model`
- `base_vendor_brand`, `base_market_name`, `base_market_enname`
- `base_lcd_density`

**Target Properties:**
- `target_display_id`

### Custom Strategy Example

Create a custom strategy by implementing the `PropStrategy` class:

```python
from src.core.prop_strategies import PropStrategy, STRATEGY_REGISTRY

class MyCustomStrategy(PropStrategy):
    def apply(self, target_dir: Path) -> bool:
        # Your modification logic here
        key = self.config["config"]["key"]
        value = self._get_context_value("base_device_code")
        # ... modify build.prop files
        return True
    
    def check_condition(self) -> bool:
        # Optional: custom condition logic
        return super().check_condition()

# Register the strategy
STRATEGY_REGISTRY["my_custom"] = MyCustomStrategy
```

Then use it in `props.yml`:

```json
{
  "strategies": [
    {
      "name": "my_custom",
      "enabled": true,
      "priority": 75,
      "config": {
        "key": "ro.custom.property",
        "value": "custom_value"
      }
    }
  ]
}
```

### Utility Functions

The `prop_utils.py` module provides utility functions for property manipulation:

```python
from src.core.prop_utils import (
    PropCache,                    # File caching for performance
    update_or_append_prop,        # Update or append single property
    read_prop_value,             # Read single property value
    batch_update_props,          # Batch update multiple properties
    read_prop_to_dict            # Read entire file as dict
)

# Example: Using PropCache
cache = PropCache(Path("build/target"))
prop_files = cache.get_all_prop_files(exclude_patterns=("system_dlkm",))

# Example: Batch update
updates = {
    "ro.build.display.id": "ColorOS 15",
    "ro.product.model": "OP8T"
}
batch_update_props(Path("build/target/system/build.prop"), updates)
```

### Hierarchical Configuration

Like other config files, `props.yml` follows the three-layer inheritance:

1. **Common** (`devices/common/props.yml`): Default strategies for all devices
2. **Chipset** (`devices/chipset/<FAMILY>/props.yml`): Chipset-specific overrides
3. **Target** (`devices/target/<DEVICE>/props.yml`): Device-specific overrides

Example device-specific override:

```json
{
  "strategies": [
    {
      "name": "watermark",
      "config": {
        "template": "{value} | Ported by YourName"
      }
    }
  ]
}
```

---

## ⚠️ Disclaimer

The binary tools included in this project are for the **Linux x86_64** architecture only. The author is not responsible for any damage to your device.

## 📄 License

This project is licensed under the MIT License.
