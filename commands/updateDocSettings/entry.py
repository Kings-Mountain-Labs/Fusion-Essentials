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

# Event handler for the documentOpening event.
def update_doc_settings(eventArgs: adsk.core.DocumentEventArgs):
    # Make sure that it is the first time opening the document and that it is a Fusion Design and not Eagle or something.
    # Creating a new document will not trigger this event, so it should only trigger with imported files.
    # futil.log(f"{eventArgs.document.isSaved} {eventArgs.document.isModified} {eventArgs.document.isActive}")
    if eventArgs.document.dataFile.versions.count == 1 and eventArgs.document.objectType == "adsk::fusion::FusionDocument":
        design = adsk.fusion.FusionDocument.cast(eventArgs.document).design
        design.designType = adsk.fusion.DesignTypes.ParametricDesignType
        update_units, unit = get_settings()
        futil.log(f"Update units: {update_units}, unit: {unit}")
        if update_units:
            design.fusionUnitsManager.distanceDisplayUnits = unit

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
