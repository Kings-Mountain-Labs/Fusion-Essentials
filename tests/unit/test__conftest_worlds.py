# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The shared world fakes, each driven through the seam a tool reads it through."""

import math
import os

import pytest

import live_api_facts as _api_facts
from conftest import (FakeAppearance, FakeAppearances, FakeUnitsManager,
                      FakeApplication, FakeBaseFeature, FakeBaseFeatures, FakeCAMParameter,
                      FakeDataFile, FakeDataFolder, FakeExportManager, FakeFusionDocument,
                      _ExportOptions,
                      FakeDocumentReference, FakeFeature, FakeFeatures, FakeJoint, FakeJoints,
                      FakeModelParameter,
                      FakeMachine, FakeMatrix3D,
                      FakeMotionLink, FakeMotionLinks, FakePoint, FakeRigidGroup, FakeRigidGroups,
                      FakeSelection, FakeSetups, FakeTimeline, FakeTimelineObject, FakeTool,
                      FakeUserParameter, FakeVector3D,
                      FakeUserParameters, MakeComp, _MotionLimits, make_cam_parameters,
                      make_data_tree,
                      make_design, make_joint, make_document_world, make_occurrence, make_sketch,
                      make_sketch_curve, make_timeline)


class TestDocumentWorld:
    def test_the_active_documents_design_product_reaches_the_root_component(self):
        # The hop every design tool makes: activeDocument -> products -> the design -> its root.
        design = make_design(bodies=["Body1"])
        app = make_document_world(design=design, name="Bracket",
                                  selections=[FakeSelection(entity="face")])
        product = app.activeDocument.products.itemByProductType("DesignProductType")
        assert product.rootComponent.bRepBodies.itemByName("Body1") is not None
        assert app.activeDocument.products.itemByProductType("CAMProductType") is None
        assert app.userInterface.activeSelections.item(0).entity == "face"

    def test_a_closed_document_reads_invalid_and_its_name_raises(self):
        app = make_document_world(design=make_design(), name="Bracket")
        doc = app.activeDocument
        assert doc.name == "Bracket" and doc.isValid is True
        assert doc.close(False) is True
        assert doc.isValid is False
        with pytest.raises(RuntimeError, match="deleted Object"):
            doc.name

    def test_a_save_that_lands_advances_the_data_files_version(self):
        data_file = FakeDataFile("Bracket", version=3)
        app = make_document_world(design=make_design(), data_file=data_file)
        assert app.activeDocument.save("checkpoint") is True
        assert data_file.versionNumber == 4 and app.activeDocument.isModified is False

    def test_a_refreshed_reference_lands_the_sources_latest_version(self):
        # refresh_lands is the DECLARED landing case: a stale reference refuses by default.
        source = FakeDataFile("Sub", version=2, latest_version=5)
        ref = FakeDocumentReference(data_file=source, version=2, out_of_date=True,
                                    refresh_lands=True)
        assert ref.getLatestVersion() is True
        assert ref.version == 5 and ref.isOutOfDate is False

    def test_a_stale_reference_refuses_both_refresh_routes_by_default(self):
        # The MEASURED derive-link state: both routes raise the same message and nothing moves,
        # while isOutOfDate and dataFile.* keep reading.
        source = FakeDataFile("Sub", version=1, latest_version=5)
        ref = FakeDocumentReference(data_file=source, version=1, out_of_date=True)
        with pytest.raises(RuntimeError, match="InternalValidationError : res"):
            ref.getLatestVersion()
        with pytest.raises(RuntimeError, match="InternalValidationError : res"):
            ref.version = 5
        assert ref.version == 1 and ref.isOutOfDate is True
        assert ref.dataFile.latestVersionNumber == 5

    def test_the_declared_refusing_refresh_states_are_reachable(self):
        # The platform lie a refresh must re-check for, and a raise of the caller's own on either
        # route - each replacing the measured refusal.
        source = FakeDataFile("Sub", version=2, latest_version=5)
        lying = FakeDocumentReference(data_file=source, out_of_date=True, stays_out_of_date=True)
        assert lying.getLatestVersion() is True and lying.isOutOfDate is True
        derive = FakeDocumentReference(data_file=source, version=1, out_of_date=True,
                                       latest_raises="InternalValidationError")
        with pytest.raises(RuntimeError, match="InternalValidationError"):
            derive.getLatestVersion()
        derive.version = source.latestVersionNumber
        assert derive.version == 5 and derive.isOutOfDate is False
        # ...and that setter can refuse too, which is the third state.
        stubborn = FakeDocumentReference(data_file=source, version=1, out_of_date=True,
                                         setter_raises="2 : InternalValidationError")
        with pytest.raises(RuntimeError, match="InternalValidationError"):
            stubborn.version = 5
        assert stubborn.version == 1 and stubborn.isOutOfDate is True

    def test_a_save_that_versions_nothing_answers_true_and_leaves_the_document_modified(self):
        # The false success: the bool says nothing, so isModified is the read that catches it.
        data_file = FakeDataFile("Bracket", version=3)
        doc = FakeFusionDocument(name="Bracket", data_file=data_file, save_versions=False)
        assert doc.save("checkpoint") is True
        assert doc.isModified is True and data_file.versionNumber == 3
        assert doc._saves == [("save", ("checkpoint",))]

    def test_a_refused_activate_is_still_counted_and_leaves_the_document_in_the_background(self):
        # The count is what says the switch was ASKED for: isActive alone reads the same on a
        # document that was already forward and on one nothing ever called activate() on.
        already = FakeFusionDocument(name="A")
        stuck = FakeFusionDocument(name="B", is_active=False, activate_ok=False)
        assert stuck.activate() is False
        assert stuck.isActive is False and stuck._activates == 1
        assert already.isActive is True and already._activates == 0

    def test_a_document_added_to_the_walk_is_the_one_handed_back(self):
        app = FakeApplication()
        made = app.documents.add("FusionDesignDocumentType")
        assert app.documents.count == 1 and app.documents.item(0) is made


class TestTimelineWorld:
    def test_a_marker_move_past_either_end_answers_true_and_leaves_the_marker(self):
        # Measured: the bool is no signal that the marker moved - only markerPosition says that.
        timeline = make_timeline("Sketch1", "Extrude1", marker=1)
        assert timeline.moveToEnd() is True and timeline.markerPosition == 2
        # The exact boundary: the marker may sit AT count, and one step further answers True
        # while landing nowhere.
        assert timeline.movetoNextStep() is True and timeline.markerPosition == 2
        assert timeline.moveToPreviousStep() is True and timeline.markerPosition == 1
        assert timeline.moveToBeginning() is True and timeline.markerPosition == 0
        assert timeline.moveToPreviousStep() is True and timeline.markerPosition == 0

    def test_a_move_the_platform_refuses_answers_false(self):
        # the only False left: move_ok models the refusal, distinct from a move that lands nowhere.
        stuck = FakeTimeline(move_ok=False, marker=1)
        assert stuck.moveToBeginning() is False and stuck.markerPosition == 1

    def test_a_refused_roll_leaves_the_entry_where_it_was(self):
        stubborn = FakeTimelineObject("Extrude1", roll_ok=False)
        assert stubborn.rollTo(True) is False and stubborn.isRolledBack is False
        assert make_timeline("Extrude1").item(0).rollTo(True) is True

    def test_an_open_base_feature_is_invisible_while_its_siblings_are_not(self):
        features = FakeFeatures(features=[FakeFeature("Extrude1")])
        base = features.baseFeatures.add()
        assert features.baseFeatures.count == 1
        assert base.startEdit() is True
        assert features.baseFeatures.count == 0
        assert features.baseFeatures.itemByName(base.name) is None
        assert features.itemByName("Extrude1") is not None
        assert base.finishEdit() is True
        assert features.baseFeatures.itemByName(base.name) is base

    def test_a_scope_never_opened_reads_apart_from_one_opened_and_closed(self):
        # _open alone cannot tell the two apart - it is False before the first startEdit and False
        # again after finishEdit, which is why the counts exist.
        base = FakeBaseFeature()
        untouched = FakeBaseFeature()
        assert base.startEdit() is True and base.finishEdit() is True
        assert (base._starts, base._finishes) == (1, 1)
        assert (untouched._starts, untouched._finishes) == (0, 0)
        assert base._open is untouched._open is False

    def test_the_base_feature_add_hands_back_is_the_one_the_caller_supplied(self):
        held = FakeBaseFeature("Held")
        collection = FakeBaseFeatures(made=held)
        assert collection.count == 0            # nothing is in the walk until add() runs
        assert collection.add() is held
        assert collection.count == 1 and collection.itemByName("Held") is held
        # a supplied feature is governed by the same invisibility rule as a self-made one
        assert held.startEdit() is True and collection.count == 0

    def test_a_timeline_read_inside_an_open_scope_raises(self):
        design = make_design(timeline=make_timeline("Extrude1", raises="scope is open"))
        with pytest.raises(RuntimeError, match="scope is open"):
            design.timeline.count


class TestParameterWorld:
    def test_a_deleted_parameter_leaves_the_walk_and_a_miss_answers_none(self):
        width = FakeUserParameter("width", expression="50 mm", value=5.0)
        params = FakeUserParameters([width, FakeUserParameter("depth")])
        assert params.itemByName("nope") is None
        assert params.count == 2
        assert width.deleteMe() is True
        assert params.count == 1 and params.itemByName("width") is None

    def test_a_parameter_that_refuses_deletion_stays_in_the_walk(self):
        stuck = FakeUserParameter("width", delete_ok=False)
        params = FakeUserParameters([stuck])
        assert stuck.deleteMe() is False
        assert params.itemByName("width") is stuck

    def test_an_added_parameter_joins_the_design_walk(self):
        design = make_design(user_parameters=FakeUserParameters())
        made = design.userParameters.add("width", None, "mm", "note")
        assert design.userParameters.itemByName("width") is made
        assert made.unit == "mm" and made.comment == "note"

    def test_a_model_parameter_answers_its_maker_and_a_user_parameter_has_no_such_member(self):
        # MEASURED: createdBy on a model parameter never declines, and the DECLINE that leaves a row
        # flat is UserParameter carrying no such member - a fake with it the other way round makes
        # the guard in _owner_facts look like it is catching the wrong shape.
        assert FakeModelParameter().createdBy.name == "Extrude1"
        assert FakeModelParameter(owner=FakeFeature("Sketch2")).createdBy.name == "Sketch2"
        with pytest.raises(AttributeError):
            FakeUserParameter("width").createdBy

    def test_a_bodyless_designs_appearances_read_empty_rather_than_declining(self):
        # MEASURED: a design carrying no body answers an EMPTY collection, not a decline - an asset
        # arrives with the geometry. A fake that declined would make every appearance read look
        # unavailable on the design a test builds without bodies.
        assert make_design().appearances.count == 0


class TestJointMotionWorld:
    def test_a_rotation_beyond_an_enabled_limit_is_ignored_and_one_at_the_bound_lands(self):
        limits = _MotionLimits(minimum=math.radians(-10.0), maximum=math.radians(10.0))
        joint = make_joint(kind="revolute", rotation_limits=limits)
        motion = joint.jointMotion
        motion.rotationValue = math.radians(5.0)
        parked = motion.rotationValue
        motion.rotationValue = math.radians(45.0)
        assert motion.rotationValue == parked
        # The exact boundary: AT the enabled bound the assignment lands.
        motion.rotationValue = math.radians(10.0)
        assert round(math.degrees(motion.rotationValue), 4) == 10.0

    def test_a_stored_rotation_is_verbatim_and_lands_on_the_measured_store_grid(self, monkeypatch):
        motion = make_joint(kind="revolute").jointMotion
        motion.rotationValue = math.radians(750.0)
        # Verbatim: neither normalized into [0,360) nor accumulated onto the turn just made.
        assert round(math.degrees(motion.rotationValue), 6) == 750.0
        monkeypatch.setitem(_api_facts.BEHAVIOR, "joint_revolute_store_grid_deg", 0.1)
        motion.rotationValue = math.radians(12.34)
        assert round(math.degrees(motion.rotationValue), 6) == 12.3

    def test_a_cylindrical_motion_answers_no_slide_direction_vector(self):
        motion = make_joint(kind="cylindrical", slide=1.0).jointMotion
        assert motion.slideValue == 1.0
        assert not hasattr(motion, "slideDirectionVector")

    def test_a_rolled_back_motion_link_leaves_the_collection(self):
        joints = FakeJoints(joints=[make_joint(name="Rev1"), make_joint(name="Sld1", kind="slider")])
        links = FakeMotionLinks(new_link=FakeMotionLink(joint_one=joints.itemByName("Rev1")))
        link = links.add(links.createInput(joints.item(0), joints.item(1)))
        assert links.count == 1
        assert link.deleteMe() is True and links.count == 0

    def test_a_coupling_write_lands_all_five_arguments_and_a_refused_one_lands_none(self):
        # The live five-argument call: a DOF and a ValueInput per side plus the direction. The
        # re-value read-back is ml.valueOne.value, so the number has to land inside the parameter.
        link = FakeMotionLink(value_one=1.0, value_two=1.0)
        assert link.setMotionData("rotate", 1.0, "slide", 4.0, True) is True
        assert (link.valueOne.value, link.valueTwo.value) == (1.0, 4.0)
        assert link.motionOne == "rotate" and link.motionTwo == "slide"
        assert link.isReversed is True
        refusing = FakeMotionLink(value_one=1.0, value_two=1.0, set_motion_ok=False)
        assert refusing.setMotionData("rotate", 1.0, "slide", 9.0, True) is False
        assert refusing.valueTwo.value == 1.0 and refusing.isReversed is False

    def test_a_link_reporting_neither_coupled_motion_is_its_own_state(self):
        assert not hasattr(FakeMotionLink(), "motionOne")
        assert FakeMotionLink(motion_one="rotate", motion_two="slide").motionOne == "rotate"

    def test_a_motion_that_stores_nothing_takes_the_write_and_keeps_the_old_value(self):
        # The swallowed write: distinct from a limit refusal, and only a read-back catches it.
        motion = make_joint(kind="revolute", rotation=math.radians(5.0), stores=False).jointMotion
        motion.rotationValue = math.radians(30.0)
        assert round(math.degrees(motion.rotationValue), 4) == 5.0
        slider = make_joint(kind="slider", slide=1.0, stores=False).jointMotion
        slider.slideValue = 9.0
        assert slider.slideValue == 1.0

    def test_a_joint_whose_health_will_not_read_raises_rather_than_reading_healthy(self):
        # An unreadable state must not degrade to a confident verdict - the caller publishes null.
        with pytest.raises(RuntimeError, match="health state"):
            FakeJoint(health_readable=False).healthState
        assert FakeJoint(health=7).healthState == 7

    def test_a_motion_setter_answers_its_bool_and_records_what_it_was_given(self):
        joint = FakeJoint(motion_set_ok=False)
        assert joint.setAsRevoluteJointMotion(2) is False
        assert joint.setAsRigidJointMotion() is False
        assert joint._motion_calls == [("revolute", (2,)), ("rigid", ())]

    def test_a_rigid_group_reads_back_the_membership_setoccurrences_landed(self):
        groups = FakeRigidGroups()
        group = groups.add(["occ1", "occ2"])
        assert group.occurrences.count == 2
        # Both arguments are required live, and includeChildren travels with the membership.
        assert group.setOccurrences(["occ1"], True) is True
        assert groups.itemByName(group.name).occurrences.count == 1
        assert group._sets[-1] == (["occ1"], True)
        stubborn = FakeRigidGroup(occurrences=["occ1"], set_ok=False)
        assert stubborn.setOccurrences([], False) is False and stubborn.occurrences.count == 1


class TestCamJobWorld:
    def test_a_setup_created_through_the_input_joins_the_walk_with_what_it_was_given(self):
        setups = FakeSetups()
        machine = FakeMachine(description="Haas VF-2", vendor="Haas", model="VF-2")
        job = setups.createInput("MillingOperation")
        job.name = "Op1 Setup"
        job.models = ["Body1"]
        job.machine = machine
        made = setups.add(job)
        assert setups.itemByName("Op1 Setup") is made
        assert setups._added[0].machine is machine and setups._added[0].models == ["Body1"]

    def test_a_tool_parameter_publishes_its_payload_through_the_second_value_hop(self):
        tool = FakeTool(description="10mm flat",
                        parameters=make_cam_parameters(("tool_diameter", "10 mm", 1.0)))
        diameter = tool.parameters.itemByName("tool_diameter")
        assert diameter.expression == "10 mm" and diameter.value.value == 1.0
        assert tool.parameters.itemByName("tool_taperAngle") is None

    def test_a_write_to_a_locked_parameter_lands(self):
        # The state that makes cam_edit_operation's refuse-BEFORE-write load-bearing: a locked
        # parameter takes the assignment (measured), so a write made first is a real edit the UI
        # never offers, not a no-op the caller could shrug off.
        params = make_cam_parameters(("contours", "'a'"), ("strategy", "'swarf'"))
        locked = params.itemByName("strategy")
        locked.isEditable = False
        locked.expression = "'adaptive'"
        assert locked.expression == "'adaptive'"
        settable = params.itemByName("contours")
        settable.expression = "'b'"
        assert settable.expression == "'b'"
        assert FakeCAMParameter("context", "'part'").isEditable is True


class TestDataWorld:
    def test_a_file_in_the_root_folder_answers_both_of_its_parents(self):
        project = make_data_tree(name="MCP Test", files=["Part v1"])
        part = project.rootFolder.dataFiles.itemByName("Part v1")
        assert part.parentFolder is project.rootFolder
        assert part.parentProject.name == "MCP Test"

    def test_a_deleted_file_leaves_the_folder_walk_and_a_refused_delete_stays(self):
        project = make_data_tree(files=["Part v1", FakeDataFile("Stuck", delete_ok=False)])
        root = project.rootFolder
        assert root.dataFiles.count == 2
        assert root.dataFiles.itemByName("Part v1").deleteMe() is True
        assert root.dataFiles.count == 1 and root.dataFiles.itemByName("Part v1") is None
        assert root.dataFiles.itemByName("Stuck").deleteMe() is False
        assert root.dataFiles.itemByName("Stuck") is not None

    def test_a_copy_lands_in_the_target_folder_under_the_SOURCE_name(self):
        # DataFile.copy takes no name, which is why a rename follows it - a fake that applied the
        # requested name here would hide the second call the rename disclosure exists for.
        project = make_data_tree(files=[FakeDataFile("Template", file_id="urn:src")])
        target = FakeDataFolder("Archive")
        made = project.rootFolder.dataFiles.itemByName("Template").copy(target)
        assert made.name == "Template" and made.id == "urn:adsk.file:copy"
        assert target.dataFiles.itemByName("Template") is made

    def test_a_file_that_refuses_a_rename_raises_on_the_assignment(self):
        stubborn = FakeDataFile("Template", rename_ok=False)
        with pytest.raises(RuntimeError, match="read-only"):
            stubborn.name = "Renamed"
        assert stubborn.name == "Template"
        assert FakeDataFile("Template", copy_ok=False).copy(FakeDataFolder("Archive")) is None

    def test_a_move_that_the_platform_refuses_leaves_the_file_where_it_was(self):
        project = make_data_tree(files=[FakeDataFile("Part v1", move_ok=False)])
        root = project.rootFolder
        part = root.dataFiles.item(0)
        assert part.move(FakeDataFolder("Archive")) is False
        assert part.parentFolder is root


class TestExportWorld:
    def test_the_two_factory_arg_orders_record_the_same_pair(self, tmp_path):
        # STL takes (geometry, path) and STEP (path, geometry) - a fake that got one of the two
        # backwards would record the path as the geometry and every dispatch test would still pass.
        em = FakeExportManager()
        em.createSTEPExportOptions(str(tmp_path / "a.step"), "GEOM")
        em.createSTLExportOptions("GEOM", str(tmp_path / "a.stl"))
        assert [c.kind for c in em._calls] == ["step", "stl"]
        assert all(c.geom == "GEOM" and c.path.startswith(str(tmp_path)) for c in em._calls)

    def test_an_execute_that_answers_false_still_lands_the_file(self, tmp_path):
        # The bool and the disk are separate: a FALSE execute() over a landed file is measured, so a
        # fake tying the two together would hide the shape the tool's landed check exists for.
        path = str(tmp_path / "part.f3d")
        em = FakeExportManager(execute_ok=False)
        opts = em.createFusionArchiveExportOptions(path, "GEOM")
        assert em.execute(opts) is False
        assert em._executed is opts
        with open(path) as fh:
            assert fh.read()

    def test_an_execute_that_answers_true_can_write_nothing(self, tmp_path):
        path = str(tmp_path / "scan.stl")
        em = FakeExportManager(writes=lambda opts: False)
        assert em.execute(em.createSTLExportOptions("MESH", path)) is True
        assert not os.path.exists(path)

    def test_a_dropped_write_leaves_the_seeded_value_the_read_back_answers(self):
        opts = _ExportOptions("stl", "p.stl", drops=("unitType",), seeded={"unitType": "FACTORY"})
        opts.unitType = "INCHES"
        assert opts.unitType == "FACTORY"
        opts.isBinaryFormat = True
        assert opts.isBinaryFormat is True

    def test_a_knob_no_test_named_is_absent_rather_than_defaulted(self):
        opts = _ExportOptions("step", "p.step")
        assert not hasattr(opts, "meshRefinement")
        with pytest.raises(AttributeError):
            opts["meshRefinement"]
        assert opts["kind"] == "step"

    def test_the_dxf_sketch_options_units_read_raises_rather_than_answering(self):
        opts = FakeExportManager().createDXFSketchExportOptions("p.dxf", "SKETCH")
        with pytest.raises(RuntimeError, match="not supported by DXF"):
            opts.units


class TestPlacementMatrix:
    def test_a_rotation_about_a_pivot_leaves_that_point_where_it_was(self):
        # setToRotation bakes the pivot correction into the translation column - a rotation that
        # dropped it would swing the part about the WORLD origin instead of its own.
        m = FakeMatrix3D()
        assert m.setToRotation(math.pi / 2, FakeVector3D(0.0, 0.0, 1.0),
                               FakePoint(10.0, 0.0, 0.0)) is True
        assert [round(v, 6) for v in m._apply_point(10.0, 0.0, 0.0)] == [10.0, 0.0, 0.0]

    def test_a_composed_move_accumulates_so_a_second_identical_move_is_not_a_no_op(self):
        base, step = FakeMatrix3D(), FakeMatrix3D(t=(5.0, 0.0, 0.0))
        assert base.transformBy(step) is True and base.asArray()[3] == 5.0
        base.transformBy(step)
        assert base.asArray()[3] == 10.0

    def test_two_composed_rotations_apply_the_argument_second(self):
        # Measured: rotX(90).transformBy(rotZ(90)) sends +X to +Y. The ROTATION product is what this
        # pins - the translation column alone cannot tell self*other from other*self, and the
        # swapped product sends +X to +Z instead.
        x90, z90 = FakeMatrix3D(), FakeMatrix3D()
        x90.setToRotation(math.pi / 2, FakeVector3D(1.0, 0.0, 0.0), FakePoint())
        z90.setToRotation(math.pi / 2, FakeVector3D(0.0, 0.0, 1.0), FakePoint())
        assert x90.transformBy(z90) is True
        assert [round(v, 6) for v in x90._apply_vector(1.0, 0.0, 0.0)] == [0.0, 1.0, 0.0]
        rows = [[round(v, 6) for v in x90.asArray()[i:i + 3]] for i in (0, 4, 8)]
        assert rows == [[0.0, 0.0, 1.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]

    def test_a_translation_assigned_after_a_rotation_is_recorded_as_the_pivot_clobber(self):
        m = FakeMatrix3D()
        m.setToRotation(math.pi / 2, FakeVector3D(0.0, 0.0, 1.0), FakePoint(10.0, 0.0, 0.0))
        assert m._direct_translation is False
        m.translation = FakeVector3D(1.0, 2.0, 3.0)
        assert m._direct_translation is True

    def test_a_translation_read_is_a_fresh_vector_and_none_is_refused(self):
        # Measured: the object read back is never the one assigned, so mutating a read result moves
        # nothing; and None RAISES rather than clearing the column.
        m, v = FakeMatrix3D(), FakeVector3D(1.0, 2.0, 3.0)
        m.translation = v
        read = m.translation
        assert read is not v
        assert (read.x, read.y, read.z) == (1.0, 2.0, 3.0)
        read.x = 99.0
        assert m.translation.x == 1.0
        with pytest.raises(RuntimeError, match="invalid argument value"):
            m.translation = None

    def test_the_translation_column_takes_a_vector_and_nothing_else(self):
        # Measured: None answers "3 : invalid argument value" while a Point3D/tuple/int answers a
        # TYPE error - so a duck-typed x/y/z check would accept the Point3D the live member refuses.
        m = FakeMatrix3D()
        for wrong in (FakePoint(1.0, 2.0, 3.0), (1.0, 2.0, 3.0), [1.0, 2.0, 3.0], 5):
            with pytest.raises(TypeError):
                m.translation = wrong
        assert (m.translation.x, m.translation.y, m.translation.z) == (0.0, 0.0, 0.0)

    def test_an_unassigned_matrix_reports_its_own_offset_and_its_axes(self):
        m = FakeMatrix3D(t=(1.0, 2.0, 3.0))
        assert (m.translation.x, m.translation.y, m.translation.z) == (1.0, 2.0, 3.0)
        origin, x_axis, _y, _z = m.getAsCoordinateSystem()
        assert (origin.x, origin.y, origin.z) == (1.0, 2.0, 3.0)
        assert (round(x_axis.x, 6), round(x_axis.y, 6)) == (1.0, 0.0)


class TestComponentBodies:
    def test_a_body_object_with_no_name_is_refused_where_it_was_handed_in(self):
        # Renaming it into BRepBody(<that object>) makes `name` the object itself: every by-name
        # read then misses and the row publishes junk, far from the line that built it.
        with pytest.raises(TypeError, match="carries no name"):
            MakeComp(bodies=[object()])
        assert MakeComp(bodies=["Body1"]).bRepBodies.itemByName("Body1") is not None


class TestAssemblyPlacement:
    def test_a_plain_occurrence_reads_local_with_two_empty_collections(self):
        # Measured: absence is not a live state for any of the three - a plain local occurrence
        # answers False and two EMPTY walks, so a default that dropped the member would teach a
        # shape Fusion never presents.
        occ = make_occurrence("Bracket:1")
        assert occ.isReferencedComponent is False
        assert occ.joints.count == 0 and occ.bRepBodies.count == 0

    def test_one_read_can_decline_while_the_rest_still_answer(self):
        occ = make_occurrence("Bracket:1",
                              raises_on={"isReferencedComponent": "3 : read declined"})
        with pytest.raises(RuntimeError, match="read declined"):
            occ.isReferencedComponent
        assert occ.joints.count == 0 and occ.name == "Bracket:1"

    def test_the_placement_walk_matches_on_token_because_identity_never_answers(self):
        # Measured elsewhere: two reads of ONE component hand back different Python objects sharing
        # one entityToken, so an identity compare finds none of the placements.
        one, two = MakeComp("Bolt", entity_token="TOK"), MakeComp("Bolt", entity_token="TOK")
        occs = [make_occurrence("Bolt:1", component=one), make_occurrence("Bolt:2", component=two)]
        root = MakeComp("Root", occurrences=occs)
        found = root.allOccurrencesByComponent(MakeComp("Bolt", entity_token="TOK"))
        assert found.count == 2
        assert root.allOccurrencesByComponent(MakeComp("Other", entity_token="OTHER")).count == 0
        # A component carrying no token cannot be matched at all - that is not "placed nowhere".
        assert root.allOccurrencesByComponent(MakeComp("Untokened")).count == 0


class TestAppearanceWorld:
    def test_a_copy_under_a_name_the_collection_already_holds_raises(self):
        # MEASURED: live addByCopy raises '3 : appearance name already exists in document' and adds
        # nothing; appearance_set reaches it through safe(), so the raise arrives as the None its
        # look-up-first reuse path re-checks on.
        apps = FakeAppearances([FakeAppearance("AgentColor_1E8E3E")])
        with pytest.raises(RuntimeError, match="already exists"):
            apps.addByCopy(FakeAppearance("Base"), "AgentColor_1E8E3E")
        assert apps.count == 1 and apps._copied == []

    def test_a_copy_keeps_its_sources_asset_id_and_its_colour_channels(self):
        # MEASURED: a copy shares the base's asset id, so the NAME is the only axis telling two
        # overrides minted off one base apart - and it exposes the base's own channels, which is
        # what an albedo write then looks for.
        base = FakeAppearance("Base", color_props=("opaque_albedo", "opaque_luminance_modifier"),
                              appearance_id="asset:Base")
        apps = FakeAppearances()
        made = apps.addByCopy(base, "AgentColor_FF0000")
        assert made.id == "asset:Base" and made.name == "AgentColor_FF0000"
        assert [p.id for p in made.appearanceProperties] == ["opaque_albedo",
                                                             "opaque_luminance_modifier"]
        assert apps.itemByName("AgentColor_FF0000") is made and apps._copied[0][0] is base


class TestUnitsWorld:
    """MEASURED by units-manager-internal-units-and-convert: internalUnits is a SENTINEL string,
    polymorphic across the length/angle boundary, and convert's three refusals carry three
    different messages."""

    def test_the_sentinel_from_unit_lifts_a_database_number_into_either_dimension(self):
        # ONE call shape converts a length and an angle, which is what lets _param_summary hand
        # internalUnits straight through whatever the parameter's own unit turns out to be.
        um = FakeUnitsManager()
        assert um.convert(1.0, um.internalUnits, "mm") == 10.0
        assert um.convert(1.0, um.internalUnits, "deg") == pytest.approx(math.degrees(1.0))

    def test_a_literal_from_unit_converts_within_one_dimension_and_refuses_across(self):
        # Only the sentinel crosses: a caller reaching an angle out of a centimetre is refused
        # rather than handed the number a table lookup alone would produce.
        um = FakeUnitsManager()
        assert um.convert(1.0, "cm", "mm") == 10.0
        with pytest.raises(RuntimeError, match="not compatible"):
            um.convert(1.0, "cm", "deg")

    def test_an_empty_to_unit_and_an_unknown_one_are_refused_APART(self):
        # Two different measured messages: collapsing them would let a caller read "no units at
        # all" as "a unit I do not know", which are different repairs.
        um = FakeUnitsManager()
        with pytest.raises(RuntimeError, match="Bad units parameter"):
            um.convert(1.0, um.internalUnits, "")
        with pytest.raises(RuntimeError, match="not a valid unit string"):
            um.convert(1.0, um.internalUnits, "furlong")


class TestSketchWorld:
    def test_a_curve_lands_in_both_the_flat_walk_and_its_own_kind(self):
        # A '<type>:<index>' ref indexes the per-kind sub-collection while a count read-back walks
        # the flat one, so a curve missing from either side reads as a draw that never landed.
        line, circle = make_sketch_curve("L0"), make_sketch_curve("C0")
        sketch = make_sketch("Plate", lines=[line], circles=[circle])
        assert sketch.sketchCurves.count == 2
        assert sketch.sketchCurves.sketchLines.item(0) is line
        assert sketch.sketchCurves.sketchCircles.item(0) is circle
        assert sketch.sketchCurves.sketchArcs.count == 0

    def test_a_deferred_sketch_still_answers_the_profiles_it_held(self):
        # Measured (sketch-profiles-under-compute-deferred): the flag does not empty profiles, it
        # freezes them - so a stale count reads exactly like a fresh one and the flag is the tell.
        sketch = make_sketch(profiles=[object(), object()], is_compute_deferred=True)
        assert sketch.isComputeDeferred is True and sketch.profiles.count == 2
