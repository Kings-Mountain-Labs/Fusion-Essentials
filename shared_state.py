#  Copyright 2023 by Ian Rist

import json
import os
import adsk.core
import os
import platform
import tempfile
from . import config
from .lib import fusion360utils as futil

def get_settings_directory():
    user_home = os.path.expanduser("~")

    if platform.system() == "Windows":
        # On Windows, you might want to use the AppData folder.
        return os.path.join(user_home, 'AppData', 'Roaming', f'{config.COMPANY_NAME}_{config.ADDIN_NAME}')

    elif platform.system() == "Darwin":
        # On MacOS, use the Application Support directory.
        return os.path.join(user_home, 'Library', 'Application Support', f'{config.COMPANY_NAME}_{config.ADDIN_NAME}')

    else:
        # For Linux or other platforms, you can just use a directory in the home folder.
        return os.path.join(user_home, f'.{config.COMPANY_NAME}_{config.ADDIN_NAME}')

# Ensure the directory exists.
settings_dir = get_settings_directory()
if not os.path.exists(settings_dir):
    os.makedirs(settings_dir)

SETTINGS_FILE = os.path.join(settings_dir, 'FusionEssentialsSettings.json')

DEFAULT_ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'commands', 'resources', 'default', '')

def load_settings(module_name):
    all_settings = {}
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as file:
            all_settings = json.load(file)
    return all_settings.get(module_name, {})["settings"]

def load_settings_init(module_id, module_name, default_settings, img_path):
    all_settings = {}
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as file:
            all_settings = json.load(file)
    if module_id not in all_settings.keys():
        all_settings[module_id] = {}
        all_settings[module_id]["name"] = module_name
        all_settings[module_id]["settings"] = default_settings
        if img_path:
            all_settings[module_id]["img_path"] = img_path
        else:
            all_settings[module_id]["img_path"] = DEFAULT_ICON
    else:
        if "settings" not in all_settings[module_id]: # for migration to the new format
            tmp = all_settings[module_id]
            all_settings[module_id] = {}
            all_settings[module_id]["name"] = module_name
            all_settings[module_id]["settings"] = tmp
            if img_path:
                all_settings[module_id]["img_path"] = img_path
            else:
                all_settings[module_id]["img_path"] = DEFAULT_ICON
        merge_settings(default_settings, all_settings[module_id]["settings"])

    _write_settings(all_settings)

def save_settings(module_id, settings):
    all_settings = {}
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as file:
            all_settings = json.load(file)

    all_settings[module_id]["settings"] = settings

    _write_settings(all_settings)

def _write_settings(all_settings):
    """Replace settings after the complete JSON is written and synced."""
    serialized = json.dumps(all_settings, indent=4)
    temp_fd, temp_path = tempfile.mkstemp(
        prefix='.FusionEssentials-', suffix='.tmp', dir=os.path.dirname(SETTINGS_FILE))
    try:
        with os.fdopen(temp_fd, 'w', encoding='utf-8') as file:
            temp_fd = None
            file.write(serialized)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, SETTINGS_FILE)
    except BaseException as failure:
        for cleanup, value in ((os.close, temp_fd), (os.unlink, temp_path)):
            if value is None:
                continue
            try:
                cleanup(value)
            except OSError as cleanup_error:
                failure.add_note(f"Settings temporary cleanup failed for {temp_path}: "
                                 f"{cleanup_error}")
        raise

def get_all_module_settings():
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as file:
            return json.load(file)
    return {}

def merge_settings(default_settings, user_settings):
    # Iterate over default settings
    for key, value in default_settings.items():
        # If the setting is not in user_settings, add it
        if key not in user_settings:
            user_settings[key] = value
        # If the value itself is a dictionary, then recurse
        elif isinstance(value, dict) and isinstance(user_settings[key], dict):
            merge_settings(value, user_settings[key])
    return user_settings