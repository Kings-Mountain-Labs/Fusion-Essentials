from .updateDocSettings import entry as updateDocSettings

commands = [
    updateDocSettings
]

def start():
    for command in commands:
        command.start()

def stop():
    for command in commands:
        command.stop()