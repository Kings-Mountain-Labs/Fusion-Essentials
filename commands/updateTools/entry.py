import json
import adsk.core, adsk.fusion, adsk.cam, traceback
import os
from ...lib import fusion360utils as futil
from ... import config
import time
import random
from typing import List, Dict
import math
from adsk.cam import ToolLibrary, Tool, DocumentToolLibrary
from hashlib import sha256

app = adsk.core.Application.get()
ui: adsk.core.UserInterface = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_{config.ADDIN_NAME}_Update_Tools_from_Libraries'
CMD_NAME = 'Update Tools from Libraries'
CMD_Description = 'Update Tools from Tool Library'
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
    setup_input = inputs.addSelectionInput('setups', 'Operation(s) to Update', 'Select the setups, operations, or folders that you would like relinked.')
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
    library_input.tooltipDescription = 'Select the tool library you would like to replace from.'
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
    operations: List[adsk.cam.Operation] = []
    for obj_ind in range(setup_input.selectionCount):
        obj = setup_input.selection(obj_ind).entity
        if obj.classType() == 'adsk::cam::Setup':
            setup: adsk.cam.Setup = obj
            for operation in setup.allOperations:
                operations.append(operation)
        elif obj.classType() == 'adsk::cam::Operation':
            operation: adsk.cam.Operation = obj
            operations.append(operation)
        elif obj.classType() == 'adsk::cam::CAMFolder':
            folder: adsk.cam.CAMFolder = obj
            for operation in folder.allOperations:
                operations.append(operation)
        else:
            futil.log(f'Unknown entity type: {obj.classType()}')
        
    # remove duplicates
    # store a list of the unique operation ids
    unique_ids = []
    # store the unique operations
    unique_operations = []
    for operation in operations:
        if operation.operationId not in unique_ids:
            unique_ids.append(operation.operationId)
            unique_operations.append(operation)
    operations = unique_operations
    
    replace_with_library_tool(operations, library, correlation_type)

def remove_tip_keys(tool: dict) -> dict: # APIDUMB: For some reason when you get the tool back from a operation after the file has been closed and reopened all the tip values become zero
    # Remove the tip keys from the tool dictionary
    keys_to_remove = ['tip-angle', 'tip-diameter', 'tip-length', 'tip-offset', 'tip-radius', 'tip-type']
    for key in keys_to_remove:
        if key in tool.keys():
            tool.pop(key)
    return tool

class LibraryTool:
    tool: Tool
    used_already: bool
    document_index: int
    def __init__(self, tool: Tool):
        self.tool = tool
        self.document_index = -1

    def get_tool(self, dtl: DocumentToolLibrary) -> Tool:
        if self.document_index == -1:
            dtl.add(self.tool)
            self.document_index = dtl.count - 1
        futil.log(f'Library Tool {dict(self.tool.toJson())["description"]}: {self.tool.toJson()==dtl.item(self.document_index).toJson()}')
        return dtl.item(self.document_index)


def replace_with_library_tool(operations: List[adsk.cam.Operation], library: ToolLibrary, correlation_type: str):
    # Iterate through each operation in the setup and replace the tool with the library tool
    library_tool_description: Dict[str, int] = {}
    library_tool_product_ids: Dict[str, int] = {}
    library_tool_geometry_hash: Dict[str, int] = {}
    library_tool_used: List[LibraryTool] = []
    bad_correlation = False
    cam = adsk.cam.CAM.cast(app.activeProduct)
    dtl = cam.documentToolLibrary
    geom_debug = "Library tools: \n"
    for i in range(library.count):
        tool: Tool = library.item(i)
        tool_json = json.loads(tool.toJson(), parse_float=lambda x: round(float(x), 3)) # APIDUMB: All the floats coming out of a newly opened file are .3f so we need to do this so the hash matches
        library_tool_used.append(LibraryTool(tool))
        lib_num = library_tool_used.__len__() - 1
        
        library_tool_description[tool_json["description"]] = lib_num
        library_tool_product_ids[tool_json["product-id"]] = lib_num
        if "geometry" in tool_json.keys():
            geometry = json.dumps(remove_tip_keys(tool_json["geometry"]))
            geometry_hash = sha256(geometry.encode()).hexdigest()
            geom_debug += f'{geometry_hash}: \"{geometry}\"\n'
            library_tool_geometry_hash[geometry_hash] = lib_num
    geom_debug += "Setup tools: \n"
    for operation in operations:
        tool = operation.tool
        tool_json = json.loads(tool.toJson(), parse_float=lambda x: round(float(x), 3)) # APIDUMB: WHAT IF I DONT WANT TO USE JSON??? WHAT ABOUT JUST ACCESSING THE PROPERTIES DIRECTLY???
        preset_name: str
        if operation.toolPreset is None:
            preset_name = ''
        else:
            preset_name = operation.toolPreset.name
        # futil.log(f'Tool: {json.dumps(tool_json)}')
        print_str = f'Operation: {operation.name}'
        # pad the string to 32 characters
        print_str += ' ' * (32 - len(operation.name))
        if correlation_type == 'Description':
            description = tool_json["description"]
            print_str += f'matching by Description: {description}'
            if description != '': # dont match if the description is empty
                library_tool = library_tool_description.get(description)
        elif correlation_type == 'Product ID':
            product_id = tool_json["product-id"]
            print_str += f'matching by Product ID: {product_id}'
            if product_id != '': # dont match if the product id is empty
                library_tool = library_tool_product_ids.get(product_id)
        elif correlation_type == 'Geometry':
            geometry = json.dumps(remove_tip_keys(tool_json["geometry"]))
            geometry_hash = sha256(geometry.encode()).hexdigest()
            geom_debug += f'{geometry_hash}: \"{geometry}\"\n'
            print_str += f'matching by Geometry Hash: {geometry_hash}'
            library_tool = library_tool_geometry_hash.get(geometry_hash)
        if library_tool:
            lib_tool = library_tool_used[library_tool]
            print_str += f'\t Found Match'
            operation.tool = lib_tool.get_tool(dtl)
            presets = json.loads(lib_tool.tool.toJson())['start-values']['presets']
            preset_descriptions = [preset['name'] for preset in presets]
            if preset_name in preset_descriptions:
                items = lib_tool.tool.presets.itemsByName(preset_name)
                operation.toolPreset = items[0]
                print_str += f'\t Preset: {preset_name} successfully set'
            else:
                print_str += f'\t Preset: {preset_name} not found in library tool'
                # futil.log(f"{json.dumps(presets)}\n{preset_descriptions}")
                bad_correlation = True
        else:
            print_str += f'\t No Match Found'
            bad_correlation = True
        
        futil.log(print_str)

    # save geom_debug to a file for debug
    # fn = f'geom_debug_{time.strftime("%Y%m%d-%H%M%S")}.txt'
    # with open(fn, 'w') as f:
    #     f.write(geom_debug)
    # log the file location
    # futil.log(f'Geometry Debug: {fn}')
    # futil.log(f"Directory: {os.getcwd()}")

    if bad_correlation:
        ui.messageBox(f'Some tools could not be correlated to the library.\nCheck the Text Command Panel for details.')
    

def command_preselect(args: adsk.core.SelectionEventArgs):
    # APINOTDUMB: This runs when you open a command and so it acts like a selection filter
    inputs = args.activeInput.parentCommand.commandInputs
    # make sure that anything selected is a setup
    if args.selection.entity is not None and not args.selection.entity.classType() in ['adsk::cam::Setup', 'adsk::cam::Operation', 'adsk::cam::CAMFolder']:
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