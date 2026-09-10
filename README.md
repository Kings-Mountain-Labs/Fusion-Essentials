# Fusion-Essentials

A set of quality-of-life improvements for your Fusion workflow, plus a local MCP server that lets an
AI assistant work inside your open Fusion session. Both ship in the same add-in, which runs inside
Fusion on your machine.

## Installation

Requires the **Autodesk Fusion desktop application**. Windows is the tested platform; the manifest
also targets macOS, but macOS support is unverified. Fusion supplies the Python runtime: using the
add-in does not require a separate Python, Node.js, or package installation.

1. Download and extract this repository, or clone it, into a folder named **Fusion-Essentials**.
   That folder must directly contain `Fusion-Essentials.py` and `Fusion-Essentials.manifest`, along
   with `commands/` and `lib/`; rename a ZIP's `Fusion-Essentials-main` folder if necessary.
2. In Fusion, open **Utilities > Add-Ins > Scripts and Add-Ins**, select the **Add-Ins** tab, and
   use **+** to register that folder's add-in. Select **Fusion-Essentials** and click **Run**.
   Fusion remembers the registered location. See Autodesk's [installation reference](https://help.autodesk.com/cloudhelp/ENU/Fusion-360-API/files/UsingSamplesFromGitHub_UM.htm)
   and [Add-Ins controls](https://help.autodesk.com/cloudhelp/ENU/Fusion-Model/files/SLD-MANAGE-SCRIPTS-ADD-INS.htm).
3. Open **Fusion Essentials Settings** in the Design workspace to choose features. Changes take
   effect after **Stop**, then **Run**, in the Add-Ins dialog. MCP remains off until you enable it.

**Update:** Stop the add-in, replace its files with the new download (or pull changes in your clone),
then Run it again. Keep the same folder name and location. Settings are stored separately, on Windows
in `%USERPROFILE%\AppData\Roaming\GTF_Fusion-Essentials\FusionEssentialsSettings.json`; back up that file before upgrading.

**Remove:** Stop the add-in, disable its startup option in the Add-Ins dialog, and remove its registered
entry and installation folder. Remove the server entry from any MCP client you configured. The settings
file is retained; delete it separately if you want to reset preferences before reinstalling.

## QoL features

1. **Add Tool Holder** This command provides a quick way to add a single-body toolholder to the tool library.
2. **Clean Chamfer** This command will take a set of surfaces that form an existing chamfer and turn them into a single freeform surface with the isocurves aligned to the original surfaces. This is useful for interpolating chamfers with a ball endmill, although it is made largely obsolete by the Pencil operation.
3. **Automatically Enable Design History** This command will automatically enable design history for what it perceives to be a newly imported file.
4. **Automatically Switch Units** This command will automatically switch the units of a newly imported file to the units of the current document.
5. **Ability to Change Settings** You can enable/disable features or change the default units, and the settings persist between sessions.
6. **Color Holes** This command will color all same-sized holes in a part and tell you what nominal size they might be based on the defaults in common CAD software.
7. **Update Tools from Libraries** This command in the Manufacturing workspace will replace tools in your document with identical tools from the library they came from.

## The MCP server

Everything about the server - setup, connecting a client, the full tool list, permissions, and
why it is built the way it is - lives in the [MCP Server README](commands/mcpServer/README.md).
It is **off by default**.

The tools are workflow-agnostic. Each does a single job and assumes nothing about how your shop
works, so a repeatable procedure is something you assemble in your client out of whichever calls it
needs. Anything that changes the model reads the design back afterwards, so an LLM works like an
incremental designer rather than a script shotgun.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for contributor Python setup, offline unit tests, Fusion-backed
verification, and the MCP tool-authoring recipe.

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
