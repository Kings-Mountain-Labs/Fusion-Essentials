from .genPanels import entry as genPanels
from .settings import entry as settings
from .. import shared_state
import os

from .updateDocSettings import entry as updateDocSettings
from .cleanChamfer import entry as cleanChamfer
from .addHolder import entry as addHolder
from .colorHoles import entry as colorHoles
from .updateTools import entry as updateTools

commands = [
    updateDocSettings,
    cleanChamfer,
    addHolder,
    colorHoles,
    updateTools
]

default_settings: dict = {}
template_en = {
    "type": "checkbox",
    "label": f"Enable ",
    "default": True
}
for command in commands:
    default_settings[command.CMD_ID] = template_en.copy()
    default_settings[command.CMD_ID]["label"] += command.CMD_NAME

ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', 'settings', '')

shared_state.load_settings_init("FEATURE_ENABLEMENT", "Settings", default_settings, ICON_FOLDER)

def start():
    genPanels.start() # we need to make the panels that we are going to use first
    settings.start(commands)

def stop():
    for command in commands:
        command.stop()
    settings.stop()
    genPanels.stop() # we need to delete the panels last