# Fusion-Essentials
 A small set of QoL improvements fro your Fusion 360 workflow.

## Features
1. **Add Holder** This command provides a quick way to add a toolholder to the tool library, currently it only supports single body toolholders (this will be fixed in the future).
2. **Clean Chamfer** This command will take a set of surfaces that form a existing chamfer and turn them into a single freeform surface with the isocurves aligned to the original surfaces. This is useful for interpolating chamfers with a ball endmill, although it is made largely obsolete by the Pencil operation.
3. **Automatically Enable Design History** This command will automatically enable design history for for what it perceives to be a newly imported file.
4. **Automatically Switch Units** This command will automatically switch the units of a newly imported file to the units of the current document.
5. **Ability to Change Settings** You can enable/disable or change the default units and the settings will persist between sessions. There is no guarantee that they will persist over updates of the add-in, until a 1.0 release is made.
6. **Color Holes** This command will color all same sized holes in a part and tell you what nominal size they might be based on the defaults in common CAD software.
7. **Update Tools from Library** This command in the Manufacturing workspace will replace tools in you document with identical tools form a library that they came from.

## Installation
The current recommended way to install this Add-In is to use Github Desktop to clone the repository to your local machine and then use the `Add-Ins` dialog in Fusion 360 to install it.
1. Click on the green `Code` button and select `Open with Github Desktop`.
2. When prompted to download Github Desktop, do so and install it.
3. Once installed, the repository should open in Github Desktop.
4. Click the `Clone` button to clone the repository to your local machine.
5. Once the repository is cloned, open Fusion 360, fo to the `Utilities` tab, and click on the `Add-Ins` button in the toolbar.
6. In the `Add-Ins` dialog, click the green `+` button and navigate to the `Fusion-Essentials` folder in the repository you just cloned.
7. Select the `Fusion-Essentials` folder and click `Select Folder`.
8. Select the `Fusion-Essentials` add-in in the `Add-Ins` dialog and click `Run` to start the add-in.
9. If you want the add-in to start automatically when Fusion 360 starts, click the `Run on Startup` checkbox.


I plan to make it available in the Autodesk App Store in the future.

## License

Licensed under either of

- Apache License, Version 2.0 ([LICENSE-APACHE](LICENSE-APACHE) or
  http://www.apache.org/licenses/LICENSE-2.0)
- MIT license ([LICENSE-MIT](LICENSE-MIT) or http://opensource.org/licenses/MIT)

at your option.

### Contribution

Unless you explicitly state otherwise, any contribution intentionally submitted for inclusion in the
work by you, as defined in the Apache-2.0 license, shall be dual licensed as above, without any
additional terms or conditions.