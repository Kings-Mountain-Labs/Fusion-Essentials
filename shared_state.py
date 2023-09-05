#  Copyright 2023 by Ian Rist

import json
import os
import adsk.core
import os
import platform
from . import config

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

def load_settings(module_name):
    all_settings = {}
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as file:
            all_settings = json.load(file)
    return all_settings.get(module_name, {})

def load_settings_init(module_name, default_settings):
    all_settings = {}
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as file:
            all_settings = json.load(file)
    if module_name not in all_settings.keys():
        all_settings[module_name] = default_settings
    else:
        merge_settings(default_settings, all_settings[module_name])
    save_settings(module_name, all_settings[module_name])

def save_settings(module_name, settings):
    all_settings = {}
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as file:
            all_settings = json.load(file)

    all_settings[module_name] = settings

    with open(SETTINGS_FILE, 'w') as file:
        json.dump(all_settings, file, indent=4)

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