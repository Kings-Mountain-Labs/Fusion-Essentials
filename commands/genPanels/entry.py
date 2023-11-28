import adsk.core, adsk.fusion
from ... import config

app = adsk.core.Application.get()
ui = app.userInterface

WORKSPACE_ID = 'FusionSolidEnvironment'

def start():
    workspace = ui.workspaces.itemById(WORKSPACE_ID)
    toolbar_tab = workspace.toolbarTabs.add(config.design_tab_id, config.design_tab_name)
    panel = toolbar_tab.toolbarPanels.add(config.tools_panel_id, config.tools_panel_name, config.tools_panel_after, False)

def stop():
    # Get the various UI elements for this command
    workspace = ui.workspaces.itemById(WORKSPACE_ID)
    panel = workspace.toolbarPanels.itemById(config.tools_panel_id)
    toolbar_tab = workspace.toolbarTabs.itemById(config.design_tab_id)
    panel.deleteMe()
    toolbar_tab.deleteMe()