#  Copyright 2023 by Ian Rist

import adsk.core, adsk.fusion
from ...lib import fusion360utils as futil
from ... import config
from ... import shared_state

app = adsk.core.Application.get()

CMD_ID = f'{config.COMPANY_NAME}_{config.ADDIN_NAME}_updateDocSettings'

# Local list of event handlers used to maintain a reference so
# they are not released and garbage collected.
local_handlers = []

DEFAULT_SETTINGS = {
    "settings": {
        "option_checkbox": {
            "type": "checkbox",
            "label": "Automatically change units",
            "default": True
        },
        "units": {
            "type": "dropdown",
            "label": "Units",
            "options": ["in", "mm", "ft", "m", "cm"],
            "default": "in"
        }
    }
}

# Initialize the settings on first use
if not shared_state.load_settings(CMD_ID):
    shared_state.save_settings(CMD_ID, DEFAULT_SETTINGS)

# Executed when add-in is run.
def start():
    futil.add_handler(app.documentOpened, update_doc_settings, local_handlers=local_handlers)

def stop():
    local_handlers = []

# Event handler for the documentOpening event.
def update_doc_settings(args: adsk.core.DocumentEventArgs):
    eventArgs = adsk.core.DocumentEventArgs.cast(args)
    # Make sure that it is the first time opening the document and that it is a Fusion Design and not Eagle or something.
    # Creating a new document will not trigger this event, so it should only trigger with imported files.
    if  eventArgs.document.dataFile.versions.count == 1 and eventArgs.document.objectType == "adsk::fusion::FusionDocument":
        design = adsk.fusion.FusionDocument.cast(eventArgs.document).design
        design.designType = adsk.fusion.DesignTypes.ParametricDesignType
        update_units, unit = get_settings()
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
