# Assuming you have not changed the general structure of the template no modification is needed in this file.
import os

from .lib import loaded_attestation

loaded_attestation.begin(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     'commands', 'mcpServer'))
try:
    from . import commands
finally:
    loaded_attestation.finish()
from .lib import fusion360utils as futil


def run(context):
    try:
        # This will run the start function in each of your commands as defined in commands/__init__.py
        commands.start()

    except:
        futil.handle_error('run')


def stop(context):
    try:
        # Remove all of the event handlers your app has created
        futil.clear_handlers()

        # This will run the start function in each of your commands as defined in commands/__init__.py
        commands.stop()

    except:
        futil.handle_error('stop')