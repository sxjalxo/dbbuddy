"""Dynamic plugin loader for mapping plugins"""
import importlib
import logging

logger = logging.getLogger(__name__)


def load_mapping_plugin(plugin_name: str):
    """
    Dynamically load a mapping plugin by name.
    
    Args:
        plugin_name: Name of the plugin module (e.g., "default_mapping")
        
    Returns:
        An instance of the loaded plugin class, or DefaultMapping as fallback
    """
    try:
        module_path = f"dbbuddy.plugins.{plugin_name}"
        module = importlib.import_module(module_path)

        # Look for explicit Plugin class (enforced naming convention)
        if hasattr(module, 'Plugin'):
            from dbbuddy.plugins.base import MappingPlugin
            Plugin = getattr(module, 'Plugin')
            if isinstance(Plugin, type) and issubclass(Plugin, MappingPlugin):
                return Plugin()

        # Fallback to scanning for MappingPlugin subclasses (for backward compatibility)
        from dbbuddy.plugins.base import MappingPlugin
        for attr in dir(module):
            obj = getattr(module, attr)
            if isinstance(obj, type):
                if issubclass(obj, MappingPlugin) and obj is not MappingPlugin:
                    return obj()

    except Exception as e:
        logger.warning(f"Failed to load plugin '{plugin_name}': {str(e)}")

    # Fallback to default mapping
    logger.warning(f"Plugin '{plugin_name}' not found. Using default_mapping.")
    from dbbuddy.plugins.default_mapping import Plugin
    return Plugin()
