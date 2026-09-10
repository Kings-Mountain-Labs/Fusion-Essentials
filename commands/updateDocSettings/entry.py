import adsk.core, adsk.fusion
from ...lib import fusion360utils as futil
from ... import config
from ... import shared_state

app = adsk.core.Application.get()
ui = app.userInterface

CMD_ID = f'{config.COMPANY_NAME}_{config.ADDIN_NAME}_updateDocSettings'
CMD_NAME = 'Automatic Import Settings'

DEFAULT_SETTINGS = {
    "option_checkbox": {
        "type": "checkbox",
        "label": "Automatically change units",
        "default": False
    },
    "units": {
        "type": "dropdown",
        "label": "Units",
        "options": ["in", "mm", "ft", "m", "cm"],
        "default": "mm"
    }
}

# Initialize the settings on first use
shared_state.load_settings_init(CMD_ID, CMD_NAME, DEFAULT_SETTINGS, None)

# Executed when add-in is run.
def start():
    futil.add_handler(app.documentOpened, update_doc_settings)

def stop():
    futil.clear_handlers()

# Event handler for the documentOpened event.
def update_doc_settings(eventArgs: adsk.core.DocumentEventArgs):
    # Make sure that it is the first time opening the document and that it is a Fusion Design and not Eagle or something.
    # Creating a new document will not trigger this event, so it should only trigger with imported files.
    #
    # CRITICAL: this runs INSIDE Fusion's documentOpened callback on the main thread, mid-open. Any
    # exception here is thrown during the document load itself and can destabilize/CRASH the session
    # - observed live opening a multi-reference CAM document: 'eventArgs.document.dataFile' was None
    # (the dataFile isn't populated yet at this point in a heavy/reference-resolving open), so the
    # original 'eventArgs.document.dataFile.versions.count' raised AttributeError mid-open. So EVERY
    # access is now defensive and the whole body is wrapped: this handler must NEVER raise.
    try:
        doc = eventArgs.document
        if doc is None or doc.objectType != "adsk::fusion::FusionDocument":
            return
        data_file = doc.dataFile          # may be None during a heavy/cold open - do not chain
        if data_file is None:
            return
        try:
            is_first_version = data_file.versions.count == 1
        except Exception:
            return                         # versions not available yet; skip silently
        if not is_first_version:
            return
        design = adsk.fusion.FusionDocument.cast(doc).design
        if not design:
            return
        design.designType = adsk.fusion.DesignTypes.ParametricDesignType
        update_units, unit = get_settings()
        futil.log(f"Update units: {update_units}, unit: {unit}")
        if update_units and unit is not None:
            design.fusionUnitsManager.distanceDisplayUnits = unit
    except Exception:
        # Never let this open-time handler raise - log and swallow so the document open can't crash.
        futil.log("update_doc_settings: skipped (guarded) - could not apply doc settings on open")

def get_settings():
    settings = shared_state.load_settings(CMD_ID)
    units: adsk.fusion.DistanceUnits = None
    if settings["units"]["default"] == "in":
        units = adsk.fusion.DistanceUnits.InchDistanceUnits
    elif settings["units"]["default"] == "mm":
        units = adsk.fusion.DistanceUnits.MillimeterDistanceUnits
    elif settings["units"]["default"] == "ft":
        units = adsk.fusion.DistanceUnits.FootDistanceUnits
    elif settings["units"]["default"] == "m":
        units = adsk.fusion.DistanceUnits.MeterDistanceUnits
    elif settings["units"]["default"] == "cm":
        units = adsk.fusion.DistanceUnits.CentimeterDistanceUnits
    else:
        units = adsk.fusion.DistanceUnits.InchDistanceUnits
    return (settings["option_checkbox"]["default"], units)
