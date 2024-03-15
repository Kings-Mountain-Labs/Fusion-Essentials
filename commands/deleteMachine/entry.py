import json
import adsk.core, adsk.fusion, adsk.cam, traceback
import os
from ...lib import fusion360utils as futil
from ... import config
import time
import random
from typing import List, Dict
import math
from adsk.cam import ToolLibrary, Tool
from hashlib import sha256

app = adsk.core.Application.get()
ui: adsk.core.UserInterface = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_{config.ADDIN_NAME}_Delete_Machines'
CMD_NAME = 'Delete Machines'
CMD_Description = 'Delete the machine out of all of the setups'
IS_PROMOTED = True

WORKSPACE_ID = 'CAMEnvironment'
PANEL_ID = 'CAMManagePanel'
COMMAND_BESIDE_ID = ''

ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

# Local list of event handlers used to maintain a reference so
# they are not released and garbage collected.
local_handlers = []

def start():
    cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_Description, ICON_FOLDER)
    futil.add_handler(cmd_def.commandCreated, command_created)
    workspace = ui.workspaces.itemById(WORKSPACE_ID)
    panel = workspace.toolbarPanels.itemById(PANEL_ID)
    control = panel.controls.addCommand(cmd_def, COMMAND_BESIDE_ID, False)
    control.isPromoted = IS_PROMOTED

def stop():
    # Get the various UI elements for this command
    workspace = ui.workspaces.itemById(WORKSPACE_ID)
    panel = workspace.toolbarPanels.itemById(PANEL_ID)
    command_control = panel.controls.itemById(CMD_ID)
    command_definition = ui.commandDefinitions.itemById(CMD_ID)

    if command_control:
        command_control.deleteMe()

    if command_definition:
        command_definition.deleteMe()

def command_created(args: adsk.core.CommandCreatedEventArgs):
    # General logging for debug.
    futil.log(f'{CMD_NAME} Command Created Event')
    futil.add_handler(args.command.execute, command_execute, local_handlers=local_handlers)
    futil.add_handler(args.command.preSelect, command_preselect, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)

    inputs = args.command.commandInputs

    # Select the setup/s to be relinked
    setup_input = inputs.addSelectionInput('setups', 'Setup(s) to Remove Machine from', 'Select the setups that you would like to yeet.')
    setup_input.setSelectionLimits(1, 0)


def command_execute(args: adsk.core.CommandEventArgs):
    # General logging for debug
    inputs = args.command.commandInputs
    setup_input: adsk.core.SelectionCommandInput = inputs.itemById('setups')
    
    for obj_ind in range(setup_input.selectionCount):
        obj = setup_input.selection(obj_ind).entity
        obj: adsk.cam.Setup
        obj.machine
        

def command_preselect(args: adsk.core.SelectionEventArgs):
    # APINOTDUMB: This runs when you open a command and so it acts like a selection filter
    inputs = args.activeInput.parentCommand.commandInputs
    # make sure that anything selected is a setup
    if args.selection.entity is not None and not args.selection.entity.classType() in ['adsk::cam::Setup']:
        args.isSelectable = False

# This event handler is called when the command terminates.
def command_destroy(args: adsk.core.CommandEventArgs):
    global local_handlers
    local_handlers = []
    futil.log(f'{CMD_NAME} Command Destroy Event')
