# Copyright (c) Fusion-Essentials contributors
# Dual-licensed under the MIT and Apache-2.0 licenses; see LICENSE-MIT and LICENSE-APACHE.

"""The document, cloud-data and export worlds: the session seam, the cloud tree, the exporters."""

from tests.fakes.scaffold import _NamedCollection, fusion_fake


# -- document world --------------------------------------------------------

@fusion_fake(live_type="Selection", facts=("shape-dump-document-world",))
class FakeSelection:
    """One entry of ui.activeSelections: the picked entity and the click point."""
    def __init__(self, entity=None, point=None):
        self.entity = entity
        self.point = point


@fusion_fake(live_type="Selections", facts=("shape-dump-document-world",))
class FakeSelections:
    """ui.activeSelections: the counted/item walk a selection read makes, plus the clear() a
    selection tool drives - which answers the bool its caller gates on ("Returns true if
    successful") and empties the walk only then. The calls are counted privately in _cleared."""
    def __init__(self, selections=(), clear_ok=True):
        self._items = list(selections)
        self._clear_ok = clear_ok
        self._cleared = 0

    @property
    def count(self):
        return len(self._items)

    def item(self, i):
        return _NamedCollection(self._items).item(i)

    def clear(self):
        self._cleared += 1
        if self._clear_ok:
            self._items = []
        return self._clear_ok


@fusion_fake(live_type="UserInterface", facts=("shape-dump-document-world",))
class FakeUserInterface:
    """app.userInterface as the selection and workspace reads reach it: its activeSelections, the
    activeSelectionChanged event a pick listener attaches to, and the workspace walk.

    `selection_changed`, `workspaces` and `active_workspace` are set only when given: no shape dump
    covers Workspace or the event object, so the stand-ins for those come from the test that needs
    them, and a UI answering neither read stays a testable state. `active_workspace=None` is the
    DIFFERENT state where the read answers "no workspace is active"."""

    _UNSET = object()

    def __init__(self, selections=None, selection_changed=None, workspaces=_UNSET,
                 active_workspace=_UNSET):
        self.activeSelections = FakeSelections() if selections is None else selections
        if selection_changed is not None:
            self.activeSelectionChanged = selection_changed
        if workspaces is not FakeUserInterface._UNSET:
            self.workspaces = _NamedCollection(workspaces)
        if active_workspace is not FakeUserInterface._UNSET:
            self.activeWorkspace = active_workspace


@fusion_fake(live_type="Products", facts=("shape-dump-document-world",))
class FakeProducts:
    """doc.products: itemByProductType is the lookup every product read goes through
    ('DesignProductType' / 'CAMProductType'); a product this document does not carry answers None,
    which is the no-Manufacture-workspace state a CAM tool gates on."""
    def __init__(self, design=None, cam=None):
        self._by_type = {"DesignProductType": design, "CAMProductType": cam}
        self._items = [p for p in (design, cam) if p is not None]

    @property
    def count(self):
        return len(self._items)

    def item(self, i):
        return _NamedCollection(self._items).item(i)

    def itemByProductType(self, product_type):
        return self._by_type.get(product_type)


# The message a read on a CLOSED document's held wrapper raises (measured).
_CLOSED_DOCUMENT_READ = "3 : An API Object refers to a deleted Object"


@fusion_fake(live_type="FusionDocument",
             facts=("shape-dump-document-world", "closed-document-wrapper-reads",
                    "closed-document-name-raises"))
class FakeFusionDocument:
    """A design document - the concrete class a Fusion design answers, Document being the base.
    Carries the design/products a tool reaches its product through, the dataFile a save state is
    read off, and the save/saveAs/saveMilestone/close/activate writes, each answering the bool its
    caller gates on. `closed=True` is the measured closed wrapper: isValid reads False and a name
    read RAISES; close() puts a live one into that state. `save_versions=False` is the declared
    state of a save that answers True and versions nothing - no measurement row carries it - so the
    version number does not move and the document stays MODIFIED, which is the only read that
    separates it from a save that landed. `save_raises` is the message a refusing save THROWS with,
    which a caller must surface rather than swallow into a false ok."""
    def __init__(self, name="Untitled", design=None, data_file=None, is_saved=False,
                 is_modified=True, is_active=True, is_visible=True,
                 version=None, references=(), products=None,
                 save_ok=True, close_ok=True, activate_ok=True, save_versions=True, closed=False,
                 save_raises=None):
        self._name = name
        self._closed = closed
        self.design = design
        self.dataFile = data_file
        self.isSaved = is_saved
        self.isModified = is_modified
        self.isActive = is_active
        self.isVisible = is_visible
        self.version = version
        self.documentReferences = _NamedCollection(list(references))
        self.products = FakeProducts(design=design) if products is None else products
        self._save_ok, self._close_ok, self._activate_ok = save_ok, close_ok, activate_ok
        self._save_versions = save_versions
        self._save_raises = save_raises
        self._saves = []
        self._closes = []
        self._activates = 0

    @property
    def name(self):
        if self._closed:
            raise RuntimeError(_CLOSED_DOCUMENT_READ)
        return self._name

    @name.setter
    def name(self, value):
        self._name = value

    @property
    def isValid(self):
        return not self._closed

    def _record_save(self, kind, args):
        self._saves.append((kind, args))
        if self._save_raises:
            raise RuntimeError(self._save_raises)
        if not self._save_ok:
            return False
        if not self._save_versions:
            return True
        held = getattr(self.dataFile, "versionNumber", None)
        if isinstance(held, int):
            self.dataFile.versionNumber = held + 1
        self.isModified = False
        self.isSaved = True
        return True

    def save(self, description=""):
        return self._record_save("save", (description,))

    def saveAs(self, name, folder, description="", tag=""):
        return self._record_save("saveAs", (name, folder, description, tag))

    def saveMilestone(self, name, description=""):
        return self._record_save("milestone", (name, description))

    def close(self, save_changes=False):
        self._closes.append(bool(save_changes))
        if self._close_ok:
            self._closed = True
        return self._close_ok

    def activate(self):
        # counted as well as flagged: isActive alone cannot tell a document that was ASKED to come
        # forward from one that was already there.
        self._activates += 1
        if self._activate_ok:
            self.isActive = True
        return self._activate_ok


@fusion_fake(live_type="Documents", facts=("shape-dump-document-world",))
class FakeDocuments:
    """app.documents: the POSITIONAL walk an 'open:N' address indexes, plus add/open/
    openUsingContext - each records its arguments, appends the document it answers to the walk, and
    hands it back. `new_document` is that document; None builds a fresh FakeFusionDocument."""
    def __init__(self, documents=(), new_document=None):
        self._items = list(documents)
        self._new = new_document
        self._opened = []

    @property
    def count(self):
        return len(self._items)

    def item(self, i):
        return _NamedCollection(self._items).item(i)

    def _admit(self, record):
        self._opened.append(record)
        doc = self._new if self._new is not None else FakeFusionDocument()
        self._items.append(doc)
        return doc

    def add(self, document_type):
        return self._admit(("add", document_type))

    def open(self, data_file, visible=True):
        return self._admit(("open", data_file, bool(visible)))

    def openUsingContext(self, data_file, context, visible=True):
        return self._admit(("openUsingContext", data_file, context, bool(visible)))


@fusion_fake(live_type="Data", facts=("shape-dump-document-world", "shape-dump-data-world"))
class FakeData:
    """app.data: the active hub/project a cloud read starts from, the hubs walk, the project list
    that dataProjects.add mints into, and findFileById/findFolderById - each answering what is
    registered under that id, None otherwise. activeHub is a PROPERTY whose assignment lands (the
    measured default) and which a scenario subclass can take and refuse to change; every
    assignment is recorded in _hub_sets whether or not it lands."""
    def __init__(self, active_project=None, projects=(), active_hub=None, files_by_id=None,
                 hubs=(), folders_by_id=None):
        self.activeProject = active_project
        self._active_hub = active_hub
        self._hub_sets = []
        listed = list(projects) or ([active_project] if active_project is not None else [])
        self.dataProjects = _CloudProjects(listed)
        self.dataHubs = _NamedCollection(list(hubs))
        self._files = dict(files_by_id or {})
        self._folders = dict(folders_by_id or {})

    @property
    def activeHub(self):
        return self._active_hub

    @activeHub.setter
    def activeHub(self, hub):
        self._hub_sets.append(hub)
        self._active_hub = hub

    def findFileById(self, file_id):
        return self._files.get(file_id)

    def findFolderById(self, folder_id):
        return self._folders.get(folder_id)


@fusion_fake(live_type="Application", facts=("shape-dump-document-world",))
class FakeApplication:
    """The session seam a tool reads through: activeDocument and the documents walk, the
    activeProduct a design comes off, app.data and app.userInterface. `import_manager`,
    `active_viewport`, `version` and `preferences` are set only when a test supplies one -
    ImportManager and Preferences carry no shape dump to build a shared stand-in from, and a session
    whose viewport, build number or preferences do not read is its own tested state."""
    def __init__(self, active_document=None, documents=None, active_product=None, data=None,
                 user_interface=None, import_manager=None, active_viewport=None, version=None,
                 preferences=None):
        self.activeDocument = active_document
        self.documents = FakeDocuments() if documents is None else documents
        self.activeProduct = active_product
        self.data = FakeData() if data is None else data
        self.userInterface = FakeUserInterface() if user_interface is None else user_interface
        if import_manager is not None:
            self.importManager = import_manager
        if active_viewport is not None:
            self.activeViewport = active_viewport
        if version is not None:
            self.version = version
        if preferences is not None:
            self.preferences = preferences


_REFRESH_REFUSED = "2 : InternalValidationError : res"


@fusion_fake(live_type="DocumentReference",
             facts=("shape-dump-document-world", "derive-reference-version-setter-present"))
class FakeDocumentReference:
    """One Document.documentReferences entry: the source dataFile, the version it holds (settable),
    isOutOfDate, and getLatestVersion().

    MEASURED on a genuinely STALE DeriveFeature reference: both refresh routes refuse with
    RuntimeError(_REFRESH_REFUSED) - the version ASSIGNMENT and getLatestVersion() alike - while
    isOutOfDate and dataFile.* keep reading. So a reference built out_of_date REFUSES by default,
    and naming any other refresh outcome below declares a state instead of that measurement. The
    cloud tier's ACT 11b derive leg is where that refusal is read on a run.

    `refresh_lands` is the refresh that takes (version moves to the file's latest, isOutOfDate
    clears) - DECLARED, not measured, and the only reference measured at all was a derive link's.
    `stays_out_of_date` is the platform LIE, the refresh answering True while isOutOfDate stays
    True; `refresh_ok` False is getLatestVersion refusing out loud; `latest_raises`/`setter_raises`
    carry a message of the caller's own."""
    def __init__(self, data_file=None, version=1, out_of_date=False, refresh_ok=True,
                 stays_out_of_date=False, latest_raises=None, setter_raises=None,
                 refresh_lands=False):
        self.dataFile = data_file
        self._version = version
        self.isOutOfDate = out_of_date
        self._refresh_ok = refresh_ok
        self._stays_out_of_date = stays_out_of_date
        declared = bool(refresh_lands or stays_out_of_date or not refresh_ok
                        or latest_raises or setter_raises)
        refuses = bool(out_of_date) and not declared
        self._latest_raises = latest_raises or (_REFRESH_REFUSED if refuses else None)
        self._setter_raises = setter_raises or (_REFRESH_REFUSED if refuses else None)

    @property
    def version(self):
        return self._version

    @version.setter
    def version(self, value):
        if self._setter_raises:
            raise RuntimeError(self._setter_raises)
        # Declared, not measured: on the measured reference the assignment never lands, so the
        # staleness this recomputes is a state only an opted-in test sees.
        self._version = value
        self.isOutOfDate = value != getattr(self.dataFile, "latestVersionNumber", None)

    def getLatestVersion(self):
        if self._latest_raises:
            raise RuntimeError(self._latest_raises)
        if not self._refresh_ok:
            return False
        latest = getattr(self.dataFile, "latestVersionNumber", None)
        self._version = latest if isinstance(latest, int) else self._version
        self.isOutOfDate = bool(self._stays_out_of_date)
        return True


@fusion_fake(factory_for="FakeApplication")
def make_document_world(design=None, name="Untitled", data_file=None, others=(), selections=(),
                        project=None):
    """An Application whose ACTIVE document holds `design` and answers it as its DesignProductType
    product, with the already-built `others` open beside it in the documents walk, `project` as
    app.data's active project and `selections` held by app.userInterface."""
    active = FakeFusionDocument(name=name, design=design, data_file=data_file)
    return FakeApplication(active_document=active,
                           documents=FakeDocuments([active] + list(others)),
                           active_product=design,
                           data=FakeData(active_project=project),
                           user_interface=FakeUserInterface(FakeSelections(selections)))


# -- data world ------------------------------------------------------------

class _CloudArray(_NamedCollection):
    """A cloud collection as the data walks read it: the counted/item/itemByName walk plus the
    asArray() bulk fetch every dataFiles/dataFolders/dataProjects enumeration goes through."""

    def asArray(self):
        return list(self)


class _CloudFolders(_CloudArray):
    """A folder's dataFolders: the walk plus add(name), which creates the child IN the folder, so
    the re-list a create verifies through sees it."""

    def __init__(self, folder, items, raises=None):
        super().__init__(items, raises=raises)
        self._folder = folder

    def add(self, name):
        return self._folder._add_folder(name)


class _CloudProjects(_CloudArray):
    """A hub's dataProjects: the walk plus add(name, purpose, contributors), which mints a project
    into the walk and answers it. The arguments land in _added."""

    def __init__(self, items=(), raises=None):
        super().__init__(items, raises=raises)
        self._added = []

    def add(self, name, purpose="", contributors=""):
        self._added.append((name, purpose, contributors))
        project = FakeDataProject(name=name, project_id="newid:" + name)
        self._items.append(project)
        return project


@fusion_fake(live_type="DataFile", facts=("shape-dump-data-world",))
class FakeDataFile:
    """One cloud file: the name/id/versionId identity a resolve keys on, its versionNumber beside
    latestVersionNumber (the pair a freshness read compares), where it lives, the parent-reference
    pair a delete's orphan guard reads, and the move/deleteMe writes, each answering the bool its
    caller gates on and only then changing what a read-back sees. `file_id` is the LINEAGE urn,
    unset unless a test supplies one. `parent_refs_raise` is the reference read that will not
    answer: 'flag' fails at hasParentReferences, 'property' at the parentReferences read itself, and
    'array' one step later, when the collection it handed back is ENUMERATED.
    `latest_raises` is the tip number that will not read at all - the state a version report has to
    publish as unreadable rather than diagnose. `versions` are the OLDER files a restore picks from
    (`versions_raise` the cloud read of them that will not answer at all) and
    promote() answers the bool its caller gates on; `is_milestone=None` is the mark flag that will
    not read, and `milestones` is set only when a test supplies the collection, a file that answers
    no milestone read being its own tested state.

    `child_refs` are the files this one references (`child_refs_raise` is the flag answering True
    over an enumeration that then throws), copy() lands a NEW file carrying the SOURCE name in the
    target folder - it takes no name, which is why a rename follows it - `copy_ok=False` is the copy
    that answers nothing, and `rename_ok=False` the file whose name assignment RAISES.
    `is_configured_design` is the Configured Design flag a DataFile carries (the data-world shape
    dump lists isConfiguredDesign on the type), set only when a test asks - a file left alone does
    not answer that read at all, which is the state every caller's default covers."""
    def __init__(self, name="Part", file_id=None, version=1, latest_version=None, extension="f3d",
                 parent_folder=None, parent_project=None, version_id=None, is_complete=True,
                 move_ok=True, delete_ok=True, web_url=None, parent_refs=(),
                 parent_refs_raise=None, latest_raises=False, versions=(), versions_raise=None,
                 promote_ok=True, promote_raises=None, is_milestone=False, milestones=None,
                 child_refs=(),
                 child_refs_raise=False, copy_ok=True, rename_ok=True,
                 date_created=1_700_000_000, description="", is_configured_design=None):
        self._rename_ok = rename_ok
        self._name = name
        self._child_refs = list(child_refs)
        self._child_refs_raise = child_refs_raise
        self._copy_ok = copy_ok
        self.dateCreated = date_created
        self.description = description
        self.id = file_id
        self.versionNumber = version
        self._latest_version = version if latest_version is None else latest_version
        self._latest_raises = latest_raises
        self._is_milestone = is_milestone
        if milestones is not None:
            self.milestones = milestones
        if is_configured_design is not None:
            self.isConfiguredDesign = is_configured_design
        self._versions = list(versions)
        self._versions_raise = versions_raise
        self._promote_ok, self._promote_raises = promote_ok, promote_raises
        self._promotes = 0
        self.versionId = version_id
        self.fileExtension = extension
        self.fusionWebURL = web_url
        self.parentFolder = parent_folder
        self.parentProject = parent_project
        self.isComplete = is_complete
        self._move_ok, self._delete_ok = move_ok, delete_ok
        self._parent_refs = list(parent_refs)
        self._parent_refs_raise = parent_refs_raise
        self._moves = []
        self._deleted = False

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        if not self._rename_ok:
            raise RuntimeError("3 : name is read-only on this file")
        self._name = value

    @property
    def latestVersionNumber(self):
        if self._latest_raises:
            raise RuntimeError("2 : InternalValidationError")
        return self._latest_version

    @latestVersionNumber.setter
    def latestVersionNumber(self, value):
        self._latest_version = value

    @property
    def isMilestone(self):
        # None is the flag that will NOT read - a distinct state from False, which a caller has to
        # report as unreadable rather than as a mark that is absent.
        if self._is_milestone is None:
            raise RuntimeError("3 : cloud read failed")
        return self._is_milestone

    @property
    def versions(self):
        return _CloudArray(self._versions, raises=self._versions_raise)

    def promote(self):
        self._promotes += 1
        if self._promote_raises:
            raise RuntimeError(self._promote_raises)
        return self._promote_ok

    @property
    def hasChildReferences(self):
        return True if self._child_refs_raise else bool(self._child_refs)

    @property
    def childReferences(self):
        return _CloudArray(self._child_refs,
                           raises="3 : cloud read failed" if self._child_refs_raise else None)

    def copy(self, target_folder):
        if not self._copy_ok:
            return None
        made = FakeDataFile(self._name, file_id="urn:adsk.file:copy", rename_ok=self._rename_ok,
                            parent_folder=target_folder,
                            parent_project=getattr(target_folder, "parentProject", None))
        target_folder._files.append(made)
        return made

    @property
    def hasParentReferences(self):
        if self._parent_refs_raise == "flag":
            raise RuntimeError("3 : cloud read failed")
        return True if self._parent_refs_raise == "array" else bool(self._parent_refs)

    @property
    def parentReferences(self):
        if self._parent_refs_raise == "property":
            raise RuntimeError("3 : cloud read failed")
        return _CloudArray(self._parent_refs,
                           raises=("3 : cloud read failed"
                                   if self._parent_refs_raise == "array" else None))

    def move(self, folder):
        self._moves.append(folder)
        if self._move_ok:
            self.parentFolder = folder
        return self._move_ok

    def deleteMe(self):
        if self._delete_ok:
            self._deleted = True
        return self._delete_ok


@fusion_fake(live_type="DataFolder", facts=("shape-dump-data-world",))
class FakeDataFolder:
    """One folder of the cloud tree: its name/id, the dataFiles and dataFolders a bounded walk
    reads, the parent pair a path is rebuilt from, isRoot, uploadFile answering the future a poll
    handle is minted from, and deleteMe answering the bool its caller gates on. A child whose own
    deleteMe() SUCCEEDED is gone from both walks, so a delete read-back sees what live sees; one
    that refused stays. `files_raise`/`folders_raise` are the enumeration that will not answer at
    all - a HOLE in a walk, which is not the same answer as an empty folder. A folder created
    through dataFolders.add inherits the upload pair, so a mkdir -p destination behaves like the
    root it was made under."""
    def __init__(self, name="Root", folder_id=None, files=(), folders=(), parent_folder=None,
                 parent_project=None, is_root=False, delete_ok=True, files_raise=None,
                 folders_raise=None, upload_future=None, upload_raises=None):
        self.name = name
        self.id = folder_id
        self._files = [f if hasattr(f, "name") else FakeDataFile(f) for f in files]
        self._folders = list(folders)
        self.parentFolder = parent_folder
        self.parentProject = parent_project
        self.isRoot = is_root
        self._delete_ok = delete_ok
        self._files_raise, self._folders_raise = files_raise, folders_raise
        self._upload_future, self._upload_raises = upload_future, upload_raises
        self._uploads = []
        self._deleted = False
        for child in self._files + self._folders:
            child.parentFolder = self

    @property
    def dataFiles(self):
        return _CloudArray([f for f in self._files if not getattr(f, "_deleted", False)],
                           raises=self._files_raise)

    @property
    def dataFolders(self):
        return _CloudFolders(self, [f for f in self._folders if not getattr(f, "_deleted", False)],
                             raises=self._folders_raise)

    def _add_folder(self, name):
        child = FakeDataFolder(name, folder_id="fid:" + name, parent_folder=self,
                               parent_project=self.parentProject,
                               upload_future=self._upload_future,
                               upload_raises=self._upload_raises)
        self._folders.append(child)
        return child

    def uploadFile(self, path):
        if self._upload_raises:
            raise RuntimeError(self._upload_raises)
        self._uploads.append(path)
        return self._upload_future

    def deleteMe(self):
        if self._delete_ok:
            self._deleted = True
        return self._delete_ok


@fusion_fake(live_type="DataProject", facts=("shape-dump-data-world",))
class FakeDataProject:
    """One project: its name/id and the rootFolder every folder path is walked from."""
    def __init__(self, name="Project", project_id=None, root_folder=None):
        self.name = name
        self.id = project_id
        self.rootFolder = (FakeDataFolder("Root", is_root=True) if root_folder is None
                           else root_folder)


def _stamp_project(folder, project):
    """Stamp `project` on a folder and everything under it - the parentProject hop a file's
    location is published from."""
    folder.parentProject = project
    for child in list(folder._files) + list(folder._folders):
        child.parentProject = project
        if isinstance(child, FakeDataFolder):
            _stamp_project(child, project)


@fusion_fake(factory_for="FakeDataProject")
def make_data_tree(name="Project", files=(), folders=()):
    """A project whose ROOT folder holds `files` (names or FakeDataFile objects) and `folders` with
    their own nested contents, everything under it back-linked to its folder and to the project -
    the two hops a file's location is published from."""
    root = FakeDataFolder("Root", files=files, folders=list(folders), is_root=True)
    project = FakeDataProject(name=name, root_folder=root)
    _stamp_project(root, project)
    return project


# -- export world ----------------------------------------------------------

# Reading DXFSketchExportOptions.units aborts the live transaction, so the knob is modelled as
# unreadable: a regression that touches it - even through safe() - goes red in a test run.
_DXF_UNITS_READ = "3 : Distance unit is not supported by DXF"


@fusion_fake(scenario_double="a bag for the *ExportOptions family (STL/STEP/OBJ/DXFSketch/...), no "
                             "member of which is a SHAPES dump: it publishes nothing of its own - "
                             "every attribute on one is what its factory seeded or what the tool "
                             "under test assigned - so it has no live surface to sweep")
class _ExportOptions:
    """One created options object: the kind/path/geometry its factory recorded, plus free attribute
    get/set (also by subscript) for the knobs a tool writes and reads back. `drops` names the
    properties whose assignment is SWALLOWED - the read-back keeps answering whatever `seeded` put
    there, which is the one state a read-back check can catch - and `raises_on` maps a property to
    the message its READ throws with. A knob no test names is ABSENT, never a made-up default."""
    def __init__(self, kind, path, geom=None, drops=(), seeded=None, raises_on=None):
        object.__setattr__(self, "_drops", set(drops))
        object.__setattr__(self, "_raises_on", dict(raises_on or {}))
        for name, value in dict(seeded or {}).items():
            object.__setattr__(self, name, value)
        self.kind = kind
        self.path = path
        self.geom = geom

    def __getattr__(self, k):
        # runs only for an attribute nothing set, so an unreadable knob stays unreadable however the
        # code under test reaches it.
        message = (self.__dict__.get("_raises_on") or {}).get(k)
        if message:
            raise RuntimeError(message)
        raise AttributeError(k)

    def __setattr__(self, k, v):
        if k in self.__dict__.get("_drops", ()):
            return
        object.__setattr__(self, k, v)

    def __getitem__(self, k):
        return getattr(self, k)

    def __setitem__(self, k, v):
        setattr(self, k, v)


@fusion_fake(live_type="ExportManager",
             facts=("shape-dump-document-world", "export-arg-orders",
                    "fusion-archive-execute-bool-vs-landed-file"))
class FakeExportManager:
    """design.exportManager: one create*ExportOptions factory per format, and the execute() every
    write goes through. The MEASURED arg orders differ by format - STL/3MF/OBJ take (geometry,
    path), the rest (path, geometry) - and each factory records its options bag on the private
    _calls walk before handing it back. `options_class` swaps that bag for a scenario subclass, the
    way an options object that drops a write is reached.

    execute() writes a stub file at the options' path unless `writes(opts)` answers False, then
    answers `execute_ok`: the bool and the file are separate knobs because a FALSE execute() over a
    landed file is measured, so the disk is the verdict and the bool is not."""
    def __init__(self, options_class=None, execute_ok=True, writes=None):
        self._options_class = _ExportOptions if options_class is None else options_class
        self._execute_ok = execute_ok
        self._writes = writes
        self._calls = []
        self._executed = None

    def _opt(self, kind, path, geom, **kw):
        rec = self._options_class(kind, path, geom, **kw)
        self._calls.append(rec)
        return rec

    def createSTEPExportOptions(self, path, geom=None):
        return self._opt("step", path, geom)

    def createIGESExportOptions(self, path, geom=None):
        return self._opt("iges", path, geom)

    def createSATExportOptions(self, path, geom=None):
        return self._opt("sat", path, geom)

    def createSMTExportOptions(self, path, geom=None):
        return self._opt("smt", path, geom)

    def createUSDExportOptions(self, path, geom=None):
        return self._opt("usd", path, geom)

    def createFusionArchiveExportOptions(self, path, geom=None):
        return self._opt("f3d", path, geom)

    def createSTLExportOptions(self, geom, path):
        return self._opt("stl", path, geom)

    def createC3MFExportOptions(self, geom, path):
        return self._opt("3mf", path, geom)

    def createOBJExportOptions(self, geom, path):
        return self._opt("obj", path, geom)

    def createDXFSketchExportOptions(self, path, sketch):
        return self._opt("dxf", path, sketch, raises_on={"units": _DXF_UNITS_READ})

    def execute(self, opts):
        self._executed = opts
        if self._writes is None or self._writes(opts):
            with open(opts.path, "w") as fh:
                fh.write("export-stub")
        return self._execute_ok
