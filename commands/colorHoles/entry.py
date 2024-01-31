import adsk.core, adsk.fusion
import os
from ...lib import fusion360utils as futil
from ... import config
from ... import shared_state
from ...timer import Timer, format_timer
from typing import List, Dict
import math
from random import random
from pathlib import Path
import csv
from adsk.core import Point3D, Matrix3D

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_{config.ADDIN_NAME}_Color_Holes'
CMD_NAME = 'Color Holes'
CMD_Description = 'Color Holes based on their size'
IS_PROMOTED = True

WORKSPACE_ID = 'FusionSolidEnvironment'
PANEL_ID = 'InspectPanel'
COMMAND_BESIDE_ID = 'MeasureSurface'

ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

DEFAULT_SETTINGS = {
    "hover_size": {
        "type": "checkbox",
        "label": "Show Size on Selection",
        "default": True
    },
    "preview_default": {
        "type": "checkbox",
        "label": "Preview Colors by Default",
        "default": True
    }
}

def loadHoles():
    tmplist = []
    holepath = os.path.join(Path(__file__).resolve().parent, 'HoleSizes.csv')
    with open(holepath, newline='') as csvfile:
        csvreader = csv.reader(csvfile)
        header = next(csvreader)
        for row in csvreader:
            tmplist.append(row)
    return tmplist

# Initialize the settings on first use
if not shared_state.load_settings(CMD_ID):
    shared_state.save_settings(CMD_ID, DEFAULT_SETTINGS)

timer = Timer()

# Local list of event handlers used to maintain a reference so
# they are not released and garbage collected.
local_handlers = []
_holes: list = None
_custom_graphics_group: adsk.fusion.CustomGraphicsGroup = None

def start():
    global _holes
    _holes = loadHoles()
    cmd_def = ui.commandDefinitions.addButtonDefinition(CMD_ID, CMD_NAME, CMD_Description, ICON_FOLDER)
    futil.add_handler(cmd_def.commandCreated, command_created)
    workspace = ui.workspaces.itemById(WORKSPACE_ID)
    panel = workspace.toolbarPanels.itemById(PANEL_ID)
    control = panel.controls.addCommand(cmd_def, COMMAND_BESIDE_ID, False)
    control.isPromoted = IS_PROMOTED
    futil.add_handler(ui.activeSelectionChanged, active_selection_changed)
    futil.add_handler(app.documentActivated, document_changed)
    global _custom_graphics_group
    design = adsk.fusion.Design.cast(app.activeProduct)
    _custom_graphics_group = design.rootComponent.customGraphicsGroups.add()

def stop():
    # Get the various UI elements for this command
    clear_graphics()
    workspace = ui.workspaces.itemById(WORKSPACE_ID)
    panel = workspace.toolbarPanels.itemById(PANEL_ID)
    command_control = panel.controls.itemById(CMD_ID)
    command_definition = ui.commandDefinitions.itemById(CMD_ID)

    if command_control:
        command_control.deleteMe()

    if command_definition:
        command_definition.deleteMe()

    # Remove the event handlers
    futil.clear_handlers()

def document_changed(args: adsk.core.DocumentEventArgs):
    # General logging for debug.
    futil.log(f'{CMD_NAME} Document Changed Event')
    global _custom_graphics_group
    design = adsk.fusion.Design.cast(app.activeProduct)
    _custom_graphics_group = design.rootComponent.customGraphicsGroups.add()


def active_selection_changed(args: adsk.core.ActiveSelectionEventArgs):
    sels = args.currentSelection
    global _custom_graphics_group
    settings = shared_state.load_settings(CMD_ID)
    if len(sels) == 1 and sels[0].entity.objectType == "adsk::fusion::BRepFace" and settings['hover_size']["default"] == True:
        ent: adsk.fusion.BRepFace = sels[0].entity
        cylinderface = adsk.core.Cylinder.cast(ent.geometry)
        if cylinderface and continuous_edges(ent):
            _, _, _, radius = cylinderface.getData()
            posSize = findNear(radius)
            if len(posSize) == 0:
                name = f"D{trt_str(radius*20)}"
            elif len(posSize) == 1:
                name = posSize[0]
            else:
                name = posSize[0]
                for n in posSize[1:]:
                    name = f"{name}\n{n}"
            # Now display it using a 2D ui element
            billboard = adsk.fusion.CustomGraphicsBillBoard.create(Point3D.create(0, 0, 0))
            clear_graphics()
            custom_text: adsk.fusion.CustomGraphicsText = _custom_graphics_group.addText(name, "Arial", 0.5, Matrix3D.create())
            # APIDUMB: Why is the color of the text a CustomGraphicsColorEffect and not a Color?
            # custom_text.color = adsk.core.Color.create(0, 0, 0, 1)
            custom_text.billBoarding = billboard
            custom_text.isSelectable = False
            custom_text.depthPriority = 1
        else:
            clear_graphics()
    else:
        clear_graphics()

def clear_graphics():
    global _custom_graphics_group
    if _custom_graphics_group:
        for graphic in range(_custom_graphics_group.count):
            _custom_graphics_group.item(graphic).deleteMe()

def command_created(args: adsk.core.CommandCreatedEventArgs):
    # General logging for debug.
    settings = shared_state.load_settings(CMD_ID)
    futil.log(f'{CMD_NAME} Command Created Event')
    futil.add_handler(args.command.execute, command_execute, local_handlers=local_handlers)
    futil.add_handler(args.command.executePreview, command_preview, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)

    inputs = args.command.commandInputs

    # Add the inputs to the command dialog.
    bodiesSelInput = inputs.addSelectionInput('bodies', 'Bodies', 'Select the bodies to analyse.')
    bodiesSelInput.addSelectionFilter('Bodies')
    bodiesSelInput.isFullWidth = True


    semiCmd = inputs.addBoolValueInput('semi', 'Color Partial Surfaces', True, "", True)
    previewCmd = inputs.addBoolValueInput('preview', 'Preview Selection', True, "", settings['preview_default']["default"])


def command_execute(args: adsk.core.CommandEventArgs):
    # General logging for debug
    futil.log(f'{CMD_NAME} Command Execute Event')
    inputs = args.command.commandInputs
    # Get the inputs.
    bodiesSel: adsk.core.SelectionCommandInput = inputs.itemById('bodies')
    semiInput: adsk.core.BoolValueCommandInput = inputs.itemById('semi')
    previewInput: adsk.core.BoolValueCommandInput = inputs.itemById('preview')

    # Color them in
    create_color(bodiesSel, semiInput.value)

# This function will be called when the command needs to compute a new preview in the graphics window
def command_preview(args: adsk.core.CommandEventArgs):
    inputs = args.command.commandInputs
    # Get the inputs.
    bodiesSel: adsk.core.SelectionCommandInput = inputs.itemById('bodies')
    semiInput: adsk.core.BoolValueCommandInput = inputs.itemById('semi')
    previewInput: adsk.core.BoolValueCommandInput = inputs.itemById('preview')

    if previewInput.value == True:
        # Color them in
        create_color(bodiesSel, semiInput.value)

class rgbCl:
    def __init__(self, r, g, b, o, n):
        self.r = r
        self.g = g
        self.b = b
        self.o = o
        self.n = n
        self.rgb = f"{r}-{g}-{b}-{o}"
        self.name = f"CH_{self.n}"


def mk_color(rgb: rgbCl):
    app = adsk.core.Application.get()
    ui  = app.userInterface
    design = adsk.fusion.Design.cast(app.activeProduct)
    favoriteAppearances = design.appearances
    try:
        myColor = favoriteAppearances.itemByName(rgb.name)
    except:
        myColor = None
    if myColor:
        return myColor
    else:
        # Get the existing Yellow appearance.            
        fusionMaterials = app.materialLibraries.itemByName('Fusion 360 Appearance Library')
        yellowColor = fusionMaterials.appearances.itemByName('Paint - Enamel Glossy (Yellow)')
        
        # Copy it to the design, giving it a new name.
        newColor = design.appearances.addByCopy(yellowColor, rgb.name)

        # Change the color of the appearance to red.
        colorProp = adsk.core.ColorProperty.cast(newColor.appearanceProperties.itemByName('Color'))
        colorProp.value = adsk.core.Color.create(rgb.r, rgb.g, rgb.b, rgb.o)
        # Assign it to the body.            
        return newColor

def trt_str(rad):
    return str(round(rad, 6))

def findNear(rad):
    posSizes = []
    for row in _holes:
        dif = abs(float(row[1]) - rad*20) # multiply by 2 to get dia then mult by 10 to get from cm to mm
        #_ui.messageBox(f"Hole Size: {rad}\nCompaired Size: {row[1]}\nDif: {dif}")
        if dif < 0.00011:
            posSizes.append(row[0])
    return posSizes

def continuous_edges(face):
    return face.loops.count > 1

def create_color(bodies, semi: bool):

    holes = []
    fiq = []
    for j in range(0, bodies.selectionCount):
        body =  bodies.selection(j).entity
        faces = body.faces
        for i in range(faces.count):
            face = faces.item(i)
            cylinderface = adsk.core.Cylinder.cast(face.geometry)
            if cylinderface and continuous_edges(face):
                res, origin, axis, radius = cylinderface.getData()
                holes.append([j, i, radius, origin.x, origin.y, origin.z, axis.x, axis.y, axis.z])
                fiq.append(face)
    sizes = {}

    for hole in holes:
        if trt_str(hole[2]) not in sizes.keys():
            posSize = findNear(hole[2])
            if len(posSize) == 0:
                name = f"D{trt_str(hole[2]*20)}"
            elif len(posSize) == 1:
                name = posSize[0]
            else:
                name = posSize[0]
                for n in posSize[1:]:
                    name = f"{name} or {n}"
            sizes[trt_str(hole[2])] = rgbCl(int(random()*255), int(random()*255), int(random()*255), 0, name) 
    
    for face in fiq:
        cylinderface = adsk.core.Cylinder.cast(face.geometry)
        if cylinderface:
            res, origin, axis, radius = cylinderface.getData()
            color = sizes[trt_str(radius)]
            app = mk_color(color)
            face.appearance = app

# This event handler is called when the command terminates.
def command_destroy(args: adsk.core.CommandEventArgs):
    global local_handlers
    local_handlers = []
    futil.log(f'{CMD_NAME} Command Destroy Event')