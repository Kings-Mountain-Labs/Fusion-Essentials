#  Copyright 2023 by Ian Rist

import adsk.core, adsk.fusion
import os
from ...lib import fusion360utils as futil
from ... import config
from typing import List

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
    futil.add_handler(args.command.destroy, command_destroy, local_handlers=local_handlers)

    inputs = args.command.commandInputs

    face_chain_input = inputs.addSelectionInput('chain', 'Selected Faces', 'Select the chamfered Faces to be patched.')
    # We need to allow only planar, cylindrical, and conical faces because those are the only ones
    # that will be from a valid chamfer and can be correctly patched.
    face_chain_input.selectionFilters = ['PlanarFaces', 'CylindricalFaces', 'ConicalFaces']
    face_chain_input.setSelectionLimits(1, 0)

    # We need to give the user the option to wither create just a patch or splice it back into the model.
    # To do this we will create a radio group with two options.
    sew_option = inputs.addBoolValueInput('sew_mode', 'Sew into Solid', True, '', True)

def command_execute(args: adsk.core.CommandEventArgs):
    # General logging for debug
    futil.log(f'{CMD_NAME} Command Execute Event')
    inputs = args.command.commandInputs
    patch_faces(inputs.itemById('chain'), inputs.itemById('sew_mode').value)

# This function will be called when the command needs to compute a new preview in the graphics window
def command_preview(args: adsk.core.CommandEventArgs):
    inputs = args.command.commandInputs
    patch_faces(inputs.itemById('chain'), inputs.itemById('sew_mode').value)

# This function will be called when the user changes anything in the command dialog
def command_input_changed(args: adsk.core.InputChangedEventArgs):
    changed_input = args.input
    inputs = args.inputs
    futil.log(f'{CMD_NAME} Input Changed Event fired from a change to {changed_input.id}')

def patch_faces(selections: adsk.core.SelectionCommandInput, sew: bool):
    tangent_faces = face_chain_finder(selections)
    # first we will find the loop around the boundary of the faces
    product = adsk.fusion.Design.cast(app.activeProduct)
    active_comp = product.activeComponent
    patches = active_comp.features.patchFeatures
    firstTLN: adsk.fusion.TimelineObject = None
    faces_to_delete = []
    facess = []
    for i in tangent_faces:
        facess.append(get_faces(i, selections))
    for faces in facess:
        loop = loop_finder(faces)
        patch_input = patches.createInput(loop, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        patch = patches.add(patch_input)
        if firstTLN is None:
            firstTLN = patch.timelineObject
        secondTLN = patch.timelineObject
        # If we are going to sew the patch into the model then we need to add it to the list of bodies to be deleted
        if sew:
            for j in range(len(faces)):
                faces_to_delete.append(faces[j].entityToken)
    
    # if we are not going to sew then we are done
    if sew:
        pass
    if len(tangent_faces) > 1 or sew:
        tgs: adsk.fusion.TimelineGroups = product.timeline.timelineGroups
        tg = tgs.add(firstTLN.index, secondTLN.index)


# We must consolidate the faces into groups based on the chains of faces that are tangent to each other.
def face_chain_finder(faces: adsk.core.SelectionCommandInput):
    # first we will create a list of all the entity tokens for the faces
    face_tokens = []
    tangent_faces = []
    if faces.selectionCount == 1:
        return [[faces.selection(0).entity.entityToken]], [[0]]
    for i in range(faces.selectionCount):
        face_tokens.append(faces.selection(i).entity.entityToken)
        # Then we will create a list of all the faces that are tangent to the given face
        tan_faces = []
        tan_face = faces.selection(i).entity.tangentiallyConnectedFaces
        for j in range(tan_face.count):
            tan_faces.append(tan_face.item(j).entityToken)
        tangent_faces.append(tan_faces)
    
    # Then we will create a list of all the unique lists of tangent faces
    unique_tangent_chains = [[face_tokens[0]]]
    for i in range(1, len(tangent_faces)):
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
        utc_inds.append([])
        for j in range(len(unique_tangent_chains[i])):
            utc_inds[i].append(face_tokens.index(unique_tangent_chains[i][j]))

    return utc_inds

def get_faces(face_tokens: List[int], input: adsk.core.SelectionCommandInput) -> List[adsk.fusion.BRepFace]:
    faces = []
    for i in face_tokens:
        faces.append(input.selection(i).entity)
    return faces

# Find the loop around the edge of a set of faces
def loop_finder(faces: List[adsk.fusion.BRepFace]) -> adsk.fusion.Path:
    # first we will find all the boundary edges
    # this can be done by adding every edge to a list and then removing the edges that are shared by two faces
    # the remaining edges will be the boundary edges
    boundary_edges_id: List[adsk.fusion.BRepEdge] = []
    for i in range(len(faces)):
        for j in range(faces[i].edges.count):
            if faces[i].edges.item(j) in boundary_edges_id:
                boundary_edges_id.remove(faces[i].edges.item(j))
            else:
                boundary_edges_id.append(faces[i].edges.item(j))

    # order the edges into a loop
    # we will start with the first edge and then find the edge that is connected to it
    # then we will find the edge that is connected to that edge and so on until we reach the first edge again
    ordered_edges_id: List[adsk.fusion.BRepEdge] = []
    ordered_edges_id.append(boundary_edges_id[0])
    boundary_edges_id.pop(0)
    while len(boundary_edges_id) > 0:
        beg = len(ordered_edges_id)
        for i in range(len(boundary_edges_id)):
            if boundary_edges_id[i].startVertex.entityToken == ordered_edges_id[-1].endVertex.entityToken:
                ordered_edges_id.append(boundary_edges_id[i])
                boundary_edges_id.pop(i)
                break
            elif boundary_edges_id[i].endVertex.entityToken == ordered_edges_id[-1].endVertex.entityToken:
                ordered_edges_id.append(boundary_edges_id[i])
                boundary_edges_id.pop(i)
                break
            elif boundary_edges_id[i].startVertex.entityToken == ordered_edges_id[-1].startVertex.entityToken:
                ordered_edges_id.append(boundary_edges_id[i])
                boundary_edges_id.pop(i)
                break
            elif boundary_edges_id[i].endVertex.entityToken == ordered_edges_id[-1].startVertex.entityToken:
                ordered_edges_id.append(boundary_edges_id[i])
                boundary_edges_id.pop(i)
                break
        if beg == len(ordered_edges_id):
            futil.log('something went wrong')
            break

    # fires we will make a ObjectCollection of the boundary edges
    boundary_edges = adsk.core.ObjectCollection.create()
    for i in range(len(ordered_edges_id)):
        boundary_edges.add(ordered_edges_id[i])
    # now we will find the loop around the boundary edges
    path = adsk.fusion.Path.create(boundary_edges, adsk.fusion.ChainedCurveOptions.connectedChainedCurves)
    return path


# This event handler is called when the command terminates.
def command_destroy(args: adsk.core.CommandEventArgs):
    global local_handlers
    local_handlers = []
    futil.log(f'{CMD_NAME} Command Destroy Event')