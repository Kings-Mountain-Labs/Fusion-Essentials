# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Reduces a solid tool-holder body to a CAM library holder profile (height/diameter segments along
an axis of revolution) - the headless core behind model_compute_holder."""

import json
import math
import random
import time
from typing import List

import adsk.core
import adsk.fusion
import adsk.cam

from ._common import safe
from ._cam_common import library_assets   # the ONE bounded CAM library folder walk

MAP_BLURB = "holder geometry: get_axis, get_tool_profile, build_holder_data, get_tooling_libraries"


def get_axis(axis_base):
    """An InfiniteLine3D axis of rotation from a cylindrical/conical/toroidal FACE, a linear EDGE, or
    a construction axis; None if the entity can't define one."""
    if isinstance(axis_base, adsk.fusion.BRepFace):
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
    elif isinstance(axis_base, adsk.fusion.BRepEdge):
        edge_type = axis_base.geometry.curveType
        if edge_type == adsk.core.Curve3DTypes.Line3DCurveType:
            line = adsk.core.Line3D.cast(axis_base.geometry)
            return line.asInfiniteLine()
    elif isinstance(axis_base, adsk.fusion.ConstructionAxis):
        return axis_base.geometry
    return None


def is_valid_axial_datum(surface, axis):
    """The Point3D where the end datum (planar face normal to the axis, perpendicular linear edge, or
    vertex) meets `axis` and pins z=0; None if the datum isn't valid for this axis."""
    if isinstance(surface, adsk.fusion.BRepFace):
        face_type = surface.geometry.surfaceType
        if face_type == adsk.core.SurfaceTypes.PlaneSurfaceType:
            plane = adsk.core.Plane.cast(surface.geometry)
            return plane.intersectWithLine(axis)
    elif isinstance(surface, adsk.fusion.BRepEdge):
        edge_type = surface.geometry.curveType
        normal, center = None, None
        if edge_type == adsk.core.Curve3DTypes.Line3DCurveType:
            line = adsk.core.Line3D.cast(surface.geometry)
            # the edge must be orthogonal to the axis
            if axis.direction.dotProduct(line.asInfiniteLine().direction) != 0:
                return None
            plane = adsk.core.Plane.create(line.startPoint, axis.direction)
            return plane.intersectWithLine(axis)
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
        point = adsk.core.Point3D.cast(surface.geometry)
        plane = adsk.core.Plane.create(point, axis.direction)
        return plane.intersectWithLine(axis)
    return None


def get_tool_profile(body, axis, plane_intersect):
    """Reduce a holder body to a turned PROFILE: [z0, z1, r0, r1] segments along the axis, z=0 at
    `plane_intersect`, occluded and duplicate segments removed."""
    plane = adsk.core.Plane.create(plane_intersect, axis.direction)
    points = []
    for edge in body.edges:
        points.append(edge.startVertex.geometry)
        points.append(edge.endVertex.geometry)
    cylindrical_points = []
    for point in points:
        cylindrical_points.append(get_cylindrical_coordinates_point(point, axis, plane))

    # coaxial cone/cylinder/torus faces only (planar faces are inherently axis-parallel; skip them)
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

    # each face -> a (r_low, r_high, z_low, z_high) segment from its extreme coaxial edges
    face_segments = []
    for face in useful_faces:
        valid_edges = []
        for edge in face.edges:
            pt = get_cylindrical_coordinates_edge(edge, axis, plane)
            if pt is not None:
                valid_edges.append(pt)
        if len(valid_edges) >= 2:
            valid_edges.sort(key=lambda x: x[1])
            z_1 = valid_edges[0][1]
            z_2 = valid_edges[-1][1]
            r_1 = valid_edges[0][0]
            r_2 = valid_edges[-1][0]
            face_segments.append((r_1, r_2, z_1, z_2))

    # drop exact-duplicate segments
    ind_to_pop = []
    face_segments.sort(key=lambda x: x[2])
    for i in range(0, len(face_segments) - 1):
        if (abs(face_segments[i][0] - face_segments[i + 1][0]) < 1e-8
                and abs(face_segments[i][1] - face_segments[i + 1][1]) < 1e-8
                and abs(face_segments[i][2] - face_segments[i + 1][2]) < 1e-8
                and abs(face_segments[i][3] - face_segments[i + 1][3]) < 1e-8):
            ind_to_pop.append(i)
    for i in range(0, len(ind_to_pop)):
        face_segments.pop(ind_to_pop[i] - i)

    profile = []
    for seg in face_segments:
        profile.append((seg[0], seg[2], 1))
        profile.append((seg[1], seg[3], 0))
    profile.sort(key=lambda x: x[1])
    profile = filter_points(profile)

    # remove points occluded by a larger coaxial segment spanning the same z
    for segment in face_segments:
        ind_to_pop = []
        for i in range(0, len(profile)):
            if profile[i][1] >= segment[3] - 1e-8 or profile[i][1] <= segment[2] + 1e-8:
                continue
            elif profile[i][0] < (((segment[1] - segment[0]) / (segment[3] - segment[2]))
                                  * (segment[2] - profile[i][1]) + segment[0]):
                ind_to_pop.append(i)
        for i in range(0, len(ind_to_pop)):
            profile.pop(ind_to_pop[i] - i)

    profile_points = []
    for i in range(0, len(profile) - 1):
        if abs(profile[i][1] - profile[i + 1][1]) < 1e-8:
            continue
        profile_points.append([profile[i][1], profile[i + 1][1], profile[i][0], profile[i + 1][0]])

    return profile_points


def filter_points(points):
    """Group profile points by z (within 1e-8), keeping the two largest-radius per z."""
    # min() over the empty grouping below raises, and a body with no coaxial faces reaches here empty.
    if not points:
        return []
    grouped_points = {}
    for x, y, z in points:
        rounded_y = round(y, 8)
        if rounded_y not in grouped_points:
            grouped_points[rounded_y] = []
        grouped_points[rounded_y].append((x, y, z))

    filtered_points = []
    min_key = min(grouped_points.keys())
    for key, group in grouped_points.items():
        sorted_group = sorted(group, key=lambda p: p[0], reverse=True)
        if key == min_key:
            filtered_points.append(sorted_group[0])
        else:
            filtered_points.extend(sorted_group[:2])
    filtered_points.sort(key=lambda p: (p[1], p[2]))
    return filtered_points


def get_cylindrical_coordinates_edge(edge, axis, plane):
    """(radius, z) of a circular/arc edge whose normal is coaxial with `axis`, else None."""
    if edge.geometry is None:
        return None
    edge_type = edge.geometry.curveType
    if (edge_type != adsk.core.Curve3DTypes.Circle3DCurveType
            and edge_type != adsk.core.Curve3DTypes.Arc3DCurveType):
        return None
    point = edge.geometry.center
    normal = edge.geometry.normal
    if not axis.isColinearTo(adsk.core.InfiniteLine3D.create(point, normal)):
        return None
    line = adsk.core.InfiniteLine3D.create(point, axis.direction)
    intersect = plane.intersectWithLine(line)
    z = point.distanceTo(intersect)
    return (edge.geometry.radius, z)


def get_cylindrical_coordinates_point(point, axis, plane):
    """(radius from axis, z along axis from `plane`) for a world point."""
    line = adsk.core.InfiniteLine3D.create(point, axis.direction)
    intersect = plane.intersectWithLine(line)
    z = point.distanceTo(intersect)
    r = point.distanceTo(axis.origin)
    return (r, z)


def build_holder_data(profile, desc, prodid="", prodlink="", prodvendor=""):
    """The holder library JSON dict for a profile: profile lengths are cm (the API unit), segment
    heights and diameters are emitted in mm, and a fresh guid + timestamp are minted per call."""
    guid = "00000000-0000-0000-0000-" + str(random.randint(100000000000, 999999999999))
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
        "vendor": prodvendor,
    }
    for segment in profile:
        seg = {
            "height": round((segment[1] - segment[0]) * 10, 3),
            "lower-diameter": round(segment[2] * 10 * 2, 3),
            "upper-diameter": round(segment[3] * 10 * 2, 3),
        }
        data["segments"].append(seg)
    return data


def generate_tool(profile, desc, prodid="", prodlink="", prodvendor=""):
    """An adsk.cam.Tool (type='holder') built from a profile + metadata, via Tool.createFromJson."""
    data = build_holder_data(profile, desc, prodid, prodlink, prodvendor)
    return adsk.cam.Tool.createFromJson(json.dumps(data))


_LIBRARY_LOCATIONS = ("CloudLibraryLocation", "LocalLibraryLocation", "ExternalLibraryLocation")


def get_tooling_libraries() -> List:
    """URLs of every cloud + local + external tool library, through the shared bounded walk."""
    toolLibraries = adsk.cam.CAMManager.get().libraryManager.toolLibraries
    urls = []
    for location in _LIBRARY_LOCATIONS:
        loc = getattr(adsk.cam.LibraryLocations, location, None)
        if loc is None:
            continue
        root = toolLibraries.urlByLocation(loc)
        assets, _truncated = library_assets(toolLibraries, root)
        urls.extend(safe(lambda a=a: a.toString()) for a in assets)
    return urls
