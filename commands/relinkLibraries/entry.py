import json
import adsk.core, adsk.fusion, adsk.cam, traceback
import os
from ...lib import fusion360utils as futil
from ... import config
import time
import random
from typing import List
import math
from adsk.cam import ToolLibrary, Tool

app = adsk.core.Application.get()
ui: adsk.core.UserInterface = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_{config.ADDIN_NAME}_RelinkLibraries'
CMD_NAME = 'Relink Tool Libraries'
CMD_Description = 'Relink tools to their tool library'
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
    # for i in range(workspace.toolbarTabs.count):
    #     futil.log(f'Tab: {workspace.toolbarTabs.item(i).id}')
    # for i in range(workspace.toolbarPanels.count):
    #     futil.log(f'Panel: {workspace.toolbarPanels.item(i).id}')
    panel = workspace.toolbarPanels.itemById(PANEL_ID)
    control = panel.controls.addCommand(cmd_def, COMMAND_BESIDE_ID, False)

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
    futil.add_handler(args.command.inputChanged, command_input_changed, local_handlers=local_handlers)
    futil.add_handler(args.command.executePreview, command_preview, local_handlers=local_handlers)
    futil.add_handler(args.command.preSelect, command_preselect, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)

    inputs = args.command.commandInputs

    # Select the setup/s to be relinked
    setup_input = inputs.addSelectionInput('setups', 'Setup(s) to Link', 'Select the setups that you would like relinked.')
    # setup_input.selectionFilters = ['SolidBodies']
    setup_input.setSelectionLimits(1, 0)

    

    prodid_input = inputs.addStringValueInput('prodid', 'Product ID', 'Enter the product ID.')
    prodid_input.value = ""

    prodlink_input = inputs.addStringValueInput('prodlink', 'Product Link', 'Enter the link to the product page.')
    prodlink_input.value = ""

    # Option to select which tooling library to use
    library_input = inputs.addDropDownCommandInput('library', 'Library', adsk.core.DropDownStyles.TextListDropDownStyle)
    # Get the list of tooling libraries
    libraries = get_tooling_libraries()
    # Format the list of libraries for display in the drop down
    library_input.tooltipDescription = 'Select the tool library you would like to link to.'
    formatted_libraries = format_library_names(libraries)
    for library in formatted_libraries:
        library_input.listItems.add(library, False)
    # print them to the console for debug
    futil.log(f'Available libraries: {libraries}')


def command_execute(args: adsk.core.CommandEventArgs):
    # General logging for debug
    inputs = args.command.commandInputs
    setup_input: adsk.core.SelectionCommandInput = inputs.itemById('setups')
    for setup_ind in range(setup_input.selectionCount):
        setup = setup_input.selection(setup_ind).entity
        futil.log(f'Setup: {setup.name} {setup.classType()}')
        


# This function will be called when the command needs to compute a new preview in the graphics window
def command_preview(args: adsk.core.CommandEventArgs):
    inputs = args.command.commandInputs

def command_preselect(args: adsk.core.SelectionEventArgs):
    # if the user is selecting the end face then we need to check to see if the axis is valid
    inputs = args.activeInput.parentCommand.commandInputs


# This function will be called when the user changes anything in the command dialog
def command_input_changed(args: adsk.core.InputChangedEventArgs):
    changed_input = args.input
    inputs = args.inputs
    axis_input: adsk.core.SelectionCommandInput = inputs.itemById('axis')
    end_face_input: adsk.core.SelectionCommandInput = inputs.itemById('end_face')
    # only make the axis and end face inputs visible if the body input has been set
    if changed_input.id == 'body' and changed_input.selectionCount > 0:
        axis_input.isVisible = True
        axis_input.isEnabled = True
    elif changed_input.id == 'body':
        axis_input.isVisible = False
        end_face_input.isVisible = False
        axis_input.clearSelection()
        end_face_input.clearSelection() 

    if changed_input.id == 'axis' and changed_input.selectionCount > 0:
        if end_face_input.selectionCount > 0:
            end_face_input.clearSelection() 
        end_face_input.isVisible = True
        end_face_input.isEnabled = True
    elif changed_input.id == 'axis':
        end_face_input.isVisible = False
        end_face_input.clearSelection()

# This event handler is called when the command terminates.
def command_destroy(args: adsk.core.CommandEventArgs):
    global local_handlers
    local_handlers = []
    futil.log(f'{CMD_NAME} Command Destroy Event')


def get_tooling_libraries() -> List:
    # Get the list of tooling libraries
    camManager = adsk.cam.CAMManager.get()
    libraryManager = camManager.libraryManager
    toolLibraries = libraryManager.toolLibraries
    fusion360Folder = toolLibraries.urlByLocation(adsk.cam.LibraryLocations.CloudLibraryLocation)
    libraries = getLibrariesURLs(toolLibraries, fusion360Folder)
    fusion360Folder = toolLibraries.urlByLocation(adsk.cam.LibraryLocations.LocalLibraryLocation)
    libraries = libraries + getLibrariesURLs(toolLibraries, fusion360Folder)
    fusion360Folder = toolLibraries.urlByLocation(adsk.cam.LibraryLocations.ExternalLibraryLocation)
    libraries = libraries + getLibrariesURLs(toolLibraries, fusion360Folder)
    return libraries

def getLibrariesURLs(libraries: adsk.cam.ToolLibraries, url: adsk.core.URL):
    ''' Return the list of libraries URL in the specified library '''
    urls: list[str] = []
    libs = libraries.childAssetURLs(url)
    for lib in libs:
        urls.append(lib.toString())
    for folder in libraries.childFolderURLs(url):
        urls = urls + getLibrariesURLs(libraries, folder)
    return urls

def format_library_names(libraries: List) -> List:
    # Format the list of libraries for display in the drop down
    formatted_libraries = []
    for library in libraries:
        formatted_libraries.append(library.split('/')[-1])
    return formatted_libraries