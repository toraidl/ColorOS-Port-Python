"""
Base Handler Module for ColorOS Porting Tool

Provides abstract base class for all feature handlers.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, List, Optional
from pathlib import Path
import logging


class BaseHandler(ABC):
    """
    Abstract base class for all feature handlers.

    All handlers must inherit from this class and implement
    the required abstract methods.
    """

    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)

    @abstractmethod
    def can_handle(self, config: Dict[str, Any]) -> bool:
        """
        Check if this handler can process the given configuration.

        Args:
            config: Configuration dictionary from features.yml

        Returns:
            True if this handler can process the config, False otherwise
        """
        pass

    @abstractmethod
    def apply(self, config: Dict[str, Any], context: Any) -> None:
        """
        Apply the configuration to the target system.

        Args:
            config: Configuration dictionary
            context: BuildContext object containing target_dir and other info
        """
        pass

    @abstractmethod
    def validate(self, config: Dict[str, Any]) -> List[str]:
        """
        Validate the configuration.

        Args:
            config: Configuration dictionary to validate

        Returns:
            List of error messages. Empty list if validation passes.
        """
        pass

    def get_config_value(
        self, config: Dict[str, Any], key: str, default: Any = None
    ) -> Any:
        """
        Safely get a value from config with default.

        Args:
            config: Configuration dictionary
            key: Key to look up
            default: Default value if key not found

        Returns:
            Value from config or default
        """
        return config.get(key, default)
