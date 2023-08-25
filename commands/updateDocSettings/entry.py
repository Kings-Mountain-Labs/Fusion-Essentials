import json
import adsk.core, adsk.fusion
import os
from ...lib import fusion360utils as futil
from ... import config
from datetime import datetime

app = adsk.core.Application.get()

# Local list of event handlers used to maintain a reference so
# they are not released and garbage collected.
local_handlers = []

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
        design.fusionUnitsManager.defaultLengthUnits = 'in'

