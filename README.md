# Fusion-Essentials
 A small set of QoL improvements fro your Fusion 360 workflow

## Installation
You have a few options for installing Fusion Essentials. The easiest way is to download the repo as a zip file and following these instructions [here](https://medium.com/@arstein/installing-and-running-fusion-360-add-ins-3ffcd7546adc) to install the add-in.
If you are familiar with git, you can clone the repo into your add-ins folder.

## Features
1. **Add Holder** This command provides a quick way to add a toolholder to the tool library (although at the moment I am still working out how to save it into the tool library), currently it only supports single body toolholders (this will be fixed in the future).
2. **Clean Chamfer** This command will take a set of surfaces that form a existing chamfer and turn them into a single freeform surface with the isocurves aligned to the original surfaces. This is useful for interpolating chamfers with a ball endmill, although it is made largely obsolete by the Pencil operation.
3. **Automatically Enable Design History** This command will automatically enable design history for for what it perceives to be a newly imported file.
4. **Automatically Switch Units** This command will automatically switch the units of a newly imported file to the units of the current document.
5. **Ability to Change Settings** You can enable/disable or change the default units and the settings will persist between sessions. (there is no grantee that they will persist over updates of the add-in, until a 1.0 release is made)