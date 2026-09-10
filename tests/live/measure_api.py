# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""Live API measurement: every API fact the unit-test fakes need, measured from LIVE Fusion.

The unit suite proves tool logic against fakes. This tool is where the fakes' API facts COME
FROM: each measurement row checks a claim against a running Fusion, and a fully-PASSING run
generates ``tests/live_api_facts.py`` - enum values, behavior flags, and the version stamp -
which conftest imports to populate the mock adsk modules and the shared fakes. The mocks are fed
by measurement, not by hand; VERIFIED_API_FACTS.md is the human-readable ledger of the same run.

Run:  py -3 tests/live/measure_api.py          (Fusion running + add-in enabled +
                                                allow_execute_api_script on)
      py -3 tests/live/measure_api.py --check  (no measuring: exit 1 when the stamp does not
                                                match the installed Fusion or any row is
                                                not PASS)
      py -3 tests/live/measure_api.py --json   (also archive results to tests/live/results/)

ROWS are DATA: id, claim, encoded_in (the fake carrying the claim), a script body, and an
expectation. A row's script may print ``FACT <dotted.key> <json>`` lines - measured values the
generator folds into live_api_facts.py; a row whose misuse kills its own script instead declares
``facts_on_pass`` and the runner records those when the row passes. Every row runs as its OWN
script because a raise can escape try/except entirely and kill the whole Python.Run invocation,
eating its printed output (out-of-range item() does exactly that, observed live) - so one row's
abort can never swallow another row's result. expect="raise_or_abort" rows assert a misuse that
never returns a value: a caught raise prints PASS, and a script-level error ALSO confirms the
claim. Rows with needs="cam" run LAST: on the first one, the runner stands up a CAM world (a box,
MeasureSetup, two face ops, MeasureFolder holding the second op) through the server's own tools;
if that build fails, every cam row reports ERROR with the failing step instead of crashing the
run. Extend coverage by adding rows, not code.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tool_verify import (  # noqa: E402  shared HTTP plumbing
    attestation_identity, call, health_gate, registered_tools)
import cloud_config  # noqa: E402  the operator's hub/project/folder, never a literal in this file
CLOUD_PROJECT = cloud_config.PROJECT

LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERIFIED_API_FACTS.md")
FACTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "live_api_facts.py")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TOOLS_DIR = os.path.join(REPO_ROOT, "commands", "mcpServer", "tools")
_ATTESTATION_TCB = ("Fusion-Essentials.py", "lib/loaded_attestation.py")
_ATTESTATION_FIELDS = ("implementation_fingerprint", "schema_fingerprint",
                       "load_id", "session_id")

# Every adsk enum FAMILY the tools reference, scraped from the tool sources so the sweep tracks the
# codebase - a new enum a tool starts using is measured automatically, no row edit. The value-pinning
# rows below still assert the specific members production branches on; this sweep is additive
# coverage for every family, pinned or not. A family is only counted from a REAL reference, never a
# comment (a stale name in a comment must not drive a live measurement).
# …Options and …Operations are scraped too, and some of those names are factory-OBJECT classes
# (...Options.create()) carrying no int member at all: the sweep row reports such a name as
# not-an-enum and the generated facts file records it in NOT_ENUMS, so only a name that does not
# RESOLVE fails the row.
_ENUM_FAMILY_RE = re.compile(r"adsk\.(core|fusion|cam|drawing)\.([A-Za-z]*(?:Types|States?|Modes|Methods|Directions|Locations|Positions|Alignments?|Sizes|Formats|Operations|Options))\b")
# A family reached only through _drawing_common.enum_value("<Family>", "<member>") never appears as
# a literal adsk.drawing.<Family> in tool source, so the textual scan above cannot see it - the
# STRING argument is the real reference. enum_value resolves exclusively against adsk.drawing.
_ENUM_BY_NAME_RE = re.compile(r"""enum_value\(\s*["']([A-Za-z]+)["']""")


def referenced_enum_families():
    fams = set()
    for fn in os.listdir(TOOLS_DIR):
        if not fn.endswith(".py"):
            continue
        with open(os.path.join(TOOLS_DIR, fn), encoding="utf-8") as fh:
            for line in fh:
                code = line.split("#", 1)[0]   # strip comments - a name in prose is not a reference
                for ns, cls in _ENUM_FAMILY_RE.findall(code):
                    fams.add(ns + "." + cls)
                for cls in _ENUM_BY_NAME_RE.findall(code):
                    fams.add("drawing." + cls)
    return sorted(fams)


_FACT_BEHAVIOR_PRINT = re.compile(r"FACT behavior\.([a-z0-9_]+)")


def emitted_behavior_keys():
    """Every behavior flag key a measurement row can emit - ``facts_on_pass`` entries plus
    ``FACT behavior.*`` prints inside row script bodies. The reverse gate in
    tests/lints/test_enum_families_measured.py consumes this: each emitted key must exist in the
    generated live_api_facts.BEHAVIOR, so a key rename here goes red until a live regen."""
    keys = set()
    for row in ROWS:
        for key in (row.get("facts_on_pass") or {}):
            if key.startswith("behavior."):
                keys.add(key[len("behavior."):])
        body = row["body_fn"]() if "body_fn" in row else row.get("body", "")
        keys |= set(_FACT_BEHAVIOR_PRINT.findall(body))
    return sorted(keys)


def _all_enums_body():
    """Script body for the enum-sweep row: dump every referenced family, then PASS iff every scraped
    name RESOLVED to a live class - one that resolves but carries no int member is a factory-object
    class, recorded as not-an-enum instead of failing the row."""
    fams = referenced_enum_families()
    lines = ["    fams = ["]
    for f in fams:
        # Resolve each family through getattr chains so a scraped name that is NOT a live class
        # (a typo, or a renamed family) yields None here instead of aborting the whole script.
        ns, cls = f.split(".", 1)
        lines.append('        ("{0}", getattr(getattr(adsk, "{1}", None), "{2}", None)),'.format(f, ns, cls))
    lines.append("    ]")
    lines.append("    missing = []")
    lines.append("    not_enum = []")
    lines.append("    for label, cls in fams:")
    lines.append("        if cls is None:")
    lines.append("            missing.append(label)")
    lines.append("            continue")
    lines.append("        n = 0")
    lines.append("        for name in dir(cls):")
    lines.append("            v = getattr(cls, name)")
    lines.append("            if not name.startswith('_') and isinstance(v, int):")
    lines.append("                print('FACT enums.' + label + '.' + name + ' ' + str(v))")
    lines.append("                n += 1")
    lines.append("        if n == 0:")
    lines.append("            not_enum.append(label)")
    lines.append("            print('FACT not_enums.' + label + ' true')")
    lines.append("    emit(not missing, 'enum-sweep: '"
                 " + str(len(fams) - len(missing) - len(not_enum)) + '/' + str(len(fams))"
                 " + ' families dumped int members'"
                 " + ('; no int member (factory class): ' + ','.join(not_enum) if not_enum else '')"
                 " + ('; UNRESOLVED: ' + ','.join(missing) if missing else ''))")
    return "\n".join(lines) + "\n"

# Each row script is self-contained: emit() prints one verdict line per check, and make_box()
# builds a 10 mm cube (1.0 in Fusion's internal cm) for rows that need real geometry. Rows that set
# need_box get it bound to `body` before their own lines run.
# A row that PROVOKES an API raise sets read_only: measured, a caught adsk error still takes the
# whole Python.Run down in a design-mutating context, while the same body survives read-only.
_TEMPLATE = '''import adsk.core, adsk.fusion, adsk.cam, adsk.drawing

def emit(ok, detail):
    print(("PASS " if ok else "FAIL ") + detail)

def dump_enum(label, cls):
    for n in dir(cls):
        v = getattr(cls, n)
        if not n.startswith("_") and isinstance(v, int):
            print("FACT enums." + label + "." + n + " " + str(v))

def dump_shape(label, obj):
    names = sorted(n for n in dir(obj) if not n.startswith("_"))
    for i in range(0, len(names), 20):
        print("SHAPE " + label + " " + " ".join(names[i:i + 20]))
    return len(names)

def make_box(des, name):
    root = des.rootComponent
    sk = root.sketches.add(root.xYConstructionPlane)
    sk.sketchCurves.sketchLines.addTwoPointRectangle(
        adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(1.0, 1.0, 0.0))
    prof = sk.profiles.item(0)
    ext = root.features.extrudeFeatures.addSimple(
        prof, adsk.core.ValueInput.createByReal(1.0),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    b = ext.bodies.item(0)
    b.name = name
    return b

def make_placed_box(des, name):
    """(the native box body of a component named `name`, the ONE occurrence placing it)."""
    root = des.rootComponent
    occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    comp = occ.component
    comp.name = name
    sk = comp.sketches.add(comp.xYConstructionPlane)
    sk.sketchCurves.sketchLines.addTwoPointRectangle(
        adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(1.0, 1.0, 0.0))
    comp.features.extrudeFeatures.addSimple(
        sk.profiles.item(0), adsk.core.ValueInput.createByReal(1.0),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    b = comp.bRepBodies.item(0)
    b.name = name + "Body"
    return b, occ

def make_mesh_rig(app, name):
    """(a fresh document, one component's tetrahedron MESH, and the TWO occurrences placing it)."""
    doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    d = adsk.fusion.Design.cast(doc.products.itemByProductType("DesignProductType"))
    d.designType = adsk.fusion.DesignTypes.DirectDesignType
    root = d.rootComponent
    first = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    comp = first.component
    comp.name = name
    comp.meshBodies.addByTriangleMeshData(
        [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
        [0, 2, 1, 0, 1, 3, 1, 2, 3, 2, 0, 3], [], [])
    mesh = comp.meshBodies.item(0)
    mesh.name = name + "Mesh"
    off = adsk.core.Matrix3D.create()
    off.translation = adsk.core.Vector3D.create(5.0, 0.0, 0.0)
    second = root.occurrences.addExistingComponent(comp, off)
    return doc, mesh, first, second

def cam_measure_setup():
    """(the active document's CAM product, its MeasureSetup or None when the world is not up)."""
    cam = adsk.cam.CAM.cast(
        adsk.core.Application.get().activeDocument.products.itemByProductType("CAMProductType"))
    for i in range(cam.setups.count):
        if cam.setups.item(i).name == "MeasureSetup":
            return cam, cam.setups.item(i)
    return cam, None

def cam_sample_tool():
    """The first tool of the bundled 'Milling Tools (Metric)' library, None when it is absent."""
    libs = adsk.cam.CAMManager.get().libraryManager.toolLibraries
    for a in libs.childAssetURLs(
            libs.urlByLocation(adsk.cam.LibraryLocations.Fusion360LibraryLocation)):
        if "Milling Tools (Metric)" in a.leafName:
            lib = libs.toolLibraryAtURL(a)
            return lib.item(0) if lib.count else None
    return None

def cam_add_face_op(setup, tool, name):
    """A face operation added to `setup` carrying `tool`, renamed to `name` - ungenerated."""
    opin = setup.operations.createInput("face")
    opin.tool = tool
    op = setup.operations.add(opin)
    op.name = name
    return op

def run(context):
    app = adsk.core.Application.get()
    des = adsk.fusion.Design.cast(app.activeProduct)
    CLOUD_PROJECT = {project!r}
{box_line}{body}'''


ROWS = [
    {
        "id": "save-image-options-defaults",
        "claim": ("SaveImageFileOptions.create(path) initializes width/height to 0 and "
                  "isBackgroundTransparent False / isAntiAliased True"),
        "encoded_in": "test__view_common.py fake options initial values; _view_common._write_image size assignment",
        "facts_on_pass": {"behavior.save_image_options_defaults": True},
        "body": """
    opts = adsk.core.SaveImageFileOptions.create("probe_never_written.png")
    emit(opts.width == 0 and opts.height == 0
         and opts.isBackgroundTransparent is False and opts.isAntiAliased is True,
         "save-image-options-defaults: w=" + str(opts.width) + " h=" + str(opts.height)
         + " transparent=" + repr(opts.isBackgroundTransparent)
         + " aa=" + repr(opts.isAntiAliased))
""",
    },
    {
        "id": "units-cm",
        "claim": "Lengths cross the API in cm - a 10 mm sketch square extruded 1.0 unit has bbox extent 1.0",
        "encoded_in": "tests/conftest.py bbox fixture; every test asserting a scale() factor",
        "need_box": True,
        "body": """
    dx = body.boundingBox.maxPoint.x - body.boundingBox.minPoint.x
    emit(abs(dx - 1.0) < 1e-6, "units-cm: bbox dx=" + str(dx) + " (expect 1.0)")
""",
    },
    {
        "id": "point3d-vectorto",
        "claim": "Point3D.vectorTo(other) == other - self",
        "encoded_in": "tests/fakes/geometry.py FakePoint.vectorTo",
        "body": """
    a = adsk.core.Point3D.create(1.0, 2.0, 3.0)
    b = adsk.core.Point3D.create(4.0, 6.0, 8.0)
    v = a.vectorTo(b)
    emit(v.x == 3.0 and v.y == 4.0 and v.z == 5.0,
         "point3d-vectorto: (" + str(v.x) + "," + str(v.y) + "," + str(v.z) + ") expect (3,4,5)")
""",
    },
    {
        "id": "vector3d-normalize-zero",
        "claim": "Vector3D.normalize() returns True even for a (near-)zero vector and leaves the components untouched - the return value is not a zero guard",
        "encoded_in": "tests/fakes/geometry.py FakeVector3D.normalize; commands/mcpServer/tools/_geom.py unit_vector (its own magnitude guard, never normalize()'s return)",
        "body": """
    z = adsk.core.Vector3D.create(0.0, 0.0, 0.0)
    rz = z.normalize()
    print("FACT behavior.vector3d_normalize_true_on_zero " + ("true" if rz else "false"))
    untouched = (z.x == 0.0 and z.y == 0.0 and z.z == 0.0)
    # The NEAR-zero vector is read back after ITS OWN normalize(): the fake leaves the components
    # alone for every vector under its tolerance, so the band is measured, not the exact zero alone.
    t = adsk.core.Vector3D.create(1e-15, 0.0, 0.0)
    tiny_before = (t.x, t.y, t.z)
    rt = t.normalize()
    tiny_after = (t.x, t.y, t.z)
    tiny_untouched = tiny_after == tiny_before
    emit(bool(rz) and untouched and bool(rt) and tiny_untouched,
         "vector3d-normalize-zero: zero->" + repr(rz) + " untouched=" + repr(untouched)
         + " tiny->" + repr(rt) + " tiny " + repr(tiny_before) + "->" + repr(tiny_after)
         + " untouched=" + repr(tiny_untouched))
""",
    },
    {
        "id": "joint-limit-out-of-range-ignored",
        "claim": ("Assigning a rotationValue/slideValue STRICTLY beyond an enabled joint limit is "
                  "IGNORED - the value stays where it was, Fusion never clamps; assigning exactly "
                  "AT an enabled bound lands on it"),
        "encoded_in": ("commands/mcpServer/tools/joint_drive.py _limit_refusal (refuse before "
                       "assigning); test_joint_drive.py refusal tests"),
        "body": """
    root = des.rootComponent
    tr = adsk.core.Matrix3D.create()
    occs = []
    for nm in ("LimA", "LimB", "LimC"):
        occ = root.occurrences.addNewComponent(tr)
        c = occ.component
        c.name = nm
        sk = c.sketches.add(c.xYConstructionPlane)
        sk.sketchCurves.sketchCircles.addByCenterRadius(
            adsk.core.Point3D.create(0.0, 0.0, 0.0), 0.5)
        c.features.extrudeFeatures.addSimple(
            sk.profiles.item(0), adsk.core.ValueInput.createByReal(0.5),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        occs.append(occ)
    geo = adsk.fusion.JointGeometry.createByPoint(
        occs[1].component.originConstructionPoint.createForAssemblyContext(occs[1]))
    ji = root.asBuiltJoints.createInput(occs[0], occs[1], geo)
    ji.setAsRevoluteJointMotion(adsk.fusion.JointDirections.ZAxisJointDirection)
    rj = root.asBuiltJoints.add(ji)
    rl = rj.jointMotion.rotationLimits
    rl.isMinimumValueEnabled = True
    rl.minimumValue = -0.17453293
    rl.isMaximumValueEnabled = True
    rl.maximumValue = 0.17453293
    rj.jointMotion.rotationValue = 0.08726646
    parked = rj.jointMotion.rotationValue
    rj.jointMotion.rotationValue = 0.78539816
    beyond = rj.jointMotion.rotationValue
    rj.jointMotion.rotationValue = 0.17453293
    at_bound = rj.jointMotion.rotationValue
    geo2 = adsk.fusion.JointGeometry.createByPoint(
        occs[2].component.originConstructionPoint.createForAssemblyContext(occs[2]))
    si = root.asBuiltJoints.createInput(occs[0], occs[2], geo2)
    si.setAsSliderJointMotion(adsk.fusion.JointDirections.ZAxisJointDirection)
    sj = root.asBuiltJoints.add(si)
    sl = sj.jointMotion.slideLimits
    sl.isMinimumValueEnabled = True
    sl.minimumValue = -1.0
    sl.isMaximumValueEnabled = True
    sl.maximumValue = 1.0
    sj.jointMotion.slideValue = 4.5
    s_beyond = sj.jointMotion.slideValue
    ok_rot = (abs(parked - 0.08726646) < 1e-5 and abs(beyond - parked) < 1e-6
              and abs(at_bound - 0.17453293) < 1e-5)
    ok_sld = abs(s_beyond) < 1e-6
    print("FACT behavior.joint_limit_out_of_range_ignored "
          + ("true" if (ok_rot and ok_sld) else "false"))
    emit(ok_rot and ok_sld,
         "joint-limit-out-of-range-ignored: parked=" + str(parked) + " beyond=" + str(beyond)
         + " at_bound=" + str(at_bound) + " slide_beyond=" + str(s_beyond)
         + " (expect 0.08727 / unchanged / 0.17453 / 0)")
""",
    },
    {
        "id": "asbuilt-rigidgroup-health-via-timeline",
        "claim": ("AsBuiltJoint and RigidGroup raise AttributeError on BOTH healthState and "
                  "errorOrWarningMessage while each one's timelineObject answers both - health "
                  "for these two types is readable only through the timeline item"),
        "encoded_in": ("_assert.py compute_state / compute_failure safe-guarded entity reads; "
                       "tests/unit/test_assembly_get.py TimelineObject-answers comment; "
                       "tests/unit/test_assembly_constrain.py poison-read comment"),
        "body": """
    root = des.rootComponent
    tr = adsk.core.Matrix3D.create()
    occs = []
    for nm in ("HlA", "HlB", "HlC", "HlD"):
        occ = root.occurrences.addNewComponent(tr)
        c = occ.component
        c.name = nm
        sk = c.sketches.add(c.xYConstructionPlane)
        sk.sketchCurves.sketchCircles.addByCenterRadius(
            adsk.core.Point3D.create(0.0, 0.0, 0.0), 0.5)
        c.features.extrudeFeatures.addSimple(
            sk.profiles.item(0), adsk.core.ValueInput.createByReal(0.5),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        occs.append(occ)
    geo = adsk.fusion.JointGeometry.createByPoint(
        occs[1].component.originConstructionPoint.createForAssemblyContext(occs[1]))
    ji = root.asBuiltJoints.createInput(occs[0], occs[1], geo)
    ji.setAsRevoluteJointMotion(adsk.fusion.JointDirections.ZAxisJointDirection)
    abj = root.asBuiltJoints.add(ji)
    # The rigid group takes the OTHER pair: grouping the jointed pair raises
    # "A joint in system exists for the provided input. System will be over constrained".
    coll = adsk.core.ObjectCollection.create()
    coll.add(occs[2])
    coll.add(occs[3])
    rg = root.rigidGroups.add(coll, True)
    def health_probe(ent):
        raises = 0
        for attr in ("healthState", "errorOrWarningMessage"):
            try:
                getattr(ent, attr)
            except AttributeError:
                raises += 1
        t = ent.timelineObject
        hs = t.healthState
        msg = t.errorOrWarningMessage
        return raises, isinstance(hs, int) and not isinstance(hs, bool), isinstance(msg, str)
    j_raises, j_hs, j_msg = health_probe(abj)
    g_raises, g_hs, g_msg = health_probe(rg)
    emit(j_raises == 2 and g_raises == 2 and j_hs and j_msg and g_hs and g_msg,
         "asbuilt-rigidgroup-health-via-timeline: asbuilt raises=" + str(j_raises)
         + "/2 timeline hs_int=" + str(j_hs) + " msg_str=" + str(j_msg)
         + "; rigidgroup raises=" + str(g_raises) + "/2 timeline hs_int="
         + str(g_hs) + " msg_str=" + str(g_msg))
""",
    },
    {
        "id": "evaluateexpression-returns-internal-cm",
        "claim": ("UnitsManager.evaluateExpression returns the value in INTERNAL units (cm) "
                  "regardless of the units argument - that argument only names the unit a BARE "
                  "number in the expression is read in, never the output unit"),
        "encoded_in": ("_inputs.length_value_input value_cm (the read-back compare's unit "
                       "convention); tests/unit/test_inputs.py + test_model_fillet.py "
                       "expression fakes, which answer in cm"),
        "body": """
    um = des.unitsManager
    a = um.evaluateExpression("13 mm", "mm")
    b = um.evaluateExpression("1 cm", "mm")
    c = um.evaluateExpression("2", "mm")
    emit(abs(a - 1.3) < 1e-9 and abs(b - 1.0) < 1e-9 and abs(c - 0.2) < 1e-9,
         "evaluateexpression-returns-internal-cm: '13 mm'->" + str(a) + " (expect 1.3) '1 cm'->"
         + str(b) + " (expect 1.0) bare '2' under a mm units arg->" + str(c) + " (expect 0.2)")
""",
    },
    {
        "id": "extrude-extent-distance-reads-requested-cm",
        "claim": ("An extrude's extentOne.distance.value reads back the REQUESTED distance in "
                  "internal cm across the extent forms model_extrude sets: symmetric+taper "
                  "(SymmetricExtentDefinition, the per-side number), one-sided+taper, two_side "
                  "(extentOne carries side one, extentTwo side two, both positive, no swap), a "
                  "cut that bottoms out inside its target (the requested value, never clipped), "
                  "and a NEGATIVE distance (the requested SIGN kept, -15 mm reads -1.5)"),
        "encoded_in": ("model_extrude's distance read-back compare (want_distance_cm vs "
                       "extentOne.distance.value); surface_extrude._landed_depth, the same contract"),
        "need_box": True,
        "body": """
    root = des.rootComponent
    feats = root.features.extrudeFeatures
    F = adsk.fusion.FeatureOperations
    V = adsk.core.ValueInput
    def profile_at(cx):
        sk = root.sketches.add(root.xYConstructionPlane)
        sk.sketchCurves.sketchCircles.addByCenterRadius(
            adsk.core.Point3D.create(cx, 0.0, 0.0), 0.4)
        return sk.profiles.item(0)
    i1 = feats.createInput(profile_at(3.0), F.NewBodyFeatureOperation)
    i1.setSymmetricExtent(V.createByReal(1.0), False, V.createByString("3 deg"))
    f1 = feats.add(i1)
    sym_type = type(f1.extentOne).__name__
    sym_v = f1.extentOne.distance.value
    i2 = feats.createInput(profile_at(6.0), F.NewBodyFeatureOperation)
    i2.setOneSideExtent(adsk.fusion.DistanceExtentDefinition.create(V.createByReal(1.0)),
                        adsk.fusion.ExtentDirections.PositiveExtentDirection,
                        V.createByString("3 deg"))
    f2 = feats.add(i2)
    one_v = f2.extentOne.distance.value
    i3 = feats.createInput(profile_at(9.0), F.NewBodyFeatureOperation)
    i3.setTwoSidesDistanceExtent(V.createByReal(1.0), V.createByReal(0.5))
    f3 = feats.add(i3)
    two_a = f3.extentOne.distance.value
    two_b = f3.extentTwo.distance.value
    skc = root.sketches.add(root.xYConstructionPlane)
    skc.sketchCurves.sketchCircles.addByCenterRadius(
        adsk.core.Point3D.create(0.5, 0.5, 0.0), 0.2)
    i4 = feats.createInput(skc.profiles.item(0), F.CutFeatureOperation)
    i4.setDistanceExtent(False, V.createByReal(0.3))
    f4 = feats.add(i4)
    cut_v = f4.extentOne.distance.value
    i5 = feats.createInput(profile_at(12.0), F.NewBodyFeatureOperation)
    i5.setDistanceExtent(False, V.createByReal(-1.5))
    f5 = feats.add(i5)
    neg_v = f5.extentOne.distance.value
    ok_all = (sym_type == "SymmetricExtentDefinition" and abs(sym_v - 1.0) < 1e-9
              and abs(one_v - 1.0) < 1e-9 and abs(two_a - 1.0) < 1e-9
              and abs(two_b - 0.5) < 1e-9 and abs(cut_v - 0.3) < 1e-9
              and abs(neg_v + 1.5) < 1e-9)
    emit(ok_all, "extrude-extent-distance-reads-requested-cm: sym(" + sym_type + ")="
         + str(sym_v) + " oneT=" + str(one_v) + " two=" + str(two_a) + "/" + str(two_b)
         + " cut_into_box=" + str(cut_v) + " neg=" + str(neg_v)
         + " (expect 1.0 / 1.0 / 1.0,0.5 / 0.3 / -1.5)")
""",
    },
    {
        "id": "joint-drive-moves-occurrence-one",
        "claim": ("Driving an as-built slider displaces occurrenceONE: with occurrenceTwo locked to "
                  "its parent, occurrenceOne's transform2 translation moves by +the commanded value "
                  "along the joint's slideDirectionVector and occurrenceTwo does not move at all"),
        "encoded_in": ("commands/mcpServer/tools/joint_drive.py the 'moved' placement read-back; "
                       "tests/unit/test_joint_drive.py TestMovedMember"),
        "facts_on_pass": {"behavior.joint_drive_moves_occurrence_one": True},
        "body": """
    root = des.rootComponent
    anchor = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    anchor.component.name = "DrvOneAnchor"
    off = adsk.core.Matrix3D.create()
    off.translation = adsk.core.Vector3D.create(4.0, 0.0, 0.0)
    mover = root.occurrences.addNewComponent(off)
    mover.component.name = "DrvOneMover"
    anchor.isGroundToParent = True
    geo = adsk.fusion.JointGeometry.createByPoint(
        mover.component.originConstructionPoint.createForAssemblyContext(mover))
    ji = root.asBuiltJoints.createInput(mover, anchor, geo)
    ji.setAsSliderJointMotion(adsk.fusion.JointDirections.XAxisJointDirection)
    j = root.asBuiltJoints.add(ji)
    v = j.jointMotion.slideDirectionVector
    b1, b2 = mover.transform2.translation, anchor.transform2.translation
    before_one = (b1.x, b1.y, b1.z)
    before_two = (b2.x, b2.y, b2.z)
    j.jointMotion.slideValue = 2.5
    a1, a2 = mover.transform2.translation, anchor.transform2.translation
    d_one = (a1.x - before_one[0], a1.y - before_one[1], a1.z - before_one[2])
    d_two = (a2.x - before_two[0], a2.y - before_two[1], a2.z - before_two[2])
    one_moved = max(abs(d_one[i] - 2.5 * (v.x, v.y, v.z)[i]) for i in range(3)) < 1e-6
    two_still = max(abs(c) for c in d_two) < 1e-6
    emit(one_moved and two_still,
         "joint-drive-moves-occurrence-one: one delta=" + str(d_one) + " two delta=" + str(d_two)
         + " slideDirectionVector=(" + str(v.x) + "," + str(v.y) + "," + str(v.z) + ")"
         + " (expect one = +2.5 along the vector, two = 0)")
""",
    },
    {
        "id": "joint-drive-sign-follows-slide-direction-vector",
        "claim": ("A slider drive displaces the moving member ALONG jointMotion.slideDirectionVector "
                  "- a joint built on the frame Y axis moves the part in +Y, not +X - and the sign "
                  "follows the commanded value: a negative command lands the part on the other side"),
        "encoded_in": ("commands/mcpServer/tools/joint_drive.py publishes that vector as "
                       "'slide_direction' beside the measured 'moved' delta; tests/unit/"
                       "test_joint_drive.py TestDriveDirection"),
        "facts_on_pass": {"behavior.joint_drive_sign_follows_slide_direction_vector": True},
        "body": """
    root = des.rootComponent
    anchor = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    anchor.component.name = "DrvSignAnchor"
    off = adsk.core.Matrix3D.create()
    off.translation = adsk.core.Vector3D.create(0.0, 4.0, 0.0)
    mover = root.occurrences.addNewComponent(off)
    mover.component.name = "DrvSignMover"
    anchor.isGroundToParent = True
    geo = adsk.fusion.JointGeometry.createByPoint(
        mover.component.originConstructionPoint.createForAssemblyContext(mover))
    ji = root.asBuiltJoints.createInput(mover, anchor, geo)
    ji.setAsSliderJointMotion(adsk.fusion.JointDirections.YAxisJointDirection)
    j = root.asBuiltJoints.add(ji)
    v = j.jointMotion.slideDirectionVector
    vec = (v.x, v.y, v.z)
    h = mover.transform2.translation
    home = (h.x, h.y, h.z)
    j.jointMotion.slideValue = 2.5
    p = mover.transform2.translation
    d_pos = (p.x - home[0], p.y - home[1], p.z - home[2])
    j.jointMotion.slideValue = -1.0
    n = mover.transform2.translation
    d_neg = (n.x - home[0], n.y - home[1], n.z - home[2])
    on_y = abs(vec[1]) > 0.999 and abs(vec[0]) < 1e-6 and abs(vec[2]) < 1e-6
    pos_ok = max(abs(d_pos[i] - 2.5 * vec[i]) for i in range(3)) < 1e-6
    neg_ok = max(abs(d_neg[i] + 1.0 * vec[i]) for i in range(3)) < 1e-6
    emit(on_y and pos_ok and neg_ok,
         "joint-drive-sign-follows-slide-direction-vector: vector=" + str(vec)
         + " delta(+2.5)=" + str(d_pos) + " delta(-1.0)=" + str(d_neg)
         + " (expect the vector on Y, then +2.5 and -1.0 along it)")
""",
    },
    {
        "id": "joint-drive-anchored-side-flips-mover",
        "claim": ("Which member moves is decided by which side is ANCHORED, not by the member order: "
                  "with occurrenceONE locked to its parent, the same positive slide command displaces "
                  "occurrenceTWO by MINUS the value along the same slideDirectionVector"),
        "encoded_in": ("commands/mcpServer/tools/joint_drive.py reports the member that moved "
                       "instead of naming one from the joint's member order; tests/unit/"
                       "test_joint_drive.py TestMovedMember"),
        "facts_on_pass": {"behavior.joint_drive_anchored_side_flips_mover": True},
        "body": """
    root = des.rootComponent
    anchor = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    anchor.component.name = "DrvFlipAnchor"
    off = adsk.core.Matrix3D.create()
    off.translation = adsk.core.Vector3D.create(-4.0, 0.0, 0.0)
    free = root.occurrences.addNewComponent(off)
    free.component.name = "DrvFlipFree"
    anchor.isGroundToParent = True
    geo = adsk.fusion.JointGeometry.createByPoint(
        free.component.originConstructionPoint.createForAssemblyContext(free))
    ji = root.asBuiltJoints.createInput(anchor, free, geo)
    ji.setAsSliderJointMotion(adsk.fusion.JointDirections.XAxisJointDirection)
    j = root.asBuiltJoints.add(ji)
    v = j.jointMotion.slideDirectionVector
    b1, b2 = anchor.transform2.translation, free.transform2.translation
    before_one = (b1.x, b1.y, b1.z)
    before_two = (b2.x, b2.y, b2.z)
    j.jointMotion.slideValue = 2.5
    a1, a2 = anchor.transform2.translation, free.transform2.translation
    d_one = (a1.x - before_one[0], a1.y - before_one[1], a1.z - before_one[2])
    d_two = (a2.x - before_two[0], a2.y - before_two[1], a2.z - before_two[2])
    one_still = max(abs(c) for c in d_one) < 1e-6
    two_flipped = max(abs(d_two[i] + 2.5 * (v.x, v.y, v.z)[i]) for i in range(3)) < 1e-6
    emit(one_still and two_flipped,
         "joint-drive-anchored-side-flips-mover: one(anchored) delta=" + str(d_one)
         + " two delta=" + str(d_two) + " slideDirectionVector=(" + str(v.x) + "," + str(v.y)
         + "," + str(v.z) + ") (expect one = 0, two = -2.5 along the vector)")
""",
    },
    {
        "id": "joint-revolute-value-stored-verbatim",
        "claim": ("A revolute jointMotion.rotationValue stores the angle it is GIVEN, verbatim: 750 "
                  "deg reads back 750, a following 30 deg reads back 30, 100 reads 100 and 390 reads "
                  "390 - the value neither accumulates the turn it just made nor normalizes into "
                  "[0,360)"),
        "encoded_in": ("commands/mcpServer/tools/joint_drive.py value_now + its "
                       "angle_deg_normalized twin and the equivalent_pose gate; tests/unit/"
                       "test_joint_drive.py TestEquivalentPose"),
        "facts_on_pass": {"behavior.joint_revolute_value_stored_verbatim": True},
        "body": """
    import math
    root = des.rootComponent
    anchor = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    anchor.component.name = "DrvSpinAnchor"
    off = adsk.core.Matrix3D.create()
    off.translation = adsk.core.Vector3D.create(0.0, -4.0, 0.0)
    spinner = root.occurrences.addNewComponent(off)
    spinner.component.name = "DrvSpinRotor"
    anchor.isGroundToParent = True
    geo = adsk.fusion.JointGeometry.createByPoint(
        spinner.component.originConstructionPoint.createForAssemblyContext(spinner))
    ji = root.asBuiltJoints.createInput(spinner, anchor, geo)
    ji.setAsRevoluteJointMotion(adsk.fusion.JointDirections.ZAxisJointDirection)
    j = root.asBuiltJoints.add(ji)
    reads = []
    for want in (750.0, 30.0, 100.0, 390.0):
        j.jointMotion.rotationValue = math.radians(want)
        reads.append(round(math.degrees(j.jointMotion.rotationValue), 6))
    verbatim = all(abs(reads[i] - w) < 1e-4
                   for i, w in enumerate((750.0, 30.0, 100.0, 390.0)))
    emit(verbatim,
         "joint-revolute-value-stored-verbatim: commanded 750/30/100/390 read back "
         + str(reads) + " (expect the same four values)")
""",
    },
    {
        "id": "joint-revolute-value-tenth-degree-grid",
        "claim": ("A revolute jointMotion.rotationValue lands on a 0.1 deg GRID: an angle that is "
                  "not a multiple of 0.1 deg reads back at the nearest tenth, so such a command "
                  "cannot be stored exactly (12.34, 20.103, 0.03 and 45.55 all read back on-grid). "
                  "An EXACT half-step rounds AWAY FROM ZERO on both signs - -20.15 reads -20.2 and "
                  "+20.15 reads +20.2 - and the boundary is exact: -20.1499999 reads -20.1"),
        "encoded_in": ("commands/mcpServer/tools/joint_drive.py _ANGLE_GRID_DEG / _ANGLE_BAND_DEG "
                       "(the half-step landing band) and the off-grid note; "
                       "tests/unit/test_joint_drive.py TestTheAngleBandIsHalfTheStoreGrid"),
        "facts_on_pass": {"behavior.joint_revolute_store_grid_deg": 0.1},
        "body": """
    import math
    root = des.rootComponent
    anchor = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    anchor.component.name = "GridAnchor"
    off = adsk.core.Matrix3D.create()
    off.translation = adsk.core.Vector3D.create(0.0, -8.0, 0.0)
    spinner = root.occurrences.addNewComponent(off)
    spinner.component.name = "GridRotor"
    anchor.isGroundToParent = True
    geo = adsk.fusion.JointGeometry.createByPoint(
        spinner.component.originConstructionPoint.createForAssemblyContext(spinner))
    ji = root.asBuiltJoints.createInput(spinner, anchor, geo)
    ji.setAsRevoluteJointMotion(adsk.fusion.JointDirections.ZAxisJointDirection)
    j = root.asBuiltJoints.add(ji)
    reads = []
    for want in (12.34, 20.103, 0.03, 45.55):
        j.jointMotion.rotationValue = math.radians(want)
        reads.append(round(math.degrees(j.jointMotion.rotationValue), 9))
    on_grid = all(abs(r - round(r * 10.0) / 10.0) < 1e-9 for r in reads)
    half = []
    for want in (-20.15, 20.15, -20.1499999):
        j.jointMotion.rotationValue = math.radians(want)
        half.append(round(math.degrees(j.jointMotion.rotationValue), 6))
    away_from_zero = (abs(half[0] + 20.2) < 1e-6 and abs(half[1] - 20.2) < 1e-6
                      and abs(half[2] + 20.1) < 1e-6)
    emit(on_grid and away_from_zero,
         "joint-revolute-value-tenth-degree-grid: commanded 12.34/20.103/0.03/45.55 read back "
         + str(reads) + " (expect every read-back a multiple of 0.1 deg); half-step "
         "-20.15/20.15/-20.1499999 read back " + str(half) + " (expect -20.2, 20.2, -20.1)")
""",
    },
    {
        "id": "design-cast",
        "claim": "Design.cast passes the active design through; a non-design casts to None",
        "encoded_in": "tests/conftest.py install() cast_design + install_mock_adsk Design.cast",
        "body": """
    d = adsk.fusion.Design.cast(app.activeProduct)
    n = adsk.fusion.Design.cast(adsk.core.Point3D.create(0.0, 0.0, 0.0))
    emit(d is not None and n is None,
         "design-cast: design->" + type(d).__name__ + " point->" + repr(n))
""",
    },
    {
        "id": "cam-operation-cast",
        "claim": "adsk.cam.Operation.cast(non-operation) returns None (tools filter on it)",
        "encoded_in": "tests/conftest.py install_mock_adsk cam.Operation.cast",
        "body": """
    n = adsk.cam.Operation.cast(adsk.core.Point3D.create(0.0, 0.0, 0.0))
    emit(n is None, "cam-operation-cast: non-op casts to " + repr(n))
""",
    },
    {
        "id": "find-entity-token-shape",
        "claim": "findEntityByToken returns a SWIG BaseVector - list-like (len/index/iterate) but NOT a Python list",
        "encoded_in": "tests/fakes/design.py MakeDesign.findEntityByToken",
        "need_box": True,
        "body": """
    hit = des.findEntityByToken(body.entityToken)
    tname = type(hit).__name__
    n = len(hit)
    first = type(hit[0]).__name__
    cnt = 0
    for x in hit:
        cnt += 1
    emit(tname != "list" and n == 1 and first == "BRepBody" and cnt == 1,
         "find-entity-token-shape: type=" + tname + " len=" + str(n) + " [0]=" + first
         + " iterated=" + str(cnt))
""",
    },
    {
        "id": "find-entity-token-miss",
        "claim": "A stale token, a garbage string, a plain name, and a truncated token each return an EMPTY falsy vector (len 0) - never a raise",
        "encoded_in": "tests/fakes/design.py MakeDesign.findEntityByToken; commands/mcpServer/tools/_inputs.py _resolve_token_entity fallthrough",
        "need_box": True,
        "facts_on_pass": {"behavior.find_entity_token_empty_on_miss": True},
        "body": """
    root = des.rootComponent
    sk = root.sketches.add(root.xYConstructionPlane)
    stale_tok = sk.entityToken
    sk.deleteMe()
    ok = True
    parts = []
    for label, s in (("stale", stale_tok), ("garbage", "bogus-token"),
                     ("name", body.name), ("truncated", body.entityToken[:30])):
        r = des.findEntityByToken(s)
        good = (len(r) == 0 and not bool(r))
        ok = ok and good
        parts.append(label + "=" + ("empty" if good else "NON-EMPTY len " + str(len(r))))
    emit(ok, "find-entity-token-miss: " + ", ".join(parts))
""",
    },
    {
        "id": "find-entity-token-multi",
        "claim": ("ONE token can name SEVERAL entities: splitting a face makes the PRE-split token "
                  "resolve to a vector of BOTH survivors, so taking [0] acts on geometry the caller "
                  "never picked"),
        "encoded_in": ("tests/unit/test_inputs.py token_env / _SplitFace (a LIST value models the "
                       "several entities one token answers with); commands/mcpServer/tools/"
                       "_inputs.py the locator pick-or-refuse over a multi-entity token"),
        "need_box": True,
        "facts_on_pass": {"behavior.find_entity_token_multi_after_face_split": True},
        "body": """
    root = des.rootComponent
    top = None
    for i in range(body.faces.count):
        f = body.faces.item(i)
        if top is None or f.pointOnFace.z > top.pointOnFace.z:
            top = f
    tok = top.entityToken
    before = len(des.findEntityByToken(tok))
    pi = root.constructionPlanes.createInput()
    pi.setByOffset(root.xZConstructionPlane, adsk.core.ValueInput.createByReal(0.5))
    plane = root.constructionPlanes.add(pi)
    faces = adsk.core.ObjectCollection.create()
    faces.add(top)
    si = root.features.splitFaceFeatures.createInput(faces, plane, True)
    root.features.splitFaceFeatures.add(si)
    after = des.findEntityByToken(tok)
    kinds = []
    for x in after:
        kinds.append(type(x).__name__)
    emit(before == 1 and len(after) > 1 and set(kinds) == set(["BRepFace"]),
         "find-entity-token-multi: pre_split=" + str(before) + " post_split="
         + str(len(after)) + " kinds=" + ",".join(kinds) + " (expect 1 then >1 BRepFace)")
""",
    },
    {
        "id": "allcomponents-design-only",
        "claim": "allComponents lives on Design (Component has none) and is counted AND iterable",
        "encoded_in": "tests/fakes/design.py MakeDesign.allComponents (the Component shape dump carries no allComponents, which is what keeps MakeComp from offering one)",
        "body": """
    comp_has = hasattr(des.rootComponent, "allComponents")
    ac = des.allComponents
    n = ac.count
    cnt = 0
    for c in ac:
        cnt += 1
    emit((not comp_has) and n == cnt and n >= 1,
         "allcomponents-design-only: on Component=" + str(comp_has) + " count=" + str(n)
         + " iterated=" + str(cnt))
""",
    },
    {
        "id": "brepbodies-protocol",
        "claim": "BRepBodies supports count / item(i) / itemByName (None on a miss) / iteration",
        "encoded_in": "tests/fakes/scaffold.py _NamedCollection",
        "need_box": True,
        "facts_on_pass": {"behavior.item_by_name_none_on_miss": True},
        "body": """
    bb = des.rootComponent.bRepBodies
    hit = bb.itemByName(body.name)
    miss = bb.itemByName("NoSuchBody")
    names = [x.name for x in bb]
    emit(bb.count >= 1 and hit is not None and miss is None and body.name in names
         and bb.item(0) is not None,
         "brepbodies-protocol: count=" + str(bb.count) + " hit=" + str(hit is not None)
         + " miss=" + repr(miss) + " iterated=" + str(len(names)))
""",
    },
    {
        "id": "timeline-group-collapse-shape",
        "claim": "A TimelineGroup is created COLLAPSED, and collapse decides what timeline.item(i) enumerates: collapsed, the group's own row is present and its members are absent; expanded, the members are present (carrying parentGroup) and the group's own row is NOT enumerated at all. TimelineObject.index RAISES 'InternalValidationError : res >= 0' on a member reached through a COLLAPSED group while name/isGroup/isSuppressed/healthState/parentGroup all read, so such a row is addressable by NAME only; the same member answers an int index once the group is expanded",
        "encoded_in": "commands/mcpServer/tools/design_get.py _tally_group + _GROUP_MEMBER_NOTE; "
                      "tests/unit/test_design_get.py TestTimelineSlice",
        "facts_on_pass": {
            "behavior.timeline_group_created_collapsed": True,
            "behavior.timeline_collapsed_member_index_raises": True,
            "behavior.timeline_group_row_enumerated_only_when_collapsed": True,
        },
        "body": """
    root = des.rootComponent
    tl = des.timeline
    base = tl.count
    for i in range(3):
        s = root.sketches.add(root.xYConstructionPlane)
        s.name = "TGrpMeasure%d" % i
    g = tl.timelineGroups.add(base, base + 2)
    g.name = "TGrpMeasure"
    created_collapsed = g.isCollapsed
    rows_collapsed = tl.count
    row_present_collapsed = any(tl.item(i).isGroup and tl.item(i).name == "TGrpMeasure"
                                for i in range(tl.count))
    member = g.item(0)
    index_raised = False
    try:
        member.index
    except Exception as e:
        index_raised = "InternalValidationError" in str(e)
    reads = {}
    for attr in ("name", "isGroup", "isSuppressed", "healthState", "parentGroup"):
        try:
            getattr(member, attr)
            reads[attr] = True
        except Exception:
            reads[attr] = False
    g.isCollapsed = False
    rows_expanded = tl.count
    row_present_expanded = any(tl.item(i).isGroup and tl.item(i).name == "TGrpMeasure"
                               for i in range(tl.count))
    index_when_expanded = None
    try:
        index_when_expanded = g.item(0).index
    except Exception:
        pass
    emit(created_collapsed is True and index_raised is True and all(reads.values())
         and row_present_collapsed is True and row_present_expanded is False
         and rows_expanded == rows_collapsed + 2
         and isinstance(index_when_expanded, int),
         "timeline-group-collapse-shape: created_collapsed=" + str(created_collapsed)
         + " rows_collapsed=" + str(rows_collapsed)
         + " rows_expanded=" + str(rows_expanded)
         + " group_row_collapsed=" + str(row_present_collapsed)
         + " group_row_expanded=" + str(row_present_expanded)
         + " member_index_raised=" + str(index_raised)
         + " member_reads=" + str(reads)
         + " member_index_expanded=" + str(index_when_expanded))
""",
    },
    {
        "id": "item-oor-brepbodies",
        "claim": "BRepBodies.item(out-of-range) never returns None - it raises RuntimeError. This row's body catches the raise and gates on its type; the expect also accepts a script-level abort, so a PASS does not say which of the two the run saw. The abort half is not this row's measurement either way: it is the live observation the module docstring records, that an out-of-range item() raise escaped try/except and killed a whole script invocation",
        "encoded_in": "tests/fakes/scaffold.py _NamedCollection.item",
        "need_box": True,
        "expect": "raise_or_abort",
        "facts_on_pass": {"behavior.collection_item_out_of_range_raises": True},
        "body": """
    try:
        r = des.rootComponent.bRepBodies.item(9999)
        emit(False, "item-oor-brepbodies: returned " + repr(r) + " with no raise")
    except Exception as e:
        emit(type(e).__name__ == "RuntimeError",
             "item-oor-brepbodies: raised catchably " + type(e).__name__ + ": " + str(e)[:60])
""",
    },
    {
        "id": "item-oor-sketches",
        "claim": "Sketches.item(out-of-range) never returns None - it raises. This row's body catches the raise and gates on its type; the expect also accepts a script-level abort, so a PASS does not say which of the two the run saw. The abort half is not this row's measurement either way: it is the live observation the module docstring records, that an out-of-range item() raise escaped try/except and killed a whole script invocation",
        "encoded_in": "tests/fakes/scaffold.py _NamedCollection.item",
        "expect": "raise_or_abort",
        "facts_on_pass": {"behavior.collection_item_out_of_range_raises": True},
        "body": """
    try:
        r = des.rootComponent.sketches.item(9999)
        emit(False, "item-oor-sketches: returned " + repr(r) + " with no raise")
    except Exception as e:
        emit(type(e).__name__ == "RuntimeError",
             "item-oor-sketches: raised catchably " + type(e).__name__)
""",
    },
    {
        "id": "objectcollection-protocol",
        "claim": "ObjectCollection.create() yields add / count / item(i) / iteration",
        "encoded_in": "tests/fakes/scaffold.py _FakeObjectCollection",
        "need_box": True,
        "body": """
    oc = adsk.core.ObjectCollection.create()
    oc.add(body)
    cnt = 0
    for x in oc:
        cnt += 1
    emit(oc.count == 1 and oc.item(0) is not None and cnt == 1,
         "objectcollection-protocol: count=" + str(oc.count) + " iterated=" + str(cnt))
""",
    },
    {
        "id": "meshbodies-no-itembyname",
        "claim": "A component's meshBodies collection has count/item but NO itemByName (unlike bRepBodies, which has all three) - a mesh must be resolved by iterate-and-match, never itemByName",
        "encoded_in": "tests/fakes/mesh.py _MeshBodies (drops itemByName off the flag; design.py's MakeComp builds meshBodies from it)",
        "facts_on_pass": {"behavior.meshbodies_has_itembyname": False},
        "body": """
    root = adsk.fusion.Design.cast(app.activeProduct).rootComponent
    mb = root.meshBodies
    bb = root.bRepBodies
    emit(hasattr(mb, "count") and hasattr(mb, "item") and not hasattr(mb, "itemByName")
         and hasattr(bb, "itemByName"),
         "meshbodies-no-itembyname: mesh count/item/itemByName="
         + str(hasattr(mb, "count")) + "/" + str(hasattr(mb, "item")) + "/"
         + str(hasattr(mb, "itemByName")) + " brep.itemByName=" + str(hasattr(bb, "itemByName")))
""",
    },
    {
        "id": "camera-viewextents-is-linear-not-area",
        "claim": "Camera.viewExtents is a LINEAR extent, not an area: with the LIMITING screen axis held fixed, a model N times taller fits to a value ~N times larger, not ~N squared. The SDK words it 'the area of the view', which is why this is measured rather than read",
        "encoded_in": "view_set.py _frame_ratio (the ratio multiplies viewExtents unsquared) and its point-of-use comment; view_screenshot.py's zoom, which scales the same property; test_view_set.py TestFocusFraming ratio expectations",
        "facts_on_pass": {"behavior.camera_view_extents_is_linear": True},
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        des = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        root = des.rootComponent
        vp = app.activeViewport

        # A 1 cm square post of a given height. Growing only the HEIGHT, on a front camera, on a
        # viewport wider than it is tall, keeps BOTH fits limited by the same screen axis - which
        # is the whole point: a first attempt at this row left the camera wherever the document had
        # it, so an iso-ish view spread the growth across both screen axes and the ratio came back
        # 5.22, neither linear (10.98) nor area (120.6). A ratio is only meaningful between two fits
        # limited by the SAME axis (which the sibling row measures).
        def post(height):
            sk = root.sketches.add(root.xYConstructionPlane)
            sk.sketchCurves.sketchLines.addTwoPointRectangle(
                adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(1.0, 1.0, 0.0))
            root.features.extrudeFeatures.addSimple(
                sk.profiles.item(0), adsk.core.ValueInput.createByReal(height),
                adsk.fusion.FeatureOperations.NewBodyFeatureOperation)

        def spans():
            bb = root.boundingBox
            return (bb.maxPoint.x - bb.minPoint.x, bb.maxPoint.z - bb.minPoint.z,
                    (bb.maxPoint.z + bb.minPoint.z) / 2.0)

        def fit_front():
            across, up, mid = spans()
            cam = vp.camera                      # front: x across the screen, z up it
            cam.upVector = adsk.core.Vector3D.create(0.0, 0.0, 1.0)
            cam.target = adsk.core.Point3D.create(0.5, 0.0, mid)
            cam.eye = adsk.core.Point3D.create(0.5, -100.0, mid)
            cam.isFitView = True
            vp.camera = cam
            return vp.camera.viewExtents

        post(2.0)
        w0, h0, _ = spans()
        e0 = fit_front()
        post(20.0)                               # same 1 cm footprint, ten times taller
        w1, h1, _ = spans()
        e1 = fit_front()
        aspect = float(vp.width) / vp.height
        model_ratio = h1 / h0
        ext_ratio = e1 / e0
        # both fits must be HEIGHT-limited or the comparison is meaningless - assert it, do not
        # assume it, since that assumption is exactly what the first version of this row got wrong
        height_limited = (w0 / h0) < aspect and (w1 / h1) < aspect
        emit(height_limited and abs(ext_ratio - model_ratio) < model_ratio * 0.2,
             "camera-viewextents-is-linear-not-area: height " + ("%.3f" % h0) + "->" + ("%.3f" % h1)
             + " (x" + ("%.2f" % model_ratio) + ") extents " + ("%.3f" % e0) + "->" + ("%.3f" % e1)
             + " (x" + ("%.2f" % ext_ratio) + ") area_would_be x"
             + ("%.1f" % (model_ratio * model_ratio))
             + " height_limited=" + repr(height_limited) + " aspect=" + ("%.3f" % aspect))
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "camera-viewextents-follows-limiting-axis",
        "claim": "Camera.viewExtents after a fit tracks whichever SCREEN AXIS limited that fit - measured on two models on one front camera: a TALL post fits to its height, a WIDE slab to its width - so it is neither the view width nor the view height, and a framing ratio cannot be taken against a world box's own spans",
        "encoded_in": "view_set.py _frame_ratio, which reconstructs the FRAME from the viewport aspect (vp.width/vp.height) instead of dividing by the world box; test_view_set.py test_a_flat_world_is_measured_against_the_frame_not_its_own_height",
        "facts_on_pass": {"behavior.camera_view_extents_follows_limiting_axis": True},
        "body": """
    def fit_front(across_cm, up_cm):
        # One temp document per shape: a second body in the same root would sit inside the fit and
        # both models would be framed at once.
        tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
        try:
            d = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
            root = d.rootComponent
            sk = root.sketches.add(root.xYConstructionPlane)
            sk.sketchCurves.sketchLines.addTwoPointRectangle(
                adsk.core.Point3D.create(0.0, 0.0, 0.0),
                adsk.core.Point3D.create(across_cm, 1.0, 0.0))
            root.features.extrudeFeatures.addSimple(
                sk.profiles.item(0), adsk.core.ValueInput.createByReal(up_cm),
                adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
            vp = app.activeViewport
            cam = vp.camera                   # front: x runs across the screen, z up it
            cam.eye = adsk.core.Point3D.create(across_cm / 2.0, -50.0, up_cm / 2.0)
            cam.target = adsk.core.Point3D.create(across_cm / 2.0, 0.0, up_cm / 2.0)
            cam.upVector = adsk.core.Vector3D.create(0.0, 0.0, 1.0)
            cam.isFitView = True
            vp.camera = cam
            bb = root.boundingBox
            return (bb.maxPoint.x - bb.minPoint.x, bb.maxPoint.z - bb.minPoint.z,
                    vp.camera.viewExtents, float(vp.width) / vp.height)
        finally:
            tmp.close(False)

    # A TALL post (1 cm across, 10 cm up) and a WIDE slab (10 across, 1 up) on the same front
    # camera. The two fits are limited by DIFFERENT screen axes, which is what tells a value that
    # follows the limiting axis apart from one that always reports the same axis - one model
    # cannot: a height-limited fit reads the same either way.
    across_t, up_t, ext_t, aspect = fit_front(1.0, 10.0)
    across_w, up_w, ext_w, _aspect2 = fit_front(10.0, 1.0)
    # the limiting axis is asserted, not assumed: a model narrower than the frame is height-limited
    tall_by_height = (across_t / up_t) < aspect
    wide_by_width = (across_w / up_w) > aspect
    tracks_up = abs(ext_t - up_t) < up_t * 0.3 and ext_t > across_t * 2.0
    tracks_across = abs(ext_w - across_w) < across_w * 0.3 and ext_w > up_w * 2.0
    emit(tall_by_height and wide_by_width and tracks_up and tracks_across,
         "camera-viewextents-follows-limiting-axis: aspect=" + ("%.3f" % aspect)
         + " tall across=" + ("%.2f" % across_t) + " up=" + ("%.2f" % up_t)
         + " extents=" + ("%.3f" % ext_t)
         + "; wide across=" + ("%.2f" % across_w) + " up=" + ("%.2f" % up_w)
         + " extents=" + ("%.3f" % ext_w)
         + "; tall_by_height=" + repr(tall_by_height) + " wide_by_width=" + repr(wide_by_width)
         + " tracks_up=" + repr(tracks_up) + " tracks_across=" + repr(tracks_across))
""",
    },
    {
        "id": "meshbody-volume-open-returns-zero",
        "claim": "MeshBody.volume on a mesh that is NOT closed RETURNS 0.0 - it does not raise; a null volume in a payload therefore means the field could not be read at all, never 'the mesh is open'",
        "encoded_in": "_mesh_common.py _mesh_summary + mesh_get's note/description; mesh_shell.py _closed (the reason the closure flag is sampled at both ends); tests/fakes/mesh.py's MeshBody fake, which reads this BEHAVIOR flag rather than hard-coding it; test_mesh_get.py's own MeshBody",
        "facts_on_pass": {"behavior.meshbody_volume_open_raises": False},
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        des = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        des.designType = adsk.fusion.DesignTypes.DirectDesignType
        mb = des.rootComponent.meshBodies.addByTriangleMeshData(
            [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0], [0, 1, 2], [], [])
        v = mb.volume
        emit((mb.isClosed is False) and isinstance(v, float) and v == 0.0,
             "meshbody-volume-open-returns-zero: isClosed=" + str(mb.isClosed)
             + " volume=" + repr(v))
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "meshbody-delete-answers-true-and-removes",
        "claim": "MeshBody.deleteMe() answers True and drops the body from meshBodies in every construction measured here - a PARAMETRIC body added in a base-feature scope, one deleted while ANOTHER component's base-feature scope is open, one a downstream mesh-repair feature consumed, and an assembly-context PROXY. A wrapper whose body is already gone RAISES ('An API Object refers to a deleted Object') rather than answering False, so no measured path returns a False here",
        "encoded_in": "tests/fakes/mesh.py MeshBody.deleteMe - its `deletes` False is a DECLARED state (the fake's docstring says so), because no construction measured here declines; mesh_delete.py, which reports a False rather than swallowing it",
        "body": """
    FLAT = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    TET = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    TETI = [0, 2, 1, 0, 1, 3, 1, 2, 3, 2, 0, 3]

    def parametric_legs():
        tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
        try:
            d = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
            root = d.rootComponent
            def add_mesh(comp, coords, idx):
                # A PARAMETRIC design takes a mesh only inside a base-feature edit scope.
                bf = comp.features.baseFeatures.add()
                bf.startEdit()
                comp.meshBodies.addByTriangleMeshData(coords, idx, [], [])
                bf.finishEdit()
                return comp.meshBodies.item(comp.meshBodies.count - 1)
            a = add_mesh(root, FLAT, [0, 1, 2])
            plain = (a.deleteMe(), root.meshBodies.count)
            b = add_mesh(root, FLAT, [0, 1, 2])
            other = root.occurrences.addNewComponent(adsk.core.Matrix3D.create()).component
            bf2 = other.features.baseFeatures.add()
            bf2.startEdit()
            scoped = (b.deleteMe(), root.meshBodies.count)
            bf2.finishEdit()
            c = add_mesh(root, TET, TETI)
            rf = root.features.meshRepairFeatures
            ri = rf.createInput(c)
            ri.meshRepairType = adsk.fusion.MeshRepairTypes.RebuildMeshRepairType
            rf.add(ri)
            consumed = (c.deleteMe(), root.meshBodies.count)
            return plain, scoped, consumed
        finally:
            tmp.close(False)

    def direct_legs():
        tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
        try:
            d = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
            d.designType = adsk.fusion.DesignTypes.DirectDesignType
            root = d.rootComponent
            comp = root.occurrences.addNewComponent(adsk.core.Matrix3D.create()).component
            comp.meshBodies.addByTriangleMeshData(FLAT, [0, 1, 2], [], [])
            native = comp.meshBodies.item(0)
            off = adsk.core.Matrix3D.create()
            off.translation = adsk.core.Vector3D.create(5.0, 0.0, 0.0)
            second = root.occurrences.addExistingComponent(comp, off)
            proxied = (native.createForAssemblyContext(second).deleteMe(), comp.meshBodies.count)
            comp.meshBodies.addByTriangleMeshData(FLAT, [0, 1, 2], [], [])
            gone = comp.meshBodies.item(0)
            gone.deleteMe()
            try:
                again = repr(gone.deleteMe())
            except Exception as ex:
                again = "raised " + type(ex).__name__ + ": " + str(ex)[:80]
            return proxied, again
        finally:
            tmp.close(False)

    plain, scoped, consumed = parametric_legs()
    proxied, again = direct_legs()
    legs = (plain, scoped, consumed, proxied)
    emit(all(r is True and n == 0 for r, n in legs) and again.startswith("raised")
         and "deleted Object" in again,
         "meshbody-delete-answers-true-and-removes: (returned, count_after) plain=" + repr(plain)
         + " foreign_scope_open=" + repr(scoped) + " repair_consumed=" + repr(consumed)
         + " proxy=" + repr(proxied) + "; second deleteMe on the same wrapper " + again)
""",
    },
    {
        "id": "meshbody-facegroups-counted-before-generation",
        "claim": "MeshBody.faceGroups answers a FaceGroups collection - never None, the read never raises - whose count is a plain int reading 1, NOT 0, on a mesh straight from addByTriangleMeshData, before any MeshGenerateFaceGroupsFeature has run; that holds for an open one-triangle mesh and for a closed tetrahedron alike, and the single group items() answers is a FaceGroup. An STL IMPORTED through meshBodies.add reads that same 1 before generation, and an accurate generation on its four facets moves the count to 4",
        "encoded_in": "tests/fakes/mesh.py MeshBody's `face_groups` knob, defaulting to the measured 1 a mesh already publishes; mesh_generate_face_groups.py's before/after faceGroups.count read-back and tests/live/verify_acts_mesh.py's observed-read-back predicate",
        "body": """
    import os, tempfile
    NL = chr(10)
    facets = [((0.0, 0.0, -1.0), ((0, 0, 0), (0, 10, 0), (10, 0, 0))),
              ((0.0, -1.0, 0.0), ((0, 0, 0), (10, 0, 0), (0, 0, 10))),
              ((-1.0, 0.0, 0.0), ((0, 0, 0), (0, 0, 10), (0, 10, 0))),
              ((0.5773503, 0.5773503, 0.5773503), ((10, 0, 0), (0, 10, 0), (0, 0, 10)))]
    text = ["solid t"]
    for nrm, tri in facets:
        text.append("facet normal %g %g %g" % nrm)
        text.append("outer loop")
        for v in tri:
            text.append("vertex %g %g %g" % v)
        text += ["endloop", "endfacet"]
    text.append("endsolid t")
    stl_path = os.path.join(tempfile.gettempdir(), "fe_measure_tetra.stl")
    fh = open(stl_path, "w")
    fh.write(NL.join(text) + NL)
    fh.close()
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        des = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        des.designType = adsk.fusion.DesignTypes.DirectDesignType
        root = des.rootComponent
        flat = root.meshBodies.addByTriangleMeshData(
            [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0], [0, 1, 2], [], [])
        tet = root.meshBodies.addByTriangleMeshData(
            [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            [0, 2, 1, 0, 1, 3, 1, 2, 3, 2, 0, 3], [], [])
        f_groups, t_groups = flat.faceGroups, tet.faceGroups
        f_n, t_n = f_groups.count, t_groups.count
        item_type = type(f_groups.item(0)).__name__ if f_n else "NONE"
        stl = root.meshBodies.add(
            stl_path, adsk.fusion.MeshUnits.MillimeterMeshUnit, None).item(0)
        i_before = stl.faceGroups.count
        fg_in = root.features.meshGenerateFaceGroupsFeatures.createInput(stl)
        fg_in.meshGenerateFaceGroupsMethodType = \\
            adsk.fusion.MeshGenerateFaceGroupsMethodTypes.AccurateGenerateFaceGroupsType
        root.features.meshGenerateFaceGroupsFeatures.add(fg_in)
        i_after = stl.faceGroups.count
        emit(f_groups is not None and t_groups is not None
             and type(f_n) is int and type(t_n) is int and f_n == 1 and t_n == 1
             and item_type == "FaceGroup" and i_before == 1 and i_after == 4,
             "meshbody-facegroups-counted-before-generation: open mesh isClosed="
             + str(flat.isClosed) + " count=" + repr(f_n)
             + "; closed mesh isClosed=" + str(tet.isClosed) + " count=" + repr(t_n)
             + "; item(0) type=" + item_type
             + "; imported STL count before=" + repr(i_before)
             + " after an accurate generation=" + repr(i_after))
    finally:
        tmp.close(False)
        try:
            os.remove(stl_path)
        except Exception:
            pass
""",
    },
    {
        "id": "facegroup-generation-reads-carry-no-verdict",
        "claim": "A face-group generation that LANDED can leave both observable reads standing still: on a coplanar 2-triangle mesh an accurate MeshGenerateFaceGroupsFeature returns no feature and leaves faceGroups.count at 1 - the count an unsegmented mesh already reads - while the groups' tempIds move [0] -> [1]. A FAST re-generation of an already-segmented tetrahedron leaves the count at 4 AND both id reads identical (FaceGroup.tempId [1,2,3,4] and PolygonMesh.triangleFaceGroupTempIds alike), while an ACCURATE re-generation of the same mesh holds the count at 4 and moves the group tempIds to [5,6,7,8]. So neither the count nor the tempIds tells a generation that did nothing from one that landed, and a tool must report the pair rather than judge it",
        "encoded_in": "mesh_generate_face_groups.py, which publishes face_group_count_before / face_group_count / changed / face_group_ids_changed off FaceGroup.tempId and refuses ONLY when no read-back answers at all; tests/live/verify_acts_mesh.py's count-moved predicate on a box mesh",
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        des = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        des.designType = adsk.fusion.DesignTypes.DirectDesignType
        root = des.rootComponent
        feats = root.features.meshGenerateFaceGroupsFeatures
        MT = adsk.fusion.MeshGenerateFaceGroupsMethodTypes

        def group_ids(mb):
            g = mb.faceGroups
            return [g.item(i).tempId for i in range(g.count)]

        def generate(mb, fast):
            inp = feats.createInput(mb)
            inp.meshGenerateFaceGroupsMethodType = (MT.FastGenerateFaceGroupsType if fast
                                                    else MT.AccurateGenerateFaceGroupsType)
            return feats.add(inp)

        flat = root.meshBodies.addByTriangleMeshData(
            [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 0.0],
            [0, 1, 2, 0, 2, 3], [], [])
        f_before, f_ids_before = flat.faceGroups.count, group_ids(flat)
        f_feat = generate(flat, False)
        f_after, f_ids_after = flat.faceGroups.count, group_ids(flat)

        tet = root.meshBodies.addByTriangleMeshData(
            [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            [0, 2, 1, 0, 1, 3, 1, 2, 3, 2, 0, 3], [], [])
        generate(tet, True)
        t_first, t_ids_first = tet.faceGroups.count, group_ids(tet)
        t_tri_first = list(tet.mesh.triangleFaceGroupTempIds)
        t_feat = generate(tet, True)
        t_again, t_ids_again = tet.faceGroups.count, group_ids(tet)
        t_tri_again = list(tet.mesh.triangleFaceGroupTempIds)
        a_feat = generate(tet, False)
        t_acc, t_ids_acc = tet.faceGroups.count, group_ids(tet)
        emit(f_feat is None and f_before == 1 and f_after == 1
             and f_ids_before == [0] and f_ids_after == [1]
             and t_feat is None and t_first == 4 and t_again == 4
             and t_ids_first == [1, 2, 3, 4] and t_ids_again == t_ids_first
             and t_tri_first == [1, 2, 3, 4] and t_tri_again == t_tri_first
             and a_feat is None and t_acc == 4 and t_ids_acc == [5, 6, 7, 8],
             "facegroup-generation-reads-carry-no-verdict: coplanar mesh feat=" + repr(f_feat)
             + " count " + repr(f_before) + "->" + repr(f_after)
             + " group tempIds " + repr(f_ids_before) + "->" + repr(f_ids_after)
             + "; tetrahedron FAST re-run count " + repr(t_first) + "->" + repr(t_again)
             + " group tempIds " + repr(t_ids_first) + "->" + repr(t_ids_again)
             + " triangle tempIds " + repr(t_tri_first) + "->" + repr(t_tri_again)
             + "; ACCURATE re-run count " + repr(t_acc) + " group tempIds "
             + repr(t_ids_again) + "->" + repr(t_ids_acc))
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "brepbody-area-cm2-solid-and-open-surface",
        "claim": "BRepBody.area reads a float in cm2 on BOTH a solid and an open (non-closed) surface body: a 2 cm cube reads 24.0 and a 2 x 3 cm single-face extruded open profile reads 6.0. The surface body reads isSolid False and volume 0.0 while its area still answers, so a null area in a payload means the field could not be read, never 'the body is open'",
        "encoded_in": "tests/fakes/design.py BRepBody's `area` knob (cm2); surface_trim.py's before/after area read-back on a surface body; _sys_common.py _selection_record's area_cm2",
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        d = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        root = d.rootComponent
        sk = root.sketches.add(root.xYConstructionPlane)
        sk.sketchCurves.sketchLines.addTwoPointRectangle(
            adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(2.0, 2.0, 0.0))
        solid = root.features.extrudeFeatures.addSimple(
            sk.profiles.item(0), adsk.core.ValueInput.createByReal(2.0),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation).bodies.item(0)
        # An OPEN profile: extrudeFeatures.createInput refuses a bare open sketch line, and
        # createOpenProfile is what turns one into the profile a surface extrude accepts.
        sk2 = root.sketches.add(root.xZConstructionPlane)
        sk2.sketchCurves.sketchLines.addByTwoPoints(
            adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(2.0, 0.0, 0.0))
        ein = root.features.extrudeFeatures.createInput(
            root.createOpenProfile(sk2.sketchCurves.sketchLines.item(0)),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        ein.isSolid = False
        ein.setDistanceExtent(False, adsk.core.ValueInput.createByReal(3.0))
        surf = root.features.extrudeFeatures.add(ein).bodies.item(0)
        sa, ua = solid.area, surf.area
        emit(isinstance(sa, float) and abs(sa - 24.0) < 1e-6
             and isinstance(ua, float) and abs(ua - 6.0) < 1e-6
             and solid.isSolid is True and surf.isSolid is False and surf.volume == 0.0,
             "brepbody-area-cm2-solid-and-open-surface: solid 2cm cube isSolid="
             + str(solid.isSolid) + " volume=" + repr(solid.volume) + " area=" + repr(sa)
             + " (expect 24.0); open 2x3cm surface isSolid=" + str(surf.isSolid)
             + " faces=" + str(surf.faces.count) + " volume=" + repr(surf.volume)
             + " area=" + repr(ua) + " (expect 6.0)")
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "meshbody-area-and-boundingbox-open-mesh",
        "claim": "MeshBody.area and MeshBody.boundingBox both READ on an open (non-watertight) mesh, the volume-open row's one-triangle mesh: area is a float in cm2 reading 0.5 for the unit right triangle, and boundingBox is a BoundingBox3D whose min/max are the mesh's own extents (0,0,0)-(1,1,0). Neither read raises and neither answers None, so a null area or box in a payload means the field could not be read, never 'the mesh is open'",
        "encoded_in": "tests/fakes/mesh.py MeshBody's `area` and `bbox` knobs; _mesh_common.py _area_volume and mesh_measure_of_body",
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        des = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        des.designType = adsk.fusion.DesignTypes.DirectDesignType
        mb = des.rootComponent.meshBodies.addByTriangleMeshData(
            [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0], [0, 1, 2], [], [])
        a = mb.area
        bb = mb.boundingBox
        lo, hi = bb.minPoint, bb.maxPoint
        box = (lo.x, lo.y, lo.z, hi.x, hi.y, hi.z)
        emit(mb.isClosed is False and isinstance(a, float) and abs(a - 0.5) < 1e-9
             and type(bb).__name__ == "BoundingBox3D"
             and box == (0.0, 0.0, 0.0, 1.0, 1.0, 0.0),
             "meshbody-area-and-boundingbox-open-mesh: isClosed=" + str(mb.isClosed)
             + " area=" + repr(a) + " (expect 0.5) bbox=" + type(bb).__name__
             + " min/max=" + repr(box) + " (expect (0,0,0,1,1,0))")
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "shape-dump-mesh-world",
        "claim": "MeshBody and its PolygonMesh dump non-empty member lists, the latter carrying both nodeCoordinatesAsDouble and normalVectorsAsDouble - the arrays a smooth's coordinate diff and a reverse's normal negation are judged on. displayMesh is a TriangleMesh, dumped alongside so the count fake is swept too. Totals are not pinned: they vary by a member or two across rigs and builds, and the SHAPE lines are the product",
        "encoded_in": "tests/fakes/mesh.py's MeshBody / _FakePolygonMesh / _FakeTriangleMesh fakes; tests/lints/test_fake_shapes_exist.py sweeps them against these dumps",
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        des = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        des.designType = adsk.fusion.DesignTypes.DirectDesignType
        mb = des.rootComponent.meshBodies.addByTriangleMeshData(
            [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0], [0, 1, 2], [], [])
        n_body = dump_shape("MeshBody", mb)
        n_poly = dump_shape("PolygonMesh", mb.mesh)
        n_tri = dump_shape("TriangleMesh", mb.displayMesh)
        # The SHAPES key is the LABEL passed above, and the fake-shape lint maps a fake onto it by
        # name - so the type displayMesh actually answers is read, not assumed.
        tri_type = type(mb.displayMesh).__name__
        arrays = [n for n in dir(mb.mesh) if not n.startswith("_")]
        emit(n_body > 0 and n_poly > 0 and n_tri > 0 and tri_type == "TriangleMesh"
             and "nodeCoordinatesAsDouble" in arrays and "normalVectorsAsDouble" in arrays,
             "shape-dump-mesh-world: MeshBody " + str(n_body) + " PolygonMesh " + str(n_poly)
             + " TriangleMesh " + str(n_tri) + " displayMesh type=" + tri_type
             + " arrays_present="
             + str("nodeCoordinatesAsDouble" in arrays and "normalVectorsAsDouble" in arrays))
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "mesh-repair-density-default",
        "claim": "MeshRepairFeatures rebuild with density LEFT UNSET creates a feature whose density reads 128.0 (the wire's '(default 128)' is measured, not assumed), and MeshRepairFeature carries NO 'parameters' collection - density comes back off feat.density, a ModelParameter, or not at all",
        "encoded_in": "mesh_repair.py's density input description and its off-the-feature read-back; tests/unit/test_mesh_repair.py",
        "facts_on_pass": {"behavior.mesh_repair_density_default": 128.0,
                          "behavior.mesh_repair_feature_has_parameters": False},
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        des = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        root = des.rootComponent
        bf = root.features.baseFeatures.add()
        bf.startEdit()
        root.meshBodies.addByTriangleMeshData(
            [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            [0, 2, 1, 0, 1, 3, 1, 2, 3, 2, 0, 3], [], [])
        bf.finishEdit()
        mb = root.meshBodies.item(0)
        feats = root.features.meshRepairFeatures
        inp = feats.createInput(mb)
        inp.meshRepairType = adsk.fusion.MeshRepairTypes.RebuildMeshRepairType
        feat = feats.add(inp)
        d = feat.density
        val = d.value if hasattr(d, "value") else d
        has_params = hasattr(feat, "parameters")
        emit(abs(float(val) - 128.0) < 1e-9 and not has_params,
             "mesh-repair-density-default: density=" + repr(val)
             + " has_parameters=" + str(has_params))
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "enum-cam-operation-states",
        "claim": "OperationStates ints: IsValid=0, IsInvalid=1 (surfaced as out_of_date by the CAM layer), Suppressed=2, NoToolpath=3",
        "encoded_in": "tests/unit/test__cam_common.py state-label map; _cam_common.py operationState reads",
        "body": """
    S = adsk.cam.OperationStates
    dump_enum("cam.OperationStates", S)
    emit(S.IsValidOperationState == 0 and S.IsInvalidOperationState == 1
         and S.SuppressedOperationState == 2 and S.NoToolpathOperationState == 3,
         "enum-cam-operation-states: valid=" + str(S.IsValidOperationState)
         + " invalid=" + str(S.IsInvalidOperationState)
         + " suppressed=" + str(S.SuppressedOperationState)
         + " no_toolpath=" + str(S.NoToolpathOperationState))
""",
    },
    {
        "id": "enum-setup-stock-modes",
        "claim": "SetupStockModes.SolidStock == 6 (the literal the stock-assignment gate keys on)",
        "encoded_in": "tests/unit/test_cam_edit_setup.py; cam_edit_setup.py SetupStockModes.SolidStock",
        "body": """
    dump_enum("cam.SetupStockModes", adsk.cam.SetupStockModes)
    emit(adsk.cam.SetupStockModes.SolidStock == 6,
         "enum-setup-stock-modes: SolidStock=" + str(adsk.cam.SetupStockModes.SolidStock))
""",
    },
    {
        "id": "enum-cam-machine-template",
        "claim": "MachineTemplate carries EXACTLY seven int members: GenericLathe=0 (a FALSY member, so a `not member` guard rejects the one valid lathe template), Generic3Axis=1, Generic4Axis=2, Generic5AxisHeadHead=3, Generic5AxisHeadTable=4, Generic5AxisTableTable=5, GenericFFF=6. The automatic enum sweep cannot see this family - its name ends in none of the ...Types/...States/...Modes suffixes the scrape matches - so it is pinned here or nowhere",
        "encoded_in": "cam_create_machine.py _TEMPLATES (wire value -> member name); tests/unit/test_cam_create_machine.py's MachineTemplate namespace",
        "body": """
    T = adsk.cam.MachineTemplate
    dump_enum("cam.MachineTemplate", T)
    members = [n for n in dir(T) if not n.startswith("_") and isinstance(getattr(T, n), int)]
    emit(len(members) == 7 and T.GenericLathe == 0 and T.Generic3Axis == 1
         and T.Generic4Axis == 2 and T.Generic5AxisHeadHead == 3
         and T.Generic5AxisHeadTable == 4 and T.Generic5AxisTableTable == 5
         and T.GenericFFF == 6,
         "enum-cam-machine-template: " + str(len(members)) + " members lathe="
         + str(T.GenericLathe) + " fff=" + str(T.GenericFFF))
""",
    },
    {
        "id": "enum-distance-units-collides-with-factory",
        "claim": "DistanceUnits.MillimeterDistanceUnits == 0 AND a freshly created STLExportOptions reads unitType == 0 - the mm member IS the factory value, so a set-then-read-back of unitType cannot tell an assignment that took from one that never happened, and only for that member. What the file is actually written in is a SEPARATE row this one asserts nothing about: stl-export-unittype-is-sticky-session-state (this body creates an options object and never exports). The automatic enum sweep cannot see this family - its name ends in none of the ...Types/...States/...Modes suffixes the scrape matches - so it is pinned here or nowhere",
        "encoded_in": "_export.py STL_UNIT_MEMBERS + applied_pair (the pre-read this collision forces); mesh_export.py _apply_stl_units; design_export.py _configure_export_options; tests/unit/test_mesh_export.py + test_design_export.py collision fakes",
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        des = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        root = des.rootComponent
        U = adsk.fusion.DistanceUnits
        dump_enum("fusion.DistanceUnits", U)
        # An options object needs real geometry to be created against; 1 cm cube, never exported.
        sk = root.sketches.add(root.xYConstructionPlane)
        sk.sketchCurves.sketchLines.addTwoPointRectangle(
            adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(1.0, 1.0, 0.0))
        root.features.extrudeFeatures.addSimple(
            sk.profiles.item(0), adsk.core.ValueInput.createByReal(1.0),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        # createSTLExportOptions validates the DIRECTORY at creation (a bare filename raises
        # "3 : The selected folder does not exist"), so the path must sit in a real folder.
        import os, tempfile
        opts = des.exportManager.createSTLExportOptions(
            root, os.path.join(tempfile.gettempdir(), "unused_measure_units.stl"))
        factory = opts.unitType
        emit(U.MillimeterDistanceUnits == 0 and factory == U.MillimeterDistanceUnits
             and factory != U.InchDistanceUnits,
             "enum-distance-units-collides-with-factory: mm=" + str(U.MillimeterDistanceUnits)
             + " in=" + str(U.InchDistanceUnits)
             + " factory unitType=" + str(factory))
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "mesh-export-component-descends-into-children",
        "claim": ("An STL export whose geometry is a COMPONENT writes that component's own bodies, "
                  "its MESH bodies, AND the bodies of the occurrences below it, BRep tessellated "
                  "in. Three legs, each with a floor: the child component's own file is 12 (a box), "
                  "the root's own box is another 12, and a tetrahedron mesh cannot tessellate below "
                  "its 4 faces - so this rig's file is 12 + 12 + 4 = 28 and the gate is that lower "
                  "bound. The total is a property of the RIG, not of the API: swap the tetrahedron "
                  "for a 12-triangle box mesh and the same three legs make 36. The triangle count "
                  "is the binary STL's own header field - bytes 80..84, little-endian uint32 - not "
                  "a count the exporter reported"),
        "encoded_in": ("mesh_export.py's redirect head - a MESH target is exported through its "
                       "owning component, and the note says the file holds every body below it; "
                       "tests/unit/test_mesh_export.py "
                       "test_the_redirect_note_names_the_brep_bodies_and_the_children_the_file_"
                       "carries"),
        "body": """
    import os, struct, tempfile
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        des = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        # Direct design: meshBodies.add* needs a BaseFeature scope in a parametric one.
        des.designType = adsk.fusion.DesignTypes.DirectDesignType
        root = des.rootComponent
        make_box(des, "RootBox")                      # 12 triangles of the root's OWN body
        child_body, child_occ = make_placed_box(des, "Child")
        # A tetrahedron mesh on the root, the sibling rows' way of making a mesh body.
        root.meshBodies.addByTriangleMeshData(
            [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0],
            [0, 2, 1, 0, 1, 3, 1, 2, 3, 2, 0, 3], [], [])
        em = des.exportManager

        def tri_count(geom, leaf):
            # An earlier run's file at the same path would be counted as this one's, so it goes
            # first: the header read below is then THIS export's or nothing.
            path = os.path.join(tempfile.gettempdir(), leaf)
            if os.path.exists(path):
                os.remove(path)
            if not em.execute(em.createSTLExportOptions(geom, path)) or not os.path.exists(path):
                return None
            with open(path, "rb") as fh:
                head = fh.read(84)
            return struct.unpack("<I", head[80:84])[0] if len(head) == 84 else None

        n_child = tri_count(child_body.parentComponent, "measure_child_only.stl")
        n_root = tri_count(root, "measure_root_descend.stl")
        # +12+4, not +12: the child's leg alone plus the root box would pass at 24 while the MESH
        # body was silently dropped, and the mesh riding is half of what this row measures.
        emit(n_child == 12 and n_root is not None and n_root >= n_child + 12 + 4,
             "mesh-export-component-descends-into-children: child component alone="
             + repr(n_child) + " root=" + repr(n_root) + " vs floor "
             + repr(None if n_child is None else n_child + 16)
             + " (child " + repr(n_child) + " + root box 12 + tetrahedron mesh 4)"
             + "; child occurrence " + child_occ.name)
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "enum-mesh-refinement-collides-with-factory",
        "claim": "MeshRefinementSettings.MeshRefinementMedium == 1 AND a freshly created STLExportOptions reads meshRefinement == 1 - the MEDIUM member is the factory value, and medium is mesh_export's DEFAULT refinement, so a set-then-read-back cannot bite on the most-travelled request. MeshRefinementHigh == 0 is a FALSY member, so a `not member` guard rejects the highest density. This read DOES determine the written file, measured here on a CURVED body exported both ways: on STL as BYTES (untouched, explicit-MEDIUM and a second medium export are byte-identical; high and low each differ from medium and from each other), and on OBJ as the TESSELLATION - vertex and face line counts - because the OBJ text embeds its own output filename in an mtllib line, so byte-identity cannot hold across differently-named files (measured: the only bytes separating two medium exports are that line): untouched, medium and medium-again tessellate identically, high and low each differently - which is why refinement publishes no verification flag. (unitType is the opposite case and is measured by stl-export-unittype-is-sticky-session-state; this row reads no unitType.) The automatic enum sweep cannot see this family - its name ends in none of the suffixes the scrape matches - so it is pinned here or nowhere",
        "encoded_in": "mesh_export.py _REFINEMENTS + _apply_refinement (which drops the pair's 'changed' half on the strength of this); _export.py applied_pair's per-knob note; tests/unit/test_mesh_export.py _refine_member",
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        des = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        root = des.rootComponent
        R = adsk.fusion.MeshRefinementSettings
        dump_enum("fusion.MeshRefinementSettings", R)
        # A CURVED body: refinement drives a TESSELLATION, so a flat-faced box writes the same
        # triangles at every setting and could not tell the settings apart.
        sk = root.sketches.add(root.xYConstructionPlane)
        sk.sketchCurves.sketchCircles.addByCenterRadius(
            adsk.core.Point3D.create(0.0, 0.0, 0.0), 1.0)
        solid = root.features.extrudeFeatures.addSimple(
            sk.profiles.item(0), adsk.core.ValueInput.createByReal(1.0),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation).bodies.item(0)
        import os, tempfile
        em = des.exportManager
        opts = em.createSTLExportOptions(
            root, os.path.join(tempfile.gettempdir(), "unused_measure_refinement.stl"))
        factory = opts.meshRefinement

        # unitType is left alone here: assigning it hands every later STL export in the session a
        # different unit (stl-export-unittype-is-sticky-session-state measures that), and these
        # comparisons only need the legs to share whatever unit the session already carries.
        def written(fmt, refine, tag):
            p = os.path.join(tempfile.gettempdir(), "measure_refine_" + tag + "." + fmt)
            o = (em.createSTLExportOptions(solid, p) if fmt == "stl"
                 else em.createOBJExportOptions(solid, p))
            if refine is not None:
                o.meshRefinement = refine
            if not em.execute(o):
                return b""
            raw = open(p, "rb").read()
            os.remove(p)
            return raw

        def key(fmt, raw):
            # What one export is COMPARED as. STL: the bytes themselves (measured reproducible).
            # OBJ: the tessellation - vertex/face line counts - because the OBJ text embeds its own
            # output filename in an mtllib line, so two differently-named files can never be
            # byte-identical however identically they tessellate.
            if fmt == "stl":
                return raw
            return (raw.count(b"\\nv "), raw.count(b"\\nf "))

        determines = {}
        sizes = {}
        obj_counts = None
        for fmt in ("stl", "obj"):
            untouched = written(fmt, None, fmt + "_untouched")
            medium = written(fmt, R.MeshRefinementMedium, fmt + "_medium")
            again = written(fmt, R.MeshRefinementMedium, fmt + "_medium_again")
            high = written(fmt, R.MeshRefinementHigh, fmt + "_high")
            low = written(fmt, R.MeshRefinementLow, fmt + "_low")
            sizes[fmt] = (len(untouched), len(medium), len(again), len(high), len(low))
            ku, km, ka = key(fmt, untouched), key(fmt, medium), key(fmt, again)
            kh, kl = key(fmt, high), key(fmt, low)
            if fmt == "obj":
                obj_counts = (ku, km, ka, kh, kl)
            determines[fmt] = (min(sizes[fmt]) > 0 and ku == km and km == ka
                               and kh != km and kl != km and kh != kl)
        emit(R.MeshRefinementMedium == 1 and R.MeshRefinementHigh == 0
             and factory == R.MeshRefinementMedium
             and determines["stl"] and determines["obj"],
             "enum-mesh-refinement-collides-with-factory: medium="
             + str(R.MeshRefinementMedium) + " high=" + str(R.MeshRefinementHigh)
             + " low=" + str(R.MeshRefinementLow)
             + " factory meshRefinement=" + str(factory)
             + " read_determines_file stl=" + str(determines["stl"])
             + " obj=" + str(determines["obj"])
             + " bytes(untouched,medium,medium-again,high,low) stl=" + str(sizes["stl"])
             + " obj=" + str(sizes["obj"]))
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "fusion-archive-execute-bool-vs-landed-file",
        "claim": "createFusionArchiveExportOptions(path, <component>) is judged by the file on disk, not by ExportManager.execute(): the row PASSes on a landed non-empty .f3d whatever the bool is, and PRINTS the bool so each build's shape is on the record",
        "encoded_in": "design_export.py _landed (the disk decides; a false bool is disclosed as execute_returned_false); tests/unit/test_design_export.py TestFileExistenceGate",
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        des = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        root = des.rootComponent
        # A COMPONENT, not the whole design: the two legs answer differently, and the component
        # leg is the one design_export._landed is built for.
        comp = root.occurrences.addNewComponent(adsk.core.Matrix3D.create()).component
        sk = comp.sketches.add(comp.xYConstructionPlane)
        sk.sketchCurves.sketchLines.addTwoPointRectangle(
            adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(2.0, 2.0, 0.0))
        comp.features.extrudeFeatures.addSimple(
            sk.profiles.item(0), adsk.core.ValueInput.createByReal(1.0),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        import os, tempfile
        path = os.path.join(tempfile.gettempdir(), "measure_f3d_component.f3d")
        if os.path.isfile(path):
            os.remove(path)
        em = des.exportManager
        did = em.execute(em.createFusionArchiveExportOptions(path, comp))
        landed = os.path.isfile(path)
        size = os.path.getsize(path) if landed else 0
        if landed:
            os.remove(path)
        emit(landed and size > 0,
             "fusion-archive-execute-bool-vs-landed-file: execute()=" + str(bool(did))
             + " landed=" + str(landed) + " size_bytes=" + str(size))
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "enum-design-types",
        "claim": "DesignTypes ints: DirectDesignType=0, ParametricDesignType=1",
        "encoded_in": "tests/unit/test__design_common.py; _inputs.py current_design_type/ModeGuard",
        "body": """
    D = adsk.fusion.DesignTypes
    dump_enum("fusion.DesignTypes", D)
    emit(D.DirectDesignType == 0 and D.ParametricDesignType == 1,
         "enum-design-types: direct=" + str(D.DirectDesignType)
         + " parametric=" + str(D.ParametricDesignType))
""",
    },
    {
        "id": "enum-joint-types",
        "claim": "JointTypes ints: Rigid=0 Revolute=1 Slider=2 Cylindrical=3 PinSlot=4 Planar=5 Ball=6 (Inferred=7 also exists)",
        "encoded_in": "tests/unit/test_assembly_get.py joint-type labels",
        "body": """
    J = adsk.fusion.JointTypes
    dump_enum("fusion.JointTypes", J)
    emit(J.RigidJointType == 0 and J.RevoluteJointType == 1 and J.SliderJointType == 2
         and J.CylindricalJointType == 3 and J.PinSlotJointType == 4
         and J.PlanarJointType == 5 and J.BallJointType == 6,
         "enum-joint-types: rigid=" + str(J.RigidJointType) + " ... ball=" + str(J.BallJointType)
         + " inferred=" + str(J.InferredJointType))
""",
    },
    {
        "id": "enum-joint-motion-types",
        "claim": "JointMotionTypes (the per-DOF motion enum setMotionData wants, DISTINCT from JointTypes) ints: RevoluteJointRotateMotionType=10, SliderJointSlideMotionType=11, CylindricalJointRotateMotionType=3, CylindricalJointSlideMotionType=4 - and on a live MotionLink between two revolute joints, setMotionData ACCEPTS the JointMotionTypes DOF value and returns True. That the JointTypes value jointMotion.jointType returns (RevoluteJointType==1) is REJECTED instead is the paired motion-link-setmotiondata-rejects-jointtypes-value row, which provokes that raise in a script of its own",
        "encoded_in": "tests/unit/test_joint_motion_link.py; _joints.py motion_link_dof map; joint_motion_link.py",
        "body": """
    M = adsk.fusion.JointMotionTypes
    J = adsk.fusion.JointTypes
    dump_enum("fusion.JointMotionTypes", M)
    ints_ok = (M.RevoluteJointRotateMotionType == 10 and M.SliderJointSlideMotionType == 11
               and M.CylindricalJointRotateMotionType == 3
               and M.CylindricalJointSlideMotionType == 4 and J.RevoluteJointType == 1)
    # The accepted call runs on a rig in its OWN document: two revolute joints on a common base,
    # linked, then handed the DOF value. 'stage' names the step in the receipt, so a rig that could
    # not be built reads differently from a call that answered.
    stage = "document"
    accepted = None
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        d = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        root = d.rootComponent
        tr = adsk.core.Matrix3D.create()
        stage = "components"
        occs = []
        for nm in ("LinkBase", "LinkA", "LinkB"):
            occ = root.occurrences.addNewComponent(tr)
            c = occ.component
            c.name = nm
            sk = c.sketches.add(c.xYConstructionPlane)
            sk.sketchCurves.sketchCircles.addByCenterRadius(
                adsk.core.Point3D.create(0.0, 0.0, 0.0), 0.5)
            c.features.extrudeFeatures.addSimple(
                sk.profiles.item(0), adsk.core.ValueInput.createByReal(0.5),
                adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
            occs.append(occ)
        stage = "joints"
        joints = []
        for i in (1, 2):
            geo = adsk.fusion.JointGeometry.createByPoint(
                occs[i].component.originConstructionPoint.createForAssemblyContext(occs[i]))
            ji = root.asBuiltJoints.createInput(occs[0], occs[i], geo)
            ji.setAsRevoluteJointMotion(adsk.fusion.JointDirections.ZAxisJointDirection)
            joints.append(root.asBuiltJoints.add(ji))
        stage = "motion link"
        ml = root.motionLinks.add(root.motionLinks.createInput(joints[0], joints[1]))
        v = adsk.core.ValueInput.createByReal(1.0)
        stage = "setMotionData with the JointMotionTypes DOF"
        jt = joints[0].jointMotion.jointType
        accepted = ml.setMotionData(M.RevoluteJointRotateMotionType, v,
                                    M.RevoluteJointRotateMotionType, v, False)
        emit(ints_ok and jt == J.RevoluteJointType and accepted is True,
             "enum-joint-motion-types: revolute_rotate=" + str(M.RevoluteJointRotateMotionType)
             + " slider_slide=" + str(M.SliderJointSlideMotionType)
             + " cyl_rotate=" + str(M.CylindricalJointRotateMotionType)
             + " cyl_slide=" + str(M.CylindricalJointSlideMotionType)
             + "; jointMotion.jointType=" + str(jt) + " (JointTypes.Revolute="
             + str(J.RevoluteJointType) + "); the DOF value -> " + repr(accepted))
    except Exception as exc:
        emit(False, "enum-joint-motion-types: RAISED at stage '" + stage + "': "
             + type(exc).__name__ + ": " + str(exc)[:120])
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "motion-link-setmotiondata-rejects-jointtypes-value",
        "claim": ("On a live MotionLink between two revolute joints, MotionLink.setMotionData "
                  "REJECTS the JointTypes value jointMotion.jointType returns "
                  "(RevoluteJointType==1) where it wants a JointMotionTypes DOF: the call raises "
                  "and the message names BAD_JOINT_DOF. The raise is provoked, so it sits in its "
                  "OWN row - this row catches it and gates on the message, and no expect is "
                  "declared, so a script-level abort is an ERROR here rather than a verdict. That "
                  "the DOF value IS accepted is the paired enum-joint-motion-types row"),
        "encoded_in": "tests/unit/test_joint_motion_link.py; _joints.py motion_link_dof map; joint_motion_link.py",
        "body": """
    M = adsk.fusion.JointMotionTypes
    J = adsk.fusion.JointTypes
    # The same rig as enum-joint-motion-types, in its OWN document: two revolute joints on a common
    # base, linked, then handed the JointTypes value instead of the DOF. 'stage' names the step in
    # the receipt, so a rig that could not be built reads differently from a call that answered.
    stage = "document"
    rejected = ""
    jt = None
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        d = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        root = d.rootComponent
        tr = adsk.core.Matrix3D.create()
        stage = "components"
        occs = []
        for nm in ("LinkBase", "LinkA", "LinkB"):
            occ = root.occurrences.addNewComponent(tr)
            c = occ.component
            c.name = nm
            sk = c.sketches.add(c.xYConstructionPlane)
            sk.sketchCurves.sketchCircles.addByCenterRadius(
                adsk.core.Point3D.create(0.0, 0.0, 0.0), 0.5)
            c.features.extrudeFeatures.addSimple(
                sk.profiles.item(0), adsk.core.ValueInput.createByReal(0.5),
                adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
            occs.append(occ)
        stage = "joints"
        joints = []
        for i in (1, 2):
            geo = adsk.fusion.JointGeometry.createByPoint(
                occs[i].component.originConstructionPoint.createForAssemblyContext(occs[i]))
            ji = root.asBuiltJoints.createInput(occs[0], occs[i], geo)
            ji.setAsRevoluteJointMotion(adsk.fusion.JointDirections.ZAxisJointDirection)
            joints.append(root.asBuiltJoints.add(ji))
        stage = "motion link"
        ml = root.motionLinks.add(root.motionLinks.createInput(joints[0], joints[1]))
        v = adsk.core.ValueInput.createByReal(1.0)
        stage = "setMotionData with the JointTypes value"
        jt = joints[0].jointMotion.jointType
        try:
            r = ml.setMotionData(jt, v, jt, v, False)
            rejected = "returned " + repr(r)
        except Exception as exc:
            rejected = type(exc).__name__ + ": " + (str(exc).strip().splitlines() or [""])[0][:70]
        emit(jt == J.RevoluteJointType and "BAD_JOINT_DOF" in rejected,
             "motion-link-setmotiondata-rejects-jointtypes-value: jointMotion.jointType="
             + str(jt) + " (JointTypes.Revolute=" + str(J.RevoluteJointType)
             + ", JointMotionTypes.RevoluteRotate="
             + str(M.RevoluteJointRotateMotionType) + ") -> " + rejected)
    except Exception as exc:
        emit(False, "motion-link-setmotiondata-rejects-jointtypes-value: RAISED at stage '"
             + stage + "': " + type(exc).__name__ + ": " + str(exc)[:120])
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "enum-joint-directions",
        "claim": "JointDirections ints: XAxis=0, YAxis=1, ZAxis=2, Custom=3",
        "encoded_in": "tests/unit/test_joint_edit.py; _joints.py JointDirections mapping",
        "body": """
    D = adsk.fusion.JointDirections
    dump_enum("fusion.JointDirections", D)
    emit(D.XAxisJointDirection == 0 and D.YAxisJointDirection == 1
         and D.ZAxisJointDirection == 2 and D.CustomJointDirection == 3,
         "enum-joint-directions: x=" + str(D.XAxisJointDirection)
         + " y=" + str(D.YAxisJointDirection) + " z=" + str(D.ZAxisJointDirection)
         + " custom=" + str(D.CustomJointDirection))
""",
    },
    {
        "id": "enum-feature-health-states",
        "claim": "FeatureHealthStates ints: Healthy=0, Warning=1, Error=2, Suppressed=3 (RolledBack=4, Unknown=5 exist and are ignored by the rollups)",
        "encoded_in": "tests/unit/test_assembly_get.py; _common.timeline_health; _assembly_detail.py health thresholds",
        "body": """
    H = adsk.fusion.FeatureHealthStates
    dump_enum("fusion.FeatureHealthStates", H)
    emit(H.HealthyFeatureHealthState == 0 and H.WarningFeatureHealthState == 1
         and H.ErrorFeatureHealthState == 2 and H.SuppressedFeatureHealthState == 3,
         "enum-feature-health-states: healthy=" + str(H.HealthyFeatureHealthState)
         + " warning=" + str(H.WarningFeatureHealthState)
         + " error=" + str(H.ErrorFeatureHealthState)
         + " suppressed=" + str(H.SuppressedFeatureHealthState))
""",
    },
    {
        "id": "enum-upload-states",
        "claim": "UploadStates ints: UploadProcessing=0, UploadFinished=1, UploadFailed=2",
        "encoded_in": "tests/unit/test_data_get_upload_status.py; data_get_upload_status.py uploadState read",
        "body": """
    U = adsk.core.UploadStates
    dump_enum("core.UploadStates", U)
    emit(U.UploadProcessing == 0 and U.UploadFinished == 1 and U.UploadFailed == 2,
         "enum-upload-states: processing=" + str(U.UploadProcessing)
         + " finished=" + str(U.UploadFinished) + " failed=" + str(U.UploadFailed))
""",
    },
    {
        "id": "enum-sweep",
        "claim": "Every adsk enum family the tools reference resolves to a live class and dumps its integer members - the catch-all that measures all families into live_api_facts.ENUMS, not just the value-pinned few. A resolved name carrying NO int member is a factory-object class (...Options.create()), recorded in live_api_facts.NOT_ENUMS; only a name that does not resolve at all FAILs the row (a stale/renamed reference in a tool)",
        "encoded_in": "commands/mcpServer/tools/*.py enum references; tests/conftest.py seeds ENUMS onto the mocks; tests/lints/test_enum_families_measured.py accepts a family in either table",
        "body_fn": _all_enums_body,
    },
    {
        "id": "enum-joint-keypoint-types",
        "claim": "JointKeyPointTypes ints: Start=0, Middle=1, End=2, Center=3",
        "encoded_in": "tests/unit/test_joint_at_geometry.py sentinel installer; _joints.py keypoint factory",
        "body": """
    K = adsk.fusion.JointKeyPointTypes
    dump_enum("fusion.JointKeyPointTypes", K)
    emit(K.StartKeyPoint == 0 and K.MiddleKeyPoint == 1 and K.EndKeyPoint == 2
         and K.CenterKeyPoint == 3,
         "enum-joint-keypoint-types: start=" + str(K.StartKeyPoint)
         + " middle=" + str(K.MiddleKeyPoint) + " end=" + str(K.EndKeyPoint)
         + " center=" + str(K.CenterKeyPoint))
""",
    },
    {
        "id": "enum-surface-types",
        "claim": "SurfaceTypes ints: Plane=0, Cylinder=1, Cone=2, Sphere=3, Torus=4 (Nurbs=7 also exists)",
        "encoded_in": "tests/unit/test_joint_at_geometry.py sentinel installer; _joints.py surface branch",
        "body": """
    S = adsk.core.SurfaceTypes
    dump_enum("core.SurfaceTypes", S)
    emit(S.PlaneSurfaceType == 0 and S.CylinderSurfaceType == 1 and S.ConeSurfaceType == 2
         and S.SphereSurfaceType == 3 and S.TorusSurfaceType == 4,
         "enum-surface-types: plane=" + str(S.PlaneSurfaceType)
         + " cylinder=" + str(S.CylinderSurfaceType) + " cone=" + str(S.ConeSurfaceType)
         + " sphere=" + str(S.SphereSurfaceType) + " torus=" + str(S.TorusSurfaceType))
""",
    },
    {
        "id": "enum-curve3d-types",
        "claim": "Curve3DTypes ints: Line=0, Arc=1, Circle=2 (Ellipse=3.. Polyline=7 also exist)",
        "encoded_in": "tests/unit/test_joint_at_geometry.py sentinel installer; _inputs.py axis curveType checks",
        "body": """
    C = adsk.core.Curve3DTypes
    dump_enum("core.Curve3DTypes", C)
    emit(C.Line3DCurveType == 0 and C.Arc3DCurveType == 1 and C.Circle3DCurveType == 2,
         "enum-curve3d-types: line=" + str(C.Line3DCurveType) + " arc=" + str(C.Arc3DCurveType)
         + " circle=" + str(C.Circle3DCurveType))
""",
    },
    {
        "id": "camera-returns-copy",
        "claim": "Viewport.camera returns a COPY - mutating it moves nothing until viewport.camera is reassigned",
        "encoded_in": "tests/fakes/geometry.py's Viewport/Camera fakes (they model a shared mutable object, the opposite, so only this row checks the real semantics)",
        "facts_on_pass": {"behavior.viewport_camera_returns_copy": True},
        "body": """
    vp = app.activeViewport
    cam = vp.camera
    e0 = cam.eye
    start_x = e0.x
    cam.eye = adsk.core.Point3D.create(start_x + 5.0, e0.y, e0.z)
    mid_x = vp.camera.eye.x
    unchanged = abs(mid_x - start_x) < 1e-9
    cam.isSmoothTransition = False
    vp.camera = cam
    applied = abs(vp.camera.eye.x - (start_x + 5.0)) < 1e-9
    emit(unchanged and applied,
         "camera-returns-copy: x after mutate=" + str(mid_x) + " (start " + str(start_x)
         + "), applied after reassign=" + str(applied))
""",
    },
    {
        "id": "basefeature-edit-scope",
        "claim": "An open base-feature edit scope is INVISIBLE: baseFeatures.count reads 0 and Design.timeline raises while open; finishEdit makes it appear (count 1)",
        "encoded_in": "tests/unit/test_model_base_feature.py; model_base_feature.py _OPEN_BASE_FEATURES comment",
        "facts_on_pass": {"behavior.open_base_feature_hidden": True},
        "body": """
    root = des.rootComponent
    n0 = root.features.baseFeatures.count
    bf = root.features.baseFeatures.add()
    started = bf.startEdit()
    try:
        open_count = root.features.baseFeatures.count
        tl_raises = False
        try:
            n = des.timeline.count
        except Exception:
            tl_raises = True
    finally:
        bf.finishEdit()
    closed_count = root.features.baseFeatures.count
    emit(bool(started) and open_count == n0 and tl_raises and closed_count == n0 + 1,
         "basefeature-edit-scope: started=" + str(started) + " open_count=" + str(open_count)
         + " (before add: " + str(n0) + ") timeline_raises=" + str(tl_raises)
         + " closed_count=" + str(closed_count))
""",
    },
    {
        "id": "export-arg-orders",
        "claim": "ExportManager arg orders differ by format: createSTLExportOptions(geometry, path) vs createSTEPExportOptions(path) - both land a file on execute()",
        "encoded_in": "tests/unit/test_design_export.py; design_export.py/_export.py",
        "need_box": True,
        "body": """
    import os
    em = des.exportManager
    base = os.path.join(os.getenv("TEMP") or "", "fe_measure_api")
    stl_path = base + ".stl"
    step_path = base + ".step"
    for p in (stl_path, step_path):
        if os.path.exists(p):
            os.remove(p)
    ok_stl = bool(em.execute(em.createSTLExportOptions(body, stl_path))) and os.path.exists(stl_path)
    ok_step = bool(em.execute(em.createSTEPExportOptions(step_path))) and os.path.exists(step_path)
    detail = "export-arg-orders: stl_landed=" + str(ok_stl) + " step_landed=" + str(ok_step)
    for p in (stl_path, step_path):
        if os.path.exists(p):
            os.remove(p)
    emit(ok_stl and ok_step, detail)
""",
    },
    {
        "id": "shape-dump-design-world",
        "claim": "Each of the 21 design-side adsk types this row DUMPS exposes a non-empty live public attribute set (dir() membership) - the set the fake-shape lint sweeps the shared fakes against. A shared fake whose live type is NOT dumped here is outside that sweep: the lint's own unmapped list carries those, and this row measures nothing about them",
        "encoded_in": ("tests/fakes/design.py (BRepBody/BRepFace/BRepEdge/MakeComp/MakeDesign) and "
                       "tests/fakes/geometry.py (FakeVector3D/FakePoint/...)"),
        "need_box": True,
        "body": """
    root = des.rootComponent
    counts = []
    counts.append(dump_shape("Design", des))
    counts.append(dump_shape("Component", root))
    counts.append(dump_shape("BRepBodies", root.bRepBodies))
    counts.append(dump_shape("BRepBody", body))
    face = body.faces.item(0)
    counts.append(dump_shape("BRepFace", face))
    counts.append(dump_shape("Plane", face.geometry))
    edge = body.edges.item(0)
    counts.append(dump_shape("BRepEdge", edge))
    counts.append(dump_shape("Line3D", edge.geometry))
    sk = root.sketches.item(0)
    counts.append(dump_shape("Sketch", sk))
    counts.append(dump_shape("Profile", sk.profiles.item(0)))
    occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    counts.append(dump_shape("Occurrence", occ))
    counts.append(dump_shape("Vector3D", adsk.core.Vector3D.create(1.0, 0.0, 0.0)))
    counts.append(dump_shape("Point3D", adsk.core.Point3D.create(0.0, 0.0, 0.0)))
    counts.append(dump_shape("Matrix3D", adsk.core.Matrix3D.create()))
    counts.append(dump_shape("BoundingBox3D", body.boundingBox))
    counts.append(dump_shape("ObjectCollection", adsk.core.ObjectCollection.create()))
    vp = app.activeViewport
    counts.append(dump_shape("Viewport", vp))
    counts.append(dump_shape("Camera", vp.camera))
    sk2 = root.sketches.add(root.xYConstructionPlane)
    sk2.sketchCurves.sketchCircles.addByCenterRadius(
        adsk.core.Point3D.create(6.0, 0.0, 0.0), 0.5)
    cyl = root.features.extrudeFeatures.addSimple(
        sk2.profiles.item(0), adsk.core.ValueInput.createByReal(1.0),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation).bodies.item(0)
    cyl_face = None
    for i in range(cyl.faces.count):
        if type(cyl.faces.item(i).geometry).__name__ == "Cylinder":
            cyl_face = cyl.faces.item(i)
    counts.append(dump_shape("Cylinder", cyl_face.geometry))
    circ_edge = None
    for i in range(cyl.edges.count):
        if type(cyl.edges.item(i).geometry).__name__ == "Circle3D":
            circ_edge = cyl.edges.item(i)
    counts.append(dump_shape("Circle3D", circ_edge.geometry))
    counts.append(dump_shape("Cone", adsk.core.Cone.create(
        adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Vector3D.create(0.0, 0.0, 1.0),
        0.5, 0.3)))
    emit(len(counts) == 21 and all(c > 0 for c in counts),
         "shape-dump-design-world: " + str(len(counts)) + " types, min attrs " + str(min(counts)))
""",
    },
    {
        "id": "shape-dump-document-world",
        "claim": "The session objects around a scratch document dump non-empty live attribute sets: Application.get() is an Application, its documents a Documents, the added document a FusionDocument (NOT a Document - that is the base class, and Document.cast returns the same FusionDocument), its products a Products, app.data a Data, app.userInterface a UserInterface, that UI's activeSelections a Selections holding a Selection once a construction plane is added to it, ValueInput.createByReal a ValueInput, and the design's exportManager an ExportManager. Document and DocumentReference are dumped from the CLASS object because no construction in this row returns either: dir() of a class is dir() of its instance minus SWIG's 'this', re-measured here on Documents, which the row holds both of",
        "encoded_in": ("tests/fakes/data_docs.py - the shared fakes for these session types; "
                       "tests/fakes/geometry.py FakeValueInput"),
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        d = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        ui = app.userInterface
        ui.activeSelections.clear()
        ui.activeSelections.add(d.rootComponent.xYConstructionPlane)
        live = [("Application", app), ("Documents", app.documents), ("FusionDocument", tmp),
                ("Products", tmp.products), ("Data", app.data), ("UserInterface", ui),
                ("Selections", ui.activeSelections), ("Selection", ui.activeSelections.item(0)),
                ("ValueInput", adsk.core.ValueInput.createByReal(1.0)),
                ("ExportManager", d.exportManager)]
        # The SHAPES key is the LABEL, and the fake-shape lint maps a fake onto it by name, so the
        # type each construction actually answers is read rather than assumed.
        wrong = [lbl + "=" + type(o).__name__ for lbl, o in live if type(o).__name__ != lbl]
        counts = [dump_shape(lbl, o) for lbl, o in live]
        ui.activeSelections.clear()
        ctl = set(n for n in dir(adsk.core.Documents) if not n.startswith("_"))
        class_ok = ctl == set(n for n in dir(app.documents) if not n.startswith("_")) - set(["this"])
        counts.append(dump_shape("Document", adsk.core.Document))
        counts.append(dump_shape("DocumentReference", adsk.core.DocumentReference))
        emit(len(counts) == 12 and all(c > 0 for c in counts) and not wrong and class_ok,
             "shape-dump-document-world: " + str(len(counts)) + " types, min attrs "
             + str(min(counts)) + ", a design document answers FusionDocument and Document/"
             "DocumentReference come off the class (class dir == instance dir minus 'this': "
             + str(class_ok) + "), mislabelled " + (", ".join(wrong) or "none"))
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "shape-dump-timeline-world",
        "claim": "In a scratch document a sketch carrying a rectangle and a point yields Sketches, SketchCurves, SketchPoints and SketchPoint, and its Profiles; an extrude fills Features and the design's Timeline with TimelineObject entries; userParameters.add yields UserParameters and UserParameter; baseFeatures.add followed by startEdit/finishEdit yields BaseFeatures and BaseFeature. Feature and Parameter are dumped from the CLASS: the extrude answers ExtrudeFeature and the parameter answers UserParameter, so no construction here returns the base type, and the class dump is checked against the class-dir-equals-instance-dir-minus-'this' reading taken on Sketches. The row also reads that neither adsk.fusion nor adsk.core carries a type NAMED Parameters - a design's parameter collections are UserParameters and ParameterList",
        "encoded_in": ("tests/fakes/design.py - the timeline, feature and parameter fakes; "
                       "tests/fakes/sketch.py - the sketch types"),
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        d = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        root = d.rootComponent
        sk = root.sketches.add(root.xYConstructionPlane)
        sk.sketchCurves.sketchLines.addTwoPointRectangle(
            adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(2.0, 1.0, 0.0))
        sk.sketchPoints.add(adsk.core.Point3D.create(0.5, 0.5, 0.0))
        ext = root.features.extrudeFeatures.addSimple(
            sk.profiles.item(0), adsk.core.ValueInput.createByReal(1.0),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        up = d.userParameters.add("shapeDumpParam", adsk.core.ValueInput.createByReal(1.0), "cm", "")
        bf = root.features.baseFeatures.add()
        bf.startEdit()
        bf.finishEdit()
        live = [("Sketches", root.sketches), ("SketchCurves", sk.sketchCurves),
                ("SketchPoints", sk.sketchPoints), ("SketchPoint", sk.sketchPoints.item(0)),
                ("Profiles", sk.profiles), ("Features", root.features),
                ("Timeline", d.timeline), ("TimelineObject", d.timeline.item(0)),
                ("UserParameters", d.userParameters), ("UserParameter", up),
                ("BaseFeatures", root.features.baseFeatures), ("BaseFeature", bf)]
        wrong = [lbl + "=" + type(o).__name__ for lbl, o in live if type(o).__name__ != lbl]
        counts = [dump_shape(lbl, o) for lbl, o in live]
        ctl = set(n for n in dir(adsk.fusion.Sketches) if not n.startswith("_"))
        class_ok = ctl == set(n for n in dir(root.sketches) if not n.startswith("_")) - set(["this"])
        no_parameters_type = not (hasattr(adsk.fusion, "Parameters")
                                  or hasattr(adsk.core, "Parameters"))
        counts.append(dump_shape("Feature", adsk.fusion.Feature))
        counts.append(dump_shape("Parameter", adsk.fusion.Parameter))
        emit(len(counts) == 14 and all(c > 0 for c in counts) and not wrong and class_ok
             and no_parameters_type and type(ext).__name__ == "ExtrudeFeature",
             "shape-dump-timeline-world: " + str(len(counts)) + " types, min attrs "
             + str(min(counts)) + ", the extrude answers " + type(ext).__name__
             + " so Feature/Parameter come off the class (class dir == instance dir minus 'this': "
             + str(class_ok) + "), timeline holds " + str(d.timeline.count)
             + " entries, no type named Parameters " + str(no_parameters_type)
             + ", mislabelled " + (", ".join(wrong) or "none"))
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "shape-dump-assembly-world",
        "claim": "Six new components in a scratch document yield an Occurrences; three joints from the first to the next three - one revolute, one slider, one cylindrical, each on the Z axis through the components' origin construction points - yield Joints, Joint and the three motion types RevoluteJointMotion, SliderJointMotion and CylindricalJointMotion off joint.jointMotion; a motion link over the revolute and the slider yields MotionLinks and MotionLink; and a rigid group over the two un-jointed occurrences yields RigidGroups and RigidGroup. Every one of the ten dumps is non-empty and answers the type its label names",
        "encoded_in": ("tests/fakes/joints.py - the joint, motion, link and rigid-group fakes; "
                       "tests/fakes/design.py FakeOccurrence"),
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        d = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        root = d.rootComponent
        occs = [root.occurrences.addNewComponent(adsk.core.Matrix3D.create()) for _ in range(6)]

        def joint(one, two, setter):
            geo = [adsk.fusion.JointGeometry.createByPoint(
                o.component.originConstructionPoint.createForAssemblyContext(o)) for o in (one, two)]
            jin = root.joints.createInput(geo[0], geo[1])
            setter(jin)
            return root.joints.add(jin)

        zax = adsk.fusion.JointDirections.ZAxisJointDirection
        rev = joint(occs[0], occs[1], lambda i: i.setAsRevoluteJointMotion(zax))
        sli = joint(occs[0], occs[2], lambda i: i.setAsSliderJointMotion(zax))
        cyl = joint(occs[0], occs[3], lambda i: i.setAsCylindricalJointMotion(zax))
        link = root.motionLinks.add(root.motionLinks.createInput(rev, sli))
        group = adsk.core.ObjectCollection.create()
        group.add(occs[4])
        group.add(occs[5])
        rigid = root.rigidGroups.add(group, False)
        live = [("Occurrences", root.occurrences), ("Joints", root.joints), ("Joint", rev),
                ("RevoluteJointMotion", rev.jointMotion), ("SliderJointMotion", sli.jointMotion),
                ("CylindricalJointMotion", cyl.jointMotion), ("MotionLinks", root.motionLinks),
                ("MotionLink", link), ("RigidGroups", root.rigidGroups), ("RigidGroup", rigid)]
        wrong = [lbl + "=" + type(o).__name__ for lbl, o in live if type(o).__name__ != lbl]
        counts = [dump_shape(lbl, o) for lbl, o in live]
        emit(len(counts) == 10 and all(c > 0 for c in counts) and not wrong,
             "shape-dump-assembly-world: " + str(len(counts)) + " types, min attrs "
             + str(min(counts)) + ", " + str(root.occurrences.count) + " occurrences carrying "
             + str(root.joints.count) + " joints, " + str(root.motionLinks.count)
             + " motion link, " + str(root.rigidGroups.count) + " rigid group, mislabelled "
             + (", ".join(wrong) or "none"))
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "shape-dump-assembly-world-2",
        "claim": "A scratch assembly of six single-box components - the first two overlapping by half a box - yields the assembly types the joint/assembly test doubles stand for: a planar-face JointGeometry through JointOrigins.createInput/add gives JointOriginInput, JointOrigin and JointOrigins; asBuiltJoints.createInput(occ, occ, None)/add gives AsBuiltJointInput, AsBuiltJoint, AsBuiltJoints and a RigidJointMotion off the joint; joints.createInput gives JointInput; contactSets.add over the overlapping pair gives ContactSet and ContactSets; createInterferenceInput/analyzeInterference over that pair gives InterferenceInput, InterferenceResults and InterferenceResult; assemblyConstraints.createInput, one geometricRelationships.add between two planar faces, and add gives AssemblyConstraintInput, AssemblyConstraint and AssemblyConstraints; a revolute joint driven to rotationValue 0.5 sets snapshots.hasPendingSnapshot True and snapshots.add then gives Snapshot and Snapshots; the extrude's extentDefinition.distance is a ModelParameter; timelineGroups.add(0, 1) gives TimelineGroup and TimelineGroups. Every one of the 22 dumps is non-empty and answers the type its label names. There is no InterferenceBody type in adsk.fusion or adsk.core - the row reads that absence, and reads that InterferenceResult.interferenceBody answers BRepBody",
        "encoded_in": "tests/live_api_facts.py SHAPES - the measured surface a shared fake for any of these assembly/joint types is swept against by test_fake_shapes_exist.py; today the types are carried by per-file fakes in test_assembly_get.py, test_assembly_inspect_interference.py, test_assembly_capture_position.py, test_joint_create_as_built.py and test_design_edit_timeline.py",
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        d = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        root = d.rootComponent

        def placed(dx, name):
            m = adsk.core.Matrix3D.create()
            m.translation = adsk.core.Vector3D.create(dx, 0.0, 0.0)
            occ = root.occurrences.addNewComponent(m)
            c = occ.component
            c.name = name
            sk = c.sketches.add(c.xYConstructionPlane)
            sk.sketchCurves.sketchLines.addTwoPointRectangle(
                adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(1.0, 1.0, 0.0))
            ext = c.features.extrudeFeatures.addSimple(
                sk.profiles.item(0), adsk.core.ValueInput.createByReal(1.0),
                adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
            return occ, c, ext

        def planar_face(body):
            for i in range(body.faces.count):
                f = body.faces.item(i)
                if type(f.geometry).__name__ == "Plane":
                    return f
            return None

        def origin_geometry(occ):
            return adsk.fusion.JointGeometry.createByPoint(
                occ.component.originConstructionPoint.createForAssemblyContext(occ))

        one, comp_one, ext_one = placed(0.0, "ShapeA")
        two = placed(0.5, "ShapeB")[0]
        three = placed(4.0, "ShapeC")[0]
        four = placed(6.0, "ShapeD")[0]
        five = placed(8.0, "ShapeE")[0]
        six = placed(10.0, "ShapeF")[0]

        jgeo = adsk.fusion.JointGeometry.createByPlanarFace(
            planar_face(comp_one.bRepBodies.item(0)), None,
            adsk.fusion.JointKeyPointTypes.CenterKeyPoint)
        jo_input = comp_one.jointOrigins.createInput(jgeo)
        jo = comp_one.jointOrigins.add(jo_input)

        # An as-built joint with NO geometry is the rigid one, so its jointMotion is the
        # RigidJointMotion dumped below.
        ab_input = root.asBuiltJoints.createInput(one, two, None)
        ab = root.asBuiltJoints.add(ab_input)
        j_input = root.joints.createInput(origin_geometry(one), origin_geometry(two))

        cset = d.contactSets.add([one, two])
        coll = adsk.core.ObjectCollection.create()
        coll.add(one)
        coll.add(two)
        i_input = d.createInterferenceInput(coll)
        i_results = d.analyzeInterference(i_input)
        i_result = i_results.item(0)
        volume_body = type(i_result.interferenceBody).__name__
        no_body_type = not (hasattr(adsk.fusion, "InterferenceBody")
                            or hasattr(adsk.core, "InterferenceBody"))

        c_input = root.assemblyConstraints.createInput()
        c_input.geometricRelationships.add(
            planar_face(three.bRepBodies.item(0)), planar_face(four.bRepBodies.item(0)),
            False, adsk.core.ValueInput.createByReal(0.0))
        constraint = root.assemblyConstraints.add(c_input)

        # snapshots.add() raises without a pending position, so the drive comes first and the
        # flag is read (not assumed) before the capture.
        rev_input = root.joints.createInput(origin_geometry(five), origin_geometry(six))
        rev_input.setAsRevoluteJointMotion(adsk.fusion.JointDirections.ZAxisJointDirection)
        rev = root.joints.add(rev_input)
        rev.jointMotion.rotationValue = 0.5
        pending = d.snapshots.hasPendingSnapshot
        snap = d.snapshots.add()

        group = d.timeline.timelineGroups.add(0, 1)
        live = [("Snapshots", d.snapshots), ("Snapshot", snap),
                ("AsBuiltJoints", root.asBuiltJoints), ("AsBuiltJoint", ab),
                ("AsBuiltJointInput", ab_input), ("JointInput", j_input),
                ("JointOrigins", comp_one.jointOrigins), ("JointOrigin", jo),
                ("JointOriginInput", jo_input), ("JointGeometry", jgeo),
                ("AssemblyConstraints", root.assemblyConstraints),
                ("AssemblyConstraint", constraint), ("AssemblyConstraintInput", c_input),
                ("ContactSets", d.contactSets), ("ContactSet", cset),
                ("InterferenceInput", i_input), ("InterferenceResults", i_results),
                ("InterferenceResult", i_result), ("RigidJointMotion", ab.jointMotion),
                ("ModelParameter", ext_one.extentDefinition.distance),
                ("TimelineGroups", d.timeline.timelineGroups), ("TimelineGroup", group)]
        wrong = [lbl + "=" + type(o).__name__ for lbl, o in live if type(o).__name__ != lbl]
        counts = [dump_shape(lbl, o) for lbl, o in live]
        emit(len(counts) == 22 and all(c > 0 for c in counts) and not wrong
             and pending is True and volume_body == "BRepBody" and no_body_type,
             "shape-dump-assembly-world-2: " + str(len(counts)) + " types, min attrs "
             + str(min(counts)) + ", " + str(root.occurrences.count) + " components carrying "
             + str(root.asBuiltJoints.count) + " as-built joint, "
             + str(comp_one.jointOrigins.count) + " joint origin, "
             + str(root.assemblyConstraints.count) + " assembly constraint, "
             + str(d.contactSets.count) + " contact set, " + str(i_results.count)
             + " interference pair whose volume answers " + volume_body
             + " (adsk carries no InterferenceBody type: " + str(no_body_type) + "), "
             + str(d.snapshots.count) + " captured position after the revolute drive set "
             "has_pending " + str(pending) + ", " + str(d.timeline.timelineGroups.count)
             + " timeline group, mislabelled " + (", ".join(wrong) or "none"))
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "shape-dump-data-world",
        "claim": "The cloud data model dumps three types from a READ-ONLY, bounded look: the configured project (named by the tests/live cloud config) found by name in app.data.dataProjects is a DataProject (app.data.activeProject RAISES '2 : InternalValidationError : group' on 2705.1.11 in every context tried - a fresh session, an unsaved scratch document, and a cloud document open and active - so no row and no tool can lean on it), its rootFolder a DataFolder, and the DataFile dumped is the root folder's first file - or, when the root holds no file, the first file of the root's FIRST subfolder. At most those two folders are opened and only item(0) of each is touched: no recursive walk, which has been measured killing the add-in. When neither folder holds a file, DataFile is dumped from the class object under the same class-dir-equals-instance-dir-minus-'this' reading, taken here on DataFolder; the detail names which of the three sources supplied it",
        "encoded_in": "tests/fakes/data_docs.py - the shared fakes for these cloud types",
        "body": """
    # Data.activeProject raises on 2705.1.11 in every context, so the project is found by name.
    projects = app.data.dataProjects
    proj = None
    for i in range(projects.count):
        if projects.item(i).name == CLOUD_PROJECT:
            proj = projects.item(i)
            break
    if proj is None:
        emit(False, "shape-dump-data-world: no project named '" + CLOUD_PROJECT
             + "' (named by the tests/live cloud config) in the active hub")
        return
    folder = proj.rootFolder
    dfile = None
    source = "no file in the root or its first subfolder - the class object"
    if folder.dataFiles.count:
        dfile = folder.dataFiles.item(0)
        source = "the root folder's first file"
    elif folder.dataFolders.count and folder.dataFolders.item(0).dataFiles.count:
        dfile = folder.dataFolders.item(0).dataFiles.item(0)
        source = "the first subfolder's first file"
    live = [("DataProject", proj), ("DataFolder", folder)]
    if dfile is not None:
        live.append(("DataFile", dfile))
    wrong = [lbl + "=" + type(o).__name__ for lbl, o in live if type(o).__name__ != lbl]
    counts = [dump_shape(lbl, o) for lbl, o in live]
    if dfile is None:
        counts.append(dump_shape("DataFile", adsk.core.DataFile))
    ctl = set(n for n in dir(adsk.core.DataFolder) if not n.startswith("_"))
    class_ok = ctl == set(n for n in dir(folder) if not n.startswith("_")) - set(["this"])
    emit(len(counts) == 3 and all(c > 0 for c in counts) and not wrong and class_ok,
         "shape-dump-data-world: " + str(len(counts)) + " types, min attrs " + str(min(counts))
         + ", DataFile from " + source + " (project '" + proj.name + "' root holds "
         + str(folder.dataFiles.count) + " files and " + str(folder.dataFolders.count)
         + " folders; class dir == instance dir minus 'this': " + str(class_ok)
         + "), mislabelled " + (", ".join(wrong) or "none"))
""",
    },
    {
        "id": "shape-dump-data-cloud-collections",
        "claim": "The cloud COLLECTION types dump from the same READ-ONLY, bounded look as shape-dump-data-world: DataFiles and DataFolders off the root folder of the configured project (named by the tests/live cloud config), found by name in app.data.dataProjects (activeProject raises on 2705.1.11), DataProjects and DataHubs off app.data - at most the root folder plus its FIRST subfolder are opened and only item(0) of each is touched, no recursive walk. All four carry asArray, and so does the parentReferences of the one DataFile reached (parentReferences answers a DataFiles). DataFileFuture is dumped from the class object under the class-dir-equals-instance-dir-minus-'this' reading, re-measured in this row on the live DataProjects, and carries both uploadState and dataFile. Data.activeHub carries a SETTER function, so 'no public setter' is not what stops a programmatic hub switch. MEASURED BY HAND and deliberately NOT re-measured by any row: assigning it LANDS - data_switch_hub reported switched:true both ways between the account's two hubs on 2705.1.4 - and the switch CLOSES every open document, which would destroy the sweep's own scratch",
        "encoded_in": ("tests/fakes/data_docs.py FakeData, FakeDataFolder, FakeDataFile and the _CloudArray "
                       "collection fakes (_CloudProjects among them); data_delete_file.py's "
                       "parentReferences.asArray() read"),
        "body": """
    data = app.data
    proj = None
    for i in range(data.dataProjects.count):
        if data.dataProjects.item(i).name == CLOUD_PROJECT:
            proj = data.dataProjects.item(i)
            break
    if proj is None:
        emit(False, "shape-dump-data-cloud-collections: no project named '" + CLOUD_PROJECT
             + "' (named by the tests/live cloud config)")
        return
    folder = proj.rootFolder
    dfile = None
    source = "no file in the root or its first subfolder"
    if folder.dataFiles.count:
        dfile = folder.dataFiles.item(0)
        source = "the root folder's first file"
    elif folder.dataFolders.count and folder.dataFolders.item(0).dataFiles.count:
        dfile = folder.dataFolders.item(0).dataFiles.item(0)
        source = "the first subfolder's first file"
    live = [("DataFiles", folder.dataFiles), ("DataFolders", folder.dataFolders),
            ("DataProjects", data.dataProjects), ("DataHubs", data.dataHubs)]
    wrong = [lbl + "=" + type(o).__name__ for lbl, o in live if type(o).__name__ != lbl]
    counts = [dump_shape(lbl, o) for lbl, o in live]
    counts.append(dump_shape("DataFileFuture", adsk.core.DataFileFuture))
    no_as_array = [lbl for lbl, o in live if not hasattr(o, "asArray")]
    # parentReferences is the read data_delete_file's destructive path fails CLOSED on, so the
    # collection it answers is asked for asArray HERE rather than assumed from the sibling four.
    # None means no DataFile was reachable to ask: the gate below wants True, so an unmeasurable
    # project FAILS the row instead of passing it vacuously.
    parents = hasattr(dfile.parentReferences, "asArray") if dfile is not None else None
    ctl = set(n for n in dir(adsk.core.DataProjects) if not n.startswith("_"))
    class_ok = ctl == set(n for n in dir(data.dataProjects) if not n.startswith("_")) - set(["this"])
    fut = set(n for n in dir(adsk.core.DataFileFuture) if not n.startswith("_"))
    hub = adsk.core.Data.__dict__.get("activeHub")
    hub_settable = getattr(hub, "fset", None) is not None
    emit(len(counts) == 5 and all(c > 0 for c in counts) and not wrong and not no_as_array
         and parents is True and class_ok and hub_settable
         and "uploadState" in fut and "dataFile" in fut,
         "shape-dump-data-cloud-collections: " + str(len(counts)) + " types, min attrs "
         + str(min(counts)) + " (project '" + proj.name + "' root holds "
         + str(folder.dataFiles.count) + " files and " + str(folder.dataFolders.count)
         + " folders; DataFile from " + source + "), no asArray: "
         + (", ".join(no_as_array) or "none") + ", parentReferences.asArray="
         + (repr(parents) if parents is not None else
            "UNMEASURED - no DataFile reachable in project '" + proj.name + "'")
         + ", class dir == instance dir minus 'this': " + str(class_ok)
         + ", DataFileFuture carries uploadState+dataFile: "
         + str("uploadState" in fut and "dataFile" in fut)
         + ", Data.activeHub setter present: " + str(hub_settable)
         + ", mislabelled " + (", ".join(wrong) or "none"))
""",
    },
    {
        "id": "shape-dump-drawing-world",
        "claim": "The row makes and removes its OWN source, so nothing it measures depends on what a project happens to hold: it adds a scratch design carrying one placed box, saves it into the configured cloud project (named by the tests/live cloud config) as MeasureDrawingSource, takes that document's DataFile as the createDrawingInput source, and in a finally closes the document and deletes the file. Right after saveAs the DataFile's id is the LOCAL cache path - the cloud urn: id lands asynchronously, about two seconds - so the row pumps doEvents under a 20 second clock bound until the urn: form answers and FAILS naming the timeout if it never does. Before saving, ONE listing of that folder's own dataFiles (never recursive) deletes any MeasureDrawingSource a previous run left behind; that listing LAGS its own deletes, so an entry it names can already be gone and the row reports the entries seen and the deletes that took rather than inferring a leftover from the difference. The same lag makes deleteMe RAISE InternalValidationError while a just-closed file is still settling, so the removal pumps doEvents and retries under a clock bound - measured taking two or three attempts. DrawingManager.get() answers a DrawingManager and createDrawingInput answers a CreateDrawingInput whose customSize hands out a CustomSheetSize already carrying a positive width and height and at least two zones each way, so 'a DEFAULT CustomSheetSize' is read rather than assumed. The deleting of the source is reported but does NOT gate the row: a False leaves the file for the next run's sweep and the detail names it. The eleven document-side types (DrawingDocument, Drawing, Sheets, Sheet, Views, View, DrawingSketches, DrawingSketch, Images, DrawingExportManager, DocumentSettings) come off their CLASS objects: adsk.core.DocumentTypes carries no drawing member at all, so documents.add cannot make one, and DrawingManager.createDrawing would mint a SECOND cloud file, which this row does not call. The class dump rests on the class-dir-equals-instance-dir-minus-'this' reading, re-measured here on CreateDrawingInput, which the row holds both of. The collection types are named Views/Images, NOT DrawingViews/DrawingImages, and the settings type is DocumentSettings - the labels are what the fake-shape lint maps a fake onto, so each live one is read back rather than assumed",
        "encoded_in": ("tests/fakes/drawing.py's drawing world - FakeDrawingDocument, FakeDrawing, "
                       "FakeSheets/FakeSheet, FakeViews/FakeView, FakeDrawingSketches/"
                       "FakeDrawingSketch, FakeImages, FakeDrawingExportManager, "
                       "FakeDocumentSettings, FakeDrawingManager, FakeCreateDrawingInput, "
                       "FakeCustomSheetSize; _drawing_common.active_drawing_document and "
                       "_drawing_common.sheet_facts read these types live"),
        "body": """
    import time as _clock
    SOURCE_NAME = "MeasureDrawingSource"
    projects = app.data.dataProjects
    project = None
    for i in range(projects.count):
        if projects.item(i).name == CLOUD_PROJECT:
            project = projects.item(i)
            break
    folder = None if project is None else project.rootFolder

    def source_file():
        \"\"\"The row's own saved source in the target folder, or None - ONE flat listing.\"\"\"
        # dataFiles hands back a FRESH snapshot per property access, so the count and the item
        # must come off ONE bound collection - indexing a later access with an earlier count
        # raises 'invalid argument index' while the folder is still settling after a save.
        files = folder.dataFiles
        for i in range(files.count):
            if files.item(i).name == SOURCE_NAME:
                return files.item(i)
        return None

    # ONE flat listing, every delete raise-safe. The listing LAGS its own deletes, so an entry it
    # names here can already be gone - the row reports both counts and infers nothing from a
    # delete that did not take.
    seen = 0
    swept = 0
    if folder is not None:
        listing = folder.dataFiles
        for i in range(listing.count):
            if listing.item(i).name != SOURCE_NAME:
                continue
            seen += 1
            try:
                swept += 1 if listing.item(i).deleteMe() else 0
            except Exception:
                pass
    dm = adsk.drawing.DrawingManager.get()
    di = None
    cs = None
    urn = None
    deleted = None
    settle = None
    tries = 0
    tmp = None if folder is None else app.documents.add(
        adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        if tmp is not None:
            src_design = adsk.fusion.Design.cast(
                tmp.products.itemByProductType("DesignProductType"))
            make_placed_box(src_design, "MeasureDrawingBox")
            started = _clock.time()
            tmp.saveAs(SOURCE_NAME, folder,
                       "scratch source the shape-dump-drawing-world row saves and deletes", "")
            # saveAs answers immediately with dataFile.id still the LOCAL cache path; the cloud
            # urn: id lands asynchronously, and both createDrawingInput and deleteMe want the
            # settled file, so the pump waits for that form rather than the first truthy id.
            deadline = started + 20.0
            while _clock.time() < deadline:
                df = tmp.dataFile
                got = df.id if df is not None else ""
                if got and got.startswith("urn:"):
                    urn = got
                    settle = round(_clock.time() - started, 2)
                    break
                adsk.doEvents()
                _clock.sleep(0.2)
            if urn is not None:
                di = dm.createDrawingInput(
                    tmp.dataFile, adsk.drawing.DrawingCreationModes.AutomaticDrawingCreationMode)
                cs = di.customSize
    finally:
        if tmp is not None:
            tmp.close(False)
            # deleteMe RAISES InternalValidationError while the just-closed file is still
            # settling, and the folder listing lags its own deletes - so the retry pumps
            # doEvents, treats a raise as one more not-yet, and stops on the first True.
            stop = _clock.time() + 25.0
            while _clock.time() < stop:
                leftover = source_file()
                if leftover is None:
                    break
                tries += 1
                try:
                    if leftover.deleteMe():
                        deleted = True
                        break
                except Exception:
                    pass
                deleted = False
                for _ in range(10):
                    adsk.doEvents()
                    _clock.sleep(0.2)
    live = [("DrawingManager", dm)]
    if di is not None:
        live.append(("CreateDrawingInput", di))
        live.append(("CustomSheetSize", cs))
    wrong = [lbl + "=" + type(o).__name__ for lbl, o in live if type(o).__name__ != lbl]
    counts = [dump_shape(lbl, o) for lbl, o in live]
    for name in ("DrawingDocument", "Drawing", "Sheets", "Sheet", "Views", "View",
                 "DrawingSketches", "DrawingSketch", "Images", "DrawingExportManager",
                 "DocumentSettings"):
        counts.append(dump_shape(name, getattr(adsk.drawing, name)))
    doc_types = [n for n in dir(adsk.core.DocumentTypes) if not n.startswith("_")]
    no_drawing_doc_type = not [n for n in doc_types if "Drawing" in n]
    aliases = ("DrawingViews", "DrawingView", "DrawingImages", "DrawingDimensions",
               "DrawingDocumentSettings")
    no_drawing_aliases = not any(hasattr(adsk.drawing, n) for n in aliases)
    # None means no CreateDrawingInput was reachable to take the reading on: the gate wants True,
    # so a source that never saved or never settled FAILS the row instead of passing it on the
    # eleven class dumps.
    class_ok = None
    if di is not None:
        ctl = set(n for n in dir(adsk.drawing.CreateDrawingInput) if not n.startswith("_"))
        class_ok = ctl == set(n for n in dir(di) if not n.startswith("_")) - set(["this"])
    size = None if cs is None else (cs.width, cs.height, cs.horizontalZones, cs.verticalZones)
    sized = size is not None and size[0] > 0 and size[1] > 0 and size[2] >= 2 and size[3] >= 2
    emit(len(counts) == 14 and all(c > 0 for c in counts) and not wrong and class_ok is True
         and no_drawing_doc_type and no_drawing_aliases and sized and urn is not None,
         "shape-dump-drawing-world: " + str(len(counts)) + " types, min attrs "
         + str(min(counts)) + ", source " + SOURCE_NAME + " saved into "
         + ("'" + project.name + "'" if project is not None
            else "NO project named '" + CLOUD_PROJECT + "' - nothing was saved")
         + " and its cloud urn: id "
         + ("settled in " + str(settle) + "s" if urn is not None
            else "NEVER settled inside the 20s bound, so no CreateDrawingInput was taken")
         + "; the source was "
         + ("removed after " + str(tries) + " delete attempt(s)" if deleted is True
            else ("scratch file left: " + SOURCE_NAME + " after " + str(tries) + " attempt(s)")
            if deleted is False else "not found to delete")
         + "; the opening sweep saw " + str(seen) + " listing entr(ies) under that name and "
         + str(swept) + " deleted"
         + "; DocumentTypes names no drawing document "
         "(it carries " + ",".join(doc_types) + ") so the eleven document-side types come off "
         "the class (class dir == instance dir minus 'this' on CreateDrawingInput: "
         + str(class_ok) + "); adsk.drawing carries none of " + ",".join(aliases) + ": "
         + str(no_drawing_aliases) + ", a fresh CreateDrawingInput hands out customSize "
         + str(size) + " as (width, height, horizontal zones, vertical zones), mislabelled "
         + (", ".join(wrong) or "none"))
""",
    },
    {
        "id": "shape-dump-appearance-world",
        "claim": "The appearance world dumps seven library types - MaterialLibraries off app.materialLibraries, the first MaterialLibrary carrying appearances, its Appearances, that library's first Appearance, the Appearance's appearanceProperties, the ColorProperty among them and the Color that property's value answers - and the row reads that Appearance.appearanceProperties answers a PROPERTIES collection: there is no type named AppearanceProperties in adsk.core or adsk.fusion. Beside them it reads the PLAIN answers a fresh scratch entity gives with no override applied, which is what a body/occurrence/face double has to start from: BRepBody.appearance is already a live Appearance (never None) and opacity reads 1.0, but visibleOpacity on the NATIVE body RAISES InternalValidationError while the same read through an assembly-context proxy answers 1.0; Occurrence.appearance reads None, its visibleOpacity 1.0, and Occurrence carries no opacity member at all; Component.opacity reads 1.0 and BRepFace.appearance is a live Appearance. The native visibleOpacity raise is CAUGHT and the row keeps reading, so a build that stops raising fails this row rather than passing it quietly",
        "encoded_in": ("tests/fakes/appearance.py's library world (FakeMaterialLibraries, "
                       "FakeMaterialLibrary, FakeAppearances, FakeAppearance, _Properties, "
                       "ColorProperty, FakeColor); tests/fakes/design.py FakeOccurrence - its "
                       "appearance default and its absent opacity member - and its BRepBody / "
                       "BRepFace; appearance_set._reads_as and appearance_set._apply_opacity read "
                       "these members live"),
        "facts_on_pass": {"behavior.occurrence_appearance_none_by_default": True,
                          "behavior.occurrence_has_no_opacity": True,
                          "behavior.native_body_visible_opacity_raises": True},
        "body": """
    libs = app.materialLibraries
    lib = None
    for i in range(libs.count):
        if libs.item(i).appearances.count:
            lib = libs.item(i)
            break
    ap = lib.appearances.item(0)
    props = ap.appearanceProperties
    cp = None
    for i in range(props.count):
        if type(props.item(i)).__name__ == "ColorProperty":
            cp = props.item(i)
            break
    live = [("MaterialLibraries", libs), ("MaterialLibrary", lib),
            ("Appearances", lib.appearances), ("Appearance", ap), ("Properties", props),
            ("ColorProperty", cp), ("Color", cp.value)]
    wrong = [lbl + "=" + type(o).__name__ for lbl, o in live if type(o).__name__ != lbl]
    counts = [dump_shape(lbl, o) for lbl, o in live]
    no_appearance_properties = not (hasattr(adsk.core, "AppearanceProperties")
                                    or hasattr(adsk.fusion, "AppearanceProperties"))
    plain = {}
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        d = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        native, occ = make_placed_box(d, "AppearanceDump")
        comp = occ.component
        proxy = native.createForAssemblyContext(occ)
        plain["body_appearance"] = type(native.appearance).__name__
        plain["body_opacity"] = native.opacity
        try:
            plain["native_visible_opacity"] = repr(native.visibleOpacity)
        except Exception as ex:
            plain["native_visible_opacity"] = "raised " + type(ex).__name__ + " " + str(ex)[:70]
        plain["proxy_visible_opacity"] = proxy.visibleOpacity
        plain["occ_appearance"] = occ.appearance
        plain["occ_visible_opacity"] = occ.visibleOpacity
        plain["occ_has_opacity"] = hasattr(occ, "opacity")
        plain["component_opacity"] = comp.opacity
        plain["face_appearance"] = type(native.faces.item(0).appearance).__name__
    finally:
        tmp.close(False)
    emit(len(counts) == 7 and all(c > 0 for c in counts) and not wrong
         and no_appearance_properties
         and plain["body_appearance"] == "Appearance" and plain["body_opacity"] == 1.0
         and plain["native_visible_opacity"].startswith("raised ")
         and plain["proxy_visible_opacity"] == 1.0 and plain["occ_appearance"] is None
         and plain["occ_visible_opacity"] == 1.0 and plain["occ_has_opacity"] is False
         and plain["component_opacity"] == 1.0 and plain["face_appearance"] == "Appearance",
         "shape-dump-appearance-world: " + str(len(counts)) + " types, min attrs "
         + str(min(counts)) + ", library '" + lib.name + "' of " + str(libs.count)
         + " holds " + str(lib.appearances.count) + " appearances, '" + ap.name
         + "' carries " + str(props.count) + " properties whose collection answers "
         + type(props).__name__ + " (adsk carries no AppearanceProperties type: "
         + str(no_appearance_properties) + "); plain reads on a fresh entity: BRepBody.appearance="
         + plain["body_appearance"] + " opacity=" + repr(plain["body_opacity"])
         + " native visibleOpacity=" + plain["native_visible_opacity"]
         + " proxy visibleOpacity=" + repr(plain["proxy_visible_opacity"])
         + ", Occurrence.appearance=" + repr(plain["occ_appearance"]) + " visibleOpacity="
         + repr(plain["occ_visible_opacity"]) + " carries an opacity member="
         + str(plain["occ_has_opacity"]) + ", Component.opacity="
         + repr(plain["component_opacity"]) + ", BRepFace.appearance="
         + plain["face_appearance"] + ", mislabelled " + (", ".join(wrong) or "none"))
""",
    },
    {
        "id": "appearance-duplicate-name-raises-and-occurrence-write-fans-out",
        "claim": ("design.appearances.addByCopy(base, name) with a name the document already holds "
                  "RAISES '3 : appearance name already exists in document' and the collection "
                  "count does not move - the refusal is a raise, not a null answer. An "
                  "OCCURRENCE-level appearance write then lands on every body of that occurrence "
                  "carrying no body-level override and leaves one that does: a two-body component "
                  "whose second body was given its own appearance reads first='the occurrence's', "
                  "second='its own' after the write, through the native bodies and through their "
                  "assembly-context proxies alike. BRepBody.visibleOpacity on the NATIVE body "
                  "raises InternalValidationError both before AND after body.opacity is written "
                  "(the write itself reads back), so the assembly-context proxy is the only route "
                  "that answers a rendered opacity"),
        "encoded_in": ("tests/fakes/appearance.py FakeAppearances.addByCopy; tests/fakes/design.py "
                       "FanoutOcc's parent FakeOccurrence and its BRepBody appearance member; "
                       "appearance_set's occurrence arm and its opacity read-back"),
        "body": """
    libs = app.materialLibraries
    lib = None
    for i in range(libs.count):
        if libs.item(i).appearances.count > 1:
            lib = libs.item(i)
            break
    src_a = lib.appearances.item(0)
    src_b = lib.appearances.item(1)
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        d = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        root = d.rootComponent
        occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        comp = occ.component
        comp.name = "AppProbe"
        for x in (0.0, 3.0):
            sk = comp.sketches.add(comp.xYConstructionPlane)
            sk.sketchCurves.sketchLines.addTwoPointRectangle(
                adsk.core.Point3D.create(x, 0.0, 0.0),
                adsk.core.Point3D.create(x + 1.0, 1.0, 0.0))
            comp.features.extrudeFeatures.addSimple(
                sk.profiles.item(0), adsk.core.ValueInput.createByReal(1.0),
                adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        plain, overridden = comp.bRepBodies.item(0), comp.bRepBodies.item(1)
        first = d.appearances.addByCopy(src_a, "ProbeCopy")
        n1 = d.appearances.count
        dup, why = None, ""
        try:
            dup = d.appearances.addByCopy(src_b, "ProbeCopy")
        except Exception as ex:
            why = (str(ex).strip().splitlines() or [""])[-1][:70]
        added = d.appearances.count - n1
        override = d.appearances.addByCopy(src_b, "ProbeOverride")
        overridden.appearance = override
        occ.appearance = first
        fan = (plain.appearance.name, overridden.appearance.name,
               plain.createForAssemblyContext(occ).appearance.name,
               overridden.createForAssemblyContext(occ).appearance.name)
        def vis(body):
            try:
                return repr(body.visibleOpacity)
            except Exception as ex:
                return "raised " + type(ex).__name__
        before = vis(plain)
        plain.opacity = 0.5
        after, wrote = vis(plain), plain.opacity
        proxy_after = vis(plain.createForAssemblyContext(occ))
    finally:
        tmp.close(False)
    emit(dup is None and why.startswith("3 : appearance name already exists") and added == 0
         and fan == ("ProbeCopy", "ProbeOverride", "ProbeCopy", "ProbeOverride")
         and before.startswith("raised ") and after.startswith("raised ")
         and wrote == 0.5 and proxy_after == "0.5",
         "appearance-duplicate-name-raises-and-occurrence-write-fans-out: duplicate addByCopy="
         + ("raised " + repr(why) if why else "ANSWERED " + repr(dup and dup.name))
         + " added=" + str(added) + " (expect 0) | after the occurrence write (plain body,"
         " overridden body, and each through its proxy)=" + str(fan)
         + " expect ('ProbeCopy','ProbeOverride','ProbeCopy','ProbeOverride')"
         + " | native visibleOpacity before=" + before + " after opacity:=" + repr(wrote)
         + " it reads " + after + " while the proxy reads " + proxy_after)
""",
    },
    {
        "id": "shape-dump-pmi-world",
        "claim": "PMI authoring runs on this installation, and the row proves it by CREATING what it dumps: in a scratch design holding a box with one hole, component.pmiAnnotations answers a PMIAnnotations, its leaderLineNotes a PMILeaderLineNotes whose createInput(planar face) answers a PMILeaderLineNoteInput and add() a PMILeaderLineNote, its holeThreadNotes a PMIHoleThreadNotes whose createInput([cylindrical face]) answers a PMIHoleThreadNoteInput and add() a PMIHoleThreadNote. A created note's segments answer a PMISegmentVector, and the six document-free factories - PMITextSegment.create(text), PMISymbolSegment.create(member), PMILineBreakSegment.create(), PMIGeometricValue.create(), PMIGeometricValueTolerance.create() and PMIDisplaySettings.create(), the last four taking NO argument - each answer their own type. Both adds are gated, so a session where the Design/Manufacturing Extension is not entitled FAILS this row naming the refusal instead of passing on the reads alone",
        "encoded_in": ("tests/fakes/pmi.py's PMI world - FakePMIAnnotations, the two note "
                       "collections and inputs, FakePMILeaderLineNote/FakePMIHoleThreadNote, the "
                       "segment, value, tolerance and display-settings fakes; "
                       "_pmi.walk_annotations, _pmi.build_segments and _pmi.annotation_record "
                       "read these types live"),
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        d = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        root = d.rootComponent
        sk = root.sketches.add(root.xYConstructionPlane)
        sk.sketchCurves.sketchLines.addTwoPointRectangle(
            adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(4.0, 4.0, 0.0))
        body = root.features.extrudeFeatures.addSimple(
            sk.profiles.item(0), adsk.core.ValueInput.createByReal(1.0),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation).bodies.item(0)
        top = None
        for i in range(body.faces.count):
            f = body.faces.item(i)
            if type(f.geometry).__name__ == "Plane" and f.pointOnFace.z > 0.5:
                top = f
        holes = root.features.holeFeatures
        hin = holes.createSimpleInput(adsk.core.ValueInput.createByReal(0.4))
        sk2 = root.sketches.add(top)
        hin.setPositionBySketchPoint(sk2.sketchPoints.add(adsk.core.Point3D.create(2.0, 2.0, 0.0)))
        hin.setDistanceExtent(adsk.core.ValueInput.createByReal(0.5))
        holes.add(hin)
        cyl = None
        for i in range(body.faces.count):
            if type(body.faces.item(i).geometry).__name__ == "Cylinder":
                cyl = body.faces.item(i)
        plane_face = None
        for i in range(body.faces.count):
            f = body.faces.item(i)
            if type(f.geometry).__name__ == "Plane" and f.pointOnFace.z > 0.5:
                plane_face = f
        notes = root.pmiAnnotations.leaderLineNotes
        note_in = notes.createInput(plane_face)
        note_in.segments = [adsk.fusion.PMITextSegment.create("ShapeDump")]
        note = notes.add(note_in)
        hnotes = root.pmiAnnotations.holeThreadNotes
        hole_in = hnotes.createInput([cyl])
        hole_note = hnotes.add(hole_in)
        live = [("PMIAnnotations", root.pmiAnnotations),
                ("PMILeaderLineNotes", notes), ("PMILeaderLineNoteInput", note_in),
                ("PMILeaderLineNote", note), ("PMIHoleThreadNotes", hnotes),
                ("PMIHoleThreadNoteInput", hole_in), ("PMIHoleThreadNote", hole_note),
                ("PMISegmentVector", note.segments),
                ("PMITextSegment", adsk.fusion.PMITextSegment.create("dump")),
                ("PMISymbolSegment", adsk.fusion.PMISymbolSegment.create(
                    adsk.fusion.PMISymbolTypes.DiameterPMISymbolType)),
                ("PMILineBreakSegment", adsk.fusion.PMILineBreakSegment.create()),
                ("PMIGeometricValue", adsk.fusion.PMIGeometricValue.create()),
                ("PMIGeometricValueTolerance", adsk.fusion.PMIGeometricValueTolerance.create()),
                ("PMIDisplaySettings", adsk.fusion.PMIDisplaySettings.create())]
        wrong = [lbl + "=" + type(o).__name__ for lbl, o in live if type(o).__name__ != lbl]
        counts = [dump_shape(lbl, o) for lbl, o in live]
        emit(len(counts) == 14 and all(c > 0 for c in counts) and not wrong
             and notes.count == 1 and hnotes.count == 1 and len(note.segments) == 1,
             "shape-dump-pmi-world: " + str(len(counts)) + " types, min attrs "
             + str(min(counts)) + ", PMI authoring is entitled here - the component holds "
             + str(notes.count) + " leader note carrying " + str(len(note.segments))
             + " segment and " + str(hnotes.count) + " hole/thread note, both created by this "
             "row, mislabelled " + (", ".join(wrong) or "none"))
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "shape-dump-mesh-calculator-quality",
        "claim": "A BRepBody's meshManager answers a MeshManager whose createMeshCalculator() answers a TriangleMeshCalculator; a FRESH calculator reads all four of its knobs - maxNormalDeviation, surfaceTolerance, maxAspectRatio, maxSideLength - as 0.0, so it carries no tolerances of its own. setQuality returns True and writes surfaceTolerance ALONE: the other three stay 0.0 after it, and HighQualityTriangleMesh lands a strictly SMALLER surfaceTolerance than NormalQualityTriangleMesh on the same body, which is the comparison a quality that silently did nothing would fail. calculate() answers a TriangleMesh carrying nodes, and meshManager.displayMeshes answers a TriangleMeshList",
        "encoded_in": ("tests/fakes/mesh.py FakeMeshManager and FakeTriangleMeshCalculator (its "
                       "four zero defaults and the surfaceTolerance setQuality writes); "
                       "save_as_mesh._tessellate reads meshManager, createMeshCalculator, the "
                       "BOOL setQuality returns and calculate live"),
        "facts_on_pass": {"behavior.mesh_set_quality_writes_surface_tolerance_only": True},
        "need_box": True,
        "body": """
    def knobs(c):
        return (c.maxNormalDeviation, c.surfaceTolerance, c.maxAspectRatio, c.maxSideLength)

    mm = body.meshManager
    calc = mm.createMeshCalculator()
    fresh = knobs(calc)
    took = calc.setQuality(adsk.fusion.TriangleMeshQualityOptions.NormalQualityTriangleMesh)
    normal = knobs(calc)
    high_calc = mm.createMeshCalculator()
    high_took = high_calc.setQuality(adsk.fusion.TriangleMeshQualityOptions.HighQualityTriangleMesh)
    high = knobs(high_calc)
    mesh = calc.calculate()
    live = [("MeshManager", mm), ("TriangleMeshCalculator", calc), ("TriangleMesh", mesh),
            ("TriangleMeshList", mm.displayMeshes)]
    wrong = [lbl + "=" + type(o).__name__ for lbl, o in live if type(o).__name__ != lbl]
    counts = [dump_shape(lbl, o) for lbl, o in live]
    untouched = [normal[i] == 0.0 and high[i] == 0.0 for i in (0, 2, 3)]
    emit(len(counts) == 4 and all(c > 0 for c in counts) and not wrong
         and fresh == (0.0, 0.0, 0.0, 0.0) and took is True and high_took is True
         and normal[1] > 0.0 and high[1] > 0.0 and high[1] < normal[1] and all(untouched)
         and mesh.nodeCount > 0,
         "shape-dump-mesh-calculator-quality: " + str(len(counts)) + " types, min attrs "
         + str(min(counts)) + ", a fresh calculator reads " + str(fresh)
         + " as (maxNormalDeviation, surfaceTolerance, maxAspectRatio, maxSideLength); setQuality "
         "returned " + repr(took) + "/" + repr(high_took) + " and left normal=" + str(normal)
         + " high=" + str(high) + ", so only surfaceTolerance moved and high < normal is "
         + str(high[1] < normal[1]) + "; calculate() gave " + str(mesh.nodeCount) + " nodes and "
         + str(mesh.triangleCount) + " triangles, displayMeshes holds "
         + str(mm.displayMeshes.count) + ", mislabelled " + (", ".join(wrong) or "none"))
""",
    },
    {
        "id": "shape-dump-units-manager",
        "claim": "A design's unitsManager and its fusionUnitsManager BOTH answer a FusionUnitsManager - neither read hands back the base UnitsManager, so that base type is dumped from its CLASS object under the class-dir-equals-instance-dir-minus-'this' reading, re-measured in this row on the live FusionUnitsManager. The base's public names are a STRICT subset of the subclass's, and the four the subclass adds are exactly design, distanceDisplayUnits, massDisplayUnits and unitSystem: a build that moved a member between the two fails this row rather than letting a UnitsManager-shaped fake keep a surface the live object no longer has",
        "encoded_in": ("tests/fakes/design.py FakeUnitsManager - this dump is the SHAPES key that "
                       "sweeps its defaultLengthUnits and evaluateExpression, both among "
                       "UnitsManager's 15 public names; _param_common._param_summary converts "
                       "through the same object"),
        "body": """
    fum = des.unitsManager
    fum_two = des.fusionUnitsManager
    counts = [dump_shape("FusionUnitsManager", fum)]
    counts.append(dump_shape("UnitsManager", adsk.core.UnitsManager))
    base = set(n for n in dir(adsk.core.UnitsManager) if not n.startswith("_"))
    derived = set(n for n in dir(adsk.fusion.FusionUnitsManager) if not n.startswith("_"))
    class_ok = derived == set(n for n in dir(fum) if not n.startswith("_")) - set(["this"])
    both_fusion = (type(fum).__name__ == "FusionUnitsManager"
                   and type(fum_two).__name__ == "FusionUnitsManager")
    added = sorted(derived - base)
    emit(len(counts) == 2 and all(c > 0 for c in counts) and class_ok and both_fusion
         and base < derived
         and added == ["design", "distanceDisplayUnits", "massDisplayUnits", "unitSystem"],
         "shape-dump-units-manager: " + str(len(counts)) + " types, min attrs "
         + str(min(counts)) + ", unitsManager answers " + type(fum).__name__
         + " and fusionUnitsManager answers " + type(fum_two).__name__
         + " so UnitsManager comes off the class (class dir == instance dir minus 'this': "
         + str(class_ok) + "); UnitsManager carries " + str(len(base))
         + " public names, FusionUnitsManager " + str(len(derived))
         + ", a strict superset adding " + ", ".join(added))
""",
    },
    {
        "id": "units-manager-internal-units-and-convert",
        "claim": "UnitsManager.internalUnits is not a unit name: it reads the SENTINEL string 'InternalUnits', and that sentinel is POLYMORPHIC - handed to convert() as the from-unit it means centimetres for a length target and radians for an angular one, so ONE call shape converts both. convert(1.0, sentinel, 'mm') answers 10.0, convert(1.0, sentinel, 'ft') 0.032808..., and convert(1.0, sentinel, 'deg') 57.295777... off the same 1.0. The refusals are the other half of the claim, because they are what a fake that scaled by a table would never produce: an EMPTY to-unit raises '3 : Bad units parameter' (so the sentinel cannot be converted into 'no units'), a literal 'cm' to 'deg' raises '6 : The input and output units are not compatible' (only the sentinel crosses the length/angle boundary), and a to-unit outside the vocabulary raises '6 : The units parameter is not a valid unit string'",
        "encoded_in": ("tests/fakes/design.py FakeUnitsManager - its internalUnits reads the sentinel "
                       "flag and its convert refuses the incompatible pair on the other; "
                       "_param_common._param_summary passes units_manager.internalUnits straight "
                       "through as convert's from-unit, so the sentinel is what makes that "
                       "conversion work on a length AND on an angle"),
        "facts_on_pass": {"behavior.units_manager_internal_units_sentinel": "InternalUnits",
                          "behavior.units_manager_convert_refuses_incompatible": True},
        "read_only": True,
        "body": """
    um = des.unitsManager
    sentinel = um.internalUnits

    def answer(value, src, dst):
        try:
            return um.convert(value, src, dst)
        except Exception as exc:
            return "raised " + str(exc)

    mm = answer(1.0, sentinel, "mm")
    ft = answer(1.0, sentinel, "ft")
    deg = answer(1.0, sentinel, "deg")
    blank = answer(1.0, sentinel, "")
    incompatible = answer(1.0, "cm", "deg")
    bad_unit = answer(1.0, "cm", "xyzzy")

    def near(got, want):
        return isinstance(got, float) and abs(got - want) < 1e-9

    emit(sentinel == "InternalUnits"
         and near(mm, 10.0) and near(ft, 0.032808398950131233)
         and near(deg, 57.29577951308232)
         and blank == "raised 3 : Bad units parameter"
         and incompatible == "raised 6 : The input and output units are not compatible"
         and bad_unit == "raised 6 : The units parameter is not a valid unit string",
         "units-manager-internal-units-and-convert: internalUnits=" + repr(sentinel)
         + "; 1.0 of it reads " + repr(mm) + " mm, " + repr(ft) + " ft, " + repr(deg)
         + " deg - one sentinel, cm for a length and radians for an angle; to '' -> "
         + repr(blank) + "; 'cm'->'deg' -> " + repr(incompatible)
         + "; 'cm'->'xyzzy' -> " + repr(bad_unit))
""",
    },
    {
        "id": "parameter-favorite-maker-text-value-and-fresh-appearances",
        "claim": "Four plain reads the parameter and appearance doubles stand on, measured on one scratch design carrying a dimensioned sketch, an extrude and one TEXT user parameter. (1) ModelParameter.isFavorite reads the bool False on every allParameters entry outside userParameters. (2) ModelParameter.createdBy never declines and never reads None: each one answers the entity that made it - the Sketch for a sketch dimension's parameter, the ExtrudeFeature for an extrude's - so a model parameter with no readable maker was not reachable here; the DECLINE belongs to UserParameter, which carries NO createdBy member at all and raises AttributeError on the read. (3) Parameter.value on a TEXT parameter RAISES 'Parameter is not numeric type' while textValue answers the unquoted string, so the textValue fallback is live code; a text parameter is made with units 'Text' and a QUOTED string-literal expression, an unquoted one is refused at add with 'Invalid expression', and the same unquoted string under empty units makes a NUMERIC parameter whose textValue raises 'Parameter is not text type' instead. (4) Design.appearances is an Appearances collection that starts EMPTY and fills from GEOMETRY, not from any apply: it counts 0 on a design with no bodies, still 0 after a sketch is drawn, and becomes 1 the moment the extrude brings a body in - that one entry is the body's default material appearance, named 'Steel - Satin'. The row reads the count at all three moments, so binding the collection early and asserting its count late (which reads the CURRENT count, never the captured one) cannot pass this claim",
        "encoded_in": ("tests/fakes/design.py FakeModelParameter (its owner-None branch and its "
                       "isFavorite default), FakeUserParameter (its absent createdBy and its "
                       "text-parameter value read) and MakeDesign's appearances default; "
                       "_param_common._owner_facts and _param_common._param_summary branch on the "
                       "first three"),
        "facts_on_pass": {"behavior.model_parameter_created_by_answers_maker": True,
                          "behavior.user_parameter_has_no_created_by": True,
                          "behavior.text_parameter_value_raises": True,
                          "behavior.fresh_design_appearances_count": 0},
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        d = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        root = d.rootComponent
        appearances = d.appearances
        # The COUNT is captured at each moment, never the collection: `appearances` is live, so
        # reading its count at emit time would answer the post-extrude number for every moment.
        appearances_t0 = appearances.count
        sk = root.sketches.add(root.xYConstructionPlane)
        sk.sketchCurves.sketchLines.addTwoPointRectangle(
            adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(2.0, 1.0, 0.0))
        line = sk.sketchCurves.sketchLines.item(0)
        sk.sketchDimensions.addDistanceDimension(
            line.startSketchPoint, line.endSketchPoint,
            adsk.fusion.DimensionOrientations.AlignedDimensionOrientation,
            adsk.core.Point3D.create(1.0, -1.0, 0.0))
        appearances_sketched = appearances.count
        root.features.extrudeFeatures.addSimple(
            sk.profiles.item(0), adsk.core.ValueInput.createByReal(1.0),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        appearances_bodied = appearances.count
        appearance_name = appearances.item(0).name if appearances_bodied else None
        makers = []
        favorites = []
        for i in range(d.allParameters.count):
            p = d.allParameters.item(i)
            favorites.append(p.isFavorite)
            try:
                makers.append(type(p.createdBy).__name__)
            except Exception as ex:
                makers.append("raised " + type(ex).__name__)
        model_only = d.userParameters.count == 0
        text = d.userParameters.add(
            "shapeDumpText", adsk.core.ValueInput.createByString("'Roughing'"), "Text", "")
        try:
            text_value = repr(text.value)
        except Exception as ex:
            text_value = "raised " + type(ex).__name__ + " " + str(ex)[:60]
        try:
            user_maker = repr(text.createdBy)
        except Exception as ex:
            user_maker = "raised " + type(ex).__name__ + " " + str(ex)[:60]
        try:
            d.userParameters.add(
                "shapeDumpBare", adsk.core.ValueInput.createByString("Roughing"), "Text", "")
            unquoted = "accepted"
        except Exception as ex:
            unquoted = "raised " + type(ex).__name__ + " " + str(ex)[:60]
        numeric = d.userParameters.add(
            "shapeDumpNumeric", adsk.core.ValueInput.createByString("Roughing"), "", "")
        try:
            numeric_text = repr(numeric.textValue)
        except Exception as ex:
            numeric_text = "raised " + type(ex).__name__ + " " + str(ex)[:60]
        emit(model_only and len(favorites) == 3 and all(f is False for f in favorites)
             and sorted(set(makers)) == ["ExtrudeFeature", "Sketch"]
             and text.valueType == adsk.fusion.ParameterValueTypes.TextParameterValueType
             and text.textValue == "Roughing" and text.unit == "Text"
             and text_value.startswith("raised ") and "not numeric type" in text_value
             and user_maker.startswith("raised AttributeError")
             and unquoted.startswith("raised ") and "Invalid expression" in unquoted
             and numeric.valueType == adsk.fusion.ParameterValueTypes.NumericParameterValueType
             and numeric_text.startswith("raised ") and "not text type" in numeric_text
             and type(appearances).__name__ == "Appearances" and appearances_t0 == 0
             and appearances_sketched == 0 and appearances_bodied == 1
             and appearance_name == "Steel - Satin",
             "parameter-favorite-maker-text-value-and-fresh-appearances: "
             + str(len(favorites)) + " model parameters (no user parameter yet: "
             + str(model_only) + ") read isFavorite " + str(favorites) + " and createdBy "
             + str(makers) + "; a TEXT parameter (units 'Text', a quoted literal) reads unit "
             + repr(text.unit) + " valueType " + str(text.valueType) + " textValue "
             + repr(text.textValue) + " and value " + text_value
             + "; UserParameter.createdBy " + user_maker + "; the same string UNQUOTED under "
             "units 'Text' " + unquoted + ", and under empty units it makes valueType "
             + str(numeric.valueType) + " whose textValue " + numeric_text
             + "; " + type(appearances).__name__ + " counts " + str(appearances_t0)
             + " with no body, " + str(appearances_sketched) + " after the sketch and "
             + str(appearances_bodied) + " after the extrude, that entry being "
             + repr(appearance_name) + " - it fills from geometry, not from an apply")
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "brepedge-evaluator-tangent-follows-curve-not-edge",
        "claim": ("BRepEdge.evaluator.getTangent, taken at the parameter of startVertex.geometry, "
                  "returns a vector ANTI-parallel to (endVertex - startVertex) exactly when "
                  "isParamReversed reads True, and parallel when it reads False - the evaluator "
                  "follows the underlying CURVE, so the edge's own heading is the flag applied to "
                  "it. Measured over EVERY linear edge of a shelled box, and the row fails unless "
                  "at least one of them reads isParamReversed True, so a rig with nothing to "
                  "discriminate cannot pass. A plain box read the flag False on all twelve edges"),
        "encoded_in": ("tests/fakes/design.py's BRepEdge fake (its param_reversed argument) and "
                       "_edge_common._edge_tangent, which negates the evaluator tangent "
                       "when isParamReversed reads True before _edge_convexity signs one edge's "
                       "dihedral off that heading"),
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        d = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        root = d.rootComponent
        sk = root.sketches.add(root.xYConstructionPlane)
        sk.sketchCurves.sketchLines.addTwoPointRectangle(
            adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(4.0, 3.0, 0.0))
        solid = root.features.extrudeFeatures.addSimple(
            sk.profiles.item(0), adsk.core.ValueInput.createByReal(2.0),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation).bodies.item(0)
        top = None
        for i in range(solid.faces.count):
            f = solid.faces.item(i)
            if type(f.geometry).__name__ == "Plane" and abs(f.geometry.normal.z - 1.0) < 1e-6:
                top = f
        opening = adsk.core.ObjectCollection.create()
        opening.add(top)
        # The shell is what puts reversed edges on the body: the same box unshelled read the flag
        # False on every edge, leaving the claim's True half unmeasured.
        shell_in = root.features.shellFeatures.createInput(opening)
        shell_in.insideThickness = adsk.core.ValueInput.createByReal(0.3)
        root.features.shellFeatures.add(shell_in)
        linear = 0
        n_reversed = 0
        agree = 0
        disagreed = []
        for i in range(solid.edges.count):
            e = solid.edges.item(i)
            if type(e.geometry).__name__ != "Line3D":
                continue
            linear += 1
            start = e.startVertex.geometry
            end = e.endVertex.geometry
            against = bool(e.isParamReversed)
            if against:
                n_reversed += 1
            at = e.evaluator.getParameterAtPoint(start)
            got = e.evaluator.getTangent(at[1]) if at[0] else (False, None)
            if not (at[0] and got[0]):
                disagreed.append("edge " + str(i) + " did not evaluate")
                continue
            t = got[1]
            dot = (t.x * (end.x - start.x) + t.y * (end.y - start.y)
                   + t.z * (end.z - start.z))
            if (dot < 0.0) == against:
                agree += 1
            else:
                disagreed.append("edge " + str(i) + " isParamReversed=" + repr(against)
                                 + " dot=" + ("%.4f" % dot))
        emit(linear > 0 and n_reversed > 0 and agree == linear,
             "brepedge-evaluator-tangent-follows-curve-not-edge: shelled box, linear edges="
             + str(linear) + " reading isParamReversed True=" + str(n_reversed)
             + " | tangent-vs-start-to-end sign matches the flag on " + str(agree)
             + " of them | mismatches=" + str(disagreed[:3]))
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "matrix3d-invert-singular-answers-true-and-corrupts",
        "claim": ("Matrix3D.invert() on a SINGULAR matrix returns True and leaves the matrix "
                  "corrupted (nan/inf entries) - the False return every careful caller gates on "
                  "is a decline the singular case never produces, so the gate is defensive, not "
                  "a singularity detector; rigid occurrence transforms cannot be singular, which "
                  "is why the callers' math stays sound"),
        "encoded_in": ("model_inspect._measuring_axes and model_hole._world_lift invert() gates; "
                       "tests/fakes/geometry.py FakeMatrix3D invertible=False contract"),
        "body": """
    import math
    m = adsk.core.Matrix3D.create()
    ok_set = m.setWithArray([1.0, 0.0, 0.0, 2.0,
                             0.0, 1.0, 0.0, 3.0,
                             0.0, 0.0, 0.0, 1.0,
                             0.0, 0.0, 0.0, 1.0])
    before = list(m.asArray())
    r = m.invert()
    after = list(m.asArray())
    corrupted = (before != after) and any(
        (isinstance(v, float) and (math.isnan(v) or math.isinf(v))) for v in after)
    emit(bool(ok_set) and r is True and corrupted,
         "matrix3d-invert-singular-answers-true-and-corrupts: setWithArray=" + str(ok_set)
         + " invert_returned=" + str(r) + " corrupted_to_nan_inf=" + str(corrupted))
""",
    },
    {
        "id": "matrix3d-transformby-applies-the-argument-after-self",
        "claim": ("Matrix3D.transformBy(other) applies `other` AFTER this matrix in world "
                  "coordinates: a 90 deg Z rotation transformed by a (5,0,0) translation reads "
                  "translation (5,0,0), while the same translation transformed by the rotation "
                  "reads (0,5,0) - so the composition is other*self, and a caller composing a "
                  "translation matrix onto a rotation keeps the offset unrotated. The ROTATION "
                  "product follows the same order: rotX(90).transformBy(rotZ(90)) sends +X to +Y "
                  "with asArray rows (0,0,1)/(1,0,0)/(0,1,0), where the swapped product would send "
                  "+X to +Z"),
        "encoded_in": ("commands/mcpServer/tools/assembly_move.py translation compose; "
                       "tests/fakes/geometry.py FakeMatrix3D.transformBy"),
        "body": """
    import math
    a = adsk.core.Matrix3D.create()
    a.setToRotation(math.pi / 2.0, adsk.core.Vector3D.create(0.0, 0.0, 1.0),
                    adsk.core.Point3D.create(0.0, 0.0, 0.0))
    b = adsk.core.Matrix3D.create()
    b.translation = adsk.core.Vector3D.create(5.0, 0.0, 0.0)
    a.transformBy(b)
    fwd = a.translation
    c = adsk.core.Matrix3D.create()
    c.translation = adsk.core.Vector3D.create(5.0, 0.0, 0.0)
    d = adsk.core.Matrix3D.create()
    d.setToRotation(math.pi / 2.0, adsk.core.Vector3D.create(0.0, 0.0, 1.0),
                    adsk.core.Point3D.create(0.0, 0.0, 0.0))
    c.transformBy(d)
    rev = c.translation
    x90 = adsk.core.Matrix3D.create()
    x90.setToRotation(math.pi / 2.0, adsk.core.Vector3D.create(1.0, 0.0, 0.0),
                      adsk.core.Point3D.create(0.0, 0.0, 0.0))
    z90 = adsk.core.Matrix3D.create()
    z90.setToRotation(math.pi / 2.0, adsk.core.Vector3D.create(0.0, 0.0, 1.0),
                      adsk.core.Point3D.create(0.0, 0.0, 0.0))
    x90.transformBy(z90)
    a = [round(v, 6) for v in x90.asArray()]
    rows = [a[0:3], a[4:7], a[8:11]]
    spun = adsk.core.Vector3D.create(1.0, 0.0, 0.0)
    spun.transformBy(x90)
    emit(abs(fwd.x - 5.0) < 1e-9 and abs(fwd.y) < 1e-9
         and abs(rev.x) < 1e-9 and abs(rev.y - 5.0) < 1e-9
         and rows == [[0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
         and abs(spun.y - 1.0) < 1e-9 and abs(spun.z) < 1e-9,
         "matrix3d-transformby-applies-the-argument-after-self: rot.transformBy(trans)=("
         + str(fwd.x) + "," + str(fwd.y) + ") expect (5,0) | trans.transformBy(rot)=("
         + str(rev.x) + "," + str(rev.y) + ") expect (0,5) | rotX.transformBy(rotZ) rows="
         + str(rows) + " expect [[0,0,1],[1,0,0],[0,1,0]] | +X spins to ("
         + str(round(spun.x, 6)) + "," + str(round(spun.y, 6)) + ","
         + str(round(spun.z, 6)) + ") expect (0,1,0)")
""",
    },
    {
        "id": "matrix3d-asarray-row-major-translation-3-7-11",
        "claim": ("Matrix3D.asArray() returns the 16 cells ROW-MAJOR with the translation in "
                  "elements 3, 7 and 11 - a column-major read would find the offset at 12/13/14 "
                  "and mis-report every position built from it"),
        "encoded_in": ("commands/mcpServer/tools/assembly_move.py before/after asArray compare; "
                       "tests/fakes/geometry.py FakeMatrix3D.asArray"),
        "body": """
    m = adsk.core.Matrix3D.create()
    m.translation = adsk.core.Vector3D.create(1.0, 2.0, 3.0)
    a = list(m.asArray())
    emit(len(a) == 16 and a[3] == 1.0 and a[7] == 2.0 and a[11] == 3.0
         and a[12] == 0.0 and a[13] == 0.0 and a[14] == 0.0,
         "matrix3d-asarray-row-major-translation-3-7-11: [3,7,11]=("
         + str(a[3]) + "," + str(a[7]) + "," + str(a[11]) + ") expect (1,2,3) | [12,13,14]=("
         + str(a[12]) + "," + str(a[13]) + "," + str(a[14]) + ") expect (0,0,0)")
""",
    },
    {
        "id": "matrix3d-translation-copies-and-refuses-none",
        "claim": ("Matrix3D.translation hands back a FRESH Vector3D on every read - the object "
                  "read is never the one assigned, so mutating a read result moves nothing - and "
                  "assigning None RAISES '3 : invalid argument value' rather than clearing the "
                  "column, so the member takes a Vector3D and nothing else"),
        "encoded_in": "tests/fakes/geometry.py FakeMatrix3D.translation property and its setter",
        # The None assignment RAISES, and a caught adsk error still takes the whole Python.Run down
        # outside a read-only context; the matrix here is a value object, so nothing is given up.
        "read_only": True,
        "body": """
    m = adsk.core.Matrix3D.create()
    v = adsk.core.Vector3D.create(1.0, 2.0, 3.0)
    m.translation = v
    read = m.translation
    is_same = read is v
    round_trips = read.x == 1.0 and read.y == 2.0 and read.z == 3.0
    refused = False
    try:
        m.translation = None
    except Exception as e:
        refused = "invalid argument value" in str(e)
    # The member is TYPED, not duck-typed: a Point3D carries the same x/y/z and is still refused.
    wrong = []
    for label, bad in (("Point3D", adsk.core.Point3D.create(1.0, 2.0, 3.0)),
                       ("tuple", (1.0, 2.0, 3.0)), ("int", 5)):
        try:
            m.translation = bad
            wrong.append(label + "=ACCEPTED")
        except Exception as e:
            wrong.append(label + "=" + type(e).__name__)
    typed = all("ACCEPTED" not in w for w in wrong)
    emit(is_same is False and round_trips and refused and typed,
         "matrix3d-translation-copies-and-refuses-none: read_is_assigned=" + str(is_same)
         + " (expect False) round_trips=" + str(round_trips)
         + " none_refused=" + str(refused) + " non_vector=" + ",".join(wrong))
""",
    },
    {
        "id": "matrix3d-setrotation-bakes-the-pivot-into-the-translation-column",
        "claim": ("Matrix3D.setToRotation(angle, axis, pivot) carries the pivot as a CORRECTION in "
                  "the translation column, not as a separate centre: a 90 deg Z rotation about "
                  "(10,0,0) reads translation (10,-10,0) and maps that pivot point to itself. "
                  "ASSIGNING mat.translation overwrites that correction, so the same matrix then "
                  "rotates about the WORLD origin - the pivot maps to (0,10,0) with (0,0,0) "
                  "assigned and to (5,10,0) with (5,0,0) assigned - while COMPOSING the offset "
                  "with transformBy keeps the pivot and adds the shift, mapping it to (15,0,0). "
                  "transformBy also ACCUMULATES: two 90 deg Z rotations send +X to -X"),
        "encoded_in": ("commands/mcpServer/tools/assembly_move.py, whose rotate arm composes the "
                       "translation as its own matrix rather than assigning the column; "
                       "tests/fakes/geometry.py FakeMatrix3D.setToRotation and its transformBy"),
        # Transient value objects only - nothing in the design is touched, so the row is read-only.
        "read_only": True,
        "body": """
    import math

    def spun(m, x, y, z):
        p = adsk.core.Point3D.create(x, y, z)
        p.transformBy(m)
        return (round(p.x, 6) + 0.0, round(p.y, 6) + 0.0, round(p.z, 6) + 0.0)

    def rot(pivot):
        m = adsk.core.Matrix3D.create()
        m.setToRotation(math.radians(90.0), adsk.core.Vector3D.create(0.0, 0.0, 1.0),
                        adsk.core.Point3D.create(*pivot))
        return m

    base = rot((10.0, 0.0, 0.0))
    col = base.translation
    column = (round(col.x, 6) + 0.0, round(col.y, 6) + 0.0, round(col.z, 6) + 0.0)
    kept = spun(base, 10.0, 0.0, 0.0)
    zeroed = rot((10.0, 0.0, 0.0))
    zeroed.translation = adsk.core.Vector3D.create(0.0, 0.0, 0.0)
    world = spun(zeroed, 10.0, 0.0, 0.0)
    shifted = rot((10.0, 0.0, 0.0))
    shifted.translation = adsk.core.Vector3D.create(5.0, 0.0, 0.0)
    world_shift = spun(shifted, 10.0, 0.0, 0.0)
    composed = rot((10.0, 0.0, 0.0))
    tm = adsk.core.Matrix3D.create()
    tm.translation = adsk.core.Vector3D.create(5.0, 0.0, 0.0)
    composed.transformBy(tm)
    kept_shift = spun(composed, 10.0, 0.0, 0.0)
    twice = rot((0.0, 0.0, 0.0))
    twice.transformBy(rot((0.0, 0.0, 0.0)))
    accum = spun(twice, 1.0, 0.0, 0.0)
    emit(column == (10.0, -10.0, 0.0) and kept == (10.0, 0.0, 0.0)
         and world == (0.0, 10.0, 0.0) and world_shift == (5.0, 10.0, 0.0)
         and kept_shift == (15.0, 0.0, 0.0) and accum == (-1.0, 0.0, 0.0),
         "matrix3d-setrotation-bakes-the-pivot-into-the-translation-column: column=" + str(column)
         + " expect (10,-10,0) | pivot maps to " + str(kept) + " expect (10,0,0)"
         + " | translation:=(0,0,0) -> " + str(world) + " expect (0,10,0)"
         + " | translation:=(5,0,0) -> " + str(world_shift) + " expect (5,10,0)"
         + " | transformBy(translate 5) -> " + str(kept_shift) + " expect (15,0,0)"
         + " | two 90 deg rotations send +X to " + str(accum) + " expect (-1,0,0)")
""",
    },
    {
        "id": "occurrence-plain-reads-referenced-false-empty-collections",
        "claim": ("A plain LOCAL occurrence answers isReferencedComponent False and EMPTY joints "
                  "and bRepBodies collections - absence is not a live state for any of the three, "
                  "so a fake that drops the member teaches an API shape Fusion never presents"),
        "encoded_in": "tests/fakes/design.py FakeOccurrence joints/bRepBodies/isReferencedComponent",
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        d = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        occ = d.rootComponent.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        referenced = occ.isReferencedComponent
        n_joints = occ.joints.count
        n_bodies = occ.bRepBodies.count
        emit(referenced is False and n_joints == 0 and n_bodies == 0,
             "occurrence-plain-reads-referenced-false-empty-collections: isReferencedComponent="
             + str(referenced) + " joints.count=" + str(n_joints)
             + " bRepBodies.count=" + str(n_bodies) + " (expect False/0/0)")
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "occurrence-plain-reads-valid-and-lit",
        "claim": "A plain LOCAL occurrence made by addNewComponent, before anything is modelled in it, answers isValid True, isLightBulbOn True, isIsolated False, isVisible True and isReferencedComponent False - a fresh instance is live, lit, un-isolated and visible, so none of those flags has an unset or declining state a fake may model, and isIsolated in particular answers the bool False rather than nothing. Its boundingBox2 asked for solid bodies answers NOTHING (None, not an empty box) while the component holds no body, and answers a BoundingBox3D once one extrude lands - the 1 cm cube's box, min (0,0,0) to max (1,1,1). boundingBox2 takes a BITWISE BoundingBoxEntityTypes value, not a list, and the row reads both moments on the SAME occurrence so the None is the bodyless state rather than a different object",
        "encoded_in": ("tests/fakes/design.py FakeOccurrence - its valid, light_bulb_on and isolated "
                       "knobs (installed as plain isValid/isLightBulbOn/isIsolated reads) and its "
                       "bodies_bounding_box knob, whose None stands for the read that answers "
                       "nothing on an instance placing no body"),
        "facts_on_pass": {"behavior.occurrence_plain_is_valid": True,
                          "behavior.occurrence_plain_is_lit": True},
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        d = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        solid = adsk.fusion.BoundingBoxEntityTypes.SolidBRepBodyBoundingBoxEntityType
        occ = d.rootComponent.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        valid = occ.isValid
        lit = occ.isLightBulbOn
        isolated = occ.isIsolated
        visible = occ.isVisible
        referenced = occ.isReferencedComponent
        empty_box = occ.boundingBox2(solid)
        comp = occ.component
        sk = comp.sketches.add(comp.xYConstructionPlane)
        sk.sketchCurves.sketchLines.addTwoPointRectangle(
            adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(1.0, 1.0, 0.0))
        comp.features.extrudeFeatures.addSimple(
            sk.profiles.item(0), adsk.core.ValueInput.createByReal(1.0),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        box = occ.boundingBox2(solid)
        kind = type(box).__name__
        corners = None if box is None else (box.minPoint.x, box.minPoint.y, box.minPoint.z,
                                            box.maxPoint.x, box.maxPoint.y, box.maxPoint.z)
        emit(valid is True and lit is True and isolated is False and visible is True
             and referenced is False and empty_box is None
             and kind == "BoundingBox3D" and corners == (0.0, 0.0, 0.0, 1.0, 1.0, 1.0),
             "occurrence-plain-reads-valid-and-lit: a fresh local occurrence reads isValid="
             + str(valid) + " isLightBulbOn=" + str(lit) + " isIsolated=" + str(isolated)
             + " isVisible=" + str(visible) + " isReferencedComponent="
             + str(referenced)
             + " (expect True/True/False/True/False); boundingBox2(solid bodies) answers "
             + repr(empty_box) + " while it places no body and " + kind + " once one extrude "
             "lands, cornered " + str(corners) + " (expect None then 0,0,0 to 1,1,1)")
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "closed-document-wrapper-reads",
        "claim": ("Two never-saved documents both answer name 'Untitled' (nothing disambiguates "
                  "them), their root components answer BYTE-IDENTICAL entityTokens, and Document "
                  "itself exposes no entityToken; a CLOSED document's held wrapper still reads "
                  "isValid False and compares == False against a live document without raising - "
                  "the exact reads _write_guard.document_key prunes and matches by"),
        "encoded_in": ("_write_guard.py document_key / prune_closed_documents docstrings; "
                       "tests/unit/test_view_set.py _DocWrapper + _SHARED_ROOT_TOKEN; "
                       "cam_generate.py same-name wire note"),
        "body": """
    DT = adsk.core.DocumentTypes.FusionDesignDocumentType
    d1 = app.documents.add(DT)
    d2 = app.documents.add(DT)
    try:
        n1, n2 = d1.name, d2.name
        same_name = (n1 == n2)
        untitled = (n1 == "Untitled")
        t1 = adsk.fusion.FusionDocument.cast(d1).design.rootComponent.entityToken
        t2 = adsk.fusion.FusionDocument.cast(d2).design.rootComponent.entityToken
        no_ent = not hasattr(d1, "entityToken")
        d1.close(False)
        v = d1.isValid
        eq = (d1 == app.activeDocument)
        emit(same_name and untitled and t1 == t2 and no_ent and v is False and eq is False,
             "closed-document-wrapper-reads: names=" + repr(n1) + "/" + repr(n2)
             + " names_same=" + str(same_name)
             + " root_tokens_identical=" + str(t1 == t2) + " token=" + repr(t1)
             + " document_has_entityToken=" + str(not no_ent)
             + " closed_isValid=" + repr(v) + " closed_eq_active=" + repr(eq))
    finally:
        try:
            d2.close(False)
        except Exception:
            pass
""",
    },
    {
        "id": "closed-document-name-raises",
        "claim": ("Reading .name on a CLOSED document's held wrapper raises RuntimeError "
                  "'An API Object refers to a deleted Object'. This row catches the raise and "
                  "gates on its message; the expect also accepts a script-level abort, so a PASS "
                  "does not say which of the two the run saw"),
        "encoded_in": ("_write_guard.py name-read comment in document_key; "
                       "tests/unit/test_view_set.py _DocWrapper raises contract"),
        "expect": "raise_or_abort",
        "body": """
    DT = adsk.core.DocumentTypes.FusionDesignDocumentType
    d1 = app.documents.add(DT)
    d1.close(False)
    try:
        n = d1.name
        emit(False, "closed-document-name-raises: answered " + repr(n) + " with no raise")
    except Exception as e:
        emit(type(e).__name__ == "RuntimeError" and "deleted Object" in str(e),
             "closed-document-name-raises: raised catchably " + type(e).__name__
             + ": " + str(e)[:60])
""",
    },
    {
        "id": "sketchtext-heightparameter-settable-geometry-follows",
        "claim": ("SketchText.heightParameter.value accepts a WRITE (0.8 set to 0.4 reads back "
                  "0.4) and the glyph geometry FOLLOWS proportionally (the boundingBox width "
                  "halves with the height) - so an edit-path resize / fit-to-width is "
                  "measure-and-rescale on the live text, not a delete-and-recreate"),
        "encoded_in": ("sketch_set_text's edit path (the _CREATE_ONLY contract this measurement "
                       "unblocks rewording); _sketch_detail's heightParameter read"),
        "body": """
    root = des.rootComponent
    sk = root.sketches.add(root.xYConstructionPlane)
    ipt = sk.sketchTexts.createInput2("Fit Me", 0.8)
    placed = ipt.setAsMultiLine(
        adsk.core.Point3D.create(0.0, 0.0, 0.0),
        adsk.core.Point3D.create(4.8, 0.8, 0.0),
        adsk.core.HorizontalAlignments.LeftHorizontalAlignment,
        adsk.core.VerticalAlignments.BottomVerticalAlignment, 0)
    st = sk.sketchTexts.add(ipt)
    w_before = st.boundingBox.maxPoint.x - st.boundingBox.minPoint.x
    st.heightParameter.value = 0.4
    v = st.heightParameter.value
    w_after = st.boundingBox.maxPoint.x - st.boundingBox.minPoint.x
    ratio = (w_after / w_before) if w_before else 0.0
    emit(bool(placed) and abs(v - 0.4) < 1e-9 and abs(ratio - 0.5) < 0.02,
         "sketchtext-heightparameter-settable-geometry-follows: height 0.8->set 0.4 reads "
         + str(v) + " width " + str(round(w_before, 4)) + "->" + str(round(w_after, 4))
         + " ratio " + str(round(ratio, 4)) + " (expect ~0.5)")
""",
    },
    {
        "id": "stl-export-unittype-is-sticky-session-state",
        "claim": ("The unit an STL export lands in is STICKY SESSION STATE, not a property of the "
                  "document and not readable anywhere: an export that leaves unitType UNTOUCHED "
                  "writes the unit of the LAST EXPLICIT unitType assignment made in the Fusion "
                  "session, and that carries ACROSS DOCUMENTS - a BRAND-NEW document, exported "
                  "untouched, writes the unit an export in another document assigned. unitType "
                  "reads 0 BEFORE every assignment regardless of what the file will be written in "
                  "- the new document's own fresh options object included - so no read of a fresh "
                  "options object names the unit. The read-BACK after an assignment is a different "
                  "read and it DOES return the assigned member (inch reads 3, cm reads 1), which "
                  "is what makes applied_pair's post-read gate able to confirm a non-mm request - "
                  "only the mm member collides with the factory 0. Measured on a 1 cm cube: "
                  "explicit mm -> 10.0, untouched after it -> 10.0, explicit inch -> 0.393701, "
                  "untouched after it -> 0.393701, a NEW DOCUMENT untouched after it -> 0.393701, "
                  "explicit cm -> 1.0. The mm and inch files are the same BYTE LENGTH (a binary "
                  "STL of a fixed triangle count always is), so a size comparison cannot tell them "
                  "apart, while EVERY vertex coordinate differs by 25.4x. ONE clause here is an "
                  "OBSERVATION rather than a measurement, because a script cannot restart Fusion "
                  "to test it - the POST-RESTART default: the value read mm on the first export "
                  "after a restart, which is how this masquerades as 'the document's units' or as "
                  "a fixed inch default depending on what ran earlier in the session. "
                  "Consequence: a writer that does not SET unitType inherits the unit from an "
                  "unrelated earlier export"),
        "encoded_in": ("_export.STL_UNIT_MEMBERS / stl_unit_enum and every STL writer that bakes "
                       "unitType; mesh_export's stl_units note"),
        "need_box": True,
        "body": """
    import struct, tempfile, os
    um = des.unitsManager
    em = des.exportManager
    mm = um.convert(1.0, "cm", "mm")
    inch = um.convert(1.0, "cm", "in")

    # Every leg SETS before it observes, so the row proves the stickiness rather than inheriting
    # it: a row that merely read the untouched export would report whatever an earlier export in
    # this Fusion session happened to leave behind, and would pass or fail by luck of ordering.
    # Both reads are captured because they answer DIFFERENT questions: the read on a fresh options
    # object never names the unit the file will get, while the read-BACK after an assignment does
    # return the member assigned - which is what lets applied_pair's post-read gate confirm a
    # non-mm request. Reporting only the first would read as a claim about both.
    backreads = []

    # (largest absolute vertex coordinate, byte length, every vertex coordinate) of a binary STL,
    # consumed and deleted.
    def read_stl(p):
        raw = open(p, "rb").read()
        os.remove(p)
        tris = struct.unpack("<I", raw[80:84])[0]
        coords = []
        pos = 84
        for _ in range(tris):
            vals = struct.unpack("<12fH", raw[pos:pos+50])
            pos += 50
            coords.extend(vals[3:12])       # the 9 vertex floats; vals[0:3] is the facet normal
        big = max(abs(v) for v in coords) if coords else 0.0
        return big, len(raw), coords

    def leg(label, unit_member):
        p = os.path.join(tempfile.gettempdir(), "measure_sticky_" + label + ".stl")
        o = em.createSTLExportOptions(body, p)
        try:
            r = o.unitType
        except Exception as exc:
            r = "unreadable(" + type(exc).__name__ + ")"
        if unit_member is not None:
            o.unitType = unit_member
            backreads.append((label, unit_member, safe_read(o)))
        em.execute(o)
        return (r,) + read_stl(p)

    def safe_read(o):
        try:
            return o.unitType
        except Exception as exc:
            return "unreadable(" + type(exc).__name__ + ")"

    U = adsk.fusion.DistanceUnits
    r1, set_mm, size_mm, coords_mm = leg("set-mm", U.MillimeterDistanceUnits)
    r2, after_mm, _s2, _c2 = leg("untouched-after-mm", None)
    r3, set_in, size_in, coords_in = leg("set-inch", U.InchDistanceUnits)
    r4, after_in, _s4, _c4 = leg("untouched-after-inch", None)
    # A BRAND-NEW document, exported UNTOUCHED: nothing in it has ever seen a unitType assignment,
    # so what it writes is what the SESSION carries - the inch leg above.
    doc2 = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        d2 = adsk.fusion.Design.cast(doc2.products.itemByProductType("DesignProductType"))
        b2 = make_box(d2, "StickyCarry")
        p2 = os.path.join(tempfile.gettempdir(), "measure_sticky_new_document.stl")
        o2 = d2.exportManager.createSTLExportOptions(b2, p2)
        r5 = safe_read(o2)
        wrote = d2.exportManager.execute(o2)
        carried, _s5, _c5 = read_stl(p2) if wrote else (0.0, 0, [])
    finally:
        doc2.close(False)
    r6, set_cm, _s6, _c6 = leg("set-cm", U.CentimeterDistanceUnits)
    # Left at mm deliberately: this row MUTATES session state every other STL export inherits, so
    # it restores the post-restart default rather than leaving the session on another unit.
    leg("restore-mm", U.MillimeterDistanceUnits)

    follows = abs(after_mm - mm) < 1e-3 and abs(after_in - inch) < 1e-3
    assigns = abs(set_mm - mm) < 1e-3 and abs(set_in - inch) < 1e-3
    # the box is a 1 cm cube, so a file written in cm carries 1.0 where the mm one carries 10.0
    assigns_cm = abs(set_cm - 1.0) < 1e-3
    carries = abs(carried - inch) < 1e-3
    reads_zero = r1 == 0 and r2 == 0 and r3 == 0 and r4 == 0 and r5 == 0 and r6 == 0
    # EVERY coordinate, not just the largest: the two files carry the same triangles in the same
    # order, so the pairs line up and a single scale factor either holds across all of them or not.
    per_coord = (len(coords_mm) == len(coords_in) and bool(coords_mm)
                 and all(abs(a - b * 25.4) < 1e-2 for a, b in zip(coords_mm, coords_in)))
    # The read-BACK returns what was assigned - the inch and cm legs are the ones that matter, since
    # mm collides with the factory 0 and so proves nothing on its own.
    backs_match = all(got == want for _lbl, want, got in backreads)
    inch_back = [got for lbl, _w, got in backreads if lbl == "set-inch"]
    cm_back = [got for lbl, _w, got in backreads if lbl == "set-cm"]
    emit(follows and assigns and assigns_cm and carries and reads_zero and backs_match
         and per_coord and size_mm == size_in,
         "stl-export-unittype-is-sticky-session-state: follows=" + str(follows)
         + " assigns=" + str(assigns) + " cm=" + str(assigns_cm)
         + " carries_into_a_new_document=" + str(carries)
         + " reads0=" + str(reads_zero) + " backs_match=" + str(backs_match)
         + " every_coord_25.4x=" + str(per_coord)
         + " same_byte_length=" + str(size_mm == size_in)
         + "; set_mm=" + str(round(set_mm, 6)) + " untouched_after_mm=" + str(round(after_mm, 6))
         + " (expect " + str(round(mm, 6)) + ")"
         + " set_inch=" + str(round(set_in, 6)) + " untouched_after_inch=" + str(round(after_in, 6))
         + " new_document_untouched=" + str(round(carried, 6))
         + " (expect " + str(round(inch, 6)) + ")"
         + " set_cm=" + str(round(set_cm, 6)) + " (expect 1.0)"
         + " reads_before_assign=" + str([r1, r2, r3, r4, r5, r6])
         + " inch_reads_back=" + str(inch_back) + " cm_reads_back=" + str(cm_back)
         + " bytes=" + str(size_mm))
""",
    },
    {
        "id": "stl-unittype-read-poisoned-by-units-toggle",
        "claim": ("Assigning Design.fusionUnitsManager.distanceDisplayUnits POISONS "
                  "STLExportOptions.unitType's READ for that document: the read then raises "
                  "RuntimeError '3 : unexpected document units' and does NOT recover when the "
                  "display units are put back to what they were. A document that was never "
                  "toggled reads 0. So unitType's read can fail outright, not merely mislead - "
                  "any code reading it needs safe(), and no writer may infer the file's unit "
                  "from it"),
        "encoded_in": ("_export.applied_pair's unitType handling and mesh_export's "
                       "_apply_stl_unit read-back"),
        "body": """
    # Its OWN document, closed at the end: the toggle is not reversible within a document, so
    # doing it in the runner's shared document would poison every later row that reads unitType.
    import tempfile, os
    doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        d2 = adsk.fusion.Design.cast(app.activeProduct)
        b = make_box(d2, "PoisonProbe")
        em = d2.exportManager
        path = os.path.join(tempfile.gettempdir(), "measure_poison.stl")
        before = em.createSTLExportOptions(b, path).unitType
        um = d2.fusionUnitsManager
        was = um.distanceDisplayUnits
        um.distanceDisplayUnits = adsk.fusion.DistanceUnits.InchDistanceUnits
        um.distanceDisplayUnits = was
        restored = um.distanceDisplayUnits == was
        try:
            after = em.createSTLExportOptions(b, path).unitType
            raised = ""
        except Exception as exc:
            after = None
            raised = type(exc).__name__ + ": " + str(exc)[:80]
        emit(before == 0 and after is None and "unexpected document units" in raised
             and restored,
             "stl-unittype-read-poisoned-by-units-toggle: read_before_toggle=" + str(before)
             + " (expect 0) display_units_restored=" + str(restored)
             + " read_after_restore=" + (repr(raised) if raised else str(after))
             + " (expect a raise naming unexpected document units)")
    finally:
        doc.close(False)
""",
    },
    {
        "id": "cam-machine-library-deleteasset",
        "claim": ("machineLibrary.importMachine stores a loaded machine into the Local location "
                  "under a new name and machineAtURL loads it back; the stored asset's "
                  "URL.leafName carries the FILE EXTENSION ('MeasureDeleteMe.mch') while the "
                  "machine's own catalog label - its description; Machine exposes NO 'name' "
                  "attribute at all (measured: reading .name raises AttributeError) - carries no "
                  "extension, the shape the delete matcher's stem compare exists for; "
                  "deleteAsset(url) then returns True and a re-walk of childAssetURLs no "
                  "longer lists the asset. Self-cleaning: the machine this row imports is the "
                  "one it deletes"),
        "encoded_in": ("cam_create_machine.py handler importMachine/machineAtURL gates + "
                       "cam_delete_machine.py handler deleteAsset read-backs"),
        "body": """
    libs = adsk.cam.CAMManager.get().libraryManager
    lib = libs.machineLibrary
    f360 = adsk.cam.LibraryLocations.Fusion360LibraryLocation
    local_loc = adsk.cam.LibraryLocations.LocalLibraryLocation
    local = lib.urlByLocation(local_loc)
    src = (lib.createQuery(f360, "", "").execute() or [None])[0]
    if src is None:
        emit(False, "cam-machine-library-deleteasset: no bundled machine to import - inconclusive")
        return
    url = lib.importMachine(src, local, "MeasureDeleteMe")
    stored = bool(url)
    leaf = url.leafName if stored else ""
    # startswith/endswith, not equality: the library DEDUPES a colliding import name (measured:
    # with a stray asset already wearing the name, this import lands as 'MeasureDeleteMe 3.mch'),
    # and the claim is the extension contrast, not the exact stem.
    leaf_has_ext = leaf.startswith("MeasureDeleteMe") and leaf.endswith(".mch")
    back = lib.machineAtURL(url) if stored else None
    loaded = back is not None
    # The machine's DESCRIPTION is the catalog label read for the no-extension contrast - Machine
    # exposes no 'name' attribute at all (measured: reading .name raises AttributeError), so the
    # label the delete matcher's stem compare runs against is the description.
    desc = back.description if loaded else ""
    desc_has_ext = desc.endswith(".mch")
    ok = stored and lib.deleteAsset(url)
    still = stored and any(
        u.leafName == leaf for u in lib.childAssetURLs(local))
    emit(stored and leaf_has_ext and loaded and desc != "" and not desc_has_ext
         and desc != leaf and ok is True and not still,
         "cam-machine-library-deleteasset: stored=" + str(stored) + " leafName=" + repr(leaf)
         + " (expect the .mch extension) machine.description=" + repr(desc)
         + " description_has_extension=" + str(desc_has_ext) + " loaded_back=" + str(loaded)
         + " deleteAsset=" + str(ok) + " still_listed=" + str(still))
""",
    },
    {
        "id": "cam-machine-query-keyed-on-model",
        "claim": ("The machine-library query is keyed on vendor/model and CANNOT reach a stored "
                  "machine by a description that is not its model - query by the description "
                  "returns 0 hits while query by the model returns the machine and the full local "
                  "walk contains it. A post-delete re-resolve by such a label therefore proves "
                  "nothing; the asset walk is the load-bearing read-back. Self-cleaning: the "
                  "machine this row imports is the one it deletes"),
        "encoded_in": ("cam_delete_machine.py handler's no-evidence sentence and point-of-use "
                       "comment; cam_create_machine.py's create side writing the name onto model"),
        "body": """
    libs = adsk.cam.CAMManager.get().libraryManager
    lib = libs.machineLibrary
    f360 = adsk.cam.LibraryLocations.Fusion360LibraryLocation
    local_loc = adsk.cam.LibraryLocations.LocalLibraryLocation
    local = lib.urlByLocation(local_loc)
    src = (lib.createQuery(f360, "", "").execute() or [None])[0]
    if src is None:
        emit(False, "cam-machine-query-keyed-on-model: no bundled machine - inconclusive")
        return
    src.description = "MeasureQKey"
    # model/vendor left as the source's: the description is deliberately NOT the model
    url = lib.importMachine(src, local, "MeasureQKey")
    try:
        stored = lib.machineAtURL(url)
        by_desc = len(lib.createQuery(local_loc, "", "MeasureQKey").execute() or [])
        by_model = lib.createQuery(local_loc, "", stored.model).execute() or []
        model_reaches = any(m.description == "MeasureQKey" for m in by_model)
        walk_has = any(m.description == "MeasureQKey"
                       for m in (lib.createQuery(local_loc, "", "").execute() or []))
        emit(by_desc == 0 and model_reaches and walk_has,
             "cam-machine-query-keyed-on-model: by_description_hits=" + str(by_desc)
             + " (expect 0) by_model_reaches=" + str(model_reaches)
             + " full_walk_contains=" + str(walk_has))
    finally:
        lib.deleteAsset(url)
""",
    },
    {
        "id": "construction-offset-value-reads-signed-cm",
        "claim": ("A ConstructionPlaneOffsetDefinition's offset ModelParameter .value reads the "
                  "requested offset in SIGNED internal cm (-12 mm requested reads -1.2) - the "
                  "read a construction-offset read-back compare gates on"),
        "encoded_in": ("model_construction's offset read-back compare; the MODEL-2 measurement "
                       "trail (the on_path form stores distance and offset as separate "
                       "parameters)"),
        "body": """
    root = des.rootComponent
    pi = root.constructionPlanes.createInput()
    ok_set = pi.setByOffset(root.xYConstructionPlane, adsk.core.ValueInput.createByReal(-1.2))
    plane = root.constructionPlanes.add(pi)
    d = plane.definition
    v = d.offset.value
    emit(bool(ok_set) and type(d).__name__ == "ConstructionPlaneOffsetDefinition"
         and abs(v + 1.2) < 1e-9,
         "construction-offset-value-reads-signed-cm: definition=" + type(d).__name__
         + " offset.value=" + repr(v) + " (expect -1.2 for a -12 mm request)")
""",
    },
    {
        "id": "shape-dump-torus",
        "claim": "core.Torus - the surface a toroidal BRepFace carries, measured on the fillet of a cylinder's circular edge - exposes origin (the torus CENTRE: a torus created centred at (2, 3, -1) reads that point back), axis, majorRadius, minorRadius and copy(). origin is the point the torus keypoint gate lifts into world and compares a JointGeometry against: it transforms a COPY of that point, and the live surface's own origin reads unchanged after that transform",
        "encoded_in": "tests/unit/test_joint_at_geometry.py's torus-face fakes (face.geometry.origin); _joints.py's torus keypoint gate; _holder.py's Torus.cast reads",
        "body": """
    # Centred AWAY from the world origin: a torus created at (0,0,0) reads (0,0,0) back under any
    # origin convention, so that rig cannot tell a centre from anything else.
    t = adsk.core.Torus.create(
        adsk.core.Point3D.create(2.0, 3.0, -1.0), adsk.core.Vector3D.create(0.0, 0.0, 1.0),
        1.0, 0.25)
    n = dump_shape("Torus", t)
    names = [x for x in dir(t) if not x.startswith("_")]
    members = ("origin" in names and "axis" in names and "majorRadius" in names
               and "minorRadius" in names and "copy" in names)
    o = t.origin
    at_centre = (abs(o.x - 2.0) < 1e-9 and abs(o.y - 3.0) < 1e-9 and abs(o.z + 1.0) < 1e-9)
    # The keypoint gate lifts a COPY of that point into world; the surface must read the same
    # centre afterwards, which is what makes the lift safe to take on a live face.
    m = adsk.core.Matrix3D.create()
    m.translation = adsk.core.Vector3D.create(5.0, 0.0, 0.0)
    moved = t.origin.copy()
    moved_ok = bool(moved.transformBy(m)) and abs(moved.x - 7.0) < 1e-9
    after = t.origin
    surface_still = (abs(after.x - 2.0) < 1e-9 and abs(after.y - 3.0) < 1e-9
                     and abs(after.z + 1.0) < 1e-9)
    # A REAL toroidal face: a constant-radius fillet on a cylinder's circular edge, in its own
    # document so the shared scratch keeps the geometry the other rows measure.
    face_types = []
    carried = False
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        d = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        r2 = d.rootComponent
        sk = r2.sketches.add(r2.xYConstructionPlane)
        sk.sketchCurves.sketchCircles.addByCenterRadius(
            adsk.core.Point3D.create(0.0, 0.0, 0.0), 1.0)
        cyl = r2.features.extrudeFeatures.addSimple(
            sk.profiles.item(0), adsk.core.ValueInput.createByReal(1.0),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation).bodies.item(0)
        bname = cyl.name
        seed = adsk.core.ObjectCollection.create()
        for i in range(cyl.edges.count):
            if type(cyl.edges.item(i).geometry).__name__ == "Circle3D":
                seed.add(cyl.edges.item(i))
                break
        kinds = set()
        if seed.count:
            fi = r2.features.filletFeatures.createInput()
            fi.addConstantRadiusEdgeSet(seed, adsk.core.ValueInput.createByReal(0.2), False)
            r2.features.filletFeatures.add(fi)
            filleted = r2.bRepBodies.itemByName(bname)   # the fillet re-issues the body
            for i in range(filleted.faces.count if filleted is not None else 0):
                kinds.add(type(filleted.faces.item(i).geometry).__name__)
        face_types = sorted(kinds)
        carried = "Torus" in kinds
    finally:
        tmp.close(False)
    emit(n > 0 and members and at_centre and moved_ok and surface_still and carried,
         "shape-dump-torus: " + str(n) + " attrs origin=" + repr((o.x, o.y, o.z))
         + " (created at (2.0, 3.0, -1.0)) major=" + repr(t.majorRadius)
         + " minor=" + repr(t.minorRadius) + " origin-copy transformed to x="
         + repr(round(moved.x, 6)) + " surface origin still "
         + repr((after.x, after.y, after.z))
         + " filleted-cylinder face geometry types=" + str(face_types))
""",
    },
    {
        "id": "shape-dump-infinite-line-and-sphere",
        "claim": "core.InfiniteLine3D and core.Sphere - two transient geometry types created off their own static factory with no document open. InfiniteLine3D.create(origin, direction) carries origin, direction and isColinearTo (true for a second line on the same axis, false for one offset from it - the test the holder profile reduction runs); Sphere.create(origin, radius) carries origin (the sphere CENTRE) and radius",
        "encoded_in": "tests/fakes/geometry.py FakeInfiniteLine3D (test__holder.py, test_assembly_move.py, test_joint_create_origin.py, and the Line3D fake's asInfiniteLine) and Sphere (test__sys_common.py's sphere-face record, which reads the TYPE NAME only) - this dump is the SHAPES key test_fake_shapes_exist.py sweeps both against",
        "body": """
    line = adsk.core.InfiniteLine3D.create(
        adsk.core.Point3D.create(1.0, 2.0, 3.0), adsk.core.Vector3D.create(0.0, 0.0, 1.0))
    n_line = dump_shape("InfiniteLine3D", line)
    # A second line ON the axis and one offset from it: an isColinearTo that answered the same for
    # both would pass a rig built from one line alone.
    same = adsk.core.InfiniteLine3D.create(
        adsk.core.Point3D.create(1.0, 2.0, 9.0), adsk.core.Vector3D.create(0.0, 0.0, 1.0))
    apart = adsk.core.InfiniteLine3D.create(
        adsk.core.Point3D.create(4.0, 2.0, 3.0), adsk.core.Vector3D.create(0.0, 0.0, 1.0))
    colinear = (line.isColinearTo(same), line.isColinearTo(apart))
    # Centred away from the world origin: a sphere created at (0,0,0) reads (0,0,0) back under any
    # convention, so that rig cannot tell a centre from anything else.
    sph = adsk.core.Sphere.create(adsk.core.Point3D.create(2.0, 3.0, -1.0), 0.5)
    n_sph = dump_shape("Sphere", sph)
    o = sph.origin
    at_centre = (abs(o.x - 2.0) < 1e-9 and abs(o.y - 3.0) < 1e-9 and abs(o.z + 1.0) < 1e-9)
    emit(n_line > 0 and n_sph > 0 and colinear == (True, False)
         and at_centre and abs(sph.radius - 0.5) < 1e-9,
         "shape-dump-infinite-line-and-sphere: InfiniteLine3D " + str(n_line)
         + " attrs isColinearTo(on-axis, offset)=" + repr(colinear) + "; Sphere " + str(n_sph)
         + " attrs origin=" + repr((o.x, o.y, o.z)) + " (created at (2.0, 3.0, -1.0)) radius="
         + repr(sph.radius))
""",
    },
    {
        "id": "shape-dump-ellipse3d",
        "claim": "core.Ellipse3D - the curve an extruded ellipse's two elliptical BRepEdges carry (curveType Ellipse3DCurveType) - exposes center, majorRadius and minorRadius, reading back the 2.0 x 1.0 cm ellipse the sketch was drawn at",
        "encoded_in": "find_geometry.py's ellipse_edge record (major_radius/minor_radius/center reads)",
        "body": """
    # A REAL elliptical edge, in its own document so the shared scratch keeps the geometry the
    # other rows measure. A transient factory would not prove what a BRepEdge hands back.
    kinds = []
    n = 0
    members = False
    major = None
    minor = None
    centred = False
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        d = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        r2 = d.rootComponent
        sk = r2.sketches.add(r2.xYConstructionPlane)
        sk.sketchCurves.sketchEllipses.add(
            adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(2.0, 0.0, 0.0),
            adsk.core.Point3D.create(0.0, 1.0, 0.0))
        solid = r2.features.extrudeFeatures.addSimple(
            sk.profiles.item(0), adsk.core.ValueInput.createByReal(1.0),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation).bodies.item(0)
        ell = None
        seen = set()
        for i in range(solid.edges.count):
            g = solid.edges.item(i).geometry
            seen.add(type(g).__name__ + ":" + str(g.curveType))
            if g.curveType == adsk.core.Curve3DTypes.Ellipse3DCurveType and ell is None:
                ell = g
        kinds = sorted(seen)
        if ell is not None:
            n = dump_shape("Ellipse3D", ell)
            names = [x for x in dir(ell) if not x.startswith("_")]
            members = ("center" in names and "majorRadius" in names and "minorRadius" in names)
            if members:
                major = ell.majorRadius
                minor = ell.minorRadius
                c = ell.center
                centred = abs(c.x) < 1e-9 and abs(c.y) < 1e-9 and abs(c.z) < 1e-9
    finally:
        tmp.close(False)
    emit(n > 0 and members and centred
         and abs(major - 2.0) < 1e-6 and abs(minor - 1.0) < 1e-6,
         "shape-dump-ellipse3d: " + str(n) + " attrs majorRadius=" + repr(major)
         + " minorRadius=" + repr(minor) + " (drawn 2.0 x 1.0 cm) centre-at-origin="
         + repr(centred) + " edge curve types=" + str(kinds))
""",
    },
    {
        "id": "fillet-tangent-chain-loop-faces",
        "claim": "A fillet driven from ONE edge of an 8-edge tangent top loop (4 lines + 4 arcs, isTangentChain=True) lands FilletFeature.faces.count == 8 - the chain expands across every tangent neighbour, so the number of edges HANDED IN predicts nothing about what got filleted",
        "encoded_in": "_edge_common.py's tangent-chain wording and its off-the-feature face read-back",
        "need_box": True,
        "body": """
    root = des.rootComponent
    bname = body.name
    verticals = adsk.core.ObjectCollection.create()
    for i in range(body.edges.count):
        e = body.edges.item(i)
        g = e.geometry
        if type(g).__name__ != "Line3D":
            continue
        d = g.startPoint.vectorTo(g.endPoint)
        if abs(d.x) < 1e-9 and abs(d.y) < 1e-9 and abs(d.z) > 1e-9:
            verticals.add(e)
    fi = root.features.filletFeatures.createInput()
    fi.addConstantRadiusEdgeSet(verticals, adsk.core.ValueInput.createByReal(0.2), False)
    root.features.filletFeatures.add(fi)
    b = root.bRepBodies.itemByName(bname)
    top = None
    for i in range(b.faces.count):
        f = b.faces.item(i)
        if type(f.geometry).__name__ == "Plane" and f.geometry.normal.z > 0.9:
            top = f
    loop_edges = top.edges.count
    seed = adsk.core.ObjectCollection.create()
    seed.add(top.edges.item(0))
    fi2 = root.features.filletFeatures.createInput()
    fi2.addConstantRadiusEdgeSet(seed, adsk.core.ValueInput.createByReal(0.1), True)
    feat = root.features.filletFeatures.add(fi2)
    emit(loop_edges == 8 and feat.faces.count == 8,
         "fillet-tangent-chain-loop-faces: verticals=" + str(verticals.count)
         + " top loop edges=" + str(loop_edges)
         + " seeded=1 fillet faces=" + str(feat.faces.count))
""",
    },
    {
        "id": "fillet-feature-has-no-edges",
        "claim": "FilletFeature exposes NO 'edges' member: it is absent from dir() and reading it raises AttributeError, so the edges a fillet consumed cannot be read back off the feature - only its faces can",
        "encoded_in": "_edge_common.py's face-based read-back (the reason a fillet payload never names the filleted edges)",
        "need_box": True,
        "facts_on_pass": {"behavior.fillet_feature_has_edges": False},
        "body": """
    root = des.rootComponent
    seed = adsk.core.ObjectCollection.create()
    seed.add(body.edges.item(0))
    fi = root.features.filletFeatures.createInput()
    fi.addConstantRadiusEdgeSet(seed, adsk.core.ValueInput.createByReal(0.1), False)
    feat = root.features.filletFeatures.add(fi)
    listed = "edges" in [n for n in dir(feat) if not n.startswith("_")]
    try:
        feat.edges
        read = "no raise"
    except AttributeError:
        read = "AttributeError"
    except Exception as e:
        read = type(e).__name__
    emit((not listed) and read == "AttributeError",
         "fillet-feature-has-no-edges: in_dir=" + str(listed) + " read=" + read
         + " faces=" + str(feat.faces.count))
""",
    },
    {
        "id": "thread-designation-multi-type-identity",
        "claim": "A thread DESIGNATION carried by several thread types is the SAME thread in each: M5x0.8, M10x1.5 and M6x1 are each carried by three metric types (ANSI Metric M Profile / GB Metric profile / ISO Metric profile) and every geometric scalar - majorDiameter, minorDiameter, pitchDiameter, threadPitch, threadAngle - is equal across the carriers at a shared class, differing only in threadType itself; 1/4-20 UNC has exactly ONE carrier. This is what makes _threads.resolve_thread_info's library-order first pick safe instead of an ambiguity it must refuse",
        "encoded_in": "_threads.py resolve_thread_info's first-pick comment; model_hole tap / model_thread wire prose; tests/unit/test_model_thread.py's thread-table fakes",
        "facts_on_pass": {"behavior.thread_same_designation_types_identical": True},
        "body": """
    tf = des.rootComponent.features.threadFeatures
    tdq = tf.threadDataQuery

    def carriers(desig):
        hits = []
        for t in tdq.allThreadTypes:
            for s in tdq.allSizes(t):
                if desig in tdq.allDesignations(t, s):
                    hits.append(t)
                    break
        return hits

    def scalars(ti):
        return (round(ti.majorDiameter, 6), round(ti.minorDiameter, 6),
                round(ti.pitchDiameter, 6), round(ti.threadPitch, 6),
                round(ti.threadAngle, 6))

    # The three library thread types the claim names, compared as VALUES rather than counted: a
    # different trio of three carriers would pass a count on its own.
    WANT = set(("ANSI Metric M Profile", "GB Metric profile", "ISO Metric profile"))
    same = True
    named_trio = True
    seen_types = set()
    detail = []
    for desig in ("M5x0.8", "M10x1.5", "M6x1"):
        hits = carriers(desig)
        seen_types.update(hits)
        if set(hits) != WANT:
            named_trio = False
        shared = set()
        for i, t in enumerate(hits):
            cls = set(tdq.allClasses(False, t, desig))
            shared = cls if i == 0 else (shared & cls)
        if len(hits) != 3 or not shared:
            same = False
            detail.append(desig + ": " + str(len(hits)) + " types, shared classes "
                          + str(len(shared)))
            continue
        pick = sorted(shared)[0]
        vals = set()
        for t in hits:
            vals.add(scalars(tf.createThreadInfo(False, t, desig, pick)))
        same = same and len(vals) == 1
        detail.append(desig + ": 3 types class=" + pick + " distinct scalar sets="
                      + str(len(vals)))
    unc = carriers("1/4-20 UNC")
    emit(same and named_trio and len(unc) == 1,
         "thread-designation-multi-type-identity: " + "; ".join(detail)
         + "; named_trio=" + str(named_trio) + " carriers=" + "|".join(sorted(seen_types))
         + "; 1/4-20 UNC carriers=" + str(len(unc)))
""",
    },
    {
        "id": "min-distance-parallel-planes-is-plane-separation",
        "claim": ("measureMinimumDistance between two PARALLEL PLANAR faces that do not overlap "
                  "laterally returns the separation between their PLANES, not the minimum between "
                  "the bounded faces (2.0 cm reported where the faces' nearest points are "
                  "sqrt(13) cm apart); a Point3D lying on a planar face's plane but outside its "
                  "boundary reads 0.0 from that face; a PERPENDICULAR pair and a COPLANAR pair "
                  "both return the bounded minimum. Plus the two co-space facts the correction "
                  "rests on: a face's boundingBox brackets its own plane origin along the normal "
                  "axis, and that holds for assembly-context PROXY faces of DIFFERENT occurrences, "
                  "whose plane separation reproduces the API's own number"),
        "encoded_in": ("_geom.py parallel_plane_facts (the bounded-gap measure and its read-back "
                       "gate) and _comparable_boxes (the one-space precondition); test__geom.py "
                       "TestParallelPlaneFacts + TestBoxComparabilityPrecondition; the "
                       "parallel-pair tests in test_model_measure_between.py and "
                       "test_model_measure_relation.py"),
        "body": """
    root = des.rootComponent
    mm = app.measureManager

    def plate(x0, x1, height, name):
        sk = root.sketches.add(root.xYConstructionPlane)
        sk.sketchCurves.sketchLines.addTwoPointRectangle(
            adsk.core.Point3D.create(x0, 0.0, 0.0), adsk.core.Point3D.create(x1, 1.0, 0.0))
        ext = root.features.extrudeFeatures.addSimple(
            sk.profiles.item(0), adsk.core.ValueInput.createByReal(height),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        b = ext.bodies.item(0)
        b.name = name
        return b

    def cube(comp, size):
        sk = comp.sketches.add(comp.xYConstructionPlane)
        sk.sketchCurves.sketchLines.addTwoPointRectangle(
            adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(size, size, 0.0))
        comp.features.extrudeFeatures.addSimple(
            sk.profiles.item(0), adsk.core.ValueInput.createByReal(size),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation)

    def face_where(body, axis, coord):
        # Sign-agnostic, like lowest_planar and brackets below. Measured on 2705.1.4 on this rig:
        # the four SIDE faces read OUTWARD normals (x=4 reads -1, x=5 reads +1), while the two
        # faces parallel to the originating SKETCH PLANE both read that plane's normal (0,0,+1),
        # the bottom one included. Only the AXIS the normal runs along holds in both cases. The
        # plane origin's coordinate on that axis is what picks between the two parallel faces,
        # since every point of a plane x = k has x = k; the sign never carried the selection.
        for i in range(body.faces.count):
            f = body.faces.item(i)
            g = f.geometry
            if g.surfaceType != adsk.core.SurfaceTypes.PlaneSurfaceType:
                continue
            if abs(abs(getattr(g.normal, axis)) - 1.0) < 1e-9 and abs(getattr(g.origin, axis) - coord) < 1e-9:
                return f
        return None

    def lowest_planar(body):
        pick = None
        for i in range(body.faces.count):
            f = body.faces.item(i)
            g = f.geometry
            if g.surfaceType != adsk.core.SurfaceTypes.PlaneSurfaceType or abs(g.normal.z) < 0.5:
                continue
            if pick is None or g.origin.z < pick.geometry.origin.z:
                pick = f
        return pick

    def brackets(face):
        # The co-space test the bounded measure needs: a face's own box must contain its own plane
        # origin along the normal axis, or the two reads are not in one frame.
        g = face.geometry
        bb = face.boundingBox
        axis = "x" if abs(g.normal.x) > 0.5 else ("y" if abs(g.normal.y) > 0.5 else "z")
        lo = getattr(bb.minPoint, axis)
        hi = getattr(bb.maxPoint, axis)
        o = getattr(g.origin, axis)
        return lo - 1e-6 <= o <= hi + 1e-6

    # Plate A spans x 0..1, z 0..1; B spans x 4..5, z 0..3; C spans x 8..9, z 0..1. A's and B's TOP
    # faces sit on parallel planes 2 cm apart and 3 cm apart along x, so the bounded minimum is
    # sqrt(3^2 + 2^2) = 3.6056. A's and C's tops are COPLANAR 7 cm apart - the case the API gets
    # RIGHT, so a probe of coplanar faces alone REFUTES this defect.
    a = plate(0.0, 1.0, 1.0, "MeasPlateA")
    b = plate(4.0, 5.0, 3.0, "MeasPlateB")
    c = plate(8.0, 9.0, 1.0, "MeasPlateC")
    top_a = face_where(a, "z", 1.0)
    top_b = face_where(b, "z", 3.0)
    top_c = face_where(c, "z", 1.0)
    side_b = face_where(b, "x", 4.0)
    if top_a is None or top_b is None or top_c is None or side_b is None:
        emit(False, "face pick failed: a=" + repr(top_a is not None) + " b=" + repr(top_b is not None)
             + " c=" + repr(top_c is not None) + " side=" + repr(side_b is not None))
    else:
        par = mm.measureMinimumDistance(top_a, top_b).value
        perp = mm.measureMinimumDistance(top_a, side_b).value
        cop = mm.measureMinimumDistance(top_a, top_c).value
        pt = mm.measureMinimumDistance(adsk.core.Point3D.create(10.0, 0.5, 1.0), top_a).value
        emit(abs(par - 2.0) < 1e-6, "parallel pair: API " + str(par) + " = plane separation 2.0, "
             "bounded minimum " + str((13.0) ** 0.5))
        emit(abs(perp - 3.0) < 1e-6, "perpendicular pair: API " + str(perp) + " = bounded (3.0)")
        emit(abs(cop - 7.0) < 1e-6, "COPLANAR pair: API " + str(cop) + " = bounded (7.0)")
        emit(abs(pt) < 1e-6, "point on the plane 9 cm outside the boundary: " + str(pt) + " (0.0)")
        emit(brackets(top_a), "native face: its box brackets its own plane origin")

    m1 = adsk.core.Matrix3D.create()
    o1 = root.occurrences.addNewComponent(m1)
    m2 = adsk.core.Matrix3D.create()
    m2.translation = adsk.core.Vector3D.create(10.0, 4.0, 2.0)
    o2 = root.occurrences.addNewComponent(m2)
    cube(o1.component, 2.0)
    cube(o2.component, 2.0)
    p1 = lowest_planar(o1.bRepBodies.item(0))
    p2 = lowest_planar(o2.bRepBodies.item(0))
    if p1 is None or p2 is None:
        emit(False, "proxy face pick failed")
    else:
        sep = abs(p2.geometry.origin.z - p1.geometry.origin.z)
        api = mm.measureMinimumDistance(p1, p2).value
        emit(brackets(p1) and brackets(p2),
             "proxy faces of DIFFERENT occurrences: each box brackets its own plane origin")
        emit(abs(api - sep) < 1e-6, "two proxies: plane separation " + str(sep)
             + " reproduces the API's " + str(api) + " - one coordinate space")
""",
    },
    {
        "id": "min-distance-position-order-parallel-faces",
        "claim": ("MeasureResults.positionOne is documented as the point on the FIRST entity, and "
                  "for two PARALLEL PLANAR faces it comes back on the SECOND: measured on a 1 cm "
                  "box, a = top (z=1) and b = bottom (z=0) reads positionOne z=0 and positionTwo "
                  "z=1, and swapping the two arguments swaps the pair with them. A NON-PARALLEL "
                  "pair that is APART keeps the documented order in BOTH argument orders, so the "
                  "order alone cannot carry the labels. Why the non-parallel leg needs a SECOND "
                  "body to discriminate is measured on the same box: a perpendicular pair of ONE "
                  "body TOUCHES - top vs the +x side reads 0 in both argument orders and returns "
                  "the SAME POINT twice, which agrees with either order and proves nothing"),
        "encoded_in": ("model_measure_between._points_on, which labels the pair by MEASURING each "
                       "point against 'a' instead of trusting positionOne/positionTwo order; "
                       "test_model_measure_between.py TestClosestPointLabelBinding"),
        "need_box": True,
        "body": """
    root = des.rootComponent
    mm = app.measureManager

    def plate(x0, x1, height, name):
        sk = root.sketches.add(root.xYConstructionPlane)
        sk.sketchCurves.sketchLines.addTwoPointRectangle(
            adsk.core.Point3D.create(x0, 0.0, 0.0), adsk.core.Point3D.create(x1, 1.0, 0.0))
        ext = root.features.extrudeFeatures.addSimple(
            sk.profiles.item(0), adsk.core.ValueInput.createByReal(height),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        b = ext.bodies.item(0)
        b.name = name
        return b

    def face_where(body, axis, coord):
        # Sign-agnostic: measured, the two faces parallel to the originating SKETCH PLANE both read
        # that plane's normal (0,0,+1), the BOTTOM one included - so only the AXIS the normal runs
        # along holds. The plane origin's coordinate on that axis picks between the two.
        for i in range(body.faces.count):
            f = body.faces.item(i)
            g = adsk.core.Plane.cast(f.geometry)
            if g is None:
                continue
            n = g.normal
            on_axis = abs(getattr(n, axis)) > 0.5
            if on_axis and abs(getattr(g.origin, axis) - coord) < 1e-6:
                return f
        return None

    other = plate(4.0, 5.0, 2.0, "OrderPlate")
    top = face_where(body, "z", 1.0)
    bottom = face_where(body, "z", 0.0)
    side = face_where(other, "x", 4.0)
    if top is None or bottom is None or side is None:
        emit(False, "face pick failed: top=" + repr(top is not None)
             + " bottom=" + repr(bottom is not None) + " side=" + repr(side is not None))
    else:
        # PARALLEL, 1 cm apart: the point labelled positionOne must land on the SECOND argument.
        p = mm.measureMinimumDistance(top, bottom)
        q = mm.measureMinimumDistance(bottom, top)
        emit(abs(p.positionOne.z - 0.0) < 1e-6 and abs(p.positionTwo.z - 1.0) < 1e-6,
             "parallel a=top b=bottom: positionOne z=" + str(p.positionOne.z)
             + " (expect 0.0, on b) positionTwo z=" + str(p.positionTwo.z) + " (expect 1.0, on a)")
        emit(abs(q.positionOne.z - 1.0) < 1e-6 and abs(q.positionTwo.z - 0.0) < 1e-6,
             "parallel a=bottom b=top: positionOne z=" + str(q.positionOne.z)
             + " (expect 1.0, on b) positionTwo z=" + str(q.positionTwo.z) + " (expect 0.0, on a)")
        # NON-PARALLEL and APART (3 cm along x): the documented order HOLDS in both orders. Keyed on
        # x, because both nearest points share z here and z would not tell the two faces apart.
        r = mm.measureMinimumDistance(top, side)
        s = mm.measureMinimumDistance(side, top)
        emit(abs(r.positionOne.x - 1.0) < 1e-6 and abs(r.positionTwo.x - 4.0) < 1e-6,
             "non-parallel a=top b=side: positionOne x=" + str(r.positionOne.x)
             + " (expect 1.0, on a) positionTwo x=" + str(r.positionTwo.x) + " (expect 4.0, on b)")
        emit(abs(s.positionOne.x - 4.0) < 1e-6 and abs(s.positionTwo.x - 1.0) < 1e-6,
             "non-parallel a=side b=top: positionOne x=" + str(s.positionOne.x)
             + " (expect 4.0, on a) positionTwo x=" + str(s.positionTwo.x) + " (expect 1.0, on b)")
        emit(r.value > 1e-6 and s.value > 1e-6,
             "non-parallel pair is APART: " + str(r.value) + " - a 0-distance pair agrees with "
             "either order, which the touching leg below measures")
        # The TOUCHING case the discriminator rests on, measured rather than described: two
        # perpendicular faces of ONE body meet, so the result carries no order to read.
        adj = face_where(body, "x", 1.0)
        if adj is None:
            emit(False, "face pick failed: the box's +x side")
        else:
            u = mm.measureMinimumDistance(top, adj)
            w = mm.measureMinimumDistance(adj, top)

            def same_point(r):
                # the claim is about BOTH argument orders, so each result is gated on its own
                return (abs(r.positionOne.x - r.positionTwo.x) < 1e-6
                        and abs(r.positionOne.y - r.positionTwo.y) < 1e-6
                        and abs(r.positionOne.z - r.positionTwo.z) < 1e-6)

            one_point_u, one_point_w = same_point(u), same_point(w)
            emit(u.value < 1e-6 and w.value < 1e-6 and one_point_u and one_point_w,
                 "perpendicular pair of ONE body: distance " + str(u.value) + "/" + str(w.value)
                 + " positionOne=" + str((u.positionOne.x, u.positionOne.y, u.positionOne.z))
                 + " positionTwo=" + str((u.positionTwo.x, u.positionTwo.y, u.positionTwo.z))
                 + " same_point_twice=" + str(one_point_u) + "/" + str(one_point_w))
""",
    },
    {
        "id": "profile-centroid-component-local",
        "claim": "Profile.areaProperties().centroid reads in the profile's own COMPONENT-LOCAL frame, not world: on a component placed at x=5.0 cm, a 2.0x1.0 cm rectangle at that component's sketch origin reports centroid (1.0, 0.5, 0) - world would read 6.0. The assembly-context PROXY (Sketch.createForAssemblyContext) reports the IDENTICAL point, so a native sketch and a proxied one put their centroids in one space",
        "encoded_in": "_sketch_detail._profiles - the published 'centroid' and the composite handle's locator; _inputs._refind_profile, whose design-wide scan matches a locator centroid against this same local read; test__sketch_detail.py test_profile_centroid_scales_linearly",
        "body": """
    root = des.rootComponent
    m = adsk.core.Matrix3D.create()
    m.translation = adsk.core.Vector3D.create(5.0, 0.0, 0.0)
    occ = root.occurrences.addNewComponent(m)
    placed_x = occ.transform2.translation.x
    comp = occ.component
    sk = comp.sketches.add(comp.xYConstructionPlane)
    sk.sketchCurves.sketchLines.addTwoPointRectangle(
        adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(2.0, 1.0, 0.0))
    native = sk.profiles.item(0).areaProperties().centroid
    proxy = sk.createForAssemblyContext(occ).profiles.item(0).areaProperties().centroid
    # The placement read-back first: an occurrence that did NOT move would make (1.0, 0.5, 0) the
    # world answer too, and the row would pass without discriminating the two frames.
    placed = abs(placed_x - 5.0) < 1e-6
    local = (abs(native.x - 1.0) < 1e-6 and abs(native.y - 0.5) < 1e-6 and abs(native.z) < 1e-6)
    same = (abs(proxy.x - native.x) < 1e-6 and abs(proxy.y - native.y) < 1e-6
            and abs(proxy.z - native.z) < 1e-6)
    emit(placed and local and same, "profile-centroid-component-local: occurrence at x="
         + str(placed_x) + " native (" + str(native.x) + "," + str(native.y) + ","
         + str(native.z) + ") proxy (" + str(proxy.x) + "," + str(proxy.y) + ","
         + str(proxy.z) + ") - world would read x=6.0")
""",
    },
    {
        "id": "vector3d-transformby-mutates-in-place",
        "claim": ("Vector3D.transformBy(matrix) MUTATES the receiver and answers a TRUE value: "
                  "(1,2,3) through a +90 deg rotation about Z reads back (-2,1,3) on the SAME "
                  "object - a receiver still reading (1,2,3) would say the call answers a new "
                  "vector instead. copy() is independent BOTH ways: a copy taken before the "
                  "transform still reads (1,2,3), and transforming a copy leaves the receiver "
                  "where the first transform put it"),
        "encoded_in": ("_assembly_detail._world_axes, which copies each axis vector before "
                       "transformBy and then publishes the RECEIVER; tests/fakes/geometry.py FakeVector3D"),
        "body": """
    import math
    v = adsk.core.Vector3D.create(1.0, 2.0, 3.0)
    before = v.copy()
    rot = adsk.core.Matrix3D.create()
    rot.setToRotation(math.pi / 2.0, adsk.core.Vector3D.create(0.0, 0.0, 1.0),
                      adsk.core.Point3D.create(0.0, 0.0, 0.0))
    returned = v.transformBy(rot)
    moved = (round(v.x, 9), round(v.y, 9), round(v.z, 9))
    copy_held = (before.x, before.y, before.z) == (1.0, 2.0, 3.0)
    # the other direction: the COPY is transformed, and the receiver must not follow it
    twin = v.copy()
    twin.transformBy(rot)
    receiver_held = (round(v.x, 9), round(v.y, 9), round(v.z, 9)) == moved
    emit(bool(returned) and moved == (-2.0, 1.0, 3.0) and copy_held and receiver_held,
         "vector3d-transformby-mutates-in-place: returned=" + repr(returned)
         + " receiver (1.0,2.0,3.0)->" + str(moved) + " (expect (-2.0,1.0,3.0))"
         + " pre-transform copy still (1,2,3)=" + repr(copy_held)
         + " receiver unmoved while its copy transformed=" + repr(receiver_held))
""",
    },
    {
        "id": "meshbody-assembly-context-proxy",
        "claim": ("MeshBody.createForAssemblyContext(occ) mints a WORKING proxy: the object it "
                  "returns has type name MeshBody, keeps the native's name, reads assemblyContext "
                  "as the occurrence it was asked for, and its nativeObject ties back to the "
                  "native by a byte-equal entityToken - while the NATIVE's own assemblyContext "
                  "reads None. Two placements of one component answer two proxies whose contexts "
                  "are DIFFERENT fullPathNames; one shared context, or a proxy answering nothing, "
                  "refutes"),
        "encoded_in": ("_inputs._placed_meshes_named and _placement_refusal - the mesh half of the "
                       "'<occurrence>:<mesh>' address, which LIFTS the placed component's mesh into "
                       "the named occurrence and refuses only a lift that hands nothing back"),
        "body": """
    doc, mesh, occ_a, occ_b = make_mesh_rig(app, "MeshCtx")
    try:
        pa = mesh.createForAssemblyContext(occ_a)
        pb = mesh.createForAssemblyContext(occ_b)
        kinds = [type(pa).__name__, type(pb).__name__]
        names_kept = pa.name == mesh.name and pb.name == mesh.name
        ctx_a, ctx_b = pa.assemblyContext, pb.assemblyContext
        paths = [None if ctx_a is None else ctx_a.fullPathName,
                 None if ctx_b is None else ctx_b.fullPathName]
        want = [occ_a.fullPathName, occ_b.fullPathName]
        ctx_ok = paths == want and want[0] != want[1]
        native_ctx = mesh.assemblyContext
        back = [None if pa.nativeObject is None else pa.nativeObject.entityToken,
                None if pb.nativeObject is None else pb.nativeObject.entityToken]
        ties = back == [mesh.entityToken, mesh.entityToken]
        emit(kinds == ["MeshBody", "MeshBody"] and names_kept and ctx_ok and ties
             and native_ctx is None,
             "meshbody-assembly-context-proxy: types=" + ",".join(kinds)
             + " name kept=" + repr(names_kept) + " contexts=" + str(paths)
             + " (expect " + str(want) + ") nativeObject ties to the native token="
             + repr(ties) + " native assemblyContext=" + repr(native_ctx))
    finally:
        doc.close(False)
""",
    },
    {
        "id": "meshbody-proxy-token-differs",
        "claim": ("A mesh assembly-context proxy's OWN entityToken DIFFERS from its native's AND "
                  "from the other placement's proxy token, while proxy.nativeObject.entityToken is "
                  "byte-equal to the native's - so an identity keys on (nativeObject or self)."
                  "entityToken, and a handle minted in one placement cannot resolve to the other. "
                  "A proxy token equal to the native's, or two placements sharing one, refutes"),
        "encoded_in": ("tests/fakes/mesh.py _MeshProxy - the MeshBody fake's lift, whose per-placement "
                       "token is what keeps _common.native_identity from merging two placements"),
        "body": """
    doc, mesh, occ_a, occ_b = make_mesh_rig(app, "TokComp")
    try:
        pa = mesh.createForAssemblyContext(occ_a)
        pb = mesh.createForAssemblyContext(occ_b)
        pa_eq_native = pa.entityToken == mesh.entityToken
        pb_eq_native = pb.entityToken == mesh.entityToken
        pa_eq_pb = pa.entityToken == pb.entityToken
        back = [None if pa.nativeObject is None else pa.nativeObject.entityToken,
                None if pb.nativeObject is None else pb.nativeObject.entityToken]
        ties = back == [mesh.entityToken, mesh.entityToken]
        ctx_a, ctx_b = pa.assemblyContext, pb.assemblyContext
        paths = [None if ctx_a is None else ctx_a.fullPathName,
                 None if ctx_b is None else ctx_b.fullPathName]
        want = [occ_a.fullPathName, occ_b.fullPathName]
        emit(not pa_eq_native and not pb_eq_native and not pa_eq_pb and ties and paths == want,
             "meshbody-proxy-token-differs: pa_eq_native=" + repr(pa_eq_native)
             + " pb_eq_native=" + repr(pb_eq_native) + " pa_eq_pb=" + repr(pa_eq_pb)
             + " (expect all False) nativeObject ties to the native token=" + repr(ties)
             + " contexts=" + str(paths) + " (expect " + str(want) + ")")
    finally:
        doc.close(False)
""",
    },
    {
        "id": "mesh-combine-tool-lands-where-placed",
        "claim": ("A mesh COMBINE takes its tool geometry WHERE THE TOOL'S OCCURRENCE PLACES IT: "
                  "with A's mesh at local x 0..1, B's mesh at local x 2..3 and B's OCCURRENCE "
                  "placed 5 cm further out, the merged target reads max x 8.0 - the tool's WORLD "
                  "position. Max x 3.0 (B's component-local position) refutes it, and 1.0 says "
                  "nothing merged. The tool is handed over as B's assembly-context PROXY, the "
                  "object an '<occurrence>:<mesh>' address resolves to, so the placement is in "
                  "front of the API rather than hidden from it"),
        "encoded_in": ("mesh_combine.py's note - the where-placed sentence; "
                       "tests/unit/test_mesh_combine.py TestNoteStatesTheReach"),
        "body": """
    doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        des = adsk.fusion.Design.cast(doc.products.itemByProductType("DesignProductType"))
        des.designType = adsk.fusion.DesignTypes.DirectDesignType
        root = des.rootComponent
        tris = [0, 2, 1, 0, 1, 3, 1, 2, 3, 2, 0, 3]
        comp_a = root.occurrences.addNewComponent(adsk.core.Matrix3D.create()).component
        comp_a.name = "MeshLocalA"
        comp_a.meshBodies.addByTriangleMeshData(
            [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0], tris, [], [])
        # B's mesh sits at LOCAL x 2..3 and its occurrence is placed 5 cm out, so the two candidate
        # answers - local 3.0 and world 8.0 - cannot be confused with each other, nor with the 1.0
        # that means nothing merged at all.
        off = adsk.core.Matrix3D.create()
        off.translation = adsk.core.Vector3D.create(5.0, 0.0, 0.0)
        occ_b = root.occurrences.addNewComponent(off)
        comp_b = occ_b.component
        comp_b.name = "MeshLocalB"
        comp_b.meshBodies.addByTriangleMeshData(
            [2.0, 0.0, 0.0, 3.0, 0.0, 0.0, 2.0, 1.0, 0.0, 2.0, 0.0, 1.0], tris, [], [])
        before = round(comp_a.meshBodies.item(0).boundingBox.maxPoint.x, 3)
        tool = comp_b.meshBodies.item(0).createForAssemblyContext(occ_b)
        feats = comp_a.features.meshCombineFeatures
        inp = feats.createInput(comp_a.meshBodies.item(0), [tool])
        # MERGE, not join: two DISJOINT meshes merge into one body holding both shells, so the
        # result's bounding box is the union - which is what the position question is asked of.
        inp.meshCombineOperationType = adsk.fusion.MeshCombineOperationTypes.MergeMeshCombineType
        err = ""
        try:
            feats.add(inp)
        except Exception as e:
            err = " add raised: " + str(e)
        box = comp_a.meshBodies.item(0).boundingBox
        got = round(box.maxPoint.x, 3)
        local = abs(got - 3.0) < 0.05
        world = abs(got - 8.0) < 0.05
        emit(world and not local,
             "mesh-combine-tool-lands-where-placed: target max x before=" + str(before)
             + " after=" + str(got) + " (local=3.0 world=8.0 unmerged=1.0) local=" + repr(local)
             + " world=" + repr(world) + " aabb min=(" + str(round(box.minPoint.x, 3)) + ","
             + str(round(box.minPoint.y, 3)) + "," + str(round(box.minPoint.z, 3)) + ") max=("
             + str(got) + "," + str(round(box.maxPoint.y, 3)) + ","
             + str(round(box.maxPoint.z, 3)) + ")" + err)
    finally:
        doc.close(False)
""",
    },
    {
        "id": "meshbodyvector-shape",
        "claim": ("An Occurrence's meshBodies is a MeshBodyVector, NOT a counted collection: len() "
                  "answers 1 for a component holding one mesh and ITERATING it yields "
                  "assembly-context proxies (each assemblyContext reads that occurrence), while "
                  ".count and .item(i) - the two members every counted walk here uses - BOTH raise "
                  "AttributeError. A .count answering a number, or an iteration yielding natives, "
                  "refutes"),
        "encoded_in": ("_inputs._bodies_named_in's MeshBodyVector note and _component_bodies, "
                       "which asks the PLACED COMPONENT for meshes instead of the occurrence"),
        "body": """
    doc, mesh, occ_a, occ_b = make_mesh_rig(app, "MeshVec")
    try:
        coll = occ_a.meshBodies
        kind = type(coll).__name__
        n = len(coll)
        ctxs = []
        for b in coll:
            c = b.assemblyContext
            ctxs.append(None if c is None else c.fullPathName)
        reads = []
        try:
            coll.count
            reads.append("count=NO RAISE")
        except Exception as e:
            reads.append("count=" + type(e).__name__)
        try:
            coll.item(0)
            reads.append("item=NO RAISE")
        except Exception as e:
            reads.append("item=" + type(e).__name__)
        emit(kind == "MeshBodyVector" and n == 1 and ctxs == [occ_a.fullPathName]
             and reads == ["count=AttributeError", "item=AttributeError"],
             "meshbodyvector-shape: type=" + kind + " len=" + str(n)
             + " iterated contexts=" + str(ctxs) + " (expect ["
             + repr(occ_a.fullPathName) + "]) " + ", ".join(reads)
             + " (expect AttributeError on both)")
    finally:
        doc.close(False)
""",
    },
    {
        "id": "entity-proxy-token-shared",
        "claim": ("SIX reads of ONE placed body - three createForAssemblyContext(occ) calls and "
                  "three reads off that occurrence's bRepBodies - hand back six DISTINCT Python "
                  "wrappers (no two are the same object) that all carry ONE byte-identical "
                  "entityToken: identity never answers 'same entity', the token does. Two reads "
                  "answering the SAME object, or two tokens differing, refutes"),
        "encoded_in": ("tests/fakes/scaffold.py entity_proxy / _EntityProxy, which hands a distinct "
                       "Python object per reference while every read delegates to one entity"),
        "body": """
    body, occ = make_placed_box(des, "ProxyTok")
    made = [body.createForAssemblyContext(occ) for _ in range(3)]
    read = [occ.bRepBodies.item(0) for _ in range(3)]
    wrappers = made + read
    tokens = [w.entityToken for w in wrappers]
    one_token = len(set(tokens)) == 1
    same_object = []
    for i in range(len(wrappers)):
        for j in range(i + 1, len(wrappers)):
            if wrappers[i] is wrappers[j]:
                same_object.append(str(i) + "/" + str(j))
    emit(one_token and bool(tokens[0]) and not same_object,
         "entity-proxy-token-shared: 6 wrappers, distinct tokens=" + str(len(set(tokens)))
         + " (expect 1) pairs that are the SAME object=" + (",".join(same_object) or "none")
         + " (expect none) token=" + repr(tokens[0][:24]))
""",
    },
    {
        "id": "body-proxy-token-differs",
        "claim": ("A body's occurrence PROXY and its NATIVE answer DIFFERENT entityTokens, so one "
                  "physical body reached both ways reads as two entities under a token key: the "
                  "proxy's nativeObject ties back to the native (byte-equal token) while the "
                  "native's own nativeObject reads None, and each wrapper re-reads its own token "
                  "unchanged. Equal tokens on the two, or a native answering a nativeObject, "
                  "refutes"),
        "encoded_in": ("tests/fakes/design.py body_proxy / _OccurrenceProxy, whose token is folded per "
                       "placement; _inputs._body_key, which keys on (nativeObject or self)"),
        "body": """
    native, occ = make_placed_box(des, "ProxySplit")
    proxy = native.createForAssemblyContext(occ)
    tok_native = native.entityToken
    tok_proxy = proxy.entityToken
    back = proxy.nativeObject
    back_token = None if back is None else back.entityToken
    native_back = native.nativeObject
    ctx = proxy.assemblyContext
    stable = proxy.entityToken == tok_proxy and native.entityToken == tok_native
    emit(tok_proxy != tok_native and back_token == tok_native and native_back is None
         and ctx is not None and stable,
         "body-proxy-token-differs: proxy token differs from native="
         + repr(tok_proxy != tok_native) + " proxy.nativeObject token == native token="
         + repr(back_token == tok_native) + " native.nativeObject=" + repr(native_back)
         + " (expect None) proxy context="
         + repr(None if ctx is None else ctx.fullPathName)
         + " each token re-reads unchanged=" + repr(stable))
""",
    },
    {
        "id": "objectcollection-duplicate-add-takes",
        "claim": ("ObjectCollection.add TAKES a duplicate: adding the SAME Point3D twice answers "
                  "True both times and leaves count 2 - the collection de-dupes nothing, so a "
                  "False from add() is a genuine refusal of that object and never 'it was already "
                  "in there'. A second add answering False, or a count still 1, refutes"),
        "encoded_in": ("tests/fakes/scaffold.py _FakeObjectCollection.add, whose False is reserved for "
                       "its `refuse` list"),
        "body": """
    p = adsk.core.Point3D.create(1.0, 2.0, 3.0)
    oc = adsk.core.ObjectCollection.create()
    first = oc.add(p)
    after_first = oc.count
    second = oc.add(p)
    after_second = oc.count
    emit(first is True and second is True and after_first == 1 and after_second == 2,
         "objectcollection-duplicate-add-takes: add=" + repr(first) + " count=" + str(after_first)
         + ", duplicate add=" + repr(second) + " count=" + str(after_second)
         + " (expect True/1 then True/2)")
""",
    },
    {
        "id": "cam-setup-occurrence-model-survives-a-body-swap",
        "claim": ("A setup whose model is an OCCURRENCE keeps that selection when the component's "
                  "contents are replaced: with the component's only body deleted and a differently "
                  "sized one extruded in its place, Setup.models still answers the same one "
                  "Occurrence and the setup reads isValid. The two model members take DIFFERENT "
                  "types - SetupInput.models takes a Python LIST and Setup.models takes an "
                  "ObjectCollection, each refusing the other with a TypeError. The relative stock "
                  "box is the one thing that did NOT follow: stockXLow/High still read the "
                  "ORIGINAL body's extents after the swap, so this row reports them rather than "
                  "claiming the setup re-derived. That staleness is a CACHE, not a frozen "
                  "derivation - discriminated by hand on a saved copy of this rig: the box also "
                  "survived a face toolpath regenerating valid against the new body, and a save + "
                  "reopen then read the NEW body's extents (-21/21 -> -31/31). This row measures "
                  "the in-session reads only; the reopen leg needs a cloud round trip"),
        "encoded_in": ("cam_create_setup.py's models comment and "
                       ".claude/skills/insert-into-template/reference.md, whose part swap depends "
                       "on the selection surviving; tests/fakes/cam.py FakeSetup / FakeSetupInput"),
        "needs": "cam",
        "body": """
    cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType("CAMProductType"))
    d = adsk.fusion.Design.cast(app.activeDocument.products.itemByProductType("DesignProductType"))
    root = d.rootComponent
    occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    comp = occ.component
    comp.name = "SwapProbe"
    sk = comp.sketches.add(comp.xYConstructionPlane)
    sk.sketchCurves.sketchLines.addTwoPointRectangle(
        adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(4.0, 4.0, 0.0))
    comp.features.extrudeFeatures.addSimple(
        sk.profiles.item(0), adsk.core.ValueInput.createByReal(1.0),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    comp.bRepBodies.item(0).name = "FirstBody"
    si = cam.setups.createInput(adsk.cam.OperationTypes.MillingOperation)
    list_taken = True
    try:
        si.models = [occ]
    except TypeError:
        list_taken = False
    setup = cam.setups.add(si)
    setup.name = "SwapProbeSetup"

    def box():
        return tuple(setup.parameters.itemByName(n).expression
                     for n in ("stockXLow", "stockXHigh"))

    before_models = [m.name for m in setup.models]
    before_box = box()
    comp.bRepBodies.item(0).deleteMe()
    sk2 = comp.sketches.add(comp.xYConstructionPlane)
    sk2.sketchCurves.sketchLines.addTwoPointRectangle(
        adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(6.0, 6.0, 0.0))
    comp.features.extrudeFeatures.addSimple(
        sk2.profiles.item(0), adsk.core.ValueInput.createByReal(2.0),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    comp.bRepBodies.item(0).name = "SecondBody"
    d.computeAll()
    for _ in range(40):
        adsk.doEvents()
    after_models = [m.name for m in setup.models]
    after_box = box()
    still_valid = setup.isValid
    # The two members are typed differently: the collection the SETUP takes is refused by the INPUT.
    coll = adsk.core.ObjectCollection.create()
    coll.add(occ)
    input_takes_collection = True
    try:
        cam.setups.createInput(adsk.cam.OperationTypes.MillingOperation).models = coll
    except TypeError:
        input_takes_collection = False
    setup_takes_list = True
    try:
        setup.models = [occ]
    except TypeError:
        setup_takes_list = False
    cleaned = []
    for label, entity in (("setup", setup), ("occurrence", occ)):
        try:
            cleaned.append(label + "=" + repr(entity.deleteMe()))
        except Exception as ex:
            cleaned.append(label + "=raised " + type(ex).__name__)
    emit(list_taken and before_models == ["SwapProbe:1"] and after_models == ["SwapProbe:1"]
         and still_valid is True and input_takes_collection is False and setup_takes_list is False,
         "cam-setup-occurrence-model-survives-a-body-swap: models before=" + str(before_models)
         + " after the swap=" + str(after_models) + " isValid=" + repr(still_valid)
         + "; SetupInput.models took a list=" + str(list_taken) + " and an ObjectCollection="
         + str(input_takes_collection) + ", Setup.models took a list=" + str(setup_takes_list)
         + "; the relative stock box read " + str(before_box) + " before and " + str(after_box)
         + " after (the swapped-in body is half again as wide); cleanup " + ", ".join(cleaned))
""",
    },
    {
        "id": "cam-template-asset-index-alignment",
        "claim": ("In every folder of the Fusion360 and Local template libraries the walk reaches, "
                  "childAssetURLs and childTemplates are INDEX-ALIGNED: "
                  "templateAtURL(childAssetURLs(f)[i]).name == childTemplates(f)[i].name for every "
                  "i, and no folder holds two templates of one name - which is what lets a "
                  "template whose stored leafName spells something else (the shipped hole "
                  "templates: 'Countersink Drill Tap.f3dhsm-template' against the name 'Drill & "
                  "Tap Countersink Hole') be addressed by its position"),
        "encoded_in": ("_cam_templates._walk_library's by_position pairing and its url_basis "
                       "'folder_position' row key; tests/unit/test__cam_templates.py "
                       "TestWalkLibrary, whose fake library answers one asset per template"),
        "body": """
    lib = adsk.cam.CAMManager.get().libraryManager.templateLibrary
    roots = [adsk.cam.LibraryLocations.Fusion360LibraryLocation,
             adsk.cam.LibraryLocations.LocalLibraryLocation]
    folders = []
    for loc in roots:
        u = lib.urlByLocation(loc)
        if u is not None:
            folders.append(u)
    seen = 0
    capped = False
    # BFS over the GROWING list, so the walk reaches every depth the listing walk does; the folder
    # cap is what bounds it, and a walk that hit the cap says so.
    for f in folders:
        if len(folders) > 60:
            capped = True
            break
        for sub in (lib.childFolderURLs(f) or []):
            folders.append(sub)
    misaligned = []
    dup_names = []
    pairs = 0
    for f in folders:
        assets = list(lib.childAssetURLs(f) or [])
        temps = list(lib.childTemplates(f) or [])
        names = [t.name for t in temps]
        if len(set(names)) != len(names):
            dup_names.append(lib.displayName(f))
        if len(assets) != len(temps):
            misaligned.append(lib.displayName(f) + " counts " + str(len(assets)) + "/"
                              + str(len(temps)))
            continue
        seen += 1
        for i in range(len(temps)):
            at = lib.templateAtURL(assets[i])
            pairs += 1
            if at is None or at.name != names[i]:
                misaligned.append(lib.displayName(f) + "[" + str(i) + "] "
                                  + str(at.name if at else None) + " != " + str(names[i]))
    emit(pairs > 0 and not misaligned and not dup_names,
         "cam-template-asset-index-alignment: " + str(pairs) + " index pairs across "
         + str(seen) + "/" + str(len(folders)) + " folders"
         + (" (folder cap hit - deeper folders unmeasured)" if capped else "")
         + (" MISALIGNED: " + "; ".join(misaligned[:5]) if misaligned else "")
         + (" DUP-NAME FOLDERS: " + ", ".join(dup_names[:5]) if dup_names else ""))
""",
    },
    # CAM rows: cam-ncprogram-operations-hold-containers creates a SETUP-scoped NC program (the
    # fact it measures), which arms a modal "no tool selected" dialog on the next operation add in
    # the document - so every row that ADDS a tool-less op runs BEFORE it and deletes its op.
    {
        "id": "cam-alloperations-shape",
        "claim": "Setup.allOperations FLATTENS folder-nested ops into the collection and DROPS the folder objects; counted and iterable. setup.operations holds only top-level ops; folders hang off setup.folders",
        "encoded_in": "tests/fakes/cam.py FakeCAMFolder / FakeSetup (the shared flatten every CAM test drives); tests/unit/test_cam_delete.py; _cam_common.walk_operations",
        "needs": "cam",
        "facts_on_pass": {"behavior.alloperations_flattens_folder_children": True,
                          "behavior.alloperations_drops_folder_objects": True},
        "body": """
    cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType("CAMProductType"))
    setup = None
    for i in range(cam.setups.count):
        if cam.setups.item(i).name == "MeasureSetup":
            setup = cam.setups.item(i)
    top_names = [setup.operations.item(i).name for i in range(setup.operations.count)]
    folder = setup.folders.item(0)
    folder_names = [folder.operations.item(i).name for i in range(folder.operations.count)]
    allops = setup.allOperations
    kinds, names, casted_ok = [], [], True
    for i in range(allops.count):
        x = allops.item(i)
        kinds.append(type(x).__name__)
        names.append(str(getattr(x, "name", None)))
        if adsk.cam.Operation.cast(x) is None:
            casted_ok = False
    it_count = 0
    for x in allops:
        it_count += 1
    ok = (allops.count == 2 and casted_ok and sorted(names) == ["Face1", "Face2"]
          and top_names == ["Face1"] and folder_names == ["Face2"]
          and it_count == allops.count and kinds == ["Operation", "Operation"])
    emit(ok, "cam-alloperations-shape: allOperations=" + ",".join(names)
         + " kinds=" + ",".join(sorted(set(kinds))) + " top=" + ",".join(top_names)
         + " folder=" + ",".join(folder_names) + " iterated=" + str(it_count))
""",
    },
    {
        "id": "cam-parameter-expressions",
        "claim": "op.parameters.itemByName(name).expression is readable AND settable (readback returns what was written); string params carry single-quoted expressions",
        "encoded_in": "tests/unit/test_cam_edit_operation.py, test_cam_edit_setup.py, test_cam_set_nc_comment.py, test_cam_post.py, test_cam_edit_tools.py",
        "needs": "cam",
        "body": """
    cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType("CAMProductType"))
    op = None
    for i in range(cam.setups.count):
        if cam.setups.item(i).name == "MeasureSetup":
            op = adsk.cam.Operation.cast(cam.setups.item(i).allOperations.item(0))
    p = op.parameters.itemByName("tool_feedCutting")
    readable = p is not None and isinstance(p.expression, str)
    old = p.expression
    p.expression = "777 mm/min"
    wrote = p.expression == "777 mm/min"
    p.expression = old
    restored = p.expression == old
    ctx = op.parameters.itemByName("context")
    strat = op.parameters.itemByName("strategy")
    quoted = (ctx is not None and str(ctx.expression).startswith("'")
              and strat is not None and str(strat.expression).startswith("'"))
    emit(readable and wrote and restored and quoted,
         "cam-parameter-expressions: read=" + repr(old) + " write-readback=" + str(wrote)
         + " restored=" + str(restored)
         + " quoted(context)=" + repr(None if ctx is None else ctx.expression))
""",
    },
    {
        "id": "cam-parameter-locked-write-lands",
        "claim": ("isEditable False does NOT mean a write is dropped: 'advancedMode' reads "
                  "isEditable False on every operation and still takes a valid write ('false' -> "
                  "'true': expression AND value.value change, no raise), and takes the same write "
                  "back. Other locked parameters keep theirs (isXpress), so the flag predicts only "
                  "that the UI never offers the edit - which is why cam_edit_operation refuses "
                  "before writing instead of reporting the edit it would have made"),
        "encoded_in": ("cam_edit_operation.py's locked-parameter refusal; tests/fakes/cam.py "
                       "FakeCAMParameter's expression setter, which lands the write off this flag"),
        "needs": "cam",
        "facts_on_pass": {"behavior.cam_locked_parameter_write_lands": True},
        "body": """
    cam, setup = cam_measure_setup()
    if setup is None:
        emit(False, "cam-parameter-locked-write-lands: the harness MeasureSetup is not here")
        return
    op = adsk.cam.Operation.cast(setup.allOperations.item(0))
    p = op.parameters.itemByName("advancedMode")
    if p is None or p.isEditable is not False:
        emit(False, "cam-parameter-locked-write-lands: '" + op.name + "' carries no locked "
             "advancedMode (present=" + str(p is not None) + ")")
        return
    held = p.expression
    vheld = p.value.value
    new = "true" if held == "false" else "false"
    raised = ""
    try:
        p.expression = new
    except Exception as e:
        raised = type(e).__name__ + ": " + str(e)[:60]
    after, vafter = p.expression, p.value.value
    try:
        p.expression = held
    except Exception as e:
        raised = raised or (type(e).__name__ + ": " + str(e)[:60])
    back, vback = p.expression, p.value.value
    emit(after == new and vafter != vheld and back == held and vback == vheld and not raised,
         "cam-parameter-locked-write-lands: advancedMode (isEditable False) " + repr(held)
         + " -> " + repr(after) + " value " + repr(vheld) + " -> " + repr(vafter)
         + " | restored " + repr(back) + "/" + repr(vback) + " | "
         + ("raised " + raised if raised else "no raise"))
""",
    },
    {
        "id": "cam-parameter-bad-reference",
        "claim": ("an expression naming a parameter that does NOT exist is taken silently: no "
                  "raise, expression echoes the text verbatim, value.value still reads a finite "
                  "number, and .error is the only channel that names the failure - reading "
                  "'Failed to evaluate expression.'"),
        "encoded_in": ("tests/fakes/cam.py FakeCAMParameter.error, which answers that text for a "
                       "stored expression naming a missing parameter; the rollback arms of "
                       "cam_edit_operation.py and cam_edit_setup.py gate on it"),
        "needs": "cam",
        "facts_on_pass": {"behavior.cam_bad_reference_error_text": "Failed to evaluate expression."},
        "body": """
    cam, setup = cam_measure_setup()
    if setup is None:
        emit(False, "cam-parameter-bad-reference: the harness MeasureSetup is not here")
        return
    op = adsk.cam.Operation.cast(setup.allOperations.item(0))
    p = op.parameters.itemByName("tool_feedCutting")
    if p is None or p.isEditable is not True:
        emit(False, "cam-parameter-bad-reference: '" + op.name + "' carries no EDITABLE "
             "tool_feedCutting (present=" + str(p is not None) + " isEditable="
             + repr(None if p is None else p.isEditable) + ")")
        return
    held = p.expression
    raised = ""
    after = value = err = None
    try:
        try:
            p.expression = "NoSuchParamXyz * 2"
        except Exception as e:
            raised = type(e).__name__ + ": " + str(e)[:60]
        after = p.expression
        value = p.value.value
        err = p.error
    finally:
        p.expression = held
    finite = (isinstance(value, (int, float)) and not isinstance(value, bool)
              and value == value and abs(value) != float("inf"))
    emit(not raised and after == "NoSuchParamXyz * 2" and finite
         and err == "Failed to evaluate expression." and p.expression == held,
         "cam-parameter-bad-reference: tool_feedCutting " + repr(held)
         + " -> " + repr(after) + " value=" + repr(value) + " finite=" + str(finite)
         + " error=" + repr(err) + " | restored " + repr(p.expression) + " | "
         + ("raised " + raised if raised else "no raise"))
""",
    },
    {
        "id": "cam-tool-parameter-spells-a-backslash-doubled",
        "claim": ("A CAM tool STRING parameter's expression spells a backslash DOUBLED: writing "
                  "tool_description = 'C:\\\\Temp\\\\bin' (each backslash doubled inside the single "
                  "quotes) reads the value back as C:\\Temp\\bin, while the same text written with "
                  "single backslashes is accepted as an expression and its VALUE reads the literal "
                  "'<UNSPECIFIED>' - a silent loss, not a raise. An apostrophe stays "
                  "backslash-escaped, which is the shared codec's own spelling. The library tool "
                  "read through toolLibraryAtURL is a transient copy: the shipped library reads "
                  "its own description back afterwards, so nothing shop-owned is written"),
        "encoded_in": ("cam_edit_tools._quote, whose backslash branch doubles before the shared "
                       "codec's quoting; _cam_common.quote_expression, which escapes the "
                       "apostrophe only"),
        "body": """
    libs = adsk.cam.CAMManager.get().libraryManager.toolLibraries
    lib = None
    for a in libs.childAssetURLs(
            libs.urlByLocation(adsk.cam.LibraryLocations.Fusion360LibraryLocation)):
        if "Milling Tools (Metric)" in a.leafName:
            lib = libs.toolLibraryAtURL(a)
            break
    if lib is None or not lib.count:
        emit(False, "cam-tool-parameter-spells-a-backslash-doubled: the bundled 'Milling Tools "
             "(Metric)' library did not load - inconclusive")
        return
    t = lib.item(0)
    p = t.parameters.itemByName("tool_description")
    original = p.value.value
    reads = {}
    for label, expr in (("doubled", "'C:\\\\\\\\Temp\\\\\\\\bin'"),
                        ("single", "'C:\\\\Temp\\\\bin'"),
                        ("apostrophe", "'Bob\\\\'s tool'")):
        try:
            p.expression = expr
            reads[label] = p.value.value
        except Exception as ex:
            reads[label] = "raised " + type(ex).__name__
    # The library asset is untouched by the writes above: a FRESH load reads its own text back.
    fresh = None
    for a in libs.childAssetURLs(
            libs.urlByLocation(adsk.cam.LibraryLocations.Fusion360LibraryLocation)):
        if "Milling Tools (Metric)" in a.leafName:
            fresh = libs.toolLibraryAtURL(a).item(0).parameters.itemByName(
                "tool_description").value.value
            break
    emit(reads.get("doubled") == "C:\\\\Temp\\\\bin" and reads.get("single") == "<UNSPECIFIED>"
         and reads.get("apostrophe") == "Bob's tool" and fresh == original,
         "cam-tool-parameter-spells-a-backslash-doubled: doubled -> "
         + repr(reads.get("doubled")) + " (expect the path with single backslashes) | single -> "
         + repr(reads.get("single")) + " (expect '<UNSPECIFIED>') | apostrophe -> "
         + repr(reads.get("apostrophe")) + " | the shipped library still reads " + repr(fresh)
         + " (was " + repr(original) + ")")
""",
    },
    {
        "id": "cam-machining-time-knobs",
        "claim": "getMachiningTime on a generated op returns a positive estimate decomposing as totalFeedTime + totalRapidTime + totalToolChangeTime; the feedScale/rapidFeed/toolChangeTime arguments are INERT on this build (identical result across values) despite the API doc's percent / cm-per-s / s units",
        "encoded_in": "_cam_read.py get_machining_time_handler comment + constants; tests/unit/test__cam_common.py",
        "needs": "cam",
        "facts_on_pass": {"behavior.machining_time_args_inert": True},
        "body": """
    import time as _t
    cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType("CAMProductType"))
    op = None
    for i in range(cam.setups.count):
        if cam.setups.item(i).name == "MeasureSetup":
            op = adsk.cam.Operation.cast(cam.setups.item(i).allOperations.item(0))
    future = cam.generateToolpath(op)
    waited = 0
    while not future.isGenerationCompleted and waited < 600:
        adsk.doEvents()
        _t.sleep(0.1)
        waited += 1
    if not future.isGenerationCompleted:
        emit(False, "cam-machining-time-knobs: generation did not complete in 60s - inconclusive, rerun")
        return
    m = cam.getMachiningTime(op, 100.0, 10.58, 1.5)
    t100 = m.machiningTime
    parts = m.totalFeedTime + m.totalRapidTime + m.totalToolChangeTime
    t50 = cam.getMachiningTime(op, 50.0, 10.58, 1.5).machiningTime
    t_slow_rapid = cam.getMachiningTime(op, 100.0, 0.1, 1.5).machiningTime
    # the THIRD argument gets its own variation: the claim covers all three, and two of them moving
    # nothing says nothing about the one never varied
    t_long_change = cam.getMachiningTime(op, 100.0, 10.58, 90.0).machiningTime
    inert = (abs(t50 - t100) < 1e-6 and abs(t_slow_rapid - t100) < 1e-6
             and abs(t_long_change - t100) < 1e-6)
    emit(t100 > 0 and abs(t100 - parts) < 0.1 and inert,
         "cam-machining-time-knobs: t=" + str(round(t100, 2)) + "s parts-sum="
         + str(round(parts, 2)) + "s (totalToolChangeTime="
         + str(round(m.totalToolChangeTime, 2)) + "s) knobs-inert=" + str(inert)
         + " (feedScale 50 -> " + str(round(t50, 2)) + "s, rapidFeed 0.1 -> "
         + str(round(t_slow_rapid, 2)) + "s, toolChangeTime 90 -> "
         + str(round(t_long_change, 2)) + "s)")
""",
    },
    {
        "id": "shape-dump-cam-world",
        "claim": "Each of the 4 CAM-side adsk types this row DUMPS (CAM, Setup, Operation, CAMFolder) exposes a non-empty live public attribute set (dir() membership) - the set the fake-shape lint sweeps the shared CAM fakes against. A shared fake whose live type is NOT dumped here is outside that sweep: the lint's own unmapped list carries those, and this row measures nothing about them",
        "encoded_in": "tests/unit CAM fakes (FakeSetup/CAMFolder/op fakes) via the fake-shape lint's shared-fake scope",
        "needs": "cam",
        "body": """
    cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType("CAMProductType"))
    counts = [dump_shape("CAM", cam)]
    setup = None
    for i in range(cam.setups.count):
        if cam.setups.item(i).name == "MeasureSetup":
            setup = cam.setups.item(i)
    counts.append(dump_shape("Setup", setup))
    op = adsk.cam.Operation.cast(setup.allOperations.item(0))
    counts.append(dump_shape("Operation", op))
    counts.append(dump_shape("CAMFolder", setup.folders.item(0)))
    emit(len(counts) == 4 and all(c > 0 for c in counts),
         "shape-dump-cam-world: " + str(len(counts)) + " types, min attrs " + str(min(counts)))
""",
    },
    {
        "id": "shape-dump-cam-job-world",
        "claim": "Five more CAM types dump non-empty attribute sets off the harness world: cam.setups is a Setups, setups.createInput(MillingOperation) a SetupInput, the bundled 'Milling Tools (Metric)' library's first entry a Tool, the first operation's parameters a CAMParameters whose item(0) is a CAMParameter, and the Fusion360 machine library's first entry a Machine. The dumped Machine is the LIBRARY's; what MeasureSetup's own machine property answered is reported in the detail and gates nothing, because the harness setup is built without a machine",
        "encoded_in": "tests/fakes/cam.py - the shared fakes for these CAM types",
        "needs": "cam",
        "body": """
    cam, setup = cam_measure_setup()
    if setup is None:
        emit(False, "shape-dump-cam-job-world: the harness MeasureSetup is not in this document")
        return
    op = adsk.cam.Operation.cast(setup.allOperations.item(0))
    lib = adsk.cam.CAMManager.get().libraryManager.machineLibrary
    machines = lib.childMachines(
        lib.urlByLocation(adsk.cam.LibraryLocations.Fusion360LibraryLocation))
    live = [("Setups", cam.setups),
            ("SetupInput", cam.setups.createInput(adsk.cam.OperationTypes.MillingOperation)),
            ("Tool", cam_sample_tool()), ("CAMParameters", op.parameters),
            ("CAMParameter", op.parameters.item(0)),
            ("Machine", machines[0] if len(machines) else None)]
    missing = [lbl for lbl, o in live if o is None]
    wrong = [lbl + "=" + type(o).__name__
             for lbl, o in live if o is not None and type(o).__name__ != lbl]
    counts = [dump_shape(lbl, o) for lbl, o in live if o is not None]
    # Read LAST: an unassigned machine is an unmeasured shape, and a raise caught mid-row would
    # sit ahead of the dumps.
    try:
        own = setup.machine
        own_reads = type(own).__name__ if own is not None else "None"
    except Exception as e:
        own_reads = "raised " + str(e)[:60]
    emit(len(counts) == 6 and all(c > 0 for c in counts) and not wrong and not missing,
         "shape-dump-cam-job-world: " + str(len(counts)) + " types, min attrs "
         + str(min(counts) if counts else 0) + ", " + str(len(machines))
         + " machines in the Fusion360 library, MeasureSetup.machine reads " + own_reads
         + ", unreachable " + (", ".join(missing) or "none")
         + ", mislabelled " + (", ".join(wrong) or "none"))
""",
    },
    {
        "id": "cam-empty-toolpath-times-zero",
        "claim": "An operation whose toolpath generated EMPTY reads getMachiningTime(op, 100.0, 10.58, 1.5).machiningTime as EXACTLY 0.0 while hasToolpath reads True and operationState reads IsValid (0) - the flags alone read it as a finished pass, so the time is the only signal that separates it from one that cut. An operation reading hasToolpath False is the OTHER empty shape and the same call RAISES '3 : Machining time could not be calculated.' on it, which is why the time is asked only where hasToolpath is True. This row measures the RAISE leg on the rig's own ungenerated operation; the exact-zero leg needs an operation that generated an empty toolpath, which the MeasureSetup box cannot produce - the measured specimen is a swarf operation on a drafted wall (rails on the non-cutting side), and the leg reports NOT EXERCISED rather than passing when no such operation is in the document",
        "encoded_in": "_cam_common.is_empty_toolpath + _machining_time; tests/unit/test__cam_common.py TestIsEmptyToolpathTimeShape",
        "needs": "cam",
        "body": """
    cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType("CAMProductType"))
    raised = None
    zero_rows = []
    cut_rows = []
    for i in range(cam.setups.count):
        s = cam.setups.item(i)
        for j in range(s.allOperations.count):
            op = adsk.cam.Operation.cast(s.allOperations.item(j))
            if op is None:
                continue
            if not op.hasToolpath and raised is None:
                try:
                    cam.getMachiningTime(op, 100.0, 10.58, 1.5).machiningTime
                    raised = "NO RAISE"
                except Exception as e:
                    raised = str(e)
            if op.hasToolpath and op.operationState == 0:
                t = cam.getMachiningTime(op, 100.0, 10.58, 1.5).machiningTime
                (zero_rows if repr(t) == "0.0" else cut_rows).append(op.name + "=" + repr(t))
    # The raise leg is the one this rig can construct; the zero leg is reported, never assumed.
    raise_ok = raised is not None and "could not be calculated" in raised
    zero_leg = ("exact 0.0 on " + ", ".join(zero_rows)) if zero_rows else "NOT EXERCISED"
    emit(raise_ok,
         "cam-empty-toolpath-times-zero: hasToolpath-False raise=" + repr(raised)
         + " | empty-with-toolpath zero leg: " + zero_leg
         + " | generated ops that timed above zero: " + (", ".join(cut_rows) or "(none)"))
""",
    },
    {
        "id": "cam-children-tree",
        "claim": "Setup.children interleaves top-level Operations and folder objects whose type name is 'CAMFolder'; folder.allOperations and folder.children expose the folder's contents",
        "encoded_in": "no fake: the tools walk _cam_common.CHILD_COLLECTIONS (operations/folders/patterns) and never read children, and tests/fakes/cam.py's FakeCAMFolder / FakeSetup carry no children member",
        "needs": "cam",
        "body": """
    cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType("CAMProductType"))
    setup = None
    for i in range(cam.setups.count):
        if cam.setups.item(i).name == "MeasureSetup":
            setup = cam.setups.item(i)
    kids = setup.children
    kinds = []
    for i in range(kids.count):
        x = kids.item(i)
        kinds.append(type(x).__name__ + ":" + str(getattr(x, "name", None)))
    folder = setup.folders.item(0)
    ok = (kids.count == 2 and "Operation:Face1" in kinds and "CAMFolder:MeasureFolder" in kinds
          and type(folder).__name__ == "CAMFolder" and folder.allOperations.count == 1
          and folder.children.count == 1)
    emit(ok, "cam-children-tree: children=" + ", ".join(kinds) + " folder_type="
         + type(folder).__name__ + " folder_allops=" + str(folder.allOperations.count))
""",
    },
    {
        "id": "cam-generate-future",
        "claim": "A fresh op reads operationState NoToolpath (3) and hasToolpath False; isGenerationCompleted is the completion signal and the op then reads IsValid (0); numberOfOperations populates but numberOfCompleted is NOT a completion signal (observed 0 after a completed single-op generation)",
        "encoded_in": "tests/unit/test_cam_generate.py, test_cam_create_operation.py; cam_generate.py, cam_get_status.py",
        "needs": "cam",
        "facts_on_pass": {"behavior.number_of_completed_is_completion_signal": False},
        "body": """
    import time as _t
    cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType("CAMProductType"))
    op2 = None
    for i in range(cam.setups.count):
        if cam.setups.item(i).name == "MeasureSetup":
            for x in cam.setups.item(i).allOperations:
                o = adsk.cam.Operation.cast(x)
                if o is not None and o.name == "Face2":
                    op2 = o
    pre_state = op2.operationState
    pre_path = op2.hasToolpath
    future = cam.generateToolpath(op2)
    n_ops = future.numberOfOperations
    waited = 0
    while not future.isGenerationCompleted and waited < 600:
        adsk.doEvents()
        _t.sleep(0.1)
        waited += 1
    if not future.isGenerationCompleted:
        emit(False, "cam-generate-future: generation did not complete in 60s - inconclusive, rerun")
        return
    n_done = future.numberOfCompleted
    post_state = op2.operationState
    emit(pre_state == 3 and pre_path is False and n_ops == 1 and post_state == 0,
         "cam-generate-future: pre_state=" + str(pre_state) + " pre_path=" + str(pre_path)
         + " ops=" + str(n_ops) + " numberOfCompleted=" + str(n_done)
         + " (informational only) post_state=" + str(post_state))
""",
    },
    {
        "id": "cam-has-no-simulation-api",
        "claim": ("adsk.cam exposes NO toolpath simulation, collision, gouge or stock-verification "
                  "API: no module-level name matches simulate/collision/gouge/verify, and "
                  "GeneratedDataType carries exactly three int members - the additive analyses. "
                  "This row asserts the ABSENCE, so a build that grows the API turns it red"),
        "encoded_in": "cam_inspect_toolpaths.py, which reads validity and up-to-dateness rather than correctness; no tool in this surface offers toolpath simulation",
        "body": """
    import re as _re
    pat = _re.compile("simulat|collision|gouge|verif", _re.I)
    named = sorted(n for n in dir(adsk.cam) if not n.startswith("_") and pat.search(n))
    T = getattr(adsk.cam, "GeneratedDataType", None)
    members = sorted(n for n in dir(T) if not n.startswith("_")
                     and isinstance(getattr(T, n), int)) if T is not None else []
    emit(not named and len(members) == 3,
         "cam-has-no-simulation-api: adsk.cam names matching simulate/collision/gouge/verify="
         + (", ".join(named) or "none") + " GeneratedDataType=" + (", ".join(members) or "absent"))
""",
    },
    {
        "id": "cam-checktoolpath-raises-on-empty-setup",
        "claim": ("CAM.checkToolpath RAISES ('3 : The operations are not CAM objects') on a Setup "
                  "whose operations.count is 0, and returns a bool for a populated setup in the "
                  "same session - emptiness, not the document, is the discriminator"),
        "encoded_in": "cam_inspect_toolpaths.py _document_verdict/_scoped_verdict empty-setup split; tests/unit/test_cam_inspect_toolpaths.py TestEmptySetups",
        "needs": "cam",
        "body": """
    cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType("CAMProductType"))
    populated = None
    for i in range(cam.setups.count):
        if cam.setups.item(i).name == "MeasureSetup":
            populated = cam.setups.item(i)
    # A fresh empty setup in the same document; removed again below so no later row sees it.
    si = cam.setups.createInput(adsk.cam.OperationTypes.MillingOperation)
    empty = cam.setups.add(si)
    empty.name = "MeasureEmpty"
    try:
        raised = None
        try:
            cam.checkToolpath(empty)
            raised = False
        except Exception as e:
            raised = True
            msg = str(e)
        populated_answered = isinstance(cam.checkToolpath(populated), bool)
        emit(raised is True and populated_answered,
             "cam-checktoolpath-raises-on-empty-setup: empty raised=" + str(raised)
             + (" msg=" + msg[:60] if raised else "")
             + " populated_answers_bool=" + str(populated_answered))
    finally:
        empty.deleteMe()
""",
    },
    {
        "id": "cam-suppress-discards-toolpath",
        "claim": ("Setting Operation.isSuppressed True flips hasToolpath True -> False on a "
                  "generated op, and clearing the flag again leaves hasToolpath False - suppressing "
                  "DISCARDS the toolpath rather than hiding it, and the op carries none until it is "
                  "regenerated"),
        "encoded_in": "cam_inspect_toolpaths.py _split_suppressed + the include_suppressed input description, the DISCARD fact's one wire home",
        "needs": "cam",
        "body": """
    import time as _t
    cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType("CAMProductType"))
    op = None
    for i in range(cam.setups.count):
        if cam.setups.item(i).name == "MeasureSetup":
            for x in cam.setups.item(i).allOperations:
                o = adsk.cam.Operation.cast(x)
                if o is not None and o.name == "Face2":
                    op = o
    if op.hasToolpath is False:
        # the world builder creates its ops UNGENERATED (generate=False), so a --only run reaches
        # here before any row has generated Face2 - the row primes its own subject rather than
        # reading a discard off an op that never held a toolpath
        f = cam.generateToolpath(op)
        n = 0
        while not f.isGenerationCompleted and n < 600:
            adsk.doEvents()
            _t.sleep(0.1)
            n += 1
        if not f.isGenerationCompleted:
            emit(False, "cam-suppress-discards-toolpath: priming generation did not complete in"
                 " 60s - inconclusive, rerun")
            return
    before = op.hasToolpath
    op.isSuppressed = True
    try:
        during = op.hasToolpath
    finally:
        op.isSuppressed = False
    after_flag = op.isSuppressed
    # The read that tells DISCARDED from HIDDEN: with the flag cleared and no regeneration, a
    # hidden toolpath would come back and a discarded one cannot.
    after_toolpath = op.hasToolpath
    emit(before is True and during is False and after_flag is False and after_toolpath is False,
         "cam-suppress-discards-toolpath: hasToolpath before=" + str(before)
         + " suppressed=" + str(during) + " unsuppressed_flag_restored=" + str(after_flag)
         + " hasToolpath after unsuppression, no regeneration=" + str(after_toolpath))
""",
    },
    {
        "id": "cam-errored-op-state-pair",
        "claim": ("An op whose generation FAULTS (top height below bottom height) reads hasError "
                  "True with operationState NoToolpath (3), hasToolpath False and isValid True - "
                  "hasError True beside operationState 0 was NOT observed; correcting the fault "
                  "and regenerating reads the clean triple again (hasError False, operationState "
                  "IsValid (0), a toolpath) - measured on the RECOVERY as well as the baseline, "
                  "and the row leaves the op clean: an op left faulted poisons every later "
                  "getMachiningTime over its collection with 'Machining time could not be "
                  "calculated' (measured)"),
        "encoded_in": ("tests/unit/test__cam_common.py TestErroredOpNeverReadsValid, whose fake "
                       "carries hasError True with operationState 0; _cam_common.op_primary_state, "
                       "which classifies an errored op before it reads operationState"),
        "needs": "cam",
        "body": """
    import time as _t
    cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType("CAMProductType"))
    op = None
    for i in range(cam.setups.count):
        if cam.setups.item(i).name == "MeasureSetup":
            for x in cam.setups.item(i).allOperations:
                o = adsk.cam.Operation.cast(x)
                if o is not None and o.name == "Face1":
                    op = o
    def _generate(o):
        f = cam.generateToolpath(o)
        n = 0
        while not f.isGenerationCompleted and n < 600:
            adsk.doEvents()
            _t.sleep(0.1)
            n += 1
        return f.isGenerationCompleted
    # Baseline: the same op generated clean, so the errored reads below are a DIFFERENCE, not a
    # first look at an op of unknown history.
    op.parameters.itemByName("bottomHeight_offset").expression = "0 mm"
    if not _generate(op):
        emit(False, "cam-errored-op-state-pair: baseline generation did not complete in 60s"
             " - inconclusive, rerun")
        return
    ok_base = (op.operationState == 0 and op.hasError is False and op.hasToolpath is True)
    base = ("base=(state " + str(op.operationState) + ", hasError " + repr(op.hasError)
            + ", hasToolpath " + repr(op.hasToolpath) + ")")
    # A bottom offset ABOVE the top height is a parameter fault the generator rejects.
    op.parameters.itemByName("bottomHeight_offset").expression = "50 mm"
    if not _generate(op):
        # restore the fault before leaving: an op left faulted regenerates ERRORED forever after,
        # and every later getMachiningTime over its collection raises (measured)
        op.parameters.itemByName("bottomHeight_offset").expression = "0 mm"
        _generate(op)
        emit(False, "cam-errored-op-state-pair: fault generation did not complete in 60s"
             " - inconclusive (fault restored), rerun")
        return
    lines = (op.error or "").strip().splitlines()
    ok_err = (op.hasError is True and op.operationState == 3 and op.hasToolpath is False)
    errored = ("errored=(hasError " + repr(op.hasError) + ", operationState "
               + str(op.operationState) + ", hasToolpath " + repr(op.hasToolpath)
               + ", isValid " + repr(op.isValid) + ")")
    # THE RECOVERY, measured rather than assumed - and the self-cleaning the world depends on:
    # correct the fault, regenerate, and the clean triple must read again.
    op.parameters.itemByName("bottomHeight_offset").expression = "0 mm"
    if not _generate(op):
        emit(False, "cam-errored-op-state-pair: restore generation did not complete in 60s -"
             " Face1 LEFT ERRORED, rerun before any machining-time row")
        return
    ok_clean = (op.hasError is False and op.operationState == 0 and op.hasToolpath is True)
    emit(ok_base and ok_err and ok_clean,
         "cam-errored-op-state-pair: " + base + " " + errored
         + " restored_clean=" + str(ok_clean) + " error=" + repr(lines[0] if lines else ""))
""",
    },
    {
        "id": "cam-machine-uncleared-simulation-assignment-refused",
        "claim": ("Assigning a library machine that STILL carries its simulation model to "
                  "Setup.machine is REFUSED: the assignment raises instead of taking. The leg runs "
                  "only when the machine this run picks (the first library machine whose "
                  "kinematics carries a spindle) has a simulation model - when it does not, the "
                  "receipt records the leg as not exercised rather than claiming a refusal. The "
                  "raise is provoked, so it sits in its OWN row: an abort or a rolled-back "
                  "transaction costs this measurement and no other"),
        "encoded_in": ("cam_edit_setup's machine_strip_simulation path (the strip-before-assign "
                       "and the refusal hint) and tests/unit/test_cam_edit_setup.py "
                       "TestMachineStripSimulation"),
        "needs": "cam",
        "body": """
    cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType("CAMProductType"))
    lib = adsk.cam.CAMManager.get().libraryManager.machineLibrary
    type_id = adsk.cam.KinematicsMachineElement.staticTypeId()

    def spindle_max(m):
        # the supported route only - Machine.kinematics is flagged not officially supported
        el = m.elements.defaultItemByType(type_id)
        if el is None:
            found = m.elements.itemsByType(type_id)
            el = found[0] if found else None
        if el is None:
            return None
        rpm, stack = None, [(el.parts, 0)]
        while stack:
            coll, depth = stack.pop()
            for i in range(coll.count):
                p = coll.item(i)
                if p.spindle is not None and p.spindle.maxSpeed > 0:
                    rpm = p.spindle.maxSpeed if rpm is None else max(rpm, p.spindle.maxSpeed)
                if depth < 8:
                    stack.append((p.children, depth + 1))
        return rpm

    candidates = []
    for loc in (adsk.cam.LibraryLocations.LocalLibraryLocation,
                adsk.cam.LibraryLocations.Fusion360LibraryLocation):
        for vendor in ("Haas", ""):
            try:
                candidates.extend(lib.createQuery(loc, vendor, "").execute() or [])
            except Exception:
                pass
        if candidates:
            break
    picked = None
    for m in candidates[:250]:
        if spindle_max(m):
            picked = m
            break
    if picked is None:
        emit(False, "cam-machine-uncleared-simulation-assignment-refused: no library machine in "
             + str(len(candidates)) + " carried a kinematics spindle - inconclusive")
        return
    setup = None
    for i in range(cam.setups.count):
        if cam.setups.item(i).name == "MeasureSetup":
            setup = cam.setups.item(i)
    # The assignment is ATTEMPTED with the simulation model still attached: that is what says the
    # clearing cam_edit_setup does is needed rather than merely done.
    sim = picked.hasSimulationModel
    refused, why = None, ""
    if sim:
        try:
            setup.machine = picked
            refused = False
        except Exception as exc:
            refused = True
            why = type(exc).__name__ + ": " + (str(exc).strip().splitlines() or [""])[0][:80]
    emit(refused is not False,
         "cam-machine-uncleared-simulation-assignment-refused: " + str(picked.description)
         + " hasSimulationModel=" + repr(sim) + " uncleared assignment="
         + ("refused: " + why if refused else
            "not exercised (the machine this run picked carries no simulation model)"
            if refused is None else "SUCCEEDED - the clearing is not needed"))
""",
    },
    {
        "id": "cam-machine-simulation-refusal-spans-both-locations",
        "claim": ("The simulation-model refusal is not a property of the LOCAL library: a machine "
                  "read out of the bundled Fusion360 location whose hasSimulationModel is True is "
                  "refused by Setup.machine the same way the local one is, and the platform words "
                  "it '3 : Setting a simulation ready machine from an external library is "
                  "currently not supported' - the flag and the library, not the vendor: three "
                  "different Fusion360 machines are refused alike. The row stands up its OWN "
                  "document and setup, because a setup that has already refused one such "
                  "assignment aborts the script on the next one"),
        "encoded_in": ("_cam_common._MACHINE_LOCATIONS and cam_edit_setup's "
                       "machine_strip_simulation refusal hint, which names the simulation model "
                       "and not the location it was read from"),
        "body": """
    lib = adsk.cam.CAMManager.get().libraryManager.machineLibrary
    machines = lib.createQuery(adsk.cam.LibraryLocations.Fusion360LibraryLocation,
                               "", "").execute() or []
    sim_true = [m for m in machines if m.hasSimulationModel]
    if not sim_true:
        emit(False, "cam-machine-simulation-refusal-spans-both-locations: no Fusion360 machine of "
             + str(len(machines)) + " read hasSimulationModel True - inconclusive")
        return
    ui = app.userInterface
    was = ui.activeWorkspace.id
    doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    reads = []
    try:
        d = adsk.fusion.Design.cast(doc.products.itemByProductType("DesignProductType"))
        make_box(d, "SimRefusalBox")
        ui.workspaces.itemById("CAMEnvironment").activate()
        adsk.doEvents()
        cam = adsk.cam.CAM.cast(doc.products.itemByProductType("CAMProductType"))
        setup = cam.setups.add(cam.setups.createInput(adsk.cam.OperationTypes.MillingOperation))
        for m in sim_true[:3]:
            label = str(m.vendor) + " " + str(m.model)
            try:
                setup.machine = m
                reads.append(label + "=LANDED")
            except Exception as exc:
                reads.append(label + "=refused: "
                             + (str(exc).strip().splitlines() or [""])[-1][:80])
    finally:
        doc.close(False)
        ui.workspaces.itemById(was).activate()
        adsk.doEvents()
    external = [r for r in reads if "simulation ready machine from an external library" in r]
    emit(len(reads) == 3 and len(external) == 3,
         "cam-machine-simulation-refusal-spans-both-locations: " + str(len(sim_true)) + " of "
         + str(len(machines)) + " Fusion360 machines read hasSimulationModel True; "
         + " | ".join(reads))
""",
    },
    {
        "id": "cam-machine-spindle-max-readable",
        "claim": ("A machine's spindle maximum and axis travels are readable through the SUPPORTED "
                  "route Machine.elements -> defaultItemByType(KinematicsMachineElement."
                  "staticTypeId()) -> parts (a tree of MachinePart, EVERY part carrying .axis / "
                  ".spindle / .toolStation members, any of them null): part.spindle.maxSpeed is "
                  "rpm, and part.axis.physicalRange.min/.max are the travel of a "
                  "LinearMachineAxisType axis - what UNIT those two numbers are in is NOT measured "
                  "here, so this row reports them raw. clearSimulationModel() strips the resolved "
                  "copy (the refusal that makes stripping necessary is the paired "
                  "cam-machine-uncleared-simulation-assignment-refused row), the stripped copy "
                  "assigns, and the spindle number and every axis range then read back IDENTICALLY "
                  "through Setup.machine - a read that only discriminates when Setup.machine "
                  "answers an object OTHER than the one just assigned, so the receipt records "
                  "through_is_assigned for the case this run got"),
        "encoded_in": ("_cam_common.kinematics_parts / machine_limits / machine_spindle_max and "
                       "tests/unit/test__cam_common.py TestMachineLimits, whose fake machine "
                       "carries 12000 rpm and 762/406/508 mm travels; the strip-then-assign path "
                       "in cam_edit_setup and tests/unit/test_cam_edit_setup.py "
                       "TestMachineStripSimulation"),
        "needs": "cam",
        "body": """
    cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType("CAMProductType"))
    lib = adsk.cam.CAMManager.get().libraryManager.machineLibrary
    type_id = adsk.cam.KinematicsMachineElement.staticTypeId()

    def limits(m):
        # the supported route only - Machine.kinematics is flagged not officially supported
        el = m.elements.defaultItemByType(type_id)
        if el is None:
            found = m.elements.itemsByType(type_id)
            el = found[0] if found else None
        if el is None:
            return None, [], (False, 0)
        parts, stack = [], [(el.parts, 0)]
        while stack:
            coll, depth = stack.pop()
            for i in range(coll.count):
                p = coll.item(i)
                parts.append(p)
                if depth < 8:
                    stack.append((p.children, depth + 1))
        rpm, axes, every_part, stations = None, [], bool(parts), 0
        for p in parts:
            if p.spindle is not None and p.spindle.maxSpeed > 0:
                rpm = p.spindle.maxSpeed if rpm is None else max(rpm, p.spindle.maxSpeed)
            if p.axis is not None:
                r = p.axis.physicalRange
                axes.append((p.axis.name,
                             p.axis.axisType == adsk.cam.MachineAxisTypes.LinearMachineAxisType,
                             r.isInfinite, r.min, r.max))
            # the third member of the trio: read on EVERY part, so "each carries one" is measured
            # rather than named
            if "toolStation" in dir(p):
                if p.toolStation is not None:
                    stations += 1
            else:
                every_part = False
        return rpm, axes, (every_part, stations)

    candidates = []
    for loc in (adsk.cam.LibraryLocations.LocalLibraryLocation,
                adsk.cam.LibraryLocations.Fusion360LibraryLocation):
        for vendor in ("Haas", ""):
            try:
                candidates.extend(lib.createQuery(loc, vendor, "").execute() or [])
            except Exception:
                pass
        if candidates:
            break
    picked, rpm, axes, stations = None, None, [], (False, 0)
    for m in candidates[:250]:
        rpm, axes, stations = limits(m)
        if rpm:
            picked = m
            break
    if picked is None:
        emit(False, "cam-machine-spindle-max-readable: no library machine in "
             + str(len(candidates)) + " carried a kinematics spindle - inconclusive")
        return
    linear = [a for a in axes if a[1] and not a[2]]
    setup = None
    for i in range(cam.setups.count):
        if cam.setups.item(i).name == "MeasureSetup":
            setup = cam.setups.item(i)
    # The strip runs before the assignment; provoking the UNCLEARED refusal is the paired row's
    # job, so nothing here is attempted with the simulation model still attached.
    sim = picked.hasSimulationModel
    if sim:
        picked.clearSimulationModel()
    stripped = picked.hasSimulationModel
    setup.machine = picked
    # The API doc calls this a transient copy. Whether THIS run got one is recorded rather than
    # assumed: when Setup.machine answers the very object assigned, the read-back below compares a
    # machine against itself and settles nothing, so through_is_assigned goes in the receipt.
    through = setup.machine
    through_is_assigned = through is picked
    assigned = through is not None and through.description == picked.description
    rpm2, axes2, stations2 = limits(through)
    # every axis NUMBER, not just how many axes there are: the count survives any read that
    # answers the same shape, the ranges only survive one that answers the same machine
    same_ranges = (len(axes2) == len(axes)
                   and [repr(a) for a in sorted(axes)] == [repr(b) for b in sorted(axes2)])
    emit(rpm > 0 and len(linear) >= 1 and stations[0] and stripped is False and assigned
         and rpm2 == rpm and same_ranges,
         "cam-machine-spindle-max-readable: " + str(picked.description) + " maxSpeed=" + str(rpm)
         + " rpm, axes=" + ", ".join(a[0] + ("(linear " + str(round(a[4] - a[3], 4))
                                             + ")" if a[1] and not a[2] else
                                             "(infinite)" if a[2] else "(rotary)") for a in axes)
         + " toolStation on every part=" + str(stations[0]) + " (" + str(stations[1])
         + " non-null) hasSimulationModel " + repr(sim) + " -> " + repr(stripped)
         + " assigned=" + repr(assigned) + " through_is_assigned="
         + str(through_is_assigned) + " through-setup maxSpeed=" + str(rpm2)
         + " same_axis_ranges=" + str(same_ranges) + " (" + str(stations2[1])
         + " non-null toolStations through the setup)")
""",
    },
    {
        "id": "cam-toolpreset-per-operation",
        "claim": ("Operation.toolPreset reads the preset the operation USES (null when it runs "
                  "none), and ToolPreset.name / ToolPreset.id both read as non-empty strings on "
                  "it. With the operation's tool BOUND to a variable, that tool's preset LIST "
                  "reads: .presets answers .count as an int and .item(i).name as a string - no "
                  "tool library lookup in between"),
        "encoded_in": ("cam_get._slice_tool's active_preset + preset_names and "
                       "_cam_read._operation_summary's per-row preset; "
                       "tests/unit/test_cam_get.py TestToolSlicePresets, "
                       "tests/unit/test__cam_common.py TestOperationRowContext"),
        "needs": "cam",
        "body": """
    cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType("CAMProductType"))
    op = None
    for i in range(cam.setups.count):
        if cam.setups.item(i).name == "MeasureSetup":
            op = adsk.cam.Operation.cast(cam.setups.item(i).allOperations.item(0))
    active = op.toolPreset                      # the preset it RUNS, and legitimately null
    active_name = None if active is None else active.name
    active_id = None if active is None else active.id
    # An op that runs no preset leaves nothing to read .name/.id off, so that half reports as
    # untested rather than being asserted against a null.
    names_read = (None if active is None else
                  (isinstance(active_name, str) and isinstance(active_id, str)
                   and len(active_id) > 0))
    # The LIST the op can choose from - the read cam_get publishes as preset_names. The tool is
    # BOUND to a variable for the whole read, the shape cam_get._slice_tool uses; item(0).name is
    # read because a count alone does not prove the rows read.
    tool = op.tool
    presets = tool.presets
    n = presets.count
    first = presets.item(0).name if n else None
    # The same collection off an UNBOUND temporary is NOT read here, and must not be: it raises
    # '3 : Invalid transient tool', and that raise takes the whole Python.Run command down even
    # inside try/except - the traceback comes back with no frame from this script at all, only
    # executeTextCommand's. A try/except around it does not make it observable; it makes the row
    # unrunnable. Measure the difference from OUTSIDE a script if it is ever wanted.
    emit(isinstance(n, int) and not isinstance(n, bool)
         and (n == 0 or isinstance(first, str))
         and names_read is not False,
         "cam-toolpreset-per-operation: op.toolPreset=" + repr(active_name)
         + " name/id=" + repr(names_read)
         + "; bound: count=" + repr(n) + " item0=" + repr(first)
         + ("" if active is not None else " (op runs no preset - name/id untested)")
         + ("" if n else " (tool has no presets - that half untested)"))
""",
    },
    {
        "id": "cam-tool-dimension-parameter-names",
        "claim": ("A milling tool's cutting geometry is carried by four CAMParameters reachable "
                  "through Tool.parameters.itemByName under exactly these names - tool_diameter, "
                  "tool_fluteLength, tool_cornerRadius, tool_overallLength - each answering a "
                  ".value.value that is a number in Fusion's internal cm. A square-ended tool "
                  "still CARRIES tool_cornerRadius, reading 0, so an absent parameter and a zero "
                  "radius are different answers"),
        "encoded_in": ("_cam_common.tool_dimensions - the four names cam_get(include=['tool']) "
                       "publishes as 'dimensions'; tests/unit/test_cam_get.py "
                       "TestToolSliceDimensions"),
        "needs": "cam",
        "body": """
    tool = cam_sample_tool()
    if tool is None:
        emit(False, "cam-tool-dimension-parameter-names: no bundled sample milling library"
             " - inconclusive")
        return
    names = ["tool_diameter", "tool_fluteLength", "tool_cornerRadius", "tool_overallLength"]
    exprs = {}
    numeric = {}
    for n in names:
        p = tool.parameters.itemByName(n)
        exprs[n] = None if p is None else p.expression
        v = None if p is None else p.value.value
        numeric[n] = isinstance(v, float) and not isinstance(v, bool)
    emit(all(exprs[n] is not None for n in names) and all(numeric[n] for n in names),
         "cam-tool-dimension-parameter-names: " + repr(exprs) + " numeric=" + repr(numeric)
         + " of " + str(tool.parameters.count) + " parameters on the tool")
""",
    },
    {
        "id": "cam-op-spindle-speed-vs-machine-max",
        "claim": ("An operation's tool_spindleSpeed CAMParameter reads as a NUMBER through "
                  ".value.value, in the same rpm unit as MachineSpindle.maxSpeed: setting the "
                  "expression to 24999 reads back 24999.0. With a machine assigned, that machine's "
                  "kinematics spindle maximum reads as a number off Setup.machine, so the two "
                  "sides of the over-max comparison are both readable numbers - WHICH WAY the "
                  "comparison lands is a property of the machine in the library, not of the API, "
                  "and is reported rather than asserted"),
        "encoded_in": ("_cam_common.op_spindle_speed / spindle_check, whose "
                       "spindle_over_machine_max flag publishes the comparison as fact; "
                       "tests/unit/test__cam_common.py TestSpindleCheck"),
        "needs": "cam",
        "body": """
    cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType("CAMProductType"))
    setup, op = None, None
    for i in range(cam.setups.count):
        if cam.setups.item(i).name == "MeasureSetup":
            setup = cam.setups.item(i)
            op = adsk.cam.Operation.cast(setup.allOperations.item(0))
    p = op.parameters.itemByName("tool_spindleSpeed")
    if p is None:
        emit(False, "cam-op-spindle-speed-vs-machine-max: the op carries no tool_spindleSpeed"
             " parameter - inconclusive")
        return
    before = p.expression
    native = p.value.value
    native_is_number = isinstance(native, float) or isinstance(native, int)
    now, rpm, has_machine = None, None, False
    try:
        p.expression = "24999"
        now = p.value.value
        # the machine's own maximum, off the SAME kinematics route the comparison uses
        m = setup.machine
        has_machine = m is not None
        if m is not None:
            el = m.elements.defaultItemByType(adsk.cam.KinematicsMachineElement.staticTypeId())
            if el is None:
                found = m.elements.itemsByType(adsk.cam.KinematicsMachineElement.staticTypeId())
                el = found[0] if found else None
            stack = [(el.parts, 0)] if el is not None else []
            while stack:
                coll, depth = stack.pop()
                for i in range(coll.count):
                    part = coll.item(i)
                    if part.spindle is not None and part.spindle.maxSpeed > 0:
                        rpm = (part.spindle.maxSpeed if rpm is None
                               else max(rpm, part.spindle.maxSpeed))
                    if depth < 8:
                        stack.append((part.children, depth + 1))
    finally:
        # the traversal above can raise; every later row reads this same operation, so the
        # 24999 this row wrote is undone whether or not the measurement completed
        p.expression = before
    reads_back = native_is_number and now is not None and abs(now - 24999.0) < 1e-6
    restored = p.expression == before
    over = None if rpm is None else (now > rpm)
    # A machine IS assigned, so its maximum has to read as a number - that, not the direction of
    # the comparison, is what makes spindle_over_machine_max computable at all. Asserting the
    # direction would assert a fact about whichever library machine got picked.
    comparable = isinstance(rpm, (int, float)) and not isinstance(rpm, bool)
    emit(reads_back and restored and (not has_machine or comparable),
         "cam-op-spindle-speed-vs-machine-max: native=" + repr(native) + " expression="
         + repr(before) + " set-24999 read .value.value=" + repr(now) + " machine max="
         + repr(rpm) + " over=" + repr(over) + " restored=" + repr(restored)
         + ("" if comparable else
            " (a machine is assigned but its kinematics carries no spindle maximum)"
            if has_machine else " (no machine assigned - the comparison half is untested)"))
""",
    },
    {
        "id": "cam-machining-time-toolpathless-op-times",
        "claim": ("CAM.getMachiningTime over an ObjectCollection of NON-suppressed generated "
                  "operations returns a time, and it STILL returns one when one of those "
                  "operations carries no toolpath but is NOT suppressed (suppressing an op "
                  "discards its toolpath, so clearing the flag again leaves exactly that state). "
                  "Both legs are POSITIVE measurements and this row declares no expect, so a "
                  "script-level abort is an ERROR here. Paired with "
                  "cam-machining-time-suppressed-op-contributes-nothing, the two rows separate "
                  "the two properties a suppressed op carries: the missing toolpath (times fine, "
                  "contributes nothing) and the flag itself (also times fine, contributes "
                  "nothing) - neither breaks the call"),
        "encoded_in": ("_cam_read._timeable_ops / get_machining_time_handler and "
                       "tests/unit/test__cam_common.py TestMachiningTimeExcludesSuppressed"),
        "needs": "cam",
        "body": """
    import time as _t
    cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType("CAMProductType"))
    ops = []
    for i in range(cam.setups.count):
        if cam.setups.item(i).name == "MeasureSetup":
            for x in cam.setups.item(i).allOperations:
                o = adsk.cam.Operation.cast(x)
                if o is not None:
                    ops.append(o)

    def _generate(o):
        f = cam.generateToolpath(o)
        n = 0
        while not f.isGenerationCompleted and n < 600:
            adsk.doEvents()
            _t.sleep(0.1)
            n += 1
        return f.isGenerationCompleted

    for o in ops:
        o.isSuppressed = False
        if not _generate(o):
            emit(False, "cam-machining-time-toolpathless-op-times: generation did not complete in"
                 " 60s - inconclusive, rerun")
            return
    clean = adsk.core.ObjectCollection.create()
    for o in ops:
        clean.add(o)
    good = cam.getMachiningTime(clean, 100.0, 10.58, 1.5).machiningTime
    emit(good > 0, "cam-machining-time-toolpathless-op-times: " + str(len(ops))
         + " unsuppressed ops timed at " + str(round(good, 2)) + "s")
    # A suppressed op is ALSO toolpath-less (cam-suppress-discards-toolpath), so the suppressed
    # collection the paired row measures cannot tell the two candidate causes apart on its own.
    # Suppressing and un-suppressing leaves this same collection holding an UNSUPPRESSED op with no
    # toolpath: a time here rules the empty toolpath out as the cause.
    ops[-1].isSuppressed = True
    ops[-1].isSuppressed = False
    toolpath_gone = ops[-1].hasToolpath is False
    control, control_err = None, ""
    try:
        control = cam.getMachiningTime(clean, 100.0, 10.58, 1.5).machiningTime
    except Exception as exc:
        control_err = type(exc).__name__ + ": " + (str(exc).strip().splitlines() or [""])[0][:60]
    emit(toolpath_gone and control is not None and control > 0,
         "cam-machining-time-toolpathless-op-times: unsuppressed op with NO toolpath in the same"
         " collection -> " + (str(round(control, 2)) + "s" if control is not None else control_err)
         + " (its toolpath is gone=" + str(toolpath_gone) + ")")
""",
    },
    {
        "id": "cam-machining-time-suppressed-op-contributes-nothing",
        "claim": ("CAM.getMachiningTime over an ObjectCollection SUCCEEDS with a suppressed "
                  "operation in it, and the suppressed op CONTRIBUTES NOTHING: the collection with "
                  "one op suppressed times at the unsuppressed ops' own total (measured against "
                  "the same collection fully unsuppressed and against the suppressed op's own "
                  "per-op figure). This REFUTES the flag-breaks-the-call reading of the recorded "
                  "production-job failures beside _timeable_ops: those collections' op STATES were "
                  "never read, and one confound is now measured - an op left FAULTED regenerates "
                  "errored and fails the whole call (cam-errored-op-state-pair's recovery leg). "
                  "_timeable_ops' suppressed exclusion stands as a deterministic construction - "
                  "the excluded op would add nothing - not as an API necessity"),
        "encoded_in": ("_cam_read._timeable_ops / get_machining_time_handler and "
                       "tests/unit/test__cam_common.py TestMachiningTimeExcludesSuppressed"),
        "needs": "cam",
        "body": """
    import time as _t
    cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType("CAMProductType"))
    ops = []
    for i in range(cam.setups.count):
        if cam.setups.item(i).name == "MeasureSetup":
            for x in cam.setups.item(i).allOperations:
                o = adsk.cam.Operation.cast(x)
                if o is not None:
                    ops.append(o)

    def _generate(o):
        f = cam.generateToolpath(o)
        n = 0
        while not f.isGenerationCompleted and n < 600:
            adsk.doEvents()
            _t.sleep(0.1)
            n += 1
        return f.isGenerationCompleted

    for o in ops:
        o.isSuppressed = False
        if not _generate(o):
            emit(False, "cam-machining-time-suppressed-op-contributes-nothing: generation did not"
                 " complete in 60s - inconclusive, rerun")
            return
    args = (100.0, 10.58, 1.5)
    coll = adsk.core.ObjectCollection.create()
    for o in ops:
        coll.add(o)
    t_all = cam.getMachiningTime(coll, *args).machiningTime
    t_last = cam.getMachiningTime(ops[-1], *args).machiningTime
    # The discriminating leg: the SAME collection, its last op suppressed. The unsuppress runs in
    # a finally so no exit leaves a parked operation behind for the rest of the sweep.
    ops[-1].isSuppressed = True
    try:
        t_mixed = cam.getMachiningTime(coll, *args).machiningTime
    finally:
        ops[-1].isSuppressed = False
    contributes_nothing = abs(t_mixed - (t_all - t_last)) < 0.05
    emit(t_all > 0 and t_last > 0 and t_mixed > 0 and t_mixed < t_all and contributes_nothing,
         "cam-machining-time-suppressed-op-contributes-nothing: all ops "
         + str(round(t_all, 2)) + "s, last op alone " + str(round(t_last, 2))
         + "s, with last SUPPRESSED " + str(round(t_mixed, 2))
         + "s (the call SUCCEEDS and reads all-minus-last)")
""",
    },
    {
        "id": "cam-advanced-swarf-surface-set-editable",
        "claim": ("A fresh advanced_swarf operation reads advancedSwarfSurfaces isEditable True, "
                  "while swarfUpperContour reads isEditable False and checkSurfaceSelection does "
                  "not resolve through itemByName at all. Nor does ANY of the seven names "
                  "cam_select_geometry probes for a curve selection (contours, pockets, "
                  "swarfContours, edgeSel, curves, machiningBoundarySel, stockContours). So this "
                  "strategy is reachable through its ONE settable surface set, and through no curve "
                  "selection at all - the seven ABSENT names are why, not the contour reads, whose "
                  "isEditable is recorded as read and decides nothing. The op carries a tool and "
                  "sits in a FRESH setup: a tool-less op added under the harness raises a modal "
                  "'Failed to generate toolpath - no tool selected' dialog that parks the thread"),
        "encoded_in": ("cam_select_geometry._SURFACE_TARGET_PARAM's 'swarf' entry "
                       "(advancedSwarfSurfaces) with _surface_params' isEditable filter, which "
                       "make surface_target='swarf' the one settable role here; and _apply_curve's "
                       "no-curve-parameter refusal, which is what a chain/face selection meets on "
                       "this strategy and which hands back the surface set instead"),
        "needs": "cam",
        "body": """
    cam, setup = cam_measure_setup()
    # The pre-flight the no-blocked-op rule requires: creating an op on a strategy this flag reads
    # False for raises a modal licence dialog that parks the main thread.
    allowed = None
    for s in setup.operations.compatibleStrategies:
        if s.name == "advanced_swarf":
            allowed = s.isGenerationAllowed
    if allowed is not True:
        emit(False, "cam-advanced-swarf-surface-set-editable: advanced_swarf reads"
             " isGenerationAllowed=" + repr(allowed) + " - not created, inconclusive")
        return
    si = cam.setups.createInput(adsk.cam.OperationTypes.MillingOperation)
    si.models = list(setup.models)
    own = cam.setups.add(si)
    own.name = "MeasureAdvSwarfSetup"
    tool = cam_sample_tool()
    if tool is None:
        emit(False, "cam-advanced-swarf-surface-set-editable: no bundled sample milling library"
             " - inconclusive")
        return
    opin = own.operations.createInput("advanced_swarf")
    opin.tool = tool
    op = own.operations.add(opin)
    op.name = "MeasureAdvSwarf"
    drive = op.parameters.itemByName("advancedSwarfSurfaces")
    upper = op.parameters.itemByName("swarfUpperContour")
    check = op.parameters.itemByName("checkSurfaceSelection")
    drive_editable = None if drive is None else drive.isEditable
    upper_editable = None if upper is None else upper.isEditable
    # _CURVE_PARAM_CANDIDATES, in the tool's own probe order: a name resolving here would mean a
    # chain/face selection lands somewhere on this op rather than reaching the refusal.
    curve_hits = [nm for nm in ("contours", "pockets", "swarfContours", "edgeSel", "curves",
                                "machiningBoundarySel", "stockContours")
                  if op.parameters.itemByName(nm) is not None]
    emit(drive is not None and drive_editable is True and upper is not None
         and upper_editable is False and check is None and not curve_hits,
         "cam-advanced-swarf-surface-set-editable: advancedSwarfSurfaces present="
         + str(drive is not None) + " isEditable=" + repr(drive_editable)
         + " | swarfUpperContour present=" + str(upper is not None) + " isEditable="
         + repr(upper_editable) + " | checkSurfaceSelection resolves=" + str(check is not None)
         + " | curve-probe names that resolve=" + str(curve_hits) + " (expect none)")
""",
    },
    {
        "id": "cam-curves-parameter-carriers",
        "claim": ("A fresh trace, multi_axis_contour, morph and project operation each resolve "
                  "parameters.itemByName('curves'), and those four are the only ones of the five "
                  "measured here that do. Morph and project ALSO resolve machiningBoundarySel, and "
                  "'curves' is still the FIRST of cam_select_geometry's seven probe names either of "
                  "them resolves - so the probe order is what decides where a chain selection lands "
                  "on those two. A fresh pocket2d resolves neither 'curves' nor "
                  "machiningBoundarySel. The message records, per strategy, every one of the seven "
                  "names that resolves, in probe order. Every op carries a tool and is created with "
                  "generationMode SkipGeneration in its own setup: a create left on the platform's "
                  "user preference can generate at once and park the main thread behind a modal "
                  "dialog"),
        "encoded_in": ("cam_select_geometry._CURVE_PARAM_CANDIDATES, whose order puts "
                       "_DRIVE_CURVES_PARAM ('curves') ahead of _MACHINING_BOUNDARY_PARAM so a "
                       "morph/project chain reaches the drive curves rather than the boundary; "
                       "tests/unit/test_cam_select_geometry.py's operation fakes, which name the "
                       "parameters each strategy carries"),
        "needs": "cam",
        "body": """
    cam, setup = cam_measure_setup()
    tool = cam_sample_tool()
    if tool is None:
        emit(False, "cam-curves-parameter-carriers: no bundled sample milling library"
             " - inconclusive")
        return
    wanted = ("trace", "multi_axis_contour", "morph", "project", "pocket2d")
    allowed = {}
    for s in setup.operations.compatibleStrategies:
        if s.name in wanted:
            allowed[s.name] = s.isGenerationAllowed
    # The pre-flight the no-blocked-op rule requires: creating an op on a strategy this flag reads
    # False for raises a modal licence dialog that parks the main thread.
    blocked = [nm + "=" + repr(allowed.get(nm)) for nm in wanted if allowed.get(nm) is not True]
    if blocked:
        emit(False, "cam-curves-parameter-carriers: isGenerationAllowed is not True for "
             + ", ".join(blocked) + " - nothing created, inconclusive")
        return
    si = cam.setups.createInput(adsk.cam.OperationTypes.MillingOperation)
    si.models = list(setup.models)
    own = cam.setups.add(si)
    own.name = "MeasureCurvesSetup"
    # _CURVE_PARAM_CANDIDATES in the tool's own probe order - the first hit is where a chain lands.
    order = ("contours", "pockets", "swarfContours", "edgeSel", "curves",
             "machiningBoundarySel", "stockContours")
    census = {}
    for nm in wanted:
        opin = own.operations.createInput(nm)
        opin.tool = tool
        opin.generationMode = adsk.cam.AutomaticGenerationModes.SkipGeneration
        op = own.operations.add(opin)
        census[nm] = [p for p in order if op.parameters.itemByName(p) is not None]
    with_curves = [nm for nm in wanted if "curves" in census[nm]]
    first = dict((nm, (census[nm][0] if census[nm] else None)) for nm in wanted)
    emit(with_curves == ["trace", "multi_axis_contour", "morph", "project"]
         and "machiningBoundarySel" in census["morph"]
         and "machiningBoundarySel" in census["project"]
         and first["morph"] == "curves" and first["project"] == "curves"
         and "curves" not in census["pocket2d"]
         and "machiningBoundarySel" not in census["pocket2d"],
         "cam-curves-parameter-carriers: "
         + "; ".join(nm + " -> " + ",".join(census[nm] or ["(none)"]) for nm in wanted))
""",
    },
    {
        "id": "cam-ncprogram-operations-hold-containers",
        "claim": ("NCProgram.operations holds what was ASSIGNED - a setup assigned to the program "
                  "reads back as an element whose objectType is adsk::cam::Setup, NOT its "
                  "operations - while NCProgram.filteredOperations holds adsk::cam::Operation "
                  "elements. So len(NCProgram.operations) is an ITEM count and the operations "
                  "figure is the filtered read"),
        "encoded_in": ("cam_post._program_counts (program_operation_count off filteredOperations, "
                       "program_item_count off operations) and _cam_read.get_nc_programs_handler's "
                       "operation_count/item_count pair; tests/unit/test_cam_post.py _NCProgram, "
                       "whose filteredOperations expands the stored containers"),
        "needs": "cam",
        "body": """
    cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType("CAMProductType"))
    setup = None
    for i in range(cam.setups.count):
        if cam.setups.item(i).name == "MeasureSetup":
            setup = cam.setups.item(i)
    nc_input = cam.ncPrograms.createInput()
    nc_input.displayName = "MeasureNCItems"
    # The SETUP itself is what a scoped post assigns - the shape cam_post sends.
    nc_input.operations = [setup]
    prog = cam.ncPrograms.add(nc_input)
    try:
        stored = [x.objectType for x in prog.operations]
        posted = [x.objectType for x in prog.filteredOperations]
        walked = len([x for x in setup.allOperations])
        emit(len(stored) == 1 and stored[0].endswith("Setup") and len(posted) >= 1
             and all(t.endswith("Operation") for t in posted),
             "cam-ncprogram-operations-hold-containers: operations=" + str(stored)
             + " filteredOperations=" + str(len(posted)) + "x" + str(sorted(set(posted)))
             + " setup walks " + str(walked) + " operations")
    finally:
        prog.deleteMe()
""",
    },
    {
        "id": "cam-template-library-deleteasset",
        "claim": ("templateLibrary.importTemplate stores a template built from live operations "
                  "into the Local location under a leafName whose STEM is the template's name; "
                  "templateAtURL loads it back; deleteAsset(url) returns True and a re-walk of "
                  "childAssetURLs no longer lists it - the ASSET WALK is what confirms the "
                  "delete, and it is the read cam_delete_template rests its claim on. "
                  "templateAtURL on the DELETED address is not part of that proof: it raises "
                  "rather than answering null (cam-templateaturl-raises-on-deleted-url). "
                  "Self-cleaning: the template this row imports is the one it deletes"),
        "encoded_in": ("cam_delete_template.py's read-backs and its sibling-inference comment; "
                       "save-side importTemplate gates"),
        "needs": "cam",
        "body": """
    # Each step names itself before it runs: the runner truncates a traceback's inner frame, so a
    # bare raise here reports a line number that cannot be resolved back to a step.
    stage = "start"
    try:
        stage = "resolve-cam"
        cam = adsk.cam.CAM.cast(
            app.activeDocument.products.itemByProductType("CAMProductType"))
        stage = "find-setup"
        setup = None
        for i in range(cam.setups.count):
            if cam.setups.item(i).name == "MeasureSetup":
                setup = cam.setups.item(i)
        stage = "collect-ops"
        ops = [adsk.cam.Operation.cast(x) for x in setup.allOperations]
        ops = [o for o in ops if o is not None][:1]
        stage = "createFromOperations(" + str(len(ops)) + " ops)"
        result = adsk.cam.CAMTemplate.createFromOperations(ops)
        # The live API is self-contradictory here: the docstring says a CAMTemplate returns, the
        # annotation says list[Operation] - so the return is DESCRIBED and type-GATED rather than
        # assumed. Handing importTemplate a non-CAMTemplate aborts the whole invocation, taking
        # every printed verdict with it, so the shape is proven before it is passed on.
        shape = type(result).__name__
        if isinstance(result, list):
            shape += "[" + str(len(result)) + "]"
            shape += " of " + (type(result[0]).__name__ if result else "-")
        tmpl = adsk.cam.CAMTemplate.cast(result)
        if tmpl is None and isinstance(result, list) and result:
            tmpl = adsk.cam.CAMTemplate.cast(result[0])
        if tmpl is None:
            emit(False, "cam-template-library-deleteasset: createFromOperations returned "
                 + shape + " - no CAMTemplate casts out of it, so the import is not attempted")
            return
        stage = "set-name (returned " + shape + ")"
        tmpl.name = "MeasureTmplDel"
        stage = "library"
        lib = adsk.cam.CAMManager.get().libraryManager.templateLibrary
        local = lib.urlByLocation(adsk.cam.LibraryLocations.LocalLibraryLocation)
        stage = "importTemplate"
        url = lib.importTemplate(tmpl, local)
        stored = bool(url)
        leaf = url.leafName if stored else ""
        stem = leaf.rpartition(".")[0] if "." in leaf else leaf
        stem_matches = stem == "MeasureTmplDel"
        stage = "templateAtURL"
        loaded = stored and (lib.templateAtURL(url) is not None)
        stage = "deleteAsset"
        ok = stored and lib.deleteAsset(url)
        stage = "re-walk"
        still = stored and any(u.leafName == leaf for u in lib.childAssetURLs(local))
        # templateAtURL is NOT called on the deleted url here: it raises rather than answering
        # null - the separate cam-templateaturl-raises-on-deleted-url row measures that raise, in
        # its own script, where a raise that escapes costs one row's output instead of this one's.
        # The asset WALK is the read this delete is confirmed by, which is the same read
        # cam_delete_template rests its claim on.
        emit(stored and stem_matches and loaded and ok is True and not still,
             "cam-template-library-deleteasset: stored=" + str(stored) + " leaf=" + repr(leaf)
             + " stem_matches_name=" + str(stem_matches) + " loaded_back=" + str(loaded)
             + " deleteAsset=" + str(ok) + " still_listed=" + str(still))
    except Exception as exc:
        emit(False, "cam-template-library-deleteasset: RAISED at stage '" + stage + "': "
             + type(exc).__name__ + ": " + str(exc)[:160])
""",
    },
    {
        "id": "cam-templateaturl-raises-on-deleted-url",
        "claim": ("CAMTemplateLibrary.templateAtURL does NOT honour its own docstring's 'Returns "
                  "null if the specified template does not exist' for a url whose asset was just "
                  "deleted: it raises RuntimeError, and the message names '3 : Given URL does not "
                  "point to a template'. So a delete's read-back cannot ask this to confirm "
                  "absence - the asset WALK must - and any caller reading it needs safe(). This "
                  "row catches the raise and gates on its message; the expect also accepts a "
                  "script-level abort, so a PASS does not say which of the two the run saw"),
        "encoded_in": ("cam_delete_template.py's safe()-wrapped loads_after read and the comment "
                       "naming the asset walk as the load-bearing leg"),
        "needs": "cam",
        "expect": "raise_or_abort",
        "body": """
    cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType("CAMProductType"))
    setup = None
    for i in range(cam.setups.count):
        if cam.setups.item(i).name == "MeasureSetup":
            setup = cam.setups.item(i)
    ops = [adsk.cam.Operation.cast(x) for x in setup.allOperations]
    ops = [o for o in ops if o is not None][:1]
    result = adsk.cam.CAMTemplate.createFromOperations(ops)
    tmpl = adsk.cam.CAMTemplate.cast(result)
    if tmpl is None and isinstance(result, list) and result:
        tmpl = adsk.cam.CAMTemplate.cast(result[0])
    if tmpl is None:
        emit(False, "cam-templateaturl-raises-on-deleted-url: no CAMTemplate to import")
        return
    tmpl.name = "MeasureTmplRaise"
    lib = adsk.cam.CAMManager.get().libraryManager.templateLibrary
    local = lib.urlByLocation(adsk.cam.LibraryLocations.LocalLibraryLocation)
    url = lib.importTemplate(tmpl, local)
    if not url or not lib.deleteAsset(url):
        emit(False, "cam-templateaturl-raises-on-deleted-url: could not stage a deleted url")
        return
    # The misuse: the asset at this url is gone. A null return here would REFUTE the claim.
    try:
        r = lib.templateAtURL(url)
        emit(False, "cam-templateaturl-raises-on-deleted-url: returned " + repr(r)
             + " with no raise - the docstring's null contract holds after all")
    except Exception as e:
        msg = str(e).strip().splitlines()[0]
        emit(type(e).__name__ == "RuntimeError" and "does not point to a template" in msg,
             "cam-templateaturl-raises-on-deleted-url: raised catchably "
             + type(e).__name__ + ": " + msg[:80])
""",
    },
    {
        "id": "cam-ncprogram-filtered-ops-tie-by-operationid",
        "claim": ("An adsk.cam.Operation carries NO entityToken (the read raises AttributeError) "
                  "- but an Operation from NCProgram.filteredOperations still ties to the same "
                  "operation reached through the setup walk: operationId matches and == is True "
                  "for every filtered op paired by name with its walked twin, while wrapper "
                  "identity 'is' does not carry it"),
        "encoded_in": ("_cam_read._program_held_ops position note - filteredOperations hands "
                       "back Operations with no walk, and operationId is the measured tie a "
                       "breadcrumb beside the position discriminator would run on"),
        "needs": "cam",
        "body": """
    import time as _t
    cam = adsk.cam.CAM.cast(app.activeDocument.products.itemByProductType("CAMProductType"))
    setup = None
    for i in range(cam.setups.count):
        if cam.setups.item(i).name == "MeasureSetup":
            setup = cam.setups.item(i)
    walked = [adsk.cam.Operation.cast(x) for x in setup.allOperations]
    walked = [o for o in walked if o is not None]
    def _generate(o):
        f = cam.generateToolpath(o)
        n = 0
        while not f.isGenerationCompleted and n < 600:
            adsk.doEvents()
            _t.sleep(0.1)
            n += 1
        return f.isGenerationCompleted
    for o in walked:
        o.isSuppressed = False
        if not o.hasToolpath and not _generate(o):
            emit(False, "cam-ncprogram-filtered-ops-tie-by-operationid: generation did not"
                 " complete in 60s - inconclusive, rerun")
            return
    no_token = 0
    for o in walked:
        try:
            o.entityToken
        except AttributeError:
            no_token += 1
    nc_input = cam.ncPrograms.createInput()
    nc_input.displayName = "MeasureNC"
    # NCProgramInput.operations is a vector<OperationBase> setter: a plain Python LIST,
    # never an ObjectCollection (which raises) - same fact cam_post._operations_collection pins.
    nc_input.operations = list(walked)
    prog = cam.ncPrograms.add(nc_input)
    try:
        filtered = [adsk.cam.Operation.cast(x) for x in prog.filteredOperations]
        filtered = [o for o in filtered if o is not None]
        pairs = 0
        id_ties = 0
        eq_ties = 0
        is_ties = 0
        for f in filtered:
            for w in walked:
                if w.name == f.name:
                    pairs += 1
                    if f.operationId == w.operationId:
                        id_ties += 1
                    if f == w:
                        eq_ties += 1
                    if f is w:
                        is_ties += 1
        emit(no_token == len(walked) and pairs > 0 and id_ties == pairs and eq_ties == pairs,
             "cam-ncprogram-filtered-ops-tie-by-operationid: no-entityToken=" + str(no_token)
             + "/" + str(len(walked)) + " name-paired=" + str(pairs) + " operationId-ties="
             + str(id_ties) + " eq-ties=" + str(eq_ties) + " wrapper-is-ties=" + str(is_ties))
    finally:
        prog.deleteMe()
""",
    },
    {
        "id": "cam-inspection-results-count-zero",
        "claim": ("A CAM product that has never recorded a probing result reads inspectionResults "
                  "as a CAMInspectionResults whose count is 0 - NOT None - while a document "
                  "carrying no CAM product never reaches that read at all: "
                  "products.itemByProductType('CAMProductType') RAISES '3 : failed to find "
                  "product' on a fresh design document. A None inspectionResults, or a "
                  "no-CAM-product document answering None instead of raising, refutes"),
        "encoded_in": ("tests/fakes/cam.py make_inspection_cam, whose measures=None models the "
                       "property answering None; cam_inspect_toolpaths' inspection reads"),
        "needs": "cam",
        "body": """
    cam, _setup = cam_measure_setup()
    results = cam.inspectionResults
    kind = type(results).__name__
    n = None if results is None else results.count
    # the OTHER document shape, in its own scratch: a design that never entered manufacture
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    answered = "NO RAISE"
    try:
        try:
            got = tmp.products.itemByProductType("CAMProductType")
            answered = "returned " + repr(got)
        except Exception as e:
            answered = type(e).__name__ + ": " + (str(e).strip().splitlines() or [""])[0][:60]
    finally:
        tmp.close(False)
    emit(kind == "CAMInspectionResults" and n == 0 and "failed to find product" in answered,
         "cam-inspection-results-count-zero: inspectionResults type=" + kind + " count="
         + repr(n) + " (expect CAMInspectionResults / 0); a document with no CAM product: "
         + answered + " (expect the 'failed to find product' raise)")
""",
    },
    {
        "id": "cam-generate-all-skips-suppressed",
        "claim": ("CAM.generateAllToolpaths(True) SKIPS a suppressed operation: with one op "
                  "suppressed (hasToolpath already False, since suppressing DISCARDS the "
                  "toolpath), the sweep runs to isGenerationCompleted and that op still reads "
                  "hasToolpath False, operationState 2 and isSuppressed True. A hasToolpath True "
                  "or an operationState 0 on it afterwards would say the sweep regenerated it"),
        "encoded_in": ("cam_generate.py's all-scope arm and tests/unit/test_cam_generate.py; "
                       "_cam_common.op_primary_state, which buckets a suppressed op before it "
                       "reads any toolpath flag"),
        "needs": "cam",
        "body": """
    import time as _t
    cam, setup = cam_measure_setup()
    op = None
    for x in setup.allOperations:
        o = adsk.cam.Operation.cast(x)
        if o is not None and o.name == "Face2":
            op = o
    op.isSuppressed = True
    try:
        before_path = op.hasToolpath
        future = cam.generateAllToolpaths(True)
        waited = 0
        while not future.isGenerationCompleted and waited < 900:
            adsk.doEvents()
            _t.sleep(0.1)
            waited += 1
        if not future.isGenerationCompleted:
            emit(False, "cam-generate-all-skips-suppressed: the sweep did not complete in 90s"
                 " - inconclusive, rerun")
            return
        after_path = op.hasToolpath
        after_state = op.operationState
        after_flag = op.isSuppressed
    finally:
        op.isSuppressed = False
    emit(before_path is False and after_path is False and after_state == 2
         and after_flag is True,
         "cam-generate-all-skips-suppressed: suppressed op hasToolpath " + repr(before_path)
         + " -> " + repr(after_path) + " across the sweep (expect False on both sides),"
         + " operationState " + str(after_state) + " (expect 2), isSuppressed "
         + repr(after_flag) + " (expect True)")
""",
    },
    {
        "id": "cam-suppress-clears-fault-channel",
        "claim": ("Suppressing an ERRORED operation CLEARS its fault channel: an op faulted by a "
                  "bottom height above its top reads hasError True with operationState 3, and "
                  "with isSuppressed set True the same op reads hasError False, error '' and "
                  "operationState 2 - the error text is gone from the op, not carried beside the "
                  "suppression. A hasError still True under suppression refutes. The row restores "
                  "the op: the fault is corrected and regenerated clean before it returns"),
        "encoded_in": ("_cam_common.op_primary_state / op_state_facts, which bucket a suppressed "
                       "op before reading its error; cam_inspect_toolpaths' suppressed split"),
        "needs": "cam",
        "body": """
    import time as _t
    cam, setup = cam_measure_setup()
    op = None
    for x in setup.allOperations:
        o = adsk.cam.Operation.cast(x)
        if o is not None and o.name == "Face1":
            op = o
    def _generate(o):
        f = cam.generateToolpath(o)
        n = 0
        while not f.isGenerationCompleted and n < 600:
            adsk.doEvents()
            _t.sleep(0.1)
            n += 1
        return f.isGenerationCompleted
    # A bottom offset ABOVE the top height is the fault the generator rejects - the same rig the
    # errored-state row runs on, so this measures the SUPPRESSION and not a new fault shape.
    op.parameters.itemByName("bottomHeight_offset").expression = "50 mm"
    if not _generate(op):
        op.parameters.itemByName("bottomHeight_offset").expression = "0 mm"
        _generate(op)
        emit(False, "cam-suppress-clears-fault-channel: fault generation did not complete in 60s"
             " - inconclusive (fault restored), rerun")
        return
    faulted = (op.hasError, op.operationState, (op.error or "").strip().splitlines() or [""])
    op.isSuppressed = True
    cleared = (op.hasError, op.error, op.operationState, op.isSuppressed)
    op.isSuppressed = False
    op.parameters.itemByName("bottomHeight_offset").expression = "0 mm"
    if not _generate(op):
        emit(False, "cam-suppress-clears-fault-channel: restore generation did not complete in"
             " 60s - Face1 LEFT ERRORED, rerun before any machining-time row")
        return
    clean = op.hasError is False and op.operationState == 0 and op.hasToolpath is True
    emit(faulted[0] is True and faulted[1] == 3 and cleared[0] is False and cleared[1] == ""
         and cleared[2] == 2 and cleared[3] is True and clean,
         "cam-suppress-clears-fault-channel: faulted=(hasError " + repr(faulted[0])
         + ", state " + str(faulted[1]) + ", error " + repr(faulted[2][0][:40])
         + ") suppressed=(hasError " + repr(cleared[0]) + ", error " + repr(cleared[1])
         + ", state " + str(cleared[2]) + ", isSuppressed " + repr(cleared[3])
         + ") (expect False / '' / 2 / True) restored_clean=" + repr(clean))
""",
    },
    {
        "id": "cam-deleted-machine-keeps-setup-copy",
        "claim": ("A setup KEEPS its own copy of an assigned machine after that machine is "
                  "deleted from the library: deleteAsset(url) returns True and the asset is gone "
                  "from the Local walk, while Setup.machine still reads the same label it read "
                  "before the delete. A Setup.machine answering None, or a label that changed, "
                  "refutes. Self-cleaning: the Local machine this row imports is the one it "
                  "deletes, and it touches no other library asset"),
        "encoded_in": ("cam_delete_machine.py's asset-walk read-back, which is what a delete is "
                       "confirmed by; cam_edit_setup's machine assignment (Setup.machine takes a "
                       "transient copy)"),
        "needs": "cam",
        "body": """
    cam, setup = cam_measure_setup()
    lib = adsk.cam.CAMManager.get().libraryManager.machineLibrary
    local = lib.urlByLocation(adsk.cam.LibraryLocations.LocalLibraryLocation)
    machine = adsk.cam.Machine.createFromTemplate(adsk.cam.MachineTemplate.Generic3Axis)
    machine.description = "MeasureKeepsCopy"
    machine.model = "MeasureKeepsCopy"
    url = lib.importMachine(machine, local, "MeasureKeepsCopy")
    if not url:
        emit(False, "cam-deleted-machine-keeps-setup-copy: importMachine stored nothing"
             " - inconclusive")
        return
    leaf = url.leafName
    stored = lib.machineAtURL(url)
    if stored.hasSimulationModel:
        stored.clearSimulationModel()
    setup.machine = stored
    label_before = None if setup.machine is None else setup.machine.description
    deleted = lib.deleteAsset(url)
    listed = [u.leafName for u in lib.childAssetURLs(local)]
    label_after = None if setup.machine is None else setup.machine.description
    emit(label_before == "MeasureKeepsCopy" and deleted is True and leaf not in listed
         and label_after == "MeasureKeepsCopy",
         "cam-deleted-machine-keeps-setup-copy: assigned label=" + repr(label_before)
         + " deleteAsset=" + repr(deleted) + " asset " + repr(leaf) + " gone from the Local walk="
         + repr(leaf not in listed) + " setup.machine label after the delete="
         + repr(label_after) + " (expect the same label; None or a changed label refutes)")
""",
    },
    {
        "id": "cam-operation-strategy-vector-shape",
        "claim": ("Setup.operations.compatibleStrategies is a raw std::vector binding: len() and "
                  "[i] answer while hasattr(vec, 'count') and hasattr(vec, 'item') are both False, "
                  "so it is walked by ITERATION. Its elements carry name/title/isGenerationAllowed "
                  "and all ELEVEN long classification spellings _STRATEGY_FLAGS reads "
                  "(is2DStrategy ... isSuppressible); the short ones (isCutting, is2D, "
                  "isDrilling) raise AttributeError, so a row reading them would publish nothing"),
        "encoded_in": ("cam_create_operation._compatible_strategies (list() over the vector) and "
                       "_STRATEGY_FLAGS, the wire-key -> API-property table cam_get's strategies "
                       "slice publishes; tests/unit/test_cam_create_operation.py's vector fake"),
        "needs": "cam",
        "body": """
    cam, setup = cam_measure_setup()
    vec = setup.operations.compatibleStrategies
    n = len(vec)
    has_count = hasattr(vec, "count")
    has_item = hasattr(vec, "item")
    if not n:
        emit(False, "cam-operation-strategy-vector-shape: the vector is empty - inconclusive")
        return
    first = vec[0]
    long_reads = []
    # every API property _STRATEGY_FLAGS publishes, so a spelling the table gets wrong shows up
    # here as <AttributeError> instead of as a silently absent wire key
    for prop in ("is2DStrategy", "is3DStrategy", "isDrillingStrategy", "isMillingStrategy",
                 "isRotaryStrategy", "isTurningStrategy", "isFinishingStrategy",
                 "isAdditiveStrategy", "isCuttingStrategy", "isSupportStrategy",
                 "isSuppressible"):
        try:
            long_reads.append(prop + "=" + repr(getattr(first, prop)))
        except AttributeError:
            long_reads.append(prop + "=<AttributeError>")
    short_answered = []
    for prop in ("isCutting", "is2D", "isDrilling"):
        try:
            getattr(first, prop)
            short_answered.append(prop)
        except AttributeError:
            pass
    emit(n > 0 and has_count is False and has_item is False and not short_answered
         and "<AttributeError>" not in " ".join(long_reads),
         "cam-operation-strategy-vector-shape: len=" + str(n) + " [0].name="
         + repr(first.name) + " isGenerationAllowed=" + repr(first.isGenerationAllowed)
         + " hasattr count=" + str(has_count) + " item=" + str(has_item) + " | "
         + ", ".join(long_reads) + " | short spellings that answer=" + str(short_answered))
""",
    },
    {
        "id": "cam-operation-input-generation-mode",
        "claim": ("OperationInput carries a generationMode property that takes an "
                  "AutomaticGenerationModes member and reads the assigned member back, and the "
                  "family carries exactly three int members (ForceGeneration, SkipGeneration, "
                  "UserPreference). The message records the FACTORY value the input arrives with - "
                  "the mode a create that assigns nothing runs under. Measured on an input that is "
                  "never added, so this row creates no operation"),
        "encoded_in": ("cam_create_operation's SkipGeneration assignment on the non-generating "
                       "path (its read-back publishes payload key generation_mode_note only on a "
                       "disagreement) and cam_apply_template._GEN_MODES, the "
                       "friendly-key -> member table cam_apply_template sets on "
                       "CreateFromCAMTemplateInput.mode; tests/unit/test_cam_create_operation.py's "
                       "OperationInput fake"),
        "needs": "cam",
        "body": """
    cam, setup = cam_measure_setup()
    M = adsk.cam.AutomaticGenerationModes
    dump_enum("cam.AutomaticGenerationModes", M)
    members = sorted(n for n in dir(M) if not n.startswith("_") and isinstance(getattr(M, n), int))
    # READ ONLY: the input is built and never added, so no operation is created here and no
    # generation can be attempted off it.
    opin = setup.operations.createInput("face")
    if not hasattr(opin, "generationMode"):
        emit(False, "cam-operation-input-generation-mode: OperationInput carries no"
             " generationMode on this build")
        return
    factory = opin.generationMode
    opin.generationMode = M.SkipGeneration
    after = opin.generationMode
    emit(after == M.SkipGeneration and len(members) == 3
         and M.SkipGeneration != M.ForceGeneration and M.SkipGeneration != M.UserPreference,
         "cam-operation-input-generation-mode: factory generationMode=" + repr(factory)
         + " after assigning SkipGeneration=" + repr(after) + " | members=" + str(members)
         + " Force=" + str(M.ForceGeneration) + " Skip=" + str(M.SkipGeneration)
         + " UserPreference=" + str(M.UserPreference))
""",
    },
    {
        "id": "cam-blocked-strategy-op-facts",
        "claim": ("The entitlement flag ANSWERS per strategy: 'chamfer' in a milling setup's "
                  "compatibleStrategies reads a bool for isGenerationAllowed rather than null, and "
                  "on this install it reads False. The message records which value this run saw - "
                  "True is an entitled install with no blocked strategy to measure, not a "
                  "refutation; only a flag that does not read refutes. MEASURED BY HAND, and "
                  "deliberately NOT re-measured by any row: an op on a blocked "
                  "strategy is created normally (operations.add lands it) and reads hasToolpath "
                  "False, operationState 3 (NoToolpath), isValid True, error '' and warning '' - "
                  "the pre-generate shape cam_generate's entitlement arm EXCLUDES rather than "
                  "launching. No row creates one, because on 2705.1.4 creating a blocked-strategy "
                  "op raises a modal licence dialog at the next event pump that parks Fusion's "
                  "main thread until a human closes it"),
        "encoded_in": ("cam_select_geometry._BLOCKED_GENERATE and cam_generate's entitlement "
                       "pre-flight, both gated on strategy_generation_allowed; cam_get's "
                       "strategies note"),
        "needs": "cam",
        "body": """
    cam, setup = cam_measure_setup()
    # READ ONLY: nothing is created here. Creating an op on a strategy this flag reads False for
    # raises a modal dialog that parks the main thread, so the flag is where the row stops.
    allowed = None
    seen = False
    for s in setup.operations.compatibleStrategies:
        if s.name == "chamfer":
            seen = True
            allowed = s.isGenerationAllowed
    emit(seen and isinstance(allowed, bool),
         "cam-blocked-strategy-op-facts: 'chamfer' offered by the setup=" + str(seen)
         + " isGenerationAllowed=" + repr(allowed)
         + " (False = blocked on this install, the case the wire strings describe; True = an"
         + " entitled install, so there is no blocked op to read create-time facts off)")
""",
    },
    {
        "id": "cam-name-collisions",
        "claim": ("Two CAM name writes, each read back off the object written. A second SETUP "
                  "cannot take the name a sibling setup carries: the write reads back as some "
                  "OTHER name (an exact match on the first setup's name refutes). An OPERATION "
                  "renamed onto a sibling's name under one setup is recorded as it reads - both "
                  "the taken and the deduped shape pass, and the receipt carries which one this "
                  "run got; a name that does not read back at all refutes. The renamed operation "
                  "is LEFT IN PLACE: Operation.deleteMe on an op the platform just deduped, in the "
                  "same script, kills the Fusion process (measured six times); the twin setup and "
                  "the never-renamed operation are deleted"),
        "encoded_in": ("_cam_common.resolve_cam_node's ambiguity refusal and its '<name>#<n>' "
                       "addresses, which exist for same-named CAM nodes; cam_create_operation's "
                       "operation report"),
        "needs": "cam",
        "body": """
    cam, setup = cam_measure_setup()
    tool = cam_sample_tool()
    if tool is None:
        emit(False, "cam-name-collisions: no bundled sample milling library - inconclusive")
        return
    first = cam_add_face_op(setup, tool, "MeasureNameA")
    second = cam_add_face_op(setup, tool, "MeasureNameB")
    second.name = "MeasureNameA"
    sibling_name = first.name
    op_read = second.name
    si = cam.setups.createInput(adsk.cam.OperationTypes.MillingOperation)
    twin = cam.setups.add(si)
    setup_name = setup.name
    twin.name = setup_name
    setup_read = twin.name
    twin.deleteMe()
    first.deleteMe()
    emit(isinstance(op_read, str) and isinstance(setup_read, str) and setup_read != setup_name,
         "cam-name-collisions: an operation renamed onto its sibling's name reads back "
         + repr(op_read) + " (the sibling reads " + repr(sibling_name) + ", shares it="
         + repr(op_read == sibling_name) + "); a second setup named " + repr(setup_name)
         + " reads back " + repr(setup_read) + " (expect NOT that name); the renamed op "
         + repr(op_read) + " is left in the setup")
""",
    },
    {
        "id": "cam-empty-name-write",
        "claim": ("Assigning an EMPTY STRING to the name of an operation, a folder and a setup "
                  "reads back a string from each: the empty string, the prior name, or a name the "
                  "platform substitutes (a folder reads back '1') - the receipt records which, "
                  "plus the refusal text where one raised. A name that does not read back as a "
                  "string refutes. The writes are provoked in their OWN row: a raise or a "
                  "rolled-back transaction costs this measurement and no other. The three nodes "
                  "are LEFT IN PLACE (deleteMe on a node the platform just renamed, in the same "
                  "script, kills Fusion - measured); the run closes the scratch document after "
                  "this last row"),
        "encoded_in": ("_cam_common._segment / _UNREAD_SEGMENT, the breadcrumb's empty-name "
                       "branch; cam_edit_folders' name read-backs"),
        "needs": "cam",
        "body": """
    cam, setup = cam_measure_setup()
    tool = cam_sample_tool()
    if tool is None:
        emit(False, "cam-empty-name-write: no bundled sample milling library - inconclusive")
        return
    op = cam_add_face_op(setup, tool, "MeasureEmptyOp")
    folder = setup.folders.addFolder("MeasureEmptyFolder")
    si = cam.setups.createInput(adsk.cam.OperationTypes.MillingOperation)
    own = cam.setups.add(si)
    own.name = "MeasureEmptySetup"
    reads = []
    clean = []
    for label, obj in (("operation", op), ("folder", folder), ("setup", own)):
        prior = obj.name
        why = ""
        try:
            obj.name = ""
        except Exception as e:
            why = " refused " + type(e).__name__ + ": " + (
                str(e).strip().splitlines() or [""])[0][:40]
        after = obj.name
        clean.append(isinstance(after, str))
        reads.append(label + " " + repr(prior) + " -> " + repr(after) + why)
    emit(len(reads) == 3 and all(clean),
         "cam-empty-name-write: " + "; ".join(reads)
         + " | each read back a string=" + str(clean))
""",
    },
    {
        "id": "pattern-elements-count-equals-quantity",
        "claim": ("A circular pattern made with quantity 6 answers patternElements.count == 6 and "
                  "leaves 6 bodies, and a 2 x 3 rectangular pattern answers 6: the element count "
                  "INCLUDES the seed, so a read-back that disagrees with the requested quantity "
                  "is a real shortfall, never an off-by-one"),
        "encoded_in": ("model_pattern_circular's and model_pattern_rectangular's rung-3 refusal on "
                       "real_total != quantity; tests/unit/test_model_pattern_circular.py and "
                       "test_model_pattern_rectangular.py feature stubs whose patternElements are "
                       "sized off the request"),
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        des = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        root = des.rootComponent
        sk = root.sketches.add(root.xYConstructionPlane)
        P = adsk.core.Point3D.create
        sk.sketchCurves.sketchLines.addTwoPointRectangle(P(2, 0, 0), P(3, 1, 0))
        ext = root.features.extrudeFeatures.addSimple(
            sk.profiles.item(0), adsk.core.ValueInput.createByReal(1.0),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        coll = adsk.core.ObjectCollection.create()
        coll.add(ext.bodies.item(0))
        cp = root.features.circularPatternFeatures
        ci = cp.createInput(coll, root.zConstructionAxis)
        ci.quantity = adsk.core.ValueInput.createByReal(6)
        ci.totalAngle = adsk.core.ValueInput.createByString("360 deg")
        circ = cp.add(ci).patternElements.count
        bodies = root.bRepBodies.count
        rp = root.features.rectangularPatternFeatures
        ri = rp.createInput(coll, root.xConstructionAxis, adsk.core.ValueInput.createByReal(2),
                            adsk.core.ValueInput.createByReal(5.0),
                            adsk.fusion.PatternDistanceType.SpacingPatternDistanceType)
        ri.setDirectionTwo(root.yConstructionAxis, adsk.core.ValueInput.createByReal(3),
                           adsk.core.ValueInput.createByReal(5.0))
        rect = rp.add(ri).patternElements.count
        emit(circ == 6 and bodies == 6 and rect == 6,
             "pattern-elements-count-equals-quantity: circular quantity 6 -> elements "
             + str(circ) + " bodies " + str(bodies) + " | rectangular 2x3 -> elements " + str(rect))
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "sketch-profiles-under-compute-deferred",
        "claim": ("With isComputeDeferred True, Sketch.profiles still answers and holds the "
                  "regions closed BEFORE the deferral: two regions read 2, a third region drawn "
                  "while deferred still reads 2, and 3 once compute resumes"),
        "encoded_in": ("tests/fakes/sketch.py make_sketch(profiles=, is_compute_deferred=) - the "
                       "profile list a deferred sketch hands a tool is the stale one; the arrange "
                       "and extrude deferral refusals guard that state"),
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        des = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        root = des.rootComponent
        sk = root.sketches.add(root.xYConstructionPlane)
        P = adsk.core.Point3D.create
        sk.sketchCurves.sketchLines.addTwoPointRectangle(P(0, 0, 0), P(1, 1, 0))
        sk.sketchCurves.sketchLines.addTwoPointRectangle(P(2, 0, 0), P(3, 1, 0))
        before = sk.profiles.count
        sk.isComputeDeferred = True
        sk.sketchCurves.sketchLines.addTwoPointRectangle(P(4, 0, 0), P(5, 1, 0))
        during = sk.profiles.count
        sk.isComputeDeferred = False
        after = sk.profiles.count
        emit(before == 2 and during == 2 and after == 3,
             "sketch-profiles-under-compute-deferred: before " + str(before) + " | deferred, third "
             "region drawn " + str(during) + " | resumed " + str(after))
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "timeline-move-past-either-end-answers-true",
        "claim": ("A marker move PAST either end answers TRUE and leaves the marker where it was: "
                  "with the marker at count, movetoNextStep() returns True and markerPosition is "
                  "still count; with it at 0, moveToPreviousStep() returns True and markerPosition "
                  "is still 0. The bool is therefore no signal that the marker moved - a caller "
                  "that needs to know reads markerPosition back"),
        "encoded_in": ("tests/fakes/design.py - FakeTimeline's move helper answers True and "
                       "leaves the marker on an out-of-range target, and "
                       "tests/unit/test__conftest_worlds.py pins that pair of boundaries"),
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        des = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        root = des.rootComponent
        P = adsk.core.Point3D.create
        sk = root.sketches.add(root.xYConstructionPlane)
        sk.sketchCurves.sketchLines.addTwoPointRectangle(P(0, 0, 0), P(1, 1, 0))
        ext = root.features.extrudeFeatures
        ei = ext.createInput(sk.profiles.item(0),
                             adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
        ei.setDistanceExtent(False, adsk.core.ValueInput.createByReal(1.0))
        ext.add(ei)
        tl = des.timeline
        total = tl.count
        tl.moveToEnd()
        at_end = tl.markerPosition
        next_ok = tl.movetoNextStep()
        after_next = tl.markerPosition
        tl.moveToBeginning()
        at_start = tl.markerPosition
        prev_ok = tl.moveToPreviousStep()
        after_prev = tl.markerPosition
        emit(at_end == total and next_ok is True and after_next == total
             and at_start == 0 and prev_ok is True and after_prev == 0,
             "timeline-move-past-either-end-answers-true: count " + str(total)
             + " | at end " + str(at_end) + " -> movetoNextStep " + str(next_ok)
             + " marker " + str(after_next)
             + " | at 0 -> moveToPreviousStep " + str(prev_ok) + " marker " + str(after_prev))
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "design-computeall-returns-true",
        "claim": ("Design.computeAll() RETURNS True on a healthy design - the SDK documents 'Returns "
                  "true if successful' and the call answers a bool, not None"),
        "encoded_in": ("tests/fakes/design.py - MakeDesign's computeAll answers True; "
                       "design_recompute.py ignores the bool and judges the recompute by the "
                       "timeline health it reads afterwards"),
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        des = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        root = des.rootComponent
        P = adsk.core.Point3D.create
        sk = root.sketches.add(root.xYConstructionPlane)
        sk.sketchCurves.sketchLines.addTwoPointRectangle(P(0, 0, 0), P(1, 1, 0))
        answered = des.computeAll()
        emit(answered is True,
             "design-computeall-returns-true: computeAll() -> " + repr(answered)
             + " (type " + type(answered).__name__ + ")")
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "design-activate-root-reads-back",
        "claim": ("Occurrence.activate() answers True and the design then reads activeOccurrence = "
                  "that occurrence with isRootComponentActive False; Design.activateRootComponent() "
                  "answers True and the design reads activeOccurrence None with "
                  "isRootComponentActive True. It answers True when the root is ALREADY active too, "
                  "leaving activeOccurrence None - the call is idempotent, so a restore-to-root can "
                  "be made unconditionally and its ANSWER never separates 'went back' from 'was "
                  "already there'; only the activeOccurrence read-back does. Occurrence carries NO "
                  "deactivate member - activate() and isActive are its whole activation surface. "
                  "MEASURED BY HAND on a cloud DERIVE rig and deliberately NOT re-measured here "
                  "(the rig needs two saved cloud files and three source versions): a DERIVED "
                  "occurrence behaves identically on all three legs, so a False or raising answer "
                  "could not be forced in any state driven"),
        "encoded_in": ("design_activate_component.py's return-to-root read-back "
                       "(isRootComponentActive / activeOccurrence); doc_insert_derive.py's "
                       "unconditional restore-to-root and the activeOccurrence read-back it "
                       "publishes; tests/unit/"
                       "test_design_activate_component.py's design knobs for both reads"),
        "facts_on_pass": {"behavior.activate_root_component_true_when_already_root": True},
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        des = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        occ = des.rootComponent.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        start = (des.isRootComponentActive, des.activeOccurrence)
        # The ALREADY-ROOT leg: a fresh design is already at the root, so this call has nothing to
        # do and its answer is the one a restore fallback would branch on.
        already = des.activateRootComponent()
        idle = (des.isRootComponentActive, des.activeOccurrence)
        went = occ.activate()
        on_child, child_root = des.activeOccurrence, des.isRootComponentActive
        came = des.activateRootComponent()
        back, back_root = des.activeOccurrence, des.isRootComponentActive
        emit(start == (True, None) and already is True and idle == (True, None) and went is True
             and on_child is not None and on_child.fullPathName == occ.fullPathName
             and child_root is False and came is True
             and back is None and back_root is True and not hasattr(occ, "deactivate"),
             "design-activate-root-reads-back: start=" + repr(start)
             + "; already-root activateRootComponent() -> " + repr(already)
             + " leaves (isRootComponentActive, activeOccurrence)=" + repr(idle)
             + "; occ.activate() -> " + repr(went) + " activeOccurrence="
             + repr(on_child.fullPathName if on_child else None)
             + " isRootComponentActive=" + repr(child_root)
             + "; activateRootComponent() -> " + repr(came) + " activeOccurrence="
             + repr(back.fullPathName if back else None)
             + " isRootComponentActive=" + repr(back_root)
             + "; Occurrence.deactivate present=" + repr(hasattr(occ, "deactivate")))
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "derive-reference-version-setter-present",
        "claim": ("DocumentReference exposes a SETTABLE version (its descriptor carries an fset) and "
                  "a getter-only isOutOfDate, beside getLatestVersion and dataFile. MEASURED BY HAND "
                  "on a cloud rig and deliberately NOT re-measured by any row (it needs two saved "
                  "cloud files, three source versions and a close/reopen to go stale): on a "
                  "DeriveFeature's DocumentReference that is genuinely out of date - isOutOfDate "
                  "True, dataFile.versionNumber 1, latestVersionNumber 3 - BOTH refresh routes "
                  "REFUSE with the same catchable RuntimeError '2 : InternalValidationError : res', "
                  "the version ASSIGNMENT and getLatestVersion() alike; reading version or "
                  "referencedDocument on that same reference raises the catchable "
                  "'2 : InternalValidationError : doc', while isOutOfDate, dataFile.* and "
                  "parentDocument all read. After every attempt isOutOfDate is still True and the "
                  "derived component still holds the FIRST version's single body, so no refresh "
                  "landed and none silently half-landed"),
        "encoded_in": ("tests/fakes/data_docs.py FakeDocumentReference (version setter, setter_raises, "
                       "latest_raises); doc_update_xref.py _refresh_one"),
        "body": """
    DR = adsk.core.DocumentReference
    n = dump_shape("DocumentReference", DR)
    names = set(x for x in dir(DR) if not x.startswith("_"))
    ver_set = getattr(DR.__dict__.get("version"), "fset", None) is not None
    ood_set = getattr(DR.__dict__.get("isOutOfDate"), "fset", None) is not None
    emit(n > 0 and ver_set and not ood_set
         and "getLatestVersion" in names and "dataFile" in names,
         "derive-reference-version-setter-present: DocumentReference dumps " + str(n)
         + " attrs; version setter present=" + str(ver_set)
         + " isOutOfDate setter present=" + str(ood_set)
         + " getLatestVersion present=" + str("getLatestVersion" in names)
         + " dataFile present=" + str("dataFile" in names))
""",
    },
    {
        "id": "dxf-sketch-options-carries-units-unread",
        "claim": ("createDXFSketchExportOptions(path, sketch) answers a DXFSketchExportOptions whose "
                  "dir() LISTS 'units' beside the three content flags - so an attribute check cannot "
                  "guard the read that dxf-sketch-options-units-read-is-fatal measures, and only "
                  "never taking that read does. This row builds exactly that row's rig and stops at "
                  "the listing without reading units, so a regression in the rig - a design that "
                  "does not cast, a sketch that does not add, a factory that does not answer - "
                  "reddens HERE instead of silently turning its sibling's abort into a false PASS"),
        "encoded_in": ("design_export.py's _write_dxf (which sets the three content flags and never "
                       "reads units); tests/unit/test_design_export.py's DXF options fake"),
        "body": """
    import os, tempfile
    # Its own scratch document: the product active when the sweep reaches this row is whatever
    # the previous row left, and its sibling PASSES on ANY abort, so this rig must not lean on it.
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        d = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        root = d.rootComponent
        sk = root.sketches.add(root.xYConstructionPlane)
        sk.sketchCurves.sketchLines.addByTwoPoints(
            adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(1.0, 1.0, 0.0))
        opts = d.exportManager.createDXFSketchExportOptions(
            os.path.join(tempfile.gettempdir(), "unused_measure_dxf_units.dxf"), sk)
        n = dump_shape("DXFSketchExportOptions", opts)
        names = set(x for x in dir(opts) if not x.startswith("_"))
        flags = [f for f in ("isConstructionExported", "isPointsExported",
                             "isProjectedGeometryExported") if f not in names]
        emit(opts is not None and n > 0 and sk.sketchCurves.count == 1
             and "units" in names and not flags,
             "dxf-sketch-options-carries-units-unread: options type="
             + type(opts).__name__ + " dumps " + str(n) + " attrs from a "
             + str(sk.sketchCurves.count) + "-curve sketch; 'units' listed="
             + str("units" in names) + " (NOT read here); missing content flags: "
             + (", ".join(flags) or "none"))
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "dxf-sketch-options-units-read-is-fatal",
        "claim": ("A DXF options units read can abort Python.Run without a caught "
                  "verdict. An opaque abort is ERROR, not proof; this row uses the run-owned "
                  "scratch and creates no additional document."),
        "encoded_in": ("design_export.py's no-dxf_units comment and _write_dxf (which never reads "
                       "units); tests/unit/test_design_export.py's DXF options fake"),
        "expect": "raise_or_abort",
        "facts_on_pass": {"behavior.dxf_sketch_options_units_read_raises": True},
        "body": """
    import os, tempfile
    # The document exists before Python.Run, so an uncatchable abort cannot leak a new tab.
    d = adsk.fusion.Design.cast(app.activeProduct)
    root = d.rootComponent
    sk = root.sketches.add(root.xYConstructionPlane)
    sk.sketchCurves.sketchLines.addByTwoPoints(
        adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(1.0, 1.0, 0.0))
    # Nothing is exported: the options object only records the filename, and the row dies at the
    # units read below, so this path is never written.
    opts = d.exportManager.createDXFSketchExportOptions(
        os.path.join(tempfile.gettempdir(), "unused_measure_dxf_units.dxf"), sk)
    try:
        u = opts.units
        emit(False, "dxf-sketch-options-units-read-is-fatal: answered " + repr(u) + " with no raise")
    except Exception as e:
        emit(False, "dxf-sketch-options-units-read-is-fatal: raised CATCHABLY "
             + type(e).__name__ + ": " + str(e)[:60])
""",
    },
    {
        "id": "direct-design-timeline-raises",
        "claim": ("In a DIRECT design the timeline read RAISES '3 : this is not a parametric "
                  "design' - it does not answer None and the attribute is not absent, so a guard "
                  "must catch the platform's error rather than an AttributeError"),
        "encoded_in": ("tests/unit/test_design_get.py's direct-design scenario subclass, which "
                       "raises this message; design_get.py's timeline-slice guard wraps the read"),
        "body": """
    tmp = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    try:
        des = adsk.fusion.Design.cast(tmp.products.itemByProductType("DesignProductType"))
        des.designType = adsk.fusion.DesignTypes.DirectDesignType
        message = ""
        raised = False
        try:
            n = des.timeline.count
        except Exception as exc:
            raised = True
            message = str(exc)
        emit(raised and "not a parametric design" in message,
             "direct-design-timeline-raises: raised=" + str(raised) + " message=" + repr(message))
    finally:
        tmp.close(False)
""",
    },
    {
        "id": "python-run-stdout-shims-nest-per-mcp-execute",
        "claim": ("Inside a Python.Run script sys.stdout bottoms out at Fusion's CatchOut, and every "
                  "MCP.Execute call wraps it in one more __main__._NsSanitizedWriter shim (a 1 MiB "
                  "lifetime cap on each) that nothing removes: the chain is one shim deeper after a "
                  "trivial MCP.Execute run from inside the script. The tool's prelude has already "
                  "pointed the outer shim straight at CatchOut with its counter reset, which is what "
                  "keeps this row's own prints reaching the returned text"),
        "encoded_in": ("sys_execute_script._UNJAM_PRELUDE (prepended to every script) and "
                       "tests/unit/test_sys_execute_script.py's jammed-chain test"),
        "body": """
    import sys as _s
    import json as _j
    def chain(out):
        layers = []
        while type(out).__name__ == "_NsSanitizedWriter":
            layers.append(out)
            out = out._original
        return layers, out
    before, bottom = chain(_s.stdout)
    outer_ok = (not before) or (before[0]._original is bottom and before[0]._truncated is False)
    payload = _j.dumps({"featureType": "script",
                        "object": {"readOnly": True, "script": "def run(_c):\\n    pass\\n"}},
                       separators=(",", ":"))
    app.executeTextCommand('MCP.Execute "' + payload.replace('"', '\\\\"') + '"')
    after, bottom2 = chain(_s.stdout)
    emit(type(bottom).__name__ == "CatchOut" and bottom2 is bottom and outer_ok
         and len(after) == len(before) + 1,
         "python-run-stdout-shims-nest-per-mcp-execute: bottom=" + type(bottom).__name__
         + " shims before=" + str(len(before)) + " after one MCP.Execute=" + str(len(after))
         + " outer points at bottom=" + str(outer_ok))
""",
    },
]


# CAM world-building scripts (run by _build_cam_world, not as measurement rows).
_CAM_BOX_SCRIPT = '''import adsk.core, adsk.fusion

def run(context):
    app = adsk.core.Application.get()
    des = adsk.fusion.Design.cast(app.activeProduct)
    root = des.rootComponent
    sk = root.sketches.add(root.xYConstructionPlane)
    sk.sketchCurves.sketchLines.addTwoPointRectangle(
        adsk.core.Point3D.create(0.0, 0.0, 0.0), adsk.core.Point3D.create(4.0, 3.0, 0.0))
    prof = sk.profiles.item(0)
    ext = root.features.extrudeFeatures.addSimple(
        prof, adsk.core.ValueInput.createByReal(1.5),
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    ext.bodies.item(0).name = "CamMeasureBox"
    print("box made")
'''

_CAM_LIBURL_SCRIPT = '''import adsk.core, adsk.cam

def run(context):
    libs = adsk.cam.CAMManager.get().libraryManager.toolLibraries
    root = libs.urlByLocation(adsk.cam.LibraryLocations.Fusion360LibraryLocation)
    for a in libs.childAssetURLs(root):
        if "Milling Tools (Metric)" in a.leafName:
            print("LIBURL " + a.toString())
            return
    print("no matching sample library")
'''


def _scratch_call(scratch_handle, tool, arguments):
    """Call a scratch mutation bound to the exact run-created document handle."""
    args = dict(arguments)
    args["expect_document"] = scratch_handle
    return call(tool, args)


def _build_cam_world(scratch_handle):
    """Stand up MeasureSetup (box, Face1 top-level, Face2 inside MeasureFolder) in the CURRENT scratch
    doc via the server's own tools. Returns None on success, else the failing step's error."""
    is_error, payload = _scratch_call(scratch_handle, "sys_execute_script", {"script": _CAM_BOX_SCRIPT})
    if is_error:
        return "box: " + str(payload)[:120]
    is_error, payload = _scratch_call(scratch_handle, "view_switch_workspace", {"workspace": "manufacture"})
    if is_error:
        return "manufacture: " + str(payload)[:120]
    is_error, payload = _scratch_call(scratch_handle, "sys_execute_script", {"script": _CAM_LIBURL_SCRIPT})
    lib_url = None
    if not is_error and isinstance(payload, str):
        for ln in payload.splitlines():
            if ln.startswith("LIBURL "):
                lib_url = ln[7:].strip()
    if not lib_url:
        return "sample tool library not found: " + str(payload)[:120]
    is_error, payload = _scratch_call(scratch_handle, "cam_create_setup", {"operation_type": "milling", "name": "MeasureSetup"})
    if is_error:
        return "setup: " + str(payload)[:120]
    op_names = []
    for _ in range(2):
        is_error, payload = _scratch_call(scratch_handle, "cam_create_operation", {
            "setup": "MeasureSetup", "strategy": "face", "tool_library_url": lib_url,
            "tool_index": 0, "generate": False})
        if is_error:
            return "operation: " + str(payload)[:120]
        op_names.append(payload.get("operation") if isinstance(payload, dict) else None)
    is_error, payload = _scratch_call(scratch_handle, "cam_edit_folders",
                             {"action": "create", "setup": "MeasureSetup", "name": "MeasureFolder"})
    if is_error:
        return "folder: " + str(payload)[:120]
    is_error, payload = _scratch_call(scratch_handle, "cam_edit_folders",
                             {"action": "move", "setup": "MeasureSetup", "folder": "MeasureFolder",
                              "operations": [op_names[1] or "Face2"]})
    if is_error:
        return "move: " + str(payload)[:120]
    return None


def _compose(row):
    box_line = ""
    if row.get("needs") == "cam":
        # A platform generation the previous row left in flight fails on an op added under it
        # with a MODAL dialog that parks the main thread, so a CAM row pumps ~2 s before its body.
        box_line = ("    import time as _settle_t\n"
                    "    for _ in range(20):\n"
                    "        adsk.doEvents()\n"
                    "        _settle_t.sleep(0.1)\n")
    if row.get("need_box"):
        box_line += '    body = make_box(des, "PB_{0}")\n'.format(row["id"].replace("-", "_"))
    body = row["body_fn"]() if "body_fn" in row else row["body"]
    return _TEMPLATE.format(project=CLOUD_PROJECT, box_line=box_line,
                            body=body.strip("\n") + "\n")


def _verdict_lines(payload):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return [ln.strip() for ln in text.splitlines() if ln.strip().startswith(("PASS ", "FAIL "))]


def _fact_lines(payload):
    """Parse 'FACT <dotted.key> <json-value>' lines a row script printed."""
    text = payload if isinstance(payload, str) else ""
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln.startswith("FACT "):
            continue
        try:
            _, key, raw = ln.split(" ", 2)
            out.append((key, json.loads(raw)))
        except (ValueError, TypeError):
            pass
    return out


def _shape_lines(payload):
    """Parse 'SHAPE <TypeName> <attr attr ...>' lines into {type: set(attrs)}."""
    text = payload if isinstance(payload, str) else ""
    shapes = {}
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln.startswith("SHAPE "):
            continue
        parts = ln.split()
        if len(parts) >= 3:
            shapes.setdefault(parts[1], set()).update(parts[2:])
    return shapes


def _judge(row, is_error, payload):
    """One row's verdict: (status, detail). status is PASS / FAIL / ERROR."""
    if is_error:
        if row.get("expect") == "raise_or_abort":
            return "ERROR", "expected refusal evidence was unavailable: " + str(payload)
        # NOT truncated: this is a traceback, and the frame that names the defect is the LAST one.
        # The console line slices for width on its own; --json keeps the whole thing, which is the
        # only way to read why a row raised without re-running it by hand.
        return "ERROR", str(payload)
    lines = _verdict_lines(payload)
    fails = [ln[5:] for ln in lines if ln.startswith("FAIL ")]
    if fails:
        # NOT truncated either: the evidence for a FAIL is the numbers at the end of the detail.
        return "FAIL", "; ".join(fails)
    if not lines:
        return "ERROR", "no verdict output"
    # Nor a PASS: a shape dump's measured numbers sit at the END of its detail, and the --json
    # archive is where they are kept. The console line slices for width on its own.
    return "PASS", "; ".join(ln[5:] for ln in lines)


# --- scratch-document bookkeeping -------------------------------------------------------------
# The run's own document is addressed by its 'open:N' index (doc_get's open_index), never by "the
# active document": measured live on 2705.1.4 - an uncaught raise inside a row script does NOT close
# a document the script had already added, the document stays open AND ACTIVE, and a bare
# doc_close then closes THAT (or whatever else came forward) instead of the scratch.

_ACTIVATE_TRIES = 10
_ACTIVATE_SLEEP = 0.2


def _open_doc_rows():
    """doc_get's 'open_documents' rows, each carrying the open_index that addresses it. An
    unreadable session yields [] - every caller treats that as "cannot address anything" and
    refuses to close, rather than falling back to the active document."""
    is_error, payload = call("doc_get", {"max_results": 200})
    if is_error or not isinstance(payload, dict):
        return []
    rows = payload.get("open_documents")
    return rows if isinstance(rows, list) else []


def _scratch_row(rows, scratch_handle):
    """Return the exact run-created document row, or None when its handle is absent."""
    for row in rows:
        if row.get("document_handle") == scratch_handle:
            return row
    return None


def _reclaim_scratch(scratch_handle):
    """Activate the exact run-created document by handle; never close an unowned document."""
    rows = _open_doc_rows()
    scratch = _scratch_row(rows, scratch_handle)
    if scratch is None:
        return -1
    if scratch.get("is_active"):
        return 0
    is_error, _payload = call("doc_activate", {"name": scratch_handle})
    if is_error:
        return -1
    for _ in range(_ACTIVATE_TRIES):
        row = _scratch_row(_open_doc_rows(), scratch_handle)
        if row is not None and row.get("is_active"):
            return 0
        time.sleep(_ACTIVATE_SLEEP)
    return -1


def _close_scratch(scratch_handle):
    """Close only the exact run-created document handle, refusing stale or missing identity."""
    if _reclaim_scratch(scratch_handle) < 0:
        print("NOT closing scratch: its exact session handle is stale or activation was not confirmed.")
        return False
    rows = _open_doc_rows()
    scratch = _scratch_row(rows, scratch_handle)
    if scratch is None or scratch.get("is_saved") is not False:
        print("NOT closing scratch: its exact session handle no longer reads as an unsaved document.")
        return False
    is_error, _payload = call("doc_close", {"name": scratch_handle, "save_changes": False,
                                           "expect_document": scratch_handle})
    if is_error:
        return False
    is_error, payload = call("doc_get", {"max_results": 200})
    if is_error or not isinstance(payload, dict) or payload.get("truncated") is not False:
        return False
    rows = payload.get("open_documents")
    if not isinstance(rows, list) or any(not isinstance(row, dict)
            or not isinstance(row.get("document_handle"), str) for row in rows):
        return False
    return _scratch_row(rows, scratch_handle) is None

def _fusion_version(health=None):
    if health is None:
        health_gate()
    is_error, payload = call("workspace_orient", {})
    if is_error or not isinstance(payload, dict) or "fusion_version" not in payload:
        sys.exit("workspace_orient did not return fusion_version - is the add-in current?")
    return payload["fusion_version"]


_STAMP_RE = re.compile(r"^Stamp: Fusion (\S+) \| verified (\S+) \| source ([0-9a-f]{64})$", re.M)
_LOADED_RE = re.compile(
    r"^Loaded: implementation ([0-9a-f]{64}) \| schema ([0-9a-f]{64}) \| load (\S+) \| session (\S+)$",
    re.M)


def _measure_source_hash():
    """Return the normalized hash of this harness and the capture trust boundary."""
    entries = [("tests/live/measure_api.py", __file__)]
    entries.extend((rel, os.path.join(REPO_ROOT, *rel.split("/")))
                   for rel in _ATTESTATION_TCB)
    hasher = hashlib.sha256()
    for rel, source_path in entries:
        with open(source_path, encoding="utf-8", newline=None) as fh:
            source = fh.read().replace("\r\n", "\n").replace("\r", "\n")
        hasher.update(rel.encode("utf-8") + b"\0" + source.encode("utf-8") + b"\0")
    return hasher.hexdigest()


def _current_attestation(health=None):
    """Return a complete loaded identity from health or refuse the run."""
    identity = attestation_identity(health if health is not None else health_gate())
    if identity is None:
        sys.exit("health did not provide a complete loaded implementation/schema identity")
    return identity


def _attestation_drift(expected, current):
    """The first loaded identity field that changed, or None."""
    if current is None:
        return "loaded attestation became unavailable"
    for field in _ATTESTATION_FIELDS:
        if current.get(field) != expected.get(field):
            return field + " changed"
    return None


def _ledger_cell(value):
    return str(value).replace("|", "/")


def _ledger_fields(row, result="PASS"):
    return tuple(_ledger_cell(value) for value in
                 (result, row["id"], row["claim"], row["encoded_in"]))


def _ledger_row(row, status="PASS", detail=""):
    result = status if status == "PASS" else "{0}: {1}".format(status, detail)
    return "| " + " | ".join(_ledger_fields(row, result)) + " |"


def _ledger_projection(text):
    projection = []
    for line in text.splitlines():
        cells = line.split("|")
        if len(cells) == 6 and not cells[0].strip() and not cells[-1].strip():
            if cells[1].strip() not in ("result", "---"):
                projection.append(tuple(cell.strip() for cell in cells[1:5]))
    return projection


def write_api_facts(facts, fusion_version, stamp_date, shapes=None):
    """Generate tests/live_api_facts.py from a fully-PASSING run: 'enums.*' keys become ENUMS,
    'not_enums.*' NOT_ENUMS, 'behavior.*' BEHAVIOR, dumped shapes SHAPES. conftest imports the
    module to populate the mocks; the fake-shape lint checks SHARED fakes against SHAPES."""
    enums, behavior, not_enums = {}, {}, set()
    for key, value in facts.items():
        if key.startswith("enums."):
            family, member = key[len("enums."):].rsplit(".", 1)
            enums.setdefault(family, {})[member] = value
        elif key.startswith("not_enums."):
            not_enums.add(key[len("not_enums."):])
        elif key.startswith("behavior."):
            behavior[key[len("behavior."):]] = value
    lines = [
        "# GENERATED by tests/live/measure_api.py against live Fusion - DO NOT EDIT.",
        "# Regenerate: py -3 tests/live/measure_api.py (a fully-PASSING run rewrites this file).",
        '"""Measured adsk API facts. conftest populates the mock adsk modules and the shared fakes',
        'from these values, so the mocks carry measured data, not hand-typed claims. Each value is',
        'owned by the measurement row of the same name in tests/live/VERIFIED_API_FACTS.md."""',
        "",
        'FUSION_VERSION = "{0}"'.format(fusion_version),
        'VERIFIED_ON = "{0}"'.format(stamp_date),
        "",
        "# '<adsk namespace>.<Class>' -> {member: int} - seeded onto the mock adsk modules.",
        "ENUMS = {",
    ]
    for family in sorted(enums):
        lines.append('    "{0}": {{'.format(family))
        for member, value in sorted(enums[family].items(), key=lambda kv: (kv[1], kv[0])):
            lines.append('        "{0}": {1},'.format(member, value))
        lines.append("    },")
    lines += [
        "}",
        "",
        "# Referenced families that resolve to a live class carrying NO int member - factory-object",
        "# classes (Options.create()), measured as such, never seeded onto the mock namespaces.",
        "NOT_ENUMS = [",
    ]
    for family in sorted(not_enums):
        lines.append('    "{0}",'.format(family))
    lines += [
        "]",
        "",
        "# Behavior flags the shared fakes consume.",
        "BEHAVIOR = {",
    ]
    for key in sorted(behavior):
        # repr: a measured STRING (an error text) lands quoted; str() would emit it bare and the
        # module would not import.
        lines.append('    "{0}": {1!r},'.format(key, behavior[key]))
    lines += [
        "}",
        "",
        "# Live public attribute membership per adsk type (dir() of a real object) - the",
        "# fake-shape lint requires every SHARED fake attribute to exist here.",
        "SHAPES = {",
    ]
    for tname in sorted(shapes or {}):
        lines.append('    "{0}": ['.format(tname))
        attrs = sorted(shapes[tname])
        for i in range(0, len(attrs), 6):
            lines.append("        " + " ".join('"{0}",'.format(a) for a in attrs[i:i + 6]))
        lines.append("    ],")
    lines += ["}", ""]
    with open(FACTS, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))
    return FACTS


def write_ledger(results, fusion_version, stamp_date, source_hash, attestation):
    """Regenerate VERIFIED_API_FACTS.md from measurement results. results rows are (row, status, detail)."""
    if not isinstance(attestation, dict) or not all(
            isinstance(attestation.get(field), str) and attestation[field]
            for field in _ATTESTATION_FIELDS):
        raise ValueError("ledger needs the complete loaded implementation/schema/session identity")
    lines = [
        "# Live-verified mock contracts (generated by measure_api.py - do not edit)",
        "",
        "Each row is a CLAIM about the live adsk API, measured against a running Fusion by",
        "`measure_api.py`, beside the fakes and tool code that lean on it. PASS means the PLATFORM",
        "behaved as the claim states on that run. It does NOT mean every fake named in 'encoded in'",
        "agrees with the claim: a fixture may encode the OPPOSITE on purpose, to keep a consumer",
        "that must not depend on the real semantics under stress - camera-returns-copy names a fake",
        "modelling a shared mutable camera, and cam-children-tree names no fake at all because",
        "no tool reads children. Each such cell says so in its own words, so the",
        "'encoded in' text is what tells you which kind of row you are reading.",
        "",
        "A non-PASS row means the CLAIM no longer holds: update the fakes and their consumers, then",
        "re-run to refresh the stamp. `--check` fails when the stamp differs from the installed",
        "Fusion or any row is not PASS.",
        "",
        "Stamp: Fusion {0} | verified {1} | source {2}".format(
            fusion_version, stamp_date, source_hash),
        "Loaded: implementation {0} | schema {1} | load {2} | session {3}".format(
            attestation["implementation_fingerprint"], attestation["schema_fingerprint"],
            attestation["load_id"], attestation["session_id"]),
        "",
        "| result | claim id | claim | encoded in |",
        "|---|---|---|---|",
    ]
    for row, status, detail in results:
        lines.append(_ledger_row(row, status, detail))
    with open(LEDGER, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def check():
    """Stamp-vs-installed-Fusion gate; measures nothing. Exit 0 = current, 1 = stale or non-PASS."""
    if not os.path.exists(LEDGER):
        print("VERIFIED_API_FACTS.md does not exist - run measure_api.py once against live Fusion.")
        return 1
    with open(LEDGER, encoding="utf-8") as fh:
        text = fh.read()
    m = _STAMP_RE.search(text)
    loaded = _LOADED_RE.search(text)
    if not m or not loaded:
        print("VERIFIED_API_FACTS.md has no complete stamp/source/loaded identity - "
              "regenerate it (run measure_api.py).")
        return 1
    stamped_version, stamped_date, stamped_source = m.group(1), m.group(2), m.group(3)
    stamped_attestation = dict(zip(_ATTESTATION_FIELDS, loaded.groups()))
    table_lines = [ln for ln in text.splitlines()
                   if ln.startswith("|") and not ln.startswith(("| result", "|---"))]
    problems = [
        "malformed ledger row: " + ln
        for ln in table_lines
        if len(ln.split("|")) != 6
    ] + [
        "non-PASS row: " + ln
        for ln in table_lines
        if not ln.startswith("| PASS ")
    ]
    health = health_gate()
    current_attestation = _current_attestation(health)
    live = _fusion_version(health)
    if _ledger_projection(text) != [_ledger_fields(row) for row in ROWS]:
        problems.append("ledger rows do not match the current ROWS registry (id/claim/source/order)")
    if live != stamped_version:
        problems.append("stamp is Fusion {0} (verified {1}) but the installed Fusion is {2} - "
                        "re-run the measurements to refresh the stamp".format(
                            stamped_version, stamped_date, live))
    if _measure_source_hash() != stamped_source:
        problems.append("ledger source hash does not match the measurement/capture source - "
                        "re-run the measurements to refresh the stamp")
    for field in ("implementation_fingerprint", "schema_fingerprint"):
        if stamped_attestation[field] != current_attestation[field]:
            problems.append("ledger loaded {0} does not match the current server".format(field))
    if problems:
        print("\n".join(problems))
        return 1
    print("contracts current: Fusion {0}, verified {1}, all rows PASS".format(live, stamped_date))
    return 0


def cloud_rows():
    """The ids of every row whose script addresses the operator's cloud project. Derived from the
    bodies, so a new cloud row joins the pre-flight by READING CLOUD_PROJECT, not by being listed."""
    out = []
    for row in ROWS:
        body = row["body_fn"]() if "body_fn" in row else row.get("body", "")
        if "CLOUD_PROJECT" in body:
            out.append(row["id"])
    return out


def run_measurements(write_json, only=None):
    if only:
        known = {row["id"] for row in ROWS}
        unknown = sorted(set(only) - known)
        if unknown:
            sys.exit("Unknown measurement row ID(s): " + ", ".join(unknown)
                     + ". Choose IDs from the ROWS registry in tests/live/measure_api.py; "
                     + "repeat --only for each selected row.")
    source_hash = _measure_source_hash()
    # FIRST, before any read of the session: these rows find their project BY NAME, and unconfigured
    # each would fail on an empty name and take the all-PASS gate down with it. Nothing here needs
    # Fusion, so the refusal costs no connection and opens no scratch document.
    blocked = [r for r in cloud_rows() if not only or r in only]
    if blocked and not CLOUD_PROJECT:
        sys.exit("{0} row(s) need the operator's cloud project by name ({1}), and {2} names none. "
                 "Write it holding {3}, or re-run with --only naming rows that do not read it."
                 .format(len(blocked), ", ".join(blocked), cloud_config.CONFIG_PATH,
                         cloud_config.CONFIG_SHAPE))
    health = health_gate()
    pinned_attestation = _current_attestation(health)
    fusion_version = _fusion_version(health)
    if "sys_execute_script" not in registered_tools(health):
        sys.exit("sys_execute_script is not registered - enable allow_execute_api_script in the "
                 "mcpServer settings and reload the add-in, then re-run.")
    is_error, payload = call("doc_new", {})
    if is_error:
        sys.exit("doc_new refused: {0}".format(payload))
    scratch = payload.get("document_handle") if isinstance(payload, dict) else None
    if not isinstance(scratch, str) or not scratch.startswith("session:"):
        sys.exit("doc_new made a document without an exact session handle - refusing to measure, "
                 "because cleanup cannot safely identify the run's scratch. Close the new document "
                 "by hand and re-run.")
    results = []
    facts = {}
    shapes = {}
    cam_world = {"built": False, "err": None}
    try:
        if _reclaim_scratch(scratch) < 0:
            sys.exit("run scratch handle could not be activated before first row")
        for row in ROWS:
            if only and row["id"] not in only:
                continue
            if row.get("needs") == "cam" and not cam_world["built"]:
                cam_world["err"] = _build_cam_world(scratch)
                cam_world["built"] = True
            if row.get("needs") == "cam" and cam_world["err"]:
                results.append((row, "ERROR", "cam world: " + cam_world["err"]))
                print("  {0:6} {1:28} {2}".format("ERROR", row["id"], "cam world: " + cam_world["err"][:70]))
                continue
            script_args = {"script": _compose(row), "expect_document": scratch}
            if row.get("read_only"):
                script_args["read_only"] = True
            is_error, payload = call("sys_execute_script", script_args)
            status, detail = _judge(row, is_error, payload)
            if status == "PASS":
                facts.update(row.get("facts_on_pass") or {})
                facts.update(_fact_lines(payload))
                for tname, attrs in _shape_lines(payload).items():
                    shapes.setdefault(tname, set()).update(attrs)
            results.append((row, status, detail))
            print("  {0:6} {1:28} {2}".format(status, row["id"], detail[:90]))
            if _reclaim_scratch(scratch) < 0:
                sys.exit("run scratch handle could not be activated after row; stopping subsequent rows")
            time.sleep(0.1)
    finally:
        cleanup_ok = _close_scratch(scratch)
    if not cleanup_ok:
        print("Measurement evidence NOT published: scratch cleanup was not confirmed.")
        return 1
    if _measure_source_hash() != source_hash:
        print("Measurement evidence NOT published: measurement/capture source "
              "changed during the run.")
        return 1
    try:
        current_attestation = _current_attestation()
    except (Exception, SystemExit) as exc:
        print("Measurement evidence NOT published: loaded attestation could not be read: "
              + str(exc))
        return 1
    identity_problem = _attestation_drift(pinned_attestation, current_attestation)
    if identity_problem:
        print("Measurement evidence NOT published: " + identity_problem + " during the run.")
        return 1
    stamp_date = time.strftime("%Y-%m-%d")
    # The ledger and live_api_facts.py describe ONE run and are written on the same condition:
    # a partial or failing run leaves both at the last complete run, so the stamp never claims
    # rows a dead script channel prevented from executing.
    if only:
        # A subset run measures a subset. Writing the ledger from it would republish the whole
        # file from the handful of rows that ran, deleting every row the filter skipped - the
        # all-PASS gate cannot see the difference between "skipped" and "absent".
        print("\n{0} and live_api_facts.py NOT rewritten - this was a --only run of {1} row(s). "
              "Run the full sweep to republish.".format(os.path.basename(LEDGER), len(results)))
    elif all(s == "PASS" for _, s, _ in results):
        write_ledger(results, fusion_version, stamp_date, source_hash, pinned_attestation)
        print("\nwrote {0} (stamp: Fusion {1}, {2})".format(LEDGER, fusion_version, stamp_date))
        print("wrote {0} ({1} enum families, {2} not-an-enum families, {3} behavior flags, "
              "{4} shaped types)".format(
                  write_api_facts(facts, fusion_version, stamp_date, shapes),
                  sum(1 for k in facts if k.startswith("enums.")) and len(
                      {k.rsplit(".", 1)[0] for k in facts if k.startswith("enums.")}),
                  sum(1 for k in facts if k.startswith("not_enums.")),
                  sum(1 for k in facts if k.startswith("behavior.")),
                  len(shapes)))
    else:
        failed = [r["id"] for r, s, _ in results if s != "PASS"]
        print("\n{0} and live_api_facts.py NOT rewritten - {1} non-PASS row(s): {2}".format(
            os.path.basename(LEDGER), len(failed), ", ".join(failed[:12])
            + (", ..." if len(failed) > 12 else "")))
    if write_json:
        results_dir = os.path.join(os.path.dirname(LEDGER), "results")
        os.makedirs(results_dir, exist_ok=True)
        path = os.path.join(results_dir, "contracts-{0}.json".format(time.strftime("%Y%m%d-%H%M%S")))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"fusion_version": fusion_version, "date": stamp_date,
                       "rows": [{"id": r["id"], "status": s, "detail": d}
                                for r, s, d in results]}, fh, indent=2)
        print("wrote {0}".format(path))
    return 1 if any(s != "PASS" for _, s, _ in results) else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--only", metavar="ID", action="append",
                    help="Run only the row(s) with this id; repeatable. A row's full traceback is "
                         "truncated in the sweep's summary line, so this is how you read one. A "
                         "partial run NEVER rewrites the ledger - the all-PASS gate sees the "
                         "skipped rows as absent, not as passing.")
    args = ap.parse_args()
    sys.exit(check() if args.check
             else run_measurements(args.json, only=set(args.only) if args.only else None))
