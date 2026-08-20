"""
Blueprint package with automatic module registration.
All blueprints should be defined as 'bp' or '*_bp' variables in their respective modules.
"""
from utils.module_loader import load_modules, get_modules_info

__all__ = ['load_modules', 'get_modules_info']
