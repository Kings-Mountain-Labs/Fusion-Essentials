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
from adsk.core import Point3D, Matrix3D, Cylinder, Vector3D, InfiniteLine3D, Line3D, Selection
from adsk.fusion import BRepFace, TemporaryBRepManager

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
    selections = args.currentSelection
    global _custom_graphics_group
    settings = shared_state.load_settings(CMD_ID)
    if len(selections) == 1 and selections[0].entity.objectType == "adsk::fusion::BRepFace" and settings["hover_size"]["default"]:
        ent: BRepFace = selections[0].entity
        cylinder_face = Cylinder.cast(ent.geometry)
        if cylinder_face and continuous_edges(ent) and is_cylinder_inward(ent):
            _, pt, _, radius = cylinder_face.getData()
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
            mat = best_display_point(ent, cylinder_face)
            custom_text: adsk.fusion.CustomGraphicsText = _custom_graphics_group.addText(name, "Arial", 0.25, mat)
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

def best_display_point(face: BRepFace, cylinder: Cylinder) -> Matrix3D:
    matrix = Matrix3D.create()
    # We have two selection criteria, first we want to check if it is a blind hole, if it is we want to display the opposite face
    # If it is not a blind hole, we want to display the face that is closest to the camera
    
    # First we check if it is a blind hole, we will check of there is a face that shares a edge with the current face that is intersected by the axis of the cylinder
    # First we will make a line and infinite line using the axis of the cylinder
    # APIDUMB: Cylinder is really just a circle sketch in 3D, because this cylinder has no length
    res, origin, axis, radius = cylinder.getData()
    # get a oriented bounding box around the hole aligned with the axis of the hole and then use those extents for the line
    # make a temporary duplicate of the bodies
    tempBrepMgr = TemporaryBRepManager.get()
    face_cpy = tempBrepMgr.copy(face)
    rotation_matrix = Matrix3D.create()
    rotation_to_vector = Vector3D.create(0, 0, 1)
    axis.normalize()
    angle = axis.angleTo(rotation_to_vector)
    # create a pivot vector that is perpendicular to the axis and the rotation_to_vector
    pivot_vector = axis.crossProduct(rotation_to_vector)
    rotation_matrix.setToRotation(angle, pivot_vector, origin)
    tempBrepMgr.transform(face_cpy, rotation_matrix)
    # get the bounding box of the face
    box = face_cpy.boundingBox
    # get the height of the box
    height = box.maxPoint.z - box.minPoint.z
    # scale the axis to the height of the box
    axis.scaleBy(height)
    # APIDUMB: There should be no such thing as a Point3D, points are just vectors, having to switch between them is just silly
    axis.scaleBy(0.5)
    start_point = origin.copy()
    start_point.translateBy(axis)
    axis_m = axis.copy()
    axis_m.scaleBy(-1)
    end_point = origin.copy()
    end_point.translateBy(axis_m)
    cylinder_axis = Line3D.create(origin, end_point)
    inf_line = InfiniteLine3D.create(origin, axis)
    edges = face.edges
    oppose_point = []
    for edge in edges:
        # get the face that is not the current face
        other_face = edge.faces.item(0) if edge.faces.item(0) != face else edge.faces.item(1)
        # check if the face intersects the infinite line
        # APIDUMB: InfiniteLine3D needs a intersectWithSurfaceEvaluator method to determine if it truly intersects with a face
        inters = inf_line.intersectWithSurface(other_face.geometry)
        # check if there are intersections
        for inter_point in inters:
            # Check to see that the vector from the farthest point on the line to the intersection point is the opposite as the normal of the face at the point
            far_point = cylinder_axis.startPoint if cylinder_axis.startPoint.distanceTo(inter_point) > cylinder_axis.endPoint.distanceTo(inter_point) else cylinder_axis.endPoint
            intersection_vector = far_point.vectorTo(inter_point)
            _, normal = other_face.evaluator.getNormalAtPoint(inter_point)
            if intersection_vector.dotProduct(normal) < 0:
                oppose_point.append(inter_point)
    # if there are faces to oppose we will use the first one, and just put a message if there is more than one
    if len(oppose_point) > 1:
        pass
        futil.log("There are more than one face to oppose, this should not happen")

    if len(oppose_point) > 0:
        point: Point3D = oppose_point[0]
        # Find the point on the line farthest from the intersection point
        if point.distanceTo(cylinder_axis.startPoint) > point.distanceTo(cylinder_axis.endPoint):
            matrix.translation = cylinder_axis.startPoint.asVector()
        else:
            matrix.translation = cylinder_axis.endPoint.asVector()
    else:
        # If there are no faces to oppose, we will just use the point on the line that is closest to the camera
        # lets get the view direction
        view = app.activeViewport
        # get the point on the line that is closest to the camera
        if cylinder_axis.startPoint.distanceTo(view.camera.eye) < cylinder_axis.endPoint.distanceTo(view.camera.eye):
            matrix.translation = cylinder_axis.startPoint.asVector()
        else:
            matrix.translation = cylinder_axis.endPoint.asVector()
    return matrix

def is_cylinder_inward(face: BRepFace):
    cylinder = Cylinder.cast(face.geometry)
    if cylinder:
        res, origin, axis, radius = cylinder.getData()
        # get the normal of the face at a point on the cylinder and see if it is pointing towards the center of the cylinder
        _, normal = face.evaluator.getNormalAtPoint(face.pointOnFace)
        # get the vector from the origin of the cylinder to the point on the face
        vec = face.pointOnFace.vectorTo(origin)
        # if the dot product of the normal and the vector is negative, the normal is pointing away from the center of the cylinder's axis
        return (normal.dotProduct(vec) > 0)
    return False
    

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
            cylinder_face = Cylinder.cast(face.geometry)
            if cylinder_face and continuous_edges(face) and is_cylinder_inward(face):
                res, origin, axis, radius = cylinder_face.getData()
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
        cylinder_face = Cylinder.cast(face.geometry)
        if cylinder_face:
            res, origin, axis, radius = cylinder_face.getData()
            color = sizes[trt_str(radius)]
            app = mk_color(color)
            face.appearance = app

# This event handler is called when the command terminates.
def command_destroy(args: adsk.core.CommandEventArgs):
    global local_handlers
    local_handlers = []
    futil.log(f'{CMD_NAME} Command Destroy Event')