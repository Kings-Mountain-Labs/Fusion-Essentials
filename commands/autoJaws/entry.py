import adsk.core, adsk.fusion
import os
from ...lib import fusion360utils as futil
from ... import config
from ... import shared_state
from ...timer import Timer, format_timer
from typing import List, Dict
import math
from adsk.core import ValueInput, Vector3D, Point3D, Matrix3D, Line3D, DistanceValueCommandInput, AngleValueCommandInput, InputChangedEventArgs
from adsk.fusion import BRepBody, BRepFace, TemporaryBRepManager

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_{config.ADDIN_NAME}_AutoJaws'
CMD_NAME = 'Auto Jaws'
CMD_Description = 'Automatically generate soft jaws for a given part.'
PALETTE_NAME = 'My Palette Sample'
IS_PROMOTED = True

WORKSPACE_ID = 'FusionSolidEnvironment'
PANEL_ID = 'SolidModifyPanel'
COMMAND_BESIDE_ID = 'FusionPartingLineSplitCmd'

ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

DEFAULT_SETTINGS = {
    "jaw_thickness": {
        "type": "value",
        "label": "Specify the default thickness of the soft jaws",
        "default": "1.0 in",
        "units": "cm",
    },
    "jaw_height": {
        "type": "value",
        "label": "Specify the height of the soft jaws",
        "default": "2 in",
        "units": "cm",
    },
    "jaw_offset": {
        "type": "value",
        "label": "Specify the offset of the soft jaws from the part",
        "default": "0.5 in",
        "units": "cm",
    },
    "jaw_end_offset": {
        "type": "value",
        "label": "Specify the offset of the end of the soft jaws from the part",
        "default": "0.5 in",
        "units": "cm",
    },
    "jaw_middle_spacing": {
        "type": "value",
        "label": "Specify the spacing between the two soft jaws",
        "default": "0.25 in",
        "units": "cm",
    },
}

# Initialize the settings on first use
if not shared_state.load_settings(CMD_ID):
    shared_state.save_settings(CMD_ID, DEFAULT_SETTINGS)

timer = Timer()


class PartDatum:
    flb_point: Point3D
    frb_point: Point3D
    flt_point: Point3D
    rlb_point: Point3D
    right_vector: Vector3D
    top_vector: Vector3D
    rear_vector: Vector3D
    theta: float
    bottom_center: Point3D
    rotation_matrix: Matrix3D

    def __init__(self, flb_point: Point3D, frb_point: Point3D, flt_point: Point3D, rlb_point: Point3D, theta: float):
        self.flb_point = flb_point
        self.frb_point = frb_point
        self.flt_point = flt_point
        self.rlb_point = rlb_point
        self.right_vector = flb_point.vectorTo(frb_point)
        self.top_vector = flb_point.vectorTo(flt_point)
        self.rear_vector = flb_point.vectorTo(rlb_point)
        self.theta = theta
        self.get_bottom_center_with_transformation()

    def get_bottom_center_with_transformation(self) -> (Point3D, Matrix3D):
        # this function will return the bottom center point of the part datum with the transformation applied
        # get the center of the bottom face
        self.bottom_center = self.flb_point.copy()
        # APIDUMB: WHY THE FUCK CAN I NOT MULTIPLY A VECTOR BY A SCALAR????
        half_right_vector = self.right_vector.copy()
        half_rear_vector = self.rear_vector.copy()
        half_right_vector.scaleBy(0.5)
        half_rear_vector.scaleBy(0.5)
        self.bottom_center.translateBy(half_right_vector)
        self.bottom_center.translateBy(half_rear_vector)
        # get the rotation matrix for the datum, this should be the axis normal to the bottom face
        self.rotation_matrix = Matrix3D.create()
        self.rotation_matrix.setToRotation(self.top_vector.angleTo(Vector3D.create(0, 0, 1)), self.top_vector.crossProduct(Vector3D.create(0, 0, 1)), self.bottom_center)
        
    def get_angular_x_y(self) -> (Vector3D, Vector3D):
        # rotate the x (right vector) of the bounding back by theta about the top vector
        x_vector = self.right_vector.copy()
        y_vector = self.right_vector.copy()
        x_rotation_matrix = Matrix3D.create()
        x_rotation_matrix.setToRotation(-self.theta, self.top_vector, self.bottom_center)
        x_vector.transformBy(x_rotation_matrix)
        y_rotation_matrix = Matrix3D.create()
        y_rotation_matrix.setToRotation(-self.theta + math.pi/2, self.top_vector, self.bottom_center)
        y_vector.transformBy(y_rotation_matrix)
        return x_vector, y_vector

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
    settings = shared_state.load_settings(CMD_ID)
    futil.log(f'{CMD_NAME} Command Created Event')
    futil.add_handler(args.command.execute, command_execute, local_handlers=local_handlers)
    futil.add_handler(args.command.inputChanged, command_input_changed, local_handlers=local_handlers)
    futil.add_handler(args.command.executePreview, command_preview, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)

    inputs = args.command.commandInputs

    part_selection_input = inputs.addSelectionInput('part', 'Select Part', 'Select the part that the jaws will be offset from')
    part_selection_input.selectionFilters = ['SolidBodies']
    part_selection_input.setSelectionLimits(1, 0)

    face_chain_input = inputs.addSelectionInput('bottom_face', 'Select Bottom Face', 'Select the bottom face of the part that the jaws will be offset from')
    # We need to allow only planar, cylindrical, and conical faces because those are the only ones
    # that will be from a valid chamfer and can be correctly patched.
    face_chain_input.selectionFilters = ['PlanarFaces']
    face_chain_input.setSelectionLimits(1, 1)

    jaw_theta_input = inputs.addAngleValueCommandInput('jaw_theta', 'Jaw Angle', ValueInput.createByString("0 deg"))
    jaw_theta_input.isVisible = False

    jaw_thickness_input = inputs.addDistanceValueCommandInput('jaw_thickness', 'Jaw Thickness', ValueInput.createByString(settings["jaw_thickness"]["default"]))
    jaw_thickness_input.minimumValue = 0.0
    jaw_height_input = inputs.addDistanceValueCommandInput('jaw_height', 'Jaw Height', ValueInput.createByString(settings["jaw_height"]["default"]))
    jaw_height_input.minimumValue = 0.0
    jaw_offset_input = inputs.addDistanceValueCommandInput('jaw_offset', 'Jaw Offset', ValueInput.createByString(settings["jaw_offset"]["default"]))
    jaw_end_offset_input = inputs.addDistanceValueCommandInput('jaw_end_offset', 'Jaw End Offset', ValueInput.createByString(settings["jaw_end_offset"]["default"]))
    jaw_middle_spacing_input = inputs.addDistanceValueCommandInput('jaw_middle_spacing', 'Jaw Middle Spacing', ValueInput.createByString(settings["jaw_middle_spacing"]["default"]))
    jaw_middle_spacing_input.minimumValue = 0.0

    # Disable them, this makes the manipulator disappear
    hide_show_distance_inputs(inputs, False)


def command_execute(args: adsk.core.CommandEventArgs):
    # General logging for debug
    futil.log(f'{CMD_NAME} Command Execute Event')
    inputs = args.command.commandInputs
    # get the selected face
    face = args.command.commandInputs.itemById('bottom_face').selection(0).entity
    # get the selected parts, and put them in a list
    parts: List[BRepBody] = []
    for part_num in range(args.command.commandInputs.itemById('part').selectionCount):
        parts.append(args.command.commandInputs.itemById('part').selection(part_num).entity)
    # get the jaw theta
    theta_input: AngleValueCommandInput = args.command.commandInputs.itemById('jaw_theta')
    jaw_theta = theta_input.value
    # get the bounding box
    bounding_box = generate_bounding_box_with_face(parts, face, jaw_theta)
    # show the bounding box
    generate_soft_jaws(args, parts, face, jaw_theta, bounding_box)

# This function will be called when the command needs to compute a new preview in the graphics window
def command_preview(args: adsk.core.CommandEventArgs):
    inputs = args.command.commandInputs
    # get the selected face
    face = args.command.commandInputs.itemById('bottom_face').selection(0).entity
    # get the selected parts, and put them in a list
    parts: List[BRepBody] = []
    for part_num in range(args.command.commandInputs.itemById('part').selectionCount):
        parts.append(args.command.commandInputs.itemById('part').selection(part_num).entity)
    # get the jaw theta
    theta_input: AngleValueCommandInput = args.command.commandInputs.itemById('jaw_theta')
    jaw_theta = theta_input.value
    # get the bounding box
    bounding_box = generate_bounding_box_with_face(parts, face, jaw_theta)
    # show the bounding box
    show_bounding_box(bounding_box)
    generate_soft_jaws(args, parts, face, jaw_theta, bounding_box)


# This function will be called when the user changes anything in the command dialog
def command_input_changed(args: InputChangedEventArgs):
    changed_input = args.input
    inputs = args.inputs
    # within the input changed event we want to check if there are valid selections for the part and the face
    # if there are we shall make the other inputs visible
    # otherwise we shall hide them
    # (changed_input.id == 'part' or changed_input.id == 'bottom_face' or changed_input.id == 'jaw_theta')
    if inputs.itemById('part').selectionCount > 0 and inputs.itemById('bottom_face').selectionCount > 0:
        inputs.itemById('jaw_theta').isVisible = True
        hide_show_distance_inputs(inputs, True)
        face = inputs.itemById('bottom_face').selection(0).entity
        # get the selected parts, and put them in a list
        parts: List[BRepBody] = []
        for part_num in range(inputs.itemById('part').selectionCount):
            parts.append(inputs.itemById('part').selection(part_num).entity)
        # get the jaw theta
        theta_input: AngleValueCommandInput = inputs.itemById('jaw_theta')
        jaw_theta = theta_input.value
        # get the bounding box
        bounding_box = generate_bounding_box_with_face(parts, face, jaw_theta)
        if changed_input.id != 'jaw_theta':
            x, y = bounding_box.get_angular_x_y()
            ret = theta_input.setManipulator(bounding_box.bottom_center, x, y)
            if not ret:
                futil.log('Failed to set manipulator for jaw_theta_input')
        jaw_middle_spacing_input: DistanceValueCommandInput = inputs.itemById('jaw_middle_spacing')
        jaw_thickness_input: DistanceValueCommandInput = inputs.itemById('jaw_thickness')
        jaw_end_offset_input: DistanceValueCommandInput = inputs.itemById('jaw_end_offset')
        jaw_offset_input: DistanceValueCommandInput = inputs.itemById('jaw_offset')
        jaw_height_input: DistanceValueCommandInput = inputs.itemById('jaw_height')
        if changed_input.id != 'jaw_middle_spacing':
            current_expression = jaw_middle_spacing_input.expression
            # the manipulator position is going to be the center point on the bottom of the bounding box and the direction will be the y vector
            ret = jaw_middle_spacing_input.setManipulator(bounding_box.bottom_center, bounding_box.rear_vector)
            # APIDUMB: this is brain dead, why does it try and project the position of the manipulator to the normal of the extension axis????????
            jaw_middle_spacing_input.expression = current_expression
            if not ret:
                futil.log('Failed to set manipulator for jaw_middle_spacing_input')

        if changed_input.id != 'jaw_thickness':
            current_expression = jaw_thickness_input.expression
            # the manipulator position is going to be the center point on the bottom of the bounding box and the direction will be the y vector
            manipulator_origin = bounding_box.bottom_center.copy()
            offset_vector = bounding_box.rear_vector.copy()
            offset_vector.normalize()
            offset_vector.scaleBy(jaw_middle_spacing_input.value/2)
            manipulator_origin.translateBy(offset_vector)
            ret = jaw_thickness_input.setManipulator(manipulator_origin, bounding_box.rear_vector)
            jaw_thickness_input.expression = current_expression
            if not ret:
                futil.log('Failed to set manipulator for jaw_thickness_input')

        if changed_input.id != 'jaw_end_offset':
            current_expression = jaw_end_offset_input.expression
            manipulator_origin = bounding_box.bottom_center.copy()
            offset_vector = bounding_box.right_vector.copy()
            offset_vector.scaleBy(0.5)
            manipulator_origin.translateBy(offset_vector)
            ret = jaw_end_offset_input.setManipulator(manipulator_origin, bounding_box.right_vector)
            jaw_end_offset_input.expression = current_expression
            if not ret:
                futil.log('Failed to set manipulator for jaw_end_offset_input')

        if changed_input.id != 'jaw_offset':
            current_expression = jaw_offset_input.expression
            ret = jaw_offset_input.setManipulator(bounding_box.bottom_center, bounding_box.top_vector)
            jaw_offset_input.expression = current_expression
            if not ret:
                futil.log('Failed to set manipulator for jaw_offset_input')

        if changed_input.id != 'jaw_height':
            # we put this one pretty far off to the side out of the action of the other manipulators
            current_expression = jaw_height_input.expression
            manipulator_origin = bounding_box.bottom_center.copy()
            offset_vector = bounding_box.rear_vector.copy()
            offset_vector.normalize()
            offset_vector.scaleBy(jaw_middle_spacing_input.value/2 + jaw_thickness_input.value/2)
            manipulator_origin.translateBy(offset_vector)
            offset_vector = bounding_box.right_vector.copy()
            offset_vector.scaleBy(0.5)
            manipulator_origin.translateBy(offset_vector)
            offset_vector = bounding_box.right_vector.copy()
            offset_vector.normalize()
            offset_vector.scaleBy(jaw_end_offset_input.value/2)
            manipulator_origin.translateBy(offset_vector)
            pointing_vector = bounding_box.top_vector.copy()
            pointing_vector.scaleBy(-1)
            ret = jaw_height_input.setManipulator(manipulator_origin, pointing_vector)
            jaw_height_input.expression = current_expression
            if not ret:
                futil.log('Failed to set manipulator for jaw_height_input')

    else:
        inputs.itemById('jaw_theta').isVisible = False
        hide_show_distance_inputs(inputs, False)
    # ux feel stuff
    if changed_input.id == 'part':
        # if we just selected a part, and the user isn't holding ctrl or shift and there is no face selected then we should activate the face selection

        pass

def generate_soft_jaws(args: adsk.core.CommandEventArgs, bodies: List[BRepBody], face: BRepFace, theta: float, part_datum: PartDatum):
    # first things first, lets create a new component to house the jaws, within the active component
    jaw_offset_expression = args.command.commandInputs.itemById('jaw_offset').expression
    jaw_end_offset_expression = args.command.commandInputs.itemById('jaw_end_offset').expression
    jaw_middle_spacing_expression = args.command.commandInputs.itemById('jaw_middle_spacing').expression
    jaw_thickness_expression = args.command.commandInputs.itemById('jaw_thickness').expression
    jaw_height_expression = args.command.commandInputs.itemById('jaw_height').expression
    des = adsk.fusion.Design.cast(app.activeProduct)
    root = des.rootComponent
    new_component = root.occurrences.addNewComponent(Matrix3D.create()).component
    new_component.name = 'Softest Jaws'
    # Create a offset plane from the face by the jaw offset
    constructionPlanes = new_component.constructionPlanes
    planeInput = constructionPlanes.createInput()
    planeInput.setByOffset(face, ValueInput.createByString(f"-({jaw_offset_expression})"))
    
    plane = constructionPlanes.add(planeInput)
    plane.name = "JawTopPlane"
    # create a sketch on the plane, and put a point on the plane that is the projection of the bottom center of the bounding box
    # APIDUMB: Why is there no point primitive that that isn't part of a sketch?
    sketches = new_component.sketches
    sketch = sketches.add(plane)
    sketch_points = sketch.sketchPoints
    our_origin = sketch_points.add(sketch.modelToSketchSpace(part_datum.bottom_center.copy())) # we have to transform it by the sketch origin
    our_origin.isFixed = True
    # now we will make a second point that is the projection of the bottom center of the bounding box onto the plane
    # get the normal of the plane
    # APIDUMB: Why is there no normal property on the ConstructionPlane, if in reality all planes are just planes why not treat them as planes and have them impl the construction part?
    # That may be constrained by the ergonomics of Cpp but its still a sharp corner to the situation, ya feel? I ended up not needing that functionality but it annoyed me.
    # now we can constrain them together, and the one point to the construction plane
    plane_point: adsk.fusion.SketchPoint = sketch.project(our_origin).item(0)
    constraints = sketch.geometricConstraints
    # now we need to make a point off to one side of the origin point in the x axis of the bounding box to use to create a second plane
    secondary_point = sketch.sketchToModelSpace(plane_point.geometry.copy())
    secondary_point.translateBy(part_datum.right_vector)
    secondary_pt = sketch_points.add(sketch.modelToSketchSpace(secondary_point))
    secondary_pt.isFixed = True
    # now we can constrain the secondary point to the origin point
    planeInput = constructionPlanes.createInput()
    planeInput.setByThreePoints(our_origin, plane_point, secondary_pt)
    plane2 = constructionPlanes.add(planeInput)
    plane2.name = "JawCrossSectionPlane"

    tertiary_point = sketch.sketchToModelSpace(plane_point.geometry.copy())
    tertiary_point.translateBy(part_datum.rear_vector)
    tertiary_pt = sketch_points.add(sketch.modelToSketchSpace(tertiary_point))
    tertiary_pt.isFixed = True
    # now we can generate the third plane, this plane we will use for generating the holes
    planeInput = constructionPlanes.createInput()
    planeInput.setByThreePoints(our_origin, plane_point, tertiary_pt)
    plane3 = constructionPlanes.add(planeInput)
    plane3.name = "CenterPlane"

    # now we create a sketch on the plane, and draw the cross section of the jaw
    sketch2 = sketches.add(plane2) # APIDUMB: I really want to be able to give it a orientation like NX so that using Horizontal and Vertical constraints are useful (if i cant they I have to consider them non-deterministic)
    # Now we shall draw the two rectangles that define the cross section of the jaw
    # they are identical and will both be jaw_height by jaw_thickness and spaced apart from our_origin by jaw_middle_spacing with their top edges coincident to our_origin
    # I would draw the first and mirror it, but you may not really want the yaws centered on the part, so I will make it easy to edit for now
    # First make the top points, they will be be coincident with the other plane and we can use that to establish our horizontal (we dont know if sketch horizontal really is horizontal)
    our_origin_projection: adsk.fusion.SketchPoint = sketch2.project(our_origin).item(0)
    plane_point_projection: adsk.fusion.SketchPoint = sketch2.project(plane_point).item(0)
    center_line = sketch2.sketchCurves.sketchLines.addByTwoPoints(our_origin_projection, plane_point_projection)
    center_line.isConstruction = True
    constraints2 = sketch2.geometricConstraints
    fd_vector: Vector3D = None
    def create_jaw_half():
        nonlocal fd_vector
        # tir = top inner right
        our_tir_point: adsk.fusion.SketchPoint = None
        if fd_vector is None:
            our_tir_point = sketch2.sketchPoints.add(plane_point_projection.geometry.copy()) # we just put it here because its kinda close to where its gotta end up
        else:
            new_pt = plane_point_projection.geometry.copy()
            fd_vector.scaleBy(-1)
            new_pt.translateBy(fd_vector)
            our_tir_point = sketch2.sketchPoints.add(new_pt)
        
        constraints2.addCoincidentToSurface(our_tir_point, plane2)
        # now we can add distances to the points
        dimensions = sketch2.sketchDimensions

        mid_spacing = dimensions.addDistanceDimension(plane_point_projection, our_tir_point, adsk.fusion.DimensionOrientations.AlignedDimensionOrientation, Point3D.create())
        mid_spacing.parameter.expression = f"({jaw_middle_spacing_expression})/2" # this is such a ass interface, the way the object model handles Parameters vs expressions is strange

        # get the vector from the projected origin to the first point to second point and make the second point to that extent beyond the first, so that it ends up on the right side
        offset_vector = plane_point_projection.geometry.vectorTo(our_tir_point.geometry)
        new_pt = our_tir_point.geometry.copy()
        new_pt.translateBy(offset_vector)
        fd_vector = offset_vector.copy()
        our_tor_point = sketch2.sketchPoints.add(new_pt)
        constraints2.addCoincidentToSurface(our_tor_point, plane2)

        side_spacing = dimensions.addDistanceDimension(our_tor_point, our_tir_point, adsk.fusion.DimensionOrientations.AlignedDimensionOrientation, Point3D.create())
        side_spacing.parameter.expression = f"{jaw_thickness_expression}"

        # add a point on the inside lower end
        _, offset_vector_2 = face.evaluator.getNormalAtPoint(face.pointOnFace)
        offset_vector_2.normalize()
        offset_vector_2.scaleBy(-1)
        new_pt_2 = sketch2.sketchToModelSpace(our_tir_point.geometry).copy()
        new_pt_2.translateBy(offset_vector_2)

        # Now we draw a line between the two points
        sketch_lines = sketch2.sketchCurves.sketchLines
        # APIDUMB: the addThreePointRectangle function is scuffed
        side_line = sketch_lines.addThreePointRectangle(our_tir_point, our_tor_point, sketch2.modelToSketchSpace(new_pt_2))
        # now that we have gouged our eyes out, lets constrain the lines correctly
        # And we just have to hope that the order is deterministic
        constraints2.addPerpendicular(side_line.item(0), side_line.item(1))
        constraints2.addPerpendicular(side_line.item(1), side_line.item(2))
        constraints2.addPerpendicular(side_line.item(2), side_line.item(3))
        height = dimensions.addDistanceDimension(side_line.item(1).startSketchPoint, side_line.item(1).endSketchPoint, adsk.fusion.DimensionOrientations.AlignedDimensionOrientation, Point3D.create())
        height.parameter.expression = f"{jaw_height_expression}"
        # now we must constrain all the points to the plane
        constraints2.addCoincident(plane_point_projection, side_line.item(0))
        constraints2.addCoincidentToSurface(side_line.item(1).startSketchPoint, plane2)
        constraints2.addCoincidentToSurface(side_line.item(1).endSketchPoint, plane2)
        constraints2.addCoincidentToSurface(side_line.item(3).startSketchPoint, plane2)
        constraints2.addCoincidentToSurface(side_line.item(3).endSketchPoint, plane2)

        constraints2.addParallel(side_line.item(1), center_line)
    # dont worry about this, I know what im doing
    create_jaw_half()
    create_jaw_half()

    # Now finally we can extrude the soft jaws
    extrudes = new_component.features.extrudeFeatures
    object_collection = adsk.core.ObjectCollection.create()
    for profile in sketch2.profiles:
        # futil.log(f"Adding profile {profile.areaProperties().area}")
        object_collection.add(profile)
    extrude_input = extrudes.createInput(object_collection, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    extrude_input.setSymmetricExtent(ValueInput.createByString(f"({part_datum.rear_vector.length} cm)/2 + {jaw_end_offset_expression}"), False)
    extrude_input.isSolid = True

    jaw_bodies = extrudes.add(extrude_input)

    # And now the fun part, creating the hole for the part
    # APIDUMB: Literally all the BRep types should have hella evaluators, the fact that I have to invent everything on my own is dumb, like really excruciatingly dumb
    
    # Complete the following steps to create a soft jaw hollowing, repeat for each jaw
    # Step 1: Boolean the part from the jaw
    # Step 2: Collect all the surfaces that the boolean generated
    # Step 3: Preprocess and remove clearance any shitty geometry
    # Step 3.1: Replace all Z-axis toroids with a cylindrical counterbore
    # Step 4: Project each of the faces onto the sketch plane
    # Step 5: Do a extruded cut from the projection on the plane to the face with a through body extent
    # Step 6: Remove non flat floor faces
    # Step 7: Dogbone internal corners
    # Step 7.1: Find all sharp vertical internal radiuses
    # Step 7.2: Create a dogbone sketch on the top plane
    # Step 7.3: Extrude the dogbone sketch to the bottom of the edge

    # Step 1
    

def generate_bounding_box_with_face(bodies: List[BRepBody], face: BRepFace, theta: float) -> PartDatum:
    # This function will make a temporary duplicate of the bodies and align the face to the xy plane and rotate them by the theta
    # Then we will run the generate_bounding_box function on the temporary bodies and get the bounding box back
    # then we will do a coordinate transformation on the bounding box to get the points we need
    # the points will be the front left bottom, front right bottom, front left top, and rear left bottom

    # make a temporary duplicate of the bodies
    tempBrepMgr = TemporaryBRepManager.get()
    temp_bodies = []
    for body in bodies:
        temp_bodies.append(tempBrepMgr.copy(body))
    
    # align the face to the xy plane
    # get the normal of the face
    _, normal = face.evaluator.getNormalAtPoint(face.pointOnFace)
    # get the angle between the normal and the z axis
    z_axis = Vector3D.create(0, 0, 1)
    face_angle = normal.angleTo(z_axis)
    # rotate the bodies by the angle
    # get the rotation axis
    rotation_axis = normal.crossProduct(z_axis)
    # get the rotation matrix
    rotation_matrix2 = Matrix3D.create()
    rotation_matrix2.setToRotation(face_angle, rotation_axis, face.pointOnFace)
    # additionally we would like to rotate the bodies about the normal of the face by the theta
    # get the rotation matrix
    rotation_matrix = Matrix3D.create()
    rotation_matrix.setToRotation(theta, normal, face.pointOnFace)
    # multiply the matrices
    rotation_matrix.transformBy(rotation_matrix2)
    # rotate the bodies
    for body in temp_bodies:
        tempBrepMgr.transform(body, rotation_matrix)

    # get the bounding box
    bounding_box = generate_bounding_box(temp_bodies)
    # get the points
    flb_point = Point3D.create(bounding_box.minPoint.x, bounding_box.minPoint.y, bounding_box.maxPoint.z)
    frb_point = Point3D.create(bounding_box.maxPoint.x, bounding_box.minPoint.y, bounding_box.maxPoint.z)
    rlb_point = Point3D.create(bounding_box.minPoint.x, bounding_box.maxPoint.y, bounding_box.maxPoint.z)
    flt_point = Point3D.create(bounding_box.minPoint.x, bounding_box.minPoint.y, bounding_box.minPoint.z)

    # now transform the points back to the original coordinate system
    # get the inverse of the rotation matrix
    rotation_matrix.invert()
    # transform the points
    flb_point.transformBy(rotation_matrix)
    frb_point.transformBy(rotation_matrix)
    flt_point.transformBy(rotation_matrix)
    rlb_point.transformBy(rotation_matrix)

    # return the points
    return PartDatum(flb_point, frb_point, flt_point, rlb_point, theta)

def generate_bounding_box(bodies: List[BRepBody]) -> adsk.core.BoundingBox3D:
    # This function will generate a bounding around the given bodies
    bounding_box = bodies[0].boundingBox
    if len(bodies) > 1:
        for body in bodies:
            bounding_box.combine(body.boundingBox)
    return bounding_box


def show_bounding_box(part_datum: PartDatum):
    # We would like to show the bounding box we have created with custom graphics as it is not aligned to the coordinate system
    # We will use the graphics object to do this
    des = adsk.fusion.Design.cast(app.activeProduct)
    root = des.rootComponent
    # get the graphics object
    graphics = root.customGraphicsGroups.add()
    # first lets create the 3 perpendicular vectors representing the bounding box
    # now we will create the rest of the points
    # get the front right top point
    front_right_top = part_datum.flb_point.copy()
    front_right_top.translateBy(part_datum.right_vector)
    front_right_top.translateBy(part_datum.top_vector)
    # get the rear left top point
    rear_left_top = part_datum.flb_point.copy()
    rear_left_top.translateBy(part_datum.rear_vector)
    rear_left_top.translateBy(part_datum.top_vector)
    # get the rear right bottom point
    rear_right_bottom = part_datum.flb_point.copy()
    rear_right_bottom.translateBy(part_datum.rear_vector)
    rear_right_bottom.translateBy(part_datum.right_vector)
    # get the rear right top point
    rear_right_top = part_datum.flb_point.copy()
    rear_right_top.translateBy(part_datum.rear_vector)
    rear_right_top.translateBy(part_datum.right_vector)
    rear_right_top.translateBy(part_datum.top_vector)
    # now we will create the lines
    # create the lines
    # front face
    graphics.addCurve(Line3D.create(part_datum.flb_point, part_datum.frb_point))
    graphics.addCurve(Line3D.create(part_datum.frb_point, front_right_top))
    graphics.addCurve(Line3D.create(front_right_top, part_datum.flt_point))
    graphics.addCurve(Line3D.create(part_datum.flt_point, part_datum.flb_point))
    # rear face
    graphics.addCurve(Line3D.create(part_datum.rlb_point, rear_left_top))
    graphics.addCurve(Line3D.create(rear_left_top, rear_right_top))
    graphics.addCurve(Line3D.create(rear_right_top, rear_right_bottom))
    graphics.addCurve(Line3D.create(rear_right_bottom, part_datum.rlb_point))
    # side lines
    graphics.addCurve(Line3D.create(part_datum.flb_point, part_datum.rlb_point))
    graphics.addCurve(Line3D.create(part_datum.frb_point, rear_right_bottom))
    graphics.addCurve(Line3D.create(part_datum.flt_point, rear_left_top))
    graphics.addCurve(Line3D.create(front_right_top, rear_right_top))

    # now we will create the text
    app.activeViewport.refresh()


def hide_show_distance_inputs(inputs: adsk.core.CommandInputs, show: bool):
    # this function will hide or show the distance inputs based on the show bool
    # if show is true, it will show the inputs, if false, it will hide them
    inputs.itemById('jaw_thickness').isVisible = show
    inputs.itemById('jaw_height').isVisible = show
    inputs.itemById('jaw_offset').isVisible = show
    inputs.itemById('jaw_end_offset').isVisible = show
    inputs.itemById('jaw_middle_spacing').isVisible = show

# This event handler is called when the command terminates.
def command_destroy(args: adsk.core.CommandEventArgs):
    global local_handlers
    local_handlers = []
    futil.log(f'{CMD_NAME} Command Destroy Event')