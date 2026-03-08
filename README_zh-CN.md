<div align="center">

# ColorOS 移植工具 (Python 版)

一个基于 Python 的 ColorOS 移植工具，由 AI 大模型 (Gemini, Qwen 等) 创作。

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

## ✨ 特性

- **上下文感知架构**: 使用 `Context` 对象管理整个移植生命周期。
- **模块化设计**: 将不同功能分离到独立的模块中 (`rom`, `props`, `patcher`, `packer`)。
- **配置驱动**: 使用 JSON 配置文件进行设备特定的设置。
- **自动化修补**:
    - `PropertyModifier`: 自动同步底包和移植包之间的属性。
    - `SmaliPatcher`: 反编译并修补 `services.jar` 和 `framework.jar`。
- **高级打包**: 支持将分区打包为 EROFS 或 EXT4，并生成 `super.img` 或 OTA `payload.bin`。
- **增强配置框架**: 新增验证、灵活条件和依赖管理功能。

## 📱 兼容机型

本工具理论上支持 **SM8250** 及更高版本的高通骁龙芯片。

**目前预计支持的机型包括：**
-   **一加 SM8250 系列**: 一加 8, 一加 8 Pro, 一加 8T
-   **OPPO Find X3** (SM8350)
-   **一加 SM8350 系列**: 一加 9, 一加 9 Pro

**ColorOS 16 重要提示：**
ColorOS 16 需要特定的内核支持。如果尝试移植 ColorOS 16，请确保你的设备内核兼容。

## 🚀 开始使用

本节将指导你如何设置并运行此工具。

### 先决条件

- **操作系统**: Linux 发行版 (如 Ubuntu, Arch)，x86_64 架构。
- **Python**: Python 3.10 或更高版本。
- **Java**: Java Development Kit (JDK) 11 或更高版本。
- **Docker**: (推荐) 用于无痛安装。

### 方案一：使用 Docker 部署 (推荐)

我们推荐使用 Docker 来运行此工具。它会创建一个包含所有必需依赖项的自给自足的环境。

1.  **构建 Docker 镜像:**
    ```bash
    docker build -t coloros-port .
    ```

2.  **运行容器:**
    通过挂载本地文件夹，使容器可以访问你的 ROM 文件并将输出写回你的电脑。
    ```bash
    # 示例:
    docker run --rm -it \
      -v /path/to/your/roms:/roms \
      -v $(pwd)/build:/app/build \
      coloros-port \
      python3 main.py --baserom /roms/base_rom.zip --portrom /roms/port_rom.zip
    ```
    - **请记得将 `/path/to/your/roms` 替换为你电脑上的实际路径。**
    - 输出文件将位于你主机的 `build` 目录中。

### 方案二：手动设置

1.  **克隆仓库:**
    ```bash
    git clone https://github.com/toraidl/ColorOS-Port-Python.git
    cd ColorOS-Port-Python
    ```

2.  **设置文件权限:**
    ```bash
    chmod +x -R bin/linux/x86_64/
    ```

3.  **运行脚本:**
    - **基本用法:**
        ```bash
        python3 main.py --baserom <path/to/base.zip> --portrom <path/to/port.zip>
        ```
    - **高级用法 (带参数):**
        ```bash
        # 指定设备代号，打包类型，并启用调试日志
        python3 main.py \
          --baserom <path/to/base.zip> \
          --portrom <path/to/port.zip> \
          --device_code OP4E7L1 \
          --pack_type super \
          --debug
        ```
    - 默认情况下，输出文件将位于 `build` 目录中。

## 🛠️ 分层配置系统

该项目使用强大的三层继承系统进行 ROM 修改，从而轻松实现扩展和多设备支持，无需重复逻辑。修改按以下顺序加载和合并：

1.  **通用层 (`devices/common/`)**: 适用于所有设备的全局补丁。
2.  **芯片组层 (`devices/chipset/<FAMILY>/`)**: 特定于芯片组的修改。
3.  **目标层 (`devices/target/<DEVICE>/`)**: 特定于设备的硬件补丁。

> 更多示例，如 `features.yml` 和 `replacements.yml`，请参阅 `devices` 目录。

---

## 📖 增强配置框架指南

新框架提供了强大的功能，用于定义设备特定的修改，支持验证、灵活的条件和依赖管理。

### 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                      配置层级                                 │
├─────────────────────────────────────────────────────────────┤
│  通用层    →    芯片组层    →    目标层                      │
│  (所有设备)    (SM8250/8350)   (特定设备)                    │
└─────────────────────────────────────────────────────────────┘
         ↓                    ↓                      ↓
         └────────────────────┴──────────────────────┘
                              ↓
                       合并后的配置
                              ↓
              ┌───────────────────────────────┐
              │      新框架组件                 │
              ├───────────────────────────────┤
              │  • Schema 验证                 │
              │  • 条件评估                    │
              │  • 依赖解析                    │
              │  • 合并策略                    │
              └───────────────────────────────┘
```

### 1. JSON Schema 验证

所有配置文件都会根据定义的 schema 自动验证，及早发现错误。

**支持的配置文件:**
- `replacements.yml` - 文件替换规则
- `features.yml` - 功能标志和构建属性
- `port_config.yml` - 移植配置设置

**验证错误示例:**
```
✗ devices/target/DEVICE/replacements.yml
  - [1:5] 缺少必需字段 'type'
  - [3:10] 未知字段 'condtion' (你是不是想说 'condition'?)
```

### 2. 复合条件

使用 `and`、`or`、`not` 运算符，将简单的布尔标志替换为强大的复合条件。

#### 条件类型

| 条件 | 说明 | 示例 |
|------|------|------|
| `android_version` | 基础 Android 版本范围 | `{"min": 13, "max": 14}` |
| `port_android_version` | 移植包 Android 版本 | `{"min": 15, "max": 15}` |
| `rom_type` | ROM 类型检查 | `"ColorOS"`, `"OxygenOS"` |
| `rom_version` | ROM 版本匹配 | `{"contains": "16.0.1"}` |
| `region` | 区域检查 | `"CN"`, `"Global"` |
| `file_exists` | 文件存在检查 | `"path/to/file.zip"` |
| `target_exists` | 目标路径存在 | `true` |

#### 旧格式 (仍然支持)
```json
{
  "condition_android_version": 13,
  "condition_port_is_coloros": true
}
```

#### 新复合格式
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

#### 高级示例

**多 ROM 类型 (OR):**
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

**排除区域 (NOT):**
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

**版本范围:**
```json
{
  "condition": {
    "android_version": {"min": 13, "max": 14}
  }
}
```

### 3. 依赖管理

规则可以声明依赖关系以确保正确的执行顺序。

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

**优势:**
- 自动拓扑排序
- 循环依赖检测
- 清晰的错误消息

### 4. 合并策略

控制配置在各层之间的合并方式。

#### 策略

| 策略 | 行为 | 使用场景 |
|------|------|----------|
| `append` (默认) | 添加新项目，去重 | 大多数情况 |
| `override` | 完全替换父配置 | 设备特定覆盖 |
| `remove` | 从父配置删除 | 禁用继承的规则 |

#### 示例

**覆盖父配置:**
```json
{
  "replacements": [
    {
      "description": "自定义相机修复",
      "merge_strategy": "override",
      "type": "unzip_override",
      "source": "devices/target/MYDEVICE/camera_custom.zip"
    }
  ]
}
```

**删除继承的规则:**
```json
{
  "replacements": [
    {
      "merge_strategy": "remove",
      "remove_by_description": "原版 NFC 修复"
    }
  ]
}
```

### 5. 完整示例

```json
{
  "replacements": [
    {
      "id": "camera_fix_group",
      "description": "ColorOS 15 相机修复",
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
          "description": "相机框架",
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
          "description": "ODM 文件",
          "type": "unzip_override",
          "source": "devices/target/MYDEVICE/camera_odm.zip",
          "target_base_dir": "build/target/"
        }
      ]
    }
  ]
}
```

### 6. 验证和调试

#### 验证所有配置
```bash
python3 -c "
from src.core.config_schema import validate_all_configs
results = validate_all_configs('devices')
for path, (valid, errors) in results.items():
    status = '✓' if valid else '✗'
    print(f'{status} {path}')
"
```

#### 测试条件评估
```bash
python3 -c "
from src.core.conditions import ConditionEvaluator, BuildContext
evaluator = ConditionEvaluator()
ctx = BuildContext()
ctx.base_android_version = 13
ctx.portIsColorOS = True

rule = {'condition': {'android_version': {'min': 13, 'max': 13}}}
print(f'条件通过：{evaluator.evaluate(rule, ctx)}')
"
```

#### 合并报告
框架在配置加载期间生成详细报告：
```
[INFO] Config 'replacements.yml' loaded from 3 layer(s)
[DEBUG] Config 'replacements.yml' missing (expected): 0 file(s)
[INFO] Applied 12 override rules, skipped 5
```

---

## 🔧 配置驱动的属性修改

属性修改系统 (`PropertyModifier`) 使用配置驱动的架构和可插拔策略。你可以在 `devices/common/props.yml` 中定义规则，而不是在 Python 代码中硬编码修改逻辑。

### 架构概览

```
┌─────────────────────────────────────────────────────────────┐
│                    属性修改系统                               │
├─────────────────────────────────────────────────────────────┤
│  配置文件 (props.yml)                                        │
│       ↓                                                       │
│  策略注册表                                                    │
│       ↓                                                       │
│  基于优先级的执行                                               │
│       ↓                                                       │
│  目标 build.prop 文件                                          │
└─────────────────────────────────────────────────────────────┘
```

### 内置策略

| 策略 | 功能 | 说明 |
|------|------|------|
| `string_replace` | 全局替换 | 替换设备代码、型号、名称等字符串 |
| `prop_set` | 属性设置 | 从静态值、模板或上下文设置属性 |
| `prop_copy` | 属性复制 | 从底包复制关键属性 |
| `watermark` | 版本水印 | 添加 "Ported By" 水印 |
| `fingerprint` | 指纹生成 | 重新生成构建指纹 |

### 配置格式

规则按**功能**分组，而不是按单个属性：

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

### 条件

策略支持使用比较运算符进行条件执行：

| 运算符 | 后缀 | 示例 |
|--------|------|------|
| 小于 | `_lt` | `"port_android_version_lt": 16` |
| 小于等于 | `_lte` | `"base_android_version_lte": 14` |
| 大于 | `_gt` | `"port_android_version_gt": 14` |
| 大于等于 | `_gte` | `"base_android_version_gte": 13` |
| 不等于 | `_ne` | `"region_ne": "CN"` |

### 上下文变量

在配置映射和模板中可用：

**移植包 ROM 属性：**
- `port_device_code`, `port_product_model`, `port_product_name`
- `port_product_device`, `port_vendor_device`, `port_vendor_model`
- `port_vendor_brand`, `port_android_version`, `port_is_coloros_global`

**底包 ROM 属性：**
- `base_device_code`, `base_product_model`, `base_product_name`
- `base_product_device`, `base_vendor_device`, `base_vendor_model`
- `base_vendor_brand`, `base_market_name`, `base_market_enname`
- `base_lcd_density`

**目标属性：**
- `target_display_id`

### 自定义策略示例

通过实现 `PropStrategy` 类创建自定义策略：

```python
from src.core.prop_strategies import PropStrategy, STRATEGY_REGISTRY

class MyCustomStrategy(PropStrategy):
    def apply(self, target_dir: Path) -> bool:
        # 你的修改逻辑在这里
        key = self.config["config"]["key"]
        value = self._get_context_value("base_device_code")
        # ... 修改 build.prop 文件
        return True
    
    def check_condition(self) -> bool:
        # 可选：自定义条件逻辑
        return super().check_condition()

# 注册策略
STRATEGY_REGISTRY["my_custom"] = MyCustomStrategy
```

然后在 `props.yml` 中使用它：

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

### 分层配置

与其他配置文件一样，`props.yml` 遵循三层继承：

1. **通用层** (`devices/common/props.yml`): 所有设备的默认策略
2. **芯片组层** (`devices/chipset/<FAMILY>/props.yml`): 芯片组特定覆盖
3. **目标层** (`devices/target/<DEVICE>/props.yml`): 设备特定覆盖

设备特定覆盖示例：

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

## ⚠️ 免责声明

本项目中包含的二进制工具仅适用于 **Linux x86_64** 架构。作者对你的设备可能发生的任何损坏概不负责。

## 📄 许可证

本项目基于 MIT 许可证授权。
