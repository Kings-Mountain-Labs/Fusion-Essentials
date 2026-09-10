# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""ACT rows: the vise that holds the machined part, and the joint bench beside it.

The billet, the fixed and moving jaws and the lead screw that drives them, jointed and driven live
with the grip proven by measurement rather than by the joint calls returning ok; then the bench
that gives every other joint motion and assembly verb a rig of its own.
"""

from verify_core import (
    _RECALL, _as_built, _box, _captured, _constrained, _ctx_get, _datum_plane, _driven_angle,
    _driven_slide, _dwell, _extruded, _fg, _grounded, _interference_measured, _joint_bench,
    _joint_is, _joint_limits, _jointed, _jointed_at_geometry, _joints_listed,
    _made_component, _measured, _mod360, _motion_linked, _moved_occurrence, _near, _num, _recall,
    _refused, _revolved, _rigid_grouped, _watch)


# --- ACT 7b: THE MOTION BENCH - every joint and assembly verb on rigs of its own ---------------
# The vise act ahead of this one is the STORY's mechanism; these rows are the rest of the
# vocabulary, each on a scratch rig so nothing here can disturb the part in its fixture.
_MOTION = (
    # A rigid group on two scratch boxes. Its NAME is kept: the vise act ahead of this one built a
    # rigid group and a motion link of its own, so the lifecycle below has to address the relation
    # it created rather than whichever one the design lists first.
    _box("GrpA", ox=1400, tint="#5E6AD2") + _box("GrpB", ox=1440, tint="#8A94A6")
    + [
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("assembly_rigid_group", {"occurrences": ["GrpA:1", "GrpB:1"]}, _rigid_grouped(2),
     ("bench_group", _recall("bench_group", lambda p: p["assembly_rigid_group"]))),
    # a real cylinder-face joint on a scratch pin/bore cameo pair.
    ("model_create_component", {"name": "PinCameo", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "PinS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 300, "cy": 0, "radius": 5}],
                             "sketch_name": "PinS"}, "ok", None),
    ("model_extrude", {"sketch_name": "PinS", "profile_index": 0, "distance": 20}, _extruded, None),
    ("model_create_component", {"name": "BoreCameo", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "BoreS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 300, "cy": 0, "radius": 8}],
                             "sketch_name": "BoreS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 300, "cy": 0, "radius": 5.5}],
                             "sketch_name": "BoreS"}, "ok", None),
    ("sketch_get", {"sketch_name": "BoreS"}, "ok", ("bore_ring", lambda p: p["profiles"][-1]["handle"])),
    ("model_extrude", lambda c: {"sketch_name": "BoreS", "profile_index": _ctx_get(c, "bore_ring", "bore ring"), "distance": 20}, _extruded, None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    _watch("BoreCameo:1"),
    ("find_geometry", {"target": "PinCameo", "kind": "cylinder_face", "max_results": 1}, "ok", _fg("pin_cyl")),
    ("find_geometry", {"target": "BoreCameo", "kind": "cylinder_face", "radius": 5.5, "max_results": 1}, "ok", _fg("bore_cyl")),
    ("joint_at_geometry", lambda c: {"handle_one": _ctx_get(c, "pin_cyl", "pin face"), "handle_two": _ctx_get(c, "bore_cyl", "bore face"), "motion": "revolute"}, _jointed_at_geometry, None),
    # THE MOTION VOCABULARY at the geometry seam - a ball on a real SPHERE face, an explicit frame
    # axis, and rigid. Each beat takes its own free cameo pair, chained as a TREE (sphere - post -
    # post), so no beat closes a loop on another's joint. The sphere is built through the surface
    # family the same way the fill cameo builds one: a half-disc arc revolved into a closed sheet and
    # sealed solid, which is what gives this beat a genuine SphereSurfaceType face to joint at (a
    # sphere face takes ONLY CenterKeyPoint - the rule inside _joints.build_joint_geometry).
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("model_create_component", {"name": "BallSphere", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xz", "name": "BallProf"}, "ok", None),
    # the arc's endpoints must sit ON the revolve axis (x=0) or the revolved surface is an open tube
    # enclosing nothing; on an xz sketch +Y maps to world -Z, so this sphere sits alone at z=+300.
    ("sketch_add_geometry", {"geometry": [{"kind": "arc", "cx": 0, "cy": -300, "x1": 0,
                                           "y1": -294, "sweep_deg": 180}],
                             "sketch_name": "BallProf"}, "ok", None),
    ("surface_revolve", {"sketch_name": "BallProf", "axis": "z", "angle_deg": 360}, "ok", None),
    ("surface_fill", {"tools": ["BallSphere"], "operation": "new"},
     lambda p: p.get("all_solid") is True, None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("model_create_component", {"name": "BallPost", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "BallPostS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 1700, "cy": 0, "radius": 5}],
                             "sketch_name": "BallPostS"}, "ok", None),
    ("model_extrude", {"sketch_name": "BallPostS", "profile_index": 0, "distance": 20}, _extruded, None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("model_create_component", {"name": "AxisPost", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "AxisPostS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 1700, "cy": 60, "radius": 5}],
                             "sketch_name": "AxisPostS"}, "ok", None),
    ("model_extrude", {"sketch_name": "AxisPostS", "profile_index": 0, "distance": 20}, _extruded, None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("model_create_component", {"name": "RigidPost", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "RigidPostS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 1700, "cy": 120, "radius": 5}],
                             "sketch_name": "RigidPostS"}, "ok", None),
    ("model_extrude", {"sketch_name": "RigidPostS", "profile_index": 0, "distance": 20}, _extruded, None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    _watch("BallPost:1"),
    ("find_geometry", {"target": "BallSphere", "kind": "sphere_face", "max_results": 1}, "ok",
     _fg("ball_face")),
    ("find_geometry", {"target": "BallPost", "kind": "cylinder_face", "max_results": 1}, "ok",
     _fg("ball_post_cyl")),
    ("find_geometry", {"target": "AxisPost", "kind": "cylinder_face", "max_results": 1}, "ok",
     _fg("axis_post_cyl")),
    ("find_geometry", {"target": "RigidPost", "kind": "cylinder_face", "max_results": 1}, "ok",
     _fg("rigid_post_cyl")),
    # the ball seats the sphere's CENTRE on the post's own key point, and the label proves which
    # key point the sphere face resolved to. A ball joint reads no axis at all, so 'axis' is null
    # and the note carries NO axis sentence - not the frame-axis caveat, not the derived-axis one.
    ("joint_at_geometry", lambda c: {"handle_one": _ctx_get(c, "ball_face", "the sphere face"),
                                     "handle_two": _ctx_get(c, "ball_post_cyl", "the ball post wall"),
                                     "motion": "ball", "name": "BallSeat"},
     lambda p: p.get("jointed") is True and p.get("geometry_one") == "sphere_face@center"
     and p.get("axis") is None
     and "FRAME's" not in (p.get("note") or "") and "world_axis=" not in (p.get("note") or "")
     and "derived the motion axis" not in (p.get("note") or ""), None),
    # an EXPLICIT axis is frame-relative, and the note says so in the words that stop a caller
    # reading it as a world axis - plus the one tool that does set a true world axis.
    ("joint_at_geometry", lambda c: {"handle_one": _ctx_get(c, "ball_post_cyl", "the ball post wall"),
                                     "handle_two": _ctx_get(c, "axis_post_cyl", "the axis post wall"),
                                     "motion": "revolute", "axis": "y", "name": "FrameAxisSpin"},
     lambda p: "FRAME's y axis, NOT world y" in (p.get("note") or "")
     and "joint_edit(world_axis=" in (p.get("note") or ""), None),
    # an axis outside the Choice is refused by name, listing what the input carries - nothing built.
    ("joint_at_geometry", lambda c: {"handle_one": _ctx_get(c, "ball_post_cyl", "the ball post wall"),
                                     "handle_two": _ctx_get(c, "axis_post_cyl", "the axis post wall"),
                                     "motion": "revolute", "axis": "diagonal"}, "refused", None),
    # rigid has no motion to aim, so it too publishes a null axis and an axis-free note.
    ("joint_at_geometry", lambda c: {"handle_one": _ctx_get(c, "ball_post_cyl", "the ball post wall"),
                                     "handle_two": _ctx_get(c, "rigid_post_cyl", "the rigid post wall"),
                                     "motion": "rigid", "name": "PostLock"},
     lambda p: p.get("jointed") is True and p.get("axis") is None
     and "FRAME's" not in (p.get("note") or "")
     and "derived the motion axis" not in (p.get("note") or ""), None),
    # NEW-1: the TORUS keypoint gate. createByNonPlanarFace(torus, CenterKeyPoint) is measured
    # correct on a PARAMETRIC torus and silently WRONG inside a base feature (it hands back the
    # owning component's origin with no error), so the tool compares the keypoint against the
    # torus's own centre in the same world frame. This beat is the parametric side: the joint lands
    # and the payload names the key point it resolved to. (The two base-feature halves need a torus
    # built INSIDE a base feature; no tool on this surface builds one unattended - see STORY.)
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("model_create_component", {"name": "TorusRing", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xz", "name": "TorusProf"}, "ok", None),
    # on an xz sketch +Y maps to world -Z: this circle sits at world (40, 0, 400) and revolving it
    # about z sweeps a torus of major radius 40 centred on the z axis at z=400, alone up there.
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 40, "cy": -400, "radius": 6}],
                             "sketch_name": "TorusProf"}, "ok", None),
    ("model_revolve", {"sketch_name": "TorusProf", "profile_index": 0, "axis": "z",
                       "angle_deg": 360}, _revolved, None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("model_create_component", {"name": "TorusPost", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "TorusPostS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 1700, "cy": 180, "radius": 5}],
                             "sketch_name": "TorusPostS"}, "ok", None),
    ("model_extrude", {"sketch_name": "TorusPostS", "profile_index": 0, "distance": 20}, _extruded, None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("find_geometry", {"target": "TorusRing", "kind": "torus_face", "max_results": 1}, "ok",
     _fg("torus_face")),
    ("find_geometry", {"target": "TorusPost", "kind": "cylinder_face", "max_results": 1}, "ok",
     _fg("torus_post_cyl")),
    ("joint_at_geometry", lambda c: {"handle_one": _ctx_get(c, "torus_face", "the torus face"),
                                     "handle_two": _ctx_get(c, "torus_post_cyl",
                                                            "the torus post wall"),
                                     "motion": "rigid", "name": "TorusSeat"},
     lambda p: p.get("jointed") is True and p.get("geometry_one") == "torus_face@center", None),
    # A NON-RIGID as-built joint, on its own far-grid pair: an as-built joint moves nothing, so the
    # plate is built already seated on the pin's top face (z=20) and jointed where it stands. The
    # anchor is that shared face, reached by the pin's 'top' snap.
    ("model_create_component", {"name": "AsbPin", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "AsbPinS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 800, "y1": 300,
                                           "x2": 820, "y2": 320}],
                             "sketch_name": "AsbPinS"}, "ok", None),
    ("model_extrude", {"sketch_name": "AsbPinS", "profile_index": 0, "distance": 20}, _extruded, None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("model_create_component", {"name": "AsbPlate", "activate": True}, _made_component, None),
    ("model_construction", {"kind": "plane", "plane": "xy", "offset": 20, "name": "AsbSeat"},
     _datum_plane("xy"), None),
    ("sketch_create", {"plane": "AsbSeat", "name": "AsbPlateS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 790, "y1": 290,
                                           "x2": 830, "y2": 330}],
                             "sketch_name": "AsbPlateS"}, "ok", None),
    ("model_extrude", {"sketch_name": "AsbPlateS", "profile_index": 0, "distance": 10}, _extruded, None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    _watch("AsbPlate:1"),
    # the motion is read back off the CREATED joint, and the resolved anchor is named in the payload.
    # 'name' rides the same create: AsBuiltJoints.createInput/add take no name, so it is applied
    # post-create and READ BACK - 'joint' is what the browser shows, never an echo.
    ("joint_create_as_built", {"occurrence_one": "AsbPin:1", "occurrence_two": "AsbPlate:1",
                               "geometry": "AsbPin:1:top", "joint_type": "revolute", "axis": "z",
                               "name": "AsbNamed"},
     lambda p: p.get("joint_type") == "revolute" and bool(p.get("geometry"))
     and p.get("joint") == "AsbNamed",
     ("asb_joint", lambda p: p["joint"])),
    # an INDEPENDENT read of the same joint: the tool's own read-back is not the only witness.
    ("assembly_get", {},
     lambda p: any(j.get("type") == "revolute"
                   and {j.get("occurrence_one"), j.get("occurrence_two")} == {"AsbPin:1", "AsbPlate:1"}
                   for j in (p.get("joints") or [])), None),
    # the beat the read-backs cannot fake: a motion that reads back but cannot be DRIVEN is no DOF.
    ("joint_drive", lambda c: {"joint_name": _ctx_get(c, "asb_joint", "the as-built revolute"),
                               "angle_deg": 30},
     lambda p: abs(p.get("value_now", {}).get("angle_deg", 0) - 30) < 0.5, None),
    ("joint_drive", lambda c: {"joint_name": _ctx_get(c, "asb_joint", "the as-built revolute"),
                               "angle_deg": 0}, _driven_angle(0), None),
    # Fusion refuses a non-rigid as-built joint with a null geometry, so the tool names the missing
    # anchor instead of letting add() raise.
    ("joint_create_as_built", {"occurrence_one": "AsbPin:1", "occurrence_two": "AsbPlate:1",
                               "joint_type": "revolute"}, "refused", None),
    # a rigid as-built joint IGNORES a geometry it is handed, so the pairing is refused rather than
    # accepted and dropped.
    ("joint_create_as_built", {"occurrence_one": "AsbPin:1", "occurrence_two": "AsbPlate:1",
                               "joint_type": "rigid", "geometry": "AsbPin:1:top"}, "refused", None),
    # an AsBuiltJoint exposes NO offset/angle ModelParameter for ANY motion - both parametric-drive
    # refusals name AS-BUILT and route to joint_create instead of the dead-end generic wording.
    ("joint_edit", lambda c: {"joint_name": _ctx_get(c, "asb_joint", "the as-built revolute"),
                              "offset": 5}, _refused("AS-BUILT", "joint_create"), None),
    ("joint_edit", lambda c: {"joint_name": _ctx_get(c, "asb_joint", "the as-built revolute"),
                              "angle": 30}, _refused("AS-BUILT", "joint_create"), None),
    # a SECOND as-built joint on an already-jointed pair is refused by the platform at add()
    # ("System will be over constrained") - measured; the tool surfaces it, never a false ok.
    ("joint_create_as_built", {"occurrence_one": "AsbPin:1", "occurrence_two": "AsbPlate:1",
                               "joint_type": "rigid"},
     _refused("over constrained"), None),
    # THE JOINT BENCH: one grounded base, a STATION for every motion type spaced along it, and a
    # flag-shaped indicator arm at each. The arm shape is the point - a disc turning about its own
    # axis shows nothing, so every station carries a bar whose far end reads its position at a
    # glance, and the stations are spread along the base so the seven motions stand side by side
    # instead of on top of each other. Each arm is drawn OFF the base and its joint carries it to
    # its station, so the mate itself is visible; the drive pass below then moves the ones that
    # have a degree of freedom, which is the only way a motion type can be told from a label.
    ("model_create_component", {"name": "JointBase", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "JBaseS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 860, "y1": 300,
                                           "x2": 1190, "y2": 340}],
                             "sketch_name": "JBaseS"}, "ok", None),
    ("model_extrude", {"sketch_name": "JBaseS", "profile_index": 0, "distance": 10},
     _extruded, None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("appearance_set", {"target": "JointBase", "color": "#37474F"}, "ok", None),
    # GROUNDED TO PARENT - the base is the fixed frame every station's motion is read against. An
    # arm that moved because the base drifted would read as the joint working.
    ("assembly_ground", {"occurrence": "JointBase:1", "ground_to_parent": True}, _grounded, None),
] + _joint_bench() + [
    # LIMITS on the revolute station, set AFTER the bench's own pass has swung it 90 deg and put it
    # back. Every published limit is read BACK off the live JointLimits, so an empty
    # 'limits_unverified' beside real numbers is the write confirmed.
    ("joint_edit", {"joint_name": "JRev", "min_deg": -45, "max_deg": 45},
     _joint_limits("JRev", min_deg=-45, max_deg=45), None),
    # THE RETYPE, on a station that is already visible: one joint walked through every motion the
    # tool carries, each retype witnessed by the design's own joint walk rather than by the writer,
    # which publishes the type it was ASKED for. It ends back on the revolute it started as.
    ("joint_edit", {"joint_name": "JRig", "joint_type": "revolute", "axis": "z"}, "ok", None),
    ("assembly_get", {}, _joint_is("JRig", "revolute"), None),
    ("joint_edit", {"joint_name": "JRig", "joint_type": "slider", "axis": "x"}, "ok", None),
    ("assembly_get", {}, _joint_is("JRig", "slider"), None),
    ("joint_edit", {"joint_name": "JRig", "joint_type": "cylindrical", "axis": "z"}, "ok", None),
    ("assembly_get", {}, _joint_is("JRig", "cylindrical"), None),
    ("joint_edit", {"joint_name": "JRig", "joint_type": "planar", "axis": "z"}, "ok", None),
    ("assembly_get", {}, _joint_is("JRig", "planar"), None),
    ("joint_edit", {"joint_name": "JRig", "joint_type": "ball"}, "ok", None),
    ("assembly_get", {}, _joint_is("JRig", "ball"), None),
    # pin_slot alone takes TWO frame directions - it rotates about one and slides along another, so
    # the pair must differ.
    ("joint_edit", {"joint_name": "JRig", "joint_type": "pin_slot", "axis": "z",
                    "slide_axis": "y"}, "ok", None),
    ("assembly_get", {}, _joint_is("JRig", "pin_slot"), None),
    ("joint_edit", {"joint_name": "JRig", "joint_type": "pin_slot", "axis": "y",
                    "slide_axis": "y"}, "refused", None),
    ("joint_edit", {"joint_name": "JRig", "joint_type": "rigid"}, "ok", None),
    ("assembly_get", {}, _joint_is("JRig", "rigid"), None),
] + _box("LnkA", ox=1400, oy=120, tint="#E5533C") + _box(
    "LnkB", ox=1400, oy=120, tint="#1E88E5", shape="disc") + [
    # A LINK PARTNER THAT HAS NEVER BEEN DRIVEN: the bench's own drive pass has already moved every
    # station, and the second-member guard reads the session drive registry - so the coupling below
    # needs one fresh revolute to refuse on.
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("assembly_ground", {"occurrence": "LnkB:1", "ground_to_parent": True}, _grounded, None),
    ("joint_create", {"occurrence_one": "LnkA:1:top", "occurrence_two": "LnkB:1:top",
                      "joint_type": "revolute", "axis": "z", "name": "BenchLink"},
     _jointed("BenchLink"), None),
    # COUPLE the driven revolute station to it at ratio 2 - the DOF-fix step, across two chains
    # that share nothing.
    ("joint_motion_link", {"joint_one": "JRev", "joint_two": "BenchLink", "ratio": 2},
     _motion_linked("JRev", "BenchLink", False),
     ("bench_link", _recall("bench_link", lambda p: p["motion_link"]))),
    # every joint built above, counted off the design-wide walk by an independent read.
    ("assembly_get", {}, _joints_listed(10), None),
    # the relations LIFECYCLE: list, suppress round-trip, re-value the link (was_reversed
    # disclosed), the measured set_occurrences refusal, and a delete with the survivor re-list.
    # Both names are the ones the CREATING calls read back off their own relation - a motion link's
    # auto-name is session-global ('Motion Link 9' is a legal first link), and the vise's relations
    # sit ahead of these in the design's own list.
    ("assembly_get", {"include": ["relations"]},
     lambda p: p.get("relation_counts", {}).get("rigid_groups", 0) >= 1
     and p.get("relation_counts", {}).get("motion_links", 0) >= 1, None),
    ("assembly_edit_relations", lambda c: {"kind": "rigid_group", "name": _ctx_get(c, "bench_group", "the bench's rigid group"), "action": "suppress"},
     lambda p: p.get("is_suppressed") is True, None),
    ("assembly_edit_relations", lambda c: {"kind": "rigid_group", "name": _ctx_get(c, "bench_group", "the bench's rigid group"), "action": "unsuppress"},
     lambda p: p.get("is_suppressed") is False, None),
    ("assembly_edit_relations", lambda c: {"kind": "motion_link", "name": _ctx_get(c, "bench_link", "the bench's motion link"), "action": "set_values", "ratio": 3},
     lambda p: p.get("ratio") == 3.0 and "was_reversed" in p, None),
    # back to the 2:1 the bench link was built at, read back the same way the re-value above was.
    ("assembly_edit_relations", lambda c: {"kind": "motion_link", "name": _ctx_get(c, "bench_link", "the bench's motion link"), "action": "set_values", "ratio": 2},
     lambda p: p.get("ratio") == 2.0 and "was_reversed" in p, None),
    # the measured set_occurrences refusal, asserted in the WORDS that make it a fact: the build it
    # was measured on and the platform sentence it would raise. Nothing is written, so the group
    # still holds the two members assembly_rigid_group gave it - read back on the next row.
    ("assembly_edit_relations", lambda c: {"kind": "rigid_group", "name": _ctx_get(c, "bench_group", "the bench's rigid group"), "action": "set_occurrences", "occurrences": ["GrpA:1"]},
     _refused("2705.0.87", "Cannot be edited before rolling back"), None),
    # the group the refusal named, found BY NAME in the design's own relation list, still counts the
    # two occurrences assembly_rigid_group built it from - the refusal wrote nothing.
    ("assembly_get", {"include": ["relations"]},
     lambda p: next((g["occurrence_count"] for g in p["relations"]["rigid_groups"]
                     if g.get("name") == _RECALL.get("bench_group")), None) == 2, None),
    ("assembly_edit_relations", {"kind": "rigid_group", "name": "NoSuchGroup", "action": "delete"}, "refused", None),
    # contact sets: the design-level lifecycle on a scratch set built from the bench's own parts -
    # create (>=2 distinct members), the single-member refusal, re-member, rename reading the LANDED
    # name back, a suppress round-trip, the two analysis flags (restored), and delete + re-list.
    ("assembly_edit_contacts", {"action": "create", "members": ["GrpA:1", "GrpB:1"]},
     lambda p: p.get("member_count") == 2 and bool(p.get("contact_set")),
     ("contact_set", lambda p: p["contact_set"])),
    ("assembly_edit_contacts", {"action": "create", "members": ["GrpA:1"]}, "refused", None),
    ("assembly_get", {"include": ["contacts"]},
     lambda p: p.get("contact_count", 0) >= 1 and "enabled" in p.get("contact_analysis", {}), None),
    ("assembly_edit_contacts", lambda c: {"action": "set_members", "name": _ctx_get(c, "contact_set", "contact set name"), "members": ["GrpA:1", "PinCameo:1"]},
     lambda p: p.get("member_count") == 2, None),
    ("assembly_edit_contacts", lambda c: {"action": "rename", "name": _ctx_get(c, "contact_set", "contact set name"), "new_name": "SweepContacts"},
     lambda p: str(p.get("contact_set", "")).startswith("SweepContacts"),
     ("contact_set", lambda p: p["contact_set"])),
    ("assembly_edit_contacts", lambda c: {"action": "suppress", "name": _ctx_get(c, "contact_set", "contact set name")},
     lambda p: p.get("is_suppressed") is True, None),
    ("assembly_edit_contacts", lambda c: {"action": "unsuppress", "name": _ctx_get(c, "contact_set", "contact set name")},
     lambda p: p.get("is_suppressed") is False, None),
    # scope is REFUSED while contact analysis is off - the platform raises '3 : Contact analysis is
    # disabled.' on the write - so the enable comes first and the design is left as it was found.
    ("assembly_edit_contacts", {"action": "set_analysis_scope", "scope": "contact_sets"}, "refused", None),
    ("assembly_edit_contacts", {"action": "enable_analysis"}, lambda p: p.get("analysis_enabled") is True, None),
    ("assembly_edit_contacts", {"action": "set_analysis_scope", "scope": "contact_sets"},
     lambda p: p.get("scope") == "contact_sets", None),
    # put the scope back to the design's own all_bodies BEFORE disabling: the flag is retained under
    # a disable and comes back on the next enable, so skipping this would leave the story document
    # carrying a contact_sets scope it never had.
    ("assembly_edit_contacts", {"action": "set_analysis_scope", "scope": "all_bodies"},
     lambda p: p.get("scope") == "all_bodies", None),
    ("assembly_edit_contacts", {"action": "disable_analysis"},
     lambda p: p.get("analysis_enabled") is False and p.get("scope") == "all_bodies", None),
    ("assembly_edit_contacts", {"action": "delete", "name": "NoSuchContactSet"}, "refused", None),
    ("assembly_edit_contacts", lambda c: {"action": "delete", "name": _ctx_get(c, "contact_set", "contact set name")},
     lambda p: p.get("deleted") is True, None),
    _watch("JointBase:1"),
    # DRIVE ON CAMERA, on the bench that carries a station per motion type: the revolute swung
    # inside the limits set above and the slider run out, both read back off the joint.
    ("joint_drive", {"joint_name": "JRev", "angle_deg": 40}, _driven_angle(40), None),
    ("joint_drive", {"joint_name": "JSld", "distance": 18}, _driven_slide(18), None),
    # JRev (a link member) is in the session drive registry, so driving its partner BenchLink must
    # REFUSE (the second-member guard). This is also the live proof that
    # MotionLink.jointOne/jointTwo resolve: a wrong property name would leave the partner lookup
    # blind and this drive would wrongly succeed, failing the row.
    ("joint_drive", {"joint_name": "BenchLink", "angle_deg": 10}, "refused", None),
    # the driven pose read back off the JOINTS themselves - the two drives above reported their own
    # value_now, and this is the second witness to the revolute's.
    ("assembly_get", {}, _joints_listed(10, {"JRev": 40}), None),
    ("assembly_inspect_interference", {}, _interference_measured, None),   # driven pose
    ("joint_drive", {"joint_name": "JRev", "angle_deg": 0}, _driven_angle(0), None),
    ("joint_drive", {"joint_name": "JSld", "distance": 0}, _driven_slide(0), None),
    ("assembly_inspect_interference", {}, _interference_measured, None),   # rest pose
    # pose + constrain cameos (do not disturb the jointed bench).
    ("model_create_component", {"name": "PoseCameo", "activate": True}, _made_component, None),
    ("sketch_create", {"plane": "xy", "name": "PoseS"}, "ok", None),
    ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": 300, "y1": 100,
                                           "x2": 320, "y2": 120}],
                             "sketch_name": "PoseS"}, "ok", None),
    ("model_extrude", {"sketch_name": "PoseS", "profile_index": 0, "distance": 10}, _extruded, None),
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    # the cameo's first move, off a fresh occurrence still at the identity transform - so the
    # position read back off transform2 is the 40 mm this call composed onto it.
    ("assembly_move", {"occurrence": "PoseCameo:1", "dx": 40}, _moved_occurrence(40), None),
    # the pending pose is transient until captured: status sees it armed, and discard_pending throws
    # it away - the pending flag clears while the captured-marker collection is left exactly as it
    # was. The marker count is RECALLED rather than asserted at zero: the vise act captured the
    # clamped pose before this one ran, so what this beat is about is the count not MOVING.
    ("assembly_capture_position", {"action": "status"},
     lambda p: p.get("has_pending") is True and _num(p.get("snapshot_count")),
     ("snap_before", _recall("snap_before", lambda p: p["snapshot_count"]))),
    ("assembly_capture_position", {"action": "discard_pending"},
     lambda p: p.get("discarded") is True and p.get("has_pending") is False
     and p.get("snapshot_count") == _RECALL.get("snap_before"), None),
    # re-arm the move the capture below records - the discard consumed the first one. Where the
    # discard left the part is what this run measures, so only the read-back itself is asserted.
    ("assembly_move", {"occurrence": "PoseCameo:1", "dx": 40}, _moved_occurrence(), None),
    ("assembly_capture_position", {"action": "capture"}, _captured, None),
])
_MOTION += _box("ConA", ox=360, tint="#E5533C") + _box("ConB", ox=360, tint="#1E88E5", shape="disc") + [
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("assembly_constrain", {"snap_one": "ConA:1:bottom", "snap_two": "ConB:1:top", "flipped": True}, _constrained, None),
    ("design_recompute", {}, "ok", None),
] + _box("MateSeat", ox=460, tint="#6A1B9A") + _box("MateArm", ox=520, tint="#FDD835", shape="bar") + [
    # ONE CONSTRAINT, SEVERAL RELATIONSHIPS - the table in Fusion's own Constrain Components dialog,
    # where a single constraint FEATURE carries a row per geometry pair, each row with its own type,
    # offset and angle. That is how Fusion actually locates a part: a set solved TOGETHER, because one
    # face pair almost never fixes anything. The row above is the single-pair shorthand; this is the
    # set form.
    # The two rows take DIFFERENT freedoms, which is what makes the set solvable: a SEAT (the arm's
    # underside onto the seat block's top face, 2 mm proud, flipped so the two faces oppose) and a
    # TURN about it (30 deg between the two front faces). Two rows reaching for the SAME freedom
    # over-constrain instead - measured on a live document: a face-to-face mate plus a concentric on
    # one pair of discs computes with a WARNING, with and without the offset, and the tool refuses
    # rather than leave a warned constraint in the design.
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("assembly_constrain", {"relationships": [
        {"snap_one": "MateArm:1:bottom", "snap_two": "MateSeat:1:top", "flip": True, "offset": 2},
        {"snap_one": "MateArm:1:front", "snap_two": "MateSeat:1:front", "angle_deg": 30},
    ]},
     # BOTH ROWS IN ONE NUMBER. The count is read off the CREATED constraint, never echoed - a
     # constraint holding FEWER rows than were submitted is refused by the tool - and the rotation the
     # tool measures for itself is 180 - 30: the seat row's flip and the turn row's angle composed.
     # Neither row on its own produces 150.
     lambda p: _constrained(p) and _measured(
         "both relationship rows landed in ONE constraint, and both acted",
         {"relationship_count": p.get("relationship_count"),
          "relationships_submitted": p.get("relationships_submitted"),
          "moved": p.get("moved")},
         p.get("relationships_submitted") == 2 and p.get("relationship_count") >= 2
         and any(_near(m.get("rotation_deg"), 150.0, 0.5)
                 for m in (p.get("moved") or []))), None),
    ("design_recompute", {}, "ok", None),
    # THE TWO ROWS PROVEN BY THEIR EFFECTS, which is the only honest way to tell them apart: no read
    # publishes a per-row TYPE (the dialog's Type column has no counterpart on the wire), so a count
    # of two says two rows landed and nothing about what each one did. The arm's own bounding box
    # says both. Its underside sits 2 mm above the seat block's 10 mm top face - the seat row, read on
    # Z, the one axis the layout pass never moves - and a 34 x 10 mm bar turned 30 deg measures
    # 34.45 x 25.66 across the world axes, which is the turn row and nothing else.
    ("model_inspect", {"target": "MateArm:1"},
     lambda p: _measured("the seat row holds the arm 2 mm proud of a 10 mm block, the turn row has "
                         "it 30 deg off the world axes",
                         {"min_z": (p.get("min_point") or {}).get("z"),
                          "x": p.get("x"), "y": p.get("y")},
                         _near((p.get("min_point") or {}).get("z"), 12.0, 0.05)
                         and _near(p.get("x"), 34.445, 0.05)
                         and _near(p.get("y"), 25.660, 0.05)), None),
    # RESTRUCTURE: two more pin instances (they share the PinCameo component's geometry), one of
    # them re-parented under the bore cameo, then both removed so the bench is left as it was found.
    # Fusion numbers an instance from a per-component counter, so every path here is READ back,
    # never a predicted ':2'.
    ("design_add_instance", {"component": "PinCameo", "x": 60, "y": -60, "units": "mm"},
     lambda p: p.get("created") is True and p.get("component") == "PinCameo"
     and str(p.get("full_path", "")).startswith("PinCameo:"),
     ("pin_b", lambda p: p["full_path"])),
    # the SECOND call names the same component while two instances of it exist - the bare name that
    # would otherwise be ambiguous resolves because every candidate is an instance of ONE component.
    ("design_add_instance", {"component": "PinCameo", "x": 90, "y": -60, "units": "mm"},
     lambda p: p.get("created") is True
     and p.get("full_path") not in ("PinCameo:1", None), ("pin_c", lambda p: p["full_path"])),
    ("design_get", {"include": ["tree"]}, "ok", None),
    # the re-parent: the browser path changes, the world position does not.
    ("design_move_occurrence", lambda c: {"occurrence": _ctx_get(c, "pin_b", "the second pin"),
                                          "into_component": "BoreCameo:1"},
     lambda p: p.get("changed") is True and str(p.get("full_path", "")).startswith("BoreCameo:1+")
     and p.get("world_position_preserved") is True, ("pin_b", lambda p: p["full_path"])),
    # there is no root target: the API moves an occurrence into another OCCURRENCE, so the direction
    # is refused by name instead of being attempted and failing inside Fusion.
    ("design_move_occurrence", lambda c: {"occurrence": _ctx_get(c, "pin_b", "the second pin"),
                                          "into_component": "root"}, "refused", None),
    # a component may not hold an instance of itself, on either tool.
    ("design_add_instance", {"component": "BoreCameo", "into_component": "BoreCameo:1"},
     "refused", None),
    ("design_move_occurrence", {"occurrence": "BoreCameo:1", "into_component": "BoreCameo:1"},
     "refused", None),
    ("design_delete_occurrence", lambda c: {"occurrence": _ctx_get(c, "pin_b", "the second pin")},
     "ok", None),
    ("design_delete_occurrence", lambda c: {"occurrence": _ctx_get(c, "pin_c", "the third pin")},
     "ok", None),
]

# ACT 7: THE VISE - the billet the bracket is cut from, and the machine vise that holds it.
# Geometry contract (all mm; the Bracket occupies x[-60,60] y[-40,40] z[0,45] with its boss):
#   STOCK      x[-63,63] y[-43,43] z[-8,48]   - 3 mm all round, 8 mm of grip stock under the part
#   ViseBase   x[-95,95] y[-95,95] z[-40,-8]  - the stock and both jaws seat on its z=-8 top
#   JawFixed   x[-70,70] y[43,78]  z[-8,10]   - its grip face IS the stock's +y side
#   JawMoving  x[-70,70] y[-84,-49] z[-8,10]  - 6 mm open; the slider closes it onto y=-43
#   LeadScrew  r6 on the world Y axis, y[-117,-80], with a cross handle at y[-125,-117]
# The jaws top out at z=10 while every machined feature - the pocket floor at z=14, the bores and
# the boss above it - stands clear above them, which is what the 8 mm of grip stock buys.

def _plate(comp, sketch, z_offset, x1, y1, x2, y2, height):
    """Component + its own build plane at z_offset + one rectangle, extruded up by height.
    The plane lives INSIDE the component: sketch_create resolves construction-plane names in
    the ACTIVE component, so a root-level plane is invisible after activate."""
    plane = comp + "Floor"
    return [
        ("model_create_component", {"name": comp, "activate": True}, _made_component, None),
        ("model_construction", {"kind": "plane", "plane": "xy", "offset": z_offset,
                                "name": plane}, _datum_plane("xy"), None),
        ("sketch_create", {"plane": plane, "name": sketch}, "ok", None),
        ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": x1, "y1": y1,
                                               "x2": x2, "y2": y2}],
                                 "sketch_name": sketch}, "ok", None),
        ("model_extrude", {"sketch_name": sketch, "profile_index": 0, "distance": height},
         _extruded, None),
    ]


# The occurrences the clamped-fixture gate judges. assembly_inspect_interference takes no scope, so
# it reports the whole design - including the scratch cameos, whose own bodies overlap by design.
_FIXTURE_PARTS = {"Bracket:1", "STOCK:1", "ViseBase:1", "JawFixed:1", "JawMoving:1", "LeadScrew:1"}


def _fixture_rest_clean(p):
    """assembly_inspect_interference read over the FIXTURE: the only pair of it allowed to overlap
    is the part inside the billet it is cut from. A jaw biting the part, or a jaw into the base, is
    a fixture that does not hold, and the interference census is the only read that says so.

    A row naming the same occurrence twice is one cameo's own two bodies, and a row naming no
    fixture part belongs to a cameo rig - neither is this gate's business."""
    rows = (p.get("measured") or {}).get("interferences") or []
    strays = []
    for i in rows:
        a = (i.get("occurrence_one") or "").replace("/", "+").split("+")[-1]
        b = (i.get("occurrence_two") or "").replace("/", "+").split("+")[-1]
        if a == b or not ({a, b} & _FIXTURE_PARTS):
            continue
        if sorted([a, b]) != ["Bracket:1", "STOCK:1"]:
            strays.append(sorted([a, b]))
    return _interference_measured(p) and _measured(
        "the clamped fixture overlaps nowhere but part-in-billet",
        {"interference_count": (p.get("measured") or {}).get("interference_count"),
         "strays": strays[:4]}, not strays)


def _linked_screw_angle(joint, mm, deg_per_mm):
    """assembly_get: the screw's own driven value after the jaw was driven, read off the DESIGN's
    joint walk rather than off the drive that moved it.

    A motion link's whole claim is that driving one member carries the other, and the partner's
    stored value is the only read that shows it. The magnitude is the ratio times the jaw's travel;
    the SIGN is the platform's to pick from the two joints' own frames, so it is reported and the
    magnitude is what is asserted."""
    def check(p):
        row = next((j for j in (p.get("joints") or []) if j.get("name") == joint), None)
        got = ((row or {}).get("value_now") or {}).get("angle_deg")
        return _measured(f"'{joint}' turned {abs(mm * deg_per_mm)} deg with the jaw's {mm} mm",
                         {"joint": row and {"name": row.get("name"), "type": row.get("type"),
                                            "value_now": row.get("value_now")}},
                         _num(got) and _mod360(abs(got), abs(mm * deg_per_mm)) < 0.5)
    return check


_VISE = (
    # THE BILLET FIRST, dressed before any of the fixture exists. Its look has to be settled before
    # the jaws are there to close on it - a stock that changes appearance halfway through the
    # clamping reads as the clamping doing it.
    _plate("STOCK", "StockS", -8, -63, -43, 63, 43, 56)
    + [
        ("design_activate_component", {"occurrence": "root"}, "ok", None),
        ("appearance_set", {"target": "STOCK", "color": "#8D6E63"}, "ok", None),
        # HALF translucent, so the bracket inside stays visible through the billet it is cut from -
        # the whole point of the fixture shot is the part in the stock in the vise, and an opaque
        # billet hides the part. Opacity here is the browser's Opacity Control (Component.opacity),
        # NOT the appearance's transparency: the two are unrelated, and a fully opaque colour still
        # renders see-through under an opacity override. Read back off what actually RENDERS, since
        # the override is inherited from parent components.
        ("appearance_set", {"target": "STOCK:1", "opacity": 50},
         lambda p: p.get("opacity_rendered") == 50, None),
        # the billet measured against the part it encloses: the stated allowance is 3 mm a side, so
        # a 120 x 80 part comes out of a 126 x 86 billet.
        ("model_inspect", {"target": "STOCK:1"},
         lambda p: _measured("the billet is the part's box plus its allowance",
                             {"x": p.get("x"), "y": p.get("y"), "z": p.get("z")},
                             _near(p.get("x"), 126.0, 0.5) and _near(p.get("y"), 86.0, 0.5)), None),
    ]
    + _plate("ViseBase", "VBase", -40, -95, -95, 95, 95, 32)
    + _plate("JawFixed", "JFixS", -8, -70, 43, 70, 78, 18)
    + _plate("JawMoving", "JMovS", -8, -70, -84, 70, -49, 18)
    # THE LEAD SCREW, on the world Y axis so the revolute about 'y' spins it about its own centre
    # rather than orbiting it: a circle on an XZ plane, extruded along +Y, with a cross handle on
    # its outboard end. The handle is what makes the rotation readable - a plain cylinder turning
    # about its own axis shows nothing.
    + [
        ("model_create_component", {"name": "LeadScrew", "activate": True}, _made_component, None),
        ("model_construction", {"kind": "plane", "plane": "xz", "offset": -117,
                                "name": "ScrewPlane"}, _datum_plane("xz"), None),
        ("sketch_create", {"plane": "ScrewPlane", "name": "ScrewS"}, "ok", None),
        ("sketch_add_geometry", {"geometry": [{"kind": "circle", "cx": 0, "cy": 0, "radius": 6}],
                                 "sketch_name": "ScrewS"}, "ok", None),
        ("model_extrude", {"sketch_name": "ScrewS", "profile_index": 0, "distance": 37},
         _extruded, None),
        ("model_construction", {"kind": "plane", "plane": "xz", "offset": -125,
                                "name": "HandlePlane"}, _datum_plane("xz"), None),
        ("sketch_create", {"plane": "HandlePlane", "name": "HandleS"}, "ok", None),
        ("sketch_add_geometry", {"geometry": [{"kind": "rectangle", "x1": -3, "y1": -30,
                                               "x2": 3, "y2": 30}],
                                 "sketch_name": "HandleS"}, "ok", None),
        ("model_extrude", {"sketch_name": "HandleS", "profile_index": 0, "distance": 8},
         _extruded, None),
    ]
) + [
    ("design_activate_component", {"occurrence": "root"}, "ok", None),
    ("appearance_set", {"target": "ViseBase", "color": "#455A64"}, "ok", None),
    ("appearance_set", {"target": "JawFixed", "color": "#78909C"}, "ok", None),
    ("appearance_set", {"target": "JawMoving", "color": "#E5533C"}, "ok", None),
    ("appearance_set", {"target": "LeadScrew", "color": "#F5A623"}, "ok", None),
    # THE FIXTURE SKELETON. Every component here has its origin at the WORLD origin (geometry is
    # drawn in world coordinates), so a ':origin' snap aligns already-aligned frames - a positional
    # no-op, no teleport - and the slider/revolute axes ride that world-aligned frame.
    ("assembly_ground", {"occurrence": "ViseBase:1", "ground_to_parent": True}, _grounded, None),
    # the fixed side is ONE body as far as the machine is concerned: base plus fixed jaw.
    ("assembly_rigid_group", {"occurrences": ["ViseBase:1", "JawFixed:1"]},
     _rigid_grouped(2), None),
    ("joint_create", {"occurrence_one": "JawMoving:1:origin", "occurrence_two": "ViseBase:1:origin",
                      "joint_type": "slider", "axis": "y", "name": "JawSlide"},
     _jointed("JawSlide"), None),
    ("joint_create", {"occurrence_one": "LeadScrew:1:origin", "occurrence_two": "ViseBase:1:origin",
                      "joint_type": "revolute", "axis": "y", "name": "ScrewTurn"},
     _jointed("ScrewTurn"), None),
    # TRAVEL LIMITS on the jaw, wide enough for the open pose below and stopping where the jaw would
    # run off its own way. Every published limit is the value READ BACK off the live JointLimits.
    ("joint_edit", {"joint_name": "JawSlide", "min_mm": -10, "max_mm": 8},
     _joint_limits("JawSlide", min_mm=-10, max_mm=8), None),
    # the part in the stock, as-built rigid - the billet and what will be cut out of it are one
    # piece until the cutter says otherwise. The stock is NOT jointed to the base: a billet welded to
    # the vise body is not being held by anything, and the jaws closing on it would prove nothing.
    # What holds it is the grip, captured below once the jaw has actually closed.
    ("joint_create_as_built", {"occurrence_one": "Bracket:1", "occurrence_two": "STOCK:1"},
     _as_built, None),
    # THE LEAD SCREW COUPLED TO THE JAW: 45 degrees of handle per millimetre of travel. The ratio is
    # joint_two's motion per ONE unit of joint_one, each in its own display unit - deg here, mm
    # there - and 45 is chosen so the closing travel lands the screw on 270 deg: a revolute's value
    # reads modulo full turns, so a link angle that came out a multiple of 360 would be
    # indistinguishable from a link that carried nothing.
    ("joint_motion_link", {"joint_one": "JawSlide", "joint_two": "ScrewTurn", "ratio": 45},
     _motion_linked("JawSlide", "ScrewTurn", False), None),
    _watch(["ViseBase:1", "STOCK:1"]),
    # OPEN, then CLOSED, both read back off the joint - the vise is watched working rather than
    # found already shut. The open pose is held a beat and shot before the jaw comes in. The closing
    # 6 mm is the authored gap between the moving jaw's grip face (y=-49) and the billet side
    # (y=-43), so it lands the jaw flush rather than driving it into the part.
    ("joint_drive", {"joint_name": "JawSlide", "distance": -4}, _driven_slide(-4), None),
    _dwell(1.5),
    ("view_screenshot", {"view": "current", "width": 500, "height": 400}, "ok", None),
    ("joint_drive", {"joint_name": "JawSlide", "distance": 6}, _driven_slide(6), None),
    _dwell(1.5),
    # THE LINK TRANSMITTING, read off the PARTNER rather than off the drive that moved it: the jaw
    # travelled 6 mm, so the screw stands at 6 x 45 deg. This is what the coupling claims, and the
    # design's own joint walk is the witness the writer cannot be.
    ("assembly_get", {}, _linked_screw_angle("ScrewTurn", 6, 45), None),
    # GRIP VERIFIED BY MEASURE, not by trust, and BY NAME rather than by face handle - a handle-form
    # model_measure_between reads a distance the occurrence-name form disagrees with on this build
    # (ledger row MEASBTW-1), so the grip stands on the form whose answer is trusted.
    # The FIXED jaw's face is flush from birth and proves only that nothing moved it; the MOVING
    # jaw's is the load-bearing one, and its extent is read a second way below.
    ("model_measure_between", {"a": "JawFixed", "b": "STOCK"},
     lambda p: p.get("distance", 99) <= 0.1, None),
    ("model_measure_between", {"a": "JawMoving", "b": "STOCK"},
     lambda p: p.get("distance", 99) <= 0.1, None),
    # ...and the same grip as a POSITION: touching and penetrating both measure zero distance, so
    # the moving jaw's own box is read for where its grip face came to rest - y=-43 is the billet
    # side, and anything past it is a jaw inside the part.
    ("model_inspect", {"target": "JawMoving:1"},
     lambda p: _measured("the moving jaw closed ONTO the billet side, not past it",
                         {"max_point": p.get("max_point"), "min_point": p.get("min_point")},
                         _near((p.get("max_point") or {}).get("y"), -43.0, 0.05)), None),
    # A DRIVE LEAVES A TRANSIENT POSE, and a joint create is REFUSED while one is pending (it would
    # silently revert it). So the clamped pose is recorded into the timeline first - which is also
    # the right order physically: the jaw is closed, that closure is what the grip joint captures.
    ("assembly_capture_position", {"action": "capture"}, _captured, None),
    # THE GRIP ITSELF, taken only now that both faces measure closed onto the billet: an as-built
    # joint mates two occurrences WHERE THEY ALREADY ARE, so taking it here records the clamped pose
    # rather than creating it. ONE jaw takes the joint - jointing the stock to both jaws would close
    # the loop twice and be refused as over constrained (measured).
    ("joint_create_as_built", {"occurrence_one": "STOCK:1", "occurrence_two": "JawMoving:1",
                               "name": "GripJaw"},
     lambda p: _as_built(p) and _measured("the grip joint names the jaw it was taken on",
                                          {"joint": p.get("joint")}, p.get("joint") == "GripJaw"), None),
    # THE CLAMPED READ: nothing in the fixture overlaps but the part inside its own billet.
    ("assembly_inspect_interference", {}, _fixture_rest_clean, None),
    ("view_screenshot", {"width": 500, "height": 400}, "ok", None),
]
