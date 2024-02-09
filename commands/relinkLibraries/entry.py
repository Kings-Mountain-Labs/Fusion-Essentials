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
    futil.add_handler(args.command.preSelect, command_preselect, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)

    inputs = args.command.commandInputs

    # Select the setup/s to be relinked
    setup_input = inputs.addSelectionInput('setups', 'Setup(s) to Link', 'Select the setups that you would like relinked.')
    setup_input.setSelectionLimits(1, 0)

    # Make a drop down for correlation type
    correlation_input = inputs.addDropDownCommandInput('correlation', 'Correlation Type', adsk.core.DropDownStyles.TextListDropDownStyle)
    correlation_input.listItems.add('Description', True)
    correlation_input.listItems.add('Product ID', False)
    correlation_input.listItems.add('Geometry', False)

    # Option to select which tooling library to use
    library_input = inputs.addDropDownCommandInput('library', 'Library', adsk.core.DropDownStyles.TextListDropDownStyle)
    # Get the list of tooling libraries
    libraries = get_tooling_libraries()
    # Format the list of libraries for display in the drop down
    library_input.tooltipDescription = 'Select the tool library you would like to link to.'
    formatted_libraries = format_library_names(libraries)
    for library in formatted_libraries:
        library_input.listItems.add(library, True)
    # print them to the console for debug
    futil.log(f'Available libraries: {libraries}')


def command_execute(args: adsk.core.CommandEventArgs):
    # General logging for debug
    inputs = args.command.commandInputs
    setup_input: adsk.core.SelectionCommandInput = inputs.itemById('setups')
    correlation_input: adsk.core.DropDownCommandInput = inputs.itemById('correlation')
    correlation_type = correlation_input.selectedItem.name
    library_input: adsk.core.DropDownCommandInput = inputs.itemById('library')
    camManager = adsk.cam.CAMManager.get()
    libraryManager = camManager.libraryManager
    toolLibraries = libraryManager.toolLibraries
    libraries = get_tooling_libraries()
    formatted_libraries = format_library_names(libraries)
    library_index = formatted_libraries.index(library_input.selectedItem.name)
    library_url = adsk.core.URL.create(libraries[library_index])
    library = toolLibraries.toolLibraryAtURL(library_url)

    for setup_ind in range(setup_input.selectionCount):
        setup: adsk.cam.Setup = setup_input.selection(setup_ind).entity
        replace_with_library_tool(setup, library, correlation_type)
        futil.log(f'Setup: {setup.name} {setup.classType()}')

def replace_with_library_tool(setup: adsk.cam.Setup, library: adsk.cam.ToolLibrary, correlation_type: str):
    # Iterate through each operation in the setup and replace the tool with the library tool
    library_tool_description: Dict[str, adsk.cam.Tool] = {}
    library_tool_product_ids: Dict[str, adsk.cam.Tool] = {}
    library_tool_geometry_hash: Dict[str, adsk.cam.Tool] = {}
    for i in range(library.count):
        tool: adsk.cam.Tool = library.item(i)
        tool_json = json.loads(tool.toJson())
        library_tool_description[tool_json["description"]] = tool
        library_tool_product_ids[tool_json["product-id"]] = tool
        geometry = json.dumps(tool_json["geometry"])
        geometry_hash = sha256(geometry.encode()).hexdigest()
        library_tool_geometry_hash[geometry_hash] = tool
    for operation in setup.allOperations:
        operation: adsk.cam.Operation
        tool = operation.tool
        tool_json = json.loads(tool.toJson()) # APIDUMB: WHAT IF I DONT WANT TO USE JSON??? WHAT ABOUT JUST ACCESSING THE PROPERTIES DIRECTLY???
        # futil.log(f'Tool: {tool_json}')
        print_str = f'Operation: {operation.name}'
        # pad the string to 32 characters
        print_str += ' ' * (32 - len(operation.name))
        if correlation_type == 'Description':
            description = tool_json["description"]
            print_str += f'matching by Description: {description}'
            library_tool = library_tool_description.get(description)
        elif correlation_type == 'Product ID':
            product_id = tool_json["product-id"]
            print_str += f'matching by Product ID: {product_id}'
            library_tool = library_tool_product_ids.get(product_id)
        elif correlation_type == 'Geometry':
            geometry = json.dumps(tool_json["geometry"])
            geometry_hash = sha256(geometry.encode()).hexdigest()
            print_str += f'matching by Geometry Hash: {geometry_hash}'
            library_tool = library_tool_geometry_hash.get(geometry_hash)
        if library_tool:
            print_str += f'\t Found Match'
            operation.tool = library_tool
        else:
            print_str += f'\t No Match Found'
        futil.log(print_str)
    

def command_preselect(args: adsk.core.SelectionEventArgs):
    # APINOTDUMB: This runs when you open a command and so it acts like a selection filter
    inputs = args.activeInput.parentCommand.commandInputs
    # make sure that anything selected is a setup
    if args.selection.entity is not None and args.selection.entity.classType() != 'adsk::cam::Setup':
        args.isSelectable = False

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