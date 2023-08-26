from .updateDocSettings import entry as updateDocSettings
from .cleanChamfer import entry as cleanChamfer

commands = [
    updateDocSettings,
    cleanChamfer
]

def start():
    for command in commands:
        command.start()

def stop():
    for command in commands:
        command.stop()