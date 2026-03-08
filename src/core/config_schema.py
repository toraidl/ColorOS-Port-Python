"""
简化版配置验证模块
"""

import yaml
from pathlib import Path
from typing import Dict, List, Tuple, Any, Optional


def validate_replacements(data: Dict) -> List[str]:
    """验证 replacements.yml"""
    errors = []
    if "replacements" not in data:
        errors.append("Missing required field 'replacements'")
        return errors

    for i, rule in enumerate(data["replacements"]):
        if "description" not in rule:
            errors.append(f"Rule [{i}]: Missing 'description'")
        if "type" not in rule:
            errors.append(f"Rule [{i}]: Missing 'type'")
    return errors


def validate_features(data: Dict) -> List[str]:
    """验证 features.yml"""
    errors = []
    valid_keys = {
        "oplus_feature",
        "app_feature",
        "permission_feature",
        "permission_oplus_feature",
        "features_remove",
        "features_remove_force",
        "features_remove_conditional",
        "xml_features",
        "build_props",
        "props_remove",
        "props_add",
    }
    for key in data.keys():
        if key not in valid_keys:
            errors.append(f"Unknown field: '{key}'")
    return errors


def validate_props(data: Dict) -> List[str]:
    """验证 props.yml"""
    errors = []

    # Check version
    if "version" not in data:
        errors.append("Missing required field 'version'")

    # Validate rules
    rules = data.get("rules", data.get("strategies", []))
    if not isinstance(rules, list):
        errors.append("'rules' must be a list")
        return errors

    valid_strategies = {
        "string_replace",
        "prop_set",
        "prop_copy",
        "watermark",
        "fingerprint",
    }

    for i, rule in enumerate(rules):
        rule_prefix = f"Rule [{i}]"

        if "name" not in rule:
            errors.append(f"{rule_prefix}: Missing 'name'")
            continue

        name = rule["name"]
        if name not in valid_strategies:
            errors.append(f"{rule_prefix}: Unknown strategy '{name}'")

        # Validate config based on strategy type
        config = rule.get("config", {})

        if name == "string_replace":
            if "mappings" not in config:
                errors.append(f"{rule_prefix}: 'string_replace' requires 'mappings'")
            elif not isinstance(config["mappings"], list):
                errors.append(f"{rule_prefix}: 'mappings' must be a list")

        elif name == "prop_set":
            if "properties" not in config:
                errors.append(f"{rule_prefix}: 'prop_set' requires 'properties'")
            elif not isinstance(config["properties"], list):
                errors.append(f"{rule_prefix}: 'properties' must be a list")
            else:
                for j, prop in enumerate(config["properties"]):
                    if "key" not in prop:
                        errors.append(f"{rule_prefix}: Property [{j}] missing 'key'")
                    if not any(k in prop for k in ["value", "source", "template"]):
                        errors.append(
                            f"{rule_prefix}: Property [{j}] must have 'value', 'source', or 'template'"
                        )

        elif name == "prop_copy":
            if "properties" not in config:
                errors.append(f"{rule_prefix}: 'prop_copy' requires 'properties'")
            elif not isinstance(config["properties"], list):
                errors.append(f"{rule_prefix}: 'properties' must be a list")
            else:
                for j, prop in enumerate(config["properties"]):
                    if "key" not in prop:
                        errors.append(f"{rule_prefix}: Property [{j}] missing 'key'")

        elif name == "watermark":
            required = ["target_key", "template"]
            for field in required:
                if field not in config:
                    errors.append(f"{rule_prefix}: 'watermark' requires '{field}'")

    return errors


def validate_port_config(data: Dict) -> List[str]:
    """验证 port_config.yml"""
    errors = []
    required = ["partition_to_port", "possible_super_list"]
    for field in required:
        if field not in data:
            errors.append(f"Missing required field: '{field}'")
    return errors


def validate_config(config_path: str) -> Tuple[bool, List[str]]:
    """验证单个配置文件"""
    path = Path(config_path)
    if not path.exists():
        return False, [f"File not found: {config_path}"]

    try:
        with open(path, "r", encoding='utf-8') as f:
            if path.suffix == '.json':
                import json
                data = json.load(f)
            else:
                data = yaml.safe_load(f)
    except Exception as e:
        return False, [f"Invalid config format: {e}"]

    filename = path.name
    errors = []

    if filename in ["replacements.yml", "replacements.yml"]:
        errors = validate_replacements(data)
    elif filename in ["features.yml", "features.yml"]:
        errors = validate_features(data)
    elif filename in ["port_config.yml", "port_config.yml"]:
        errors = validate_port_config(data)
    elif filename in ["props.yml", "props.yml"]:
        errors = validate_props(data)
    else:
        return True, []

    return len(errors) == 0, errors


def validate_all_configs(
    base_dir: str = "devices",
) -> Dict[str, Tuple[bool, List[str]]]:
    """验证所有配置文件"""
    results = {}
    base = Path(base_dir)

    patterns = [
        "**/replacements.yml",
        "**/features.yml",
        "**/port_config.yml",
        "**/props.yml",
        "**/replacements.yml",
        "**/features.yml",
        "**/port_config.yml",
        "**/props.yml",
    ]
    
    for pattern in patterns:
        for config_file in base.glob(pattern):
            is_valid, errors = validate_config(str(config_file))
            results[str(config_file)] = (is_valid, errors)

    return results
