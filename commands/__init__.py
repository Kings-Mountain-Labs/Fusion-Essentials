from .updateDocSettings import entry as updateDocSettings
from .cleanChamfer import entry as cleanChamfer
from .genPanels import entry as genPanels
from .addHolder import entry as addHolder
from .colorHoles import entry as colorHoles
from .settings import entry as settings

commands = [
    updateDocSettings,
    cleanChamfer,
    addHolder,
    colorHoles,
    settings
]

def start():
    genPanels.start() # we need to make the panels that we are going to use first
    for command in commands:
        command.start()

def stop():
    for command in commands:
        command.stop()
    genPanels.stop() # we need to delete the panels last