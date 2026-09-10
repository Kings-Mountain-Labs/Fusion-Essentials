# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The adsk.core geometry fakes: points, vectors, matrices, boxes, surfaces, curves and the view."""

import math

import live_api_facts as _api_facts

from tests.fakes.scaffold import fusion_fake

# These mimic the *interface* a tool reads, not the whole API. Names match the
# Fusion type names because tools branch on ``type(entity).__name__``.


@fusion_fake(live_type="Point3D", facts=("shape-dump-design-world", "point3d-vectorto"))
class FakePoint:
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = x, y, z

    def vectorTo(self, other):
        # Mirrors adsk.core.Point3D.vectorTo -> Vector3D(other - self).
        return FakeVector3D(other.x - self.x, other.y - self.y, other.z - self.z)

    def distanceTo(self, other):
        # Mirrors adsk.core.Point3D.distanceTo -> Euclidean distance.
        return ((self.x - other.x) ** 2 + (self.y - other.y) ** 2
                + (self.z - other.z) ** 2) ** 0.5

    def copy(self):
        # Mirrors adsk.core.Point3D.copy -> an independent point at the same coordinates. A world
        # lift copies a surface's own origin BEFORE transforming it, so the surface keeps its
        # untouched reading (_joints._world_torus_centre). type(self), not FakePoint: a subclass
        # modelling a platform that DECLINES the transform must survive that copy.
        return type(self)(self.x, self.y, self.z)

    def transformBy(self, m):
        """Move this point by `m` (a FakeMatrix3D) IN PLACE, answering the bool a caller gates
        on (model_hole._sketch_space_point treats a falsy answer as 'not expressible'). A POINT
        takes the matrix's rotation AND its translation - the live point/vector split
        FakeMatrix3D's two applies keep. Subclass and return False for the platform DECLINING the
        transform."""
        self.x, self.y, self.z = m._apply_point(self.x, self.y, self.z)
        return True


@fusion_fake(live_type="BoundingBox3D", facts=("shape-dump-design-world",))
class FakeBoundingBox3D:
    def __init__(self, min_pt, max_pt):
        self.minPoint = min_pt
        self.maxPoint = max_pt


@fusion_fake(factory_for="FakeBoundingBox3D", facts=("units-cm",))
def make_bbox(minp, maxp):
    """A FakeBoundingBox3D from (min xyz, max xyz) tuples in cm (Fusion's internal unit). The plain
    function, so a module-level fake that HOLDS a bounding box can build one without a fixture."""
    return FakeBoundingBox3D(FakePoint(*minp), FakePoint(*maxp))


@fusion_fake(live_type="ValueInput", facts=("shape-dump-document-world",))
class FakeValueInput:
    """What ValueInput.createByReal answers, and the class stands in for the factory itself
    (monkeypatch createByReal to it): realValue is the number that went in, read back off the
    input a feature was built from. There is no `real` - the live type carries none."""
    def __init__(self, real):
        self.realValue = real


# -- vectors / surfaces / curves for selection.py classification -----------
#
# selection.py branches on type(surface).__name__ ("Plane", "Cylinder", ...)
# and type(curve).__name__ ("Line3D", "Circle3D", ...), so the geometry fakes
# below are NAMED to match those runtime type names exactly.

@fusion_fake(live_type="Vector3D", facts=("shape-dump-design-world", "vector3d-normalize-zero"))
class FakeVector3D:
    """Vector matching Fusion's real Vector3D interface: plain .x/.y/.z plus .length/.copy()/
    .normalize().

    Zero-vector behavior is the measured BEHAVIOR flag: live normalize() reports success and
    leaves the components untouched, so callers zero-check by magnitude, never by the return
    value.
    """
    def __init__(self, x=0.0, y=0.0, z=0.0):
        self.x, self.y, self.z = x, y, z

    @property
    def length(self):
        return (self.x ** 2 + self.y ** 2 + self.z ** 2) ** 0.5

    def copy(self):
        # type(self), not FakeVector3D: a subclass modelling ONE refusing axis must survive the
        # copy a lift takes before transforming (_assembly_detail._world_axes copies, then transforms).
        return type(self)(self.x, self.y, self.z)

    def normalize(self):
        mag = (self.x ** 2 + self.y ** 2 + self.z ** 2) ** 0.5
        if mag < 1e-12:
            return bool(_api_facts.BEHAVIOR["vector3d_normalize_true_on_zero"])
        self.x, self.y, self.z = self.x / mag, self.y / mag, self.z / mag
        return True

    def dotProduct(self, other):
        # Mirrors adsk.core.Vector3D.dotProduct.
        return self.x * other.x + self.y * other.y + self.z * other.z

    def isParallelTo(self, other):
        # Mirrors adsk.core.Vector3D.isParallelTo: parallel regardless of sense (cross ~ 0).
        cx = self.y * other.z - self.z * other.y
        cy = self.z * other.x - self.x * other.z
        cz = self.x * other.y - self.y * other.x
        return (cx * cx + cy * cy + cz * cz) ** 0.5 < 1e-9

    def transformBy(self, m):
        """Move this DIRECTION by `m` (a FakeMatrix3D) IN PLACE, answering the bool a caller gates
        on. A vector takes the matrix's ROTATION ONLY, so a placement's translation can never reach
        an axis through it - the same live split FakePoint.transformBy sits on the other side of.
        Subclass and return False for one axis the platform declines to express."""
        self.x, self.y, self.z = m._apply_vector(self.x, self.y, self.z)
        return True

    def asPoint(self):
        """The same three components as a Point3D - what a translation column is read through when
        a caller wants a POSITION out of a placement's translation vector."""
        return FakePoint(self.x, self.y, self.z)


@fusion_fake(live_type="Matrix3D",
             facts=("shape-dump-design-world",
                    "matrix3d-invert-singular-answers-true-and-corrupts",
                    "matrix3d-transformby-applies-the-argument-after-self",
                    "matrix3d-asarray-row-major-translation-3-7-11",
                    "matrix3d-translation-copies-and-refuses-none"))
class FakeMatrix3D:
    """Numeric adsk.core.Matrix3D: a rotation of `deg` about Z followed by a translation `t` (cm).

    Its PUBLIC surface is the pair a world lift calls on the matrix itself - ``copy()`` and
    ``invert()``, which inverts IN PLACE and answers a bool the way Matrix3D.invert does; pass
    ``invertible=False`` for the platform DECLINING the inversion - the False every production
    caller gates on (model_inspect._measuring_axes, model_hole._world_lift). Measured: a SINGULAR
    matrix does not produce that False - invert() answers True and corrupts the matrix to nan/inf;
    rigid occurrence transforms cannot be singular, so the gate is defensive. The
    rotation/translation state and the arithmetic below it are underscored because they are this
    fake's own plumbing to a fake Point3D/Vector3D, not members of the type it impersonates - a
    fake that published them would teach tool code an API Fusion has no equivalent of.

    That arithmetic keeps the POINT/VECTOR split the live API makes: Point3D.transformBy takes the
    translation, Vector3D.transformBy does not. So a fake point calls ``_apply_point`` and a fake
    DIRECTION calls ``_apply_vector``, and a lift that leaked a placement's translation into an axis
    reads wrong here instead of passing.

    A ROTATED placement is what tells a real lift apart from a backwards one - under identity the
    two are the same matrix.

    ``setToRotation``/``transformBy`` carry the general rotation the (deg, t) constructor is one case
    of, so a rotation about ANY axis through ANY origin is expressible. setToRotation bakes the
    PIVOT correction into the translation column, which is why assigning ``translation`` after it
    destroys the pivot and a caller composes a translation matrix instead; ``_direct_translation``
    records that assignment so a test can tell the two apart.
    """
    def __init__(self, deg=0.0, t=(0.0, 0.0, 0.0), invertible=True):
        self._deg = float(deg)
        self._t = tuple(float(v) for v in t)
        self._invertible = bool(invertible)
        r = math.radians(self._deg)
        c, s = math.cos(r), math.sin(r)
        self._r = ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))
        self._rotated = False
        self._composed = []
        self._direct_translation = False

    def copy(self):
        m = type(self)(self._deg, self._t, self._invertible)
        m._r, m._t, m._rotated = self._r, self._t, self._rotated
        m._direct_translation = self._direct_translation
        return m

    def invert(self):
        if not self._invertible:
            return False
        # (R, t) -> (R^-1, -R^-1 t); a rotation matrix's inverse is its transpose.
        self._r = tuple(zip(*self._r))
        self._deg = -self._deg
        self._t = tuple(-v for v in self._apply_vector(*self._t))
        return True

    @property
    def translation(self):
        """Matrix3D.translation - the offset column as a FRESH Vector3D each read (measured:
        matrix3d-translation-copies-and-refuses-none - the object handed back is never the one
        assigned), so mutating what a read returned moves nothing."""
        return FakeVector3D(*self._t)

    @translation.setter
    def translation(self, vec):
        # Measured: the member takes a Vector3D and NOTHING else. None answers
        # "3 : invalid argument value"; a Point3D/tuple/list/int/Vector2D answers a TYPE error, so
        # duck-typing on x/y/z here would accept a Point3D the live member rejects.
        if vec is None:
            raise RuntimeError("3 : invalid argument value")
        if not isinstance(vec, FakeVector3D):
            raise TypeError("translation takes a Vector3D, not a %s" % type(vec).__name__)
        if self._rotated:
            self._direct_translation = True
        self._t = (float(vec.x), float(vec.y), float(vec.z))

    def setToRotation(self, angle, axis, origin):
        """Matrix3D.setToRotation(angle_rad, axis, origin) -> bool. The translation column becomes
        the PIVOT correction that holds `origin` fixed, so the rotation runs about that point."""
        ax, ay, az = (float(getattr(axis, a, 0.0) or 0.0) for a in ("x", "y", "z"))
        n = (ax * ax + ay * ay + az * az) ** 0.5
        if n < 1e-12:
            return False
        ax, ay, az = ax / n, ay / n, az / n
        c, s, k = math.cos(angle), math.sin(angle), 1.0 - math.cos(angle)
        self._r = ((c + ax * ax * k, ax * ay * k - az * s, ax * az * k + ay * s),
                   (ay * ax * k + az * s, c + ay * ay * k, ay * az * k - ax * s),
                   (az * ax * k - ay * s, az * ay * k + ax * s, c + az * az * k))
        o = tuple(float(getattr(origin, a, 0.0) or 0.0) for a in ("x", "y", "z"))
        self._t = tuple(a - b for a, b in zip(o, self._apply_vector(*o)))
        self._rotated = True
        return True

    def transformBy(self, other):
        """Matrix3D.transformBy(other) -> bool: `other` is applied AFTER this matrix in world
        coordinates (measured: matrix3d-transformby-applies-the-argument-after-self - rotate-then-
        translate lands the offset, the reverse order rotates it). It ACCUMULATES, so two successive
        moves of +5 leave the placement 10 out and a repeated move never reads as unchanged."""
        self._composed.append(other)
        rows = getattr(other, "_r", ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
        self._r = tuple(tuple(sum(rows[i][k] * self._r[k][j] for k in range(3))
                              for j in range(3)) for i in range(3))
        moved = other._apply_vector(*self._t) if hasattr(other, "_apply_vector") else self._t
        self._t = tuple(a + b for a, b in zip(moved, getattr(other, "_t", (0.0, 0.0, 0.0))))
        self._rotated = self._rotated or bool(getattr(other, "_rotated", False))
        return True

    def asArray(self):
        """Matrix3D.asArray - the 16 cells ROW-MAJOR, the translation in elements 3/7/11 (measured:
        matrix3d-asarray-row-major-translation-3-7-11)."""
        return [self._r[0][0], self._r[0][1], self._r[0][2], self._t[0],
                self._r[1][0], self._r[1][1], self._r[1][2], self._t[1],
                self._r[2][0], self._r[2][1], self._r[2][2], self._t[2],
                0.0, 0.0, 0.0, 1.0]

    def getAsCoordinateSystem(self):
        """(origin, xAxis, yAxis, zAxis) - the Python shape of the void-return plus four
        output-parameter API; the axes are this matrix's rotation on the world basis."""
        return (FakePoint(*self._t),
                FakeVector3D(*self._apply_vector(1.0, 0.0, 0.0)),
                FakeVector3D(*self._apply_vector(0.0, 1.0, 0.0)),
                FakeVector3D(*self._apply_vector(0.0, 0.0, 1.0)))

    def _apply_vector(self, x, y, z):
        """The rotation ONLY - what a Vector3D gets, so a direction never picks up a placement."""
        return tuple(row[0] * x + row[1] * y + row[2] * z for row in self._r)

    def _apply_point(self, x, y, z):
        """The rotation AND the translation - what a Point3D gets."""
        vx, vy, vz = self._apply_vector(x, y, z)
        return (vx + self._t[0], vy + self._t[1], vz + self._t[2])


@fusion_fake(live_type="InfiniteLine3D", facts=("shape-dump-infinite-line-and-sphere",))
class FakeInfiniteLine3D:
    """Numeric adsk.core.InfiniteLine3D: origin + direction, with the colinearity test the holder
    profile reduction runs (parallel directions AND the origin offset lying along the direction)."""
    def __init__(self, origin, direction):
        self.origin = origin
        self.direction = direction

    @staticmethod
    def create(origin, direction):
        return FakeInfiniteLine3D(origin, direction)

    def isColinearTo(self, other):
        if not self.direction.isParallelTo(other.direction):
            return False
        off = self.origin.vectorTo(other.origin)
        return off.length < 1e-9 or off.isParallelTo(self.direction)


@fusion_fake(live_type="Plane", facts=("shape-dump-design-world", "enum-surface-types"))
class Plane:
    """surfaceType is intrinsic (a Plane fake IS always PlaneSurfaceType) - set automatically so
    callers reading face.geometry.surfaceType don't each hand-wire the same measured constant.
    create/intersectWithLine mirror the adsk.core.Plane geometry the holder reduction leans on."""
    def __init__(self, normal, origin=None):
        self.normal = normal
        self.origin = origin
        self.surfaceType = _api_facts.ENUMS["core.SurfaceTypes"]["PlaneSurfaceType"]

    @staticmethod
    def create(origin, normal):
        return Plane(normal, origin)

    def intersectWithLine(self, line):
        # Mirrors adsk.core.Plane.intersectWithLine: None for a line parallel to the plane.
        denom = self.normal.dotProduct(line.direction)
        if abs(denom) < 1e-12:
            return None
        t = self.normal.dotProduct(line.origin.vectorTo(self.origin)) / denom
        return FakePoint(line.origin.x + t * line.direction.x,
                         line.origin.y + t * line.direction.y,
                         line.origin.z + t * line.direction.z)


@fusion_fake(live_type="Cylinder", facts=("shape-dump-design-world", "enum-surface-types"))
class Cylinder:
    """surfaceType is intrinsic - see Plane."""
    def __init__(self, axis, origin=None):
        self.axis = axis
        self.origin = origin
        self.surfaceType = _api_facts.ENUMS["core.SurfaceTypes"]["CylinderSurfaceType"]


@fusion_fake(live_type="Cone", facts=("shape-dump-design-world", "enum-surface-types"))
class Cone:
    """A conical surface - mirrors Cylinder's .axis (both expose it per the live _inputs.py
    face-axis code). surfaceType is intrinsic - see Plane."""
    def __init__(self, axis, origin=None):
        self.axis = axis
        self.origin = origin
        self.surfaceType = _api_facts.ENUMS["core.SurfaceTypes"]["ConeSurfaceType"]


@fusion_fake(live_type="Sphere", facts=("shape-dump-infinite-line-and-sphere",))
class Sphere:
    """A spherical surface - the face type _sys_common reports no single axis for. Carries no
    origin/radius: no consumer reads them off this stand-in."""


@fusion_fake(live_type="Torus", facts=("shape-dump-torus", "enum-surface-types"))
class Torus:
    """A toroidal surface - surfaceType is intrinsic, see Plane. `origin` is the torus CENTRE, the
    point the torus keypoint gate lifts into world and compares a JointGeometry against: a torus
    created centred at (2, 3, -1) reads that point back, and the surface's own origin reads
    unchanged after a transform of a COPY of it."""
    def __init__(self, origin=None):
        self.origin = origin
        self.surfaceType = _api_facts.ENUMS["core.SurfaceTypes"]["TorusSurfaceType"]


@fusion_fake(live_type="Line3D", facts=("shape-dump-design-world", "enum-curve3d-types"))
class Line3D:
    """curveType is intrinsic - see Plane."""
    def __init__(self, start=None, end=None):
        self.startPoint = start
        self.endPoint = end
        self.curveType = _api_facts.ENUMS["core.Curve3DTypes"]["Line3DCurveType"]

    def asInfiniteLine(self):
        # Mirrors adsk.core.Line3D.asInfiniteLine: through startPoint along start->end.
        d = self.startPoint.vectorTo(self.endPoint)
        d.normalize()
        return FakeInfiniteLine3D(self.startPoint, d)


@fusion_fake(live_type="Circle3D", facts=("shape-dump-design-world", "enum-curve3d-types"))
class Circle3D:
    """curveType is intrinsic - see Plane."""
    def __init__(self, normal, center=None, radius=None):
        self.normal = normal
        self.center = center
        self.radius = radius
        self.curveType = _api_facts.ENUMS["core.Curve3DTypes"]["Circle3DCurveType"]


# -- viewport / camera fakes -----------------------------------------------

def camera_state(cam):
    """The field values one camera carries - what a restore is judged on, since a viewport read
    hands back a copy."""
    return (cam.eye, cam.target, cam.upVector, cam.cameraType, cam.isFitView,
            cam.perspectiveAngle, cam.viewExtents)

@fusion_fake(live_type="Camera",
             facts=("shape-dump-design-world", "camera-viewextents-is-linear-not-area"))
class Camera:
    """A viewport camera: the eye/target/up an orient rewrites, the projection and perspective
    angle an orient read-back gates on, and the LINEAR viewExtents a zoom scales. `eye`, `target`
    and `up` take a FakePoint or an (x, y, z) tuple."""
    def __init__(self, eye=(10.0, 10.0, 10.0), target=(0.0, 0.0, 0.0), up=None,
                 camera_type=None, is_fit_view=False, perspective_angle=0.0,
                 view_extents=100.0):
        self.eye = FakePoint(*eye) if isinstance(eye, tuple) else eye
        self.target = FakePoint(*target) if isinstance(target, tuple) else target
        self.upVector = FakePoint(*up) if isinstance(up, tuple) else up
        self.cameraType = (_api_facts.ENUMS["core.CameraTypes"]["OrthographicCameraType"]
                           if camera_type is None else camera_type)
        self.isFitView = is_fit_view
        self.perspectiveAngle = perspective_angle
        self.viewExtents = view_extents

    def _copy(self):
        """A detached duplicate carrying the same field values - what a viewport read hands back
        (BEHAVIOR['viewport_camera_returns_copy']). Private: live Camera exposes no copy()."""
        dup = type(self).__new__(type(self))
        dup.__dict__.update(self.__dict__)
        return dup


@fusion_fake(live_type="Viewport", facts=("shape-dump-design-world", "camera-returns-copy"))
class Viewport:
    """The viewport a view tool drives: its camera, visual style, pixel size, and the image saves
    a capture makes. What the tool DID is recorded privately: `_assigned` every camera written
    back (`_original_camera` is the one the viewport started with), `_calls` the refresh/save order,
    `_fit_calls` the framing passes, `_options_used` the last save options object.

    fit() and refresh() answer the bool the SDK declares them to; `fit_ok=False` is the fit the
    platform declines, which cam_activate_setup gates on with `vp.fit() is not True`.

    Reading `camera` hands back a detached copy where BEHAVIOR['viewport_camera_returns_copy'] is
    True (row camera-returns-copy), so a tool that mutates a read camera without assigning it back
    moves nothing here."""
    def __init__(self, camera=None, width=1516, height=757, visual_style=0,
                 frame=(200.0, 100.0), fit_ok=True, save_ok=True, png=b"PNGBYTES"):
        self._cam = Camera() if camera is None else camera
        self._original_camera = self._cam
        self._assigned = []
        self._fit_calls = 0
        self._calls = []
        self._options_used = None
        self._frame = frame
        self._fit_ok = fit_ok
        self._save_ok, self._png = save_ok, png
        self.width, self.height = width, height
        self.visualStyle = visual_style

    @property
    def camera(self):
        if _api_facts.BEHAVIOR["viewport_camera_returns_copy"]:
            return self._cam._copy()
        return self._cam

    @camera.setter
    def camera(self, value):
        self._assigned.append(value)
        self._cam = value

    def fit(self):
        self._fit_calls += 1
        return self._fit_ok

    def refresh(self):
        self._calls.append("refresh")
        return True

    def viewToModelSpace(self, pt):
        """The model point under a screen point: the frame maps linearly onto the `frame`
        rectangle, so the spans a framing pass measures come out exactly frame wide and high."""
        import adsk.core
        frame_w, frame_h = self._frame
        return adsk.core.Point3D.create((pt.x / self.width - 0.5) * frame_w, 0.0,
                                        -(pt.y / self.height - 0.5) * frame_h)

    def saveAsImageFile(self, path, width, height):
        self._calls.append("save")
        return self._write_png(path)

    def saveAsImageFileWithOptions(self, options):
        self._calls.append("save_with_options")
        self._options_used = options
        return self._write_png(options.filename)

    def _write_png(self, path):
        if not self._save_ok:
            return False
        with open(path, "wb") as handle:
            handle.write(self._png)
        return True
