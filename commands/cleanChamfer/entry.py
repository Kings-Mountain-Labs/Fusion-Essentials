import adsk.core, adsk.fusion
import os
from ...lib import fusion360utils as futil
from ... import config
from ... import shared_state
from ...timer import Timer, format_timer
from typing import List, Dict
import math

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_{config.ADDIN_NAME}_CleanChamfer'
CMD_NAME = 'Clean Chamfer'
CMD_Description = 'Create a clean patched surface for your chamfer'
PALETTE_NAME = 'My Palette Sample'
IS_PROMOTED = True

WORKSPACE_ID = 'FusionSolidEnvironment'
PANEL_ID = 'SolidModifyPanel'
COMMAND_BESIDE_ID = 'FusionChamferCommand'

ICON_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'resources', '')

DEFAULT_SETTINGS = {
    "option_checkbox": {
        "type": "checkbox",
        "label": "Sew by Default",
        "default": False
    },
    "permissive": {
        "type": "checkbox",
        "label": "Permissive Tangency Mode",
        "default": False
    }
}

# Initialize the settings on first use
if not shared_state.load_settings(CMD_ID):
    shared_state.save_settings(CMD_ID, DEFAULT_SETTINGS)

timer = Timer()

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
    # futil.add_handler(args.command.inputChanged, command_input_changed, local_handlers=local_handlers)
    futil.add_handler(args.command.validateInputs, command_validateinputs, local_handlers=local_handlers)
    futil.add_handler(args.command.executePreview, command_preview, local_handlers=local_handlers)
    futil.add_handler(args.command.preSelect, command_preselect, local_handlers=local_handlers)
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)

    inputs = args.command.commandInputs

    face_chain_input = inputs.addSelectionInput('chain', 'Selected Faces', 'Select the chamfered Faces to be patched.')
    # We need to allow only planar, cylindrical, and conical faces because those are the only ones
    # that will be from a valid chamfer and can be correctly patched.
    face_chain_input.selectionFilters = ['PlanarFaces', 'CylindricalFaces', 'ConicalFaces', 'SplineFaces']
    face_chain_input.setSelectionLimits(1, 0)

    # We need to give the user the option to wither create just a patch or splice it back into the model.
    # To do this we will create a radio group with two options.
    sew_option = inputs.addBoolValueInput('sew_mode', 'Sew into Solid', True, '', settings["option_checkbox"]["default"])

    permissive_input = inputs.addBoolValueInput('permissive', 'Permissive Mode', True, '', settings["permissive"]["default"])

def command_execute(args: adsk.core.CommandEventArgs):
    # General logging for debug
    futil.log(f'{CMD_NAME} Command Execute Event')
    inputs = args.command.commandInputs
    patch_faces(inputs.itemById('chain'), inputs.itemById('sew_mode').value)

# This function will be called when the command needs to compute a new preview in the graphics window
def command_preview(args: adsk.core.CommandEventArgs):
    inputs = args.command.commandInputs
    patch_faces(inputs.itemById('chain'), False)

def command_validateinputs(args: adsk.core.ValidateInputsEventArgs):
    # The only thing we are doing here is making sure that they are only selecting one body
    chain_selection: adsk.core.SelectionCommandInput = args.inputs.itemById('chain')
    if chain_selection.selectionCount > 1:
        # we will make a dict of the bodies that are selected with the number of faces that are selected on that body
        body_dict = {}
        for i in range(chain_selection.selectionCount):
            entityToken = chain_selection.selection(i).entity.body.entityToken
            if entityToken in body_dict:
                body_dict[entityToken] += 1
            else:
                body_dict[entityToken] = 1
        # then we will check to see if there is more than one body in the dict
        if len(body_dict) > 1:
            # get all the faces we want, and then add them to the selection
            # APIDUMB: There is no way to remove a selection from a selection command input but you can add to it and clear it
            max_body = max(body_dict, key=body_dict.get)
            good_faces = []
            for i in range(chain_selection.selectionCount):
                if chain_selection.selection(i).entity.body.entityToken == max_body:
                    good_faces.append(chain_selection.selection(i).entity)
            chain_selection.clearSelection()
            for i in range(len(good_faces)):
                chain_selection.addSelection(good_faces[i])

def command_preselect(args: adsk.core.SelectionEventArgs):
    if args.activeInput.id == 'chain':
        # if there are already faces selected then we have to make sure that the new selection is on the same body
        # first if there are no faces selected then we will allow the selection
        if args.activeInput.selectionCount == 0:
            args.isSelectable = True
        elif args.selection.entity is None:
            pass
        elif args.activeInput.selection(0).entity is None:
            pass
        elif args.activeInput.selection(0).entity.body == args.selection.entity.body:
            args.isSelectable = True
        else:
            args.isSelectable = False

# This function will be called when the user changes anything in the command dialog
# def command_input_changed(args: adsk.core.InputChangedEventArgs):
#     changed_input = args.input
#     inputs = args.inputs
#     futil.log(f'{CMD_NAME} Input Changed Event fired from a change to {changed_input.id}')

def patch_faces(selections: adsk.core.SelectionCommandInput, sew: bool):
    timer.mark('find_chains')
    tangent_faces = face_chain_finder(selections)
    # first we will find the loop around the boundary of the faces
    product = adsk.fusion.Design.cast(app.activeProduct)
    active_comp = product.activeComponent
    features = active_comp.features
    firstTLN: adsk.fusion.TimelineObject = None
    stitch_entities = adsk.core.ObjectCollection.create()
    unstitch_entities = adsk.core.ObjectCollection.create()
    faces_to_delete = []
    facess = []
    timer.mark('get_faces')
    for i, tf in enumerate(tangent_faces):
        timer.mark(f'get_faces:{i}')
        facess.append(get_faces(tf, selections))
    timer.mark('patch_faces')
    for i, faces in enumerate(facess):
        timer.mark(f'patch_faces:{i}')
        if len(faces) == 1:
            continue
        body, tln1, tln2 = patcher(faces, features)
        if firstTLN is None:
            firstTLN = tln1
        secondTLN = tln2
        # If we are going to sew the patch into the model then we need to add it to the list of bodies to be deleted
        stitch_entities.add(body)
        if sew:
            for j in range(len(faces)):
                unstitch_entities.add(faces[j])
                faces_to_delete.append(faces[j].entityToken)
    
    # if we are not going to sew then we are done
    if sew:
        # we know that all the faces are on the same body so we can just
        # unstitch the body, get the parts of the unstitched body we want, and then delete the faces that are selected
        # and then sew the body back together with the patch faces
        
        # now we will unstitch the body, by creating a unstitch feature
        timer.mark('unstitch')
        unstitch_features = active_comp.features.unstitchFeatures
        unstitch = unstitch_features.add(unstitch_entities, False)
        timer.mark('find_good_faces')
        for i in range(unstitch.bodies.count):
            # if this body has and faces in the list of faces to delete then we will not add it to the new body
            if unstitch.bodies.item(i).faces.item(0).entityToken in faces_to_delete:
                pass
            else:
                stitch_entities.add(unstitch.bodies.item(i))
        # now we will delete the faces that are selected
        timer.mark('delete_faces')
        delete_features = active_comp.features.deleteFaceFeatures
        delete_entities = adsk.core.ObjectCollection.create()
        for i in range(len(faces_to_delete)):
            ent_list = active_comp.parentDesign.findEntityByToken(faces_to_delete[i])
            delete_entities.add(ent_list[0])
        delete = delete_features.add(delete_entities)
        # now we will sew the body back together
        timer.mark('stitch')
        stitch_features = active_comp.features.stitchFeatures
        stitch_input = stitch_features.createInput(stitch_entities, adsk.core.ValueInput.createByString('0.1 mm'))
        stitch = stitch_features.add(stitch_input)
        secondTLN = stitch.timelineObject
        
    if firstTLN is not None and firstTLN.index != secondTLN.index:
        timer.mark('timeline')
        tgs: adsk.fusion.TimelineGroups = product.timeline.timelineGroups
        tg = tgs.add(firstTLN.index, secondTLN.index)

    timing = timer.finish()
    if config.DEBUG:
        futil.log(format_timer(timing))

def are_vectors_parallel(vector1: adsk.core.Vector3D, vector2: adsk.core.Vector3D, tol: float = 1e-6) -> bool:
    if abs(vector1.angleTo(vector2)) < tol:
        return True
    else:
        return False

# APIDUMB: There is no way to determine if two faces are tangent to each other, you can get all tangent faces and see if there is a intersection in the list but that is not very efficient
# like it takes like 0.3 sec per face or more to get the tangent faces, which makes the command unresponsive for a long time
def are_faces_tangent(face1: adsk.fusion.BRepFace, face2: adsk.fusion.BRepFace, edge: adsk.fusion.BRepEdge, permissive: bool = False) -> bool:
    # we are going to select a set of 3d points on the edge and determine the normal on each of the faces at the points
    # then we will compare and see of the normals are parallel within a certain tolerance
    # first we will get the 3d points on the edge
    points: List(adsk.core.Point3D) = []
    parameters = []
    pts = 11
    length = edge.length
    _, start_geom, end_geom = edge.evaluator.getParameterExtents()
    for i in range(pts):
        _, param = edge.evaluator.getParameterAtLength(start_geom, length*i/(pts - 1))
        parameters.append(param)
    parameters[0] += 1e-6
    parameters[-1] -= 1e-6
    good, points = edge.evaluator.getPointsAtParameters(parameters)
    # now we will get the normals on the faces at the points
    good1, normals1 = face1.evaluator.getNormalsAtPoints(points)
    good2, normals2 = face2.evaluator.getNormalsAtPoints(points)
    if not good1 or not good2 or not good:
        return False
    # now we will compare the normals
    tol = 1e-6
    if permissive:
        tol = 1e-2
    for i in range(pts):
        if not are_vectors_parallel(normals1[i], normals2[i], tol=tol):
            # futil.log(f'Normals are not parallel at point {i}')
            # futil.log(f'Normal 1: {normals1[i].asArray()}')
            # futil.log(f'Normal 2: {normals2[i].asArray()}')
            # futil.log(f'Angle: {math.degrees(normals1[i].angleTo(normals2[i]))}\t{normals1[i].angleTo(normals2[i])}')
            return False
    return True

# We must consolidate the faces into groups based on the chains of faces that are tangent to each other.
def face_chain_finder(selections: adsk.core.SelectionCommandInput):
    # first we will create a list of all the entity tokens for the faces
    face_tokens = []
    tangent_faces = []
    if selections.selectionCount <= 1:
        return [[0]]
    
    permissive = selections.parentCommand.commandInputs.itemById('permissive').value

    for i in range(selections.selectionCount):
        timer.mark(f'find_chains:nextedge{i}')
        face_tokens.append(selections.selection(i).entity.entityToken)
        # Then we will create a list of all the faces that are tangent to the given face
        valid_faces = []
        timer.mark(f'find_chains:nextedge{i}_neighbor')
        neighbor_edges: adsk.fusion.BRepEdges = selections.selection(i).entity.edges
        for j in range(neighbor_edges.count):
            edge_faces = neighbor_edges.item(j).faces
            for k in range(edge_faces.count):
                if edge_faces.item(k).entityToken != face_tokens[i]:
                    if are_faces_tangent(selections.selection(i).entity, edge_faces.item(k), neighbor_edges.item(j), permissive=permissive):
                        valid_faces.append(edge_faces.item(k).entityToken)
        # now we take the intersection of the two lists to get the faces that are tangent and neighbor faces
        tangent_faces.append(valid_faces)
    
    # Then we will create a list of all the unique lists of tangent faces
    unique_tangent_chains = [[face_tokens[0]]]
    for i in range(1, len(tangent_faces)):
        timer.mark(f'find_chains:unique_chains{i}')
        b = -1
        ind_to_pop = []
        for j in range(len(unique_tangent_chains)):
            if set(tangent_faces[i]).intersection(set(unique_tangent_chains[j])).__len__() > 0:
                if b != -1:
                    unique_tangent_chains[b].extend(unique_tangent_chains[j])
                    ind_to_pop.append(j)
                unique_tangent_chains[j].append(face_tokens[i])
                b = j
            elif j == len(unique_tangent_chains) - 1 and b == -1:
                unique_tangent_chains.append([face_tokens[i]])
        for j in range(len(ind_to_pop)):
            unique_tangent_chains.pop(ind_to_pop[j] - j)
    
    # also make a list of the index of the faces
    utc_inds = []
    for i in range(len(unique_tangent_chains)):
        timer.mark(f'find_chains:utc_inds{i}')
        utc_inds.append([])
        for j in range(len(unique_tangent_chains[i])):
            utc_inds[i].append(face_tokens.index(unique_tangent_chains[i][j]))
    return utc_inds

def get_faces(face_tokens: List[int], input: adsk.core.SelectionCommandInput) -> List[adsk.fusion.BRepFace]:
    faces = []
    for i in face_tokens:
        faces.append(input.selection(i).entity)
    return faces

def add_to_vertex_dict(vertex_dict, face: adsk.fusion.BRepFace):
    """Add the edge to the dictionary based on its start and end vertices."""
    for vertex in face.vertices:
        token = vertex.entityToken
        if token in vertex_dict:
            vertex_dict[token].append(face)
        else:
            vertex_dict[token] = [face]

def add_to_edge_dict(edge_dict, edict, face: adsk.fusion.BRepFace):
    """Add the face to the dictionary based on its edges."""
    for edge in face.edges:
        token = edge.entityToken
        edict[token] = edge
        if token in edge_dict:
            edge_dict[token].append(face)
        else:
            edge_dict[token] = [face]

def are_edges_connected(edge1: adsk.fusion.BRepEdge, edge2: adsk.fusion.BRepEdge) -> bool:
    if edge1.startVertex.entityToken == edge2.startVertex.entityToken or edge1.startVertex.entityToken == edge2.endVertex.entityToken or edge1.endVertex.entityToken == edge2.startVertex.entityToken or edge1.endVertex.entityToken == edge2.endVertex.entityToken:
        return True
    else:
        return False

def find_farthest_edge(face: adsk.fusion.BRepFace, next_face: adsk.fusion.BRepFace) -> adsk.fusion.BRepEdge:
    # first we must figure out what edge or edges are shared between the two faces
    shared_edges: List[adsk.fusion.BRepEdge] = []
    for i in range(face.edges.count):
        faces = face.edges.item(i).faces
        for j in range(faces.count):
            if faces.item(j).entityToken == next_face.entityToken:
                shared_edges.append(face.edges.item(i))
    # now we will find the edge that is farthest away from the shared edge
    farthest_edge = None
    farthest_dist = 0
    for i in range(face.edges.count):
        if face.edges.item(i) not in shared_edges:
            dists = []
            for j in range(len(shared_edges)):
                dists.append(face.edges.item(i).startVertex.geometry.distanceTo(shared_edges[j].startVertex.geometry))
                dists.append(face.edges.item(i).startVertex.geometry.distanceTo(shared_edges[j].endVertex.geometry))
                dists.append(face.edges.item(i).endVertex.geometry.distanceTo(shared_edges[j].startVertex.geometry))
                dists.append(face.edges.item(i).endVertex.geometry.distanceTo(shared_edges[j].endVertex.geometry))
            avg_dist = sum(dists)/len(dists)
            if avg_dist > farthest_dist:
                farthest_edge = face.edges.item(i)
                farthest_dist = avg_dist
    return farthest_edge


# Find the loop around the edge of a set of faces
def patcher(faces: List[adsk.fusion.BRepFace], features: adsk.fusion.Features) -> (adsk.fusion.BRepBody, adsk.fusion.TimelineObject, adsk.fusion.TimelineObject):
    edge_dict = {}
    edict = {}
    for face in faces:
        add_to_edge_dict(edge_dict, edict, face)
    the_faces = faces.copy()
    ordered_faces = [the_faces.pop(0)]
    we_ok = True
    while the_faces:
        last_face = ordered_faces[-1]
        matched_face = None
        for edge in last_face.edges:
            possible_faces = edge_dict.get(edge.entityToken, [])
            for face in possible_faces:
                if face != last_face and face in the_faces:  # Ensure we're not re-adding the same face
                    matched_face = face
                    break
            if matched_face:
                break
        if matched_face:
            we_ok = True
            ordered_faces.append(matched_face)
            the_faces.remove(matched_face)
        elif we_ok:
            # flip ordered faces and try again
            ordered_faces.reverse()
            we_ok = False
        else:
            futil.log(f'Failed to find a matching face for the last face. Something is very wrong.')
            break
    faces = ordered_faces

    interior_edges_id: Dict[adsk.fusion.BRepEdge] = {}
    exterior_edges_id: Dict[adsk.fusion.BRepEdge] = {}
    # all of the edged with more than one face attached to them are interior edges
    for key, val in edge_dict.items():
        if len(val) > 1:
            interior_edges_id[key] = edict[key]
        else:
            exterior_edges_id[key] = edict[key]

    # if the faces make a loop then we will need to make two loops and use a loft instead of a patch
    # we will check to see if the first and last faces are tangent to each other
    lofts = features.loftFeatures
    loft_input = lofts.createInput(adsk.fusion.FeatureOperations.NewBodyFeatureOperation)

    vertex_dict = {}
    for face in faces:
        add_to_vertex_dict(vertex_dict, face)

    boundary_edges_id_1: List[adsk.fusion.BRepEdge] = []
    boundary_edges_id_2: List[adsk.fusion.BRepEdge] = []
    # for the first face we want to seed the two boundary edges lists with the two edges that touch the first shared edge
    second_face_set = set([faces[1].vertices.item(j).entityToken for j in range(faces[1].vertices.count)])
    for i in range(faces[0].edges.count):
        if set([faces[0].edges.item(i).startVertex.entityToken, faces[0].edges.item(i).endVertex.entityToken]).intersection(second_face_set).__len__() > 0 and faces[0].edges.item(i).entityToken in exterior_edges_id.keys():
            if len(boundary_edges_id_1) == 0:
                boundary_edges_id_1.append(faces[0].edges.item(i))
            elif len(boundary_edges_id_2) == 0:
                boundary_edges_id_2.append(faces[0].edges.item(i))
            else:
                futil.log(f'Found too many seed edges for the first face.')

    # first we will find the edge that is farthest away from the shared edge for the first face
    farthest_edge = find_farthest_edge(faces[0], faces[1])
    # then we will find the edge that is farthest away from the shared edge for the last face
    farthest_edge2 = find_farthest_edge(faces[-1], faces[-2])

    # we will iterate through the faces and add the edges to the boundary edges list until we reach the first face again
    is_closed_set = (set([faces[0].vertices.item(i).entityToken for i in range(faces[0].vertices.count)]).intersection(set([faces[-1].vertices.item(i).entityToken for i in range(faces[-1].vertices.count)])).__len__() < 2 or len(faces) == 2)
    for i in range(len(faces)):
        edges_to_place: List[adsk.fusion.BRepFace] = []
        for j in range(faces[i].edges.count):
            if faces[i].edges.item(j).entityToken in exterior_edges_id.keys():
                # first we have to exit out if one of the corners of the line does not touch a corner of another face
                if (faces[i].edges.item(j) == farthest_edge or faces[i].edges.item(j) == farthest_edge2) and is_closed_set:
                    interior_edges_id[faces[i].edges.item(j).entityToken] = faces[i].edges.item(j)
                    continue
                if i != 0: # we are going to seed the boundary edges with the first face so we dont want to be deleting those
                    if faces[i].edges.item(j) in boundary_edges_id_1:
                        boundary_edges_id_1.remove(faces[i].edges.item(j))
                        interior_edges_id[faces[i].edges.item(j).entityToken] = faces[i].edges.item(j)
                        continue
                    elif faces[i].edges.item(j) in boundary_edges_id_2:
                        boundary_edges_id_2.remove(faces[i].edges.item(j))
                        interior_edges_id[faces[i].edges.item(j).entityToken] = faces[i].edges.item(j)
                        continue
                elif faces[i].edges.item(j) in boundary_edges_id_1 or faces[i].edges.item(j) in boundary_edges_id_2: # however we do want to skip the seeded edges if we are on the first face
                    continue
                edges_to_place.append(faces[i].edges.item(j))
        while edges_to_place:
            for edge in edges_to_place:
                if are_edges_connected(boundary_edges_id_1[-1], edge):
                    boundary_edges_id_1.append(edge)
                    edges_to_place.remove(edge)
                elif are_edges_connected(boundary_edges_id_1[0], edge):
                    boundary_edges_id_1.insert(0, edge)
                    edges_to_place.remove(edge)
                elif are_edges_connected(boundary_edges_id_2[-1], edge):
                    boundary_edges_id_2.append(edge)
                    edges_to_place.remove(edge)
                elif are_edges_connected(boundary_edges_id_2[0], edge):
                    boundary_edges_id_2.insert(0, edge)
                    edges_to_place.remove(edge)
                else:
                    futil.log(f'Failed to find a matching edge for the last edge.')
    
    # if the face is not a loop then we will have remove one edge from each end of the surface
    # first we have to find the edge for each of the ending faces that is the farthest away from the edge shared with the next face

    # fires we will make a ObjectCollection of the boundary edges
    boundary_edges_1 = adsk.core.ObjectCollection.create()
    for i in boundary_edges_id_1:
        boundary_edges_1.add(i)
    # now we will find the loop around the boundary edges
    path_1 = adsk.fusion.Path.create(boundary_edges_1, adsk.fusion.ChainedCurveOptions.connectedChainedCurves)
    boundary_edges_2 = adsk.core.ObjectCollection.create()
    for i in boundary_edges_id_2:
        boundary_edges_2.add(i)
    # now we will find the loop around the boundary edges
    path_2 = adsk.fusion.Path.create(boundary_edges_2, adsk.fusion.ChainedCurveOptions.connectedChainedCurves)
    # now we will loft the two paths
    loft_input.loftSections.add(path_1)
    loft_input.loftSections.add(path_2)
    for edge in interior_edges_id.values():
        loft_input.centerLineOrRails.addRail(edge)
    
    loft_input.isSolid = False
    loft = lofts.add(loft_input)
    return loft.bodies.item(0), loft.timelineObject, loft.timelineObject


# This event handler is called when the command terminates.
def command_destroy(args: adsk.core.CommandEventArgs):
    global local_handlers
    local_handlers = []
    futil.log(f'{CMD_NAME} Command Destroy Event')