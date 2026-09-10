import json
import adsk.core, adsk.fusion, adsk.cam, traceback
import os
from ...lib import fusion360utils as futil
from ... import config
from adsk.cam import ToolLibrary, Tool

# The holder GEOMETRY + library helpers now live in the headless core shared with the
# model_compute_holder MCP tool (one source of truth - the command and the agent drive the same code).
# This module keeps only the COMMAND: the dialog, selection handlers, and the library write.
from ..mcpServer.tools import _holder

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_{config.ADDIN_NAME}_AddHolder'
CMD_NAME = 'Add Tool Holder'
CMD_Description = 'Convert a solid model tool holder into a tool holder for the tool library.'
IS_PROMOTED = True

WORKSPACE_ID = 'FusionSolidEnvironment'
PANEL_ID = config.tools_panel_id
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
    futil.add_handler(args.command.inputChanged, command_input_changed, local_handlers=local_handlers)
    futil.add_handler(args.command.executePreview, command_preview, local_handlers=local_handlers)
    futil.add_handler(args.command.preSelect, command_preselect, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)

    inputs = args.command.commandInputs

    # Option to select the tool body
    body_input = inputs.addSelectionInput('body', 'Tool Body', 'Select the body to be used as the tool.')
    body_input.selectionFilters = ['SolidBodies']
    body_input.setSelectionLimits(1, 1)

    # Option to select the central axis
    axis_input = inputs.addSelectionInput('axis', 'Axis', 'Select the axis to be used as the axis of rotation.')
    axis_input.selectionFilters = ['ToroidalFaces', 'CylindricalFaces', 'ConicalFaces', 'LinearEdges', 'ConstructionLines']
    axis_input.setSelectionLimits(1, 1)
    axis_input.isVisible = False

    # Option to select the end face of the tool
    end_face_input = inputs.addSelectionInput('end_face', 'End Face', 'Select the end face of the tool.')
    end_face_input.selectionFilters = ['PlanarFaces', 'Edges', 'Vertices']
    end_face_input.setSelectionLimits(1, 1)
    end_face_input.isVisible = False

    # Option to name the tool
    name_input = inputs.addStringValueInput('name', 'Name', 'Enter a name for the tool.')
    # Set the default value to the name of the current document
    name_input.value = app.activeDocument.name

    prodid_input = inputs.addStringValueInput('prodid', 'Product ID', 'Enter the product ID.')
    prodid_input.value = ""

    prodlink_input = inputs.addStringValueInput('prodlink', 'Product Link', 'Enter the link to the product page.')
    prodlink_input.value = ""

    prodvendor_input = inputs.addStringValueInput('prodvendor', 'Vendor', 'Enter the Vendor Details.')
    prodvendor_input.value = ""

    # Option to select which tooling library to use
    library_input = inputs.addDropDownCommandInput('library', 'Library', adsk.core.DropDownStyles.TextListDropDownStyle)
    # Get the list of tooling libraries
    libraries = _holder.get_tooling_libraries()
    # Format the list of libraries for display in the drop down
    formatted_libraries = _holder.format_library_names(libraries)
    for library in formatted_libraries:
        library_input.listItems.add(library, False)
    # print them to the console for debug
    futil.log(f'Available libraries: {libraries}')


def command_execute(args: adsk.core.CommandEventArgs):
    # General logging for debug
    inputs = args.command.commandInputs
    # Get the selected body
    body_input: adsk.core.SelectionCommandInput = inputs.itemById('body')
    body = body_input.selection(0).entity
    # Get the selected axis
    axis_input: adsk.core.SelectionCommandInput = inputs.itemById('axis')
    axis = _holder.get_axis(axis_input.selection(0).entity)
    # Get the selected end face
    end_face_input: adsk.core.SelectionCommandInput = inputs.itemById('end_face')
    end_face = _holder.is_valid_axial_datum(end_face_input.selection(0).entity, axis)

    tool_profile = _holder.get_tool_profile(body, axis, end_face)

    # Get the name of the tool
    name_input = inputs.itemById('name')
    name = name_input.value
    # Get the product ID
    prodid_input = inputs.itemById('prodid')
    prodid = prodid_input.value
    # Get the product link
    prodlink_input = inputs.itemById('prodlink')
    prodlink = prodlink_input.value
    # Get the Vendor details
    prodvendor_input = inputs.itemById('prodvendor')
    prodvendor = prodvendor_input.value

    tool = _holder.generate_tool(tool_profile, name, prodid, prodlink, prodvendor)

    futil.log(f'Tool:\n{tool.toJson()}')

    # Get the selected library
    library_input: adsk.core.DropDownCommandInput = inputs.itemById('library')
    library = library_input.selectedItem.name
    libraries = _holder.get_tooling_libraries()
    formatted_libraries = _holder.format_library_names(libraries)
    library_index = formatted_libraries.index(library)
    library_url = adsk.core.URL.create(libraries[library_index])
    futil.log(f'Library URL: {library_url.toString()}')

    camManager = adsk.cam.CAMManager.get()
    libraryManager = camManager.libraryManager
    toolLibraries = libraryManager.toolLibraries
    library = toolLibraries.toolLibraryAtURL(library_url)
    library.add(tool)
    success = toolLibraries.updateToolLibrary(library_url, library)
    if success:
        futil.log('Tool added to library successfully')
        ui.messageBox('Tool added to library successfully')


# This function will be called when the command needs to compute a new preview in the graphics window
def command_preview(args: adsk.core.CommandEventArgs):
    inputs = args.command.commandInputs

def command_preselect(args: adsk.core.SelectionEventArgs):
    # if the user is selecting the end face then we need to check to see if the axis is valid
    inputs = args.activeInput.parentCommand.commandInputs # APIDUMB: this is hacky and dumb, you should not have to walk this far through the object tree to get the command inputs
    if args.activeInput.id == 'end_face':
        axis_input: adsk.core.SelectionCommandInput = inputs.itemById('axis')
        axis = _holder.get_axis(axis_input.selection(0).entity)
        end_face = args.selection.entity
        if _holder.is_valid_axial_datum(end_face, axis) is None:
            args.isSelectable = False


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
