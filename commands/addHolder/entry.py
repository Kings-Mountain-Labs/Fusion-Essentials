import json
import adsk.core, adsk.fusion, adsk.cam, traceback
import os
from ...lib import fusion360utils as futil
from ... import config
import time
import random
from typing import List
import math

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
    axis_input.selectionFilters = ['ToroidalFaces', 'CylindricalFaces', 'ConicalFaces']
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

    # Option to select which tooling library to use
    library_input = inputs.addDropDownCommandInput('library', 'Library', adsk.core.DropDownStyles.TextListDropDownStyle)
    # Get the list of tooling libraries
    libraries = get_tooling_libraries()
    # Format the list of libraries for display in the drop down
    formatted_libraries = format_library_names(libraries)
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
    axis = get_axis(axis_input.selection(0).entity)
    # Get the selected end face
    end_face_input: adsk.core.SelectionCommandInput = inputs.itemById('end_face')
    end_face = is_valid_axial_datum(end_face_input.selection(0).entity, axis)

    tool_profile = get_tool_profile(body, axis, end_face)
    
    # Get the name of the tool
    name_input = inputs.itemById('name')
    name = name_input.value
    # Get the product ID
    prodid_input = inputs.itemById('prodid')
    prodid = prodid_input.value
    # Get the product link
    prodlink_input = inputs.itemById('prodlink')
    prodlink = prodlink_input.value

    tool = generate_tool(tool_profile, name, prodid, prodlink)

    futil.log(f'Tool:\n{json.dumps(tool, indent=4)}')

    # Get the selected library
    library_input: adsk.core.DropDownCommandInput = inputs.itemById('library')
    library = library_input.selectedItem.name
    libraries = get_tooling_libraries()
    formatted_libraries = format_library_names(libraries)
    library_index = formatted_libraries.index(library)
    library_url = adsk.core.URL.create(libraries[library_index])
    futil.log(f'Library URL: {library_url.toString()}')

    camManager = adsk.cam.CAMManager.get()
    libraryManager = camManager.libraryManager
    toolLibraries = libraryManager.toolLibraries
    library = toolLibraries.toolLibraryAtURL(library_url)
    current_lib = json.loads(library.toJson())
    try:
        current_lib['data'].append(tool)
    except KeyError:
        current_lib['data'] = [tool]
    library = adsk.cam.ToolLibrary.createFromJson(json.dumps(current_lib))
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
        axis = get_axis(axis_input.selection(0).entity)
        end_face = args.selection.entity
        if is_valid_axial_datum(end_face, axis) is None:
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

def get_axis(axis_base: adsk.core.Base) -> adsk.core.InfiniteLine3D:
    # if it is a BRepFace then we need to get the geometry from it
    if isinstance(axis_base, adsk.fusion.BRepFace):
        # this should be a cylindrical, conical or toroidal face
        # we need to try and cast it to each of these types
        face_type = axis_base.geometry.surfaceType
        if face_type == adsk.core.SurfaceTypes.ConeSurfaceType:
            cone = adsk.core.Cone.cast(axis_base.geometry)
            return adsk.core.InfiniteLine3D.create(cone.origin, cone.axis)
        elif face_type == adsk.core.SurfaceTypes.CylinderSurfaceType:
            cylinder = adsk.core.Cylinder.cast(axis_base.geometry)
            return adsk.core.InfiniteLine3D.create(cylinder.origin, cylinder.axis)
        elif face_type == adsk.core.SurfaceTypes.TorusSurfaceType:
            torus = adsk.core.Torus.cast(axis_base.geometry)
            return adsk.core.InfiniteLine3D.create(torus.origin, torus.axis)
    # if it is a BRepEdge then we can just get the geometry from it
    elif isinstance(axis_base, adsk.fusion.BRepEdge):
        # This should be a linear edge so we can cast it and get the geometry
        edge_type = axis_base.geometry.curveType
        if edge_type == adsk.core.Curve3DTypes.Line3DCurveType:
            line = adsk.core.Line3D.cast(axis_base.geometry)
            return line.asInfiniteLine()
    return None
        
def is_valid_axial_datum(surface: adsk.core.Base, axis: adsk.core.InfiniteLine3D) -> adsk.core.Point3D:
    # This function should take in a face normal to the line, or a line perpendicular to the line, or a point
    # and return the point where the line intersects the face, or the point where the lines are closest, or the point projected onto the line
    # it should return None if the datum is not valid
    if isinstance(surface, adsk.fusion.BRepFace):
        # this should be a planar face
        # we need to try and cast it to each of these types
        face_type = surface.geometry.surfaceType
        if face_type == adsk.core.SurfaceTypes.PlaneSurfaceType:
            plane = adsk.core.Plane.cast(surface.geometry)
            return plane.intersectWithLine(axis)
    elif isinstance(surface, adsk.fusion.BRepEdge):
        # This should be a linear edge so we can cast it and get the geometry
        edge_type = surface.geometry.curveType
        normal, center = None, None
        if edge_type == adsk.core.Curve3DTypes.Line3DCurveType:
            line = adsk.core.Line3D.cast(surface.geometry)
            # check to make sure the lines are orthogonal
            if axis.direction.dotProduct(line.asInfiniteLine().direction) != 0: # APIDUMB: Why do I have to cast this to an infinite line to get the direction?
                return None
            # then we can get the point along the axis that is closest to the line
            # to do this we create a plane that contains the line and is perpendicular to the axis
            plane = adsk.core.Plane.create(line.startPoint, axis.direction)
            # then we get the point where the plane intersects the axis
            return plane.intersectWithLine(axis)
        # for the rest of the edge types other than NURBS we can get the normal of the edge to see if it is parallel to the axis
        elif edge_type == adsk.core.Curve3DTypes.NurbsCurve3DCurveType:
            return None
        elif edge_type == adsk.core.Curve3DTypes.Circle3DCurveType:
            circle = adsk.core.Circle3D.cast(surface.geometry)
            normal = circle.normal
            center = circle.center
        elif edge_type == adsk.core.Curve3DTypes.Ellipse3DCurveType:
            ellipse = adsk.core.Ellipse3D.cast(surface.geometry)
            normal = ellipse.normal
            center = ellipse.center
        elif edge_type == adsk.core.Curve3DTypes.Arc3DCurveType:
            arc = adsk.core.Arc3D.cast(surface.geometry)
            normal = arc.normal
            center = arc.center
        elif edge_type == adsk.core.Curve3DTypes.EllipticalArc3DCurveType:
            elliptical_arc = adsk.core.EllipticalArc3D.cast(surface.geometry)
            normal = elliptical_arc.normal
            center = elliptical_arc.center
        else:
            return None
        if not axis.direction.isParallelTo(normal):
            return None
        plane = adsk.core.Plane.create(center, axis.direction)
        return plane.intersectWithLine(axis)
    elif isinstance(surface, adsk.fusion.BRepVertex):
        # This should be a point
        point = adsk.core.Point3D.cast(surface.geometry)
        plane = adsk.core.Plane.create(point, axis.direction)
        return plane.intersectWithLine(axis)
    return None

def get_tool_profile(body: adsk.fusion.BRepBody, axis: adsk.core.InfiniteLine3D, plane_intersect: adsk.core.Point3D):
    # first step is to get the axis 
    # We will find Find all the points in the body and transform them all to cylindrical coordinates with the axis as the z axis
    # then we will find the most negative z value and start working our way up the z axis
    plane = adsk.core.Plane.create(plane_intersect, axis.direction)
    points = []
    for edge in body.edges:
        points.append(edge.startVertex.geometry)
        points.append(edge.endVertex.geometry)
    # now we have a list of all the points in the body
    cylindrical_points = []
    for point in points:
        cylindrical_points.append(get_cylindrical_coordinates_point(point, axis, plane))
    # now we need to find the most negative z value and start working our way up the z axis
    # we will find the index of the most negative z value
    
    # we will try and find conical, cylindrical and toroidal faces that are parallel to the axis
    # we can disregard planar faces because that are be parallel to the axis because they are inherent to the geometry
    # if we find a toroidal face we will pretend it is a chamfer, ie conical
    useful_faces = []
    for face in body.faces:
        if face.geometry.surfaceType == adsk.core.SurfaceTypes.ConeSurfaceType:
            cone = adsk.core.Cone.cast(face.geometry)
            if axis.isColinearTo(adsk.core.InfiniteLine3D.create(cone.origin, cone.axis)):
                useful_faces.append(face)
        elif face.geometry.surfaceType == adsk.core.SurfaceTypes.CylinderSurfaceType:
            cylinder = adsk.core.Cylinder.cast(face.geometry)
            if axis.isColinearTo(adsk.core.InfiniteLine3D.create(cylinder.origin, cylinder.axis)):
                useful_faces.append(face)
        elif face.geometry.surfaceType == adsk.core.SurfaceTypes.TorusSurfaceType:
            torus = adsk.core.Torus.cast(face.geometry)
            if axis.isColinearTo(adsk.core.InfiniteLine3D.create(torus.origin, torus.axis)):
                useful_faces.append(face)
        
    # now we can find what faces line up to each other
    # we need to do a few things, remove duplicates, remove faces that are smaller than another face that covers it in Z, and order the faces
    # we will start by removing duplicates
    face_segments = []
    for face in useful_faces:
        # we are hoping that the faces have clean edges, if not we will rectify that later
        # for now we will just define what we have as segments
        valid_edges = []
        for edge in face.edges:
            pt = get_cylindrical_coordinates_edge(edge, axis, plane)
            if pt is not None:
                valid_edges.append(pt)
        # now we have a list of valid edges, we will pick the highest and lowest points and use them to define the segment
        # this makes the assumption that there is no concave toroidal faces but idgaf
        # we are also hoping that there are two good points on the face
        if len(valid_edges) >= 2:
            valid_edges.sort(key=lambda x: x[1])
            z_1 = valid_edges[0][1]
            z_2 = valid_edges[-1][1]
            r_1 = valid_edges[0][0]
            r_2 = valid_edges[-1][0]
            face_segments.append((r_1, r_2, z_1, z_2))
    
    ind_to_pop = []
    face_segments.sort(key=lambda x: x[2])
    for i in range(0, len(face_segments) - 1):
        if abs(face_segments[i][0] - face_segments[i+1][0]) < 1e-8 and abs(face_segments[i][1] - face_segments[i+1][1]) < 1e-8 and abs(face_segments[i][2] - face_segments[i+1][2]) < 1e-8 and abs(face_segments[i][3] - face_segments[i+1][3]) < 1e-8:
            ind_to_pop.append(i)
    for i in range(0, len(ind_to_pop)):
        face_segments.pop(ind_to_pop[i] - i)
    # remove duplicates from the face segments
    profile = []
    for seg in face_segments:
        profile.append((seg[0], seg[2], 1))
        profile.append((seg[1], seg[3], 0))
    profile.sort(key=lambda x: x[1])
    # if there is more than 2 points in the profile with the same z value than we need to take only the two points with the largest r value
    # this can cause a lot of issues with tools designed where radii are not are the same and similar strange issues
    profile = filter_points(profile)

    for segment in face_segments:
        ind_to_pop = []
        for i in range(0, len(profile)):
            if profile[i][1] >= segment[3] - 1e-8 or profile[i][1] <= segment[2] + 1e-8:
                continue
            elif profile[i][0] < ((segment[1] - segment[0]) / (segment[3] - segment[2])) * (segment[2] - profile[i][1]) + segment[0]:
                ind_to_pop.append(i)
        for i in range(0, len(ind_to_pop)):
            profile.pop(ind_to_pop[i] - i)

    profile_points = []
    # make it a profile
    for i in range(0, len(profile) - 1):
        if abs(profile[i][1] - profile[i+1][1]) < 1e-8:
            continue
        profile_points.append([profile[i][1], profile[i+1][1], profile[i][0], profile[i+1][0]])

    return profile_points

def filter_points(points):
    # Create a dictionary to group points by their y values within 1e-8
    grouped_points = {}
    for x, y, z in points:
        # Round y to 8 decimal places to consider y values within 1e-8
        rounded_y = round(y, 8)
        if rounded_y not in grouped_points:
            grouped_points[rounded_y] = []
        grouped_points[rounded_y].append((x, y, z))
    
    # Filter the groups and keep only the two points with the largest x values
    # if this is the point with the lowest y value than we will only keep one point
    filtered_points = []
    min_key = min(grouped_points.keys())
    for key, group in grouped_points.items():
        sorted_group = sorted(group, key=lambda p: p[0], reverse=True)
        if key == min_key:
            filtered_points.append(sorted_group[0])
        else:
            filtered_points.extend(sorted_group[:2])
    # now resort the points by their y values and their z values
    filtered_points.sort(key=lambda p: (p[1], p[2]))
    return filtered_points

def get_cylindrical_coordinates_edge(edge: adsk.fusion.BRepEdge, axis: adsk.core.InfiniteLine3D, plane: adsk.core.Plane):
    # we only care if this edge is a arc or a circle
    edge_type = edge.geometry.curveType
    if edge_type != adsk.core.Curve3DTypes.Circle3DCurveType and edge_type != adsk.core.Curve3DTypes.Arc3DCurveType:
        return None
    point = edge.geometry.center
    normal = edge.geometry.normal
    # check to see if the normal is colinear to the axis
    if not axis.isColinearTo(adsk.core.InfiniteLine3D.create(point, normal)):
        return None
    
    line = adsk.core.InfiniteLine3D.create(point, axis.direction)
    intersect = plane.intersectWithLine(line)
    z = point.distanceTo(intersect)
    return (edge.geometry.radius, z)

def get_cylindrical_coordinates_point(point: adsk.core.Point3D, axis: adsk.core.InfiniteLine3D, plane: adsk.core.Plane) -> (float, float):
    # get a infinite line through the point that is parallel to the axis
    line = adsk.core.InfiniteLine3D.create(point, axis.direction)
    # get the point where the line intersects the plane
    intersect = plane.intersectWithLine(line)
    # get the distance between the point and the intersection point
    z = point.distanceTo(intersect)
    # get the distance between the point and the axis
    r = point.distanceTo(axis.origin)
    return (r, z)

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

def generate_tool(profile, desc, prodid, prodlink):
    guid = "00000000-0000-0000-0000-" + str(random.randint(100000000000,999999999999))
    data = {
        "description": desc,
        "guid": guid,
        "last_modified": math.ceil(time.time()),
        "product-id": prodid,
        "product-link": prodlink,
        "reference_guid": guid,
        "segments": [],
        "type": "holder",
        "unit": "millimeters",
        "vendor": ""
    }
    
    for segment in profile:
        seg = {
            "height": round((segment[1] - segment[0])*10, 3),
            "lower-diameter": round(segment[2]*10*2, 3),
            "upper-diameter": round(segment[3]*10*2, 3)
        }
        data["segments"].append(seg)
    return data