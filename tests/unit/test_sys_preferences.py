"""Unit tests for sys_get_preferences.py + sys_set_preferences.py - the application preferences pair.

app.preferences is application-scoped: an assignment is immediate, there is no commit step and no
undo, so the read must never guess a value it could not read and the write must refuse anything it
cannot restore. The stand-ins model the three shapes that make that hard: a member whose getter
RAISES, a setter that accepts a value and silently ignores it, and a setter that clamps.
"""

import json
import types

import pytest

from conftest import FakeApplication, load_tool

get = load_tool("sys_get_preferences")          # load first: the setter imports this module
setp = load_tool("sys_set_preferences")

# Member paths whose table entry declares an enum family, snapshotted at import - BEFORE the
# fixture below strips the families the mocked adsk would otherwise fake.
_DECLARES_A_FAMILY = {f"{key}.{m.name}" for key in get.GROUP_KEYS
                      for m in get.GROUP_MEMBERS[key].values() if m.enum is not None}


class Group:
    """A preferences group object: plain members, plus the three platform behaviours that matter -
    a getter that raises, a setter that is accepted and ignored, and a setter that clamps."""

    def __init__(self, values, raises=(), frozen=(), clamp=None):
        object.__setattr__(self, "_v", dict(values))
        object.__setattr__(self, "_raises", set(raises))
        object.__setattr__(self, "_frozen", set(frozen))
        object.__setattr__(self, "_clamp", dict(clamp or {}))

    def __dir__(self):
        # dir() LISTS a member whose getter raises - measured on this build's graphics group, where
        # three members are dir()-visible and RuntimeError on read. A fake that hid its raising
        # members would report them as names the build never had, so the presence read under test
        # would be exercised against a shape the platform does not have.
        return sorted(set(object.__dir__(self)) | set(self._v) | set(self._raises))

    def __getattr__(self, name):
        if name in self._raises:
            raise RuntimeError(f"3 : {name} is not available on this build")
        if name in self._v:
            return self._v[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in self._raises:
            raise RuntimeError(f"3 : {name} is not available on this build")
        if name in self._frozen:
            return                                  # accepted, and silently ignored
        self._v[name] = self._clamp.get(name, value)


class Coll:
    """A count/item collection (productPreferences / defaultUnitsPreferences), holding one
    per-product object per item."""

    def __init__(self, pairs):
        self._items = [obj for _name, obj in pairs]

    @property
    def count(self):
        return len(self._items)

    def item(self, i):
        return self._items[i]


class Choices:
    """A stand-in enum family under INVENTED member names - it impersonates no adsk family, so no
    measured enum int is hand-typed here. The decode walks the class it is given and is
    name-agnostic, so invented members exercise it exactly as real ones do."""
    FirstChoice = 0
    SecondChoice = 1
    ThirdChoice = 2


def _defaults_for(key):
    """Every member of one group with a plausible value, so a test only states what it cares about."""
    out = {}
    for name, member in get.GROUP_MEMBERS[key].items():
        if member.by_name:
            out[name] = types.SimpleNamespace(name="Steel")
        elif name.startswith("is") or name.startswith("are"):
            out[name] = False
        else:
            out[name] = 0
    return out


def _make_prefs(values=None, raises=None, frozen=None, clamp=None, products=("Design",)):
    """An app.preferences stand-in carrying every group in the table - the flat groups as objects,
    the collection groups as count/item collections of per-product objects."""
    values, raises = values or {}, raises or {}
    frozen, clamp = frozen or {}, clamp or {}
    ns = types.SimpleNamespace()
    for key, attr, _members in get.GROUPS:
        vals = _defaults_for(key)
        vals.update(values.get(key, {}))
        args = (raises.get(key, ()), frozen.get(key, ()), clamp.get(key))
        if key in get.COLLECTION_KEYS:
            setattr(ns, attr, Coll([(name, Group(dict(vals, name=name), *args))
                                    for name in products]))
        else:
            setattr(ns, attr, Group(vals, *args))
    return ns


class _MutePreferencesApp(FakeApplication):
    """A session whose app.preferences read RAISES - the read that will not answer, which is not
    the same state as a preferences object holding nothing."""

    @property
    def preferences(self):
        raise RuntimeError("the application preferences are unavailable")

    @preferences.setter
    def preferences(self, value):
        pass


@pytest.fixture(autouse=True)
def no_mock_enum_families(monkeypatch):
    """The mocked adsk resolves EVERY enum family name to a child Mock, and dir() on a Mock yields
    ints (call_count=0) - so an un-neutralised table would decode and validate against noise, and a
    test could pass on a family that does not exist. Every member starts here with no family; a
    test that exercises the enum path installs the real stand-in with _wire_enum."""
    for key in get.GROUP_KEYS:
        monkeypatch.setitem(get.GROUP_MEMBERS, key,
                            {n: m._replace(enum=None) for n, m in get.GROUP_MEMBERS[key].items()})


def _wire_enum(monkeypatch, group, member, family=Choices):
    """Give one member a REAL enum family object (the table otherwise carries none in tests)."""
    members = dict(get.GROUP_MEMBERS[group])
    members[member] = members[member]._replace(enum=family)
    monkeypatch.setitem(get.GROUP_MEMBERS, group, members)


def _wire_minimum(monkeypatch, group, member, minimum):
    """Give one member a documented minimum - the table's own column, wired per test so the bound is
    exercised on whichever value TYPE the test is about."""
    members = dict(get.GROUP_MEMBERS[group])
    members[member] = members[member]._replace(minimum=minimum)
    monkeypatch.setitem(get.GROUP_MEMBERS, group, members)


@pytest.fixture
def prefs(monkeypatch):
    """The default application: readable everywhere, nothing frozen."""
    p = _make_prefs(values={
        "general": {"isAutomaticVersioningEnabled": True, "automateVersioningTimeInterval": 60,
                    "defaultModelingOrientation": 1},
        "display": {"generalPrecision": 3, "angularPrecision": 1, "isPeriodDecimalPoint": True},
        "compatibility": {"recoverSaveScanFrequency": 15},
        # is-prefixed and yet an ENUM INT, measured - the type check must key on the read value
        "products": {"isFirstComponentGroundToParent": True, "isAutoLookAtSketch2": 2,
                     "defaultDesignType": 2},
        "units_defaults": {"defaultUnitSystem": 1},
    })
    for mod in (get, setp):
        monkeypatch.setattr(mod, "app", FakeApplication(preferences=p))
    return p


def _payload(res):
    assert res["isError"] is False, res
    return json.loads(res["content"][0]["text"])


def _message(res):
    assert res["isError"] is True, res
    return res["message"]


# ── the read: default projection, slices, honest nulls ──────────────────────────────────────────

class TestDefaultProjection:
    def test_default_is_the_actionable_shortlist_only(self, prefs):
        out = _payload(get.handler())["preferences"]
        assert set(out) == {"general", "display", "products", "units_defaults"}
        assert set(out["general"]) == {"isAutomaticVersioningEnabled",
                                       "automateVersioningTimeInterval",
                                       "isAutomaticSaveOnCloseEnabled", "defaultModelingOrientation"}
        assert set(out["display"]) == {"generalPrecision", "angularPrecision", "isPeriodDecimalPoint"}
        assert set(out["products"]["Design"]) == {"isFirstComponentGroundToParent",
                                                  "defaultDesignType"}
        assert out["products"]["Design"]["isFirstComponentGroundToParent"]["value"] is True

    def test_the_default_never_pulls_a_full_group(self, prefs):
        out = _payload(get.handler())["preferences"]
        assert "compatibility" not in out and "graphics" not in out
        assert len(out["units_defaults"]["Design"]) == 3

    def test_default_publishes_value_and_tier_for_every_key(self, prefs):
        rec = _payload(get.handler())["preferences"]["display"]["generalPrecision"]
        assert rec == {"value": 3, "tier": get.TIER_WRITABLE}

    def test_note_names_the_groups_not_yet_pulled(self, prefs):
        note = _payload(get.handler())["note"]
        for key in get.GROUP_KEYS:
            assert key in note, f"include slice '{key}' is invisible - the note never names it"

    def test_the_note_states_how_a_nested_member_is_addressed(self, prefs):
        assert "'<group>.<product>.<member>'" in _payload(get.handler())["note"]

    def test_note_drops_a_group_once_it_is_included(self, prefs):
        note = _payload(get.handler(include=["graphics"]))["note"]
        assert "'graphics'" not in note and "graphics" not in note.split("include=")[1]


class TestIncludeSlices:
    def test_include_adds_exactly_that_group_in_full(self, prefs):
        out = _payload(get.handler(include=["grid"]))["preferences"]
        default = set(_payload(get.handler())["preferences"])
        assert set(out) == default | {"grid"}
        assert set(out["grid"]) == {"isLayoutGridLockEnabled"}

    def test_include_takes_several_groups_at_once(self, prefs):
        out = _payload(get.handler(include=["grid", "network"]))["preferences"]
        assert {"grid", "network"} <= set(out)

    def test_included_group_returns_every_member_of_the_table(self, prefs):
        out = _payload(get.handler(include=["graphics"]))["preferences"]["graphics"]
        assert set(out) == set(get.GROUP_MEMBERS["graphics"])

    def test_unknown_include_is_refused_naming_the_valid_groups(self, prefs):
        msg = _message(get.handler(include=["usage_data"]))
        assert "usage_data" in msg and "graphics" in msg

    def test_the_include_enum_matches_the_group_keys(self):
        # Catches a HAND-EDITED schema drifting from the table's own keys. It cannot catch a key
        # added to GROUPS - both sides read it - which is what the dispatch test below covers.
        enum = get.tool.input_schema["properties"]["include"]["items"]["enum"]
        assert sorted(enum) == sorted(get.GROUP_KEYS)

    def test_every_advertised_group_actually_dispatches(self, prefs):
        # A key the guard admits but whose group object does not read publishes nothing: the schema
        # would offer a group the read never returns.
        for key in get.GROUP_KEYS:
            out = _payload(get.handler(include=[key]))["preferences"]
            assert key in out, key
            listed = out[key]["Design"] if key in get.COLLECTION_KEYS else out[key]
            assert set(listed) == set(get.GROUP_MEMBERS[key]), key

    def test_compatibility_group_carries_the_measured_members(self, prefs):
        out = _payload(get.handler(include=["compatibility"]))["preferences"]["compatibility"]
        assert out["recoverSaveScanFrequency"]["value"] == 15
        assert out["qtRenderingInterface"]["tier"] == get.TIER_REFUSED
        assert out["isEventPerformanceLogged"]["tier"] == get.TIER_WRITABLE

    def test_a_collection_group_nests_by_product_name(self, prefs):
        out = _payload(get.handler(include=["products"]))["preferences"]["products"]
        assert set(out) == {"Design"}
        assert set(out["Design"]) == set(get.GROUP_MEMBERS["products"])
        assert out["Design"]["isAutoLookAtSketch2"]["value"] == 2

    def test_every_product_item_is_published(self, monkeypatch):
        p = _make_prefs(products=("Design", "CAM"))
        monkeypatch.setattr(get, "app", FakeApplication(preferences=p))
        out = _payload(get.handler(include=["units_defaults"]))["preferences"]["units_defaults"]
        assert set(out) == {"Design", "CAM"}

    def test_an_unreadable_nested_member_is_named_by_its_full_path(self, monkeypatch):
        p = _make_prefs(raises={"products": ("isJointPreviewAnimated",)})
        monkeypatch.setattr(get, "app", FakeApplication(preferences=p))
        out = _payload(get.handler(include=["products"]))
        assert out["unreadable"] == ["products.Design.isJointPreviewAnimated"]


class TestHonestReads:
    def test_unreadable_member_reports_null_and_is_counted(self, monkeypatch):
        p = _make_prefs(raises={"graphics": ("autoThrottleEffects", "isLimitEffectsDuringNavigation")})
        monkeypatch.setattr(get, "app", FakeApplication(preferences=p))
        out = _payload(get.handler(include=["graphics"]))
        rec = out["preferences"]["graphics"]["autoThrottleEffects"]
        assert rec["value"] is None and rec["unreadable"] is True
        assert out["unreadable_count"] == 2
        assert "graphics.autoThrottleEffects" in out["unreadable"]

    def test_a_member_this_build_does_not_carry_is_unknown_not_unreadable(self, monkeypatch):
        # The two are different facts. A member that RAISES is a platform fact about this build; a
        # member the object does not carry at all means the row asking for it is wrong. Collapsing
        # them publishes a repo typo as "Fusion raises on this" - the exact false platform claim
        # the census exists to prevent.
        p = _make_prefs()
        del p.gridPreferences._v["isLayoutGridLockEnabled"]
        monkeypatch.setattr(get, "app", FakeApplication(preferences=p))
        out = _payload(get.handler(include=["grid"]))
        rec = out["preferences"]["grid"]["isLayoutGridLockEnabled"]
        assert rec["value"] is None
        assert rec["unknown_member"] is True and "unreadable" not in rec
        assert out["unknown_members"] == ["grid.isLayoutGridLockEnabled"]
        assert "unreadable" not in out           # never counted as a platform raise

    def test_a_raising_member_is_unreadable_even_though_reading_it_fails(self, monkeypatch):
        # The other side of the same split: the member IS present (dir lists it) and only the read
        # failed, so it is unreadable. A presence read that answered "absent" for it - however it is
        # spelled - would publish a platform member as a bad table row.
        p = _make_prefs(raises={"grid": ("isLayoutGridLockEnabled",)})
        monkeypatch.setattr(get, "app", FakeApplication(preferences=p))
        out = _payload(get.handler(include=["grid"]))
        rec = out["preferences"]["grid"]["isLayoutGridLockEnabled"]
        assert rec["unreadable"] is True and "unknown_member" not in rec
        assert "unknown_members" not in out

    def test_a_member_that_reads_none_is_not_reported_unreadable(self, monkeypatch):
        p = _make_prefs(values={"material": {"appearanceOverride": None}})
        monkeypatch.setattr(get, "app", FakeApplication(preferences=p))
        rec = _payload(get.handler(include=["material"]))["preferences"]["material"]["appearanceOverride"]
        assert rec["value"] is None and "unreadable" not in rec

    def test_object_member_is_published_by_name(self, prefs):
        # by_name is the DECLARED shape - the .name is the reading, so the record carries no
        # non_scalar flag; that flag marks a member the table reads as a scalar handing back an object.
        rec = _payload(get.handler(include=["material"]))["preferences"]["material"]["defaultMaterial"]
        assert rec["value"] == "Steel" and "non_scalar" not in rec

    def test_a_by_name_member_whose_name_is_empty_falls_back_to_its_type(self, monkeypatch):
        # an empty name is no reading at all - publishing "" would read as a material actually
        # called nothing.
        class Material:
            name = ""
        p = _make_prefs(values={"material": {"defaultMaterial": Material()}})
        monkeypatch.setattr(get, "app", FakeApplication(preferences=p))
        rec = _payload(get.handler(include=["material"]))["preferences"]["material"]["defaultMaterial"]
        assert rec["value"] == "Material" and rec["non_scalar"] is True

    def test_an_object_valued_member_does_not_sink_the_read(self, monkeypatch):
        # a member the table reads as a scalar can hand back an OBJECT; the whole payload is
        # JSON-encoded in one call, so that one member would raise and take every other member's
        # reading down with it. It is published by name and flagged instead.
        p = _make_prefs(values={"display": {"generalPrecision": types.SimpleNamespace(name="High")}})
        monkeypatch.setattr(get, "app", FakeApplication(preferences=p))
        out = _payload(get.handler(include=["display"]))["preferences"]["display"]
        assert out["generalPrecision"] == {"value": "High", "tier": get.TIER_WRITABLE,
                                           "non_scalar": True}
        assert out["angularPrecision"]["value"] == 0      # the rest of the group still reads

    def test_a_product_item_whose_name_is_not_a_string_is_dropped(self, monkeypatch):
        # The item name becomes a JSON object KEY and part of the '<group>.<product>.<member>'
        # address the write resolves. json.dumps rejects a key that is not str/int/float/bool/None,
        # so one object-valued name would sink the entire read - the same payload-sinking class the
        # scalar guard fixes one layer up.
        p = _make_prefs(products=("Design", "CAM"))
        p.productPreferences._items[1]._v["name"] = types.SimpleNamespace(name="CAM")
        monkeypatch.setattr(get, "app", FakeApplication(preferences=p))
        out = _payload(get.handler(include=["products"]))["preferences"]["products"]
        assert set(out) == {"Design"}

    def test_a_nameless_object_member_is_published_by_its_type(self, monkeypatch):
        class Precision:
            pass
        p = _make_prefs(values={"display": {"generalPrecision": Precision()}})
        monkeypatch.setattr(get, "app", FakeApplication(preferences=p))
        rec = _payload(get.handler(include=["display"]))["preferences"]["display"]["generalPrecision"]
        assert rec["value"] == "Precision" and rec["non_scalar"] is True

    def test_a_by_name_member_that_reports_no_name_falls_back_to_its_type(self, monkeypatch):
        # by_name says "publish .name"; an object that has none still may not reach json.dumps.
        class Material:
            pass
        p = _make_prefs(values={"material": {"defaultMaterial": Material()}})
        monkeypatch.setattr(get, "app", FakeApplication(preferences=p))
        rec = _payload(get.handler(include=["material"]))["preferences"]["material"]["defaultMaterial"]
        assert rec["value"] == "Material" and rec["non_scalar"] is True

    def test_a_scalar_member_is_not_flagged_non_scalar(self, prefs):
        rec = _payload(get.handler())["preferences"]["display"]["generalPrecision"]
        assert rec["value"] == 3 and "non_scalar" not in rec

    def test_no_active_preferences_is_an_error(self, monkeypatch):
        monkeypatch.setattr(get, "app", _MutePreferencesApp())
        msg = _message(get.handler())
        assert "preferences" in msg and "read" in msg


class TestEnumDecoding:
    def test_enum_member_reports_the_name_beside_the_int(self, prefs, monkeypatch):
        _wire_enum(monkeypatch, "general", "defaultModelingOrientation")
        rec = _payload(get.handler())["preferences"]["general"]["defaultModelingOrientation"]
        assert rec["value"] == 1 and rec["enum"] == "SecondChoice"

    def test_unknown_enum_int_degrades_to_the_bare_int(self, monkeypatch):
        p = _make_prefs(values={"general": {"defaultModelingOrientation": 77}})
        monkeypatch.setattr(get, "app", FakeApplication(preferences=p))
        _wire_enum(monkeypatch, "general", "defaultModelingOrientation")
        rec = _payload(get.handler())["preferences"]["general"]["defaultModelingOrientation"]
        assert rec["value"] == 77 and "enum" not in rec

    def test_a_member_with_no_family_publishes_the_bare_int(self, prefs):
        rec = _payload(get.handler())["preferences"]["display"]["generalPrecision"]
        assert rec["value"] == 3 and "enum" not in rec

    def test_enum_names_is_empty_when_the_family_is_absent(self):
        assert get.enum_names(None) == {}

    def test_a_bool_is_never_decoded_as_an_enum_int(self):
        assert get.enum_member_name(Choices, True) is None


# ── the write: tiers, the four-step protocol, honest failures ───────────────────────────────────

class TestTierRefusals:
    @pytest.mark.parametrize("member,fragment", [
        ("general.graphicsDriver", "unrenderable"),
        ("network.proxyHost", "cloud connectivity"),
        ("api.defaultPathForScriptsAndAddIns", "add-ins"),
        ("general.userLanguage", "UI language"),
        ("material.defaultMaterial", "NEW body"),
        ("general.activeUserInterfaceTheme", "read-only"),
        ("compatibility.chromiumGraphicsBackend", "unrenderable"),
        ("compatibility.isAcceleratedDataTransfer", "cloud upload/download"),
        ("products.Design.isAutoLookAtSketch", "isAutoLookAtSketch2"),
        ("compatibility.isLogHTTPRequestAndResponseBodies", "credentials and tokens"),
        ("compatibility.isUseLatestSpaceMouseDriver", "3D input device"),
    ])
    def test_tier_R_write_is_refused_naming_the_member_and_the_reason(self, prefs, member, fragment):
        msg = _message(setp.handler(member=member, value=1))
        assert member.split(".")[-1] in msg and fragment in msg
        assert "Nothing was written" in msg

    def test_a_refused_member_is_not_assigned(self, prefs):
        before = prefs.networkPreferences.proxyPort
        setp.handler(member="network.proxyPort", value=8080)
        assert prefs.networkPreferences.proxyPort == before


class TestWriteProtocol:
    def test_a_write_publishes_previous_beside_now(self, prefs):
        out = _payload(setp.handler(member="display.generalPrecision", value=4))
        assert out["previous"] == 3 and out["now"] == 4
        assert out["member"] == "display.generalPrecision" and out["tier"] == get.TIER_WRITABLE
        assert prefs.unitAndValuePreferences.generalPrecision == 4

    def test_the_value_is_restorable_from_the_published_previous(self, prefs):
        first = _payload(setp.handler(member="display.generalPrecision", value=4))
        back = _payload(setp.handler(member="display.generalPrecision",
                                     value=first["previous"]))
        assert back["now"] == 3 and prefs.unitAndValuePreferences.generalPrecision == 3

    def test_write_is_refused_when_the_current_value_cannot_be_read(self, monkeypatch):
        p = _make_prefs(raises={"graphics": ("autoThrottleEffects",)})
        monkeypatch.setattr(setp, "app", FakeApplication(preferences=p))
        msg = _message(setp.handler(member="graphics.autoThrottleEffects", value=True))
        assert "autoThrottleEffects" in msg and "not written" in msg

    def test_a_silent_noop_setter_is_an_error(self, monkeypatch):
        p = _make_prefs(values={"display": {"generalPrecision": 3}},
                        frozen={"display": ("generalPrecision",)})
        monkeypatch.setattr(setp, "app", FakeApplication(preferences=p))
        msg = _message(setp.handler(member="display.generalPrecision", value=4))
        assert "did not take" in msg and "3" in msg and "4" in msg

    def test_a_clamped_value_is_an_error_naming_what_landed(self, monkeypatch):
        p = _make_prefs(values={"display": {"generalPrecision": 3}},
                        clamp={"display": {"generalPrecision": 8}})
        monkeypatch.setattr(setp, "app", FakeApplication(preferences=p))
        msg = _message(setp.handler(member="display.generalPrecision", value=99))
        assert "8" in msg and "99" in msg and "3" in msg
        # observed values only - the tool saw a value, not a mechanism
        assert "clamp" not in msg.lower() and "ignored" not in msg.lower()

    def test_a_raising_setter_is_an_error(self, monkeypatch):
        p = _make_prefs(raises={"grid": ("isLayoutGridLockEnabled",)})
        monkeypatch.setattr(setp, "app", FakeApplication(preferences=p))
        msg = _message(setp.handler(member="grid.isLayoutGridLockEnabled", value=True))
        assert "grid.isLayoutGridLockEnabled" in msg and "raises" in msg


class TestCollectionMembers:
    def test_a_nested_member_round_trips_through_its_product(self, prefs):
        out = _payload(setp.handler(member="products.Design.isAutoProjectGeometry", value=True))
        assert out["member"] == "products.Design.isAutoProjectGeometry"
        assert out["previous"] is False and out["now"] is True
        assert prefs.productPreferences.item(0).isAutoProjectGeometry is True

    def test_a_nested_path_without_the_product_is_refused(self, prefs):
        msg = _message(setp.handler(member="products.isAutoProjectGeometry", value=True))
        assert "products.<product>.<member>" in msg and "Nothing was written" in msg

    def test_an_unknown_product_is_refused_listing_the_products(self, prefs):
        msg = _message(setp.handler(member="products.Nope.isAutoProjectGeometry", value=True))
        assert "Nope" in msg and "Design" in msg and "Nothing was written" in msg

    def test_a_flat_group_rejects_a_three_part_path(self, prefs):
        assert "grid.<member>" in _message(
            setp.handler(member="grid.Design.isLayoutGridLockEnabled", value=True))

    def test_an_is_prefixed_enum_int_member_refuses_a_boolean(self, prefs):
        # isAutoLookAtSketch2 is is-prefixed and holds an INT: the type check keys on the value read
        # back, never on the member's name.
        msg = _message(setp.handler(member="products.Design.isAutoLookAtSketch2", value=True))
        assert "integer" in msg
        assert prefs.productPreferences.item(0).isAutoLookAtSketch2 == 2

    def test_an_is_prefixed_enum_int_member_accepts_an_int(self, prefs):
        assert _payload(setp.handler(member="products.Design.isAutoLookAtSketch2", value=0))["now"] == 0


class TestDocumentedMinimum:
    def test_a_value_below_the_documented_minimum_is_refused_before_assignment(self, prefs):
        msg = _message(setp.handler(member="compatibility.recoverSaveScanFrequency", value=0))
        assert "at least 1" in msg and "Nothing was written" in msg
        assert prefs.compatibilityPreferences.recoverSaveScanFrequency == 15

    def test_a_value_at_the_minimum_is_accepted(self, prefs):
        assert _payload(setp.handler(member="compatibility.recoverSaveScanFrequency",
                                     value=1))["now"] == 1


class TestNonFiniteValues:
    """json.loads accepts NaN / Infinity / -Infinity, so either reaches 'value'. Neither is a number
    a preference can hold, this write has no undo, and int() of one RAISES - so both are refused
    before the assignment, naming the value that arrived."""

    @pytest.fixture
    def float_member(self, monkeypatch):
        """A member whose CURRENT value reads as a float, so the write takes the float branch."""
        p = _make_prefs(values={"graphics": {"hiddenEdgeDimming": 0.5}})
        monkeypatch.setattr(setp, "app", FakeApplication(preferences=p))
        return p

    @pytest.mark.parametrize("value,named", [(float("nan"), "nan"), (float("inf"), "inf"),
                                             (float("-inf"), "-inf")])
    def test_a_non_finite_float_member_write_is_refused_naming_the_value(self, float_member,
                                                                         value, named):
        msg = _message(setp.handler(member="graphics.hiddenEdgeDimming", value=value))
        assert named in msg.lower() and "finite" in msg
        assert "Nothing was written" in msg
        assert float_member.graphicsPreferences.hiddenEdgeDimming == 0.5

    @pytest.mark.parametrize("value,named", [(float("nan"), "nan"), (float("inf"), "inf")])
    def test_a_non_finite_integer_member_write_is_refused_rather_than_crashing(self, prefs,
                                                                              value, named):
        # int(nan) raises ValueError, so without the guard the fractional-value check itself is
        # where the call dies - an exception out of the handler, not a refusal.
        msg = _message(setp.handler(member="display.generalPrecision", value=value))
        assert named in msg.lower() and "finite" in msg
        assert prefs.unitAndValuePreferences.generalPrecision == 3

    def test_a_finite_float_is_still_accepted(self, float_member):
        assert _payload(setp.handler(member="graphics.hiddenEdgeDimming", value=0.25))["now"] == 0.25


class TestFloatMinimum:
    """The documented minimum is a property of the MEMBER, not of the int branch: a float member
    carrying one is bounded by exactly the same refusal."""

    @pytest.fixture
    def dimming(self, monkeypatch):
        p = _make_prefs(values={"graphics": {"hiddenEdgeDimming": 0.5}})
        monkeypatch.setattr(setp, "app", FakeApplication(preferences=p))
        _wire_minimum(monkeypatch, "graphics", "hiddenEdgeDimming", 1.0)
        return p

    def test_a_float_below_the_minimum_is_refused_before_assignment(self, dimming):
        msg = _message(setp.handler(member="graphics.hiddenEdgeDimming", value=0.999))
        assert "at least 1.0" in msg and "0.999" in msg
        assert "Nothing was written" in msg
        assert dimming.graphicsPreferences.hiddenEdgeDimming == 0.5

    def test_a_float_at_the_minimum_is_accepted(self, dimming):
        # the boundary is >=, not >: the minimum is itself a legal value
        assert _payload(setp.handler(member="graphics.hiddenEdgeDimming", value=1.0))["now"] == 1.0

    def test_a_float_member_without_a_minimum_is_unbounded(self, monkeypatch):
        p = _make_prefs(values={"graphics": {"hiddenEdgeDimming": 0.5}})
        monkeypatch.setattr(setp, "app", FakeApplication(preferences=p))
        assert _payload(setp.handler(member="graphics.hiddenEdgeDimming", value=-4.0))["now"] == -4.0


class TestWriteGuards:
    def test_a_missing_member_is_refused(self, prefs):
        msg = _message(setp.handler(value=4))
        assert "Provide 'member'" in msg

    def test_a_missing_value_is_refused(self, prefs):
        assert "Provide 'value'" in _message(setp.handler(member="display.generalPrecision"))

    def test_a_bare_member_name_is_refused_naming_the_groups(self, prefs):
        msg = _message(setp.handler(member="generalPrecision", value=4))
        assert "display" in msg and "general" in msg

    def test_an_unknown_group_is_refused_naming_the_groups(self, prefs):
        msg = _message(setp.handler(member="usage_data.isEnabled", value=5))
        assert "usage_data" in msg and "graphics" in msg

    def test_an_unknown_member_lists_that_groups_members(self, prefs):
        msg = _message(setp.handler(member="grid.isLayoutGridLocked", value=True))
        assert "isLayoutGridLockEnabled" in msg

    def test_the_member_path_is_case_insensitive(self, prefs):
        assert _payload(setp.handler(member="DISPLAY.generalprecision", value=4))["now"] == 4

    def test_a_boolean_for_an_integer_member_is_refused_before_assignment(self, prefs):
        msg = _message(setp.handler(member="display.generalPrecision", value=True))
        assert "integer" in msg
        assert prefs.unitAndValuePreferences.generalPrecision == 3

    def test_a_number_for_a_boolean_member_is_refused(self, prefs):
        assert "boolean" in _message(setp.handler(member="grid.isLayoutGridLockEnabled", value=1))

    def test_a_fractional_value_for_an_integer_member_is_refused(self, prefs):
        assert "integer" in _message(setp.handler(member="display.generalPrecision", value=4.5))

    def test_an_integer_is_accepted_for_a_float_member(self, prefs):
        out = _payload(setp.handler(member="general.offlineCachePeriod", value=30))
        # the value LANDED on the preference, and 'now'/'previous' are the read-back around it -
        # not an echo of the request (the equality alone passes for a write that never happened)
        assert prefs.generalPreferences.offlineCachePeriod == 30
        assert out["now"] == 30 and out["previous"] == 0


class TestEnumValues:
    @pytest.fixture
    def enum_member(self, prefs, monkeypatch):
        _wire_enum(monkeypatch, "general", "defaultModelingOrientation")

    def test_an_enum_member_name_is_resolved_to_its_int(self, prefs, enum_member):
        out = _payload(setp.handler(member="general.defaultModelingOrientation",
                                    value="FirstChoice"))
        assert out["now"] == 0 and out["enum"] == "FirstChoice"

    def test_an_unknown_enum_name_is_refused_listing_the_members(self, prefs, enum_member):
        msg = _message(setp.handler(member="general.defaultModelingOrientation", value="Fourth"))
        assert "FirstChoice=0" in msg and "SecondChoice=1" in msg
        assert prefs.generalPreferences.defaultModelingOrientation == 1

    def test_an_int_outside_the_enum_is_refused_before_assignment(self, prefs, enum_member):
        # measured: the platform ACCEPTS and STORES an out-of-enum int, so the read-back gate can
        # never catch it - only a refusal before the assignment can.
        msg = _message(setp.handler(member="general.defaultModelingOrientation", value=99))
        assert "99 is not a value" in msg and "ThirdChoice=2" in msg
        assert "Nothing was written" in msg
        assert prefs.generalPreferences.defaultModelingOrientation == 1

    def test_a_legal_int_is_still_accepted_on_an_enum_member(self, prefs, enum_member):
        out = _payload(setp.handler(member="general.defaultModelingOrientation", value=2))
        assert out["now"] == 2 and out["enum"] == "ThirdChoice"
        assert prefs.generalPreferences.defaultModelingOrientation == 2

    def test_an_int_is_unconstrained_when_the_member_has_no_family(self, prefs):
        assert _payload(setp.handler(member="display.generalPrecision", value=9))["now"] == 9

    def test_text_on_a_non_enum_member_is_refused(self, prefs):
        assert "not text" in _message(setp.handler(member="display.generalPrecision", value="4"))


class TestTheWireDescribesWhatHappens:
    def test_the_refusal_promise_covers_only_the_pre_assignment_guards(self, monkeypatch):
        # a read-back mismatch happens AFTER setattr, so it cannot be sold as "nothing was written"
        p = _make_prefs(values={"display": {"generalPrecision": 3}},
                        frozen={"display": ("generalPrecision",)})
        monkeypatch.setattr(setp, "app", FakeApplication(preferences=p))
        assert "Nothing was written" not in _message(
            setp.handler(member="display.generalPrecision", value=4))
        assert "Nothing was written" in _message(
            setp.handler(member="network.proxyHost", value="x"))


class TestEnumFamiliesAreDeclared:
    @pytest.mark.parametrize("path", [
        "display.footAndInchDisplayFormat", "display.degreeDisplayFormat",
        "display.materialDisplayUnit", "graphics.selectionDisplayStyle",
        "graphics.degradedSelectionDisplayStyle", "graphics.transparencyEffects",
        "api.defaultScriptLanguage", "api.defaultAddInLanguage",
        "general.defaultModelingOrientation", "products.isAutoLookAtSketch2",
        "units_defaults.defaultUnitSystem",
    ])
    def test_an_enum_valued_member_declares_its_family(self, path):
        # a member that ships without one publishes a bare int AND loses the setter's name path
        assert path in _DECLARES_A_FAMILY

    def test_no_mock_family_reaches_a_test(self):
        # the mocked adsk answers any family name with a Mock, whose dir() yields call_count=0 - an
        # int the validation would accept as a legal enum value. The fixture must strip them.
        for key in get.GROUP_KEYS:
            for m in get.GROUP_MEMBERS[key].values():
                assert m.enum is None or isinstance(m.enum, type), f"{key}.{m.name} carries a fake"


class TestOneSharedTierTable:
    def test_the_setter_reads_the_readers_table(self):
        assert setp._prefs is get

    def test_a_member_turned_R_in_the_table_is_labelled_and_refused(self, prefs, monkeypatch):
        members = dict(get.GROUP_MEMBERS["grid"])
        members["isLayoutGridLockEnabled"] = members["isLayoutGridLockEnabled"]._replace(
            tier=get.TIER_REFUSED, reason="the operator set it deliberately")
        monkeypatch.setitem(get.GROUP_MEMBERS, "grid", members)
        rec = _payload(get.handler(include=["grid"]))["preferences"]["grid"]["isLayoutGridLockEnabled"]
        assert rec["tier"] == get.TIER_REFUSED
        msg = _message(setp.handler(member="grid.isLayoutGridLockEnabled", value=True))
        assert "the operator set it deliberately" in msg

    def test_every_refused_member_carries_a_reason(self):
        missing = [f"{key}.{m.name}" for key in get.GROUP_KEYS
                   for m in get.GROUP_MEMBERS[key].values()
                   if m.tier == get.TIER_REFUSED and not m.reason.strip()]
        assert not missing, f"tier R members with no reason to quote in the refusal: {missing}"


class TestDeclaredOutputs:
    def test_the_read_publishes_its_declared_output(self, prefs):
        payload = _payload(get.handler())
        for spec in get.RETURNS:
            assert spec.assert_present(payload) == ""

    def test_the_write_publishes_its_declared_output(self, prefs):
        payload = _payload(setp.handler(member="display.generalPrecision", value=4))
        for spec in setp.RETURNS:
            assert spec.assert_present(payload) == ""
