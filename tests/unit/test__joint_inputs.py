"""Unit tests for ``_joint_inputs.py``'s autonomous geometry-snap resolver.

Besides resolving inputs by joint-origin NAME, a joint input may be an AUTONOMOUS
(no human selection) geometry snap: '<occurrence>:<snap>' where snap is origin |
center | top | bottom | cylinder. The tool finds that geometry in the occurrence,
builds the matching JointGeometry, and proxies it into the occurrence's assembly
context.

These tests pin the PURE parts - snap-spec parsing (_parse_snap) and the
top/bottom/largest-face selection (_pick_face) - without a live Fusion. The
JointGeometry factory calls + createForAssemblyContext proxying are live-only and
exercised against the running session separately.
"""

from conftest import BRepFace, Cylinder, Plane, _NamedCollection, load_tool, make_bbox

jt = load_tool("_joint_inputs")


# ── _parse_snap: split '<occurrence>:<snap>' ───────────────────────────────

class TestParseSnap:
    def test_plain_name_is_joint_origin(self):
        # No recognized snap suffix -> treat whole string as a JO name (back-compat).
        occ, snap = jt._parse_snap("Center of Model")
        assert occ is None and snap is None

    def test_occurrence_with_snap(self):
        occ, snap = jt._parse_snap("Boom:1:origin")
        assert occ == "Boom:1" and snap == "origin"

    def test_occurrence_with_top_snap(self):
        occ, snap = jt._parse_snap("TrussMast:1:top")
        assert occ == "TrussMast:1" and snap == "top"

    def test_cylinder_snap(self):
        occ, snap = jt._parse_snap("Cable:1:cylinder")
        assert occ == "Cable:1" and snap == "cylinder"

    def test_unknown_suffix_not_treated_as_snap(self):
        # 'A:1:wobble' — 'wobble' is not a snap keyword, so not a snap spec.
        occ, snap = jt._parse_snap("A:1:wobble")
        assert occ is None and snap is None

    def test_occurrence_colon_in_name_without_snap(self):
        # 'Mast:1' is a normal occurrence name (the ':1' is the instance), NOT a snap.
        occ, snap = jt._parse_snap("Mast:1")
        assert occ is None and snap is None


# ── _pick_face: directional faces (top/bottom/left/right/front/back) + center ──
#
# A face is identified by OBJECT IDENTITY here: BRepFace has no name live, so a labelled fake
# would assert on a member the picks could never read.

def _face(mn, mx, area=1.0, planar=True):
    """One face: its bounding box (cm), its area, and a planar or cylindrical surface."""
    return BRepFace(Plane(None) if planar else Cylinder(None), area=area,
                    bounding_box=make_bbox(mn, mx))


def _z_face(zmin, zmax, area=1.0, planar=True):
    """A face spanning zmin..zmax with no x/y extent - what the top/bottom picks rank on."""
    return _face((0.0, 0.0, zmin), (0.0, 0.0, zmax), area=area, planar=planar)


class TestPickFace:
    def _body(self):
        """(faces, bottom cap, top cap) plus a taller side face between them."""
        bottom = _z_face(0.0, 0.0, area=4.0)
        top = _z_face(2.0, 2.0, area=4.0)
        side = _z_face(0.0, 2.0, area=8.0)
        return _NamedCollection([bottom, top, side]), bottom, top

    def test_top_picks_highest_face(self):
        body, _bottom, top = self._body()
        assert jt._pick_face(body, "top") is top

    def test_bottom_picks_lowest_face(self):
        body, bottom, _top = self._body()
        assert jt._pick_face(body, "bottom") is bottom

    def test_center_picks_largest_planar_face(self):
        # 'center' uses the largest planar face, so the bigger non-planar wall is skipped.
        cap = _z_face(0.0, 0.0, area=3.0)
        wall = _z_face(0.0, 2.0, area=9.0, planar=False)
        assert jt._pick_face(_NamedCollection([cap, wall]), "center") is cap

    def test_top_bottom_skip_nonplanar_cylinder_wall(self):
        # The curved-wall trap: a cylinder's curved side wall spans the whole height, so by raw
        # Z it would beat the flat end caps for both top and bottom — but createByPlanarFace
        # needs a PLANAR face, so top/bottom must skip the non-planar wall.
        bottom_cap = _z_face(0.0, 0.0, area=1.0)
        top_cap = _z_face(5.0, 5.0, area=1.0)
        wall = _z_face(-0.5, 5.5, area=20.0, planar=False)   # extends beyond both caps
        body = _NamedCollection([bottom_cap, top_cap, wall])
        assert jt._pick_face(body, "top") is top_cap
        assert jt._pick_face(body, "bottom") is bottom_cap


class TestDirectionalFaces:
    """The 6 box faces by normal direction — needed to fully locate a part with constraints."""

    def _box(self):
        """A 10x10x10 box centered at origin: its six axis-aligned planar faces by direction."""
        return {
            "right": _face((5, -5, -5), (5, 5, 5), area=100),      # +X
            "left": _face((-5, -5, -5), (-5, 5, 5), area=100),     # -X
            "back": _face((-5, 5, -5), (5, 5, 5), area=100),       # +Y
            "front": _face((-5, -5, -5), (5, -5, 5), area=100),    # -Y
            "top": _face((-5, -5, 5), (5, 5, 5), area=100),        # +Z
            "bottom": _face((-5, -5, -5), (5, 5, -5), area=100),   # -Z
        }

    def _pick(self, faces, snap):
        return jt._pick_face(_NamedCollection(list(faces.values())), snap)

    def test_right_is_max_x(self):
        faces = self._box()
        assert self._pick(faces, "right") is faces["right"]

    def test_left_is_min_x(self):
        faces = self._box()
        assert self._pick(faces, "left") is faces["left"]

    def test_back_is_max_y(self):
        faces = self._box()
        assert self._pick(faces, "back") is faces["back"]

    def test_front_is_min_y(self):
        faces = self._box()
        assert self._pick(faces, "front") is faces["front"]

    def test_top_is_max_z(self):
        faces = self._box()
        assert self._pick(faces, "top") is faces["top"]

    def test_bottom_is_min_z(self):
        faces = self._box()
        assert self._pick(faces, "bottom") is faces["bottom"]

    def test_directional_snaps_parse(self):
        for kw in ("left", "right", "front", "back"):
            occ, snap = jt._parse_snap(f"Part:1:{kw}")
            assert occ == "Part:1" and snap == kw

    def test_empty_body_returns_none(self):
        assert jt._pick_face(_NamedCollection([]), "top") is None
